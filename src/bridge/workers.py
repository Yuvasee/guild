import re
import shlex
import sqlite3
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from bridge.claude import read_session_file
from bridge.config import Config
from bridge.drivers import (
    map_command_to_links,
    map_command_to_local_tmux_sessions,
    map_command_to_remote_names,
    read_new_entries,
)
from bridge.errors import UsageError
from bridge.procinfo import ProcessRow, process_args, read_process_table
from bridge.registry import format_age
from bridge.remote import read_tmux_sessions
from bridge.restore import build_restore_command
from bridge.transcript import find_transcript

_BASH_TOOL_SHELL_MARK = "/shell-snapshots/snapshot-"
_LABEL_MAX_CHARS = 80
_SUBAGENT_QUIET_MS = 120_000
_STATUS_WORDS = {"active": "working", "completed": "done, idle", "blocked": "waiting on a dialog"}
_CHILD_HELPER_MARKS = ("mcp", "caffeinate")
_TMUX_OWN_SOCKET_RE = re.compile(r"\btmux(\s+-\S+)*\s+-[LS]\b")
# Left behind by ordinary commands and not anyone's work. A tmux server is judged by its sessions instead.
_ORPHAN_PROGRAMS_NOT_WORK = frozenset({"gpg-agent", "ssh-agent", "tmux"})


@dataclass(frozen=True)
class ChildTab:
    id: str
    name: str
    workspace_name: str
    status: str | None
    panes: tuple[str, ...]
    children: tuple[str, ...]
    restore_command: str | None
    cwd: str | None


@dataclass(frozen=True)
class TabSession:
    surface: str
    conversation_id: str | None
    pid: int
    live: bool
    status: str
    transcript_path: str | None
    cwd: str | None


@dataclass(frozen=True)
class WorkCheck:
    live: list[str]
    unknown: list[str]


@dataclass(frozen=True)
class ClosedRecord:
    conversation_id: str
    resume_command: str


def map_tree_to_child_tab(tree: dict[str, object], parent: str, wanted: str) -> ChildTab:
    """By id or exact name among the caller's own children, so no other tab can be reached by this name."""
    workspaces = tree.get("workspaces")
    nodes = [
        (cast(dict[str, object], tab), str(workspace.get("name")))
        for workspace in cast(list[dict[str, object]], workspaces if isinstance(workspaces, list) else [])
        for tab in cast(list[object], workspace.get("sessions") or [])
        if isinstance(tab, dict)
    ]
    children = [(tab, ws) for tab, ws in nodes if tab.get("parent") == parent]
    matches = [(tab, ws) for tab, ws in children if tab.get("id") == wanted]
    matches = matches or [(tab, ws) for tab, ws in children if tab.get("name") == wanted]
    if not matches:
        raise UsageError(f"{wanted!r} is not a tab nested under yours")
    if len(matches) > 1:
        ids = ", ".join(str(tab.get("id")) for tab, _ in matches)
        raise UsageError(f"{len(matches)} of your tabs are named {wanted!r}; name one by id: {ids}")
    tab, workspace_name = matches[0]
    tab_id = str(tab.get("id"))
    surfaces = cast(list[dict[str, object]], tab.get("surfaces") or [])
    restore = tab.get("restoreCommand")
    cwd = tab.get("cwd")
    return ChildTab(
        id=tab_id,
        name=str(tab.get("name")),
        workspace_name=workspace_name,
        status=str(tab["status"]) if tab.get("status") else None,
        panes=tuple(str(s["paneID"]) for s in surfaces if s.get("paneID")),
        children=tuple(str(other.get("name")) for other, _ in nodes if other.get("parent") == tab_id),
        restore_command=restore if isinstance(restore, str) else None,
        cwd=cwd if isinstance(cwd, str) else None,
    )


