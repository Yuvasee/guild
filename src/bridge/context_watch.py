"""Each tab's context as its status line shows it: the window it really has, and tabs that can take no turn."""

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from typing import cast

from bridge.asks import send_from_bridge
from bridge.board import (
    SYSTEM_CONVERSATION,
    AddressError,
    Party,
    Recipient,
    add_user_item,
    resolve_handle,
    send_message,
)
from bridge.compaction import (
    COMPACT_AT_WINDOW_SHARE,
    COMPACT_WAIT_S,
    CONTEXT_FULL_PCT,
    DEAD_COMPACTABLE,
    DEAD_FAILING,
    DEAD_LIMIT_SCREEN,
    DEAD_REFUSED,
    DEAD_TOO_LONG,
    PHASE_COMPACTING,
    PHASE_REQUESTED,
    RETRY_AFTER_MS,
    TOLD_FORCED,
    clear_death,
    convert_tokens_to_request,
    estimate_window,
    map_screen_to_idle,
    map_screen_to_waiting,
    record_death,
    reopen_unread_messages,
    take_told_once,
)
from bridge.config import Config
from bridge.feed import BLOCKED_TAB
from bridge.host_workers import HOST_KIND_PREFIX
from bridge.leads import is_conversation_locked
from bridge.registry import format_age
from bridge.tabinput import run_agterm
from bridge.watcher import find_tab_recipient
from bridge.worker_api import convert_bridge_commands_to_gbridge

CONTEXT_EVERY_S = 90
SCREEN_LINES = 20
WINDOW_MIN_PCT = 10
"""Below this the status line's whole percent is too coarse to tell the window from."""
REQUEST_PATIENCE_MS = 5 * 60_000
COMPACT_NOW_PCT = 85
"""Past this share of its window a tab that waits for input is compacted, whatever the cycle's marks say."""
STALE_DRIVE_MS = 2 * COMPACT_WAIT_S * 1000
"""A drive ends within its wait; a phase still "compacting" this long after it started outlived its drive."""
AFTER_COMPACTION_MS = 2 * CONTEXT_EVERY_S * 1000
"""The status line can show the old size until the first request after compaction."""
RULE_CHAR = "\u2500"
_PCT_RE = re.compile(r"^\s+(\S.*?) \| (\d+)% ctx\b")
LIMIT_MARKER = "Context limit reached · /compact or /clear to continue"
PERMISSION_MARKER = "Do you want to proceed?"
PERMISSION_PATIENCE_MS = 5 * 60_000
"""A tab waiting this long on a permission dialog is named to the assistant, who can answer it for a read-only
command, instead of waiting out the compaction's hour."""
PERMISSION_LINES = 12
PERMISSION_QUOTE_CHARS = 700
LOW_MARKER = "Context low ("
_DEAD_WORDS = {
    DEAD_TOO_LONG: 'its context is over its window (every turn ends in "Prompt is too long")',
    DEAD_REFUSED: "the API refused its last turns as invalid requests",
    DEAD_FAILING: "its last turns all failed with API errors",
    DEAD_LIMIT_SCREEN: 'it shows "Context limit reached" and auto-compact is off',
}


@dataclass(frozen=True)
class ContextTab:
    conversation_id: str
    instance_id: str
    session: str
    pane: str
    name: str
    parent: str | None


@dataclass(frozen=True)
class ContextActions:
    drives: list[tuple[str, str, bool]]
    """(instance, conversation, compact told what to keep) for each /compact bridge types."""
    messages: list[tuple[int, Recipient]]


def map_screen_to_context_pct(screen: str) -> int | None:
    found = map_screen_to_model_pct(screen)
    return found[1] if found else None


def map_screen_to_model_pct(screen: str) -> tuple[str, int] | None:
    """The model and percent in the user's status line under the input box, "<model> | N% ctx | <cwd>"; none while it
    is blank, as before the first request. Only the footer: the transcript above may quote such a line."""
    found = [m for line in _footer(screen) if (m := _PCT_RE.match(line))]
    return (found[-1].group(1).strip(), int(found[-1].group(2))) if found else None


