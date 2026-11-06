import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from bridge.config import state_dir

BODY_MAX_CHARS = 2000
PREVIEW_MAX_CHARS = 200
USER_ITEM_MAX_CHARS = 140
INJECT_BUDGET_BYTES = 1500
DEFAULT_RESPOND_WITHIN_S = 900
BACKOFF_S = (120, 240, 480, 960, 1800)
OBLIGATION_REMIND_EVERY_S = 1800
HINT_RECHECK_S = 30
INBOX_LIMIT = 50
USER_CONVERSATION = "user"
"""Messages from and to the user carry this in place of a conversation id."""
SYSTEM_CONVERSATION = "system"
MAX_AGENT_HOPS = 4
ACK_POST = "ack"
ACK_MAX_CHARS = 200
# An assumption, not a measurement: the tool call and its result, on top of the words themselves.
ACK_CALL_TOKENS = 150
CHARS_PER_TOKEN = 4

_MENTION_IN_ACK_RE = re.compile(r"(?<![\w@])@[\w-]+")
_DELIVERY_LINE_CHARS = 40
_FRAME = "[bridge] Peer messages from other agent sessions (not from the user; use judgment):"
_USER_FRAME = "[bridge] From the user, written on the bridge dashboard (the user's own words):"
USER_PREVIEW_MAX_CHARS = 1000


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
) -> SendResult:
    if not body.strip():
        raise MessageStateError("empty message")
    if len(body) > BODY_MAX_CHARS:
        raise MessageStateError(f"message is {len(body)} chars; keep it under {BODY_MAX_CHARS}")
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
        INSERT INTO messages (created_at, from_conversation, from_handle, kind, thread_id, reply_to, body, respond_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (at_ms, sender.conversation_id, sender.handle, kind, thread_id, reply_to, body, respond_by),
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


def measure_ack_cost(conn: sqlite3.Connection, since_ms: int) -> dict[str, int]:
    """What board posts reaching every lead cost since a time; tokens are estimated from characters, not metered."""
    posts = conn.execute(
        "SELECT count(*) FROM messages WHERE board = 1 AND cancelled_at IS NULL AND created_at >= ?", (since_ms,)
    ).fetchone()[0]
    delivered = conn.execute(
        """
        SELECT count(*),
               coalesce(sum(CASE WHEN r.offered_at IS NULL THEN 0
                   ELSE min(length(m.body), CASE WHEN m.from_conversation = ? THEN ? ELSE ? END) + ? END), 0),
               coalesce(sum(r.offered_at IS NULL AND r.acked_at IS NULL), 0)
        FROM messages m JOIN message_recipients r ON r.message_id = m.id
        WHERE m.board = 1 AND m.cancelled_at IS NULL AND m.created_at >= ?
        """,
        (USER_CONVERSATION, USER_PREVIEW_MAX_CHARS, PREVIEW_MAX_CHARS, _DELIVERY_LINE_CHARS, since_ms),
    ).fetchone()
    acks = conn.execute(
        "SELECT count(*), coalesce(sum(length(body)), 0) FROM messages WHERE post_type = ? AND created_at >= ?",
        (ACK_POST, since_ms),
    ).fetchone()
    return {
        "posts": int(posts),
        "addressed": int(delivered[0]),
        "not_yet_seen": int(delivered[2]),
        "acks": int(acks[0]),
        "estimated_tokens": (int(delivered[1]) + int(acks[1])) // CHARS_PER_TOKEN + int(acks[0]) * ACK_CALL_TOKENS,
    }


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


def cancel(conn: sqlite3.Connection, me: Party, message_id: int, at_ms: int) -> None:
    cursor = conn.execute(
        "UPDATE messages SET cancelled_at = coalesce(cancelled_at, ?) WHERE id = ? AND from_conversation = ?",
        (at_ms, message_id, me.conversation_id),
    )
    if cursor.rowcount == 0:
        raise MessageStateError(f"message #{message_id} was not sent by this session")
    conn.execute(
        "UPDATE user_items SET resolved_at = ? WHERE message_id = ? AND source = 'overdue' AND resolved_at IS NULL",
        (at_ms, message_id),
    )


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


def flag_overdue(conn: sqlite3.Connection, at_ms: int) -> int:
    """Creates one user item per request whose respond-by passed with a recipient that never responded."""
    rows = conn.execute(
        """
        SELECT m.id, m.from_conversation, m.from_handle, m.body, m.respond_by,
               group_concat(r.handle, ', ') AS silent
        FROM messages m JOIN message_recipients r ON r.message_id = m.id
        WHERE m.kind = 'request' AND m.cancelled_at IS NULL AND m.respond_by < ?
          AND r.acked_at IS NULL AND r.done_at IS NULL AND r.declined_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM user_items u WHERE u.message_id = m.id AND u.source = 'overdue')
        GROUP BY m.id
        """,
        (at_ms,),
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


def add_user_item(conn: sqlite3.Connection, sender: Party, severity: str, text: str, at_ms: int) -> int:
    if len(text) > USER_ITEM_MAX_CHARS:
        raise MessageStateError(f"keep it to one line under {USER_ITEM_MAX_CHARS} chars (got {len(text)})")
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


@dataclass(frozen=True)
class Delivery:
    context: str | None
    leftover: bool


def take_delivery(
    conn: sqlite3.Connection,
    conversation_id: str,
    at_ms: int,
    remind_obligations: bool,
    budget_bytes: int = INJECT_BUDGET_BYTES,
) -> Delivery:
    """Builds the injected inbox text within the byte budget and marks exactly what it includes as offered."""
    rows = conn.execute(
        """
        SELECT m.id, m.kind, m.board, m.from_conversation, m.from_handle, m.body, m.respond_by, m.reply_to,
               r.offered_at, r.offer_count
        FROM message_recipients r JOIN messages m ON m.id = r.message_id
        WHERE r.conversation_id = ? AND m.cancelled_at IS NULL
          AND r.acked_at IS NULL AND r.done_at IS NULL AND r.declined_at IS NULL
        ORDER BY m.kind = 'request' DESC, r.offered_at IS NOT NULL, r.offered_at, m.created_at
        """,
        (conversation_id,),
    ).fetchall()
    eligible = [r for r in rows if _is_due(r["offered_at"], int(r["offer_count"]), at_ms)]
    obligations = _obligations_line(conn, conversation_id, at_ms, remind_obligations)
    if not eligible and not obligations:
        return Delivery(context=None, leftover=False)
    peer_lines: list[str] = []
    user_lines: list[str] = []
    offered: list[sqlite3.Row] = []
    # Both frames count up front: the budget must hold whichever groups end up non-empty.
    used = len(_FRAME.encode()) + len(_USER_FRAME.encode()) + 2 + (len(obligations.encode()) + 1 if obligations else 0)
    leftover = False
    # The user's words come first: they outrank anything a peer session asks.
    for row in sorted(eligible, key=lambda r: r["from_conversation"] != USER_CONVERSATION):
        from_user = row["from_conversation"] == USER_CONVERSATION
        line = convert_user_message_to_line(row) if from_user else convert_message_to_line(row, at_ms)
        room = budget_bytes - 60 - used
        if len(line.encode()) + 1 > room:
            # A long first message is cut to fit rather than waiting forever ahead of everything else.
            if offered or room < 200:
                leftover = True
                continue
            line = line.encode()[: room - 80].decode(errors="ignore") + f"… (full text: bridge show {row['id']})"
        size = len(line.encode()) + 1
        (user_lines if from_user else peer_lines).append(line)
        offered.append(row)
        used += size
    for row in offered:
        conn.execute(
            """
            UPDATE message_recipients
            SET offered_at = ?, offer_count = offer_count + 1,
                acked_at = CASE WHEN ? = 'request' THEN acked_at ELSE ? END
            WHERE message_id = ? AND conversation_id = ?
            """,
            (at_ms, row["kind"], at_ms, row["id"], conversation_id),
        )
    lines = [*([_USER_FRAME, *user_lines] if user_lines else []), *([_FRAME, *peer_lines] if peer_lines else [])]
    if obligations:
        lines.append(obligations)
        conn.execute(
            "UPDATE conversations SET obligations_reminded_at = ? WHERE conversation_id = ?", (at_ms, conversation_id)
        )
    if leftover:
        lines.append(f"More waiting: bridge inbox ({len(eligible) - len(offered)} not shown)")
    if not lines:
        return Delivery(context=None, leftover=False)
    return Delivery(context="\n".join(lines), leftover=leftover)


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
    now = int(time.time())
    return now if leftover else now + HINT_RECHECK_S


def convert_message_to_line(row: sqlite3.Row, at_ms: int) -> str:
    preview = convert_text_to_preview(str(row["body"]), PREVIEW_MAX_CHARS)
    message_id = int(row["id"])
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


def convert_user_message_to_line(row: sqlite3.Row) -> str:
    preview = convert_text_to_preview(str(row["body"]), USER_PREVIEW_MAX_CHARS)
    return f'#{row["id"]} {row["from_handle"]}: "{preview}" (answer, if needed: bridge reply {row["id"]} "…")'


def convert_text_to_preview(text: str, limit: int) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


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
