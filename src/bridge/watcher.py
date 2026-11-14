import json
import sqlite3
from dataclasses import dataclass
from typing import cast

from bridge.board import SYSTEM_CONVERSATION, AddressError, Party, Recipient, resolve_handle, send_message
from bridge.compaction import map_screen_to_dialog
from bridge.config import Config
from bridge.feed import BLOCKED_TAB, resolve_tab
from bridge.leads import find_lead_conversations, is_conversation_locked
from bridge.registry import record_tab_parent
from bridge.tabinput import run_agterm
from bridge.workers import find_live_lead

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
            return parent
        stray = conn.execute(
            "SELECT parent_session FROM stray_workers WHERE agterm_session = ?", (session_id,)
        ).fetchone()
        if stray:
            return str(stray["parent_session"])
        conn.execute("DELETE FROM tab_parents WHERE agterm_session = ?", (session_id,))
        return None
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


def read_screen(config: Config, session_id: str) -> str:
    return run_agterm(config, ["session", "text", "--target", session_id, "--lines", "40"]) or ""


def read_screen_tail(config: Config, session_id: str) -> str:
    return map_screen_to_tail(read_screen(config, session_id))


def map_screen_to_tail(screen: str) -> str:
    return "\n".join(line.strip()[:160] for line in [ln for ln in screen.splitlines() if ln.strip()][-SCREEN_LINES:])


def notify_blocked_tabs(
    conn: sqlite3.Connection, config: Config, blocked: list[BlockedTab], screens: dict[str, str], at_ms: int
) -> list[tuple[int, Recipient]]:
    """Counts from when this watcher first saw the dialog, since agterm's timestamp moves on every status write.

    agterm's status alone is not a dialog: it stays "blocked" after Claude Code times a dialog out, until a tool ends.
    """
    blocked = [tab for tab in blocked if map_screen_to_dialog(screens.get(tab.session_id, ""))]
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
            f"{map_screen_to_tail(screens[tab.session_id])}\n"
            f"Look: agtermctl session text --target {tab.session_id}. Answer: agtermctl session type --target "
            f'{tab.session_id} "<keys>". Settle it if it is within the brief; otherwise ask {config.assistant_handle}.'
        )
        result = send_message(conn, Party(SYSTEM_CONVERSATION, "bridge"), [owner], "info", body[:2000], at_ms)
        conn.execute("UPDATE messages SET post_type = ? WHERE id = ?", (BLOCKED_TAB, result.message_id))
        conn.execute("UPDATE blocked_notices SET notified_at = ? WHERE agterm_session = ?", (at_ms, tab.session_id))
        sent.append((result.message_id, owner))
    return sent


def find_tab_recipient(conn: sqlite3.Connection, config: Config, agterm_session: str) -> Recipient | None:
    row = conn.execute(
        """
        SELECT i.conversation_id, s.handle FROM surfaces s
        JOIN instances i ON i.surface = s.surface AND i.ended_at IS NULL AND i.conversation_id IS NOT NULL
        WHERE s.agterm_session = ? AND s.handle IS NOT NULL ORDER BY i.proc_start_ms DESC LIMIT 1
        """,
        (agterm_session,),
    ).fetchone()
    if row and not is_conversation_locked(conn, config, str(row["conversation_id"])):
        return resolve_tab(conn, str(row["conversation_id"]), str(row["handle"]))
    return None


def _find_owner(conn: sqlite3.Connection, config: Config, tab: BlockedTab) -> Recipient | None:
    if tab.parent and (owner := find_tab_recipient(conn, config, tab.parent)):
        return owner
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


@dataclass(frozen=True)
class StrayWorker:
    session_id: str
    name: str
    closed_id: int
    conversation_id: str
    worker_name: str
    lead_name: str | None
    lead_session: str | None


