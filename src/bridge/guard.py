import fnmatch
import json
import os
import re
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path, PurePosixPath

from bridge.agent_prefix import ROBOT
from bridge.backport import check_backport, is_backport_base
from bridge.claims import Holder, map_row_to_holder
from bridge.config import Config, state_dir
from bridge.db import transaction
from bridge.gitinfo import RepoFacts, read_repo_facts
from bridge.procinfo import process_start_ms
from bridge.registry import PROC_START_TOLERANCE_MS
from bridge.review_github import GitHubError, fetch_merge_facts

# Shell-glob fragments for the hook's fast gate; spacing and `git -C dir push` must still reach Python.
DEFAULT_TRIGGERS = (
    "git*push", "gh*pr*merge", "gh*pr*close", "gh*pr*update-branch", "gh*api*merge", "gh*api*pulls", "gh*api*issues",
    "gh*api*graphql", "gh*api*git/refs",
)  # fmt: skip
PUSH, MERGE, CLOSE, COMMAND = "push", "merge", "close", "command"
# A push or merge the guard would judge, written so it cannot: refused, to be written plainly.
WRAPPED = "wrapped"
_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")", "\n", ";;", "|&"})
_PR_URL_RE = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
_API_PULL_RE = re.compile(r"(?:^|/)repos/([^/\s]+)/([^/\s]+)/(pulls|issues)/([^/\s?]+)(/[^?\s]*)?")
_API_BRANCH_WRITE_RE = re.compile(r"(?:^|/)repos/[^/\s]+/[^/\s]+/(?:merges|git/refs|contents)(?:[/?]|$)")
_API_BRANCH_PATH_RE = re.compile(r"(?:^|/)repos/([\w.-]+)/([\w.-]+)/(merges|git/refs|contents)(/[^?\s]*)?")
_GRAPHQL_MUTATION_RE = re.compile(r"\bmutation\b", re.IGNORECASE)
# A state set through updatePullRequest or updateIssue may close the PR, also from a variable the guard cannot see.
_GRAPHQL_STATE_UPDATE_RE = re.compile(r"\bupdate(?:PullRequest|Issue)\s*\([^)]*(?:state|\$)", re.IGNORECASE)
_GRAPHQL_BRANCH_WRITE_RE = re.compile(
    r"\b(?:createRef|updateRefs?|deleteRef|createCommitOnBranch|updatePullRequestBranch)\b", re.IGNORECASE
)
_BRACE_EXPANSION_RE = re.compile(r"\{[^{}]*(?:,|\.\.)[^{}]*\}")
_QUOTED_TILDE_RE = re.compile(r"[\"'\\]~|~[^/\s]*[\"'\\]")
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# A redirection word: optional fd (or & for both outputs), operator, then its target unless that is the next word.
_REDIRECTION_RE = re.compile(r"(?:\d+|&)?(?:>>?|>\||<>?|>&|<&)(?!<)(.*)", re.DOTALL)
_TIMEOUT_DURATION_RE = re.compile(r"\d+(?:\.\d+)?[smhd]?")
# Variables that make git or gh act on another repo, config or program than the checkout's own.
_REPO_ENV_RE = re.compile(
    r"^(GH_REPO|GH_HOST|GH_CONFIG_DIR|GIT_DIR|GIT_WORK_TREE|GIT_COMMON_DIR|GIT_NAMESPACE|GIT_EXEC_PATH|GIT_CONFIG\w*"
    r"|HOME|XDG_CONFIG_HOME|PATH)="
)
_HARMLESS_ENV_RE = re.compile(
    r"^(GIT_TERMINAL_PROMPT|GH_PROMPT_DISABLED|NO_COLOR|CLICOLOR\w*|TERM|LANG|LC_\w+|TZ|GIT_AUTHOR_\w+"
    r"|GIT_COMMITTER_\w+|GIT_TRACE\w*|GH_DEBUG|HUSKY\w*|SKIP|PRE_COMMIT\w*)=|^(GIT_EDITOR|GIT_SEQUENCE_EDITOR|EDITOR"
    r"|VISUAL|GIT_PAGER|GH_PAGER|PAGER)=[\w./-]*$"
)
_QUOTING_RE = re.compile(r"[\"'\\]")
_WRAPPERS = frozenset({"command", "env", "time", "nohup", "exec"})
_SHELL_KEYWORDS = frozenset(
    {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "for", "case", "esac", "select", "function",
     "{", "}", "!", "[[", "]]", "coproc"}
)  # fmt: skip
# Programs that never run their arguments or input as code.
_INERT_COMMANDS = frozenset(
    {"echo", "printf", "true", "false", ":", "test", "[", "ls", "pwd", "cat", "head", "tail", "wc", "grep", "egrep",
     "fgrep", "uniq", "cut", "tr", "diff", "cmp", "jq", "sleep", "date", "which", "basename", "dirname",
     "realpath", "readlink", "stat", "du", "df", "mkdir", "touch", "cp", "mv", "rm", "ln", "chmod", "tee", "wait",
     "exit", "bridge", "gbridge"}
)  # fmt: skip
_GIT_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--attr-source"})
# `git -c` keys that neither route a push nor run a program.
_HARMLESS_CONFIG = ("user.", "color.", "advice.")
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
# env -S and its forms, with env already stripped: the string it splits into a command.
_ENV_SPLIT_RE = re.compile(r"(?:-[a-zA-Z0-9]*S|--split-string=?)(.*)", re.DOTALL)
# Programs that run the command in their words, after their options; each option matched takes the next word.
_COMMAND_RUNNERS = {
    "xargs": re.compile(r"-[0oprtx]*[nIJLPRSsdEa]"),
    "sudo": re.compile(
        r"-[A-Za-z]*[CDghpRrTtUu]|--(?:chdir|close-from|group|host|prompt|chroot|role|command-timeout|type|other-user|user)"
    ),
}
_FIND_EXECS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
# A remote named by url: scheme://, or scp-like host:path, which a remote name never is.
_URL_RE = re.compile(r"[a-z][a-z+]*://|[^/:]+:", re.IGNORECASE)
_GIT_REPO_OPTIONS = frozenset({"--git-dir", "--work-tree", "--namespace", "--exec-path", "--bare", "--super-prefix"})
_GIT_PLAIN_GLOBALS = frozenset(
    {"--no-pager", "-p", "-P", "--paginate", "--no-optional-locks", "--literal-pathspecs", "--no-replace-objects",
     "--glob-pathspecs", "--noglob-pathspecs", "--icase-pathspecs", "--no-advice"}
)  # fmt: skip
# push, checkout and switch are read for their targets; the rest push nothing and run no command named in their
# arguments, rebase --exec apart.
_GIT_READ_SUBCOMMANDS = frozenset(
    {"add", "am", "apply", "blame", "branch", "cat-file", "checkout", "cherry", "cherry-pick", "clean", "clone",
     "commit", "describe", "diff", "fetch", "for-each-ref", "format-patch", "fsck", "gc", "grep", "help", "init", "log",
     "ls-files", "ls-remote", "ls-tree", "merge", "merge-base", "mv", "name-rev", "notes", "pull", "push",
     "range-diff", "rebase", "reflog", "reset", "restore", "rev-list", "rev-parse", "revert", "rm", "shortlog", "show",
     "show-ref", "stash", "status", "switch", "symbolic-ref", "tag", "var", "version", "worktree"}
)  # fmt: skip
# Options that make a read subcommand run a program named in the command, or hooks from a template; git also takes
# any unambiguous prefix of them.
_GIT_RUNNING_OPTIONS = ("--exec", "--upload-pack", "--receive-pack", "--open-files-in-pager", "--extcmd", "--template")
# Short options that do the same, by subcommand; they may sit inside a cluster such as -nO.
_GIT_RUNNING_SHORTS = {"grep": "O", "rebase": "x", "clone": "uc"}
_GH_INERT_GROUPS = frozenset(
    {"api", "auth", "browse", "cache", "gist", "help", "issue", "label", "release", "run", "search", "status",
     "version", "workflow"}
)  # fmt: skip
# Not checkout: it moves the branch a later merge reads.
_GH_PR_INERT = frozenset(
    {"merge", "close", "update-branch", "create", "new", "view", "list", "ls", "status", "checks", "diff", "comment",
     "edit", "review", "ready", "reopen", "lock", "unlock", "revert"}
)  # fmt: skip
_GH_REPO_INERT = frozenset({"view", "list", "clone", "fork", "create"})
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
_GH_API_VALUE_SHORTS = frozenset("XfFHqtp")
# Short options of gh api, pr merge and pr close that take a value, which may look like -R.
_GH_VALUE_SHORTS = frozenset("XfFHqtpbAcR")
_GH_API_VALUE_LONGS = frozenset(
    {"--method", "--raw-field", "--field", "--header", "--input", "--jq", "--template", "--hostname", "--cache",
     "--preview", "--repo"}
)  # fmt: skip
GITHUB_STALE_S = 300
# The hook gets 5 s in all, and a hook that times out lets the command through.
MERGE_CHECK_BUDGET_S = 3.8
MERGE_FACTS_TIMEOUT_S = 3.0
MERGE_FACTS_MIN_S = 0.3
READY = "ready"
HELD = "held"
DRAFT = "a draft"
OWNER_COMMENTS_OPEN = "comments open"
NOT_APPROVED = "not approved"
NOT_OWNER_REVIEWED = "not reviewed by the owner"
CI_RED = "CI red"
CI_PENDING = "CI pending"
CONFLICTS = "conflicts"
BLOCKED_ON_GITHUB = "blocked on GitHub"
_CI_RED_STATES = ("FAILURE", "ERROR")
_PUSH_FLAGS = frozenset(
    {"--porcelain", "--follow-tags", "--no-follow-tags", "--signed", "--no-signed", "--atomic", "--no-atomic",
     "--force", "--force-with-lease", "--no-force-with-lease", "--force-if-includes", "--no-force-if-includes",
     "--set-upstream", "--thin", "--no-thin", "--quiet", "--verbose", "--progress", "--no-progress", "--verify",
     "--no-verify", "--ipv4", "--ipv6", "--no-recurse-submodules"}
)  # fmt: skip


