import contextlib
import fcntl
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from urllib.parse import urlparse

from bridge.board import (
    BODY_MAX_CHARS,
    SYSTEM_CONVERSATION,
    USER_CONVERSATION,
    USER_ITEM_MAX_CHARS,
    AddressError,
    MessageStateError,
    Party,
    Recipient,
    SendResult,
    convert_text_to_preview,
    is_delivery_owner,
    resolve_handle,
    send_message,
)
from bridge.compaction import map_screen_to_busy, map_screen_to_draft
from bridge.config import Config, state_dir
from bridge.db import now_ms, transaction
from bridge.errlog import log_error
from bridge.leads import find_lead_conversations, is_conversation_locked, is_surface_locked, list_leads
from bridge.procinfo import pid_alive, process_start_ms
from bridge.refs import extract_refs
from bridge.registry import PROC_START_TOLERANCE_MS, RECENT_REF_TURNS, current_ref_sql
from bridge.tabinput import (
    BRIDGE_TEXT_PREFIX,
    PROBE_KEY,
    TYPE_WAIT_S,
    TypeResult,
    is_tab_selected,
    map_text_to_typable,
    type_when_idle,
)

FEED_KINDS = {"blocked": "blocker", "decision": "action", "done": "info", "insight": "info"}
OPEN_KINDS = frozenset({"blocked", "decision"})
"""Kinds that wait for the user's answer; the others are news."""
OPTIONS_MAX = 6
OPTION_MAX_CHARS = 60
LINKS_MAX = 8
ANSWER_MAX_CHARS = 1000
_PR_NUMBER_RE = re.compile(r"(?<![\w/.-])#?(\d{3,6})\b")
POST_TYPES = ("heads-up", "question", "report", "handoff")
TYPED_PEER_PREVIEW_CHARS = 500
DELIVERY_RETRY_EVERY_MS = 600_000
UNDELIVERED_ALERT_AFTER_MS = 600_000
TYPER_LOCK_KEPT_S = 86_400
LOCKED_RETRY_DELAYS_S = (0.5, 1.0, 2.0, 4.0)
# Outside the pane lock a long wait costs only the typer's own process, and giving up there loses the delivery.
LOCKED_PATIENT_DELAYS_S = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0, *(60.0,) * 8)
TAB_NOT_FOUND = "Its tab was not found"
AGTERM_SILENT = "agterm is not answering"
SCREEN_UNREAD = "agterm shows its tab but does not return its screen"
BOX_PREVIEW_CHARS = 120
RELAYED_ANSWER = "relayed-answer"
WORKER_REPORT = "worker-report"
LEAD_TO_WORKER = "lead-to-worker"
TO_ASSISTANT = "to-assistant"
TO_LEAD = "to-lead"
DELIVERY_MARKS = frozenset({WORKER_REPORT, LEAD_TO_WORKER, TO_ASSISTANT, TO_LEAD})
"""Say how a message is delivered, not what kind of post it is, so the page does not read them."""
BLOCKED_TAB = "blocked-tab"
REVIEW_QUEUE = "review-queue"
_KEPT_RETRYING = (RELAYED_ANSWER, WORKER_REPORT, LEAD_TO_WORKER, TO_ASSISTANT, TO_LEAD, REVIEW_QUEUE)
_THREAD_KEY_RE = re.compile(r"[\w#:/.-]{1,80}")
_SQUASHED_DUE = r",(?:respondwithin\d+m|\d+moverdue)"
_SQUASHED_REQUEST_DUE_RE = re.compile(rf"\[bridge\]PeerREQUEST#\d+from[^,]+({_SQUASHED_DUE}),notfromtheuser;")
_MENTION_RE = re.compile(r"(?<![\w.])@([\w-]{1,64})")


@dataclass(frozen=True)
class FeedLink:
    label: str
    url: str


def map_arg_to_link(text: str) -> FeedLink:
    """`label=https://…` or a bare URL; only web links, so a page click cannot open anything else."""
    label, sep, url = text.partition("=")
    if not sep or not url.startswith(("http://", "https://")):
        label, url = "", text
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise MessageStateError(f"not a web link: {text!r}; use label=https://…")
    return FeedLink(label=label.strip() or parsed.netloc, url=url.strip())


def add_post(
    conn: sqlite3.Connection,
    sender: Party,
    kind: str,
    title: str,
    body: str,
    links: list[FeedLink],
    options: list[str],
    at_ms: int,
    urgent: bool = False,
) -> int:
    if kind not in FEED_KINDS:
        raise MessageStateError(f"kind must be one of {', '.join(FEED_KINDS)}")
    title = " ".join(title.split())
    if not title:
        raise MessageStateError("empty title")
    if len(title) > USER_ITEM_MAX_CHARS:
        raise MessageStateError(
            f"keep the title under {USER_ITEM_MAX_CHARS} chars (got {len(title)}); details go in --body"
        )
    if len(body) > BODY_MAX_CHARS:
        raise MessageStateError(f"body is {len(body)} chars; keep it under {BODY_MAX_CHARS}")
    if options and kind not in OPEN_KINDS:
        raise MessageStateError("options are for blocked and decision posts")
    if urgent and kind not in OPEN_KINDS:
        raise MessageStateError("only a blocked or decision post can be urgent")
    if len(options) > OPTIONS_MAX or any(not o.strip() or len(o) > OPTION_MAX_CHARS for o in options):
        raise MessageStateError(f"up to {OPTIONS_MAX} options, each 1 to {OPTION_MAX_CHARS} chars")
    if len(links) > LINKS_MAX:
        raise MessageStateError(f"up to {LINKS_MAX} links")
    cursor = conn.execute(
        """
        INSERT INTO user_items (created_at, from_conversation, from_handle, severity, text, source, kind, body, links,
                                options, urgent)
        VALUES (?, ?, ?, ?, ?, 'agent', ?, ?, ?, ?, ?)
        """,
        (
            at_ms,
            sender.conversation_id,
            sender.handle,
            FEED_KINDS[kind],
            title,
            kind,
            body.strip() or None,
            json.dumps([{"label": link.label, "url": link.url} for link in links]) if links else None,
            json.dumps([o.strip() for o in options]) if options else None,
            int(urgent),
        ),
    )
    return int(cursor.lastrowid or 0)