def map_screen_to_limit_reached(screen: str) -> bool:
    """Claude Code refuses every prompt until /compact or /clear, and waits for input, its box often showing a grey
    suggestion. Its footer must say so too: the notice alone may be quoted in a healthy tab's transcript."""
    tail = [line for line in screen.splitlines() if line.strip()][-SCREEN_LINES:]
    if not any(LIMIT_MARKER in line for line in tail) or not map_screen_to_waiting(screen):
        return False
    pct = map_screen_to_context_pct(screen)
    return (pct is not None and pct >= CONTEXT_FULL_PCT) or any(LOW_MARKER in line for line in _footer(screen))


def map_screen_to_permission_ask(screen: str) -> str | None:
    """The lines above Claude Code's 'Do you want to proceed?': the command or edit it asks about, without the box."""
    lines = screen.splitlines()
    at = next((i for i, line in enumerate(lines) if PERMISSION_MARKER in line), None)
    if at is None:
        return None
    shown = (line.strip(" \u2502\u256d\u256e\u2570\u256f\u2500") for line in lines[max(0, at - PERMISSION_LINES) : at])
    return "\n".join(line for line in shown if line)[-PERMISSION_QUOTE_CHARS:]


def _watch_permission_ask(
    conn: sqlite3.Connection, config: Config, tab: ContextTab, screen: str, at_ms: int, actions: ContextActions
) -> None:
    """Seen once, told once per dialog: its first sight starts the wait, a dialog gone clears both marks."""
    prefix = f"permission:{tab.conversation_id}:"
    ask = map_screen_to_permission_ask(screen)
    if ask is None:
        conn.execute("DELETE FROM signal_posts WHERE substr(key, 1, ?) = ?", (len(prefix), prefix))
        return
    digest = hashlib.sha256(ask.encode()).hexdigest()[:12]
    seen_key, told_key = f"{prefix}{digest}:seen", f"{prefix}{digest}:told"
    seen = conn.execute("SELECT posted_at FROM signal_posts WHERE key = ?", (seen_key,)).fetchone()
    if seen is None:
        conn.execute("INSERT INTO signal_posts (key, message_id, posted_at) VALUES (?, 0, ?)", (seen_key, at_ms))
        return
    waited = at_ms - int(seen[0])
    if (
        waited < PERMISSION_PATIENCE_MS
        or conn.execute("SELECT 1 FROM signal_posts WHERE key = ?", (told_key,)).fetchone()
    ):
        return
    text = (
        f"{tab.name} has waited {format_age(waited)} on a permission dialog:\n{ask}\n"
        "Answer it in the tab if the command is harmless (a read-only one the parser flagged), else ask its lead."
    )
    sent = send_from_bridge(conn, config, config.assistant_handle, text, at_ms)
    if sent:
        conn.execute(
            "INSERT INTO signal_posts (key, message_id, posted_at) VALUES (?, ?, ?)", (told_key, sent[0][0], at_ms)
        )
        actions.messages.extend(sent)


def _footer(screen: str) -> list[str]:
    """The lines under the input box's closing rule; none without one, since anything above it may be quoted."""
    lines = [line for line in screen.splitlines() if line.strip()]
    rules = [i for i, line in enumerate(lines) if line.startswith(RULE_CHAR)]
    return lines[rules[-1] + 1 :] if rules else []


def list_context_tabs(conn: sqlite3.Connection, config: Config, tree: dict[str, object]) -> list[ContextTab]:
    """Live Claude tabs agterm shows that bridge has a conversation for; hidden and locked ones are left out."""
    shown: dict[str, tuple[str, str | None]] = {}
    workspaces = tree.get("workspaces")
    for workspace in cast(list[dict[str, object]], workspaces if isinstance(workspaces, list) else []):
        tabs = workspace.get("sessions")
        for tab in cast(list[dict[str, object]], tabs if isinstance(tabs, list) else []):
            name = str(tab.get("name"))
            if config.hides(name, str(workspace.get("name"))):
                continue
            parent = tab.get("parent")
            shown[str(tab.get("id"))] = (name, str(parent) if parent else None)
    found: dict[str, ContextTab] = {}
    for row in conn.execute(
        """
        SELECT i.instance_id, i.conversation_id, s.agterm_session, s.surface FROM instances i
        JOIN surfaces s ON s.surface = i.surface
        JOIN conversations c ON c.conversation_id = i.conversation_id
        WHERE i.ended_at IS NULL AND i.agent = 'claude' AND s.agterm_session IS NOT NULL
        ORDER BY i.proc_start_ms
        """
    ):
        session = str(row["agterm_session"])
        conversation = str(row["conversation_id"])
        if session not in shown or is_conversation_locked(conn, config, conversation):
            continue
        name, parent = shown[session]
        found[session] = ContextTab(conversation, str(row["instance_id"]), session, str(row["surface"]), name, parent)
    return list(found.values())


