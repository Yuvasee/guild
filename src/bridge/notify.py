import sqlite3
import subprocess
from dataclasses import dataclass

from bridge.config import Config
from bridge.guildview import STALE_QUESTION_MS
from bridge.leads import is_conversation_locked
from bridge.reviews import find_lead_of

NOTIFY_TIMEOUT_S = 15
_SHOW_ARGUMENT = ("-e", "on run argv", "-e", "display notification (item 1 of argv)", "-e", "end run", "--")


@dataclass(frozen=True)
class UrgentNotice:
    item_id: int
    text: str


def take_next_urgent_notice(
    conn: sqlite3.Connection, config: Config, now: int, skipping: frozenset[int] = frozenset()
) -> UrgentNotice | None:
    """One at a time and marked before it fires: a restart repeats none, and loses only the one being shown. A locked
    tab's post stays unmarked and silent."""
    for row in conn.execute(
        """
        SELECT id, from_conversation FROM user_items
        WHERE urgent = 1 AND notified_at IS NULL AND resolved_at IS NULL AND source = 'agent' AND created_at > ?
        ORDER BY created_at
        """,
        (now - STALE_QUESTION_MS,),
    ).fetchall():
        conversation = str(row["from_conversation"])
        if int(row["id"]) in skipping or is_conversation_locked(conn, config, conversation):
            continue
        conn.execute("UPDATE user_items SET notified_at = ? WHERE id = ?", (now, row["id"]))
        return UrgentNotice(int(row["id"]), _map_conversation_to_notice_text(conn, conversation))
    return None


def release_urgent_notice(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute("UPDATE user_items SET notified_at = NULL WHERE id = ?", (item_id,))


def show_notification(config: Config, text: str) -> bool:
    """False only when it surely did not show: after a timeout it may have, and once beats twice."""
    command = [config.notify_command, *_SHOW_ARGUMENT, text]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=NOTIFY_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        print("urgent notification timed out", flush=True)
        return True
    except OSError as exc:
        print(f"urgent notification failed: {exc}", flush=True)
        return False
    if done.returncode != 0:
        print(f"urgent notification failed: {done.stderr.strip()}", flush=True)
    return done.returncode == 0


def _map_conversation_to_notice_text(conn: sqlite3.Connection, conversation: str) -> str:
    """Only a lead's name: their screen may be shared, and a worker's tab name can say what the work is."""
    row = conn.execute(
        """
        SELECT i.surface FROM conversations c JOIN instances i ON i.instance_id = c.instance_id
        WHERE c.conversation_id = ?
        """,
        (conversation,),
    ).fetchone()
    lead = find_lead_of(conn, row["surface"])[0] if row else None
    return f"Urgent question from {lead}" if lead else "Urgent question"
