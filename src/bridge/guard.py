import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from bridge.claims import Holder, map_row_to_holder
from bridge.config import Config, GuardRule, state_dir
from bridge.db import transaction
from bridge.gitinfo import GitInfo, read_git_info, read_remote_repo
from bridge.procinfo import process_start_ms
from bridge.registry import PROC_START_TOLERANCE_MS
from bridge.review_github import GitHubError, fetch_merge_facts

# Shell-glob fragments for the hook's fast gate; spacing and `git -C dir push` must still reach Python.
DEFAULT_TRIGGERS = ("git*push", "gh*pr*merge", "gh*pr*close", "gh*api*merge")
_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")", "\n", ";;", "|&"})
_PUSH_CONFIG_RE = re.compile(r"^\s*(pushurl|pushremote|pushdefault|push)\s*=", re.IGNORECASE)
_PR_URL_RE = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
_API_MERGE_RE = re.compile(r"(?:^|/)repos/([\w.-]+/[\w.-]+)/pulls/(\d+)/merge$")
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WRAPPERS = frozenset({"command", "env", "time", "nohup", "exec"})
_GIT_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
_GH_MERGE_VALUE_OPTIONS = frozenset(
    {
        "-t",
        "--subject",
        "-b",
        "--body",
        "-F",
        "--body-file",
        "--match-head-commit",
        "-A",
        "--author-email",
        "-c",
        "--comment",
    }
)
GITHUB_STALE_S = 300
READY = "ready"
DRAFT = "a draft"
OWNER_COMMENTS_OPEN = "comments open"
NOT_APPROVED = "not approved"
CI_RED = "CI red"
CI_PENDING = "CI pending"
CONFLICTS = "conflicts"
BLOCKED_ON_GITHUB = "blocked on GitHub"
_CI_RED_STATES = ("FAILURE", "ERROR")
_PUSH_VALUE_OPTIONS = frozenset({"-o", "--push-option", "--repo", "--receive-pack", "--exec"})


@dataclass(frozen=True)
class Target:
    key: str
    """A claim key, or a prefix ending in ':' that matches every branch of a repo (push --all)."""
    action: str


def map_command_to_targets(
    command: str, cwd: str, rules: tuple[GuardRule, ...], unreadable: list[str] | None = None
) -> list[Target]:
    """Resources a shell command would unambiguously mutate, from its text alone (no network, no git calls).

    Anything ambiguous yields no target: a wrong denial stalls an agent, a miss only loses a safety net.
    A push or merge that yields none is added to unreadable, for checks that must fail closed instead.
    """
    targets: list[Target] = []
    current_dir: str | None = cwd
    branch_override: str | None = None
    conditional_state = False
    for tokens, joined_by in segment_command(command):
        if joined_by in (";", "\n", "&") and conditional_state:
            # A cd/checkout that ran only if an earlier command succeeded (or failed) leaves later,
            # unconditional commands in an unknown directory.
            current_dir, branch_override, conditional_state = None, None, False
        if not tokens:
            continue
        if tokens[0] == "cd" and len(tokens) > 1:
            if joined_by == "||" or current_dir is None:
                current_dir = None
            else:
                current_dir = _resolve(current_dir, tokens[1])
                conditional_state = conditional_state or joined_by == "&&"
            branch_override = None
            continue
        if current_dir is None:
            if unreadable is not None and _is_branch_change(tokens):
                unreadable.append(" ".join(tokens))
            continue
        switched = _read_switched_branch(tokens)
        if switched is not None:
            branch_override = switched
            conditional_state = conditional_state or joined_by in ("&&", "||")
            continue
        if tokens[0] == "gh":
            tokens = _move_gh_repo_option_last(tokens)
        found: list[Target] = []
        if tokens[0] == "git":
            found = _git_targets(tokens[1:], current_dir, branch_override)
        elif tokens[0] == "gh" and len(tokens) > 2 and tokens[1] == "pr" and tokens[2] in ("merge", "close"):
            found = _gh_pr_targets(tokens[2], tokens[3:], current_dir, branch_override)
        elif tokens[0] == "gh" and len(tokens) > 2 and tokens[1] == "api":
            found = _gh_api_merge_targets(tokens[2:])
        # A variable or substitution is expanded by the shell later; its literal text names no real branch.
        expanded = any("$" in t or "`" in t for t in tokens)
        if unreadable is not None and (not found or expanded) and _is_branch_change(tokens):
            unreadable.append(" ".join(tokens))
        targets.extend(found)
        text = " ".join(tokens)
        for rule in rules:
            # A rule guards running that tool, not mentioning it (echo, printf, grep, docs).
            if tokens[0] == re.split(r"[\s*]", rule.trigger)[0] and rule.pattern.search(text):
                targets.append(Target(rule.resource, "matches a guarded command"))
    return targets