def answer_post(
    conn: sqlite3.Connection, item_id: int, answer: str, user: str, at_ms: int, relayed_by: Party | None = None
) -> SendResult:
    """Closes a blocked or decision post with the user's answer and queues the answer for the session. Sent as the user
    only for the user's own click; a relayed answer stays the relay's message and says so."""
    answer = answer.strip()
    if not answer:
        raise MessageStateError("empty answer")
    if len(answer) > ANSWER_MAX_CHARS:
        raise MessageStateError(f"answer is {len(answer)} chars; keep it under {ANSWER_MAX_CHARS}")
    row = conn.execute(
        "SELECT kind, text, from_conversation, from_handle, answered_at FROM user_items WHERE id = ?", (item_id,)
    ).fetchone()
    if row is None or row["from_conversation"] is None:
        raise MessageStateError(f"no post #{item_id} from a session")
    if row["answered_at"] is not None:
        raise MessageStateError(f"post #{item_id} is already answered")
    conn.execute(
        """
        UPDATE user_items SET answer = ?, answered_at = ?, answered_by = ?, resolved_at = coalesce(resolved_at, ?)
        WHERE id = ?
        """,
        (answer, at_ms, relayed_by.handle if relayed_by else None, at_ms, item_id),
    )
    recipient = resolve_tab(conn, str(row["from_conversation"]), str(row["from_handle"]))
    kind = row["kind"] or "post"
    if relayed_by is not None:
        body = f'{relayed_by.handle}, relaying {user}\'s answer to your {kind} "{row["text"]}": {answer}'
        relayed = send_message(conn, relayed_by, [recipient], "info", body, at_ms)
        conn.execute("UPDATE messages SET post_type = ? WHERE id = ?", (RELAYED_ANSWER, relayed.message_id))
        conn.execute("UPDATE user_items SET answer_message_id = ? WHERE id = ?", (relayed.message_id, item_id))
        return relayed
    body = f'Answer to your {kind} "{row["text"]}": {answer}'
    sent = send_message(conn, Party(USER_CONVERSATION, user), [recipient], "info", body, at_ms)
    conn.execute("UPDATE user_items SET answer_message_id = ? WHERE id = ?", (sent.message_id, item_id))
    return sent


def withdraw_post(conn: sqlite3.Connection, sender: Party, item_id: int, at_ms: int) -> None:
    """A question its author settled without the user. Its tab counts, so /clear does not orphan the post."""
    row = conn.execute(
        "SELECT kind, from_conversation, from_handle, resolved_at FROM user_items WHERE id = ? AND source = 'agent'",
        (item_id,),
    ).fetchone()
    if row is None or row["kind"] not in OPEN_KINDS:
        raise MessageStateError(f"no blocked or decision post #{item_id}")
    if resolve_tab(conn, str(row["from_conversation"]), str(row["from_handle"])).conversation_id != (
        sender.conversation_id
    ):
        raise MessageStateError(f"post #{item_id} is {row['from_handle']}'s, and only its author withdraws it")
    if row["resolved_at"] is not None:
        raise MessageStateError(f"post #{item_id} is already closed")
    conn.execute("UPDATE user_items SET resolved_at = ? WHERE id = ?", (at_ms, item_id))


def dismiss_undelivered_answer(conn: sqlite3.Connection, item_id: int, at_ms: int) -> bool:
    cursor = conn.execute(
        """
        UPDATE user_items SET answer_dismissed_at = ?
        WHERE id = ? AND answer_message_id IS NOT NULL AND answer_dismissed_at IS NULL
        """,
        (at_ms, item_id),
    )
    return cursor.rowcount > 0


def send_from_user(
    conn: sqlite3.Connection,
    config: Config,
    user: str,
    to: list[str],
    text: str,
    at_ms: int,
    reply_to: int | None = None,
) -> tuple[SendResult, list[Recipient]]:
    """The user's message to sessions by handle, a reply in a thread, or (no handles, no thread) a team-wide post.

    Returns the result and the recipients whose idle tabs should get it typed in.
    """
    recipients = [resolve_handle(conn, h) for h in to]
    reject_locked_recipients(conn, config, recipients)
    kind = "info"
    if reply_to is not None:
        parent = conn.execute(
            "SELECT from_conversation, from_handle FROM messages WHERE id = ?", (reply_to,)
        ).fetchone()
        if parent is None:
            raise MessageStateError(f"no message #{reply_to}")
        if parent["from_conversation"] != USER_CONVERSATION:
            recipients.append(resolve_tab(conn, str(parent["from_conversation"]), str(parent["from_handle"])))
        else:
            # Following up on one's own message goes to whoever it went to.
            for row in conn.execute(
                "SELECT conversation_id, handle FROM message_recipients WHERE message_id = ? AND conversation_id != ?",
                (reply_to, USER_CONVERSATION),
            ):
                if not is_conversation_locked(conn, config, str(row["conversation_id"])):
                    recipients.append(resolve_tab(conn, str(row["conversation_id"]), str(row["handle"])))
        kind = "reply"
    elif not recipients:
        # The lead talking to the team: every session reads it at its next turn, and the ones it concerns
        # (mentioned, or on a PR or issue it names) get it typed in right away.
        team = list_team(conn, config)
        concerned = {r.conversation_id for r in find_board_recipients(conn, text, config)}
        result = send_message(conn, Party(USER_CONVERSATION, user), team, "info", text, at_ms)
        conn.execute("UPDATE messages SET board = 1 WHERE id = ?", (result.message_id,))
        return result, [r for r in team if r.conversation_id in concerned]
    unique = list({r.conversation_id: r for r in recipients}.values())
    result = send_message(conn, Party(USER_CONVERSATION, user), unique, kind, text, at_ms, reply_to=reply_to)
    return result, unique


def post_to_board(
    conn: sqlite3.Connection,
    sender: Party,
    text: str,
    topic: str | None,
    at_ms: int,
    config: Config,
    post_type: str | None = None,
    thread_key: str | None = None,
    link: str | None = None,
) -> SendResult:
    """A post everyone can read; it reaches the sessions it mentions or whose PRs and issues it names."""
    if post_type is not None and post_type not in POST_TYPES:
        raise MessageStateError(f"type must be one of {', '.join(POST_TYPES)}")
    if thread_key is not None and not _THREAD_KEY_RE.fullmatch(thread_key):
        raise MessageStateError("a thread is an issue or PR id: letters, digits and - _ # : / .")
    if link is not None and not _is_web_url(link):
        raise MessageStateError("a link is an http(s) URL")
    found = find_board_recipients(conn, text, config)
    if sender.conversation_id in find_lead_conversations(conn):
        found = _add_every_lead(conn, config, found)
    recipients = [r for r in found if r.conversation_id != sender.conversation_id]
    thread_id: int | None = None
    if thread_key is not None:
        root = conn.execute(
            "SELECT thread_id FROM messages WHERE thread_key = ? AND board = 1 ORDER BY id LIMIT 1", (thread_key,)
        ).fetchone()
        thread_id = int(root["thread_id"]) if root else None
    result = send_message(conn, sender, recipients, "info", text, at_ms, thread_id=thread_id)
    conn.execute(
        "UPDATE messages SET board = 1, topic = ?, post_type = ?, thread_key = ?, link = ? WHERE id = ?",
        (topic, post_type, thread_key, link, result.message_id),
    )
    return result


