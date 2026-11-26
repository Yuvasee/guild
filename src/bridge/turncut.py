"""A turn an API error cut short, as the Mac's sleep does: Claude Code then runs StopFailure instead of Stop, and the
tab waits idle with nothing to start it again. bridge resumes a lead's or a worker's tab once per cut."""

import sqlite3
import time
from pathlib import Path

from bridge.board import (
    SYSTEM_CONVERSATION,
    AddressError,
    Party,
    Recipient,
    add_user_item,
    resolve_handle,
    send_message,
)
from bridge.compaction import TOO_LONG_TEXT, TrailingFailure, is_context_full, read_trailing_failure
from bridge.config import Config
from bridge.db import transaction
from bridge.errlog import log_error
from bridge.feed import TO_ASSISTANT, TURN_CUT, convert_cut_to_resume_text, resolve_tab
from bridge.leads import find_lead_conversations, is_conversation_locked, is_surface_locked
from bridge.start_context import is_nested_tab

SLEEP_TEXT = "went to sleep"
"""Claude Code's "API Error: Your computer went to sleep mid-response."."""
SWEEP_EVERY_S = 120
IDLE_AFTER_MS = 2 * 60_000
"""StopFailure resumes a cut within seconds; a tab still idle on one this long is one no hook took."""
Inspected = dict[str, tuple[tuple[int, int], TrailingFailure | None]]


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


def sweep_turn_cuts(
    conn: sqlite3.Connection, config: Config, at_ms: int, inspected: Inspected
) -> list[tuple[int, Recipient]]:
    """Tells the assistant once of each lead's or worker's tab idle on a cut no StopFailure took or whose resume could
    not be typed; resumes nothing, so a hook that takes the cut later never reads the sweep's doing as a second cut.

    inspected: each transcript's (mtime_ns, size) when last read and the failure it ended on, so an unchanged one is
    not read again, while its cut is checked every sweep: a resume still waiting at one may have failed by the next.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT i.conversation_id, i.surface, c.transcript_path FROM instances i
        JOIN conversations c ON c.conversation_id = i.conversation_id
        WHERE i.ended_at IS NULL AND i.agent = 'claude' AND i.surface IS NOT NULL AND c.transcript_path IS NOT NULL
        """
    ).fetchall()
    leads = find_lead_conversations(conn)
    live: set[str] = set()
    found: list[tuple[str, str, str, int, str]] = []
    for row in rows:
        conversation_id, surface, path = str(row["conversation_id"]), str(row["surface"]), str(row["transcript_path"])
        if not _is_resumable(conn, config, conversation_id, surface, leads):
            continue
        live.add(path)
        version = _stat_version(path)
        if version is None or at_ms - version[0] // 1_000_000 < IDLE_AFTER_MS:
            continue
        seen = inspected.get(path)
        if seen is not None and seen[0] == version:
            failure = seen[1]
        else:
            failure = read_trailing_failure(Path(path))
            inspected[path] = (version, failure)
        if failure is not None and TOO_LONG_TEXT not in failure.text:
            cause = map_failure_to_cause(failure.error, failure.text)
            found.append((conversation_id, surface, path, failure.at_ms, cause))
    for path in set(inspected) - live:
        del inspected[path]
    sent: list[tuple[int, Recipient]] = []
    for conversation_id, surface, path, cut_at, cause in found:
        seen = inspected.get(path)
        if seen is None or _stat_version(path) != seen[0]:
            # A prompt or answer since the read: the next sweep reads it again.
            inspected.pop(path, None)
            continue
        with transaction(conn):
            if not conn.execute(
                "SELECT 1 FROM instances WHERE conversation_id = ? AND surface = ? AND ended_at IS NULL",
                (conversation_id, surface),
            ).fetchone():
                continue
            told = conn.execute(
                "SELECT cut_told_at FROM conversations WHERE conversation_id = ?", (conversation_id,)
            ).fetchone()
            if (told and (told["cut_told_at"] or 0) >= cut_at) or _find_cut_taken_at(conn, conversation_id) >= cut_at:
                continue
            if is_context_full(conn, conversation_id):
                continue
            conn.execute(
                "UPDATE conversations SET cut_told_at = ? WHERE conversation_id = ?", (cut_at, conversation_id)
            )
            failed = _find_failed_resume(conn, conversation_id, cut_at)
            sent += _tell_of_untaken_cut(conn, config, conversation_id, surface, cut_at, cause, failed, at_ms)
        # Each one is a StopFailure or a typer that did not do its job: errors.log keeps them for finding why.
        why = f"its resume #{failed} not typed" if failed else "taken by no hook"
        log_error("turn cut sweep", f"{conversation_id}: cut at {_clock(cut_at)} by {cause}, {why}")
    return sent