def add_merge_base_targets(conn: sqlite3.Connection, targets: list[Target]) -> list[Target]:
    """Merging a PR also changes its base branch; the base comes from cached PR state (no network here)."""
    extra: list[Target] = []
    for target in targets:
        if target.action != "gh pr merge":
            continue
        if target.key.startswith("pr:"):
            row = conn.execute("SELECT data FROM enrichment WHERE ref = ?", (target.key,)).fetchone()
            rows = [row] if row else []
            repo = target.key.removeprefix("pr:").split("#")[0]
        else:
            # Merging by branch name: find the cached open PR whose head is that branch.
            repo, _, head = target.key.removeprefix("branch:").partition(":")
            rows = [
                r
                for r in conn.execute(
                    "SELECT data FROM enrichment WHERE ref LIKE ? AND data IS NOT NULL", (f"pr:{repo}#%",)
                )
                if json.loads(r["data"]).get("head") == head and json.loads(r["data"]).get("state") == "OPEN"
            ]
        for row in rows:
            base = json.loads(row["data"]).get("base") if row["data"] else None
            if isinstance(base, str) and base:
                extra.append(Target(f"branch:{repo}:{base}", "gh pr merge (base branch)"))
    return targets + extra


def find_conflicts(conn: sqlite3.Connection, targets: list[Target], conversation_id: str | None) -> list[Holder]:
    """Claims on the targets held by another conversation that still has a live session."""
    conflicts: dict[str, Holder] = {}
    for target in targets:
        if target.key.endswith(":"):
            where, args = "c.resource LIKE ? OR c.alias LIKE ?", [target.key + "%", target.key + "%"]
        else:
            where, args = "c.resource = ? OR c.alias = ?", [target.key, target.key]
        rows = conn.execute(
            f"""
            SELECT c.*, EXISTS (
                SELECT 1 FROM instances i WHERE i.conversation_id = c.holder_conversation AND i.ended_at IS NULL
            ) AS live
            FROM claims c WHERE ({where})
            """,
            args,
        ).fetchall()
        for row in rows:
            holder = map_row_to_holder(row)
            if holder.holder_conversation != conversation_id and _holder_is_running(conn, holder):
                conflicts[holder.resource] = holder
    return list(conflicts.values())


def _read_switched_branch(tokens: list[str]) -> str | None:
    if tokens[:1] != ["git"] or len(tokens) < 3 or tokens[1] not in ("checkout", "switch"):
        return None
    rest = tokens[2:]
    if rest[0] in ("-c", "-C", "-b", "-B", "--create", "--force-create", "--"):
        rest = rest[1:]
    return rest[0] if rest and not rest[0].startswith("-") else None


def _move_gh_repo_option_last(tokens: list[str]) -> list[str]:
    """gh takes -R anywhere, even before `pr merge`; the recognizers expect the verb right after gh."""
    kept: list[str] = []
    repo: list[str] = []
    i = 0
    while i < len(tokens):
        if tokens[i] in ("-R", "--repo") and i + 1 < len(tokens):
            repo = ["--repo", tokens[i + 1]]
            i += 2
            continue
        if tokens[i].startswith("--repo="):
            repo = ["--repo", tokens[i].split("=", 1)[1]]
        else:
            kept.append(tokens[i])
        i += 1
    return kept + repo


def _is_branch_change(tokens: list[str]) -> bool:
    """A git push or gh pr merge that may update a branch; a dry run or a tags-only push cannot."""
    if tokens[:1] == ["gh"]:
        return tokens[1:3] == ["pr", "merge"]
    if tokens[:1] != ["git"]:
        return False
    args = tokens[1:]
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in _GIT_VALUE_OPTIONS else 1
    if i >= len(args) or args[i] != "push":
        return False
    rest = args[i + 1 :]
    if "--dry-run" in rest or any(_short_flags_include(a, "n") for a in rest):
        return False
    positional: list[str] = []
    j = 0
    while j < len(rest):
        if rest[j] in _PUSH_VALUE_OPTIONS:
            j += 2
            continue
        if not rest[j].startswith("-"):
            positional.append(rest[j])
        j += 1
    refspecs = positional[1:]
    if refspecs and all(r.rpartition(":")[2].lstrip("+").startswith("refs/tags/") for r in refspecs):
        return False
    return not ("--tags" in rest and not refspecs)