def find_tab_sessions(conn: sqlite3.Connection, tab: ChildTab) -> list[TabSession]:
    """In each pane its live sessions, or with none live the last that ran there: all that closing the tab ends."""
    marks = ",".join("?" * len(tab.panes))
    rows = conn.execute(
        f"""
        SELECT i.surface, i.conversation_id, i.pid, i.ended_at, i.status, i.cwd, c.transcript_path FROM instances i
        LEFT JOIN conversations c ON c.conversation_id = i.conversation_id
        WHERE i.surface IN ({marks}) ORDER BY i.proc_start_ms DESC
        """,
        tab.panes,
    ).fetchall()
    by_pane: dict[str, list[TabSession]] = {}
    for r in rows:
        by_pane.setdefault(str(r["surface"]), []).append(
            TabSession(str(r["surface"]), r["conversation_id"], int(r["pid"]), r["ended_at"] is None, str(r["status"]),
                       r["transcript_path"], r["cwd"])
        )  # fmt: skip
    kept: list[TabSession] = []
    for sessions in by_pane.values():
        kept.extend([s for s in sessions if s.live] or sessions[:1])
    return kept


def _find_tab_conversations(conn: sqlite3.Connection, tab: ChildTab) -> list[tuple[str, str | None]]:
    """Every conversation the tab has held: /clear moves on to a new one and leaves the old one's runs going."""
    marks = ",".join("?" * len(tab.panes))
    rows = conn.execute(
        f"""
        SELECT c.conversation_id, c.transcript_path FROM conversations c
        JOIN instances i ON i.instance_id = c.instance_id WHERE i.surface IN ({marks}) ORDER BY c.first_seen_at DESC
        """,
        tab.panes,
    ).fetchall()
    return [(str(r["conversation_id"]), r["transcript_path"]) for r in rows]


def find_mid_turn(tab: ChildTab, sessions: list[TabSession]) -> list[str]:
    if any(s.live and s.status == "active" for s in sessions) or tab.status == "active":
        return [f"{tab.name} is mid-turn"]
    # A dialog holds the turn open, whatever the session reports.
    return [f"{tab.name} is blocked on a prompt"] if tab.status == "blocked" else []


def map_processes_to_live_work(rows: list[ProcessRow], claude_pids: set[int], agterm_session: str) -> list[str]:
    """Bash-tool shells a claude still owns, and what they left behind: MCP servers and caffeinate do not count.

    A nohup'd or disowned command loses its parent, and only its inherited tab id still ties it to the tab.
    """
    children: dict[int, list[ProcessRow]] = {}
    for row in rows:
        children.setdefault(row.ppid, []).append(row)
    work: list[str] = []
    for row in rows:
        if row.ppid in claude_pids and _BASH_TOOL_SHELL_MARK in row.command:
            running = children.get(row.pid)
            label = running[0].command if running else "a shell"
            work.append(f"background shell {row.pid}: {label[:_LABEL_MAX_CHARS]}")
        elif row.ppid == 1 and not _is_orphan_not_work(row.command):
            args = process_args(row.pid)
            if args is not None and args.env.get("AGTERM_SESSION_ID") == agterm_session:
                work.append(f"detached process {row.pid}: {row.command[:_LABEL_MAX_CHARS]}")
    return work


def find_unrecognised_children(rows: list[ProcessRow], claude_pids: set[int]) -> list[str]:
    """A command that exec'd over its Bash shell loses the mark, so it cannot be told from a helper with certainty."""
    return [
        f"process {row.pid} under the worker's claude: {row.command[:_LABEL_MAX_CHARS]}"
        for row in rows
        if row.ppid in claude_pids
        and _BASH_TOOL_SHELL_MARK not in row.command
        and not any(mark in row.command for mark in _CHILD_HELPER_MARKS)
    ]


def _is_orphan_not_work(command: str) -> bool:
    return command.endswith("[mux]") or Path(command.split(" ", 1)[0]).name in _ORPHAN_PROGRAMS_NOT_WORK