type RepoReader = Callable[[str], RepoFacts | None]


@dataclass(frozen=True)
class HostCheckout:
    directory: str
    """Where gbridge read it from: a directory the command runs in, resolved as the guard resolves it."""
    facts: RepoFacts
    real_root: str
    """facts.repo_root with symlinks resolved on the host."""


@dataclass(frozen=True)
class HostRepoReader:
    """A host worker's checkouts as its gbridge reported them: each known only for the directory it was read from, and
    only if it lies under a readable root; nothing elsewhere, since any other directory may hold a checkout of its own
    or be reached through a symlink."""

    checkouts: tuple[HostCheckout, ...]
    roots: tuple[str, ...]
    """The host directories whose checkouts the guard reads: the worker's mapped root and its own worktree."""
    home: str | None = None
    """The host's home, for a ~ in the command; None reads no ~ at all."""
    real_roots: tuple[str, ...] = ()
    """The roots with symlinks resolved, as gbridge found them on the host."""

    def __call__(self, directory: str) -> RepoFacts | None:
        path = PurePosixPath(os.path.normpath(directory))
        for checkout in self.checkouts:
            root = PurePosixPath(os.path.normpath(checkout.facts.repo_root))
            if PurePosixPath(os.path.normpath(checkout.directory)) != path or not (
                path == root or root in path.parents
            ):
                continue
            roots = (*self.roots, *self.real_roots)
            if _is_under(checkout.facts.repo_root, roots) and _is_under(checkout.real_root, roots):
                return checkout.facts
        return None


def _is_under(path: str, roots: tuple[str, ...]) -> bool:
    if not path.startswith("/"):
        return False
    place = PurePosixPath(os.path.normpath(path))
    return any(place == r or r in place.parents for r in (PurePosixPath(os.path.normpath(r)) for r in roots))


def make_host_repo_reader(
    checkouts: list[HostCheckout],
    roots: tuple[str, ...],
    home: str | None = None,
    real_roots: dict[str, str] | None = None,
) -> HostRepoReader:
    """real_roots maps a root to its real path on the host; only those of the roots given count."""
    kept = tuple(r for r in roots if r.startswith("/"))
    real = tuple(v for k, v in (real_roots or {}).items() if k in kept and v.startswith("/"))
    return HostRepoReader(tuple(checkouts), kept, home, real)


@dataclass(frozen=True)
class Target:
    key: str
    """A claim key, or a prefix ending in ':' that matches every branch of a repo (push --all)."""
    action: str


@dataclass(frozen=True)
class Unreadable:
    text: str
    kind: str
    """PUSH, MERGE or CLOSE it may be, or COMMAND: on a worker host, a command the guard cannot read at all."""
    refused_on_mac: bool = False
    """Its text hides which PR it merges or closes: a way around the owner's merge rule, refused on the Mac too."""