def sync_claims_flag(conn: sqlite3.Connection) -> None:
    """The PreToolUse shell guard skips Python entirely unless this file exists: a claim, or a PR the owner's rule
    keeps from merging."""
    if conn.in_transaction:
        _write_claims_flag(conn)
        return
    # The write lock orders this against a start or stop, so a stale "nothing open" cannot delete a fresh flag.
    with transaction(conn):
        _write_claims_flag(conn)


def _write_claims_flag(conn: sqlite3.Connection) -> None:
    flag = claims_flag_path()
    # Any queued PR keeps the flag: a ready PR's merge must still reach the guard once the queue goes stale.
    blocked = conn.execute("SELECT 1 FROM review_prs WHERE closed_at IS NULL LIMIT 1").fetchone()
    if blocked or conn.execute("SELECT 1 FROM claims LIMIT 1").fetchone():
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.touch()
    else:
        flag.unlink(missing_ok=True)


def claims_flag_path() -> Path:
    return state_dir() / "claims-active"


def _holder_is_running(conn: sqlite3.Connection, holder: Holder) -> bool:
    """Checks the holder's processes directly, so a crash the registry has not noticed yet cannot block anyone."""
    rows = conn.execute(
        "SELECT pid, proc_start_ms FROM instances WHERE conversation_id = ? AND ended_at IS NULL",
        (holder.holder_conversation,),
    ).fetchall()
    for row in rows:
        start = process_start_ms(int(row["pid"]))
        if start is not None and abs(start - int(row["proc_start_ms"])) <= PROC_START_TOLERANCE_MS:
            return True
    return False


def _git_targets(args: list[str], cwd: str, branch_override: str | None = None) -> list[Target]:
    directory = cwd
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i].startswith("-c") or args[i].startswith("--config-env"):
            # Inline config can redirect the push; give up rather than guess.
            return []
        if args[i] == "-C" and i + 1 < len(args):
            directory = _resolve(directory, args[i + 1])
        i += 2 if args[i] in _GIT_VALUE_OPTIONS else 1
    if i >= len(args) or args[i] != "push":
        return []
    git = read_git_info(directory)
    if git is None or _has_custom_push_config(git):
        return []
    current_branch = branch_override or git.branch
    positional: list[str] = []
    everything = False
    rest = args[i + 1 :]
    j = 0
    while j < len(rest):
        arg = rest[j]
        if arg in ("--dry-run", "--delete", "--tags", "--prune") or _short_flags_include(arg, "nd"):
            return []
        if arg in ("--all", "--mirror", "--branches"):
            everything = True
        elif arg in _PUSH_VALUE_OPTIONS:
            j += 1
        elif not arg.startswith("-"):
            positional.append(arg)
        j += 1
    # A bare push goes where the branch tracks, which is not always origin.
    tracked = _read_tracking_remote(git, current_branch) if current_branch else None
    remote = positional[0] if positional else tracked or "origin"
    repo = read_remote_repo(git, remote)
    if repo is None:
        # An explicit URL or a remote we cannot read: never substitute origin for it.
        return []
    repo = repo.lower()
    if everything:
        return [Target(f"branch:{repo}:", "git push --all")]
    refspecs = positional[1:]
    if not refspecs and _push_default(git) not in ("simple", "current", None):
        return []
    if not refspecs:
        return [Target(f"branch:{repo}:{current_branch}", "git push")] if current_branch else []
    targets: list[Target] = []
    for refspec in refspecs:
        if "*" in refspec or refspec.startswith(":"):
            return []
        destination = refspec.lstrip("+").split(":")[-1].removeprefix("refs/heads/")
        if destination == "HEAD":
            destination = current_branch or ""
        if destination.startswith("refs/") or not destination:
            return []
        if destination:
            targets.append(Target(f"branch:{repo}:{destination}", "git push"))
    return targets