def post_from_user_to_room(
    conn: sqlite3.Connection, config: Config, user: str, text: str, at_ms: int, reply_to: int | None
) -> tuple[SendResult, list[Recipient]]:
    """The room is a log: the user's post reaches who it concerns, and mentions and the person answered are typed in."""
    party = Party(USER_CONVERSATION, user)
    if reply_to is not None:
        return _reply_in_room(conn, config, user, text, at_ms, reply_to)
    recipients = _add_every_lead(conn, config, find_board_recipients(conn, text, config))
    result = send_message(conn, party, recipients, "info", text, at_ms)
    conn.execute("UPDATE messages SET board = 1 WHERE id = ?", (result.message_id,))
    mentioned = {r.conversation_id for r in find_mentioned_recipients(conn, text, config)}
    return result, [r for r in recipients if r.conversation_id in mentioned]


def _reply_in_room(
    conn: sqlite3.Connection, config: Config, user: str, text: str, at_ms: int, reply_to: int
) -> tuple[SendResult, list[Recipient]]:
    """A human post resumes a paused thread, so everyone who wrote in it hears, and can read what was held back."""
    mentioned = find_mentioned_recipients(conn, text, config)
    parent = conn.execute("SELECT thread_id, from_conversation FROM messages WHERE id = ?", (reply_to,)).fetchone()
    if parent is None:
        raise MessageStateError(f"no message #{reply_to}")
    thread_id = int(parent["thread_id"])
    held = conn.execute("SELECT 1 FROM messages WHERE thread_id = ? AND paused = 1", (thread_id,)).fetchone()
    writers: list[Recipient] = []
    body = text
    if held:
        body = f"{text} (This thread had paused; what was held back: bridge show {thread_id})"
        for row in conn.execute(
            "SELECT DISTINCT from_conversation, from_handle FROM messages "
            "WHERE thread_id = ? AND from_conversation NOT IN (?, ?)",
            (thread_id, USER_CONVERSATION, SYSTEM_CONVERSATION),
        ):
            writers.append(resolve_tab(conn, str(row["from_conversation"]), str(row["from_handle"])))
    result, typed = send_from_user(conn, config, user, [r.handle for r in mentioned], body, at_ms, reply_to)
    if parent["from_conversation"] == USER_CONVERSATION:
        # A follow-up to the user's own post inherits every lead it reached; as with the post, only a mention wakes.
        named = {r.conversation_id for r in mentioned}
        typed = [r for r in typed if r.conversation_id in named]
    extra = [r for r in writers if r.conversation_id not in {t.conversation_id for t in result.recipients}]
    for recipient in extra:
        conn.execute(
            "INSERT OR IGNORE INTO message_recipients (message_id, conversation_id, handle) VALUES (?, ?, ?)",
            (result.message_id, recipient.conversation_id, recipient.handle),
        )
    return replace(result, recipients=[*result.recipients, *extra]), typed


def _add_every_lead(conn: sqlite3.Connection, config: Config, found: list[Recipient]) -> list[Recipient]:
    """At their next hook; only a mention wakes one. A lead with no live session has nothing to deliver to."""
    recipients = {r.conversation_id: r for r in found}
    for lead in list_leads(conn):
        try:
            recipient = resolve_handle(conn, lead.name)
        except AddressError:
            continue
        if not is_conversation_locked(conn, config, recipient.conversation_id):
            recipients.setdefault(recipient.conversation_id, recipient)
    return list(recipients.values())


def find_mentioned_recipients(conn: sqlite3.Connection, text: str, config: Config) -> list[Recipient]:
    found: list[Recipient] = []
    for handle in _MENTION_RE.findall(text):
        try:
            recipient = resolve_handle(conn, handle.lower())
        except AddressError:
            continue
        if not is_conversation_locked(conn, config, recipient.conversation_id):
            found.append(recipient)
    return list({r.conversation_id: r for r in found}.values())


def find_leads_to_wake(conn: sqlite3.Connection, config: Config, text: str, sender: Party) -> list[Recipient]:
    """Only a mention wakes an idle lead: a wake costs tokens, and everything else waits for its next hook."""
    leads = find_lead_conversations(conn)
    return [
        r
        for r in find_mentioned_recipients(conn, text, config)
        if r.conversation_id in leads and r.conversation_id != sender.conversation_id
    ]


def reject_locked_recipients(conn: sqlite3.Connection, config: Config, recipients: list[Recipient]) -> None:
    for recipient in recipients:
        if is_conversation_locked(conn, config, recipient.conversation_id):
            raise AddressError(f"{recipient.handle} is in a locked tab, and locked tabs are left out")


def list_team(conn: sqlite3.Connection, config: Config) -> list[Recipient]:
    """Every live local session the dashboard shows: tabs hidden with a lock mark stay out."""
    team: dict[str, Recipient] = {}
    for row in conn.execute(
        """
        SELECT s.handle, s.tab_name, s.workspace_name, i.conversation_id, i.pid, i.status, i.peer_name,
               i.peer_messaging
        FROM instances i JOIN surfaces s ON s.surface = i.surface
        WHERE i.ended_at IS NULL AND i.conversation_id IS NOT NULL AND s.handle IS NOT NULL
        ORDER BY i.proc_start_ms
        """
    ):
        if config.hides(row["tab_name"], row["workspace_name"]):
            continue
        team[str(row["conversation_id"])] = Recipient(
            handle=str(row["handle"]),
            conversation_id=str(row["conversation_id"]),
            pid=int(row["pid"]),
            status=str(row["status"]),
            peer_name=row["peer_name"],
            peer_messaging=bool(row["peer_messaging"]),
        )
    return list(team.values())


def _is_web_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def find_board_recipients(conn: sqlite3.Connection, text: str, config: Config) -> list[Recipient]:
    found: dict[str, Recipient] = {}
    for handle in _MENTION_RE.findall(text):
        try:
            recipient = resolve_handle(conn, handle.lower())
        except AddressError:
            continue
        found[recipient.conversation_id] = recipient
    refs = extract_refs(text, issue_prefixes=config.issue_prefixes, default_repo=None)
    # "41 merged" names no repo; it still means the PR a session is on with that number.
    numbers = sorted(set(_PR_NUMBER_RE.findall(text)))
    if refs or numbers:
        marks = ",".join("?" * len(refs)) or "NULL"
        number_marks = ",".join("?" * len(numbers)) or "NULL"
        for row in conn.execute(
            f"""
            SELECT DISTINCT s.handle, i.conversation_id, i.pid, i.status, i.peer_name, i.peer_messaging
            FROM refs r JOIN conversations c ON c.conversation_id = r.conversation_id
            JOIN instances i ON i.conversation_id = c.conversation_id AND i.ended_at IS NULL
            JOIN surfaces s ON s.surface = i.surface
            LEFT JOIN enrichment e ON e.ref = r.ref
            WHERE (r.ref IN ({marks})
                   OR (r.ref LIKE 'pr:%' AND substr(r.ref, instr(r.ref, '#') + 1) IN ({number_marks})))
              AND s.handle IS NOT NULL
              AND {current_ref_sql(f"c.turns - {RECENT_REF_TURNS}")}
            """,
            [*refs, *numbers],
        ):
            found.setdefault(
                str(row["conversation_id"]),
                Recipient(
                    handle=str(row["handle"]),
                    conversation_id=str(row["conversation_id"]),
                    pid=int(row["pid"]),
                    status=str(row["status"]),
                    peer_name=row["peer_name"],
                    peer_messaging=bool(row["peer_messaging"]),
                ),
            )
    team = {r.conversation_id for r in list_team(conn, config)}
    return [r for r in found.values() if r.conversation_id in team]


