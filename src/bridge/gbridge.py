#!/usr/bin/env python3
# gbridge: installed by bridge
"""bridge for a worker on a remote host: talks to the Mac's bridge through the worker tab's ssh tunnel.

Standard library only. Not named bridge: on Linux that is the network tool.
"""

import contextlib
import fcntl
import fnmatch
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

COMMANDS = ("send", "reply", "ack", "done", "decline", "focus", "keep", "inbox", "show")
HOOK_TIMEOUT_S = 10
CLI_TIMEOUT_S = 60
TEXT_MAX_CHARS = 65_536
# Only what the Mac's hook handler reads; a tool result can be megabytes and the tunnel is slow.
HOOK_FIELDS = (
    "session_id", "cwd", "prompt", "last_assistant_message", "agent_id", "prompt_id", "source", "reason",
    "hook_event_name", "notification_type", "message",
)  # fmt: skip
GATED_EVENTS = ("PostToolUse", "PostToolUseFailure")
# Only states that carry no mail: a replayed prompt hook would take messages whose text nobody reads.
REPLAYED_EVENTS = ("Stop", "SessionEnd", "Notification")
PENDING_FILE = "pending-hook.json"
REPLAY_EVERY_S = 20
REPLAY_FOR_S = 24 * 3600
UNREACHABLE_EXIT = 75
# Claude gives the hook 15 s and runs the command if it times out; the guard must answer, or refuse, before then.
GUARD_TIMEOUT_S = 10
GUARD_DEADLINE_S = 12
GUARD_COMMAND_MAX_CHARS = 200_000
GLOBS_FILE = "guard-globs.json"
ROOTS_FILE = "readable-roots.json"
# A command's cd and git -C directories whose checkouts go with a guard check; more than this are not read.
GUARD_CHECKOUTS_MAX = 8
_GITHUB_RE = re.compile(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
_SECTION_RE = re.compile(r'^\[\s*([A-Za-z0-9.-]+)\s*(?:"((?:[^"\\]|\\.)*)")?\s*\]')
_KEY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9-]*)\s*(?:=\s*(.*))?$")
SYSTEM_CONFIG = "/etc/gitconfig"
_QUOTING_RE = re.compile(r"[\"'\\]")
_QUOTED_TILDE_RE = re.compile(r"[\"'\\]~|~[^/\s]*[\"'\\]")
_REPO_ENV_RE = re.compile(r"^(GH_REPO|GH_HOST|GIT_DIR|GIT_WORK_TREE|GIT_COMMON_DIR|GIT_NAMESPACE|GIT_CONFIG\w*)$")
# bridge.agent_prefix.LINEAR_ISSUE_TOOLS: their titles get the Mac's title check.
LINEAR_ISSUE_TOOLS = ("mcp__claude_ai_Linear__save_issue",)
# git before 2.44 lets these take a branch another worktree has checked out, leaving that worktree on the old commit.
_FORCED_SWITCH_RE = re.compile(
    r"\bgit\b[^;&|\n]*?\b(?:checkout|switch)\b[^;&|\n]*?\s(?:-[BC]|--force-create)[\s=]+([^\s;&|()]+)"
)
_OTHER_WORKTREES_RE = re.compile(r"\bgit\b[^;&|\n]*?\b(?:checkout|switch)\b[^;&|\n]*?\s--ignore-other-worktrees\b")
_COMMIT_RE = re.compile(r"\bgit\b[^;&|\n]*?\scommit\b")
LOCAL_GIT_TIMEOUT_S = 3


class GuardDeadline(Exception):
    """Not an OSError: a file read that the alarm interrupts must not swallow it."""


def main(argv: list[str]) -> int:
    directory = os.environ.get("GBRIDGE_DIR")
    if not directory:
        print("gbridge: GBRIDGE_DIR is not set; gbridge works only inside a bridge host worker", file=sys.stderr)
        return 2
    home = Path(directory)
    if argv == ["hook", "PreToolUse"]:
        run_guard(home)
        return 0
    if argv[:1] == ["hook"] and len(argv) == 2:
        run_hook(home, argv[1])
        return 0
    if argv == ["_replay"]:
        replay_pending(home)
        return 0
    if not argv or argv[0] not in COMMANDS:
        print(f"usage: gbridge {{{','.join(COMMANDS)}}} ...  (the same arguments as bridge)", file=sys.stderr)
        return 2
    stdin = sys.stdin.read() if reads_stdin(argv) else ""
    try:
        answer = post(home, "/cli", {"argv": argv, "stdin": stdin}, CLI_TIMEOUT_S)
    except OSError as exc:
        print(
            f"gbridge: the Mac's bridge is not reachable ({exc}). The tab's ssh tunnel is down: the Mac may be "
            "asleep or offline. Your work here goes on; retry later.",
            file=sys.stderr,
        )
        return UNREACHABLE_EXIT
    sys.stdout.write(str(answer.get("stdout") or ""))
    sys.stderr.write(str(answer.get("stderr") or ""))
    code = answer.get("code")
    return code if isinstance(code, int) else 1


