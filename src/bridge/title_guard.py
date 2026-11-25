import os
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from itertools import pairwise
from pathlib import PurePosixPath
from urllib.parse import unquote

from bridge.agent_prefix import CLAUDE_PREFIX, LINEAR_ISSUE_TOOLS, TITLE_RULE, is_prefixed, strip_agent_prefix
from bridge.colleague_comments import SUBSTANTIVE, list_unhandled_colleague_comments
from bridge.config import Config
from bridge.gitinfo import RepoFacts, read_repo_facts
from bridge.guard import SHELL_KEYWORDS, SHELLS, RepoReader, Target, segment_command
from bridge.leads import find_lead_name_of_conversation
from bridge.refs import names_issue, needs_issue
from bridge.restricted import find_restricted_block, find_session_surface, list_touched, map_row_to_files

_HEREDOC_SUBSTITUTION_RE = re.compile(r"^\$\(\s*cat\s+<<-?\s*(['\"]?)(\w+)\1[^\n]*\n(.*?)\n\s*\2\s*\)$", re.DOTALL)
_GIT_GLOBAL_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
_GH_GLOBAL_VALUE_OPTIONS = frozenset({"-R", "--repo", "--hostname"})
_PR_URL_RE = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
_GH_FILL = frozenset({"--fill", "-f", "--fill-first", "--fill-verbose"})
_BODY_FILE_MAX_BYTES = 1_000_000
_GIT_DIFF_BUDGET_S = 2.5


@dataclass(frozen=True)
class _Options:
    """The value-taking options of one command, by long name and short letter, each mapped to one key."""

    longs: dict[str, str]
    shorts: dict[str, str]
    git: bool = False
    """git takes an unambiguous prefix of a long name, and keeps the '=' of -m=x in the value; pflag does neither."""


def _options(spec: str, git: bool = False) -> _Options:
    """From space-separated parts such as 'title/t': the key and long name first, then any short letter."""
    longs: dict[str, str] = {}
    shorts: dict[str, str] = {}
    for part in spec.split():
        names = part.split("/")
        for name in names:
            if len(name) == 1:
                shorts[name] = names[0]
            else:
                longs[name] = names[0]
    return _Options(longs, shorts, git)


_GH_GLOBAL = "repo/R hostname"
_GH_ISSUE_PR = _options(
    f"{_GH_GLOBAL} title/t body/b body-file/F base/B head/H assignee/a label/l milestone/m project/p reviewer/r "
    "template/T recover add-assignee remove-assignee add-label remove-label add-project remove-project add-reviewer "
    "remove-reviewer"
)
_GH_PR_ANSWER = _options(f"{_GH_GLOBAL} body/b body-file/F")
_GH_MERGE = _options(f"{_GH_GLOBAL} subject/t body/b body-file/F author-email/A match-head-commit")
_GH_API = _options(f"{_GH_GLOBAL} method/X header/H field/F raw-field/f input jq/q template/t preview/p cache")
_GIT_COMMIT = _options(
    "message/m file/F reuse-message/C reedit-message/c template/t author date fixup squash trailer cleanup "
    "pathspec-from-file",
    git=True,
)
_GIT_MERGE = _options("message/m file/F strategy/s strategy-option/X into-name cleanup", git=True)
_GH_LABEL = _options(f"{_GH_GLOBAL} name/n color/c description/d")
_API_LABELS_RE = re.compile(
    r"(?:^|/)repos/[^/\s]+/[^/\s]+/(?:(issues/[^/\s?]+)(/labels)?|labels)(?:/([^?\s]*))?/?(?:\?|$)"
)
_API_ANSWER_RE = re.compile(
    r"(?:https://api\.github\.com)?/?repos/([\w.-]+/[\w.-]+)/(pulls|issues)/(\d+)/(comments|reviews)(?:/(\d+)/replies)?/?(?:\?.*)?"
)
_GH_TITLE_COMMANDS = {
    (group, action): _GH_ISSUE_PR for group in ("pr", "issue") for action in ("create", "new", "edit")
}


def find_prefixed_titles(command: str) -> list[str]:
    """Titles a shell command would give a PR, an issue or a commit that start with the robot."""
    found: list[str] = []
    for tokens, _ in segment_command(command):
        found.extend(t for t in _map_tokens_to_titles(tokens) if is_prefixed(t))
    return found