def resolve_tab(conn: sqlite3.Connection, conversation_id: str, handle: str) -> Recipient:
    """Mail follows the sender's tab (its agterm pane), not its handle: a handle can move to another tab.

    If that conversation was cleared, the tab's current one gets it; with the tab gone it waits, undelivered.
    """
    row = conn.execute(
        """
        SELECT i.conversation_id, i.pid, i.status, i.peer_name, i.peer_messaging FROM instances i
        WHERE i.conversation_id = ? AND i.ended_at IS NULL ORDER BY i.proc_start_ms DESC LIMIT 1
        """,
        (conversation_id,),
    ).fetchone()
    if row is None and conversation_id != USER_CONVERSATION:
        # After /clear the runtime that held the conversation now holds another one; follow it, or its pane.
        row = conn.execute(
            """
            SELECT i.conversation_id, i.pid, i.status, i.peer_name, i.peer_messaging
            FROM conversations c JOIN instances held ON held.instance_id = c.instance_id
            JOIN instances i ON (i.instance_id = held.instance_id OR i.surface = held.surface)
            WHERE c.conversation_id = ? AND i.ended_at IS NULL AND i.conversation_id IS NOT NULL
            ORDER BY i.instance_id = held.instance_id DESC, i.proc_start_ms DESC LIMIT 1
            """,
            (conversation_id,),
        ).fetchone()
    return Recipient(
        handle=handle,
        conversation_id=str(row["conversation_id"]) if row else conversation_id,
        pid=int(row["pid"]) if row else 0,
        status=str(row["status"]) if row else "ended",
        peer_name=row["peer_name"] if row else None,
        peer_messaging=bool(row["peer_messaging"]) if row else False,
    )


class StillLockedError(Exception):
    """The database stayed locked through every retry; the give-up is already logged."""


def retry_when_locked[T](what: str, call: Callable[[], T], delays: tuple[float, ...] = LOCKED_RETRY_DELAYS_S) -> T:
    """call again after each delay while SQLite says the database is locked. Anything else raises at once: only a
    lock is worth waiting out."""
    for delay in delays:
        try:
            return call()
        except sqlite3.OperationalError as exc:
            if not _is_lock_error(exc):
                raise
            time.sleep(delay)
    try:
        return call()
    except sqlite3.OperationalError as exc:
        if not _is_lock_error(exc):
            raise
        log_error("deliver", f"{what}: gave up, {exc}")
        raise StillLockedError(what) from exc


def _is_lock_error(exc: sqlite3.OperationalError) -> bool:
    return "locked" in str(exc) or "busy" in str(exc)


def _find_typable_row(
    conn: sqlite3.Connection, config: Config, message_id: int, conversation_id: str
) -> sqlite3.Row | None:
    # Whoever spawned this decided it is typed; the undelivered alert counts only such rows.
    conn.execute(
        "UPDATE message_recipients SET typing_wanted_at = coalesce(typing_wanted_at, ?)"
        " WHERE message_id = ? AND conversation_id = ?",
        (now_ms(), message_id, conversation_id),
    )
    row = conn.execute(
        """
        SELECT m.from_conversation, m.from_handle, m.body, m.kind, m.respond_by, s.agterm_session, i.surface,
               i.instance_id
        FROM messages m JOIN message_recipients r ON r.message_id = m.id
        JOIN instances i ON i.conversation_id = r.conversation_id AND i.ended_at IS NULL AND i.agent = 'claude'
        JOIN surfaces s ON s.surface = i.surface
        WHERE m.id = ? AND r.conversation_id = ? AND r.acked_at IS NULL AND r.offered_at IS NULL
          AND s.agterm_session IS NOT NULL
          AND m.cancelled_at IS NULL
          AND (m.from_conversation = 'user'
               OR m.post_type IN ('blocked-tab', 'lead-to-worker', 'to-assistant', 'review-queue')
               OR i.surface IN (SELECT surface FROM leads WHERE retired_at IS NULL))
        ORDER BY i.proc_start_ms DESC LIMIT 1
        """,
        (message_id, conversation_id),
    ).fetchone()
    return None if row is None or is_surface_locked(conn, config, row["surface"]) else row


