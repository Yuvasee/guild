"""Runs on a worker host as `python3 -I -S -`, with PAYLOAD prepended: standard library only, prints one JSON line.

Everything it creates is named after the worker (g-...) and lives on its own tmux socket, so the host's own tmux
sessions, ~/.claude settings and other files stay untouched; in ~/.claude.json it only adds, and on close removes,
the trust entry of the worker's own worktree.
"""

import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import time
import traceback
from typing import Any, cast

PAYLOAD: dict[str, Any] = {}

TMUX_SOCKET = "gbridge"
GBRIDGE_MARK = "# gbridge: installed by bridge"
WORKER_RE = re.compile(r"g-[a-z0-9][a-z0-9-]{0,48}")
SNAPSHOT_MARK = "/shell-snapshots/snapshot-"
HELPER_MARKS = ("mcp", "caffeinate")
SUBAGENT_QUIET_S = 120
HOOK_EVENTS = (
    "SessionStart", "UserPromptSubmit", "Stop", "SessionEnd", "PostToolUse", "PostToolUseFailure", "Notification",
    "PreToolUse", "StopFailure",
)  # fmt: skip
# bridge.agent_prefix.LINEAR_ISSUE_TOOLS go to the Mac's title check with Bash; gbridge refuses Artifact itself.
HOOK_MATCHERS = {"Notification": "permission_prompt", "PreToolUse": "Bash|Artifact|mcp__claude_ai_Linear__save_issue"}
HOOK_TIMEOUT_S = 15
GLOBS_FILE = "guard-globs.json"
ROOTS_FILE = "readable-roots.json"
PORT_WAIT_S = 600
# Claude Code saves ~/.claude.json under a proper-lockfile lock (a directory), re-reading the file inside it; it takes
# over a lock not refreshed for CLAUDE_JSON_LOCK_STALE_S.
CLAUDE_JSON_LOCK_WAIT_S = 5.0
CLAUDE_JSON_LOCK_STALE_S = 10.0
TMUX_SERVER_OPTIONS = (
    ("set-option", "-s", "escape-time", "0"),
    ("set-option", "-s", "default-terminal", "tmux-256color"),
    ("set-option", "-sa", "terminal-features", ",xterm-256color:RGB"),
    ("set-option", "-g", "status", "off"),
    ("set-option", "-g", "history-limit", "50000"),
    # Claude's terminal title reaches agterm, which shows it on the tab.
    ("set-option", "-g", "set-titles", "on"),
    ("set-option", "-g", "set-titles-string", "#{pane_title}"),
)


def main() -> None:
    try:
        result = run(PAYLOAD)
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result))


def run(payload: dict[str, Any]) -> dict[str, Any]:
    op = payload.get("op")
    if op == "sync":
        return sync(payload)
    worker = str(payload.get("worker", ""))
    if not WORKER_RE.fullmatch(worker):
        return {"ok": False, "error": f"bad worker name {worker!r}"}
    if op == "launch":
        return launch(worker, payload)
    if op == "check":
        return check(worker, payload)
    if op == "remove":
        return remove(worker)
    return {"ok": False, "error": f"unknown op {op!r}"}


def base_dir() -> str:
    return os.path.expanduser("~/.local/state/gbridge")


def worker_dir(worker: str) -> str:
    return os.path.join(base_dir(), worker)


def worktrees_dir() -> str:
    return os.path.join(base_dir(), "worktrees")


def worktree_dir(worker: str) -> str:
    return os.path.join(worktrees_dir(), worker)


def git(cwd: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=60)


def tmux(*args: str) -> subprocess.CompletedProcess[str]:
    # No user config: a restore plugin in it re-creates the owner's sessions inside our server.
    return subprocess.run(
        ["tmux", "-L", TMUX_SOCKET, "-f", "/dev/null", *args], capture_output=True, text=True, timeout=20
    )


def session_state(worker: str) -> str:
    """present, gone, or unknown: only tmux's own answer means gone, and gone lets files be deleted."""
    done = tmux("has-session", "-t", f"={worker}")
    if done.returncode == 0:
        return "present"
    if done.returncode == 1 and any(text in done.stderr for text in ("can't find session", "no server running")):
        return "gone"
    return "unknown"


