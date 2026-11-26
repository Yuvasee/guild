import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from bridge.config import state_dir
from bridge.inject import Part, save_later_parts, take_later_parts

BODY_MAX_CHARS = 2000
USER_WORDS_MAX_CHARS = 8000
"""What the owner types on the dashboard: an answer, a reply, a chat post or a note."""
USER_BODY_MAX_CHARS = USER_WORDS_MAX_CHARS + 2000
"""A message sent as the owner: their words plus the lines that say what they answer, such as a lead's plan."""
USER_PART_MAX_CHARS = 4000
"""Two parts carry the longest words; each fits beside a hook's other context."""
DELIVERED_BODY_MAX_CHARS = 2000
USER_ITEM_MAX_CHARS = 140
INJECT_BUDGET_BYTES = 1500
DEFAULT_RESPOND_WITHIN_S = 900
BACKOFF_S = (120, 240, 480, 960, 1800)
OBLIGATION_REMIND_EVERY_S = 1800
HINT_RECHECK_S = 30
HINT_SENT = 0
"""A hint file's value once mail waits for its session; the rewake waiter acts on this value alone."""
INBOX_LIMIT = 50
USER_CONVERSATION = "user"
"""Messages from and to the user carry this in place of a conversation id."""
SYSTEM_CONVERSATION = "system"
MAX_AGENT_HOPS = 4
ACK_POST = "ack"
ACK_MAX_CHARS = 200

_MENTION_IN_ACK_RE = re.compile(r"(?<![\w@])@[\w-]+")
_FRAME = "[bridge] Peer messages from other agent sessions (not from the user; use judgment):"
_USER_FRAME = "[bridge] From the user, written on the bridge dashboard (the user's own words):"


class AddressError(Exception):
    pass


class MessageStateError(Exception):
    pass


@dataclass(frozen=True)
class Party:
    conversation_id: str
    handle: str


@dataclass(frozen=True)
class Recipient:
    handle: str
    conversation_id: str
    pid: int
    status: str
    peer_name: str | None
    peer_messaging: bool


@dataclass(frozen=True)
class SendResult:
    message_id: int
    recipients: list[Recipient]
    wake: list[Recipient]
    paused: bool = False


def resolve_handle(conn: sqlite3.Connection, handle: str) -> Recipient:
    rows = conn.execute(
        """
        SELECT s.handle, i.conversation_id, i.pid, i.status, i.peer_name, i.peer_messaging
        FROM surfaces s JOIN instances i ON i.surface = s.surface AND i.ended_at IS NULL
        WHERE s.handle = ?
           OR (NOT EXISTS (SELECT 1 FROM surfaces WHERE handle = ?)
               AND s.surface = (SELECT surface FROM handle_aliases WHERE handle = ?))
        """,
        (handle, handle, handle),
    ).fetchall()
    if len(rows) != 1 or rows[0]["conversation_id"] is None:
        live = [
            str(r[0])
            for r in conn.execute(
                """
                SELECT DISTINCT s.handle FROM surfaces s JOIN instances i ON i.surface = s.surface
                WHERE i.ended_at IS NULL AND s.handle IS NOT NULL ORDER BY 1
                """
            )
        ]
        reason = "no live session" if not rows else f"{len(rows)} live sessions"
        raise AddressError(f"{reason} with handle {handle!r}; live handles: {', '.join(live) or 'none'}")
    row = rows[0]
    return Recipient(
        handle=str(row["handle"]),
        conversation_id=str(row["conversation_id"]),
        pid=int(row["pid"]),
        status=str(row["status"]),
        peer_name=row["peer_name"],
        peer_messaging=bool(row["peer_messaging"]),
    )


def fit_body(text: str) -> str:
    """Cut to the message limit; send_message and add_post apply it to every notice of bridge's own, since one too
    long to send would roll back the whole step that sends it (a review poll, every poll). The cut is logged so the
    text that outgrew the limit gets fixed."""
    if len(text) <= BODY_MAX_CHARS:
        return text
    print(f"notice cut from {len(text)} to {BODY_MAX_CHARS} chars: {text[:100]!r}", flush=True)
    return text[: BODY_MAX_CHARS - 1].rstrip() + "\u2026"