def deliver_typed(conn: sqlite3.Connection, config: Config, message_id: int, conversation_id: str) -> TypeResult | None:
    """Types a message into the recipient's tab once it waits for input, so an idle session acts at once.

    The user's reach any session; a peer's only a team lead, that lead's worker or the assistant, framed as a peer's.
    A busy session gets it from its next hook instead; whichever comes first marks it delivered. A request a hook
    has offered stays unacked for its reminders, so it is not typed as well.
    """
    gave_up = False

    def unlocked[T](what: str, call: Callable[[], T], given_up: T) -> T:
        nonlocal gave_up
        try:
            return retry_when_locked(f"#{message_id} to {conversation_id}: {what}", call)
        except StillLockedError:
            gave_up = True
            return given_up

    try:
        row = retry_when_locked(
            f"#{message_id} to {conversation_id}: find the row",
            lambda: _find_typable_row(conn, config, message_id, conversation_id),
            LOCKED_PATIENT_DELAYS_S,
        )
    except StillLockedError:
        return "failed"
    if row is None:
        return None
    instance_id = str(row["instance_id"])
    surface = str(row["surface"])
    text = _convert_message_to_typed_text(row, message_id, now_ms())
    claimed_at = now_ms()
    claimed = False

    def still_wanted() -> bool:
        # Once claimed it counts as offered, which must not stop the Enter retries.
        pending = conn.execute(
            """
            SELECT 1 FROM message_recipients r JOIN instances i ON i.instance_id = ?
            JOIN messages m ON m.id = r.message_id AND m.cancelled_at IS NULL
            WHERE r.message_id = ? AND r.conversation_id = ?
              AND ((r.acked_at IS NULL AND r.offered_at IS NULL) OR r.offered_at = ?)
              AND i.ended_at IS NULL AND i.conversation_id = r.conversation_id
            """,
            (instance_id, message_id, conversation_id, claimed_at if claimed else -1),
        ).fetchone()
        return (
            pending is not None
            and is_delivery_owner(conn, conversation_id, instance_id)
            and not is_surface_locked(conn, config, surface)
        )

    def claim() -> bool:
        nonlocal claimed, claimed_at
        # Its time is when the claim is taken, not when the wait began: the age backstop counts from it.
        claimed_at = now_ms()
        # Marked before the Enter: the prompt it submits runs a hook that would offer it again. As in a hook's offer,
        # a request stays unacked: typing it in is not an answer, and its sender keeps waiting and can escalate.
        with transaction(conn):
            cursor = conn.execute(
                """
                UPDATE message_recipients
                SET acked_at = CASE WHEN ? = 'request' THEN acked_at ELSE ? END, offered_at = ?,
                    offer_count = offer_count + 1, typing_started_at = ?, typing_pid = ?, typing_pid_start_ms = ?
                WHERE message_id = ? AND conversation_id = ? AND acked_at IS NULL AND offered_at IS NULL
                """,
                (row["kind"], claimed_at, claimed_at, claimed_at, *_this_typer(), message_id, conversation_id),
            )
            claimed = cursor.rowcount == 1
            return claimed

    def release() -> None:
        # By the claim and not the conversation: a /clear during typing moves the row to the tab's new conversation.
        with transaction(conn):
            conn.execute(
                """
                UPDATE message_recipients
                SET acked_at = CASE WHEN acked_at = ?1 THEN NULL ELSE acked_at END,
                    offer_count = max(offer_count - 1, 0), offered_at = NULL, typing_started_at = NULL,
                    typing_pid = NULL, typing_pid_start_ms = NULL
                WHERE message_id = ?2 AND typing_started_at = ?1
                """,
                (claimed_at, message_id),
            )

    session = str(row["agterm_session"])

    def may_probe_box() -> bool:
        # Only with the agent at rest and the user elsewhere: a probe key must not land among theirs.
        status = conn.execute("SELECT status FROM instances WHERE instance_id = ?", (instance_id,)).fetchone()
        return status is not None and status["status"] == "idle" and not is_tab_selected(config, session)

    def close_claim() -> None:
        with transaction(conn):
            conn.execute(
                "UPDATE message_recipients SET typing_started_at = NULL, stuck_at = NULL, typing_pid = NULL,"
                " typing_pid_start_ms = NULL WHERE message_id = ? AND typing_started_at = ?",
                (message_id, claimed_at),
            )

    def wanted_or_stop() -> bool:
        if not claimed:
            return unlocked("check it is still wanted", still_wanted, False)
        # Unreadable once typed: an Enter may submit a cancelled message, and "unwanted" after one counts as sent.
        return retry_when_locked(f"#{message_id} to {conversation_id}: check it is still wanted", still_wanted)

    try:
        result = type_when_idle(
            config,
            session,
            surface,
            text,
            wanted_or_stop,
            claim=lambda: unlocked("claim", claim, False),
            # A claim left here is released once this typer has exited, by release_dead_typing_claims.
            release=lambda: unlocked("release the claim", release, None),
            may_probe_box=lambda: unlocked("check the probe may go", may_probe_box, False),
            own_pattern=map_typed_text_to_pattern(text),
            claim_left_unsent=lambda box: unlocked(
                "claim text left unsent", lambda: claim_unsent_message(conn, conversation_id, box), None
            ),
        )
    except StillLockedError:
        # Typed, its Enter unproven: as a stuck one, the retry pass presses Enter only if it is still wanted.
        with contextlib.suppress(StillLockedError):
            what = f"#{message_id} to {conversation_id}: release the claim"
            retry_when_locked(what, release, LOCKED_PATIENT_DELAYS_S)
        return "stuck"
    if result == "typed":
        unlocked("close the claim", close_claim, None)
    # Stopped by the database, not by the message: the retry pass tries again.
    return "failed" if gave_up and result in ("unwanted", "timeout") else result


def find_unsent_message(conn: sqlite3.Connection, conversation_id: str, box: str) -> int | None:
    """The message to this tab whose typed text the box holds unedited, spacing aside; probe keys may follow it."""
    number = re.search(r"#(\d+)", box)
    if number is None:
        return None
    row = conn.execute(
        """
        SELECT m.from_conversation, m.from_handle, m.body, m.kind, m.respond_by
        FROM messages m JOIN message_recipients r ON r.message_id = m.id
        WHERE m.id = ? AND r.conversation_id = ?
          AND m.cancelled_at IS NULL AND r.done_at IS NULL AND r.declined_at IS NULL
        """,
        (int(number.group(1)), conversation_id),
    ).fetchone()
    if row is None:
        return None
    message_id = int(number.group(1))
    pattern = map_typed_text_to_pattern(_convert_message_to_typed_text(row, message_id, now_ms())).pattern
    probe = re.escape(PROBE_KEY)
    return message_id if re.fullmatch(f"{pattern}(?:{probe})*", "".join(map_text_to_typable(box).split())) else None


def claim_unsent_message(conn: sqlite3.Connection, conversation_id: str, box: str) -> Callable[[bool], None] | None:
    """Claimed before its Enter, as typing claims, so the prompt's own hook does not deliver it again; the returned
    settle closes the claim, and a claim its dead typist left, once the Enter took, or puts the row back."""
    message_id = find_unsent_message(conn, conversation_id, box)
    if message_id is None:
        return None
    claimed_at = now_ms()
    with transaction(conn):
        before = conn.execute(
            "SELECT offered_at, acked_at, offer_count, typing_started_at, typing_pid, typing_pid_start_ms, stuck_at"
            " FROM message_recipients"
            " WHERE message_id = ? AND conversation_id = ?",
            (message_id, conversation_id),
        ).fetchone()
        conn.execute(
            """
            UPDATE message_recipients
            SET acked_at = CASE WHEN (SELECT kind FROM messages WHERE id = ?1) = 'request' THEN acked_at
                                WHEN acked_at IS NULL OR acked_at = typing_started_at THEN ?2
                                ELSE acked_at END,
                offer_count = offer_count + 1, offered_at = ?2, typing_started_at = ?2, typing_pid = ?4,
                typing_pid_start_ms = ?5
            WHERE message_id = ?1 AND conversation_id = ?3
            """,
            (message_id, claimed_at, conversation_id, *_this_typer()),
        )

    def write_settle(sent: bool) -> None:
        with transaction(conn):
            if sent:
                conn.execute(
                    "UPDATE message_recipients SET typing_started_at = NULL, stuck_at = NULL, typing_pid = NULL,"
                    " typing_pid_start_ms = NULL WHERE message_id = ? AND typing_started_at = ?",
                    (message_id, claimed_at),
                )
            else:
                # Only what the claim set goes back: an answer that came in meanwhile stays.
                conn.execute(
                    """
                    UPDATE message_recipients
                    SET offered_at = CASE WHEN offered_at = :claimed THEN :offered ELSE offered_at END,
                        acked_at = CASE WHEN acked_at = :claimed THEN :acked ELSE acked_at END,
                        offer_count = CASE WHEN offered_at = :claimed THEN :count ELSE offer_count END,
                        typing_started_at = :typing, typing_pid = :pid, typing_pid_start_ms = :pid_start,
                        stuck_at = :stuck
                    WHERE message_id = :message AND typing_started_at = :claimed
                    """,
                    {
                        "claimed": claimed_at,
                        "offered": before["offered_at"],
                        "acked": before["acked_at"],
                        "count": before["offer_count"],
                        "typing": before["typing_started_at"],
                        "pid": before["typing_pid"],
                        "pid_start": before["typing_pid_start_ms"],
                        "stuck": before["stuck_at"],
                        "message": message_id,
                    },
                )

    def settle(sent: bool) -> None:
        try:
            what = f"#{message_id} to {conversation_id}: settle text left unsent"
            retry_when_locked(what, lambda: write_settle(sent))
        except StillLockedError:
            # Its claim stays with this typer's pid, so release_dead_typing_claims puts it back once it exits.
            pass

    return settle