def read_pane_screen(config: Config, tab: ContextTab) -> str | None:
    return run_agterm(config, ["session", "text", "--target", tab.session, "--pane-id", tab.pane, "--lines", "40"])


def record_context_screens(
    conn: sqlite3.Connection,
    config: Config,
    tabs: list[ContextTab],
    screens: dict[str, tuple[str | None, int]],
    at_ms: int,
) -> ContextActions:
    """screens: each tab's screen by conversation, with when its read began."""
    actions = ContextActions([], [])
    for tab in tabs:
        screen, read_at = screens.get(tab.conversation_id, (None, at_ms))
        if screen is not None:
            _record_screen(conn, config, tab, screen, read_at, at_ms, actions)
    return actions


def _record_screen(
    conn: sqlite3.Connection,
    config: Config,
    tab: ContextTab,
    screen: str,
    read_at: int,
    at_ms: int,
    actions: ContextActions,
) -> None:
    row = conn.execute("SELECT * FROM conversations WHERE conversation_id = ?", (tab.conversation_id,)).fetchone()
    held = conn.execute(
        "SELECT 1 FROM instances WHERE instance_id = ? AND conversation_id = ? AND ended_at IS NULL",
        (tab.instance_id, tab.conversation_id),
    ).fetchone()
    if row is None or held is None:
        # The tab moved on (/clear, a restart) since it was listed: the screen is another conversation's.
        return
    _watch_permission_ask(conn, config, tab, screen, at_ms, actions)
    compacted_at = row["compacted_at"]
    if compacted_at is not None and int(compacted_at) >= read_at - AFTER_COMPACTION_MS:
        # Read before, or too soon after, a compaction: it may show the full context that compaction just emptied.
        return
    footer = map_screen_to_model_pct(screen)
    pct: int | None = None
    if footer is not None:
        model, pct = footer
        tokens = row["context_tokens"]
        # Only at rest: then the stored size is the one the Stop hook read from the very request the status line
        # shows; mid-turn the hooks lag and the window would come out too small. Another model starts over.
        paired = pct >= WINDOW_MIN_PCT and map_screen_to_idle(screen)
        estimate = estimate_window(int(tokens), pct) if tokens is not None and paired else None
        known = row["context_window"] if row["context_model"] == model else None
        window = estimate if estimate is not None else known
        conn.execute(
            "UPDATE conversations SET context_pct = ?, context_pct_at = ?, context_model = ?, context_window = ?"
            " WHERE conversation_id = ?",
            (pct, at_ms, model, window, tab.conversation_id),
        )
        if row["context_dead_why"] == DEAD_LIMIT_SCREEN and WINDOW_MIN_PCT <= pct < CONTEXT_FULL_PCT:
            # A fresh, real size under the limit: the tab took a turn again.
            reopen_unread_messages(conn, tab.conversation_id)
            clear_death(conn, tab.conversation_id)
            return
    if row["context_dead_why"] is None and map_screen_to_limit_reached(screen):
        # The host's transcript is out of reach: whatever reached the tab since the last look may never have been read.
        record_death(conn, tab.conversation_id, DEAD_LIMIT_SCREEN, at_ms, at_ms - 2 * CONTEXT_EVERY_S * 1000)
    row = conn.execute("SELECT * FROM conversations WHERE conversation_id = ?", (tab.conversation_id,)).fetchone()
    dead = row["context_dead_why"]
    if dead is not None and (row["context_dead_alerted_at"] is None or dead not in DEAD_COMPACTABLE):
        if row["context_dead_alerted_at"] is None:
            _handle_death(conn, config, tab, row, at_ms, actions)
        return
    if not config.compact_at_tokens:
        return
    share_pct = int(COMPACT_AT_WINDOW_SHARE * 100)
    phase = row["compact_phase"]
    requested = row["compact_requested_at"]
    since_request = at_ms - int(requested) if requested is not None else None
    if phase is None and since_request is not None and since_request < RETRY_AFTER_MS:
        # A cycle started lately and given up; its give-up told the user, and a retry every tick would only repeat it.
        return
    asked = row["compact_warned_at"] is not None and phase is not None
    if dead is None and pct is not None and pct >= share_pct and row["context_tokens"] is None and not asked:
        _request_compaction(conn, tab, pct, at_ms, actions)
        return
    waited = phase == PHASE_REQUESTED and since_request is not None and since_request >= REQUEST_PATIENCE_MS
    stale = phase == PHASE_COMPACTING and since_request is not None and since_request >= STALE_DRIVE_MS
    full = dead is not None or (pct is not None and pct >= COMPACT_NOW_PCT)
    if not full or not (phase is None or waited or stale) or not map_screen_to_waiting(screen):
        return
    if stale:
        conn.execute("UPDATE conversations SET compact_phase = NULL WHERE conversation_id = ?", (tab.conversation_id,))
    # A dead tab's alert already said bridge compacts it.
    if _start_compaction(conn, tab, row["context_tokens"], at_ms, actions) and dead is None:
        _tell_forced(conn, config, tab, cast(int, pct), at_ms, actions)


