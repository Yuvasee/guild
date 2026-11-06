import json
import sqlite3
from dataclasses import dataclass
from typing import cast

from bridge.board import SYSTEM_CONVERSATION, AddressError, Party, Recipient, resolve_handle, send_message
from bridge.config import Config
from bridge.feed import BLOCKED_TAB, resolve_tab
from bridge.leads import find_lead_conversations, is_conversation_locked
from bridge.registry import record_tab_parent
from bridge.tabinput import run_agterm

BLOCKED_AFTER_S = 120
RENOTIFY_EVERY_MS = 600_000
SCREEN_LINES = 6


@dataclass(frozen=True)
class BlockedTab:
    session_id: str
    name: str
    parent: str | None


def map_tree_to_blocked_tabs(tree: dict[str, object], config: Config) -> list[BlockedTab]:
    blocked: list[BlockedTab] = []
    workspaces = tree.get("workspaces")
    for workspace in cast(list[dict[str, object]], workspaces if isinstance(workspaces, list) else []):
        tabs = workspace.get("sessions")
        for tab in cast(list[dict[str, object]], tabs if isinstance(tabs, list) else []):
            if tab.get("status") != "blocked" or config.hides(str(tab.get("name")), str(workspace.get("name"))):
                continue
            parent = tab.get("parent")
            blocked.append(BlockedTab(str(tab.get("id")), str(tab.get("name")), str(parent) if parent else None))
    return blocked


def read_tree(config: Config) -> dict[str, object] | None:
    output = run_agterm(config, ["tree", "--json"])
    if output is None:
        return None
    try:
        return cast(dict[str, object], json.loads(output)["result"]["tree"])
    except (ValueError, KeyError, TypeError):
        return None


def map_tree_to_parent(tree: dict[str, object] | None, session_id: str) -> str | None:
    workspaces = tree.get("workspaces") if tree else None
    for workspace in cast(list[dict[str, object]], workspaces if isinstance(workspaces, list) else []):
        sessions = workspace.get("sessions")
        for tab in cast(list[dict[str, object]], sessions if isinstance(sessions, list) else []):
            if tab.get("id") == session_id and isinstance(tab.get("parent"), str):
                return str(tab["parent"])
    return None


def find_tab_parent(conn: sqlite3.Connection, tree: dict[str, object] | None, session_id: str) -> str | None:
    """The live tree wins and corrects the stored link, which stands in only when agterm did not answer."""
    if tree is not None:
        parent = map_tree_to_parent(tree, session_id)
        if parent:
            record_tab_parent(conn, session_id, parent)
        else:
            conn.execute("DELETE FROM tab_parents WHERE agterm_session = ?", (session_id,))
        return parent
    row = conn.execute("SELECT parent_session FROM tab_parents WHERE agterm_session = ?", (session_id,)).fetchone()
    return str(row["parent_session"]) if row else None


def find_conversation_tab(conn: sqlite3.Connection, conversation_id: str) -> str | None:
    """By the registered surface: a nested agent inherits its tab's AGTERM_SESSION_ID but is not that tab's session."""
    row = conn.execute(
        """
        SELECT s.agterm_session FROM instances i JOIN surfaces s ON s.surface = i.surface
        WHERE i.conversation_id = ? AND i.ended_at IS NULL ORDER BY i.proc_start_ms DESC LIMIT 1
        """,
        (conversation_id,),
    ).fetchone()
    return str(row["agterm_session"]) if row and row["agterm_session"] else None


def find_own_workers(
    conn: sqlite3.Connection, recipients: list[Recipient], lead_session: str, tree: dict[str, object] | None
) -> list[Recipient]:
    workers: list[Recipient] = []
    for recipient in recipients:
        tab = find_conversation_tab(conn, recipient.conversation_id)
        if tab and find_tab_parent(conn, tree, tab) == lead_session:
            workers.append(recipient)
    return workers


def find_assistant_recipients(
    conn: sqlite3.Connection, config: Config, sender: Party, recipients: list[Recipient]
) -> list[Recipient]:
    """Without an agterm tab nothing can be typed, so such an assistant keeps the usual wake hint."""
    try:
        assistant = resolve_handle(conn, config.assistant_handle).conversation_id
    except AddressError:
        return []
    if (
        assistant == sender.conversation_id
        or is_conversation_locked(conn, config, assistant)
        or not find_conversation_tab(conn, assistant)
    ):
        return []
    return [r for r in recipients if r.conversation_id == assistant]