def _is_resumable(
    conn: sqlite3.Connection, config: Config, conversation_id: str, surface: str, leads: set[str]
) -> bool:
    if is_conversation_locked(conn, config, conversation_id) or is_surface_locked(conn, config, surface):
        return False
    return conversation_id in leads or is_nested_tab(conn, surface)


def _stat_version(path: str) -> tuple[int, int] | None:
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _find_cut_taken_at(conn: sqlite3.Connection, conversation_id: str) -> int:
    """When StopFailure last resumed this tab or told of its cut; 0 if never. A resume whose typing failed takes the
    cut only once the assistant was told it did not go in; one still waiting, for its typer or its batch, takes it."""
    row = conn.execute(
        """
        SELECT MAX(m.created_at, COALESCE(m.cancelled_at, 0)) AS at FROM messages m
        JOIN message_recipients r ON r.message_id = m.id
        WHERE r.conversation_id = ? AND m.post_type = ?
          AND (r.delivery_failed_at IS NULL OR r.offered_at IS NOT NULL OR r.acked_at IS NOT NULL
               OR m.cancelled_at IS NOT NULL OR m.resume_told_at IS NOT NULL)
        ORDER BY m.id DESC LIMIT 1
        """,
        (conversation_id, TURN_CUT),
    ).fetchone()
    return int(row["at"]) if row is not None else 0


def _find_failed_resume(conn: sqlite3.Connection, conversation_id: str, cut_at: int) -> int | None:
    """This cut's resume when the sweep tells of the cut, which happens only once its typing failed."""
    row = conn.execute(
        """
        SELECT MAX(m.id) AS id FROM messages m JOIN message_recipients r ON r.message_id = m.id
        WHERE r.conversation_id = ? AND m.post_type = ? AND m.created_at >= ?
        """,
        (conversation_id, TURN_CUT, cut_at),
    ).fetchone()
    return int(row["id"]) if row["id"] is not None else None


def _tell_of_untaken_cut(
    conn: sqlite3.Connection,
    config: Config,
    conversation_id: str,
    surface: str,
    cut_at: int,
    cause: str,
    failed_resume: int | None,
    at_ms: int,
) -> list[tuple[int, Recipient]]:
    tab = conn.execute("SELECT handle, agterm_session, tab_name FROM surfaces WHERE surface = ?", (surface,)).fetchone()
    handle = str(tab["handle"]) if tab and tab["handle"] else None
    name = (f"{handle}'s tab" if handle else "a tab") + (f" ({tab['tab_name']})" if tab and tab["tab_name"] else "")
    bridge = Party(SYSTEM_CONVERSATION, "bridge")
    assistant = _find_assistant(conn, config, conversation_id)
    if assistant is None:
        text = f"bridge: {name} idle since {cause} at {_clock(cut_at)}; resume it"
        add_user_item(conn, bridge, "action", text[:140], at_ms)
        return []
    look = (
        f" Look: agtermctl session text --target {tab['agterm_session']} --pane-id {surface}."
        if tab and tab["agterm_session"]
        else ""
    )
    if failed_resume is not None:
        why = f"and bridge's resume #{failed_resume} could not be typed there"
        resume = " Resume it by hand once the tab can take text."
    else:
        why = "and no StopFailure hook took the cut, so bridge did not resume it"
        resume = f' Resume it: bridge send {handle} "continue where you left off".' if handle else ""
    body = f"{name} has been idle since {cause} cut its turn at {_clock(cut_at)}, {why}.{look}{resume}"
    # As asks.send_from_bridge sends it; importing that module would load page code into every hook.
    result = send_message(conn, bridge, [assistant], "info", body, at_ms)
    conn.execute("UPDATE messages SET post_type = ? WHERE id = ?", (TO_ASSISTANT, result.message_id))
    if failed_resume is not None:
        # Only a message the assistant gets: a page item alone leaves the undelivered alert to tell it once back.
        conn.execute("UPDATE messages SET resume_told_at = ? WHERE id = ?", (at_ms, failed_resume))
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