def _request_compaction(
    conn: sqlite3.Connection, tab: ContextTab, pct: int, at_ms: int, actions: ContextActions
) -> None:
    """For a tab whose transcript bridge cannot read, as a host worker's: the hook's request, sent as a message so it
    reaches an idle tab too. Its Stop hook then starts /compact once the message is in."""
    recipient = find_instance_recipient(conn, tab)
    if recipient is None:
        return
    body = convert_tokens_to_request(0, True, pct)
    kind = conn.execute("SELECT kind FROM instances WHERE instance_id = ?", (tab.instance_id,)).fetchone()
    if kind is not None and str(kind["kind"] or "").startswith(HOST_KIND_PREFIX):
        body = convert_bridge_commands_to_gbridge(body)
    result = send_message(conn, Party(SYSTEM_CONVERSATION, "bridge"), [recipient], "info", body, at_ms)
    conn.execute("UPDATE messages SET post_type = ? WHERE id = ?", (BLOCKED_TAB, result.message_id))
    conn.execute(
        "UPDATE conversations SET compact_warned_at = 0, compact_phase = ?, compact_requested_at = ?,"
        " compact_request_message = ? WHERE conversation_id = ?",
        (PHASE_REQUESTED, at_ms, result.message_id, tab.conversation_id),
    )
    actions.messages.append((result.message_id, recipient))


def _start_compaction(
    conn: sqlite3.Connection, tab: ContextTab, tokens: int | None, at_ms: int, actions: ContextActions
) -> bool:
    # Dated now, not by an old request: a phase that stays this long after the drive's wait outlived it.
    cursor = conn.execute(
        "UPDATE conversations SET compact_phase = ?, compact_warned_at = coalesce(compact_warned_at, ?),"
        " compact_requested_at = ?"
        " WHERE conversation_id = ? AND (compact_phase IS NULL OR compact_phase = ?)",
        (PHASE_COMPACTING, tokens or 0, at_ms, tab.conversation_id, PHASE_REQUESTED),
    )
    if cursor.rowcount != 1:
        return False
    actions.drives.append((tab.instance_id, tab.conversation_id, True))
    return True


def _tell_forced(
    conn: sqlite3.Connection, config: Config, tab: ContextTab, pct: int, at_ms: int, actions: ContextActions
) -> None:
    if not take_told_once(conn, TOLD_FORCED, tab.conversation_id, at_ms):
        return
    recipients = find_alert_recipients(conn, config, tab)
    if not recipients:
        return
    body = (
        f'Tab "{tab.name}" ({tab.session}) is at {pct}% of its window and has not compacted, so bridge is typing '
        "/compact into it. Someone's words in its box are left alone, and so is any text while the tab is selected; "
        f"then bridge says so once and tries again {RETRY_AFTER_MS // 60_000} min after each wait, or you compact it "
        "by hand."
    )
    result = send_message(conn, Party(SYSTEM_CONVERSATION, "bridge"), recipients, "info", body, at_ms)
    conn.execute("UPDATE messages SET post_type = ? WHERE id = ?", (BLOCKED_TAB, result.message_id))
    actions.messages.extend((result.message_id, r) for r in recipients)