def reads_stdin(argv: list[str]) -> bool:
    """Only `keep` without its text: Claude's Bash tool can leave stdin open, and reading it would wait forever."""
    return argv[0] == "keep" and "--show" not in argv and all(arg.startswith("-") for arg in argv[1:])


def run_guard(home: Path) -> None:
    """The Mac's guard for a Bash command or a Linear issue title. Fails closed: what it has not passed is refused."""

    expired = False

    def out_of_time(signum: int, frame: object) -> None:
        nonlocal expired
        expired = True
        raise GuardDeadline(f"no answer in {GUARD_DEADLINE_S}s")

    signal.signal(signal.SIGALRM, out_of_time)
    signal.alarm(GUARD_DEADLINE_S)
    try:
        raw: Any = json.loads(sys.stdin.read() or "{}")
        payload = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
        raw_input: Any = payload.get("tool_input")
        tool_input = cast(dict[str, Any], raw_input) if isinstance(raw_input, dict) else {}
        tool = payload.get("tool_name")
        field = "command" if tool == "Bash" else "title" if tool in LINEAR_ISSUE_TOOLS else None
        text = tool_input.get(field) if field else None
        cwd = payload.get("cwd")
        if field == "command" and isinstance(text, str) and isinstance(cwd, str):
            refusal = find_worktree_refusal(text, cwd)
            if refusal:
                write_denial(refusal)
                return
        globs = read_globs(home)
        if not isinstance(text, str) or not may_need_guard(globs, text):
            return
        if len(text) > GUARD_COMMAND_MAX_CHARS:
            write_denial(f"this {field} is over {GUARD_COMMAND_MAX_CHARS} characters, too long for bridge's guard to "
                         "check; split it, keeping any push or merge in a short command of its own.")  # fmt: skip
            return
        body = {
            "event": "PreToolUse",
            "payload": {
                "session_id": payload.get("session_id"), "cwd": cwd, "tool_name": tool, "tool_input": {field: text},
            },
            "checkouts": read_guard_checkouts(home, text, cwd, lambda: expired)
            if field == "command" and isinstance(cwd, str)
            else [],
            "home": os.path.expanduser("~"),
            "real_roots": read_real_roots(home),
        }  # fmt: skip
        try:
            answer = post(home, "/hook", body, GUARD_TIMEOUT_S)
        except OSError as exc:
            what = "this command, which may push or merge," if field == "command" else "this Linear issue save"
            scope = (
                f"{what} was not run. Commands that cannot push or merge still run"
                if globs
                else "this command was not run: until the guard has answered once, every command needs its check"
            )
            write_denial(
                f"bridge's guard on the Mac could not be reached ({exc}), so {scope}. The tab's ssh tunnel is down "
                "(the Mac may be asleep or offline): retry this once the Mac is back."
            )
            return
        if expired:
            raise GuardDeadline(f"no answer in {GUARD_DEADLINE_S}s")
        if answer.get("checked") is not True:
            why = answer.get("reason") or str(answer.get("stderr") or "").strip() or "no verdict in its answer"
            write_denial(f"bridge's guard on the Mac did not check this command ({why}), so it was not run.")
            return
        save_globs(home, answer.get("globs"))
        save_roots(home, answer.get("roots"))
        output = answer.get("output")
        if isinstance(output, str) and output:
            sys.stdout.write(output)
    except Exception as exc:
        log(home, f"hook PreToolUse: {type(exc).__name__}: {exc}")
        write_denial(f"bridge's guard could not check this command ({type(exc).__name__}: {exc}), so it was not run.")
    finally:
        signal.alarm(0)