def find_owner_label_writes(command: str, label: str) -> list[str]:
    """gh commands that would put the owner's label on a PR or issue, take it off, or edit or delete the label itself,
    also inside a loop, an if or a shell's -c script. Only commands that name it: one that clears every label is undone
    by the next poll."""
    wanted = label.strip().casefold()
    if not wanted:
        return []
    found: list[str] = []
    bodies: list[tuple[int, str]] = []
    for index, (tokens, _) in enumerate(segment_command(command, bodies)):
        while tokens and tokens[0] in SHELL_KEYWORDS:
            tokens = tokens[1:]
        if not tokens:
            continue
        program = PurePosixPath(tokens[0]).name
        if program in SHELLS:
            stdin = [text for owner, text in bodies if owner == index]
            scripts = [script for flag, script in pairwise(tokens[1:]) if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", flag)]
            found += [w for script in [*scripts, *stdin] for w in find_owner_label_writes(script, label)]
        elif program == "eval":
            found += find_owner_label_writes(" ".join(tokens[1:]), label)
        elif program == "gh" and _writes_label(tokens[1:], wanted, command.casefold()):
            found.append(" ".join(tokens))
    return found


def map_label_write_to_denial(label: str, owner: str, command: str) -> str:
    return (
        f"bridge: only {owner}'s Publish sets the label {label}: it tells the review bot {owner} reviewed the PR. "
        f"This command would change it ({command[:120]}). Leave that label alone."
    )


def _writes_label(args: list[str], wanted: str, text: str) -> bool:
    """text: the whole command, casefolded, for a value the shell or a request body fills in."""
    group, i = _read_positional(args, 0)
    if group == "api":
        found, positional = _scan(args[i:], _GH_API)
        methods = [value.upper() for key, value in found if key == "method"]
        fields = [value.partition("=") for key, value in found if key in ("field", "raw-field")]

        def field(*names: str) -> list[str]:
            return [value for name, _, value in fields if name in names]

        has_input = any(key == "input" for key, _ in found)
        method = methods[-1] if methods else ("POST" if fields or has_input else "GET")
        match = _API_LABELS_RE.search(positional[0]) if positional else None
        if match is None or method in ("GET", "HEAD"):
            return False
        issue, on_labels, name = match.groups()
        if issue and not on_labels:
            named = field("labels", "labels[]")
        elif issue:
            named = [unquote(name or ""), *field("labels", "labels[]")]
        elif name:
            named = [unquote(name), *field("new_name")]
        else:
            # A repo's labels endpoint with no name creates a label, which puts it on nothing.
            return False
        return _names_label(named, wanted, text) or (has_input and wanted in text)
    action, i = _read_positional(args, i)
    if group in ("pr", "issue") and action in ("create", "new", "edit"):
        found, _ = _scan(args[i:], _GH_ISSUE_PR)
        return _names_label([v for k, v in found if k in ("label", "add-label", "remove-label")], wanted, text)
    if group == "label" and action in ("edit", "delete", "create"):
        found, positional = _scan(args[i:], _GH_LABEL)
        # create changes the label only with --force, which updates one that exists.
        if action == "create" and not any(a in ("-f", "--force") for a in args[i:]):
            return False
        return _names_label([*positional[:1], *(v for k, v in found if k == "name")], wanted, text)
    return False


def _names_label(values: list[str], wanted: str, text: str) -> bool:
    """gh and the labels endpoint take several labels joined by commas; GitHub matches names ignoring case. A value the
    shell fills in counts when the command spells the label anywhere, as in LABEL=…; gh … "$LABEL"."""
    parts = [part.strip().casefold() for value in values for part in _unwrap(value).split(",")]
    return wanted in parts or (any("$" in part for part in parts) and wanted in text)


def find_prefixed_linear_title(tool: str, tool_input: dict[str, object]) -> str | None:
    title = tool_input.get("title")
    if tool in LINEAR_ISSUE_TOOLS and isinstance(title, str) and is_prefixed(title):
        return title
    return None


def map_titles_to_denial(titles: list[str]) -> str:
    shown = titles[0].strip().splitlines()[0][:120]
    return (
        f"bridge: {TITLE_RULE}. This would title something '{shown}'. Retitle it without the prefix "
        f"('{strip_agent_prefix(shown)[:120]}') and keep '{CLAUDE_PREFIX}' at the start of the body."
    )


def _map_tokens_to_titles(tokens: list[str]) -> list[str]:
    if not tokens:
        return []
    if tokens[0] == "git":
        return _git_titles(tokens[1:])
    if tokens[0] == "gh":
        return _gh_titles(tokens[1:])
    return []


def _git_titles(args: list[str]) -> list[str]:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in _GIT_GLOBAL_VALUE_OPTIONS else 1
    if i >= len(args) or args[i] not in ("commit", "merge"):
        return []
    found, _ = _scan(args[i + 1 :], _GIT_COMMIT if args[i] == "commit" else _GIT_MERGE)
    keys = [key for key, _ in found]
    # A fixup or squash commit takes its subject from the commit it names; -m only adds to the body.
    if "fixup" in keys or "squash" in keys:
        return []
    return [_first_line(value) for key, value in found if key == "message"][:1]


def _gh_titles(args: list[str]) -> list[str]:
    group, i = _read_positional(args, 0)
    if group == "api":
        found, positional = _scan(args[i:], _GH_API)
        methods = [value.upper() for key, value in found if key == "method"]
        # graphql fields are query variables, and a GET sends fields as query parameters: neither names a title.
        if positional[:1] == ["graphql"] or methods[-1:] == ["GET"]:
            return []
        fields = (value.partition("=") for key, value in found if key in ("field", "raw-field"))
        return [_unwrap(value) for name, eq, value in fields if name == "title" and eq]
    action, i = _read_positional(args, i)
    if (group, action) == ("pr", "merge"):
        found, _ = _scan(args[i:], _GH_MERGE)
        return [_first_line(value) for key, value in found if key == "subject"]
    options = _GH_TITLE_COMMANDS.get((group or "", action or ""))
    if options is None:
        return []
    found, _ = _scan(args[i:], options)
    return [_unwrap(value) for key, value in found if key == "title"]


def _read_positional(args: list[str], start: int) -> tuple[str | None, int]:
    """The next word that is no option, and the index after it."""
    i = start
    while i < len(args):
        if args[i] in _GH_GLOBAL_VALUE_OPTIONS:
            i += 2
        elif args[i].startswith("-"):
            i += 1
        else:
            return args[i], i + 1
    return None, i


def _scan(args: list[str], options: _Options) -> tuple[list[tuple[str, str]], list[str]]:
    """Every value-taking option with its value, in order, and the positional words. A value is consumed with its
    option, so a body that reads like an option is never taken for one; an unknown option counts as a switch."""
    found: list[tuple[str, str]] = []
    positional: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == "--":
            positional.extend(args[i:])
            break
        if arg.startswith("--"):
            name, eq, value = arg[2:].partition("=")
            key = _match_long(name, options)
            if key is None:
                continue
            if not eq:
                if i >= len(args):
                    break
                value, i = args[i], i + 1
            found.append((key, value))
        elif arg.startswith("-") and len(arg) > 1:
            for k, letter in enumerate(arg[1:], start=1):
                key = options.shorts.get(letter)
                if key is None:
                    continue
                value = arg[k + 1 :]
                if value and not options.git:
                    value = value.removeprefix("=")
                elif not value and i < len(args):
                    value, i = args[i], i + 1
                found.append((key, value))
                break
        else:
            positional.append(arg)
    return found, positional


def _match_long(name: str, options: _Options) -> str | None:
    if name in options.longs:
        return options.longs[name]
    if not options.git or len(name) < 2:
        return None
    matches = {key for long, key in options.longs.items() if long.startswith(name)}
    return matches.pop() if len(matches) == 1 else None


def _unwrap(value: str) -> str:
    """The text a `$(cat <<'EOF' … EOF)` substitution or a $'…' string would yield; any other value as written."""
    match = _HEREDOC_SUBSTITUTION_RE.match(value.strip())
    if match:
        return match.group(3)
    # The lexer keeps the $ of $'…' before its text; Bash drops it and turns \n into a newline.
    if re.match(r"\$[^\w({]", value):
        return value[1:].replace("\\n", "\n")
    return value


def _first_line(value: str) -> str:
    text = _unwrap(value).lstrip("\n")
    return text.split("\n", 1)[0]


@dataclass(frozen=True)
class _Text:
    text: str
    known: bool
    """False when the shell or a file the guard cannot read fills it in: it may name an issue."""


def find_missing_issue_refusal(
    conn: sqlite3.Connection,
    config: Config,
    command: str,
    cwd: str | None,
    read_repo: RepoReader | None = None,
    read_files: bool = True,
) -> str | None:
    """Why a gh pr create, or a gh pr edit of a PR's body, would leave a review_org PR naming no issue in its branch,
    title or body, or aimed at a base other than main or a release branch; None when it is fine or the guard cannot
    tell. read_files is False where the command runs on
    another machine: a body file there is not read here."""
    if not config.issue_prefixes or not config.review_org or "gh" not in command:
        return None
    for call in _list_gh_calls(command, cwd):
        refusal = _check_pr_issue(
            conn,
            config,
            call.args,
            call.env,
            call.directory,
            read_repo or read_repo_facts,
            command if read_files else "",
            call.branch_moved,
        )
        if refusal:
            return refusal
    return None


@dataclass(frozen=True)
class _GhCall:
    args: list[str]
    env: dict[str, str]
    directory: str | None
    branch_moved: bool


def _list_gh_calls(command: str, cwd: str | None) -> list[_GhCall]:
    """Each gh command's arguments, its env prefix and the directory it runs in, when a leading cd surely set it."""
    calls: list[_GhCall] = []
    directory = cwd
    branch_moved = False
    depth = 0
    leading_cds = True
    prefixes: list[list[str]] = []
    for index, (tokens, joined_by) in enumerate(segment_command(command, prefixes=prefixes, redirections=[])):
        depth += {"(": 1, ")": -1}.get(joined_by, 0)
        while tokens and tokens[0] in SHELL_KEYWORDS:
            tokens = tokens[1:]
        if not tokens:
            continue
        if tokens[0] == "cd":
            # Only a cd that surely ran and still holds: one of the cds the command starts with, outside a subshell.
            sure = leading_cds and depth == 0 and joined_by in (";", "&&", "\n")
            directory = _change_dir(directory, tokens[1:]) if sure else None
            continue
        leading_cds = False
        if tokens[0] == "git" and any(word in ("checkout", "switch", "branch") for word in tokens[1:]):
            branch_moved = True
        elif tokens[0] == "gh":
            env = dict(w.split("=", 1) for w in prefixes[index] if "=" in w)
            calls.append(_GhCall(tokens[1:], env, directory, branch_moved))
    return calls


def _check_pr_issue(
    conn: sqlite3.Connection,
    config: Config,
    args: list[str],
    env: dict[str, str],
    directory: str | None,
    read_repo: RepoReader,
    command: str,
    branch_moved: bool,
) -> str | None:
    """command: the whole text, to tell a body file it may write first; empty where files are not read."""
    found, positional = _scan(args, _GH_ISSUE_PR)
    if positional[:1] != ["pr"] or len(positional) < 2 or positional[1] not in ("create", "new", "edit"):
        return None
    if any(a in ("-h", "--help") for a in args):
        return None
    values: dict[str, list[str]] = {}
    for key, value in found:
        values.setdefault(key, []).append(value)
    facts = read_repo(directory) if directory else None
    repo = _read_repo(values.get("repo", [])[-1:] or [env.get("GH_REPO", "")], facts)
    selector = positional[2:3]
    if positional[1] == "edit" and selector and (url := _PR_URL_RE.search(selector[0])):
        repo = str(url.group(1))
    if repo is None:
        return None
    if refusal := _check_pr_base(config, repo, values.get("base")):
        return refusal
    if not needs_issue(config, repo):
        return None
    title = _read_value(values.get("title"))
    body = _read_body(values, directory, command)
    if positional[1] == "edit":
        return _check_pr_edit(conn, config, repo, selector, None if branch_moved else facts, values, title, body)
    head_values = values.get("head")
    checkout_branch = facts.branch if facts and not branch_moved else None
    head = head_values[-1].rpartition(":")[2] if head_values else checkout_branch
    if names_issue(config.issue_prefixes, head or "", title.text, body.text):
        return None
    if head is None or _is_shell_value(head) or not (title.known and body.known):
        return None
    fill = any(a in _GH_FILL for a in args) and not (values.get("body") or values.get("body-file"))
    return map_missing_issue_to_refusal(config, f"its branch {head}, title or body", fill)


def _check_pr_base(config: Config, repo: str, bases: list[str] | None) -> str | None:
    """A review_org PR targets main, master or a release branch. One that builds on another PR joins a native GitHub
    stack through `gh stack link`, which creates it there, so GitHub retargets it to main once the PR below merges; a
    plain PR on another PR's branch only shows 'can be stacked'. Without --base the default branch is used."""
    if not bases or repo.partition("/")[0].casefold() != config.review_org.casefold():
        return None
    base = bases[-1]
    if base in ("main", "master") or base.startswith("release/") or _is_shell_value(base):
        return None
    return (
        f"bridge: base {base} is not main or a release/ branch. A PR goes to main; one that builds on another PR "
        f"joins a GitHub stack instead: `gh stack link <bottom PR or branch> … <your branch>` creates or links it."
    )


def _check_pr_edit(
    conn: sqlite3.Connection,
    config: Config,
    repo: str,
    selector: list[str],
    facts: RepoFacts | None,
    values: dict[str, list[str]],
    title: _Text,
    body: _Text,
) -> str | None:
    """Only an edit that replaces the body is judged: the body before it is not stored, so it may hold the key."""
    if not (values.get("body") or values.get("body-file")) or not body.known:
        return None
    row = _find_queue_row(conn, repo, selector[0] if selector else (facts.branch if facts else None))
    if row is None or row["issue_missing"]:
        return None
    new_title = title.text if values.get("title") else str(row["title"])
    if names_issue(config.issue_prefixes, str(row["head"]), new_title, body.text) or not title.known:
        return None
    pr = str(row["ref"]).removeprefix("pr:")
    return map_missing_issue_to_refusal(config, f"{pr}'s branch {row['head']}, title or new body", False)


def find_restricted_path_refusal(
    conn: sqlite3.Connection,
    config: Config,
    command: str,
    cwd: str | None,
    conversation_id: str | None,
    read_repo: RepoReader | None = None,
    read_files: bool = True,
) -> str | None:
    """Why a gh pr create, or a gh pr edit of a PR's body, would open a PR changing another team's restricted path
    without a short why and the assistant's OK in its body; None when it is fine or the guard cannot tell. A create's
    files are the checkout's diff from its base, so read only where the command runs here; an edit's are the queue's."""
    if not config.restricted_paths or "gh" not in command:
        return None
    deadline = time.monotonic() + _GIT_DIFF_BUDGET_S
    for call in _list_gh_calls(command, cwd):
        refusal = _check_pr_restricted(
            conn, config, call, conversation_id, read_repo or read_repo_facts, command if read_files else "", deadline
        )
        if refusal:
            return refusal
    return None


def _check_pr_restricted(
    conn: sqlite3.Connection,
    config: Config,
    call: "_GhCall",
    conversation_id: str | None,
    read_repo: RepoReader,
    command: str,
    deadline: float,
) -> str | None:
    found, positional = _scan(call.args, _GH_ISSUE_PR)
    if positional[:1] != ["pr"] or len(positional) < 2 or positional[1] not in ("create", "new", "edit"):
        return None
    if any(a in ("-h", "--help") for a in call.args):
        return None
    values: dict[str, list[str]] = {}
    for key, value in found:
        values.setdefault(key, []).append(value)
    replaces_body = bool(values.get("body") or values.get("body-file"))
    if positional[1] == "edit" and not replaces_body:
        return None
    # A create without one fills its body from commits the guard does not read: it is judged as empty.
    body = _read_body(values, call.directory, command)
    if not body.known:
        return None
    facts = read_repo(call.directory) if call.directory else None
    branch = facts.branch if facts and not call.branch_moved else None
    if positional[1] == "edit":
        selector = positional[2] if len(positional) > 2 else branch
        repo = _read_repo(values.get("repo", [])[-1:] or [call.env.get("GH_REPO", "")], facts)
        if selector and (url := _PR_URL_RE.search(selector)):
            repo = str(url.group(1))
        row = _find_queue_row(conn, repo, selector) if repo else None
        if row is None:
            return None
        files = map_row_to_files(row)
        branch = str(row["head"])
    else:
        if not command or call.directory is None or call.branch_moved:
            return None
        heads = values.get("head")
        head = heads[-1] if heads else None
        # A fork's branch (owner:branch) is not this checkout's.
        if (head and (":" in head or _is_shell_value(head))) or any(_is_shell_value(b) for b in values.get("base", [])):
            return None
        listed = _list_changed_files(call.directory, values.get("base", [""])[-1], head, deadline)
        if listed is None:
            return None
        files, branch = listed, head or branch
    touched = list_touched(config, files)
    if not touched:
        return None
    surface = find_session_surface(conn, conversation_id)
    block = find_restricted_block(conn, config, touched, body.text, branch, surface)
    return f"bridge: {block}" if block else None


def _list_changed_files(directory: str, base: str, head: str | None, deadline: float) -> list[str] | None:
    """The files a PR from head onto base (the remote's default when empty) changes; None when git cannot tell.
    Without head, the working tree counts: a commit earlier in the same command is not made yet."""
    bases = [f"origin/{base}"] if base else ["origin/HEAD", "origin/main", "origin/master"]
    for ref in bases:
        span = [f"{ref}...{head}"] if head else ["--merge-base", ref]
        try:
            done = subprocess.run(
                ["git", "-C", directory, "diff", "--name-only", "-z", "--no-renames", "--no-relative", *span, "--"],
                capture_output=True,
                text=True,
                timeout=max(0.1, deadline - time.monotonic()),
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if done.returncode == 0:
            return [name for name in done.stdout.split("\0") if name]
    return None


def find_colleague_reply_refusal(
    conn: sqlite3.Connection, config: Config, command: str, cwd: str | None, read_repo: RepoReader | None = None
) -> str | None:
    """Why a comment, review or thread reply on a review_org PR would answer a colleague's comment no call handles
    yet: a reply in its thread, or anything posted on the PR outside a thread. As of the queue's last read of GitHub;
    None when the guard cannot tell which PR or thread."""
    if not config.review_org or "gh" not in command:
        return None
    for call in _list_gh_calls(command, cwd):
        target = _map_gh_call_to_answer(conn, call, read_repo or read_repo_facts)
        if target is None:
            continue
        ref, thread = target
        if ref.removeprefix("pr:").partition("/")[0].casefold() != config.review_org.casefold():
            continue
        row = conn.execute(
            "SELECT ref FROM review_prs WHERE lower(ref) = ? AND closed_at IS NULL", (ref.lower(),)
        ).fetchone()
        if row is None:
            continue
        ref = str(row["ref"])
        if refusal := _check_colleague_answer(conn, config, ref, thread):
            return refusal
    return None


_GH_PR_CHANGES = frozenset({"edit", "comment"})
"""A push or merge is read from the shell guard's targets."""


def find_unclaimed_prs(
    conn: sqlite3.Connection,
    command: str,
    cwd: str | None,
    targets: list[Target],
    conversation_id: str,
    read_repo: RepoReader,
) -> list[str]:
    """Queued PRs this command pushes to, merges, edits or comments on, whose claim neither this session nor another
    under the same lead holds, and that it was not yet told about. Only queued PRs: the guard reads no GitHub."""
    rows: dict[str, sqlite3.Row] = {}
    for target in targets:
        if target.action.startswith("git push") or target.action == "gh pr merge":
            rows.update((str(row["ref"]), row) for row in _list_target_queue_rows(conn, target.key))
    for call in _list_gh_calls(command, cwd):
        if any(a in ("-h", "--help") for a in call.args):
            continue
        # Its value options hold gh pr comment's: a -R before the subcommand is read as an option, not as one.
        found, positional = _scan(call.args, _GH_ISSUE_PR)
        if positional[:1] != ["pr"] or positional[1:2] not in ([a] for a in _GH_PR_CHANGES):
            continue
        facts = read_repo(call.directory) if call.directory else None
        repo = _read_repo([v for k, v in found if k == "repo"][-1:] or [call.env.get("GH_REPO", "")], facts)
        selector = positional[2] if len(positional) > 2 else (facts.branch if facts and not call.branch_moved else None)
        if repo is None and selector and (url := _PR_URL_RE.search(selector)):
            repo = str(url.group(1))
        if repo and (row := _find_queue_row(conn, repo, selector)) is not None:
            rows[str(row["ref"])] = row
    if not rows:
        return []
    lead = find_lead_name_of_conversation(conn, conversation_id)
    unclaimed: list[str] = []
    for ref, row in rows.items():
        # A repo name is case-blind, a git branch is not: a claim key keeps the branch as written.
        pr, branch = ref.lower(), f"branch:{str(row['repo']).lower()}:{row['head']}"
        holders = [
            str(h[0])
            for h in conn.execute(
                # An ended holder's claim no longer keeps anyone off the PR, so it covers no one either.
                "SELECT holder_conversation FROM claims c WHERE (lower(resource) = ?1 OR lower(alias) = ?1 "
                "OR resource = ?2 OR alias = ?2) AND EXISTS (SELECT 1 FROM instances i "
                "WHERE i.conversation_id = c.holder_conversation AND i.ended_at IS NULL)",
                (pr, branch),
            )
        ]
        if conversation_id in holders or (
            lead and any(find_lead_name_of_conversation(conn, h) == lead for h in holders)
        ):
            continue
        told = conn.execute(
            "SELECT 1 FROM claim_events WHERE kind = 'nudged' AND conversation_id = ? AND resource = ?",
            (conversation_id, ref),
        ).fetchone()
        if told is None:
            unclaimed.append(ref)
    return unclaimed


def _list_target_queue_rows(conn: sqlite3.Connection, key: str) -> list[sqlite3.Row]:
    """A key ending in ':' (push --all or --mirror) is every branch of its repo."""
    if key.startswith("pr:"):
        return conn.execute(
            "SELECT * FROM review_prs WHERE lower(ref) = ? AND closed_at IS NULL", (key.lower(),)
        ).fetchall()
    repo, _, head = key.removeprefix("branch:").partition(":")
    return conn.execute(
        "SELECT * FROM review_prs WHERE lower(repo) = ? AND (head = ? OR ? = '') AND closed_at IS NULL",
        (repo.lower(), head, head),
    ).fetchall()


def _map_gh_call_to_answer(
    conn: sqlite3.Connection, call: _GhCall, read_repo: RepoReader
) -> tuple[str, str | None] | None:
    """(the PR's ref, the inline comment replied to or None for a post outside any thread) of a gh command that
    posts on a PR; None for any other command or one whose PR, thread or body the guard cannot read."""
    if any(a in ("-h", "--help") or a.startswith("--delete-last") for a in call.args):
        return None
    if call.args[:1] == ["api"]:
        return _map_api_call_to_answer(call, read_repo)
    found, positional = _scan(call.args, _GH_PR_ANSWER)
    if positional[:1] != ["pr"] or positional[1:2] not in (["comment"], ["review"]):
        return None
    keys = {key for key, _ in found}
    if positional[1] == "review" and not keys & {"body", "body-file"}:
        return None
    facts = read_repo(call.directory) if call.directory else None
    repo = _read_repo([v for k, v in found if k == "repo"][-1:] or [call.env.get("GH_REPO", "")], facts)
    branch = facts.branch if facts and not call.branch_moved else None
    selector = positional[2] if len(positional) > 2 else branch
    if repo is None and selector and (url := _PR_URL_RE.search(selector)):
        repo = str(url.group(1))
    row = _find_queue_row(conn, repo, selector) if repo else None
    return (str(row["ref"]), None) if row is not None else None


def _map_api_call_to_answer(call: _GhCall, read_repo: RepoReader) -> tuple[str, str | None] | None:
    found, positional = _scan(call.args[1:], _GH_API)
    if not positional:
        return None
    path = positional[0]
    if "{owner}" in path or "{repo}" in path:
        facts = read_repo(call.directory) if call.directory else None
        current = _read_repo([call.env.get("GH_REPO", "")], facts)
        if current is None:
            return None
        owner, _, name = current.partition("/")
        path = path.replace("{owner}", owner).replace("{repo}", name)
    match = _API_ANSWER_RE.fullmatch(path)
    fields = [v for k, v in found if k in ("field", "raw-field")]
    has_input = any(k == "input" for k, _ in found)
    methods = [v.upper() for k, v in found if k == "method"]
    method = methods[-1] if methods else ("POST" if fields or has_input else "GET")
    if match is None or method != "POST" or _is_shell_value(path):
        return None
    repo, group, number, kind, replied_to = match.groups()
    ref = f"pr:{repo}#{number}"
    if replied_to:
        return ref, f"p{replied_to}"
    named = dict(v.split("=", 1) for v in fields if "=" in v)
    if group == "pulls" and kind == "comments":
        parent = named.get("in_reply_to", "")
        return (ref, f"p{parent}") if not has_input and parent.isdigit() else None
    # With --input the fields go to the query string and the file is the body.
    if group == "pulls" and (has_input or not named.get("body")):
        return None
    return ref, None


def _check_colleague_answer(conn: sqlite3.Connection, config: Config, ref: str, thread: str | None) -> str | None:
    pending = list_unhandled_colleague_comments(conn, ref)
    if thread is not None:
        replied = conn.execute(
            "SELECT comment_id, parent_id FROM review_comments WHERE ref = ? AND comment_id = ?", (ref, thread)
        ).fetchone()
        if replied is None:
            return None
        root = replied["parent_id"] or replied["comment_id"]
        pending = [e for e in pending if root in (e["comment_id"], e["parent_id"])]
    if not pending:
        return None
    first = pending[0]
    url = first["url"]
    if first["status"] == SUBSTANTIVE:
        how = f"it requests changes: `guild comment draft {url}`"
    else:
        how = (
            f'`guild comment small {url} "what I did"` if it is a typo, rename or factual question; '
            f"else `guild comment draft {url}`"
        )
    more = f" ({len(pending) - 1} more like it on this PR: `guild comment list`)" if len(pending) > 1 else ""
    return (
        f"bridge: {first['author']}'s comment {url} is not handled: {how} ({config.owner_name} chooses; bridge "
        f"posts).{more}"
    )


def map_missing_issue_to_refusal(config: Config, where: str, fill: bool) -> str:
    keys = ", ".join(f"{p}-…" for p in config.issue_prefixes)
    team = config.issue_prefixes[0]
    filled = " With --fill only the branch name is read, not the commit messages: pass --body as well." if fill else ""
    return (
        f"bridge: this PR names no issue ({keys}) in {where}; its CI needs one. Find or create one first: "
        f'`guild linear search "<words>"` / `guild linear create --team {team} --title "…"`, then put '
        f"'Fixes {team}-N' in the body.{filled}"
    )


def _change_dir(directory: str | None, args: list[str]) -> str | None:
    if not args:
        return os.path.expanduser("~")
    if len(args) != 1 or args[0] == "-" or _is_shell_value(args[0]):
        return None
    path = os.path.expanduser(args[0])
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(directory, path)) if directory else None


def _read_repo(values: list[str], facts: RepoFacts | None) -> str | None:
    value = values[-1] if values else ""
    if _is_shell_value(value):
        return None
    if not value:
        return facts.repo if facts else None
    return value.removeprefix("https://").removeprefix("github.com/").strip("/")


def _read_value(values: list[str] | None) -> _Text:
    if not values:
        return _Text("", True)
    text = _unwrap(values[-1])
    return _Text(text, not _is_shell_value(text))


def _read_body(values: dict[str, list[str]], directory: str | None, command: str) -> _Text:
    files = values.get("body-file")
    if not files:
        return _read_value(values.get("body"))
    name = files[-1]
    # Named twice, the command may write it before gh reads it.
    if name == "-" or not command or _is_shell_value(name) or command.count(name) > 1:
        return _Text("", False)
    path = os.path.expanduser(name)
    if not os.path.isabs(path):
        if directory is None:
            return _Text("", False)
        path = os.path.join(directory, path)
    try:
        with open(path, encoding="utf-8", errors="replace") as file:
            return _Text(file.read(_BODY_FILE_MAX_BYTES), True)
    except OSError:
        return _Text("", False)


def _find_queue_row(conn: sqlite3.Connection, repo: str, selector: str | None) -> sqlite3.Row | None:
    if not selector or _is_shell_value(selector):
        return None
    number = selector.lstrip("#")
    if match := _PR_URL_RE.search(selector):
        repo, number = str(match.group(1)), str(match.group(2))
    if number.isdigit():
        ref = f"pr:{repo}#{number}"
        return conn.execute(
            "SELECT * FROM review_prs WHERE lower(ref) = ? AND closed_at IS NULL", (ref.lower(),)
        ).fetchone()
    return conn.execute(
        "SELECT * FROM review_prs WHERE lower(repo) = ? AND head = ? AND closed_at IS NULL", (repo.lower(), selector)
    ).fetchone()


def _is_shell_value(text: str) -> bool:
    return "$" in text or "`" in text
