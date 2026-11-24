import fcntl
import json
import re
import shlex
import sqlite3
import sys
import time
import uuid
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

from bridge.claude import read_session_file
from bridge.config import Config, Remote, state_dir
from bridge.deploycheck import is_check_tab
from bridge.drivers import (
    map_command_to_links,
    map_command_to_local_tmux_sessions,
    map_command_to_remote_names,
    read_new_entries,
)
from bridge.errors import UsageError
from bridge.host_workers import HOST_KIND_PREFIX, HOST_WORKER_RE, HostWorker, run_on_host
from bridge.procinfo import ProcessRow, process_args, read_process_table
from bridge.registry import format_age, record_tab_parent
from bridge.remote import read_tmux_sessions
from bridge.restore import build_restore_command
from bridge.rewake_marks import WAITER_SCRIPT
from bridge.tabinput import run_agterm
from bridge.transcript import find_transcript

WORKER_CAP = 5
"""Live workers one lead may run at once; `guild worker` refuses one more."""
_BASH_TOOL_SHELL_MARK = "/shell-snapshots/snapshot-"
_LABEL_MAX_CHARS = 80
_SUBAGENT_QUIET_MS = 120_000
_HOST_STATUS_WORDS = {"active": "active", "idle": "completed"}
_STATUS_WORDS = {"active": "working", "completed": "done, idle", "blocked": "waiting on a dialog"}
_CHILD_HELPER_MARKS = ("mcp", "caffeinate", WAITER_SCRIPT)
_TMUX_OWN_SOCKET_RE = re.compile(r"\btmux(\s+-\S+)*\s+-[LS]\b")
# Left behind by ordinary commands and not anyone's work. A tmux server is judged by its sessions instead.
# zmx hosts a live agterm session: the tab itself, which closing the tab ends.
_ORPHAN_PROGRAMS_NOT_WORK = frozenset({"gpg-agent", "ssh-agent", "tmux", "zmx"})


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
    parent: str | None = None


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


@dataclass(frozen=True)
class ClosedWorker:
    id: int
    conversation_id: str
    tab_name: str
    parent_handle: str
    resume_command: str
    parent_session: str | None
    parent_lead: str | None


@dataclass(frozen=True)
class HostResume:
    """What a closed host worker's resume line holds to start it again as itself, gbridge and all."""

    ssh: str
    worker: str
    remote_checkout: str
    remote_cwd: str
    model_flags: str


@dataclass(frozen=True)
class ConversationRun:
    pid: int
    agterm_session: str | None


def _list_tree_nodes(tree: dict[str, object]) -> list[tuple[dict[str, object], str]]:
    workspaces = tree.get("workspaces")
    return [
        (cast(dict[str, object], tab), str(workspace.get("name")))
        for workspace in cast(list[dict[str, object]], workspaces if isinstance(workspaces, list) else [])
        for tab in cast(list[object], workspace.get("sessions") or [])
        if isinstance(tab, dict)
    ]


def map_tree_to_child_tab(tree: dict[str, object], parent: str, wanted: str) -> ChildTab:
    """By id or exact name among the caller's own children, so no other tab can be reached by this name."""
    nodes = _list_tree_nodes(tree)
    children = [(tab, ws) for tab, ws in nodes if tab.get("parent") == parent]
    matches = [(tab, ws) for tab, ws in children if tab.get("id") == wanted]
    matches = matches or [(tab, ws) for tab, ws in children if tab.get("name") == wanted]
    if not matches:
        raise UsageError(f"{wanted!r} is not a tab nested under yours")
    if len(matches) > 1:
        ids = ", ".join(str(tab.get("id")) for tab, _ in matches)
        raise UsageError(f"{len(matches)} of your tabs are named {wanted!r}; name one by id: {ids}")
    return _map_node_to_child_tab(*matches[0], nodes)