def _convert_message_to_typed_text(row: sqlite3.Row, message_id: int, at_ms: int) -> str:
    if row["from_conversation"] == USER_CONVERSATION:
        return f"[bridge] {row['from_handle']} on the dashboard (#{message_id}): {row['body']}"
    preview = convert_text_to_preview(str(row["body"]), TYPED_PEER_PREVIEW_CHARS)
    if row["kind"] == "request":
        return _convert_request_to_typed_text(row, message_id, preview, at_ms)
    return (
        f"[bridge] Peer message #{message_id} from {row['from_handle']}, not from the user; use judgment: "
        f"{preview} (full text and answer: bridge show {message_id}, bridge reply {message_id})"
    )


def map_typed_text_to_pattern(text: str) -> re.Pattern[str]:
    """Matches the text, spacing removed, as any try typed it: only a request's time left differs between tries."""
    squashed = "".join(map_text_to_typable(text).split())
    # Only the header's own deadline: one quoted in the body is the body, and an edit to it is someone's.
    due = _SQUASHED_REQUEST_DUE_RE.match(squashed)
    if due is None:
        return re.compile(re.escape(squashed))
    head, tail = squashed[: due.start(1)], squashed[due.end(1) :]
    return re.compile(re.escape(head) + _SQUASHED_DUE + re.escape(tail))


def _convert_request_to_typed_text(row: sqlite3.Row, message_id: int, preview: str, at_ms: int) -> str:
    due = ""
    if row["respond_by"] is not None:
        left_min = (int(row["respond_by"]) - at_ms) // 60_000
        due = f", respond within {left_min}m" if left_min >= 0 else f", {-left_min}m overdue"
    return (
        f"[bridge] Peer REQUEST #{message_id} from {row['from_handle']}{due}, not from the user; use judgment: "
        f"{preview} (full text: bridge show {message_id}; respond: bridge ack {message_id} | "
        f'bridge reply {message_id} "…" [--done] | bridge decline {message_id})'
    )


def record_failed_delivery(
    conn: sqlite3.Connection, message_id: int, conversation_id: str, at_ms: int, unproven: bool = False
) -> None:
    """What could not be typed is retried, and a hook may still deliver it. A user's message also shows on the page,
    unless only unproven text held it, which may be Claude Code's grey suggestion. A lead's every message is retried:
    an idle lead gets no hook."""
    to_lead = conversation_id in find_lead_conversations(conn)
    conn.execute(
        """
        UPDATE message_recipients SET delivery_failed_at = coalesce(delivery_failed_at, ?), delivery_retried_at = ?,
            unproven_at = ?
        WHERE message_id = ? AND conversation_id = ? AND acked_at IS NULL
          AND EXISTS (SELECT 1 FROM messages m WHERE m.id = ?
                       AND (? OR m.from_conversation = ? OR m.post_type IN (?, ?, ?, ?, ?, ?)))
        """,
        (
            at_ms,
            at_ms,
            at_ms if unproven else None,
            message_id,
            conversation_id,
            message_id,
            to_lead,
            USER_CONVERSATION,
            *_KEPT_RETRYING,
        ),
    )


def tell_assistant_of_draft(
    conn: sqlite3.Connection, config: Config, message_id: int, conversation_id: str, at_ms: int
) -> tuple[int, Recipient] | None:
    """Text in a tab's input box holds a wake back, and bridge never clears it: it may be the owner's draft. The
    assistant hears it once per message, and the assistant's own tab shows on the page; the retries go on."""
    row = conn.execute(
        """
        SELECT r.handle, r.draft_told_at, s.agterm_session FROM message_recipients r
        LEFT JOIN instances i ON i.conversation_id = r.conversation_id AND i.ended_at IS NULL
        LEFT JOIN surfaces s ON s.surface = i.surface
        WHERE r.message_id = ? AND r.conversation_id = ?
        """,
        (message_id, conversation_id),
    ).fetchone()
    if row is None or row["draft_told_at"] is not None:
        return None
    # Also marks the draft proven for the undelivered alert, which says nothing of unproven text.
    conn.execute(
        "UPDATE message_recipients SET draft_told_at = ? WHERE message_id = ? AND conversation_id = ?",
        (at_ms, message_id, conversation_id),
    )
    if row["handle"] == config.assistant_handle:
        return None
    try:
        assistant = resolve_handle(conn, config.assistant_handle)
    except AddressError:
        return None
    tab = f"{row['handle']}'s tab" + (f" ({row['agterm_session']})" if row["agterm_session"] else "")
    body = f"{tab} has unsent text in its input box; bridge cannot deliver #{message_id} until it is cleared."
    result = send_message(conn, Party(SYSTEM_CONVERSATION, "bridge"), [assistant], "info", body, at_ms)
    conn.execute("UPDATE messages SET post_type = ? WHERE id = ?", (TO_ASSISTANT, result.message_id))
    return result.message_id, assistant


TYPING_CLAIM_BACKSTOP_MS = 3 * TYPE_WAIT_S * 1000
"""A claim this old is let go even with its typer seemingly alive: no typer waits past TYPE_WAIT_S."""


def _this_typer() -> tuple[int, int | None]:
    return os.getpid(), process_start_ms(os.getpid())


def _is_typer_running(pid: int | None, start_ms: int | None) -> bool:
    # A claim from before typers were recorded has only the age backstop.
    if pid is None:
        return True
    if not pid_alive(pid):
        return False
    started = process_start_ms(pid)
    # Unreadable start times prove nothing: such a claim waits for the age backstop.
    return started is None or start_ms is None or abs(started - start_ms) <= PROC_START_TOLERANCE_MS