def map_command_to_targets(
    command: str,
    cwd: str,
    unreadable: list[Unreadable] | None = None,
    read_repo: RepoReader = read_repo_facts,
    strict: bool = False,
    home: str | None = None,
) -> list[Target]:
    """Resources a shell command would unambiguously mutate, from its text alone (no network, no git calls).

    Anything ambiguous yields no target: a wrong denial stalls an agent, a miss only loses a safety net.
    A push, merge or close it cannot read is added to unreadable, for checks that must fail closed instead.
    strict is for a worker host: once the command mentions a trigger, every part of it must be one the guard reads,
    else it is added as COMMAND. home is then the host's, for a ~ in a path; unknown, a ~ reads nothing.
    """
    # A quoted ~ is literal to the shell, yet the lexer drops the quotes: then no ~ is read.
    host_home = (home if home and not _QUOTED_TILDE_RE.search(command) else "~") if strict else None
    targets: list[Target] = []
    current_dir: str | None = cwd
    # A cd to a path the shell builds: its literal text is still read, but the target it gives is a guess.
    dir_hidden = False
    branch_override: str | None = None
    branch_sure = True
    conditional_state = False
    moved = False
    bodies: list[tuple[int, str]] = []
    prefixes: list[list[str]] = []
    expanding: list[tuple[int, str]] = []
    redirections: list[list[str]] = []
    segments = segment_command(command, bodies, prefixes, expanding, redirections)
    strict = strict and _mentions_trigger(command)
    found: list[Unreadable] = unreadable if unreadable is not None else []
    mentioned_as_data = False
    unknown_programs: list[list[str]] = []

    def note(kind: str, words: list[str], refused_on_mac: bool = False) -> None:
        found.append(Unreadable(" ".join(words), kind, refused_on_mac))

    if strict and not segments:
        # The lexer could not read it, yet the shell may run it.
        note(COMMAND, [command])
    for index, (tokens, joined_by) in enumerate(segments):
        env = [w for w in prefixes[index] if _ENV_ASSIGNMENT_RE.match(w)]
        body = "\n".join(text for owner, text in bodies if owner == index)
        if (joined_by in (";", "\n", "&") and conditional_state) or (joined_by == "||" and moved):
            # A cd/checkout that ran only if an earlier command succeeded (or failed) leaves later,
            # unconditional commands in an unknown directory; one run because a command failed may follow a cd that
            # failed, or one that did not.
            current_dir, dir_hidden, branch_override, branch_sure, conditional_state = None, False, None, True, False
        hidden_run = any(
            _substitutes(t)
            for t in (*prefixes[index], *redirections[index], *tokens, *(b for o, b in expanding if o == index))
        )
        if strict and hidden_run:
            note(COMMAND, [*prefixes[index], *tokens])
            continue
        if not tokens:
            if strict and any(_REPO_ENV_RE.match(w) for w in env):
                note(COMMAND, env)
            continue
        if strict and (joined_by in ("(", ")") or tokens[0] in _SHELL_KEYWORDS):
            note(COMMAND, tokens)
            continue
        # A host already refuses these as commands it cannot read.
        piped = segments[index - 1][0][1:] if joined_by in ("|", "|&") else []
        piped_bodies = [text for owner, text in bodies if owner == index - 1] if joined_by in ("|", "|&") else []
        # What a pipe's producer writes is unknown: its words, each and together, stand in for it.
        stdin = [body, *piped_bodies, " ".join(piped), *piped]
        if not strict and (_runs_hidden_push(tokens, stdin) or _is_unplain_push(tokens)):
            note(WRAPPED, tokens, refused_on_mac=True)
            continue
        if tokens[0] == "cd":
            next_join = segments[index + 1][1] if index + 1 < len(segments) else None
            # In a pipeline, in the background or behind a wrapper such as env, cd moves no directory of this shell.
            if (
                joined_by in ("|", "|&")
                or next_join in ("&", "|", "|&")
                or any(not _ENV_ASSIGNMENT_RE.match(w) for w in prefixes[index])
            ):
                if strict:
                    note(COMMAND, [*prefixes[index], *tokens])
                continue
            destination = tokens[1] if len(tokens) > 1 else "~"
            hidden = (
                _is_expanded(destination)
                or destination == "-"
                or (host_home is not None and _has_inner_parent(destination))
            )
            # A redirection the shell cannot open skips the cd, and the command after it runs where it was.
            if strict and (len(tokens) > 2 or hidden or env or redirections[index]):
                note(COMMAND, tokens)
            moved = True
            if joined_by == "||" or current_dir is None:
                current_dir, dir_hidden = None, False
            else:
                current_dir = _resolve(current_dir, destination, host_home)
                dir_hidden = hidden or (dir_hidden and not destination.startswith(("/", "~")))
                conditional_state = conditional_state or joined_by == "&&"
            branch_override, branch_sure = None, True
            continue
        if tokens[0] == "gh":
            tokens = _move_gh_repo_option_last(tokens)
        noted = len(found)
        if current_dir is None:
            kind = _classify(tokens)
            if kind is not None and (kind != COMMAND or strict):
                note(kind, tokens)
        elif (switched := _read_switched_branch(tokens)) is not None:
            branch, branch_sure = switched
            if branch is not None:
                branch_override = branch
                conditional_state = conditional_state or joined_by in ("&&", "||")
        else:
            branch_unknown = strict and not branch_sure
            if tokens[0] == "git":
                pushed, unsure = _git_targets(
                    tokens[1:], env, current_dir, read_repo, branch_override, branch_unknown, host_home
                )
                # A variable or substitution is expanded by the shell later; its literal text names no real branch.
                expanded = any(_is_expanded(t) for t in tokens)
                if _is_branch_change(tokens) and (not pushed or unsure or expanded or dir_hidden):
                    note(PUSH, tokens)
                targets.extend(pushed)
            elif tokens[0] == "gh" and tokens[1:2] == ["pr"] and tokens[2:3] in (["merge"], ["close"]):
                kind = MERGE if tokens[2] == "merge" else CLOSE
                named, unsure, rewritten = _gh_pr_targets(
                    tokens[2], tokens[3:], env, current_dir, read_repo, branch_override, branch_unknown, dir_hidden
                )
                if not named or unsure or (strict and rewritten):
                    note(kind, tokens, refused_on_mac=unsure and kind == MERGE)
                targets.extend(named)
                if kind == CLOSE and any(a in ("-d", "--delete-branch") for a in tokens[3:]):
                    targets.extend(Target(t.key, "gh pr close --delete-branch") for t in named)
            elif tokens[0] == "gh" and tokens[1:2] == ["pr"] and tokens[2:3] == ["update-branch"]:
                named, unsure, rewritten = _gh_pr_targets(
                    "update-branch", tokens[3:], env, current_dir, read_repo, branch_override, branch_unknown,
                    dir_hidden,
                )  # fmt: skip
                if not named or unsure or (strict and rewritten):
                    note(PUSH, tokens)
                targets.extend(named)
            elif tokens[0] == "gh" and tokens[1:2] == ["api"]:
                named, kind, unsure = _gh_api_targets(tokens[2:])
                if kind in (MERGE, CLOSE) and (not named or unsure):
                    note(kind, tokens, refused_on_mac=True)
                elif kind == COMMAND and strict:
                    note(COMMAND, tokens)
                targets.extend(named)
        if strict and len(found) == noted:
            verdict = _read_strict_verdict(tokens, env, body, joined_by)
            if verdict == "unread":
                note(COMMAND, tokens)
            elif verdict == "data":
                mentioned_as_data = True
            elif verdict == "unknown":
                unknown_programs.append(tokens)
    if strict and mentioned_as_data and unknown_programs:
        # The trigger's text sits in data (echo, a heredoc) that a program the guard does not know may run.
        note(COMMAND, unknown_programs[0])
    return targets


def _mentions_trigger(text: str) -> bool:
    """A trigger, or the robot that the gate also sends to the guard for its title check."""
    return ROBOT in text or matches_trigger(text)


def matches_trigger(text: str) -> bool:
    """The shell gate's test, also on the text the shell runs once quotes and line continuations are gone."""
    plain = _QUOTING_RE.sub("", text.replace("\\\n", ""))
    globs = [convert_trigger_to_glob(t) for t in DEFAULT_TRIGGERS]
    return any(fnmatch.fnmatchcase(variant, glob) for glob in globs for variant in (text, plain))


def convert_trigger_to_glob(trigger: str) -> str:
    """An unquoted case pattern: `*` stays a wildcard, spaces match any run of characters."""
    if not re.fullmatch(r"[A-Za-z0-9 *_./-]+", trigger):
        raise ValueError(f"guard trigger {trigger!r} may only use letters, digits, spaces and * _ . / -")
    return "*" + "*".join(trigger.split()) + "*"


def _read_strict_verdict(tokens: list[str], env: list[str], body: str, joined_by: str) -> str | None:
    """None: read. data: an inert program whose text mentions a trigger. unknown: a program the guard does not know,
    fine unless the command's data mentions a trigger. unread: never fine in a command that mentions one."""
    head = tokens[0]
    if head in ("git", "gh"):
        if not all(_HARMLESS_ENV_RE.match(w) for w in env):
            return "unread"
        return None if (_git_is_read(tokens[1:]) if head == "git" else _gh_is_read(tokens[1:])) else "unread"
    if any(_REPO_ENV_RE.match(w) for w in (*env, *tokens)):
        return "unread"
    mentions = _mentions_trigger(" ".join([*env, *tokens, body]))
    if head in _INERT_COMMANDS:
        return "data" if mentions else None
    return "unread" if mentions or joined_by in ("|", "|&") else "unknown"