def find_worktree_refusal(command: str, cwd: str) -> str | None:
    """Checked on the host, not on the Mac: it reads the host's worktrees."""
    if _OTHER_WORKTREES_RE.search(command):
        return (
            "--ignore-other-worktrees puts two worktrees on one branch: a commit in either leaves the other's files "
            "and index on the old commit, and its next commit undoes yours. Work detached instead: git checkout "
            "--detach <start commit>, commit, then git push origin HEAD:<branch>."
        )
    forced = [m.group(1).strip("'\"") for m in _FORCED_SWITCH_RE.finditer(command)]
    commits = bool(_COMMIT_RE.search(command))
    if not forced and not commits:
        return None
    directories = dict.fromkeys([cwd, *(path for _, path in list_command_dirs(command, cwd))])
    for directory in list(directories)[:GUARD_CHECKOUTS_MAX]:
        for branch in forced:
            holder = find_other_worktree_on(directory, branch)
            if holder:
                return (
                    f"{branch} is checked out in the worktree at {holder}. Taking it here with checkout -B or "
                    "switch -C moves it under that worktree and leaves its files and index on the old commit, so "
                    f"its next commit undoes yours. Work detached instead: git checkout --detach origin/{branch} "
                    f"(or your start commit), commit, then git push origin HEAD:{branch}."
                )
        stale = read_stale_checkout(directory) if commits else None
        if stale:
            branch, old, head = stale
            return (
                f"The worktree at {directory} still has the files and index of {old[:9]}, but its branch {branch} "
                f"has moved on to {head[:9]} from another checkout. git status shows the difference as staged "
                "changes, and a commit now would undo those commits. Catch up first, keeping your own edits: "
                f"git checkout --detach {old[:12]} && git checkout {branch}"
            )
    return None


def find_other_worktree_on(directory: str, branch: str) -> str | None:
    listing = run_local_git(directory, "worktree", "list", "--porcelain")
    top = run_local_git(directory, "rev-parse", "--show-toplevel")
    if listing is None or top is None or listing.returncode or top.returncode:
        return None
    own = os.path.realpath(top.stdout.strip())
    path = None
    for line in listing.stdout.splitlines():
        if line.startswith("worktree "):
            path = line.removeprefix("worktree ")
        elif line == f"branch refs/heads/{branch}" and path and os.path.realpath(path) != own:
            return path
    return None


def read_stale_checkout(directory: str) -> tuple[str, str, str] | None:
    """(branch, the commit this worktree last checked out, the branch's head) when another checkout moved the branch
    and this worktree's index still differs from it. A worktree moves its own HEAD reflog with every switch, commit
    or reset; another worktree's commits on the branch log only the branch's reflog."""
    branch = run_local_git(directory, "symbolic-ref", "-q", "--short", "HEAD")
    git_dir = run_local_git(directory, "rev-parse", "--absolute-git-dir")
    head = run_local_git(directory, "rev-parse", "-q", "--verify", "HEAD")
    if not branch or not git_dir or not head or branch.returncode or git_dir.returncode or head.returncode:
        return None
    try:
        last = (Path(git_dir.stdout.strip()) / "logs" / "HEAD").read_text().splitlines()[-1].split()[1]
    except (OSError, UnicodeDecodeError, IndexError):
        return None
    if last == head.stdout.strip():
        return None
    staged = run_local_git(directory, "diff-index", "--cached", "--quiet", "HEAD")
    if staged is None or staged.returncode != 1:
        return None
    return branch.stdout.strip(), last, head.stdout.strip()


def run_local_git(directory: str, *args: str) -> subprocess.CompletedProcess[str] | None:
    """A git that cannot run here leaves the command to git itself."""
    try:
        return subprocess.run(
            ["git", "-C", directory, *args],
            capture_output=True,
            text=True,
            timeout=LOCAL_GIT_TIMEOUT_S,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.SubprocessError):
        return None


def read_globs(home: Path) -> list[str] | None:
    try:
        raw: Any = json.loads((home / GLOBS_FILE).read_text())
    except (OSError, ValueError):
        return None
    return [str(g) for g in cast(list[Any], raw)] if isinstance(raw, list) and raw else None


def may_need_guard(globs: list[str] | None, command: str) -> bool:
    """The Mac's own gate: its shell guard starts only for a command matching one of its globs; unknown globs pass
    every command to it. Quotes and line continuations are dropped too, since the shell runs git pu""sh as git push."""
    if globs is None:
        return True
    plain = _QUOTING_RE.sub("", command.replace("\\\n", ""))
    return any(fnmatch.fnmatchcase(text, glob) for glob in globs for text in (command, plain))


def save_globs(home: Path, globs: object) -> None:
    save_strings(home, GLOBS_FILE, globs)


