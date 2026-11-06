import shlex
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from bridge.claude import list_session_files
from bridge.drivers import LOCAL_HOST
from bridge.procinfo import ProcessArgs, process_args
from bridge.transcript import find_transcript

# Claude Code wraps every Bash call in a shell that sources this snapshot; MCP servers and caffeinate do not match it.
_SHELL_MARK = "/shell-snapshots/snapshot-"
_PS_TIMEOUT_S = 5
# Left running by a Bash call or by bridge itself without being anyone's work: ssh proxies, browsers, wake locks.
_NOT_DETACHED_WORK = (
    "-m bridge",
    "session-manager-plugin",
    "ssm start-session",
    "caffeinate",
    "Chrome",
    "ssh ",
    "tmux",
)
_NOT_A_RUN = ("vi", "vim", "nvim", "nano", "emacs", "less", "more", "man", "top", "htop", "btop", "tmux")
_LOCAL_LINK_MS = 7 * 24 * 3_600_000
_LOCAL_LINK_BEFORE_START_MS = 10 * 60_000


@dataclass(frozen=True)
class ProcessRow:
    pid: int
    ppid: int
    elapsed_ms: int
    command: str


@dataclass(frozen=True)
class RunningShell:
    label: str
    for_ms: int


@dataclass(frozen=True)
class SessionActivity:
    file_status: str | None
    """Claude's own word from its session file: idle, busy, or shell for a turn that ended with a shell running."""
    shells: tuple[RunningShell, ...]
    last_write_ms: int | None


def read_process_table() -> list[ProcessRow] | None:
    try:
        done = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,etime=,command="],
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return map_ps_output_to_rows(done.stdout) if done.returncode == 0 else None


def map_ps_output_to_rows(output: str) -> list[ProcessRow]:
    rows: list[ProcessRow] = []
    for line in output.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[0].isdigit() and parts[1].isdigit():
            rows.append(ProcessRow(int(parts[0]), int(parts[1]), convert_etime_to_ms(parts[2]), parts[3]))
    return rows


def convert_etime_to_ms(text: str) -> int:
    """ps's [[dd-]hh:]mm:ss; an unreadable value counts as just started."""
    days, _, clock = text.rpartition("-")
    try:
        seconds = 0
        for part in clock.split(":"):
            seconds = seconds * 60 + int(part)
        return (int(days or 0) * 86_400 + seconds) * 1000
    except ValueError:
        return 0


def map_processes_to_shells(rows: list[ProcessRow]) -> dict[int, tuple[RunningShell, ...]]:
    """By the pid of the claude that owns them: its Bash calls still running, foreground or background."""
    children: dict[int, list[ProcessRow]] = {}
    for row in rows:
        children.setdefault(row.ppid, []).append(row)
    shells: dict[int, list[RunningShell]] = {}
    for row in rows:
        if _SHELL_MARK in row.command:
            label = convert_command_to_label(children[row.pid][0].command) if row.pid in children else "a shell"
            shells.setdefault(row.ppid, []).append(RunningShell(label, row.elapsed_ms))
    return {pid: tuple(found) for pid, found in shells.items()}


def convert_command_to_label(command: str) -> str:
    """Program and first plain words only, since an argument may hold a secret and the page is on a shared screen."""
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    if "-c" in words:
        return "a script"
    names = [Path(word).name[:30] for word in words if not word.startswith("-") and "=" not in word]
    if names and names[0] == "sleep":
        return "a watch loop"
    return " ".join(names[:2]) or "a shell"


def map_processes_to_detached_work(
    rows: list[ProcessRow], live_claude_pids: set[int], read_args: Callable[[int], ProcessArgs | None]
) -> dict[int, list[RunningShell]]:
    """Work a Bash call left behind (nohup, setsid): no longer a child of its claude, but it inherited CLAUDE_PID.

    macOS hides the environment of platform binaries, so `nohup bash run.sh` shows through the programs it runs.
    """
    parent_of = {row.pid: row.ppid for row in rows}

    def descends_from(pid: int, ancestor: int) -> bool:
        seen = 0
        while pid in parent_of and seen < 64:
            if pid == ancestor:
                return True
            pid = parent_of[pid]
            seen += 1
        return False

    owners: dict[int, int] = {}
    for row in rows:
        args = None if any(mark in row.command for mark in _NOT_DETACHED_WORK) else read_args(row.pid)
        owner = args.env.get("CLAUDE_PID", "") if args else ""
        if owner.isdigit() and int(owner) in live_claude_pids and not descends_from(row.pid, int(owner)):
            owners[row.pid] = int(owner)
    work: dict[int, list[RunningShell]] = {}
    for row in rows:
        if row.pid in owners and row.ppid not in owners:
            shell = RunningShell(convert_command_to_label(row.command), row.elapsed_ms)
            work.setdefault(owners[row.pid], []).append(shell)
    return work