def release_dead_typing_claims(conn: sqlite3.Connection, at_ms: int, conversation_id: str | None = None) -> int:
    """A typing claim keeps hooks off its message; one whose typer is gone gives the message back to hooks and to
    the retry pass. Its text may have landed before the typer died, so the tab can get it twice."""
    rows = conn.execute(
        """
        SELECT message_id, conversation_id, typing_started_at, typing_pid, typing_pid_start_ms
        FROM message_recipients WHERE typing_started_at IS NOT NULL AND (?1 IS NULL OR conversation_id = ?1)
        """,
        (conversation_id,),
    ).fetchall()
    released = 0
    for row in rows:
        claimed_at = int(row["typing_started_at"])
        if at_ms - claimed_at < TYPING_CLAIM_BACKSTOP_MS and _is_typer_running(
            row["typing_pid"], row["typing_pid_start_ms"]
        ):
            continue
        cursor = conn.execute(
            """
            UPDATE message_recipients
            SET acked_at = CASE WHEN acked_at = ?1 THEN NULL ELSE acked_at END,
                offered_at = CASE WHEN offered_at = ?1 THEN NULL ELSE offered_at END,
                offer_count = max(offer_count - 1, 0), typing_started_at = NULL, typing_pid = NULL,
                typing_pid_start_ms = NULL
            WHERE message_id = ?2 AND conversation_id = ?3 AND typing_started_at = ?1
            """,
            (claimed_at, row["message_id"], row["conversation_id"]),
        )
        if cursor.rowcount:
            released += 1
            # As a try that failed when it claimed: the retry pass keeps its usual pace.
            record_failed_delivery(conn, int(row["message_id"]), str(row["conversation_id"]), claimed_at)
    return released


def record_stuck_delivery(conn: sqlite3.Connection, message_id: int, conversation_id: str, at_ms: int) -> None:
    """Its text was typed but stayed unsubmitted in the box; the retry presses Enter on it."""
    conn.execute(
        "UPDATE message_recipients SET stuck_at = ? WHERE message_id = ? AND conversation_id = ? AND acked_at IS NULL",
        (at_ms, message_id, conversation_id),
    )


_PENDING_SQL = """
    r.done_at IS NULL AND r.declined_at IS NULL AND m.cancelled_at IS NULL AND r.typing_wanted_at IS NOT NULL
    AND ((r.acked_at IS NULL AND r.offered_at IS NULL) OR r.typing_started_at IS NOT NULL)
"""
"""Meant to be typed and not yet in the tab: a claim taken for typing counts until its Enter is confirmed. What waits
for the next hook by design, such as a board post naming nobody, is not overdue."""


@dataclass(frozen=True)
class UndeliveredTab:
    conversation_id: str
    handle: str
    agterm_session: str | None
    surface: str | None
    tab_name: str | None
    oldest_message_id: int
    oldest_at: int
    waiting: int
    stuck_message_id: int | None
    compacting: bool
    busy: bool
    draft_proven: bool
    # Mail counts as waiting from no earlier than this: hours the Mac slept, or agterm did not answer, are not a delay.
    counted_from: int = 0


def take_new_undelivered_tabs(
    conn: sqlite3.Connection, config: Config, at_ms: int, counted_from: int = 0
) -> list[UndeliveredTab]:
    """Tabs of leads and the assistant holding a message undelivered past the limit, each once per episode.

    The episode ends once nothing there is overdue, so the next one is told again.
    """
    leads = find_lead_conversations(conn)
    rows = conn.execute(
        f"""
        SELECT r.conversation_id, r.handle, MIN(m.id) AS oldest, MIN(m.created_at) AS oldest_at, COUNT(*) AS waiting,
               MIN(CASE WHEN r.stuck_at IS NOT NULL THEN m.id END) AS stuck,
               MAX(r.draft_told_at IS NOT NULL AND r.unproven_at IS NULL) AS draft_proven,
               (SELECT i.surface FROM instances i WHERE i.conversation_id = r.conversation_id AND i.ended_at IS NULL
                ORDER BY i.proc_start_ms DESC LIMIT 1) AS surface,
               (SELECT i.status FROM instances i WHERE i.conversation_id = r.conversation_id AND i.ended_at IS NULL
                ORDER BY i.proc_start_ms DESC LIMIT 1) AS status,
               (SELECT c.compact_phase FROM conversations c WHERE c.conversation_id = r.conversation_id) AS phase
        FROM message_recipients r JOIN messages m ON m.id = r.message_id
        WHERE {_PENDING_SQL} AND max(m.created_at, ?) <= ?
        GROUP BY r.conversation_id
        """,
        (counted_from, at_ms - UNDELIVERED_ALERT_AFTER_MS),
    ).fetchall()
    current: dict[str, UndeliveredTab] = {}
    for row in rows:
        conversation = str(row["conversation_id"])
        if conversation not in leads and row["handle"] != config.assistant_handle:
            continue
        if row["surface"] is None or is_conversation_locked(conn, config, conversation):
            continue
        tab = conn.execute(
            "SELECT agterm_session, tab_name FROM surfaces WHERE surface = ?", (row["surface"],)
        ).fetchone()
        current[conversation] = UndeliveredTab(
            conversation_id=conversation,
            handle=str(row["handle"]),
            agterm_session=tab["agterm_session"] if tab else None,
            surface=str(row["surface"]),
            tab_name=tab["tab_name"] if tab else None,
            oldest_message_id=int(row["oldest"]),
            oldest_at=int(row["oldest_at"]),
            waiting=int(row["waiting"]),
            stuck_message_id=int(row["stuck"]) if row["stuck"] is not None else None,
            compacting=bool(row["phase"]),
            busy=row["status"] == "active",
            draft_proven=bool(row["draft_proven"]),
            counted_from=counted_from,
        )
    known = {str(r[0]): r[1] for r in conn.execute("SELECT conversation_id, told_at FROM undelivered_tabs")}
    for gone in known.keys() - current.keys():
        conn.execute("DELETE FROM undelivered_tabs WHERE conversation_id = ?", (gone,))
    for tab in current.values():
        if tab.conversation_id not in known:
            conn.execute(
                "INSERT INTO undelivered_tabs (conversation_id, handle, surface, oldest_message_id, started_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (tab.conversation_id, tab.handle, tab.surface, tab.oldest_message_id, at_ms),
            )
    # Untold ones come back, so an alert that could not be sent is tried on the next pass.
    return [tab for conversation, tab in current.items() if known.get(conversation) is None]