def save_roots(home: Path, roots: object) -> None:
    save_strings(home, ROOTS_FILE, roots)


def save_strings(home: Path, name: str, values: object) -> None:
    if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in cast(list[Any], values)):
        return
    text = json.dumps(values)
    with contextlib.suppress(OSError):
        if (home / name).read_text() == text:
            return
    tmp = home / f".{name}.{os.getpid()}"
    tmp.write_text(text)
    tmp.replace(home / name)


def read_roots(home: Path) -> list[str] | None:
    try:
        raw: Any = json.loads((home / ROOTS_FILE).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, list):
        return None
    return [str(r) for r in cast(list[Any], raw) if isinstance(r, str) and os.path.isabs(r)] or None


def read_guard_checkouts(
    home: Path, command: str, cwd: str, expired: Callable[[], bool] = lambda: False
) -> list[dict[str, Any]]:
    """The git facts of each checkout the command may run in, if its real path is under a root the Mac named.

    The Mac reads a push only in a directory reported here; what is left out is unreadable there and refused.
    """
    roots = read_roots(home)
    real_roots = [os.path.realpath(r) for r in roots] if roots else []
    routes: dict[str, list[str]] = {}
    for directory, route in [(cwd, cwd), *list_command_dirs(command, cwd)]:
        routes.setdefault(directory, []).append(route)
    found: list[dict[str, Any]] = []
    for directory, ways in list(routes.items())[:GUARD_CHECKOUTS_MAX]:
        real_dir = os.path.realpath(directory)
        # The Mac knows the directory only by name; every way the command reaches it must be that same place.
        if not all(os.path.isdir(way) and os.path.realpath(way) == real_dir for way in ways):
            continue
        try:
            facts = read_guard_git_info(directory)
            # A read may have swallowed the alarm; the next one must not run past the deadline too.
            if expired():
                raise GuardDeadline(f"no answer in {GUARD_DEADLINE_S}s")
            # git finds the repo from the physical directory; a symlink on the way may lead it to another one.
            physical = read_guard_git_info(real_dir)
            if expired():
                raise GuardDeadline(f"no answer in {GUARD_DEADLINE_S}s")
        except (OSError, UnicodeDecodeError):
            continue
        if facts is None or physical is None:
            continue
        real_root = os.path.realpath(facts["repo_root"])
        if os.path.realpath(physical["repo_root"]) != real_root:
            continue
        # Without the Mac's roots yet only cwd, Claude's own working directory, is reported, as before roots existed.
        inside = any(is_within(real_root, r) for r in real_roots)
        if inside or (roots is None and directory == cwd):
            found.append({**facts, "dir": directory, "real_root": real_root})
    return found


def read_real_roots(home: Path) -> dict[str, str]:
    """The Mac compares real paths with these: it cannot resolve a symlinked root itself."""
    return {r: os.path.realpath(r) for r in read_roots(home) or []}