def _git_is_read(args: list[str]) -> bool:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        arg = args[i]
        if arg in ("-C", "-c") and i + 1 < len(args) and not _is_expanded(args[i + 1]):
            if arg == "-c" and not _is_harmless_config(args[i + 1]):
                return False
            i += 2
        elif (arg.startswith("-C") and len(arg) > 2 and not _is_expanded(arg)) or arg in _GIT_PLAIN_GLOBALS:
            i += 1
        else:
            return False
    if i >= len(args):
        return True
    sub, rest = args[i], args[i + 1 :]
    shorts = _GIT_RUNNING_SHORTS.get(sub, "")
    for arg in rest:
        if arg == "--":
            break
        name = arg.split("=", 1)[0]
        if arg.startswith("--") and len(name) > 3 and any(option.startswith(name) for option in _GIT_RUNNING_OPTIONS):
            return False
        if sub == "clone" and name == "--config":
            return False
        if not arg.startswith("--") and arg.startswith("-") and any(letter in arg[1:] for letter in shorts):
            return False
    return sub in _GIT_READ_SUBCOMMANDS


def _gh_is_read(args: list[str]) -> bool:
    if not args:
        return True
    if args[0] == "pr":
        return len(args) < 2 or args[1] in _GH_PR_INERT
    if args[0] == "repo":
        return len(args) < 2 or args[1] in _GH_REPO_INERT
    return args[0] in _GH_INERT_GROUPS


def _substitutes(word: str) -> bool:
    """Command or process substitution: the shell runs a command hidden inside the word."""
    return "$(" in word or "`" in word or "<(" in word or ">(" in word


def _is_expanded(word: str) -> bool:
    return "$" in word or "`" in word or bool(_BRACE_EXPANSION_RE.search(word))