def read_local_tmux_panes() -> list[tuple[str, int]]:
    """(session name, pane pid); no tmux server, or one that does not answer, is no panes."""
    try:
        done = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{session_name}\t#{pane_pid}"],
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    panes: list[tuple[str, int]] = []
    for line in done.stdout.splitlines() if done.returncode == 0 else []:
        name, _, pid = line.partition("\t")
        if pid.isdigit():
            panes.append((name, int(pid)))
    return panes


def map_panes_to_runs(panes: list[tuple[str, int]], rows: list[ProcessRow]) -> dict[str, RunningShell]:
    """By tmux session: the first program running under a pane's shell. An idle shell or an open editor is no run."""
    by_pid = {row.pid: row for row in rows}
    children: dict[int, list[ProcessRow]] = {}
    for row in rows:
        children.setdefault(row.ppid, []).append(row)
    runs: dict[str, RunningShell] = {}
    for name, pane in panes:
        root = by_pid.get(pane)
        # `tmux new -d "k6 run"` makes the program itself the pane's process, with no shell above it.
        candidates = [root] if root and not _is_shell(root.command) else children.get(pane, [])
        for candidate in candidates:
            label = convert_command_to_label(candidate.command)
            if label != "a shell" and label.split(" ")[0] not in _NOT_A_RUN and name not in runs:
                runs[name] = RunningShell(f"tmux {name}: {label}", candidate.elapsed_ms)
    return runs


def _is_shell(command: str) -> bool:
    return Path(command.split(" ")[0]).name.lstrip("-") in ("sh", "bash", "zsh", "dash", "fish")


def load_local_runs_by_conversation(
    conn: sqlite3.Connection, runs: dict[str, RunningShell], now_ms: int
) -> dict[str, list[RunningShell]]:
    """A link holds for days, since a run outlives its command, but not for a later run under the same name."""
    linked: dict[str, list[RunningShell]] = {}
    for row in conn.execute(
        "SELECT conversation_id, session, last_seen_at FROM remote_drivers WHERE host = ? AND last_seen_at > ?",
        (LOCAL_HOST, now_ms - _LOCAL_LINK_MS),
    ):
        run = runs.get(str(row["session"]))
        if run and now_ms - run.for_ms <= int(row["last_seen_at"]) + _LOCAL_LINK_BEFORE_START_MS:
            linked.setdefault(str(row["conversation_id"]), []).append(run)
    return linked


def read_session_activity(conn: sqlite3.Connection, now_ms: int) -> dict[int, SessionActivity] | None:
    """Local files, one ps and every process's arguments: a background loop's work, never a page poll's."""
    rows = read_process_table()
    if rows is None:
        return None
    shells = map_processes_to_shells(rows)
    statuses = {item.pid: item.status for item in list_session_files()}
    instances = conn.execute(
        """
        SELECT i.pid, i.conversation_id, c.transcript_path FROM instances i
        LEFT JOIN conversations c ON c.conversation_id = i.conversation_id WHERE i.ended_at IS NULL
        """
    ).fetchall()
    detached = map_processes_to_detached_work(rows, {int(row["pid"]) for row in instances}, process_args)
    local_runs = load_local_runs_by_conversation(conn, map_panes_to_runs(read_local_tmux_panes(), rows), now_ms)
    activity: dict[int, SessionActivity] = {}
    for row in instances:
        pid = int(row["pid"])
        running = (*shells.get(pid, ()), *detached.get(pid, ()), *local_runs.get(str(row["conversation_id"]), ()))
        activity[pid] = SessionActivity(
            statuses.get(pid), running, _read_last_write_ms(row["conversation_id"], row["transcript_path"])
        )
    return activity


def _read_last_write_ms(conversation_id: str | None, known_path: str | None) -> int | None:
    path = find_transcript(conversation_id, known_path) if conversation_id else None
    if path is None:
        return None
    # A native subagent writes only its own file while the main transcript stays silent.
    files = [path, *path.with_suffix("").glob("subagents/*.jsonl")]
    times: list[float] = []
    for file in files:
        try:
            times.append(file.stat().st_mtime)
        except OSError:
            continue
    return int(max(times) * 1000) if times else None