def _gh_api_merge_targets(args: list[str]) -> list[Target]:
    """`gh api -X PUT repos/O/R/pulls/N/merge`; a GET there only asks whether it is merged."""
    put = any(
        (a in ("-X", "--method") and i + 1 < len(args) and args[i + 1].upper() == "PUT")
        or a.upper() in ("-XPUT", "--METHOD=PUT")
        for i, a in enumerate(args)
    )
    for arg in args:
        if put and (match := _API_MERGE_RE.search(arg)):
            return [Target(f"pr:{match.group(1).lower()}#{match.group(2)}", "gh pr merge")]
    return []


@dataclass(frozen=True)
class MergeFacts:
    draft: bool
    approved: bool
    """A colleague's standing approval or the review bot's."""
    checks: str | None
    """The head commit's CI rollup; None when it has no CI at all."""
    merge_state: str | None
    merge_block: str | None
    """The owner's open comments, as the review queue last read them."""


@dataclass(frozen=True)
class MergeRefusal:
    ref: str
    waits: tuple[str, ...]
    merge_block: str | None
    error: str | None = None


def list_merge_waits(facts: MergeFacts) -> tuple[str, ...]:
    """What keeps a PR from merging under the owner's rule; empty: its lead may merge it. A head commit with no CI at
    all counts as green, as on repos without PR checks."""
    waits: list[str] = []
    if facts.draft:
        waits.append(DRAFT)
    if facts.merge_block:
        waits.append(OWNER_COMMENTS_OPEN)
    if not facts.approved:
        waits.append(NOT_APPROVED)
    if facts.checks in _CI_RED_STATES:
        waits.append(CI_RED)
    elif facts.checks not in (None, "SUCCESS"):
        waits.append(CI_PENDING)
    if facts.merge_state == "DIRTY":
        waits.append(CONFLICTS)
    # Branch protection can want more than the rule does, such as a person's approval where the review bot's does not
    # count: GitHub would refuse the merge.
    if facts.merge_state == "BLOCKED":
        waits.append(BLOCKED_ON_GITHUB)
    return tuple(waits)


def map_row_to_merge_facts(row: sqlite3.Row, merge_block: str | None) -> MergeFacts:
    return MergeFacts(
        draft=bool(row["draft"]),
        approved=bool(row["bot_approved"]) or bool(json.loads(str(row["approved_by"] or "[]"))),
        checks=row["checks"],
        merge_state=row["merge_state"],
        merge_block=merge_block,
    )


def find_merge_blocks(conn: sqlite3.Connection, config: Config, targets: list[Target], now: int) -> list[MergeRefusal]:
    """Merges the owner's rule forbids. A PR the queue's fresh cache finds ready passes on that; any other is read
    from GitHub once before it is refused, and a PR whose state cannot be read is refused. A merge not mapped to a
    queued PR passes: this guard only knows the owner's PRs."""
    rows: dict[str, sqlite3.Row] = {}
    for target in targets:
        if target.action != "gh pr merge":
            continue
        if target.key.startswith("pr:"):
            found = conn.execute(
                "SELECT * FROM review_prs WHERE lower(ref) = ? AND closed_at IS NULL", (target.key.lower(),)
            ).fetchall()
        else:
            repo, _, head = target.key.removeprefix("branch:").partition(":")
            found = conn.execute(
                "SELECT * FROM review_prs WHERE lower(repo) = ? AND head = ? AND closed_at IS NULL",
                (repo.lower(), head),
            ).fetchall()
        rows.update({str(r["ref"]): r for r in found})
    if not rows:
        return []
    poll = conn.execute("SELECT ok_at FROM review_poll WHERE id = 1").fetchone()
    fresh = poll is not None and poll["ok_at"] is not None and now - int(poll["ok_at"]) <= GITHUB_STALE_S * 1000
    refusals: list[MergeRefusal] = []
    for ref, row in rows.items():
        facts = map_row_to_merge_facts(row, row["merge_block"])
        # A fresh cache may still be one poll behind a CI rerun or an approval, so a refusal is confirmed live first.
        cached_waits = list_merge_waits(facts) if fresh else ()
        if not fresh or cached_waits:
            try:
                draft, approved, checks, merge_state, theirs = fetch_merge_facts(
                    config, str(row["repo"]), int(row["number"])
                )
            except GitHubError as exc:
                # A fresh cache's own reasons stand when GitHub can't confirm them.
                refusals.append(
                    MergeRefusal(ref, cached_waits, facts.merge_block)
                    if cached_waits
                    else MergeRefusal(ref, (), row["merge_block"], str(exc))
                )
                continue
            # Threads are read live; an unanswered comment is known only from the queue's last read.
            kept = [r for r in str(row["merge_block"] or "").split("; ") if r and "review thread" not in r]
            live = [f"{theirs} review thread{'s are' if theirs > 1 else ' is'} unresolved"] if theirs else []
            block = "; ".join([*live, *kept]) or None
            facts = MergeFacts(draft, approved, checks, merge_state, block)
        waits = list_merge_waits(facts)
        if waits:
            refusals.append(MergeRefusal(ref, waits, facts.merge_block))
    return refusals