def _runs_hidden_push(tokens: list[str], stdin: Sequence[str] = ()) -> bool:
    """A shell -c or -s, eval, xargs, sudo, find -exec or env -S that runs a push or merge the guard judges.

    stdin is the text fed to the command by a heredoc or a pipe, which a shell runs as its script.
    """
    head, words = PurePosixPath(tokens[0]).name, tokens[1:]
    runs: list[list[str]] = []
    scripts: list[str] = []
    if value_option := _COMMAND_RUNNERS.get(head):
        while words and words[0].startswith("-"):
            words = words[2:] if value_option.fullmatch(words[0]) else words[1:]
        # sudo -s or -i with no command runs a shell, which reads its input.
        runs.append(words if words and not words[0].startswith("<") else ["sh", *words])
    elif head == "find":
        runs += [words[k + 1 :] for k, word in enumerate(words) if word in _FIND_EXECS]
    elif head == "eval":
        scripts.append(" ".join(words))
    elif head in _SHELLS:
        scripts += [script for flag, script in pairwise(words) if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", flag)]
        here_strings = [w.removeprefix("<<<") or after for w, after in pairwise([*words, ""]) if w.startswith("<<<")]
        scripts += [*stdin, *here_strings]
    else:
        # env's options, with env already stripped; -S splits a string into the command.
        for k, word in enumerate(tokens[: next((k for k, w in enumerate(tokens) if not w.startswith("-")), None)]):
            if split := _ENV_SPLIT_RE.fullmatch(word):
                scripts.append(split.group(1) or (tokens[k + 1] if k + 1 < len(tokens) else ""))
    runs += [segment for script in scripts for segment, _ in segment_command(script)]
    for run in runs:
        while run and run[0] in _SHELL_KEYWORDS:
            run = run[1:]
        run = _strip_prefixes(run)
        run = [PurePosixPath(run[0]).name, *run[1:]] if run else run
        if run and (
            _classify(_move_gh_repo_option_last(run) if run[0] == "gh" else run)
            # xargs reads its input as arguments; what it runs gets none.
            or _runs_hidden_push(run, () if head == "xargs" else stdin)
        ):
            return True
    return False


def _is_unplain_push(tokens: list[str]) -> bool:
    """A git push behind a -c setting not known to be harmless, or to a url instead of a remote name."""
    if not _is_branch_change(tokens):
        return False
    args = tokens[1:]
    i = 0
    while args[i].startswith("-"):
        if args[i].startswith(("-c", "--config-env")) and not (args[i] == "-c" and _is_harmless_config(args[i + 1])):
            return True
        i += 2 if args[i] in _GIT_VALUE_OPTIONS else 1
    push = _parse_push_args(args[i + 1 :])
    remote = push.positional[0] if push.positional else push.repo
    return remote is not None and bool(_URL_RE.match(remote))


def _is_harmless_config(setting: str) -> bool:
    return setting.partition("=")[0].lower().startswith(_HARMLESS_CONFIG)


def _classify(tokens: list[str]) -> str | None:
    """What a command may do to a branch or PR, when where it runs is unknown; gh's -R already moved last."""
    if tokens[:1] == ["gh"]:
        if tokens[1:2] == ["pr"] and tokens[2:3] in (["merge"], ["close"]):
            return MERGE if tokens[2] == "merge" else CLOSE
        if tokens[1:2] == ["pr"] and tokens[2:3] == ["update-branch"]:
            return PUSH
        if tokens[1:2] == ["api"]:
            return _gh_api_targets(tokens[2:])[1]
        return None
    return PUSH if _is_branch_change(tokens) else None


def add_merge_base_targets(conn: sqlite3.Connection, targets: list[Target]) -> list[Target]:
    """Merging a PR also changes its base branch; the base comes from cached PR state (no network here)."""
    extra: list[Target] = []
    for target in targets:
        if target.action != "gh pr merge":
            continue
        if target.key.startswith("pr:"):
            row = conn.execute("SELECT data FROM enrichment WHERE lower(ref) = ?", (target.key.lower(),)).fetchone()
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


def _read_switched_branch(tokens: list[str]) -> tuple[str | None, bool] | None:
    """None for a command that is no checkout or switch; else the branch it may leave checked out, and whether that is
    certain: `git checkout x` may restore a file x instead, and a form not read leaves the branch unknown."""
    if tokens[:1] != ["git"]:
        return None
    i = 1
    while i < len(tokens) and tokens[i].startswith("-"):
        i += 2 if tokens[i] in _GIT_VALUE_OPTIONS else 1
    if i >= len(tokens) or tokens[i] not in ("checkout", "switch"):
        return None
    if i > 1:
        # -C, --git-dir and the like may switch another checkout, or this one.
        return None, False
    rest = tokens[2:]
    created = bool(rest) and rest[0] in ("-c", "-C", "-b", "-B", "--create", "--force-create")
    if rest and (created or rest[0] == "--"):
        rest = rest[1:]
    if not rest or rest[0].startswith("-"):
        return None, False
    return rest[0], (created or tokens[1] == "switch") and not _is_expanded(rest[0])


def _move_gh_repo_option_last(tokens: list[str]) -> list[str]:
    """gh takes -R anywhere, even before `pr merge`; the recognizers expect the verb right after gh. Another option's
    value is kept as it is, however much it looks like -R."""
    kept: list[str] = []
    repo: list[str] = []
    i = 0
    while i < len(tokens):
        arg = tokens[i]
        name, eq, _ = arg.partition("=")
        if name == "--repo":
            repo = ["--repo", arg.split("=", 1)[1] if eq else (tokens[i + 1] if i + 1 < len(tokens) else "")]
            i += 1 if eq else 2
            continue
        if arg.startswith("--"):
            kept.append(arg)
            if not eq and (name in _GH_MERGE_VALUE_OPTIONS or name in _GH_API_VALUE_LONGS) and i + 1 < len(tokens):
                kept.append(tokens[i + 1])
                i += 1
            i += 1
            continue
        letter, value = _read_short_cluster(arg, _GH_VALUE_SHORTS)
        if letter is None:
            kept.append(arg)
            i += 1
            continue
        taken = value is None and i + 1 < len(tokens)
        if value is None:
            value = tokens[i + 1] if taken else ""
        if letter == "R":
            # A cluster such as -dR keeps its other letters.
            rest = arg[1 : arg.index("R")]
            kept.extend([f"-{rest}"] if rest else [])
            repo = ["--repo", value]
        else:
            kept.extend([arg, tokens[i + 1]] if taken else [arg])
        i += 2 if taken else 1
    return kept + repo


def _read_short_cluster(arg: str, value_letters: frozenset[str]) -> tuple[str | None, str | None]:
    """A pflag cluster such as -iXPUT or -f=x: its first letter that takes a value, and that value when it is inside
    the word (None: it is the next word). (None, None) for a word with no such letter."""
    if not arg.startswith("-") or arg.startswith("--") or len(arg) < 2:
        return None, None
    for j, letter in enumerate(arg[1:], 1):
        if letter in value_letters:
            value = arg[j + 1 :]
            return letter, (value.removeprefix("=") if value else None)
    return None, None


def _is_branch_change(tokens: list[str]) -> bool:
    """A git push that may update a branch; a dry run or a tags-only push cannot."""
    if tokens[:1] != ["git"]:
        return False
    args = tokens[1:]
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in _GIT_VALUE_OPTIONS else 1
    if i >= len(args) or args[i] != "push":
        return False
    push = _parse_push_args(args[i + 1 :])
    if push.dry:
        return False
    refspecs = push.positional[1:]
    if refspecs and all(r.rpartition(":")[2].lstrip("+").startswith("refs/tags/") for r in refspecs):
        return False
    return not (push.tags and not refspecs)


@dataclass(frozen=True)
class _PushArgs:
    positional: list[str]
    repo: str | None = None
    everything: bool = False
    dry: bool = False
    delete: bool = False
    tags: bool = False
    prune: bool = False
    unsure: bool = False
    """An option the guard does not read, or one that runs another program or pushes submodules too."""


def _parse_push_args(args: list[str]) -> _PushArgs:
    positional: list[str] = []
    flags: set[str] = set()
    repo: str | None = None
    unsure = False
    i = 0
    while i < len(args):
        arg = args[i]
        name, eq, value = arg.partition("=")
        if arg == "--":
            positional.extend(args[i + 1 :])
            break
        if not arg.startswith("-") or arg == "-":
            positional.append(arg)
        elif name in ("--push-option", "--repo", "--receive-pack", "--exec", "--recurse-submodules"):
            if not eq:
                value = args[i + 1] if i + 1 < len(args) else ""
                i += 1
            if name == "--repo":
                # Read for the Mac's claim check; a host refuses it as asked of a push it cannot fully read.
                repo, unsure = value, True
            elif name != "--push-option" and not (name == "--recurse-submodules" and value in ("check", "no")):
                unsure = True
        elif name in ("--all", "--branches", "--mirror"):
            flags.add("everything")
        elif name in ("--dry-run", "--delete", "--tags", "--prune"):
            flags.add(name.removeprefix("--").removesuffix("-run"))
        elif name in _PUSH_FLAGS:
            pass
        elif arg.startswith("--"):
            unsure = True
        else:
            for j, letter in enumerate(arg[1:], 1):
                if letter == "o":
                    if j == len(arg) - 1:
                        i += 1
                    break
                if letter in "nd":
                    flags.add({"n": "dry", "d": "delete"}[letter])
                elif letter not in "fuqv46":
                    unsure = True
        i += 1
    return _PushArgs(
        positional, repo, "everything" in flags, "dry" in flags, "delete" in flags, "tags" in flags,
        "prune" in flags, unsure,
    )  # fmt: skip


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


def _git_targets(
    args: list[str],
    env: list[str],
    cwd: str,
    read_repo: RepoReader,
    branch_override: str | None = None,
    branch_unknown: bool = False,
    home: str | None = None,
) -> tuple[list[Target], bool]:
    """The branches a git push updates, and whether config, options or environment the guard does not read may send it
    elsewhere: the Mac still checks those targets, a worker host refuses the push."""
    directory = cwd
    unsure = any(_REPO_ENV_RE.match(w) for w in env)
    i = 0
    while i < len(args) and args[i].startswith("-"):
        arg = args[i]
        if arg == "-c" and i + 1 < len(args) and _is_harmless_config(args[i + 1]):
            pass
        elif arg.startswith("-c") or arg.startswith("--config-env"):
            # Inline config can redirect the push; give up rather than guess.
            return [], True
        elif arg == "-C" and i + 1 < len(args):
            directory = _resolve(directory, args[i + 1], home)
            unsure = unsure or _is_expanded(args[i + 1]) or (home is not None and _has_inner_parent(args[i + 1]))
        elif arg.startswith("-C"):
            directory = _resolve(directory, arg[2:], home)
            # The shell expands no ~ inside -C~/x.
            host_unsure = home is not None and (_has_inner_parent(arg[2:]) or arg[2:].startswith("~"))
            unsure = unsure or _is_expanded(arg) or host_unsure
        elif arg.split("=", 1)[0] in _GIT_REPO_OPTIONS:
            unsure = True
        i += 2 if arg in _GIT_VALUE_OPTIONS else 1
    if i >= len(args) or args[i] != "push":
        return [], False
    git = read_repo(directory)
    if git is None:
        return [], True
    push = _parse_push_args(args[i + 1 :])
    if push.dry or push.tags or push.prune:
        return [], unsure or push.unsure
    refspecs = push.positional[1:]
    # pushDefault, pushRemote and push refspecs only steer a push that leaves out the remote or the refspec.
    unsure = unsure or push.unsure or git.custom_remote or (git.custom_push and not refspecs)
    current_branch = None if branch_unknown else branch_override or git.branch
    # A bare push goes where the branch tracks, which is not always origin.
    tracked = git.tracking.get(current_branch) if current_branch else None
    remote = push.positional[0] if push.positional else push.repo or tracked or "origin"
    repo = git.remotes.get(remote)
    if repo is None:
        # An explicit URL or a remote we cannot read: never substitute origin for it.
        return [], True
    repo = repo.lower()
    if push.delete:
        # A deleted branch is changed as much as a pushed one: its claim holds it too.
        names = [r.removeprefix("refs/heads/") for r in refspecs]
        if not names or any(not n or ":" in n or "*" in n or n == "HEAD" or n.startswith("refs/") for n in names):
            return [], True
        return [Target(f"branch:{repo}:{n}", "git push --delete") for n in names], unsure
    if push.everything:
        return [Target(f"branch:{repo}:", "git push --all")], unsure
    if not refspecs and git.push_default not in ("simple", "current", None):
        return [], True
    if not refspecs:
        return ([Target(f"branch:{repo}:{current_branch}", "git push")] if current_branch else []), unsure
    targets: list[Target] = []
    for refspec in refspecs:
        if "*" in refspec or refspec.startswith(":"):
            return [], True
        destination = refspec.lstrip("+").split(":")[-1].removeprefix("refs/heads/")
        if destination == "HEAD":
            destination = current_branch or ""
        if destination.startswith("refs/") or not destination:
            return [], True
        targets.append(Target(f"branch:{repo}:{destination}", "git push"))
    return targets, unsure


def _gh_api_targets(args: list[str]) -> tuple[list[Target], str | None, bool]:
    """The PR a `gh api` call merges or closes; what it may do (MERGE, CLOSE, COMMAND for a branch write or a query
    the guard cannot read, None for nothing guarded); and whether the PR is unknown though it may be changed."""
    method, fields, path, has_input = _parse_gh_api_args(args)
    if path == "graphql" or path.endswith("/graphql"):
        field = next(((typed, v.split("=", 1)[1]) for typed, v in fields if v.startswith("query=")), None)
        if has_input or field is None or field[1].startswith("$") or (field[0] and field[1].startswith("@")):
            return [], COMMAND, True
        query = field[1]
        if _GRAPHQL_MUTATION_RE.search(query):
            if re.search(r"merge|enqueue", query, re.IGNORECASE):
                return [], MERGE, True
            if re.search(r"close", query, re.IGNORECASE) or _GRAPHQL_STATE_UPDATE_RE.search(query):
                return [], CLOSE, True
            if _GRAPHQL_BRANCH_WRITE_RE.search(query):
                return [], COMMAND, True
        return [], None, False
    if method in ("GET", "HEAD"):
        return [], None, False
    if match := _API_PULL_RE.search(path):
        owner, name, group, number, suffix = match.groups()
        known = number.isdigit() and not any(c in f"{owner}/{name}" for c in "{}$`")
        pr = f"pr:{owner.lower()}/{name.lower()}#{number}"
        suffix = (suffix or "").rstrip("/")
        if group == "pulls" and suffix == "/merge":
            return ([Target(pr, "gh pr merge")] if known and method == "PUT" else []), MERGE, not known
        if not suffix:
            states = [v for _, v in fields if v.startswith("state=")]
            if has_input or any(v != "state=open" for v in states):
                return ([Target(pr, "gh pr close")] if known else []), CLOSE, not known
            return [], None, False
        if group == "pulls" and suffix == "/update-branch":
            return ([Target(pr, "gh pr update-branch")] if known else []), COMMAND, True
        return [], None, False
    if method == "PUT" and re.search(r"/pulls/[^/\s]+/merge/?$", path):
        return [], MERGE, True
    if _API_BRANCH_WRITE_RE.search(path):
        return _map_api_branch_write_to_targets(path, fields, has_input), COMMAND, True
    if not path or _is_expanded(path):
        return [], COMMAND, True
    return [], None, False


def _map_api_branch_write_to_targets(path: str, fields: list[tuple[bool, str]], has_input: bool) -> list[Target]:
    """The branch a REST write names plainly: a merge into its base, a ref update, a file written on its branch. A
    branch it names in a body or a variable, or none (a file on the default branch), gives no target."""
    match = _API_BRANCH_PATH_RE.search(path)
    if match is None or _is_expanded(path):
        return []
    owner, name, kind, rest = match.groups()
    named = {} if has_input else dict(v.split("=", 1) for _, v in fields if "=" in v)
    rest = (rest or "").split("?")[0].strip("/")
    if kind == "merges":
        branch = named.get("base")
    elif kind == "contents":
        branch = named.get("branch")
    else:
        branch = rest.removeprefix("heads/") if rest.startswith("heads/") else None
    if not branch or _is_expanded(branch) or branch.startswith("@"):
        return []
    return [Target(f"branch:{owner.lower()}/{name.lower()}:{branch}", "gh api branch write")]


def _parse_gh_api_args(args: list[str]) -> tuple[str, list[tuple[bool, str]], str, bool]:
    """Method, fields (typed for -F, whose @file is read from disk), path, and whether a body comes from --input."""
    method: str | None = None
    fields: list[tuple[bool, str]] = []
    positional: list[str] = []
    has_input = False
    i = 0
    while i < len(args):
        arg = args[i]
        option, value = "", ""
        if arg.startswith("--"):
            name, eq, value = arg.partition("=")
            if name in _GH_API_VALUE_LONGS:
                option = name
                if not eq:
                    value = args[i + 1] if i + 1 < len(args) else ""
                    i += 1
        elif arg.startswith("-") and len(arg) > 1:
            letter, attached = _read_short_cluster(arg, _GH_API_VALUE_SHORTS)
            if letter is not None:
                option = "-" + letter
                value = attached if attached is not None else (args[i + 1] if i + 1 < len(args) else "")
                i += 1 if attached is None else 0
        else:
            positional.append(arg)
        if option in ("-X", "--method"):
            method = value.upper()
        elif option in ("-f", "--raw-field", "-F", "--field"):
            fields.append((option in ("-F", "--field"), value))
        elif option == "--input":
            has_input = True
        i += 1
    path = positional[0] if positional else ""
    return method or ("POST" if fields or has_input else "GET"), fields, path, has_input


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
    held: bool = False
    """Its lead holds it for the owner's own review."""
    owner_reviewed: bool = False
    """The owner pressed Publish or recorded a decision: no approval by a colleague or the bot stands in for it."""


@dataclass(frozen=True)
class MergeRefusal:
    ref: str
    waits: tuple[str, ...]
    merge_block: str | None
    error: str | None = None
    held_why: str | None = None
    backport: str | None = None
    """The backport check's verdict: why it needs no approval, or why it still does."""


def list_merge_waits(facts: MergeFacts) -> tuple[str, ...]:
    """What keeps a PR from merging under the owner's rule; empty: its lead may merge it. A head commit with no CI at
    all counts as green, as on repos without PR checks."""
    waits: list[str] = []
    if facts.held:
        waits.append(HELD)
    if facts.draft:
        waits.append(DRAFT)
    if facts.merge_block:
        waits.append(OWNER_COMMENTS_OPEN)
    if not facts.owner_reviewed:
        waits.append(NOT_OWNER_REVIEWED)
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
        approved=bool(row["bot_approved"])
        or bool(json.loads(str(row["approved_by"] or "[]")))
        or row["approval_waived_at"] is not None,
        checks=row["checks"],
        merge_state=row["merge_state"],
        merge_block=merge_block,
        held=row["held_at"] is not None,
        owner_reviewed=row["reviewed_at"] is not None or row["approval_waived_at"] is not None,
    )