def find_lead_recipients(
    conn: sqlite3.Connection, config: Config, sender: Party, recipients: list[Recipient]
) -> list[Recipient]:
    leads = find_lead_conversations(conn)
    return [
        r
        for r in recipients
        if r.conversation_id in leads
        and r.conversation_id != sender.conversation_id
        and not is_conversation_locked(conn, config, r.conversation_id)
    ]


def read_screen_tail(config: Config, session_id: str) -> str:
    output = run_agterm(config, ["session", "text", "--target", session_id, "--lines", "30"]) or ""
    return "\n".join(line.strip()[:160] for line in [ln for ln in output.splitlines() if ln.strip()][-SCREEN_LINES:])


def notify_blocked_tabs(
    conn: sqlite3.Connection, config: Config, blocked: list[BlockedTab], screens: dict[str, str], at_ms: int
) -> list[tuple[int, Recipient]]:
    """Counts from when this watcher first saw the block, since agterm's timestamp moves on every status write."""
    current = {tab.session_id for tab in blocked}
    for row in conn.execute("SELECT agterm_session FROM blocked_notices").fetchall():
        if row[0] not in current:
            conn.execute("DELETE FROM blocked_notices WHERE agterm_session = ?", (row[0],))
    sent: list[tuple[int, Recipient]] = []
    for tab in blocked:
        conn.execute(
            "INSERT OR IGNORE INTO blocked_notices (agterm_session, first_seen_at) VALUES (?, ?)",
            (tab.session_id, at_ms),
        )
        row = conn.execute(
            "SELECT first_seen_at, notified_at FROM blocked_notices WHERE agterm_session = ?", (tab.session_id,)
        ).fetchone()
        first_seen = int(row["first_seen_at"])
        if at_ms - first_seen < BLOCKED_AFTER_S * 1000:
            continue
        if row["notified_at"] is not None and at_ms - int(row["notified_at"]) < RENOTIFY_EVERY_MS:
            continue
        owner = _find_owner(conn, config, tab)
        if owner is None:
            continue
        minutes = (at_ms - first_seen) // 60_000
        body = (
            f'Tab "{tab.name}" ({tab.session_id}) has been blocked for {minutes} min. Screen:\n'
            f"{screens.get(tab.session_id, '')}\n"
            f"Look: agtermctl session text --target {tab.session_id}. Answer: agtermctl session type --target "
            f'{tab.session_id} "<keys>". Settle it if it is within the brief; otherwise ask {config.assistant_handle}.'
        )
        result = send_message(conn, Party(SYSTEM_CONVERSATION, "bridge"), [owner], "info", body[:2000], at_ms)
        conn.execute("UPDATE messages SET post_type = ? WHERE id = ?", (BLOCKED_TAB, result.message_id))
        conn.execute("UPDATE blocked_notices SET notified_at = ? WHERE agterm_session = ?", (at_ms, tab.session_id))
        sent.append((result.message_id, owner))
    return sent


def _find_owner(conn: sqlite3.Connection, config: Config, tab: BlockedTab) -> Recipient | None:
    if tab.parent:
        row = conn.execute(
            """
            SELECT i.conversation_id, s.handle FROM surfaces s
            JOIN instances i ON i.surface = s.surface AND i.ended_at IS NULL AND i.conversation_id IS NOT NULL
            WHERE s.agterm_session = ? AND s.handle IS NOT NULL ORDER BY i.proc_start_ms DESC LIMIT 1
            """,
            (tab.parent,),
        ).fetchone()
        if row and not is_conversation_locked(conn, config, str(row["conversation_id"])):
            return resolve_tab(conn, str(row["conversation_id"]), str(row["handle"]))
    try:
        assistant = resolve_handle(conn, config.assistant_handle)
    except AddressError:
        return None
    if is_conversation_locked(conn, config, assistant.conversation_id):
        return None
    mine = conn.execute(
        "SELECT 1 FROM surfaces s JOIN instances i ON i.surface = s.surface AND i.ended_at IS NULL "
        "WHERE s.agterm_session = ? AND i.conversation_id = ?",
        (tab.session_id, assistant.conversation_id),
    ).fetchone()
    return None if mine else assistant