def _handle_death(
    conn: sqlite3.Connection,
    config: Config,
    tab: ContextTab,
    row: sqlite3.Row,
    at_ms: int,
    actions: ContextActions,
) -> None:
    """Once per death: bridge types /compact if it may, and the assistant and a worker's lead hear what happened."""
    why_key = str(row["context_dead_why"])
    compactable = why_key in DEAD_COMPACTABLE
    may_start = compactable and bool(config.compact_at_tokens)
    started = may_start and _start_compaction(conn, tab, row["context_tokens"], at_ms, actions)
    dead_at = row["context_dead_at"]
    since = f" for {format_age(at_ms - int(dead_at))}" if dead_at is not None else ""
    why = _DEAD_WORDS.get(why_key, why_key)
    if not compactable:
        did = (
            "bridge does not compact it, since compaction does not cure an API error (an outage, a login, a sleep). "
            "Look at its screen; mail to it waits until it answers again. To compact it anyway"
        )
    elif started:
        did = (
            "bridge is typing /compact into it once it waits for input, never over someone's words in its box; "
            "after compaction it gets its saved notes, the messages it missed, and a prompt to continue. If it is "
            "still stuck in 15 min, compact it by hand"
        )
    elif row["compact_phase"] is not None:
        did = f"a compaction is already under way ({row['compact_phase']}); if it does not finish, compact it by hand"
    else:
        did = "bridge could not start /compact (compaction is off in its config): compact it by hand"
    body = (
        f'Tab "{tab.name}" ({tab.session}) cannot take a turn{since}: {why}. Nothing typed into it reaches the model; '
        f'{did}: agtermctl session type --target {tab.session} --pane-id {tab.pane} "/compact", then the same with '
        "$'\\r' for Enter."
    )
    recipients = find_alert_recipients(conn, config, tab)
    conn.execute(
        "UPDATE conversations SET context_dead_alerted_at = ? WHERE conversation_id = ?", (at_ms, tab.conversation_id)
    )
    if not recipients:
        text = f'bridge: tab "{tab.name}" has a full context and no one to tell; compact it by hand'
        add_user_item(conn, Party(SYSTEM_CONVERSATION, "bridge"), "action", text[:140], at_ms)
        return
    result = send_message(conn, Party(SYSTEM_CONVERSATION, "bridge"), recipients, "info", body[:2000], at_ms)
    conn.execute("UPDATE messages SET post_type = ? WHERE id = ?", (BLOCKED_TAB, result.message_id))
    actions.messages.extend((result.message_id, r) for r in recipients)


def find_alert_recipients(conn: sqlite3.Connection, config: Config, tab: ContextTab) -> list[Recipient]:
    found: dict[str, Recipient] = {}
    try:
        assistant = resolve_handle(conn, config.assistant_handle)
    except AddressError:
        assistant = None
    if assistant is not None and not is_conversation_locked(conn, config, assistant.conversation_id):
        found[assistant.conversation_id] = assistant
    if tab.parent and (lead := find_tab_recipient(conn, config, tab.parent)):
        found[lead.conversation_id] = lead
    # A dead tab cannot read its own alert.
    found.pop(tab.conversation_id, None)
    return list(found.values())


def find_instance_recipient(conn: sqlite3.Connection, tab: ContextTab) -> Recipient | None:
    """The very Claude and conversation that were read, not whatever else runs in the tab's other pane."""
    row = conn.execute(
        """
        SELECT i.conversation_id, i.pid, i.status, i.peer_name, i.peer_messaging, s.handle FROM instances i
        JOIN surfaces s ON s.surface = i.surface
        WHERE i.instance_id = ? AND i.conversation_id = ? AND i.ended_at IS NULL
        """,
        (tab.instance_id, tab.conversation_id),
    ).fetchone()
    if row is None:
        return None
    return Recipient(
        handle=str(row["handle"] or row["conversation_id"][:8]),
        conversation_id=str(row["conversation_id"]),
        pid=int(row["pid"]),
        status=str(row["status"]),
        peer_name=row["peer_name"],
        peer_messaging=bool(row["peer_messaging"]),
    )