def send_message(
    conn: sqlite3.Connection,
    sender: Party,
    recipients: list[Recipient],
    kind: str,
    body: str,
    at_ms: int,
    respond_within_s: int | None = None,
    reply_to: int | None = None,
    thread_id: int | None = None,
    goes_now: bool = False,
) -> SendResult:
    if not body.strip():
        raise MessageStateError("empty message")
    if sender.conversation_id == SYSTEM_CONVERSATION:
        body = fit_body(body)
    limit = USER_BODY_MAX_CHARS if sender.conversation_id == USER_CONVERSATION else BODY_MAX_CHARS
    if len(body) > limit:
        print(f"refused a {len(body)}-char message: {body[:100]!r}", flush=True)
        raise MessageStateError(f"message is {len(body)} chars; keep it under {limit}")
    from bridge.pages import find_unready_citations, map_unready_citations_to_error  # hooks never cite a page

    if unready := find_unready_citations(conn, body):
        raise MessageStateError(map_unready_citations_to_error(unready))
    if reply_to is not None:
        parent = conn.execute("SELECT thread_id FROM messages WHERE id = ?", (reply_to,)).fetchone()
        if parent is None:
            raise MessageStateError(f"no message #{reply_to}")
        thread_id = int(parent["thread_id"])
    hops = _next_agent_hops(conn, thread_id, sender)
    paused = hops > MAX_AGENT_HOPS
    if paused:
        recipients = []
    if kind == "request" and respond_within_s is None:
        respond_within_s = DEFAULT_RESPOND_WITHIN_S
    respond_by = at_ms + respond_within_s * 1000 if kind == "request" and respond_within_s else None
    cursor = conn.execute(
        """
        INSERT INTO messages (created_at, from_conversation, from_handle, kind, thread_id, reply_to, body, respond_by,
                              goes_now)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (at_ms, sender.conversation_id, sender.handle, kind, thread_id, reply_to, body, respond_by, int(goes_now)),
    )
    message_id = int(cursor.lastrowid or 0)
    if reply_to is not None:
        # Replying is a response: it acknowledges the parent for the replier if it was addressed to them.
        conn.execute(
            """
            UPDATE message_recipients SET acked_at = coalesce(acked_at, ?)
            WHERE message_id = ? AND conversation_id = ?
            """,
            (at_ms, reply_to, sender.conversation_id),
        )
        resolve_settled_overdue(conn, reply_to, at_ms)
    if thread_id is None:
        conn.execute("UPDATE messages SET thread_id = ? WHERE id = ?", (message_id, message_id))
    conn.execute("UPDATE messages SET agent_hops = ?, paused = ? WHERE id = ?", (hops, int(paused), message_id))
    wake: list[Recipient] = []
    for recipient in recipients:
        if not has_open_messages(conn, recipient.conversation_id):
            wake.append(recipient)
        conn.execute(
            "INSERT OR IGNORE INTO message_recipients (message_id, conversation_id, handle) VALUES (?, ?, ?)",
            (message_id, recipient.conversation_id, recipient.handle),
        )
    return SendResult(message_id=message_id, recipients=recipients, wake=wake, paused=paused)


def _next_agent_hops(conn: sqlite3.Connection, thread_id: int | None, sender: Party) -> int:
    """Only a thread on the team board can run away between agents; a human post starts the count again."""
    if thread_id is None or sender.conversation_id == USER_CONVERSATION:
        return 0
    root = conn.execute("SELECT board FROM messages WHERE id = ?", (thread_id,)).fetchone()
    if root is None or not root["board"]:
        return 0
    last = conn.execute(
        "SELECT agent_hops FROM messages WHERE thread_id = ? AND post_type IS NOT ? ORDER BY id DESC LIMIT 1",
        (thread_id, ACK_POST),
    ).fetchone()
    return int(last["agent_hops"]) + 1 if last else 0


def add_ack(conn: sqlite3.Connection, me: Party, lead_name: str, message_id: int, text: str, at_ms: int) -> int:
    """A lead's one line on a board post: shown in the thread, delivered to nobody, so no loop can run on it."""
    line = " ".join(text.split())
    if not line or len(line) > ACK_MAX_CHARS:
        raise MessageStateError(f"an ack is one line of 1 to {ACK_MAX_CHARS} characters")
    if "?" in line or _MENTION_IN_ACK_RE.search(line):
        raise MessageStateError("an ack reaches nobody; a question or @name goes with bridge reply or bridge board")
    post = conn.execute(
        "SELECT thread_id, board, from_conversation, from_handle, cancelled_at FROM messages WHERE id = ?",
        (message_id,),
    ).fetchone()
    if post is None:
        raise MessageStateError(f"no message #{message_id}")
    if not post["board"] or post["cancelled_at"] is not None:
        raise MessageStateError(f"#{message_id} is not a board post; only those get an ack")
    if post["from_conversation"] == me.conversation_id or post["from_handle"] == lead_name:
        raise MessageStateError("a lead does not ack its own post")
    if conn.execute(
        "SELECT 1 FROM messages WHERE reply_to = ? AND post_type = ? AND from_handle = ?",
        (message_id, ACK_POST, lead_name),
    ).fetchone():
        raise MessageStateError(f"you already acked #{message_id}")
    cursor = conn.execute(
        """
        INSERT INTO messages (created_at, from_conversation, from_handle, kind, thread_id, reply_to, body, post_type)
        VALUES (?, ?, ?, 'reply', ?, ?, ?, ?)
        """,
        (at_ms, me.conversation_id, lead_name, post["thread_id"], message_id, line, ACK_POST),
    )
    conn.execute(
        "UPDATE message_recipients SET acked_at = coalesce(acked_at, ?) WHERE message_id = ? AND conversation_id = ?",
        (at_ms, message_id, me.conversation_id),
    )
    return int(cursor.lastrowid or 0)


def has_open_messages(conn: sqlite3.Connection, conversation_id: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM message_recipients r JOIN messages m ON m.id = r.message_id
        WHERE r.conversation_id = ? AND m.cancelled_at IS NULL
          AND r.acked_at IS NULL AND r.done_at IS NULL AND r.declined_at IS NULL
        LIMIT 1
        """,
        (conversation_id,),
    ).fetchone()
    return row is not None


def respond(conn: sqlite3.Connection, me: Party, message_id: int, action: str, at_ms: int) -> None:
    """Applies ack, done or decline from the recipient side and resolves an overdue item it settles."""
    column = {"ack": "acked_at", "done": "done_at", "decline": "declined_at"}[action]
    cursor = conn.execute(
        f"""
        UPDATE message_recipients SET {column} = coalesce({column}, ?), acked_at = coalesce(acked_at, ?)
        WHERE message_id = ? AND conversation_id = ?
        """,
        (at_ms, at_ms, message_id, me.conversation_id),
    )
    if cursor.rowcount == 0:
        raise MessageStateError(f"message #{message_id} is not addressed to this session")
    resolve_settled_overdue(conn, message_id, at_ms)


def resolve_settled_overdue(conn: sqlite3.Connection, message_id: int, at_ms: int) -> None:
    conn.execute(
        """
        UPDATE user_items SET resolved_at = ?
        WHERE message_id = ? AND source = 'overdue' AND resolved_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM message_recipients
              WHERE message_id = ? AND acked_at IS NULL AND done_at IS NULL AND declined_at IS NULL
          )
        """,
        (at_ms, message_id, message_id),
    )


def flag_overdue(conn: sqlite3.Connection, at_ms: int, grace_ms: int = 0) -> int:
    """Creates one user item per request whose respond-by passed grace_ms ago with a recipient that never responded:
    with batches, the longest an idle tab's wake can wait for its batch (batch_grace_ms)."""
    rows = conn.execute(
        """
        SELECT m.id, m.from_conversation, m.from_handle, m.body, m.respond_by,
               group_concat(r.handle, ', ') AS silent
        FROM messages m JOIN message_recipients r ON r.message_id = m.id
        WHERE m.kind = 'request' AND m.cancelled_at IS NULL AND m.respond_by + ? < ?
          AND r.acked_at IS NULL AND r.done_at IS NULL AND r.declined_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM user_items u WHERE u.message_id = m.id AND u.source = 'overdue')
        GROUP BY m.id
        """,
        (grace_ms, at_ms),
    ).fetchall()
    for row in rows:
        minutes = (at_ms - int(row["respond_by"])) // 60_000
        text = convert_text_to_preview(
            f"{row['from_handle']} → {row['silent']}: no response to #{row['id']} ({minutes}m overdue): {row['body']}",
            USER_ITEM_MAX_CHARS,
        )
        conn.execute(
            """
            INSERT INTO user_items (created_at, from_conversation, from_handle, severity, text, source, message_id)
            VALUES (?, ?, ?, 'action', ?, 'overdue', ?)
            """,
            (at_ms, row["from_conversation"], row["from_handle"], text, row["id"]),
        )
    return len(rows)


def convert_item_id_to_question_ref(item_id: int) -> str:
    """How the owner names a feed post; a bare #N reads as a PR."""
    return f"Q#{item_id}"


def convert_item_id_to_answer_ref(item_id: int) -> str:
    """How the owner names a done or insight post, which is news, not a question."""
    return f"A#{item_id}"


def add_user_item(conn: sqlite3.Connection, sender: Party, severity: str, text: str, at_ms: int) -> int:
    if len(text) > USER_ITEM_MAX_CHARS:
        raise MessageStateError(f"keep it to one line under {USER_ITEM_MAX_CHARS} chars (got {len(text)})")
    from bridge.pages import find_unready_citations, map_unready_citations_to_error  # hooks never cite a page

    if unready := find_unready_citations(conn, text):
        raise MessageStateError(map_unready_citations_to_error(unready))
    cursor = conn.execute(
        """
        INSERT INTO user_items (created_at, from_conversation, from_handle, severity, text, source)
        VALUES (?, ?, ?, ?, ?, 'agent')
        """,
        (at_ms, sender.conversation_id, sender.handle, severity, " ".join(text.split())),
    )
    return int(cursor.lastrowid or 0)


def retarget_open_messages(
    conn: sqlite3.Connection, old_conversation: str, new_conversation: str, instance_id: str
) -> None:
    """After /clear the tab keeps its open mail: it was addressed to the tab, not to the old context."""
    other = conn.execute(
        "SELECT 1 FROM instances WHERE conversation_id = ? AND ended_at IS NULL AND instance_id != ?",
        (old_conversation, instance_id),
    ).fetchone()
    if other is not None:
        return
    conn.execute(
        """
        UPDATE OR IGNORE message_recipients SET conversation_id = ?
        WHERE conversation_id = ? AND done_at IS NULL AND declined_at IS NULL
        """,
        (new_conversation, old_conversation),
    )
    # Mail a hook already took for the tab and saved for its next event is open mail too.
    if waiting := take_later_parts(conn, old_conversation):
        save_later_parts(conn, new_conversation, [*waiting, *take_later_parts(conn, new_conversation)])


def is_delivery_owner(conn: sqlite3.Connection, conversation_id: str, instance_id: str) -> bool:
    """With two live runtimes on one conversation, only the newest one is offered mail."""
    row = conn.execute(
        """
        SELECT instance_id FROM instances WHERE conversation_id = ? AND ended_at IS NULL
        ORDER BY proc_start_ms DESC LIMIT 1
        """,
        (conversation_id,),
    ).fetchone()
    return row is None or row["instance_id"] == instance_id


def check_user_words(text: str) -> str:
    text = text.strip()
    if not text:
        raise MessageStateError("empty text")
    if len(text) > USER_WORDS_MAX_CHARS:
        raise MessageStateError(f"your text is {len(text):,} characters; the most is {USER_WORDS_MAX_CHARS:,}")
    return text


@dataclass(frozen=True)
class Delivery:
    context: str | None
    leftover: bool
    user_messages: tuple[tuple[int, tuple[Part, ...]], ...] = ()
    """The user's messages by id, each whole in in-order parts, never in the inbox: none overtakes an older one."""


def take_user_messages(
    conn: sqlite3.Connection, conversation_id: str, at_ms: int
) -> tuple[tuple[int, tuple[Part, ...]], ...]:
    """The user's unread messages to this conversation by id, each whole in parts, marked offered."""
    rows = sorted(_list_due_mail(conn, conversation_id, at_ms, from_user=True), key=lambda r: r["id"])
    _mark_offered(conn, conversation_id, rows, at_ms)
    return tuple(
        (int(r["id"]), tuple(convert_user_message_to_parts(int(r["id"]), str(r["from_handle"]), str(r["body"]))))
        for r in rows
    )


def _list_due_mail(conn: sqlite3.Connection, conversation_id: str, at_ms: int, from_user: bool) -> list[sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT m.id, m.kind, m.board, m.from_conversation, m.from_handle, m.body, m.respond_by, m.reply_to,
               r.offered_at, r.offer_count, r.filter_held_at
        FROM message_recipients r JOIN messages m ON m.id = r.message_id
        WHERE r.conversation_id = ? AND m.cancelled_at IS NULL AND (m.from_conversation = ?) = ?
          AND r.acked_at IS NULL AND r.done_at IS NULL AND r.declined_at IS NULL
          -- A live typer owns its claim; offering it here too would let the typer's release undo this offer.
          AND r.typing_started_at IS NULL
        ORDER BY m.kind = 'request' DESC, r.offered_at IS NOT NULL, r.offered_at, m.created_at
        """,
        (conversation_id, USER_CONVERSATION, from_user),
    ).fetchall()
    return [r for r in rows if _is_due(r["offered_at"], int(r["offer_count"]), at_ms)]


def _mark_offered(conn: sqlite3.Connection, conversation_id: str, rows: list[sqlite3.Row], at_ms: int) -> None:
    for row in rows:
        conn.execute(
            """
            UPDATE message_recipients
            SET offered_at = ?, offer_count = offer_count + 1,
                acked_at = CASE WHEN ? = 'request' THEN acked_at ELSE ? END
            WHERE message_id = ? AND conversation_id = ?
            """,
            (at_ms, row["kind"], at_ms, row["id"], conversation_id),
        )


def mark_user_parts_shown(conn: sqlite3.Connection, conversation_id: str, parts: list[Part], at_ms: int) -> None:
    """A message's offer is dated by its last part shown, so a failed request that carried any part of it gives the
    whole message again (reopen_unread_messages)."""
    for message_id in {p.message_id for p in parts if p.message_id}:
        conn.execute(
            """
            UPDATE message_recipients SET offered_at = ?1, acked_at = CASE WHEN acked_at IS NULL THEN NULL ELSE ?1 END
            WHERE message_id = ?2 AND conversation_id = ?3 AND done_at IS NULL AND declined_at IS NULL
            """,
            (at_ms, message_id, conversation_id),
        )


def merge_user_messages(*groups: tuple[tuple[int, tuple[Part, ...]], ...]) -> list[Part]:
    """The parts of the user's messages, oldest message first."""
    return [part for _, parts in sorted((m for group in groups for m in group), key=lambda m: m[0]) for part in parts]


def split_user_parts(parts: list[Part]) -> tuple[list[Part], tuple[tuple[int, tuple[Part, ...]], ...]]:
    """The parts of the user's messages, by message, out of the rest: they join the user's newer and given-again
    messages by id, so a message given again after compaction keeps its place before newer ones still waiting."""
    by_message: dict[int, list[Part]] = {}
    for part in parts:
        if part.message_id:
            by_message.setdefault(part.message_id, []).append(part)
    return [p for p in parts if not p.message_id], tuple((i, tuple(ps)) for i, ps in by_message.items())


def take_delivery(
    conn: sqlite3.Connection,
    conversation_id: str,
    at_ms: int,
    remind_obligations: bool,
    budget_bytes: int = INJECT_BUDGET_BYTES,
) -> Delivery:
    """Builds the injected inbox text within the byte budget and marks exactly what it includes as offered."""
    # The user's words are never cut or budgeted: the caller packs them first, and what does not fit waits.
    user_messages = take_user_messages(conn, conversation_id, at_ms)
    eligible = _list_due_mail(conn, conversation_id, at_ms, from_user=False)
    obligations = _obligations_line(conn, conversation_id, at_ms, remind_obligations)
    if not eligible and not obligations:
        return Delivery(context=None, leftover=False, user_messages=user_messages)
    peer_lines: list[str] = []
    held_lines: list[str] = []
    offered: list[sqlite3.Row] = []
    used = len(_FRAME.encode()) + 1 + (len(obligations.encode()) + 1 if obligations else 0)
    if any(r["filter_held_at"] is not None for r in eligible):
        used += len(_convert_count_to_held_header(len(eligible)).encode()) + 1
    leftover = False
    # What the lead filter held goes last, so it never crowds out the message that woke the tab.
    for row in sorted(eligible, key=lambda r: r["filter_held_at"] is not None):
        line = convert_message_to_line(row, at_ms)
        room = budget_bytes - 60 - used
        if len(line.encode()) + 1 > room:
            # A long first message is cut to fit rather than waiting forever ahead of everything else.
            if offered or room < 200:
                leftover = True
                continue
            line = line.encode()[: room - 80].decode(errors="ignore") + f"… (full text: bridge show {row['id']})"
        size = len(line.encode()) + 1
        (held_lines if row["filter_held_at"] is not None else peer_lines).append(line)
        offered.append(row)
        used += size
    _mark_offered(conn, conversation_id, offered, at_ms)
    if held_lines:
        # Held by the lead filter until now; they come as one group, oldest first, after what woke the tab.
        peer_lines += [_convert_count_to_held_header(len(held_lines)), *held_lines]
    lines = [_FRAME, *peer_lines] if peer_lines else []
    if obligations:
        lines.append(obligations)
        conn.execute(
            "UPDATE conversations SET obligations_reminded_at = ? WHERE conversation_id = ?", (at_ms, conversation_id)
        )
    if leftover:
        lines.append(f"More waiting: bridge inbox ({len(eligible) - len(offered)} not shown)")
    return Delivery(context="\n".join(lines) if lines else None, leftover=leftover, user_messages=user_messages)


def convert_user_message_to_parts(message_id: int, handle: str, body: str) -> list[Part]:
    """The user's words whole, as typed, in numbered parts when one would not fit a hook's message."""
    pieces = split_text_to_parts(body.strip(), USER_PART_MAX_CHARS)
    total = len(pieces)
    parts: list[Part] = []
    for number, piece in enumerate(pieces, 1):
        which = f", part {number} of {total}" if total > 1 else ""
        tail = f'\n(answer, if needed: bridge reply {message_id} "…")' if number == total else ""
        parts.append(
            Part(
                f"{_USER_FRAME}\n#{message_id} {handle}{which}:\n{piece}{tail}",
                f"part {number} of {total} of {handle}'s message #{message_id}",
                f"bridge show {message_id}",
                in_order=True,
                message_id=message_id,
            )
        )
    return parts


def split_text_to_parts(text: str, limit: int) -> list[str]:
    """Pieces of at most limit characters, each ending at a space or line break when one falls in its second half."""
    pieces: list[str] = []
    while len(text) > limit:
        cut = max(text.rfind(" ", 0, limit), text.rfind("\n", 0, limit))
        if cut < limit // 2:
            cut = limit
        pieces.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return [*pieces, text]


def _convert_count_to_held_header(count: int) -> str:
    return f"{count} notice{'' if count == 1 else 's'} that waited:"


def list_inbox(conn: sqlite3.Connection, conversation_id: str, include_closed: bool) -> list[sqlite3.Row]:
    # Open = not yet acknowledged, or a request acknowledged but not closed; oldest first so nothing starves.
    open_only = """
        AND m.cancelled_at IS NULL AND r.done_at IS NULL AND r.declined_at IS NULL
        AND (r.acked_at IS NULL OR m.kind = 'request')
    """
    order = "m.created_at DESC" if include_closed else "m.created_at"
    return conn.execute(
        f"""
        SELECT m.*, r.offered_at, r.acked_at, r.done_at, r.declined_at
        FROM message_recipients r JOIN messages m ON m.id = r.message_id
        WHERE r.conversation_id = ? {"" if include_closed else open_only}
        ORDER BY {order} LIMIT {INBOX_LIMIT}
        """,
        (conversation_id,),
    ).fetchall()


def list_board(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT m.*, (SELECT count(*) FROM messages r WHERE r.thread_id = m.id AND r.id != m.id) AS replies
        FROM messages m WHERE m.board = 1 AND m.cancelled_at IS NULL ORDER BY m.id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()


def mark_seen_in_inbox(conn: sqlite3.Connection, conversation_id: str, message_ids: list[int], at_ms: int) -> None:
    """Showing informational mail delivers it; requests still need an explicit response."""
    conn.executemany(
        """
        UPDATE message_recipients SET acked_at = ?, offered_at = coalesce(offered_at, ?)
        WHERE conversation_id = ? AND message_id = ? AND acked_at IS NULL
          AND message_id IN (SELECT id FROM messages WHERE kind != 'request')
        """,
        [(at_ms, at_ms, conversation_id, message_id) for message_id in message_ids],
    )


def write_hint(pid: int, due_epoch_s: int) -> None:
    directory = hints_dir()
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / f".{pid}.{os.getpid()}.{time.time_ns()}.tmp"
    tmp.write_text(f"{due_epoch_s}\n")
    tmp.replace(directory / str(pid))


def hints_dir() -> Path:
    return state_dir() / "hints"


def next_hint_epoch(leftover: bool) -> int:
    """HINT_SENT for what waits: due for the next tool hook, and a call to an idle tab's rewake waiter."""
    return HINT_SENT if leftover else int(time.time()) + HINT_RECHECK_S


def convert_message_to_line(row: sqlite3.Row, at_ms: int) -> str:
    message_id = int(row["id"])
    preview = convert_text_to_preview(
        str(row["body"]), DELIVERED_BODY_MAX_CHARS, f"… (full text: bridge show {message_id})"
    )
    if row["kind"] == "request":
        due = ""
        if row["respond_by"] is not None:
            left_min = (int(row["respond_by"]) - at_ms) // 60_000
            due = f", respond within {left_min}m" if left_min >= 0 else f", {-left_min}m overdue"
        return (
            f'#{message_id} REQUEST from {row["from_handle"]}{due}: "{preview}" '
            f'→ bridge ack {message_id} | bridge reply {message_id} "…" [--done] | bridge decline {message_id}'
        )
    label = f"reply to #{row['reply_to']}" if row["kind"] == "reply" else "board post" if row["board"] else "info"
    return f'#{message_id} {label} from {row["from_handle"]}: "{preview}"'


def convert_text_to_preview(text: str, limit: int, cut_mark: str = "…") -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - len(cut_mark)] + cut_mark


def _is_due(offered_at: int | None, offer_count: int, at_ms: int) -> bool:
    if offered_at is None:
        return True
    backoff = BACKOFF_S[min(offer_count - 1, len(BACKOFF_S) - 1)] if offer_count else 0
    return at_ms - int(offered_at) >= backoff * 1000


def _obligations_line(conn: sqlite3.Connection, conversation_id: str, at_ms: int, force: bool) -> str | None:
    if not force:
        row = conn.execute(
            "SELECT obligations_reminded_at FROM conversations WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()
        last = row["obligations_reminded_at"] if row else None
        if last is not None and at_ms - int(last) < OBLIGATION_REMIND_EVERY_S * 1000:
            return None
    open_rows = conn.execute(
        """
        SELECT m.id, m.from_handle FROM message_recipients r JOIN messages m ON m.id = r.message_id
        WHERE r.conversation_id = ? AND m.kind = 'request' AND m.cancelled_at IS NULL
          AND r.acked_at IS NOT NULL AND r.done_at IS NULL AND r.declined_at IS NULL
        ORDER BY m.created_at LIMIT 6
        """,
        (conversation_id,),
    ).fetchall()
    if not open_rows:
        return None
    items = ", ".join(f"#{r['id']} from {str(r['from_handle'])[:40]}" for r in open_rows)
    return f'Open requests you acknowledged: {items}. Close each with bridge done N or bridge reply N "…" --done.'