def map_tree_to_stray_workers(conn: sqlite3.Connection, config: Config, tree: dict[str, object]) -> list[StrayWorker]:
    """Tabs with no agterm parent whose live conversation is a closed worker's: resumed by hand, outside any tree."""
    strays: list[StrayWorker] = []
    workspaces = tree.get("workspaces")
    for workspace in cast(list[dict[str, object]], workspaces if isinstance(workspaces, list) else []):
        tabs = workspace.get("sessions")
        for tab in cast(list[dict[str, object]], tabs if isinstance(tabs, list) else []):
            name, session_id = str(tab.get("name")), str(tab.get("id"))
            if tab.get("parent") or config.hides(name, str(workspace.get("name"))):
                continue
            row = conn.execute(
                """
                SELECT cw.id, cw.conversation_id, cw.tab_name, cw.parent_lead, cw.parent_session FROM surfaces s
                JOIN instances i ON i.surface = s.surface AND i.ended_at IS NULL
                JOIN closed_workers cw ON cw.conversation_id = i.conversation_id
                WHERE s.agterm_session = ? ORDER BY cw.closed_at DESC, cw.id DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if row:
                strays.append(
                    StrayWorker(session_id, name, int(row["id"]), str(row["conversation_id"]), str(row["tab_name"]),
                                row["parent_lead"], row["parent_session"])
                )  # fmt: skip
    return strays


def notify_stray_workers(
    conn: sqlite3.Connection, config: Config, tree: dict[str, object], at_ms: int
) -> list[tuple[int, Recipient]]:
    """The stray tab is linked to its lead at once; the lead hears of it once per tab and conversation."""
    strays = map_tree_to_stray_workers(conn, config, tree)
    current = {stray.session_id: stray.conversation_id for stray in strays}
    for row in conn.execute("SELECT agterm_session, conversation_id FROM stray_workers").fetchall():
        session = str(row["agterm_session"])
        # Another conversation in the tab may belong to another lead: the old link must not stand for it.
        if current.get(session) != row["conversation_id"]:
            conn.execute("DELETE FROM stray_workers WHERE agterm_session = ?", (session,))
            if (real := map_tree_to_parent(tree, session)) is not None:
                record_tab_parent(conn, session, real)
            else:
                conn.execute("DELETE FROM tab_parents WHERE agterm_session = ?", (session,))
    sent: list[tuple[int, Recipient]] = []
    for stray in strays:
        found = _find_stray_lead(conn, config, stray)
        if found is None:
            continue
        lead, lead_tab = found
        conn.execute(
            """
            INSERT INTO stray_workers (agterm_session, conversation_id, parent_session) VALUES (?, ?, ?)
            ON CONFLICT (agterm_session) DO UPDATE SET parent_session = excluded.parent_session
            """,
            (stray.session_id, stray.conversation_id, lead_tab),
        )
        record_tab_parent(conn, stray.session_id, lead_tab)
        row = conn.execute(
            "SELECT notified_at FROM stray_workers WHERE agterm_session = ?", (stray.session_id,)
        ).fetchone()
        if row["notified_at"] is not None:
            continue
        body = (
            f'Tab "{stray.name}" ({stray.session_id}) runs your closed worker {stray.worker_name} outside your tree; '
            f"reopen it with guild worker reopen {stray.closed_id} when it is idle."
        )
        result = send_message(conn, Party(SYSTEM_CONVERSATION, "bridge"), [lead], "info", body, at_ms)
        conn.execute("UPDATE stray_workers SET notified_at = ? WHERE agterm_session = ?", (at_ms, stray.session_id))
        sent.append((result.message_id, lead))
    return sent


def _find_stray_lead(conn: sqlite3.Connection, config: Config, stray: StrayWorker) -> tuple[Recipient, str] | None:
    """The tab that closed it; once that is gone, the lead that closed it, by the name no other tab can take."""
    if stray.lead_session and stray.lead_session != stray.session_id:
        lead = find_tab_recipient(conn, config, stray.lead_session)
        if lead is not None:
            return lead, stray.lead_session
    found = find_live_lead(conn, stray.lead_name)
    if found is None or found[1] in (None, stray.session_id):
        return None
    lead_tab = str(found[1])
    lead = find_tab_recipient(conn, config, lead_tab)
    return (lead, lead_tab) if lead else None