def map_merge_blocks_to_lines(refusals: list[MergeRefusal], owner: str, owner_at_start: str) -> list[str]:
    rule = (
        f"{owner_at_start}'s rule: a PR merges once it is approved on GitHub (a colleague or the review bot), CI is "
        f"green or absent, it is not a draft and has no conflicts, and no comment or thread of {owner}'s is open; "
        f"settle {owner}'s comments first (fix or answer, and {owner} resolves the threads)."
    )
    lines: list[str] = []
    for refusal in refusals:
        pr = refusal.ref.removeprefix("pr:")
        if refusal.error:
            lines.append(
                f"bridge: {pr} cannot be checked for merging: GitHub could not be read ({refusal.error}), and the "
                f"review queue's copy is older than {GITHUB_STALE_S // 60} minutes. Retry once GitHub answers. {rule}"
            )
            continue
        reasons = [
            f"{owner}'s comments are open ({refusal.merge_block})" if wait == OWNER_COMMENTS_OPEN else wait
            for wait in refusal.waits
        ]
        lines.append(f"bridge: {pr} is not mergeable yet: {'; '.join(reasons)}. {rule}")
    return lines


def _gh_pr_targets(action: str, args: list[str], cwd: str, branch_override: str | None = None) -> list[Target]:
    repo: str | None = None
    positional: list[str] = []
    j = 0
    while j < len(args):
        arg = args[j]
        if arg in ("-R", "--repo") and j + 1 < len(args):
            repo = args[j + 1].removeprefix("https://").removeprefix("github.com/")
            j += 2
            continue
        if arg in _GH_MERGE_VALUE_OPTIONS:
            j += 2
            continue
        if arg.startswith("--repo="):
            repo = arg.split("=", 1)[1]
        elif not arg.startswith("-"):
            positional.append(arg)
        j += 1
    verb = f"gh pr {action}"
    selector = positional[0] if positional else None
    if selector and (match := _PR_URL_RE.search(selector)):
        return [Target(f"pr:{match.group(1).lower()}#{match.group(2)}", verb)]
    git = read_git_info(cwd)
    repo = (repo or (git.repo if git else None) or "").lower()
    if not repo:
        return []
    if selector and selector.lstrip("#").isdigit():
        return [Target(f"pr:{repo}#{selector.lstrip('#')}", verb)]
    branch = selector or branch_override or (git.branch if git else None)
    return [Target(f"branch:{repo}:{branch}", verb)] if branch else []