def check_background_work(
    conn: sqlite3.Connection, config: Config, tab: ChildTab, sessions: list[TabSession]
) -> WorkCheck:
    live: list[str] = []
    unknown: list[str] = []
    rows = read_process_table()
    claude_pids = {s.pid for s in sessions if s.live}
    if rows is None:
        unknown.append("ps failed, so background shells in the tab were not checked")
    else:
        live.extend(map_processes_to_live_work(rows, claude_pids, tab.id))
        unknown.extend(find_unrecognised_children(rows, claude_pids))
    for conversation_id, known_path in _find_tab_conversations(conn, tab):
        path = find_transcript(conversation_id, known_path)
        if path is None:
            unknown.append(f"no transcript for {conversation_id}, so its tmux and remote runs were not checked")
            continue
        recent, quiet = _read_subagent_logs(path)
        if recent:
            live.append("a native subagent wrote in the last 2 minutes and may still be running")
        elif quiet:
            unknown.append("a native subagent silent for over 2 minutes cannot be told from a finished one")
        _check_tmux_work(conn, config, conversation_id, path, live, unknown)
    return WorkCheck(live, unknown)


def _read_subagent_logs(path: Path) -> tuple[bool, bool]:
    """(recent, quiet). A background subagent leaves the session idle and has no process: only its file can move."""
    times: list[float] = []
    for file in path.with_suffix("").glob("subagents/*.jsonl"):
        try:
            times.append(file.stat().st_mtime)
        except OSError:
            continue
    recent = [t for t in times if (time.time() - t) * 1000 < _SUBAGENT_QUIET_MS]
    return bool(recent), bool(times) and not recent


def _check_tmux_work(
    conn: sqlite3.Connection, config: Config, conversation_id: str, path: Path, live: list[str], unknown: list[str]
) -> None:
    # The transcript is read here as well because the watcher's table lags by a scan and misses a young worker.
    entries = read_new_entries(path, -1, 0, conversation_id, first_scan_bytes=sys.maxsize)
    commands = [command for command, _ in entries.commands]
    by_host: dict[str | None, set[str]] = {}
    touched: set[str] = set()
    for command in commands:
        by_host.setdefault(None, set()).update(map_command_to_local_tmux_sessions(command))
        touched |= map_command_to_remote_names(command, config.remotes)
        for link in map_command_to_links(command, conversation_id, config.remotes):
            by_host.setdefault(link.host, set()).add(link.session)
    stored = conn.execute("SELECT host, session FROM remote_drivers WHERE conversation_id = ?", (conversation_id,))
    for row in stored:
        by_host.setdefault(str(row["host"]), set()).add(str(row["session"]))
    remotes = {remote.name: remote for remote in config.remotes}
    for host, wanted in by_host.items():
        if not wanted:
            continue
        where = host or "this machine"
        if host is not None and host not in remotes:
            unknown.append(f"tmux sessions on {host}, no longer a configured remote: {_join(wanted)}")
            continue
        existing = read_tmux_sessions(remotes[host] if host else None)
        if existing is None:
            unknown.append(f"could not list tmux on {where}, which this worker used: {_join(wanted)}")
        else:
            live.extend(f"tmux session {name} on {where}, used by this worker" for name in sorted(wanted & existing))
    if any(_TMUX_OWN_SOCKET_RE.search(command) for command in commands):
        unknown.append("this worker ran tmux on a socket of its own (-L or -S), which is not checked")
    if touched:
        unknown.append(f"a run started outside tmux on {_join(touched)} cannot be seen from here")


def find_undelivered_reports(conn: sqlite3.Connection, tab: ChildTab, lead_conversation: str) -> list[str]:
    conversations = [conversation_id for conversation_id, _ in _find_tab_conversations(conn, tab)]
    if not conversations:
        return []
    marks = ",".join("?" * len(conversations))
    rows = conn.execute(
        f"""
        SELECT m.id, m.body FROM messages m JOIN message_recipients r ON r.message_id = m.id
        WHERE m.from_conversation IN ({marks}) AND r.conversation_id = ? AND m.cancelled_at IS NULL
          AND r.offered_at IS NULL AND r.acked_at IS NULL AND r.done_at IS NULL AND r.declined_at IS NULL
        ORDER BY m.id
        """,
        (*conversations, lead_conversation),
    ).fetchall()
    return [f"#{r['id']}: {str(r['body'])[:_LABEL_MAX_CHARS]} (bridge show {r['id']})" for r in rows]


