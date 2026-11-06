import os
import sqlite3

from bridge.board import Party
from bridge.claude import read_session_file
from bridge.config import load_config
from bridge.db import now_ms, transaction
from bridge.errors import UsageError
from bridge.gitinfo import read_git_info
from bridge.procinfo import process_start_ms
from bridge.registry import (
    apply_session_file_status,
    find_live_instance,
    make_instance_id,
    map_session_file_to_observation,
    observe_instance,
    sync_tabs,
)


def ensure_current_instance(conn: sqlite3.Connection) -> str:
    """Identifies the calling session by the Claude process that spawned this shell."""
    pid_text = os.environ.get("CLAUDE_PID", "")
    if not pid_text.isdigit():
        raise UsageError("not inside a Claude Code session (CLAUDE_PID is not set)")
    pid = int(pid_text)
    proc_start = process_start_ms(pid)
    if proc_start is None:
        raise UsageError(f"Claude process {pid} is not running")
    instance_id = make_instance_id(pid, proc_start)
    session_file = read_session_file(pid)
    git = read_git_info(session_file.cwd) if session_file and session_file.cwd else None
    # The session file can be newer than the last hook (a missed /clear or /resume), so always apply it.
    with transaction(conn):
        if find_live_instance(conn, pid, proc_start) is None:
            observation = map_session_file_to_observation(session_file) if session_file else None
            if observation is None:
                raise UsageError(f"no Claude session file for pid {pid}")
            observe_instance(conn, observation, now_ms())
        if session_file is not None:
            apply_session_file_status(conn, instance_id, session_file, git)
        row = conn.execute("SELECT ended_at FROM instances WHERE instance_id = ?", (instance_id,)).fetchone()
    if row is None or row["ended_at"] is not None:
        raise UsageError(f"session {instance_id} has ended")
    return instance_id


def _read_party_row(conn: sqlite3.Connection, instance_id: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        """
        SELECT i.conversation_id, i.pid, i.surface, s.handle
        FROM instances i LEFT JOIN surfaces s ON s.surface = i.surface
        WHERE i.instance_id = ?
        """,
        (instance_id,),
    ).fetchone()
    return row


def current_party(conn: sqlite3.Connection) -> Party:
    instance_id = ensure_current_instance(conn)
    row = _read_party_row(conn, instance_id)
    if row is not None and row["surface"] is not None and row["handle"] is None:
        # Handles come from reconcile, and a new tab's first message can beat it; the name would stay in that message.
        sync_tabs(conn, load_config(), now_ms())
        row = _read_party_row(conn, instance_id)
    if row is None or row["conversation_id"] is None:
        raise UsageError("this session has no known conversation yet; send one prompt first")
    newer = conn.execute(
        """
        SELECT pid FROM instances WHERE conversation_id = ? AND ended_at IS NULL AND instance_id != ?
          AND proc_start_ms > (SELECT proc_start_ms FROM instances WHERE instance_id = ?)
        """,
        (row["conversation_id"], instance_id, instance_id),
    ).fetchone()
    if newer is not None:
        raise UsageError(f"this conversation is also open in a newer session (pid {newer['pid']}); act from that one")
    return Party(conversation_id=str(row["conversation_id"]), handle=str(row["handle"] or f"pid:{row['pid']}"))