def has_session(worker: str) -> bool:
    return session_state(worker) == "present"


def launch(worker: str, payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("resume"):
        return relaunch(worker, payload)
    cwd = str(payload["cwd"])
    if not os.path.isdir(cwd):
        return {"ok": False, "error": f"{cwd} does not exist on this host"}
    error = check_free(worker) or next(
        (f"{path} already exists here" for path in (worker_dir(worker), worktree_dir(worker)) if os.path.exists(path)),
        None,
    )
    if error:
        return {"ok": False, "error": error}
    directory = worker_dir(worker)
    error = install_gbridge(str(payload["gbridge"]))
    if error:
        return {"ok": False, "error": error}
    updated, warnings = sync_rule_files(payload.get("rule_files") or [])
    os.makedirs(base_dir(), mode=0o700, exist_ok=True)
    added = add_worktree(worker, cwd)
    if isinstance(added, str):
        return {"ok": False, "error": added}
    checkout, cwd = added
    trust_error = set_worktree_trust(worktree_dir(worker), True)
    try:
        started = start_session(worker, directory, cwd, payload)
    except Exception:
        started = None
        error = traceback.format_exc().strip().splitlines()[-1]
    if started is None or started.returncode != 0:
        shutil.rmtree(directory, ignore_errors=True)
        # Made a moment ago and never used: nothing in it can be lost.
        git(checkout, "worktree", "remove", "--force", worktree_dir(worker))
        set_worktree_trust(worktree_dir(worker), False)
        why = f"tmux new-session failed: {started.stderr.strip()[:300]}" if started is not None else error
        return {"ok": False, "error": why}
    for option in TMUX_SERVER_OPTIONS:
        tmux(*option)
    result: dict[str, Any] = {
        "ok": True, "dir": directory, "home": os.path.expanduser("~"), "cwd": cwd, "checkout": checkout,
        "worktree": worktree_dir(worker),
    }  # fmt: skip
    if trust_error:
        warnings.append(f"Claude Code may ask whether to trust {worktree_dir(worker)}: {trust_error}")
    return add_sync_outcome(result, updated, warnings)


def check_free(worker: str) -> str | None:
    if shutil.which("tmux") is None:
        return "tmux is not installed on this host"
    state = session_state(worker)
    if state != "gone":
        return f"tmux session {worker} is {'already there' if state == 'present' else 'unknown'}"
    return None


def relaunch(worker: str, payload: dict[str, Any]) -> dict[str, Any]:
    """A closed worker again, under its old name: Claude finds the conversation only from the directory it began in,
    its own worktree, which comes back at the same path if the close removed it."""
    conversation, checkout, cwd = str(payload["resume"]), str(payload["checkout"]), str(payload["cwd"])
    worktree = worktree_dir(worker)
    if not re.fullmatch(r"[0-9a-f-]{36}", conversation):
        return {"ok": False, "error": f"bad conversation id {conversation!r}"}
    directory = worker_dir(worker)
    error = check_free(worker) or (f"{directory} already exists here" if os.path.exists(directory) else None)
    if error:
        return {"ok": False, "error": error}
    table = read_processes()
    if table is None:
        return {"ok": False, "error": "the host's process table could not be read"}
    # Claude resumes a conversation only from the directory it began in, so any Claude there may hold this one
    # (--continue and the picker name no id); two would each write their own turns into it.
    running = sorted(pid for pid, (_, command) in table.items() if "claude" in command and runs_in(pid, worktree))
    if running:
        return {"ok": False, "error": f"a Claude already runs in {worktree}: pid {', '.join(map(str, running))}"}
    error = install_gbridge(str(payload["gbridge"]))
    if error:
        return {"ok": False, "error": error}
    updated, warnings = sync_rule_files(payload.get("rule_files") or [])
    restored = not os.path.isdir(worktree)
    if restored:
        os.makedirs(worktrees_dir(), mode=0o700, exist_ok=True)
        added = git(checkout, "worktree", "add", "--detach", worktree)
        if added.returncode != 0:
            return {"ok": False, "error": f"git worktree add failed: {added.stderr.strip()[:300]}"}
    if not os.path.isdir(cwd):
        if restored:
            remove_worktree(worktree)
        return {"ok": False, "error": f"{cwd} is not in its worktree {worktree}"}
    trust_error = set_worktree_trust(worktree, True)
    try:
        started = start_session(worker, directory, cwd, payload)
    except Exception:
        started = None
        error = traceback.format_exc().strip().splitlines()[-1]
    if started is None or started.returncode != 0:
        shutil.rmtree(directory, ignore_errors=True)
        if restored and remove_worktree(worktree) is None:
            set_worktree_trust(worktree, False)
        why = f"tmux new-session failed: {started.stderr.strip()[:300]}" if started is not None else error
        return {"ok": False, "error": why}
    for option in TMUX_SERVER_OPTIONS:
        tmux(*option)
    result: dict[str, Any] = {
        "ok": True, "dir": directory, "home": os.path.expanduser("~"), "cwd": cwd, "checkout": checkout,
        "worktree": worktree, "restored": restored,
    }  # fmt: skip
    if trust_error:
        warnings.append(f"Claude Code may ask whether to trust {worktree}: {trust_error}")
    return add_sync_outcome(result, updated, warnings)


def start_session(worker: str, directory: str, cwd: str, payload: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    os.mkdir(directory, 0o700)
    write(os.path.join(directory, "token"), str(payload["token"]), 0o600)
    write(os.path.join(directory, "worker"), worker, 0o600)
    if not payload.get("resume"):
        write(os.path.join(directory, "brief.md"), str(payload["brief"]), 0o600)
        write(
            os.path.join(directory, "first-prompt"), f"Your brief is in {directory}/brief.md. Read it and do it.", 0o600
        )
    write(os.path.join(directory, GLOBS_FILE), json.dumps(list(payload.get("globs") or [])), 0o600)
    # As bridge.host_workers.list_readable_roots names them; every answer from the Mac renews them.
    roots = [str(r) for r in (payload.get("mapped_root"), worktree_dir(worker)) if r]
    write(os.path.join(directory, ROOTS_FILE), json.dumps(roots), 0o600)
    settings = build_settings(
        cwd,
        list(payload.get("disabled_plugins") or []),
        list(payload.get("denied_mcp_servers") or []),
        list(payload.get("denied_tools") or []),
    )
    write(os.path.join(directory, "settings.json"), json.dumps(settings, indent=1), 0o600)
    run_script = build_run_script(directory, cwd, str(payload["claude_flags"]), str(payload.get("resume") or ""))
    write(os.path.join(directory, "run.sh"), run_script, 0o700)
    for option in TMUX_SERVER_OPTIONS:
        # Options of our own tmux server only; it may not run yet, and new-session below starts it.
        tmux(*option)
    return tmux("new-session", "-d", "-s", worker, "-x", "200", "-y", "50", "-c", cwd, f"bash -l {directory}/run.sh")


def add_worktree(worker: str, cwd: str) -> tuple[str, str] | str:
    """The mapped checkout is shared with others on the host, so the worker gets a detached worktree of its HEAD.

    Returns (checkout root, the worker's cwd in its worktree), or an error.
    """
    if shutil.which("git") is None:
        return "git is not installed on this host"
    top = git(cwd, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return f"{cwd} is not in a git checkout; a host worker runs in a worktree of one ({top.stderr.strip()[:200]})"
    checkout = top.stdout.strip()
    relative = os.path.relpath(os.path.realpath(cwd), os.path.realpath(checkout))
    path = worktree_dir(worker)
    os.makedirs(worktrees_dir(), mode=0o700, exist_ok=True)
    added = git(checkout, "worktree", "add", "--detach", path, "HEAD")
    if added.returncode != 0:
        return f"git worktree add failed: {added.stderr.strip()[:300]}"
    worker_cwd = path if relative == "." else os.path.join(path, relative)
    if not os.path.isdir(worker_cwd):
        # Claude would start in a directory that is not there, and its tmux session end at once.
        git(checkout, "worktree", "remove", "--force", path)
        return (
            f"{relative} is not in {checkout}'s committed tree, so the worker's worktree has no such directory; "
            "start it with --cwd at the checkout or a directory committed in it"
        )
    return checkout, worker_cwd


def claude_json_path() -> str:
    return os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~"), ".claude.json")


def set_worktree_trust(worktree: str, trusted: bool) -> str | None:
    """Accepts Claude Code's "trust this folder?" dialog for the worker's worktree as Claude Code records it
    (projects[path].hasTrustDialogAccepted), or drops that path's entry; nothing else in the file changes.

    Returns why it could not, or None.
    """
    path = claude_json_path()
    keys = list(dict.fromkeys([os.path.normpath(worktree), os.path.realpath(worktree)]))
    lock = f"{path}.lock"
    held = take_lock(lock)
    if held is None:
        # Writing without it could undo a save Claude Code makes meanwhile.
        return f"Claude Code's lock {lock} was not free within {CLAUDE_JSON_LOCK_WAIT_S:.0f}s"
    taken_at, mark = held
    try:
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except FileNotFoundError:
            # Claude Code writes it on its first start; one of ours would lack what it expects there.
            return f"{path} does not exist yet" if trusted else None
        try:
            data: Any = json.loads(text)
        except ValueError as exc:
            return f"{path} is not valid JSON ({exc}); left as it is"
        if not isinstance(data, dict):
            return f"{path} holds no JSON object; left as it is"
        config = cast(dict[str, Any], data)
        projects: Any = config.get("projects")
        if projects is None and not trusted:
            return None
        if projects is None:
            projects = config["projects"] = {}
        if not isinstance(projects, dict):
            return f"{path} has a projects value that is no object; left as it is"
        entries = cast(dict[str, Any], projects)
        changed = False
        for key in keys:
            entry: Any = entries.get(key)
            if trusted and not (isinstance(entry, dict) and cast(dict[str, Any], entry).get("hasTrustDialogAccepted")):
                entries[key] = {**(cast(dict[str, Any], entry) if isinstance(entry, dict) else {}),
                                "hasTrustDialogAccepted": True}  # fmt: skip
                changed = True
            elif not trusted and key in entries:
                del entries[key]
                changed = True
        if not changed:
            return None
        # Past half the stale age Claude Code may soon take the lock over; it is not ours to write under then.
        if time.monotonic() - taken_at > CLAUDE_JSON_LOCK_STALE_S / 2 or not owns_lock(lock, mark):
            return f"held Claude Code's lock {lock} too long to write safely"
        write(path, json.dumps(config, indent=2, ensure_ascii=False), os.stat(path).st_mode & 0o777)
        return None
    except OSError as exc:
        return f"{path} could not be updated ({exc})"
    finally:
        if owns_lock(lock, mark):
            with contextlib.suppress(OSError):
                os.rmdir(lock)


def take_lock(lock: str) -> tuple[float, tuple[int, int]] | None:
    """Claude Code's own lock on ~/.claude.json, as (monotonic time taken, its mark); None when it stays held, a stale
    one included: Claude Code clears those itself, bridge never removes a lock it did not make."""
    deadline = time.monotonic() + CLAUDE_JSON_LOCK_WAIT_S
    while True:
        try:
            os.mkdir(lock)
            # Linux may give a lock made again at once the same inode, so the directory's times carry a mark too.
            stamp = time.time_ns() - secrets.randbelow(1_000_000)
            os.utime(lock, ns=(stamp, stamp))
            stat = os.stat(lock)
            return time.monotonic(), (stat.st_ino, stat.st_mtime_ns)
        except FileExistsError:
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)
        except OSError:
            return None


def owns_lock(lock: str, mark: tuple[int, int]) -> bool:
    try:
        stat = os.stat(lock)
    except OSError:
        return False
    return (stat.st_ino, stat.st_mtime_ns) == mark


def install_gbridge(source: str) -> str | None:
    target = os.path.expanduser("~/.local/bin/gbridge")
    if os.path.lexists(target):
        try:
            with open(target, encoding="utf-8") as f:
                current = f.read()
        except OSError as exc:
            return f"cannot read {target}: {exc}"
        if GBRIDGE_MARK not in current:
            return f"{target} exists and is not bridge's gbridge; it was left alone"
        if current == source:
            return None
    os.makedirs(os.path.dirname(target), exist_ok=True)
    write(target, source, 0o755)
    return None


def sync_rule_files(files: list[Any]) -> tuple[list[str], list[str]]:
    """Writes each of the Mac's rule files (skill, overlay) at its home-relative path when its content differs.

    Returns (paths written, why a file was left alone). A host file the Mac lacks stays: it may be the host's own,
    and with no record of what bridge wrote, a deletion could take it.
    """
    home = os.path.realpath(os.path.expanduser("~"))
    updated: list[str] = []
    warnings: list[str] = []
    linked: list[tuple[str, str, bytes]] = []
    for entry in files:
        relative, text, mode = str(entry["path"]), str(entry["text"]), int(entry["mode"])
        parts = relative.split("/")
        if relative.startswith("/") or any(part in ("", ".", "..") for part in parts):
            warnings.append(f"rule file {relative!r} is not a plain home-relative path; skipped")
            continue
        target = os.path.join(home, *parts)
        # Through a symlink the write would land in whatever it points at, such as a checkout of its own.
        if os.path.realpath(target) != target:
            linked.append((relative, target, text.encode("utf-8")))
            continue
        # Reading a FIFO would block the launch until ssh times out.
        if os.path.lexists(target) and not os.path.isfile(target):
            warnings.append(f"~/{relative} is not a regular file here; left alone")
            continue
        data = text.encode("utf-8")
        try:
            with open(target, "rb") as f:
                same = f.read() == data
            if same:
                if os.stat(target).st_mode & 0o777 != mode:
                    os.chmod(target, mode)
                continue
        except FileNotFoundError:
            pass
        except OSError as exc:
            warnings.append(f"~/{relative} could not be read ({exc}); left alone")
            continue
        try:
            os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
            write(target, text, mode)
        except OSError as exc:
            warnings.append(f"~/{relative} could not be written ({exc})")
            continue
        updated.append(f"~/{relative}")
    # Last, so a link to a file written above (the skill's house.md to the overlay's) reads its new text.
    for relative, target, data in linked:
        if not (os.path.isfile(target) and read_quietly(target) == data):
            warnings.append(f"~/{relative} is or sits under a symlink here and differs; left alone")
    return updated, warnings


def read_quietly(path: str) -> bytes | None:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def sync(payload: dict[str, Any]) -> dict[str, Any]:
    """The rule files alone, as a deploy on the Mac sends them; `hashes` holds each one's sha256 as read here after,
    or None when it cannot be read, so the Mac can tell a copy left stale."""
    files: list[Any] = payload.get("rule_files") or []
    updated, warnings = sync_rule_files(files)
    home = os.path.expanduser("~")
    hashes: dict[str, str | None] = {}
    for entry in files:
        relative = str(entry["path"])
        path = os.path.join(home, relative)
        # Reading a FIFO would block until ssh times out.
        data = read_quietly(path) if os.path.isfile(path) else None
        hashes[relative] = hashlib.sha256(data).hexdigest() if data is not None else None
    return add_sync_outcome({"ok": True, "hashes": hashes}, updated, warnings)


def add_sync_outcome(result: dict[str, Any], updated: list[str], warnings: list[str]) -> dict[str, Any]:
    if updated:
        result["updated"] = updated
    if warnings:
        result["warning"] = "; ".join(warnings)
    return result


def write(path: str, text: str, mode: int) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def build_settings(
    cwd: str, disabled_plugins: list[str], denied_mcp_servers: list[str], denied_tools: list[str]
) -> dict[str, Any]:
    gbridge = os.path.expanduser("~/.local/bin/gbridge")
    hooks: dict[str, Any] = {}
    for event in HOOK_EVENTS:
        group: dict[str, Any] = {
            "hooks": [{"type": "command", "command": f"{gbridge} hook {event}", "timeout": HOOK_TIMEOUT_S}]
        }
        if event in HOOK_MATCHERS:
            group["matcher"] = HOOK_MATCHERS[event]
        hooks[event] = [group]
    settings: dict[str, Any] = {"hooks": hooks, "enabledPlugins": dict.fromkeys(disabled_plugins, False)}
    if denied_mcp_servers:
        settings["deniedMcpServers"] = [{"serverName": name} for name in denied_mcp_servers]
    if denied_tools:
        settings["permissions"] = {"deny": denied_tools}
    servers = list_project_mcp_servers(cwd)
    if servers:
        # Rejected by name: no approval dialog, and claude.ai connectors stay.
        settings["disabledMcpjsonServers"] = servers
    return settings


def list_project_mcp_servers(cwd: str) -> list[str]:
    names: set[str] = set()
    seen: set[str] = set()
    for start in (cwd, os.path.realpath(cwd)):
        directory = start
        while directory not in seen:
            seen.add(directory)
            try:
                with open(os.path.join(directory, ".mcp.json"), encoding="utf-8-sig") as f:
                    data: Any = json.load(f)
            except (OSError, ValueError):
                data = None
            servers: Any = cast(dict[str, Any], data).get("mcpServers") if isinstance(data, dict) else None
            if isinstance(servers, dict):
                names.update(str(n) for n in cast(dict[str, Any], servers))
            parent = os.path.dirname(directory)
            if parent == directory:
                break
            directory = parent
    return sorted(names)


def build_run_script(directory: str, cwd: str, claude_flags: str, resume: str = "") -> str:
    q = shell_quote
    start = f"--resume {q(resume)}" if resume else f'"$(cat {q(directory + "/first-prompt")})"'
    # A non-interactive login shell does not load nvm, and claude lives in ~/.local/bin.
    return f"""#!/bin/bash
nvm_bin=$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | sort -V | tail -n 1)
export PATH="$HOME/.local/bin${{nvm_bin:+:$nvm_bin}}:$PATH"
export GBRIDGE_DIR={q(directory)}
cd {q(cwd)} || exit 1
# The tab writes the port once attached: from then on the tunnel is up and every hook reaches the Mac.
for _ in $(seq 1 {PORT_WAIT_S}); do [ -s "$GBRIDGE_DIR/port" ] && break; sleep 1; done
# The host's own environment for its workers, such as which Claude account to use; read at each start.
[ -r "$HOME/.config/gbridge/env.sh" ] && . "$HOME/.config/gbridge/env.sh"
exec claude {claude_flags} --settings {q(directory + "/settings.json")} {start}
"""


def shell_quote(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


def check(worker: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Work that closing would end: shells claude's Bash tool still runs, and subagents that wrote lately."""
    live: list[str] = []
    unknown: list[str] = []
    state = session_state(worker)
    present = state == "present"
    if present:
        panes = tmux("list-panes", "-s", "-t", f"={worker}", "-F", "#{pane_pid}")
        roots = {int(p) for p in panes.stdout.split() if p.isdigit()}
        table = read_processes()
        if table is None:
            unknown.append("the host's process table could not be read")
        else:
            claude_pids = {pid for pid in descendants(table, roots) if "claude" in table[pid][1]}
            for pid, (ppid, command) in sorted(table.items()):
                if ppid not in claude_pids:
                    continue
                if SNAPSHOT_MARK in command:
                    children = [c for c, (pp, _) in table.items() if pp == pid]
                    label = table[children[0]][1] if children else "a shell"
                    live.append(f"background shell {pid} on the host: {label[:80]}")
                elif not any(mark in command for mark in HELPER_MARKS):
                    unknown.append(f"process {pid} under the worker's claude on the host: {command[:80]}")
    conversations: list[Any] = payload.get("conversations") or []
    for conversation in map(str, conversations):
        if not re.fullmatch(r"[0-9a-f-]{36}", conversation):
            continue
        times = subagent_times(conversation)
        if any(time.time() - t < SUBAGENT_QUIET_S for t in times):
            live.append("a native subagent wrote in the last 2 minutes and may still be running")
        elif times:
            unknown.append("a native subagent silent for over 2 minutes cannot be told from a finished one")
    return {"ok": True, "state": state, "live": live, "unknown": unknown}


def read_processes() -> dict[int, tuple[int, str]] | None:
    table: dict[int, tuple[int, str]] = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return None
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", encoding="utf-8", errors="replace") as f:
                stat = f.read()
            with open(f"/proc/{name}/cmdline", "rb") as f:
                command = f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            continue
        ppid = int(stat[stat.rfind(")") + 2 :].split()[1])
        table[int(name)] = (ppid, command)
    return table


def runs_in(pid: int, directory: str) -> bool:
    try:
        cwd = os.path.realpath(os.readlink(f"/proc/{pid}/cwd"))
    except OSError:
        return False
    root = os.path.realpath(directory)
    return cwd == root or cwd.startswith(root + os.sep)


def descendants(table: dict[int, tuple[int, str]], roots: set[int]) -> set[int]:
    found = set(roots)
    changed = True
    while changed:
        changed = False
        for pid, (ppid, _) in table.items():
            if ppid in found and pid not in found:
                found.add(pid)
                changed = True
    return found


def subagent_times(conversation: str) -> list[float]:
    projects = os.path.expanduser("~/.claude/projects")
    times: list[float] = []
    try:
        project_dirs = os.listdir(projects)
    except OSError:
        return times
    for project in project_dirs:
        directory = os.path.join(projects, project, conversation, "subagents")
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            if name.endswith(".jsonl"):
                try:
                    times.append(os.stat(os.path.join(directory, name)).st_mtime)
                except OSError:
                    continue
    return times


def remove(worker: str) -> dict[str, Any]:
    """Files go only once the session is known gone: they hold the token a still running Claude reports with.

    The worktree goes only when nothing in it can be lost; otherwise it stays and `kept` says why.
    """
    state = session_state(worker)
    if state == "present":
        killed = tmux("kill-session", "-t", f"={worker}")
        if killed.returncode != 0:
            return {"ok": False, "error": f"tmux kill-session failed: {killed.stderr.strip()[:200]}"}
        state = session_state(worker)
    if state != "gone":
        return {"ok": False, "error": f"tmux session {worker} is {state}; its files are kept"}
    kept = remove_worktree(worktree_dir(worker)) if os.path.lexists(worktree_dir(worker)) else None
    # A kept worktree keeps its trust: the worker may be resumed there.
    trust_error = None if kept else set_worktree_trust(worktree_dir(worker), False)
    errors: list[str] = []
    directory = worker_dir(worker)
    if os.path.realpath(os.path.dirname(directory)) != os.path.realpath(base_dir()):
        errors.append(f"refusing to remove {directory}: not under {base_dir()}")
    elif os.path.isdir(directory):
        shutil.rmtree(directory, ignore_errors=True)
    if os.path.exists(directory):
        errors.append(f"{directory} is still there")
    # Each fails while another worker's directory is in it, so it never races a launch.
    for shared in (worktrees_dir(), base_dir()):
        with contextlib.suppress(OSError):
            os.rmdir(shared)
    result: dict[str, Any] = {"ok": not errors, "error": "; ".join(errors)}
    if trust_error:
        result["warning"] = f"its trust entry in Claude Code's config stays: {trust_error}"
    if kept:
        result["kept"] = f"{worktree_dir(worker)} is kept: {kept}"
    return result


def remove_worktree(path: str) -> str | None:
    """None once removed; else why it stays: uncommitted changes, or commits on no branch."""
    # Whatever the repo's status.showUntrackedFiles says: a new file is work too.
    status = git(path, "status", "--porcelain", "--untracked-files=all")
    if status.returncode != 0:
        return f"git status failed there ({status.stderr.strip()[:200]})"
    if status.stdout.strip():
        return "it has uncommitted changes"
    if git(path, "symbolic-ref", "-q", "HEAD").returncode != 0:
        holders = git(path, "for-each-ref", "--count=1", "--contains", "HEAD", "refs/heads", "refs/remotes")
        if holders.returncode != 0 or not holders.stdout.strip():
            return "its HEAD commit is on no branch"
    removed = git(path, "worktree", "remove", path)
    if removed.returncode != 0:
        return f"git worktree remove failed ({removed.stderr.strip()[:200]})"
    return None


if __name__ == "__main__":
    main()