def is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def list_command_dirs(command: str, cwd: str) -> list[tuple[str, str]]:
    """Where its cd and git -C words lead: the name the Mac's guard gives each, and the path the shell or git takes
    there. A word read wrongly costs nothing: the Mac never looks it up, or finds no checkout there and refuses."""
    if "<<" in command:
        # A heredoc's body is data to the shell, yet reads here as commands.
        return []
    lexer = shlex.shlex(command.replace("\\\n", ""), posix=True, punctuation_chars="();<>|&\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        words = list(lexer)
    except ValueError:
        return []
    quoted_tilde = bool(_QUOTED_TILDE_RE.search(command))
    found: list[tuple[str, str]] = []
    current: tuple[str, str] | None = (cwd, cwd)

    def resolve(base: tuple[str, str] | None, target: str, tilde: bool = True) -> tuple[str, str] | None:
        """(the guard's name for it, the path the kernel takes) from base, the same pair; None if unreadable."""
        if target.startswith("-") or any(c in target for c in "$`;&|()<>"):
            return None
        if base is None and not target.startswith(("/", "~")):
            return None
        parts = target.split("/")
        while parts and parts[0] == "..":
            parts.pop(0)
        if ".." in parts:
            # git takes a .. after a symlink physically; the Mac refuses such a path anyway.
            return None
        if target.startswith("~"):
            # The shell expands only an unquoted ~ that starts a word: not -C~/x, '~/x' or ~""/x.
            if not tilde or quoted_tilde or not (target == "~" or target.startswith("~/")):
                return None
            target = os.path.expanduser("~") + target[1:]
        name, route = base or ("/", "/")
        return os.path.normpath(os.path.join(name, target)), os.path.join(route, target)

    i = 0
    while i < len(words) - 1:
        word = words[i]
        # Only a command's own name: `echo cd x` changes no directory.
        at_start = i == 0 or not words[i - 1].strip("();|&\n")
        if not at_start:
            i += 1
            continue
        if word == "cd":
            step = resolve(current, words[i + 1])
            if step:
                found.append(step)
            # The shell's cd is logical: from there on it stands in the directory its name resolves to.
            current = (step[0], step[0]) if step else None
            i += 2
            continue
        if word == "git":
            # git -C is physical: each next -C goes on from where the last one led.
            place = current
            i += 1
            while i < len(words) and words[i].startswith("-"):
                arg = words[i]
                if arg == "-C" and i + 1 < len(words):
                    step = resolve(place, words[i + 1])
                    i += 2
                elif arg.startswith("-C") and len(arg) > 2:
                    step = resolve(place, arg[2:], tilde=False)
                    i += 1
                else:
                    i += 1
                    continue
                if step:
                    found.append(step)
                place = step
            continue
        i += 1
    return found


def write_denial(reason: str) -> None:
    decision = {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}
    sys.stdout.write(json.dumps({"hookSpecificOutput": decision}))


def run_hook(home: Path, event: str) -> None:
    """Fails open: a hook must never stop the session, whatever happens to the tunnel."""
    try:
        if event in GATED_EVENTS and time.time() < read_hint(home):
            return
        raw: Any = json.loads(sys.stdin.read() or "{}")
        payload = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
        trimmed = {k: v for k, v in payload.items() if k in HOOK_FIELDS}
        for key in ("prompt", "last_assistant_message"):
            if isinstance(trimmed.get(key), str):
                trimmed[key] = trimmed[key][:TEXT_MAX_CHARS]
        cwd = trimmed.get("cwd")
        git = read_git_info(cwd) if isinstance(cwd, str) else None
        body = {"event": event, "payload": trimmed, "git": git}
        try:
            answer = post(home, "/hook", body, HOOK_TIMEOUT_S)
        except OSError:
            if event in REPLAYED_EVENTS:
                keep_for_replay(home, body)
            raise
        # Whatever this hook says is newer than a state still waiting to be replayed.
        (home / PENDING_FILE).unlink(missing_ok=True)
        save_globs(home, answer.get("globs"))
        save_roots(home, answer.get("roots"))
        due = answer.get("hint_due")
        if isinstance(due, int):
            write_hint(home, due)
        output = answer.get("output")
        if isinstance(output, str) and output:
            sys.stdout.write(output)
    except Exception as exc:
        log(home, f"hook {event}: {type(exc).__name__}: {exc}")


def keep_for_replay(home: Path, body: dict[str, Any]) -> None:
    """A lost Stop would leave the worker shown at work for good; the replayer sends it once the Mac is back."""
    tmp = home / f".{PENDING_FILE}.{os.getpid()}"
    tmp.write_text(json.dumps({**body, "at_ns": time.time_ns()}))
    tmp.replace(home / PENDING_FILE)
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "_replay"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def replay_pending(home: Path) -> None:
    """One replayer at a time; it ends once the state is sent, superseded, or too old to matter."""
    with (home / "replay.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return
        deadline = time.monotonic() + REPLAY_FOR_S
        while time.monotonic() < deadline:
            try:
                text = (home / PENDING_FILE).read_text()
            except OSError:
                return
            body = json.loads(text)
            age_ns = time.time_ns() - int(body.pop("at_ns", time.time_ns()))
            try:
                post(home, "/hook", {**body, "age_ns": age_ns}, HOOK_TIMEOUT_S)
            except OSError:
                time.sleep(REPLAY_EVERY_S)
                continue
            with contextlib.suppress(OSError):
                if (home / PENDING_FILE).read_text() == text:
                    (home / PENDING_FILE).unlink()
            return


def post(home: Path, path: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    port = (home / "port").read_text().strip()
    if not port.isdigit():
        raise OSError("no tunnel port yet")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {(home / 'token').read_text().strip()}",
            "X-Bridge-Worker": (home / "worker").read_text().strip(),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw: Any = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        return {"stdout": "", "stderr": f"gbridge: bridge refused ({exc.code}): {detail}\n", "code": 2}
    return cast(dict[str, Any], raw) if isinstance(raw, dict) else {}


def read_hint(home: Path) -> float:
    try:
        return float((home / "hint").read_text().strip() or 0)
    except (OSError, ValueError):
        return 0.0


def write_hint(home: Path, due: int) -> None:
    tmp = home / f".hint.{os.getpid()}"
    tmp.write_text(f"{due}\n")
    tmp.replace(home / "hint")


def log(home: Path, line: str) -> None:
    try:
        with (home / "gbridge.log").open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass


def read_guard_git_info(cwd: str) -> dict[str, Any] | None:
    """None when the environment picks another repo or config than the checkout's own: the guard then reads no
    target from this checkout's facts."""
    if any(_REPO_ENV_RE.match(name) for name in os.environ):
        return None
    return read_git_info(cwd)


def read_git_info(cwd: str) -> dict[str, Any] | None:
    """From files, as on the Mac: the dashboard shows the repo and branch, and the guard reads remotes and push
    settings. Only those facts leave the host, never the config text: remote URLs can carry credentials."""
    for directory in (Path(cwd), *Path(cwd).parents):
        dot_git = directory / ".git"
        git_dir: Path | None = None
        if dot_git.is_dir():
            git_dir = dot_git
        elif dot_git.is_file():
            content = dot_git.read_text().strip()
            if content.startswith("gitdir:"):
                found = Path(content.removeprefix("gitdir:").strip())
                git_dir = found if found.is_absolute() else (directory / found).resolve()
        if git_dir is not None:
            return {"repo_root": str(directory), "branch": read_branch(git_dir), **read_config_facts(git_dir)}
    return None


def read_branch(git_dir: Path) -> str | None:
    try:
        head = (git_dir / "HEAD").read_text().strip()
    except OSError:
        return None
    if head.startswith("ref: refs/heads/"):
        return head.removeprefix("ref: refs/heads/")
    return head[:12] or None


def read_config_facts(git_dir: Path) -> dict[str, Any]:
    """Parsed as bridge.gitinfo.map_git_config_to_facts parses on the Mac; the two must agree."""
    common = git_dir
    if (git_dir / "commondir").is_file():
        common = (git_dir / (git_dir / "commondir").read_text().strip()).resolve()
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "git" / "config"
    outer = [read_text(Path(SYSTEM_CONFIG)), read_text(xdg), read_text(Path.home() / ".gitconfig")]
    local = [read_text(common / "config"), read_text(git_dir / "config.worktree")]
    remotes: dict[str, str | None] = {}
    tracking: dict[str, str] = {}
    push_default: str | None = None
    custom_push = custom_remote = False
    for is_local, text in [*((False, t) for t in outer), *((True, t) for t in local)]:
        for name, sub, key, value in scan_git_config(text):
            if name in ("include", "includeif") or (name == "url" and key in ("insteadof", "pushinsteadof")):
                custom_remote = True
            elif name == "remote" and sub is not None and key == "url":
                if not is_local or sub in remotes:
                    custom_remote = True
                else:
                    found = _GITHUB_RE.search(value)
                    remotes[sub] = f"{found.group(1)}/{found.group(2)}" if found else None
            elif name == "remote" and key == "pushurl":
                custom_remote = True
            elif (name, key) in (("remote", "push"), ("remote", "pushdefault"), ("branch", "pushremote")):
                custom_push = True
            elif name == "branch" and sub is not None and key == "remote":
                if is_local:
                    tracking[sub] = value
                else:
                    custom_push = True
            elif name == "push" and sub is None and key == "default":
                push_default = value.lower()
    return {
        "repo": remotes.get("origin"), "remotes": remotes, "tracking": tracking, "push_default": push_default,
        "custom_push": custom_push, "custom_remote": custom_remote,
    }  # fmt: skip


def scan_git_config(text: str) -> list[tuple[str, str | None, str, str]]:
    entries: list[tuple[str, str | None, str, str]] = []
    section: tuple[str, str | None] = ("", None)
    for line in text.splitlines():
        rest = line.strip()
        match = _SECTION_RE.match(rest)
        if match:
            name, sub = match.group(1).lower(), match.group(2)
            if sub is None and "." in name:
                name, _, sub = name.partition(".")
            section = (name, sub)
            rest = rest[match.end() :].strip()
        entry = _KEY_RE.match(rest)
        if entry:
            value = re.split(r"\s[#;]", entry.group(2) or "")[0].strip().strip('"')
            entries.append((section[0], section[1], entry.group(1).lower(), value))
    return entries


def read_text(path: Path) -> str:
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return ""


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