def segment_command(command: str, heredoc_bodies: list[tuple[int, str]] | None = None) -> list[tuple[list[str], str]]:
    """Simple commands, each with the operator that joined it to the previous one.

    Heredoc bodies are skipped; when heredoc_bodies is given, each body is appended to it with the index of
    the segment whose redirection opened it.

    A small shell lexer: quotes and backslashes are honoured, so a quoted or escaped `;` stays part of a
    word, adjacent quoted parts join into one word, and `#` starts a comment only at a word boundary.
    Unbalanced quotes return nothing: the shell would reject the command anyway.
    """
    segments: list[tuple[list[str], str]] = []
    words: list[str] = []
    word: list[str] = []
    in_word = False
    joined_by = ";"
    i = 0
    n = len(command)

    heredocs: list[tuple[str, bool, int]] = []
    quoted_start = False
    pending_operator: str | None = None

    def end_word() -> None:
        nonlocal in_word, quoted_start, pending_operator
        if in_word:
            text = "".join(word)
            if pending_operator is not None:
                heredocs.append((text, pending_operator == "<<-", len(segments)))
                pending_operator = None
            elif not quoted_start and text in ("<<", "<<-"):
                pending_operator = text
            elif not quoted_start and text.startswith("<<") and not text.startswith("<<<"):
                heredocs.append(
                    (
                        text.removeprefix("<<-") if text.startswith("<<-") else text[2:],
                        text.startswith("<<-"),
                        len(segments),
                    )
                )
            words.append(text)
            word.clear()
            in_word = False
            quoted_start = False

    def end_segment(operator: str) -> None:
        nonlocal words, joined_by
        end_word()
        segments.append((_strip_prefixes(words), joined_by))
        words, joined_by = [], operator

    while i < n:
        ch = command[i]
        if ch == "\\" and i + 1 < n:
            if command[i + 1] != "\n":
                word.append(command[i + 1])
                in_word = True
            i += 2
        elif ch == "'":
            close = command.find("'", i + 1)
            if close < 0:
                return []
            quoted_start = quoted_start or not in_word
            word.append(command[i + 1 : close])
            in_word = True
            i = close + 1
        elif ch == '"':
            quoted_start = quoted_start or not in_word
            j = i + 1
            while j < n and command[j] != '"':
                if command[j] == "\\" and j + 1 < n and command[j + 1] in '"\\$`':
                    word.append(command[j + 1])
                    j += 2
                    continue
                word.append(command[j])
                j += 1
            if j >= n:
                return []
            in_word = True
            i = j + 1
        elif ch in " \t":
            end_word()
            i += 1
        elif ch == "#" and not in_word:
            newline = command.find("\n", i)
            i = n if newline < 0 else newline
        elif ch == "\n":
            end_segment("\n")
            i += 1
            # Heredoc bodies are data, not commands: skip to each delimiter line.
            while heredocs:
                delimiter, strip_tabs, owner = heredocs.pop(0)
                body: list[str] = []
                while i < n:
                    newline = command.find("\n", i)
                    line = command[i:] if newline < 0 else command[i:newline]
                    i = n if newline < 0 else newline + 1
                    if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                        break
                    body.append(line)
                if heredoc_bodies is not None:
                    heredoc_bodies.append((owner, "\n".join(body)))
        elif command.startswith(("&&", "||", ";;", "|&"), i):
            end_segment(command[i : i + 2])
            i += 2
        elif ch in ";&|()":
            end_segment(ch)
            i += 1
        else:
            word.append(ch)
            in_word = True
            i += 1
    end_segment(";")
    return segments


def _short_flags_include(arg: str, letters: str) -> bool:
    return arg.startswith("-") and not arg.startswith("--") and any(ch in arg[1:] for ch in letters)


def _read_tracking_remote(git: GitInfo, branch: str) -> str | None:
    common = Path(git.git_dir)
    if (common / "commondir").is_file():
        common = (common / (common / "commondir").read_text().strip()).resolve()
    return _read_ini_value(common / "config", f'branch "{branch}"'.lower(), "remote")


def _push_default(git: GitInfo) -> str | None:
    """push.default from the repo config, else ~/.gitconfig; None when unset."""
    common = Path(git.git_dir)
    if (common / "commondir").is_file():
        common = (common / (common / "commondir").read_text().strip()).resolve()
    for path in (common / "config", Path.home() / ".gitconfig"):
        value = _read_ini_value(path, "push", "default")
        if value is not None:
            return value.lower()
    return None


def _read_ini_value(path: Path, section: str, key: str) -> str | None:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    inside = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            inside = stripped.strip("[]").strip().lower() == section
        elif inside and "=" in stripped and stripped.split("=", 1)[0].strip().lower() == key:
            return stripped.split("=", 1)[1].strip()
    return None


def _strip_prefixes(tokens: list[str]) -> list[str]:
    while tokens and (_ENV_ASSIGNMENT_RE.match(tokens[0]) or tokens[0] in _WRAPPERS):
        tokens = tokens[1:]
    return tokens


def _has_custom_push_config(git: GitInfo) -> bool:
    """pushurl, pushRemote, pushDefault or remote push refspecs make the destination non-obvious."""
    common = Path(git.git_dir)
    if (common / "commondir").is_file():
        common = (common / (common / "commondir").read_text().strip()).resolve()
    try:
        return any(_PUSH_CONFIG_RE.match(line) for line in (common / "config").read_text().splitlines())
    except OSError:
        return False


def _resolve(base: str, path: str) -> str:
    return os.path.normpath(os.path.join(base, os.path.expanduser(path)))
