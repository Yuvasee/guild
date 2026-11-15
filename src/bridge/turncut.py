"""A turn an API error cut short, as the Mac's sleep does: Claude Code then runs StopFailure instead of Stop, and the
tab waits idle with nothing to start it again. bridge resumes a lead's or a worker's tab once per cut."""

import sqlite3
import time

from bridge.board import (
    SYSTEM_CONVERSATION,
    AddressError,
    Party,
    Recipient,
    add_user_item,
    resolve_handle,
    send_message,
)
from bridge.compaction import TOO_LONG_TEXT, is_context_full
from bridge.config import Config
from bridge.feed import TO_ASSISTANT, TURN_CUT, convert_cut_to_resume_text, resolve_tab
from bridge.leads import find_lead_conversations, is_conversation_locked, is_surface_locked
from bridge.start_context import is_nested_tab

SLEEP_TEXT = "went to sleep"
"""Claude Code's "API Error: Your computer went to sleep mid-response."."""


def map_failure_to_cause(error: str | None, text: str | None) -> str:
    if text and SLEEP_TEXT in text:
        return "Mac sleep"
    return f"an API error ({error or 'unknown'})"


def take_turn_cut(
    conn: sqlite3.Connection,
    config: Config,
    conversation_id: str,
    surface: str | None,
    error: str | None,
    text: str | None,
    answered_at: int | None,
    at_ms: int,
) -> list[tuple[int, Recipient]]:
    """The messages to type: the tab's resume, or, when the turn its resume started failed too, the assistant's alert.

    answered_at: the model's newest real answer, from the transcript; one since the last resume makes this a new cut.
    Only a lead's or a worker's tab, never a locked one; a context over its window is compaction's to cure.
    """
    if (
        surface is None
        or is_conversation_locked(conn, config, conversation_id)
        or is_surface_locked(conn, config, surface)
    ):
        return []
    if conversation_id not in find_lead_conversations(conn) and not is_nested_tab(conn, surface):
        return []
    if (text and TOO_LONG_TEXT in text) or is_context_full(conn, conversation_id):
        return []
    tab = conn.execute("SELECT handle, agterm_session, tab_name FROM surfaces WHERE surface = ?", (surface,)).fetchone()
    if tab is None or not tab["handle"] or not tab["agterm_session"]:
        return []
    cause = map_failure_to_cause(error, text)
    last = conn.execute(
        """
        SELECT m.id, m.created_at, m.cancelled_at FROM messages m JOIN message_recipients r ON r.message_id = m.id
        WHERE r.conversation_id = ? AND m.post_type = ? ORDER BY m.id DESC LIMIT 1
        """,
        (conversation_id, TURN_CUT),
    ).fetchone()
    bridge = Party(SYSTEM_CONVERSATION, "bridge")
    compacted = conn.execute(
        "SELECT compacted_at FROM conversations WHERE conversation_id = ?", (conversation_id,)
    ).fetchone()
    # The transcript's answers stop at a compaction boundary; a compaction since the resume is progress too.
    progress = max(answered_at or 0, int(compacted["compacted_at"] or 0) if compacted else 0)
    if last is None or progress > int(last["created_at"]):
        recipient = resolve_tab(conn, conversation_id, str(tab["handle"]))
        result = send_message(conn, bridge, [recipient], "info", convert_cut_to_resume_text(cause, at_ms), at_ms)
        conn.execute("UPDATE messages SET post_type = ? WHERE id = ?", (TURN_CUT, result.message_id))
        return [(result.message_id, recipient)]
    if last["cancelled_at"] is not None:
        return []
    # Told once: cancelled, the resume is never typed after the alert, whatever holds it now.
    conn.execute("UPDATE messages SET cancelled_at = ? WHERE id = ?", (at_ms, last["id"]))
    name = f"{tab['handle']}'s tab" + (f" ({tab['tab_name']})" if tab["tab_name"] else "")
    body = (
        f"{name} was cut again by {cause} at {_clock(at_ms)}, after bridge resumed it once (#{last['id']} at "
        f"{_clock(int(last['created_at']))}); bridge types no second resume. Look: agtermctl session text --target "
        f"{tab['agterm_session']} --pane-id {surface}. Resume it by hand once the cause is gone."
    )
    assistant = _find_assistant(conn, config, conversation_id)
    if assistant is None:
        text = f"bridge: {name} was cut again by {cause}; resume it by hand"
        add_user_item(conn, bridge, "action", text[:140], at_ms)
        return []
    result = send_message(conn, bridge, [assistant], "info", body, at_ms)
    conn.execute("UPDATE messages SET post_type = ? WHERE id = ?", (TO_ASSISTANT, result.message_id))
    return [(result.message_id, assistant)]


def _find_assistant(conn: sqlite3.Connection, config: Config, conversation_id: str) -> Recipient | None:
    try:
        assistant = resolve_handle(conn, config.assistant_handle)
    except AddressError:
        return None
    if assistant.conversation_id == conversation_id or is_conversation_locked(conn, config, assistant.conversation_id):
        return None
    return assistant


def _clock(at_ms: int) -> str:
    return time.strftime("%H:%M", time.localtime(at_ms / 1000))
