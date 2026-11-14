import io
import os
import sqlite3
import sys
import time
import traceback
from typing import TextIO

from bridge.board import is_delivery_owner, next_hint_epoch, take_delivery, write_hint
from bridge.config import Config, db_path, load_config
from bridge.db import connect, transaction
from bridge.errlog import log_error
from bridge.feed import TYPED_MESSAGE_SQL, release_dead_typing_claims
from bridge.leads import is_conversation_locked, is_surface_locked
from bridge.procinfo import process_start_ms
from bridge.registry import make_instance_id, record_activity

WAKE = 2
UNHEARD = 3
"""Claude Code closed the waiter's channel: the waiter leaves, and a newer one or a typer takes over."""


def run_rewake() -> int:
    """`bridge-hook rewake`, run by an idle tab's waiter: exit 2 with the tab's mail on stderr starts a turn with it.

    Anything else exits 0 with nothing written. The waiter passes the fd that reaches Claude Code as
    BRIDGE_REWAKE_FD and keeps stderr for errors.
    """
    pid_text = os.environ.get("CLAUDE_PID", "")
    if not pid_text.isdigit():
        return 0
    fd_text = os.environ.get("BRIDGE_REWAKE_FD", "")
    try:
        # Unbuffered, so a pipe Claude Code closed leaves no text behind to fail again at exit.
        out = (
            io.TextIOWrapper(os.fdopen(int(fd_text), "wb", buffering=0, closefd=False), write_through=True)
            if fd_text.isdigit()
            else sys.stderr
        )
        conn = connect(db_path())
        try:
            woke = write_rewake(conn, load_config(), int(pid_text), time.time_ns(), out)
        finally:
            conn.close()
    except BrokenPipeError:
        # Claude Code stopped reading, as when its session ends or /restart replaced it: the message was rolled back
        # and still waits.
        return UNHEARD
    except Exception:
        log_error("rewake", traceback.format_exc())
        return 0
    return WAKE if woke else 0


def write_rewake(conn: sqlite3.Connection, config: Config, pid: int, event_ns: int, out: TextIO) -> bool:
    """Writes the inbox, as a hook's context would carry it, when a message waits that would be typed into this tab.

    Marked offered only once the text is written: a pipe the closing Claude Code no longer reads leaves it waiting.
    """
    start = process_start_ms(pid)
    if start is None:
        return False
    instance_id = make_instance_id(pid, start)
    at_ms = event_ns // 1_000_000
    with transaction(conn):
        row = conn.execute(
            "SELECT conversation_id, surface FROM instances WHERE instance_id = ? AND ended_at IS NULL", (instance_id,)
        ).fetchone()
        if row is None or row["conversation_id"] is None:
            return False
        conversation_id = str(row["conversation_id"])
        if (
            not is_delivery_owner(conn, conversation_id, instance_id)
            or is_conversation_locked(conn, config, conversation_id)
            or is_surface_locked(conn, config, row["surface"])
            or not _has_wakeful_mail(conn, conversation_id, instance_id)
        ):
            return False
        release_dead_typing_claims(conn, at_ms, conversation_id)
        # Parts a hook saved for later stay for the woken turn's first tool call, which takes them before mail.
        delivery = take_delivery(conn, conversation_id, at_ms, False)
        if delivery.context is None:
            return False
        record_activity(conn, instance_id, "active", event_ns)
        out.write(delivery.context + "\n")
        out.flush()
    try:
        # What did not fit goes to this turn's next tool call, or to the next rewake once it ends.
        write_hint(pid, next_hint_epoch(delivery.leftover))
    except OSError:
        log_error("rewake", traceback.format_exc())
    return True


def _has_wakeful_mail(conn: sqlite3.Connection, conversation_id: str, instance_id: str) -> bool:
    """Mail that would be typed into the tab; anything else waits for the session's own next turn, as it always did."""
    row = conn.execute(
        f"""
        SELECT 1 FROM message_recipients r JOIN messages m ON m.id = r.message_id
        JOIN instances i ON i.instance_id = ?
        WHERE r.conversation_id = ? AND r.acked_at IS NULL AND r.offered_at IS NULL
          AND r.done_at IS NULL AND r.declined_at IS NULL AND m.cancelled_at IS NULL
          AND {TYPED_MESSAGE_SQL}
        LIMIT 1
        """,
        (instance_id, conversation_id),
    ).fetchone()
    return row is not None