def map_tree_to_tab(tree: dict[str, object], session_id: str) -> ChildTab | None:
    nodes = _list_tree_nodes(tree)
    return next((_map_node_to_child_tab(tab, ws, nodes) for tab, ws in nodes if tab.get("id") == session_id), None)


def _map_node_to_child_tab(
    tab: dict[str, object], workspace_name: str, nodes: list[tuple[dict[str, object], str]]
) -> ChildTab:
    tab_id = str(tab.get("id"))
    surfaces = cast(list[dict[str, object]], tab.get("surfaces") or [])
    restore = tab.get("restoreCommand")
    cwd = tab.get("cwd")
    parent = tab.get("parent")
    return ChildTab(
        id=tab_id,
        name=str(tab.get("name")),
        workspace_name=workspace_name,
        status=str(tab["status"]) if tab.get("status") else None,
        panes=tuple(str(s["paneID"]) for s in surfaces if s.get("paneID")),
        children=tuple(str(other.get("name")) for other, _ in nodes if other.get("parent") == tab_id),
        restore_command=restore if isinstance(restore, str) else None,
        cwd=cwd if isinstance(cwd, str) else None,
        parent=parent if isinstance(parent, str) and parent else None,
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


def check_host_work(conn: sqlite3.Connection, tab: ChildTab, host: HostWorker) -> WorkCheck:
    """On the host, where the worker's processes and transcripts are; the tab here holds only ssh."""
    conversations = [conversation_id for conversation_id, _ in _find_tab_conversations(conn, tab)]
    answer = run_on_host(
        Remote(name=host.host, ssh=host.ssh), {"op": "check", "worker": host.worker, "conversations": conversations}
    )
    if not answer.get("ok"):
        return WorkCheck([], [f"{host.host} could not be asked about the worker's runs: {answer.get('error')}"])
    live = [str(line) for line in cast(list[object], answer.get("live") or [])]
    unknown = [str(line) for line in cast(list[object], answer.get("unknown") or [])]
    unknown.append(f"tmux sessions or runs the worker started itself on {host.host} are not checked")
    if answer.get("state") == "gone":
        unknown.append(f"tmux session {host.worker} is already gone from {host.host}")
    elif answer.get("state") != "present":
        unknown.append(f"{host.host} could not say whether tmux session {host.worker} is there")
    return WorkCheck(live, unknown)


def map_host_tab_to_closed_records(host: HostWorker, sessions: list[TabSession]) -> list[ClosedRecord]:
    """The by-hand line, without gbridge; guild worker reopen reads it to start the worker again with gbridge."""
    records: list[ClosedRecord] = []
    for session in sessions:
        if session.conversation_id:
            claude = f"~/.local/bin/claude {host.model_flags}" if host.model_flags else "~/.local/bin/claude"
            inner = f"cd {shlex.quote(host.remote_cwd)} && {claude} --resume {session.conversation_id}"
            if host.remote_worktree and host.remote_checkout:
                # Claude finds a conversation by its directory: a removed worktree comes back at the same path.
                checkout, worktree = host.remote_checkout, host.remote_worktree
                restore = shlex.join(["git", "-C", checkout, "worktree", "add", "--detach", worktree])
                inner = f"{{ [ -d {shlex.quote(worktree)} ] || {restore}; }} && {inner}"
            records.append(ClosedRecord(session.conversation_id, f"ssh -t {host.ssh} {shlex.quote(inner)}"))
    return records


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
    conn: sqlite3.Connection,
    records: Iterable[ClosedRecord],
    tab_name: str,
    parent_handle: str,
    parent_session: str,
    parent_lead: str | None,
    at_ms: int,
) -> list[int]:
    ids: list[int] = []
    for record in records:
        cursor = conn.execute(
            "INSERT INTO closed_workers (conversation_id, tab_name, parent_handle, parent_session, parent_lead, "
            "resume_command, closed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (record.conversation_id, tab_name, parent_handle, parent_session, parent_lead, record.resume_command,
             at_ms),
        )  # fmt: skip
        ids.append(int(cast(int, cursor.lastrowid)))
    return ids