def convert_screen_to_hold_reason(
    tab: UndeliveredTab, screen: str | None, unread_reason: str = TAB_NOT_FOUND
) -> str | None:
    """Why the tab holds its mail, as far as bridge can tell; None for text in its box that no probe proved real.

    Claude Code's grey suggestion reads as plain text, so only text a delivery's probe proved a draft is named.
    """
    box = map_screen_to_draft(screen) if screen is not None else None
    if screen is not None and box and not box.startswith(BRIDGE_TEXT_PREFIX) and not map_screen_to_busy(screen):
        if not tab.draft_proven:
            return None
        preview = convert_text_to_preview(box, BOX_PREVIEW_CHARS)
        return f"Its input box holds a draft ({preview!r}), which bridge never clears. Later messages wait behind it"
    if tab.compacting:
        return "It is compacting"
    if screen is None:
        return unread_reason
    if box and box.startswith(BRIDGE_TEXT_PREFIX):
        number = re.search(r"#(\d+)", box)
        which = f" of #{number.group(1)}" if number else ""
        return (
            f"Its input box holds bridge's own text{which}, typed but not submitted: press Enter there. "
            "Later messages wait behind it"
        )
    if tab.busy or map_screen_to_busy(screen):
        return "It is busy in a turn"
    if box is None:
        return "Bridge cannot read its input box"
    return "It looks idle with an empty input box, so bridge should have typed them: check the tab"


def tell_of_undelivered_tab(
    conn: sqlite3.Connection,
    config: Config,
    tab: UndeliveredTab,
    screen: str | None,
    at_ms: int,
    unread_reason: str = TAB_NOT_FOUND,
) -> tuple[int, Recipient] | None:
    """The assistant hears of a lead's tab; the assistant's own tab shows on the user's page instead.

    None, and the episode stays untold, when the mail arrived while the screen was read, the box holds unproven text
    or the assistant is away.
    """
    reason = convert_screen_to_hold_reason(tab, screen, unread_reason)
    if reason is None:
        return None
    still_pending = conn.execute(
        f"""
        SELECT 1 FROM message_recipients r JOIN messages m ON m.id = r.message_id
        WHERE r.conversation_id = ? AND max(m.created_at, ?) <= ? AND {_PENDING_SQL}
        """,
        (tab.conversation_id, tab.counted_from, at_ms - UNDELIVERED_ALERT_AFTER_MS),
    ).fetchone()
    if still_pending is None:
        return None
    assistant: Recipient | None = None
    if tab.handle != config.assistant_handle:
        try:
            assistant = resolve_handle(conn, config.assistant_handle)
        except AddressError:
            return None
    name = f"{tab.handle}'s tab" + (f" ({tab.tab_name})" if tab.tab_name else "")
    minutes = (at_ms - max(tab.oldest_at, tab.counted_from)) // 60_000
    since = " since the Mac woke or agterm answered again" if tab.counted_from > tab.oldest_at else ""
    more = f" and {tab.waiting - 1} more" if tab.waiting > 1 else ""
    stuck = (
        f" #{tab.stuck_message_id} was typed there but its Enter did not take."
        if tab.stuck_message_id is not None
        else ""
    )
    detail = f"{name} has not received #{tab.oldest_message_id}{more} for {minutes} min{since}.{stuck} {reason}."
    conn.execute(
        "UPDATE undelivered_tabs SET detail = ?, told_at = ? WHERE conversation_id = ?",
        (detail, at_ms, tab.conversation_id),
    )
    if assistant is None:
        return None
    result = send_message(conn, Party(SYSTEM_CONVERSATION, "bridge"), [assistant], "info", detail, at_ms)
    conn.execute("UPDATE messages SET post_type = ? WHERE id = ?", (TO_ASSISTANT, result.message_id))
    return result.message_id, assistant


def take_delivery_retries(conn: sqlite3.Connection, at_ms: int) -> list[tuple[int, Recipient]]:
    rows = conn.execute(
        """
        SELECT r.message_id, r.conversation_id, r.handle FROM message_recipients r
        JOIN messages m ON m.id = r.message_id AND m.cancelled_at IS NULL
        WHERE r.delivery_failed_at IS NOT NULL AND r.acked_at IS NULL AND r.delivery_retried_at < ?
        """,
        (at_ms - DELIVERY_RETRY_EVERY_MS,),
    ).fetchall()
    retries: list[tuple[int, Recipient]] = []
    for row in rows:
        conn.execute(
            "UPDATE message_recipients SET delivery_retried_at = ? WHERE message_id = ? AND conversation_id = ?",
            (at_ms, row["message_id"], row["conversation_id"]),
        )
        retries.append((int(row["message_id"]), resolve_tab(conn, str(row["conversation_id"]), str(row["handle"]))))
    return retries


@contextmanager
def hold_typer_lock(message_id: int, conversation_id: str) -> Generator[bool]:
    """True for the one typer of this message to this conversation; a retry, a nag or a hook's spawn meeting a live
    one gets False and must leave at once, or two type the same text into one box."""
    lock_dir = state_dir() / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / f"deliver-{message_id}-{conversation_id}.lock"
    while True:
        with path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                current = os.stat(path).st_ino
            except FileNotFoundError:
                current = None
            # Pruned between the open and the lock: a newer file at the path is the one that counts.
            if current != os.fstat(lock.fileno()).st_ino:
                continue
            # Its age is its last holder's, for prune_typer_locks.
            os.utime(path)
            yield True
            return


def prune_typer_locks(older_than_s: float = TYPER_LOCK_KEPT_S) -> None:
    """Typer lock files nobody took for a day go; one held now stays, as its typer still counts on it."""
    cutoff = time.time() - older_than_s
    lock_dir = state_dir() / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    # One pruner at a time: a second one could lock a file the first already replaced, and unlink its replacement.
    with (lock_dir / "prune-typer-locks.lock").open("a") as pruning:
        try:
            fcntl.flock(pruning, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        for path in lock_dir.glob("deliver-*.lock"):
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
                with path.open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    if os.stat(path).st_ino == os.fstat(lock.fileno()).st_ino:
                        path.unlink()
            except OSError:
                continue


def spawn_typed_delivery(message_id: int, recipients: list[Recipient]) -> None:
    """Detached per recipient: each one waits for its own tab to be idle."""
    for recipient in recipients:
        if recipient.conversation_id != USER_CONVERSATION:
            _spawn_typer(message_id, recipient.conversation_id)


def _spawn_typer(message_id: int, conversation_id: str) -> None:
    subprocess.Popen(
        [sys.executable, "-m", "bridge", "_deliver", str(message_id), conversation_id],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def queue_retry_without_db(message_id: int, conversation_id: str) -> None:
    """For a typer the database stayed locked on for minutes, so not even its failure is recorded: serve's next pass
    starts another, as the retry pass would."""
    directory = state_dir() / "typer-retries"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{message_id}-{conversation_id}").touch()


def spawn_queued_retries() -> None:
    for path in (state_dir() / "typer-retries").glob("*-*"):
        message, _, conversation = path.name.partition("-")
        if not message.isdigit():
            path.unlink(missing_ok=True)
            continue
        # Taken off only once its typer runs; a second spawn of it meets the typer lock and leaves.
        try:
            _spawn_typer(int(message), conversation)
            path.unlink(missing_ok=True)
        except OSError:
            continue