def map_tab_to_closed_records(tab: ChildTab, sessions: list[TabSession]) -> list[ClosedRecord]:
    records: list[ClosedRecord] = []
    for session in sessions:
        if session.conversation_id:
            command = _build_resume_command(tab, session, session.conversation_id)
            records.append(ClosedRecord(session.conversation_id, command))
    return records


def _build_resume_command(tab: ChildTab, session: TabSession, conversation_id: str) -> str:
    args = process_args(session.pid) if session.live else None
    if args is not None and args.argv:
        launch = build_restore_command(args.argv, conversation_id)
    elif tab.restore_command and conversation_id in tab.restore_command:
        launch = tab.restore_command
    else:
        launch = f"claude --resume {shlex.quote(conversation_id)}"
    # Claude looks a conversation up under the directory it started in, which the session file keeps.
    session_file = read_session_file(session.pid) if session.live else None
    cwd = (session_file.cwd if session_file else None) or session.cwd or tab.cwd
    return f"cd {shlex.quote(cwd)} && {launch}" if cwd else launch


def record_closed_workers(
    conn: sqlite3.Connection, records: Iterable[ClosedRecord], tab_name: str, parent_handle: str, at_ms: int
) -> list[int]:
    ids: list[int] = []
    for record in records:
        cursor = conn.execute(
            "INSERT INTO closed_workers (conversation_id, tab_name, parent_handle, resume_command, closed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (record.conversation_id, tab_name, parent_handle, record.resume_command, at_ms),
        )
        ids.append(int(cast(int, cursor.lastrowid)))
    return ids


def forget_closed_workers(conn: sqlite3.Connection, ids: list[int]) -> None:
    conn.executemany("DELETE FROM closed_workers WHERE id = ?", [(i,) for i in ids])


def list_closed_workers(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM closed_workers ORDER BY closed_at DESC, id DESC LIMIT ?", (limit,)).fetchall()


def _join(names: Iterable[str]) -> str:
    return ", ".join(sorted(names))


@dataclass(frozen=True)
class LiveWorker:
    session_id: str
    name: str
    status: str
    status_since_ms: int | None
    started_ms: int | None


def list_live_workers(conn: sqlite3.Connection, tree: dict[str, object], parent: str) -> list[LiveWorker]:
    """Every tab nested under the lead's, idle ones included: an idle worker still holds its Claude process."""
    workers: list[LiveWorker] = []
    for workspace in cast(list[dict[str, object]], tree.get("workspaces") or []):
        for tab in cast(list[dict[str, object]], workspace.get("sessions") or []):
            if tab.get("parent") != parent:
                continue
            session_id = str(tab.get("id"))
            changed = tab.get("statusChangedAt")
            started = conn.execute(
                """
                SELECT min(i.proc_start_ms) FROM surfaces s JOIN instances i ON i.surface = s.surface
                WHERE s.agterm_session = ? AND i.ended_at IS NULL
                """,
                (session_id,),
            ).fetchone()[0]
            workers.append(
                LiveWorker(
                    session_id=session_id,
                    name=str(tab.get("name") or session_id),
                    status=str(tab.get("status") or "unknown"),
                    status_since_ms=int(changed * 1000) if isinstance(changed, int | float) else None,
                    started_ms=int(started) if started is not None else None,
                )
            )
    return workers


def map_workers_to_cap_refusal(workers: list[LiveWorker], cap: int, now: int) -> str:
    """Oldest first, each with its age and last state, so the lead can pick which to close."""
    lines = [
        f"{len(workers)} of at most {cap} workers already run under this lead. Close one before starting another: "
        "guild worker close <tab name>, after its report is in."
    ]
    for worker in sorted(workers, key=lambda w: (w.started_ms is None, w.started_ms or 0, w.name)):
        age = f"started {format_age(now - worker.started_ms)} ago" if worker.started_ms is not None else "age unknown"
        word = _STATUS_WORDS.get(worker.status, worker.status)
        since = f" for {format_age(now - worker.status_since_ms)}" if worker.status_since_ms is not None else ""
        lines.append(f"- {worker.name}: {age}, {word}{since}")
    return "\n".join(lines)