def find_merge_blocks(
    conn: sqlite3.Connection, config: Config, targets: list[Target], now: int, waived: list[str] | None = None
) -> list[MergeRefusal]:
    """Merges the owner's rule forbids. A PR the queue's fresh cache finds ready passes on that; any other is read
    from GitHub once before it is refused, and a PR whose state cannot be read is refused. A merge not mapped to a
    queued PR passes: this guard only knows the owner's PRs. A backport (base in `backport_bases`) whose diff equals
    the merged default-branch PRs it names needs no approval; `waived` gets a line for each one that passes so."""
    deadline = time.monotonic() + MERGE_CHECK_BUDGET_S
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
        # Only the owner's review or its lead ends a hold, so GitHub has nothing to confirm.
        if row["held_at"] is not None:
            refusals.append(MergeRefusal(ref, (HELD,), row["merge_block"], held_why=row["held_why"]))
            continue
        facts = map_row_to_merge_facts(row, row["merge_block"])
        # A fresh cache may still be one poll behind a CI rerun or an approval, so a refusal is confirmed live first.
        cached_waits = list_merge_waits(facts) if fresh else ()
        if not fresh or cached_waits:
            try:
                left = deadline - time.monotonic()
                if left < MERGE_FACTS_MIN_S:
                    raise GitHubError("out of time before reading GitHub")
                draft, approved, checks, merge_state, his = fetch_merge_facts(
                    config, str(row["repo"]), int(row["number"]), min(left, MERGE_FACTS_TIMEOUT_S)
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
            live = [f"{his} review thread{'s are' if his > 1 else ' is'} unresolved"] if his else []
            block = "; ".join([*live, *kept]) or None
            # The owner's recorded waiver stands in for a GitHub approval; GitHub itself may not require one.
            facts = MergeFacts(
                draft,
                approved or row["approval_waived_at"] is not None,
                checks,
                merge_state,
                block,
                owner_reviewed=facts.owner_reviewed,
            )
        waits = list_merge_waits(facts)
        backport: str | None = None
        # A backport whose diff equals merged PRs carries their approval, never the owner's word on merging it.
        if NOT_APPROVED in waits and is_backport_base(config.backport_bases, str(row["base"])):
            verdict = check_backport(str(row["repo"]), int(row["number"]), config.backport_bases, deadline)
            if verdict is not None and verdict.waived:
                waits = tuple(w for w in waits if w != NOT_APPROVED)
                if not waits and waived is not None:
                    waived.append(f"bridge: {ref.removeprefix('pr:')} may merge: {verdict.reason}.")
            backport = verdict.reason if verdict is not None else None
        if waits:
            refusals.append(MergeRefusal(ref, waits, facts.merge_block, backport=backport))
    return refusals


def map_merge_blocks_to_lines(refusals: list[MergeRefusal], owner: str, owner_at_start: str) -> list[str]:
    rule = (
        f"{owner_at_start}'s rule: a PR merges once {owner} has reviewed it (Publish or a recorded decision) and it "
        "is approved on GitHub (a colleague or the review bot), CI is "
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
        reasons = [map_wait_to_reason(wait, refusal, owner) for wait in refusal.waits]
        waived = (
            f" It needs no approval: {refusal.backport}."
            if refusal.backport and NOT_APPROVED not in refusal.waits
            else ""
        )
        lines.append(f"bridge: {pr} is not mergeable yet: {'; '.join(reasons)}.{waived} {rule}")
    return lines


def map_wait_to_reason(wait: str, refusal: MergeRefusal, owner: str) -> str:
    if wait == NOT_APPROVED and refusal.backport:
        return (
            f"not approved ({refusal.backport}; a backport skips approval only when its diff equals that of the "
            "merged default-branch PRs it names)"
        )
    if wait == OWNER_COMMENTS_OPEN:
        return f"{owner}'s comments are open ({refusal.merge_block})"
    if wait == NOT_OWNER_REVIEWED:
        return (
            f"{owner} has not reviewed it (no Publish, no recorded decision); a colleague's or the bot's approval "
            "does not stand in"
        )
    if wait == HELD:
        return (
            f"held for {owner}'s review ({refusal.held_why}); the hold ends once {owner} comments or reviews on "
            "GitHub or it is published, or its lead runs guild review unhold"
        )
    return wait


# Every write to a branch the guard reads: a push, a merge into it as another PR's base, a GitHub-side update.
_BRANCH_WRITES = frozenset(
    {"git push", "git push --delete", "git push --all", "gh pr merge (base branch)", "gh pr update-branch",
     "gh pr close --delete-branch", "gh api branch write"}
)  # fmt: skip


@dataclass(frozen=True)
class HeldWrite:
    ref: str
    head: str
    held_at: int
    held_by: str | None
    held_why: str | None
    action: str


def find_held_writes(conn: sqlite3.Connection, targets: list[Target]) -> list[HeldWrite]:
    """Writes to the head branch of a PR held for the owner's review: while they read it, its fixes stay local."""
    found: dict[str, HeldWrite] = {}
    for target in targets:
        for repo, branch in _map_target_to_written_branches(conn, target):
            # An empty branch is push --all: every branch of the repo.
            rows = conn.execute(
                """
                SELECT * FROM review_prs WHERE lower(repo) = ? AND (head = ? OR ? = '')
                  AND held_at IS NOT NULL AND closed_at IS NULL
                """,
                (repo.lower(), branch, branch),
            ).fetchall()
            for row in rows:
                found.setdefault(
                    str(row["ref"]),
                    HeldWrite(
                        str(row["ref"]), str(row["head"]), int(row["held_at"]), row["held_by"], row["held_why"],
                        target.action,
                    ),
                )  # fmt: skip
    return list(found.values())


def _map_target_to_written_branches(conn: sqlite3.Connection, target: Target) -> list[tuple[str, str]]:
    """The (repo, branch) pairs a target writes: a PR named by number writes its head, and its merge its base, read
    from the queue or else from the enrichment cache."""
    if target.action not in _BRANCH_WRITES and target.action != "gh pr merge":
        return []
    merge = target.action == "gh pr merge"
    if target.key.startswith("branch:"):
        repo, _, branch = target.key.removeprefix("branch:").partition(":")
        if not merge:
            return [(repo, branch)]
        rows = conn.execute(
            "SELECT base FROM review_prs WHERE lower(repo) = ? AND head = ? AND closed_at IS NULL",
            (repo.lower(), branch),
        ).fetchall()
        return [(repo, str(row["base"])) for row in rows]
    repo = target.key.removeprefix("pr:").split("#")[0]
    row = conn.execute(
        "SELECT head, base FROM review_prs WHERE lower(ref) = ? AND closed_at IS NULL", (target.key.lower(),)
    ).fetchone()
    if row is not None:
        return [(repo, str(row["base"] if merge else row["head"]))]
    cached = conn.execute(
        "SELECT data FROM enrichment WHERE lower(ref) = ? AND data IS NOT NULL", (target.key.lower(),)
    ).fetchone()
    branch = json.loads(cached["data"]).get("base" if merge else "head") if cached else None
    return [(repo, branch)] if isinstance(branch, str) and branch else []


def map_held_writes_to_lines(held: list[HeldWrite], owner: str, now: int) -> list[str]:
    lines: list[str] = []
    for write in held:
        pr = write.ref.removeprefix("pr:")
        by = f" by {write.held_by}" if write.held_by else ""
        why = f": {write.held_why}" if write.held_why else ""
        lines.append(
            f"bridge: {write.head} is the branch of {pr}, held for {owner}'s review{by} "
            f"{_convert_ms_to_age(now - write.held_at)} ago{why}. Nothing is pushed to it while {owner} reads it "
            f"({write.action} would change it). Keep the fix in a local commit or on a local branch, and push it once "
            f"the hold ends: when {owner} comments or reviews on GitHub, or it is published, or its lead runs "
            f"`guild review unhold {pr}`."
        )
    return lines


def _convert_ms_to_age(ms: int) -> str:
    minutes = max(0, ms) // 60_000
    if minutes < 60:
        return f"{minutes} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h"
    return f"{minutes // 1440} d"


def _gh_pr_targets(
    action: str,
    args: list[str],
    env: list[str],
    cwd: str,
    read_repo: RepoReader,
    branch_override: str | None = None,
    branch_unknown: bool = False,
    dir_hidden: bool = False,
) -> tuple[list[Target], bool, bool]:
    """The PR a gh pr merge or close names; whether the shell or the environment may make it another one; and whether
    it was read from a checkout whose remote urls git config rewrites, as gh would see them."""
    repo: str | None = None
    positional: list[str] = []
    j = 0
    while j < len(args):
        arg = args[j]
        if arg in ("-R", "--repo") and j + 1 < len(args):
            repo = args[j + 1]
            j += 2
            continue
        if arg in _GH_MERGE_VALUE_OPTIONS:
            j += 2
            continue
        letter, attached = _read_short_cluster(arg, _GH_VALUE_SHORTS)
        if letter is not None:
            j += 1 if attached is not None else 2
            continue
        if arg.startswith("--repo="):
            repo = arg.split("=", 1)[1]
        elif not arg.startswith("-"):
            positional.append(arg)
        j += 1
    verb = f"gh pr {action}"
    selector = positional[0] if positional else None
    if selector and not _is_expanded(selector) and (match := _PR_URL_RE.search(selector)):
        return [Target(f"pr:{match.group(1).lower()}#{match.group(2)}", verb)], False, False
    assigned = dict(w.split("=", 1) for w in env)
    if repo is None and "GH_REPO" in assigned:
        repo = assigned["GH_REPO"]
    from_checkout = repo is None
    unsure = (
        "GH_HOST" in assigned
        or (selector is not None and _is_expanded(selector))
        or (repo is not None and _is_expanded(repo))
        # Another git dir or config makes gh read another checkout's remotes.
        or (from_checkout and any(_REPO_ENV_RE.match(w) for w in env))
        or ((from_checkout or not selector) and dir_hidden)
    )
    git = read_repo(cwd)
    rewritten = from_checkout and git is not None and git.custom_remote
    repo = (repo or (git.repo if git else None) or "").removeprefix("https://").removeprefix("github.com/").lower()
    if not repo:
        return [], unsure, rewritten
    if selector and selector.lstrip("#").isdigit():
        return [Target(f"pr:{repo}#{selector.lstrip('#')}", verb)], unsure, rewritten
    branch = selector or (None if branch_unknown else branch_override or (git.branch if git else None))
    return ([Target(f"branch:{repo}:{branch}", verb)] if branch else []), unsure, rewritten


def segment_command(
    command: str,
    heredoc_bodies: list[tuple[int, str]] | None = None,
    prefixes: list[list[str]] | None = None,
    expanding_bodies: list[tuple[int, str]] | None = None,
    redirections: list[list[str]] | None = None,
) -> list[tuple[list[str], str]]:
    """Simple commands, each with the operator that joined it to the previous one.

    Heredoc bodies are skipped; when heredoc_bodies is given, each body is appended to it with the index of
    the segment whose redirection opened it. Leading assignments and wrappers (env, command, ...) are dropped;
    when prefixes is given, each segment's dropped words are appended to it. expanding_bodies gets the bodies whose
    delimiter is unquoted, which the shell expands, substitutions included. When redirections is given, each
    segment's unquoted redirections (>f, 2>&1, < f, &>f) leave its words and are appended to it instead.

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

    heredocs: list[tuple[str, bool, int, bool]] = []
    quoted_start = False
    # Any quoting in a heredoc's delimiter makes its body plain data; otherwise the shell expands it.
    word_quoted = False
    pending_operator: str | None = None
    redirected: list[str] = []
    target_pending = False
    # The word so far ends in an unquoted < or >, so a following & belongs to it (2>&1), not a new command.
    operator_end = False

    def end_word() -> None:
        nonlocal in_word, quoted_start, pending_operator, word_quoted, target_pending
        if in_word:
            text = "".join(word)
            redirection = None if word_quoted else _REDIRECTION_RE.fullmatch(text)
            if redirections is not None and (target_pending or (redirection and not text.startswith("<<"))):
                redirected.append(text)
                target_pending = not target_pending and redirection is not None and not redirection.group(1)
                word.clear()
                in_word = quoted_start = word_quoted = False
                return
            if pending_operator is not None:
                heredocs.append((text, pending_operator == "<<-", len(segments), word_quoted))
                pending_operator = None
            elif not quoted_start and text in ("<<", "<<-"):
                pending_operator = text
            elif not quoted_start and text.startswith("<<") and not text.startswith("<<<"):
                heredocs.append(
                    (
                        text.removeprefix("<<-") if text.startswith("<<-") else text[2:],
                        text.startswith("<<-"),
                        len(segments),
                        word_quoted,
                    )
                )
            words.append(text)
            word.clear()
            in_word = False
            quoted_start = False
            word_quoted = False

    def end_segment(operator: str) -> None:
        nonlocal words, joined_by, redirected, target_pending
        end_word()
        stripped = _strip_prefixes(words)
        if prefixes is not None:
            prefixes.append(words[: len(words) - len(stripped)])
        if redirections is not None:
            redirections.append(redirected)
        segments.append((stripped, joined_by))
        words, joined_by, redirected, target_pending = [], operator, [], False

    while i < n:
        ch = command[i]
        after_operator, operator_end = operator_end, False
        if ch == "$" and command.startswith("$'", i):
            # ANSI-C quoting: a backslash escapes the next character, \' included.
            quoted_start = quoted_start or not in_word
            word_quoted = True
            j = i + 2
            while j < n and command[j] != "'":
                if command[j] == "\\" and j + 1 < n:
                    word.append({"n": "\n", "t": "\t"}.get(command[j + 1], command[j + 1]))
                    j += 2
                    continue
                word.append(command[j])
                j += 1
            if j >= n:
                return []
            in_word = True
            i = j + 1
        elif ch == "\\" and i + 1 < n:
            if command[i + 1] != "\n":
                word.append(command[i + 1])
                in_word = True
                word_quoted = True
            i += 2
        elif ch == "'":
            close = command.find("'", i + 1)
            if close < 0:
                return []
            quoted_start = quoted_start or not in_word
            word_quoted = True
            word.append(command[i + 1 : close])
            in_word = True
            i = close + 1
        elif ch == '"':
            quoted_start = quoted_start or not in_word
            word_quoted = True
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
                delimiter, strip_tabs, owner, plain_data = heredocs.pop(0)
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
                if expanding_bodies is not None and not plain_data:
                    expanding_bodies.append((owner, "\n".join(body)))
        elif ch == "&" and (after_operator or (not in_word and command.startswith("&>", i))):
            # Part of a redirection (2>&1, >&2, &>file), not a background job.
            word.append(ch)
            in_word = True
            i += 1
        elif command.startswith(("&&", "||", ";;", "|&"), i):
            end_segment(command[i : i + 2])
            i += 2
        elif ch in ";&|()":
            end_segment(ch)
            i += 1
        else:
            word.append(ch)
            in_word = True
            operator_end = ch in "<>"
            i += 1
    end_segment(";")
    return segments


def _strip_prefixes(tokens: list[str]) -> list[str]:
    while tokens:
        if _ENV_ASSIGNMENT_RE.match(tokens[0]) or tokens[0] in _WRAPPERS:
            tokens = tokens[1:]
        elif tokens[0] == "timeout" and (rest := _skip_timeout(tokens[1:])) is not None:
            tokens = rest
        else:
            break
    return tokens


def _skip_timeout(args: list[str]) -> list[str] | None:
    """The command `timeout [options] DURATION` runs; None for a form not read, which then stays unread."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        arg = args[i]
        if arg in ("--foreground", "--preserve-status", "-v", "--verbose"):
            i += 1
        elif arg in ("-s", "-k", "--signal", "--kill-after"):
            i += 2
        elif arg.startswith(("-s", "-k", "--signal=", "--kill-after=")):
            i += 1
        else:
            return None
    if i >= len(args) or not _TIMEOUT_DURATION_RE.fullmatch(args[i]) or i + 1 >= len(args):
        return None
    return args[i + 1 :]


def _has_inner_parent(path: str) -> bool:
    """A .. after a name: git -C takes it after a symlink, physically, and ~user/.. names no directory we know."""
    parts = path.split("/")
    while parts and parts[0] == "..":
        parts.pop(0)
    return ".." in parts


def _resolve(base: str, path: str, host_home: str | None = None) -> str:
    """With host_home, a path on a worker host: its ~ is the host's, and ~user stays literal, so nothing is read."""
    if host_home is None:
        return os.path.normpath(os.path.join(base, os.path.expanduser(path)))
    if path == "~" or path.startswith("~/"):
        path = host_home + path[1:]
    return os.path.normpath(os.path.join(base, path))