def hand_closed_workers_to_lead(conn: sqlite3.Connection, conversation_ids: list[str], tab: str, lead: str) -> None:
    """Every record of the conversations, older ones too, else an old id still reopens it under the old lead."""
    # A tab's parent link outlives the tab, and is read before parent_lead to find an ended worker's lead.
    conn.execute("DELETE FROM tab_parents WHERE agterm_session = ?", (tab,))
    for conversation_id in conversation_ids:
        conn.execute(
            "DELETE FROM tab_parents WHERE agterm_session IN (SELECT s.agterm_session FROM surfaces s "
            "JOIN instances i ON i.surface = s.surface WHERE i.conversation_id = ?1 "
            "UNION SELECT reopened_tab FROM closed_workers WHERE conversation_id = ?1)",
            (conversation_id,),
        )
        conn.execute(
            "UPDATE closed_workers SET parent_handle = ?, parent_lead = ?, parent_session = '' "
            "WHERE conversation_id = ?",
            (lead, lead, conversation_id),
        )


def forget_closed_workers(conn: sqlite3.Connection, ids: list[int]) -> None:
    conn.executemany("DELETE FROM closed_workers WHERE id = ?", [(i,) for i in ids])


def list_closed_workers(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM closed_workers WHERE reopened_at IS NULL ORDER BY closed_at DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()


def find_closed_worker(conn: sqlite3.Connection, wanted: str) -> ClosedWorker:
    """By id, else by tab name: the newest record of that name, when every record of it is one conversation."""
    row = None
    if wanted.isdigit():
        row = conn.execute("SELECT * FROM closed_workers WHERE id = ?", (int(wanted),)).fetchone()
    if row is None:
        rows = conn.execute(
            "SELECT * FROM closed_workers WHERE tab_name = ? ORDER BY closed_at DESC, id DESC", (wanted,)
        ).fetchall()
        if not rows:
            raise UsageError(f"no closed worker {wanted!r}; guild worker closed lists them")
        if len({r["conversation_id"] for r in rows}) > 1:
            ids = ", ".join(str(r["id"]) for r in rows)
            raise UsageError(f"{len(rows)} closed workers are named {wanted!r}; name one by id: {ids}")
        row = rows[0]
    return ClosedWorker(
        int(row["id"]), str(row["conversation_id"]), str(row["tab_name"]), str(row["parent_handle"]),
        str(row["resume_command"]), row["parent_session"], row["parent_lead"],
    )  # fmt: skip


def is_lead_of_closed(conn: sqlite3.Connection, closed: ClosedWorker, my_tab: str, my_conversation: str) -> bool:
    """The tab that closed it; or, after that tab is gone, the lead that closed it, by the name no tab can take."""
    if closed.parent_session == my_tab:
        return True
    if closed.parent_lead:
        lead = find_live_lead(conn, closed.parent_lead)
        return lead is not None and lead[0] == my_conversation
    # Closed before the lead was recorded: the handle that closed it must be a lead's, and this tab that lead's.
    row = conn.execute(
        """
        SELECT 1 FROM leads l JOIN surfaces s ON s.surface = l.surface
        JOIN instances i ON i.surface = l.surface AND i.ended_at IS NULL
        WHERE s.handle = ? AND l.retired_at IS NULL AND i.conversation_id = ?
        """,
        (closed.parent_handle, my_conversation),
    ).fetchone()
    return row is not None


def find_live_lead(conn: sqlite3.Connection, name: str | None) -> tuple[str, str | None] | None:
    """(conversation, agterm tab) of a lead that is not retired and runs now."""
    if not name:
        return None
    row = conn.execute(
        """
        SELECT i.conversation_id, s.agterm_session FROM leads l JOIN surfaces s ON s.surface = l.surface
        JOIN instances i ON i.surface = l.surface AND i.ended_at IS NULL AND i.conversation_id IS NOT NULL
        WHERE l.name = ? AND l.retired_at IS NULL ORDER BY i.proc_start_ms DESC LIMIT 1
        """,
        (name,),
    ).fetchone()
    return (str(row["conversation_id"]), row["agterm_session"]) if row else None


def find_reopened_tabs(conn: sqlite3.Connection, conversation_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT reopened_tab FROM closed_workers WHERE conversation_id = ? AND reopened_tab IS NOT NULL",
        (conversation_id,),
    ).fetchall()
    return [str(r[0]) for r in rows]


def mark_reopened(conn: sqlite3.Connection, conversation_id: str, tab: str, at_ms: int) -> None:
    """Its records stay, so a reopen that never came up can be tried again."""
    conn.execute(
        "UPDATE closed_workers SET reopened_at = ?, reopened_tab = ? WHERE conversation_id = ?",
        (at_ms, tab, conversation_id),
    )
    conn.execute("DELETE FROM stray_workers WHERE conversation_id = ?", (conversation_id,))


def unmark_reopened(conn: sqlite3.Connection, conversation_id: str, tab: str) -> None:
    """Only this attempt's mark: a later reopen may have marked another tab meanwhile."""
    conn.execute(
        "UPDATE closed_workers SET reopened_at = NULL, reopened_tab = NULL "
        "WHERE conversation_id = ? AND reopened_tab = ?",
        (conversation_id, tab),
    )


@contextmanager
def hold_reopen_lock() -> Generator[None]:
    """From the checks to the new tab, so two reopens of one worker never both pass them."""
    path = state_dir() / "worker-reopen.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def find_conversation_runs(conn: sqlite3.Connection, conversation_id: str) -> list[ConversationRun]:
    rows = conn.execute(
        """
        SELECT i.pid, s.agterm_session FROM instances i LEFT JOIN surfaces s ON s.surface = i.surface
        WHERE i.conversation_id = ? AND i.ended_at IS NULL
        """,
        (conversation_id,),
    ).fetchall()
    return [ConversationRun(int(r["pid"]), r["agterm_session"]) for r in rows]


def is_host_resume(resume_command: str) -> bool:
    return resume_command.startswith("ssh ")


def map_resume_command_to_host_resume(resume_command: str, conversation_id: str) -> HostResume | None:
    """The line map_host_tab_to_closed_records writes; None for one from before workers had their own worktree."""
    try:
        words = shlex.split(resume_command)
        inner = shlex.split(words[3]) if len(words) == 4 and words[:2] == ["ssh", "-t"] else []
    except ValueError:
        return None
    match inner:
        case [
            "{", "[", "-d", worktree, "]", "||", "git", "-C", checkout, "worktree", "add", "--detach", again, "}",
            "&&", "cd", cwd, "&&", "~/.local/bin/claude", *flags, "--resume", conversation,
        ] if again == f"{worktree};" and conversation == conversation_id:  # fmt: skip
            pass
        case _:
            return None
    worker = PurePosixPath(worktree).name
    if PurePosixPath(worktree).parent.parts[-4:] != (".local", "state", "gbridge", "worktrees"):
        return None
    if not HOST_WORKER_RE.fullmatch(worker) or not (cwd == worktree or cwd.startswith(f"{worktree}/")):
        return None
    return HostResume(words[2], worker, checkout, cwd, shlex.join(flags))


def map_resume_command_to_cwd(resume_command: str) -> Path | None:
    """The directory a local resume line starts with: the tab opens there too."""
    try:
        words = shlex.split(resume_command)
    except ValueError:
        return None
    return Path(words[1]) if len(words) > 3 and words[0] == "cd" and words[2] == "&&" else None


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
    """Every tab nested under the lead's, idle ones included: an idle worker still holds its Claude process. A deploy
    check's tab is no worker, though the check opens it under the tab that ran it."""
    workers: list[LiveWorker] = []
    for workspace in cast(list[dict[str, object]], tree.get("workspaces") or []):
        for tab in cast(list[dict[str, object]], workspace.get("sessions") or []):
            if tab.get("parent") != parent or is_check_tab(str(tab.get("name"))):
                continue
            session_id = str(tab.get("id"))
            changed = tab.get("statusChangedAt")
            row = conn.execute(
                """
                SELECT min(i.proc_start_ms) AS started, max(i.kind) AS kind, max(i.status) AS status,
                       max(i.last_activity_at) AS since
                FROM surfaces s JOIN instances i ON i.surface = s.surface
                WHERE s.agterm_session = ? AND i.ended_at IS NULL
                """,
                (session_id,),
            ).fetchone()
            name = str(tab.get("name") or session_id)
            status = str(tab.get("status") or "unknown")
            since = int(changed * 1000) if isinstance(changed, int | float) else None
            kind = str(row["kind"] or "")
            if kind.startswith(HOST_KIND_PREFIX):
                # agterm sees only ssh there; the worker's hooks know its state.
                name = f"{name} (on {kind.removeprefix(HOST_KIND_PREFIX)})"
                status = _HOST_STATUS_WORDS.get(str(row["status"]), status)
                since = int(row["since"]) if row["since"] is not None else since
            workers.append(
                LiveWorker(
                    session_id=session_id,
                    name=name,
                    status=status,
                    status_since_ms=since,
                    started_ms=int(row["started"]) if row["started"] is not None else None,
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


def count_busy_agents(conn: sqlite3.Connection) -> int:
    """Agents working on this Mac right now, leads, the assistant and workers alike: idle ones hold memory but no CPU.

    A host worker works on its host; here it is only a tab running ssh.
    """
    row = conn.execute(
        f"""
        SELECT count(*) FROM instances
        WHERE ended_at IS NULL AND status = 'active' AND coalesce(kind, '') NOT LIKE '{HOST_KIND_PREFIX}%'
        """
    ).fetchone()
    return int(row[0])


def count_busy_host_agents(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        f"""
        SELECT substr(kind, {len(HOST_KIND_PREFIX) + 1}) AS host, count(*) AS n FROM instances
        WHERE ended_at IS NULL AND status = 'active' AND kind LIKE '{HOST_KIND_PREFIX}%' GROUP BY host
        """
    ).fetchall()
    return {str(r["host"]): int(r["n"]) for r in rows}


def worker_queue_dir() -> Path:
    return state_dir() / "worker-queue"


def queue_worker_launch(launch: dict[str, str]) -> int:
    """Writes the launch for serve to start once a slot frees; returns its place in the queue, 1 = next."""
    directory = worker_queue_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{time.time_ns()}-{uuid.uuid4().hex[:8]}.json"
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(launch))
    tmp.replace(path)
    return len(list_queued_launches())


def list_queued_launches() -> list[Path]:
    directory = worker_queue_dir()
    return sorted(directory.glob("*.json")) if directory.is_dir() else []


def is_reopen_queued(conversation_id: str) -> bool:
    for path in list_queued_launches():
        try:
            launch = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(launch, dict) and cast(dict[str, object], launch).get("reopens") == conversation_id:
            return True
    return False


def start_worker_tab(conn: sqlite3.Connection, config: Config, parent: str, name: str, cwd: Path, command: str) -> str:
    started = run_agterm(
        config,
        ["session", "new", "--parent", parent, "--no-select", "--name", name, "--cwd", str(cwd),
         "--command", command, "--json"],
    )  # fmt: skip
    if started is None:
        raise UsageError("agterm could not create the tab")
    session_id = str(json.loads(started)["result"]["id"])
    record_tab_parent(conn, session_id, parent)
    return session_id
