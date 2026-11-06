import argparse
import sqlite3
import sys
from datetime import datetime

from bridge.board import (
    DEFAULT_RESPOND_WITHIN_S,
    MAX_AGENT_HOPS,
    USER_CONVERSATION,
    AddressError,
    MessageStateError,
    Party,
    Recipient,
    cancel,
    flag_overdue,
    list_board,
    list_inbox,
    mark_seen_in_inbox,
    resolve_handle,
    respond,
    send_message,
    write_hint,
)
from bridge.config import Config
from bridge.db import now_ms, transaction
from bridge.errors import UsageError
from bridge.feed import (
    FEED_KINDS,
    LEAD_TO_WORKER,
    OPEN_KINDS,
    POST_TYPES,
    TO_ASSISTANT,
    TO_LEAD,
    add_post,
    find_leads_to_wake,
    map_arg_to_link,
    post_to_board,
    reject_locked_recipients,
    resolve_tab,
    spawn_typed_delivery,
    withdraw_post,
)
from bridge.identity import current_party
from bridge.leads import find_lead_conversations, is_conversation_locked, is_lead_tab
from bridge.registry import reconcile
from bridge.watcher import (
    find_assistant_recipients,
    find_conversation_tab,
    find_lead_recipients,
    find_own_workers,
    read_tree,
)

USER_TARGET = "user"
POST_RESOLVE = "resolve"


def add_board_commands(sub: "argparse._SubParsersAction[argparse.ArgumentParser]", config: Config) -> None:  # pyright: ignore[reportPrivateUsage]
    p = sub.add_parser("send", help="message other sessions by handle (comma-separated)")
    p.add_argument("to", help="handle[,handle…]")
    p.add_argument("text")
    p.add_argument("--request", action="store_true", help="expects a response (ack/reply/done/decline)")
    p.add_argument("--respond-within", type=convert_duration_to_seconds, default=DEFAULT_RESPOND_WITHIN_S)
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("reply", help="reply to a message")
    p.add_argument("id", type=int)
    p.add_argument("text")
    p.add_argument("--done", action="store_true", help="also close the request")
    p.set_defaults(func=cmd_reply)

    for action, text in (
        ("ack", "seen, working on it"),
        ("done", "request handled"),
        ("decline", "will not do it"),
    ):
        p = sub.add_parser(action, help=f"mark message(s): {text}")
        p.add_argument("ids", type=int, nargs="+")
        p.set_defaults(func=cmd_respond, action=action)

    p = sub.add_parser("cancel", help="withdraw a message you sent")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("inbox", help="messages for this session")
    p.add_argument("--all", action="store_true", help="include closed ones")
    p.set_defaults(func=cmd_inbox)

    p = sub.add_parser("show", help="a message with its thread")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser(
        "post", help="a post in the user's feed: blocked, decision, done or insight; `post resolve N` withdraws yours"
    )
    p.add_argument("kind", choices=[*sorted(FEED_KINDS), POST_RESOLVE])
    p.add_argument("title", help="one line; the dashboard shows it first (with resolve: the post's number)")
    p.add_argument("--body", default="", help="details; '-' reads stdin")
    p.add_argument("--link", action="append", default=[], help="label=https://… (repeatable)")
    p.add_argument("--option", action="append", default=[], help="an answer button (repeatable; blocked/decision)")
    p.add_argument(
        "--urgent",
        action="store_true",
        help="blocked/decision only, when prod or a customer is hurting, money is leaking, or a deadline is today",
    )
    p.set_defaults(func=cmd_post)

    aliases = [f"tell-{config.user_alias}"] if config.user_alias else []
    p = sub.add_parser("tell-user", aliases=aliases, help="one line for the user's feed (older form of post)")
    p.add_argument("text")
    severity = p.add_mutually_exclusive_group()
    severity.add_argument("--action", dest="kind", action="store_const", const="decision")
    severity.add_argument("--blocker", dest="kind", action="store_const", const="blocked")
    p.set_defaults(func=cmd_tell_user, kind="insight")

    p = sub.add_parser("board", help="the team board: post to everyone (no TEXT: read recent posts)")
    p.add_argument("text", nargs="?")
    p.add_argument("--topic", help="a short tag, e.g. a repo or an incident")
    p.add_argument("--type", choices=POST_TYPES, help="what kind of post this is")
    p.add_argument("--thread", help="an issue or PR id; posts with the same id form a thread")
    p.add_argument("--link", help="the PR, issue or doc this post points to")
    p.set_defaults(func=cmd_board)


def reject_locked_sender(conn: sqlite3.Connection, config: Config, me: Party) -> None:
    if is_conversation_locked(conn, config, me.conversation_id):
        raise MessageStateError("this tab is locked, and locked tabs are left out of the team")


def cmd_send(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    me = current_party(conn)
    handles = [h.strip() for h in str(args.to).split(",") if h.strip()]
    if handles == [USER_TARGET]:
        raise UsageError("use `bridge post` for the user")
    kind = "request" if args.request else "info"
    sync_tabs_on_handle_miss(conn, config, handles)
    at = now_ms()
    try:
        # Resolving inside the transaction means a concurrent /clear cannot strand the message.
        with transaction(conn):
            reject_locked_sender(conn, config, me)
            recipients = [resolve_handle(conn, h) for h in handles]
            reject_locked_recipients(conn, config, recipients)
            if any(r.conversation_id == me.conversation_id for r in recipients):
                raise MessageStateError("you cannot message yourself")
            flag_overdue(conn, at)
            result = send_message(conn, me, recipients, kind, str(args.text), at, int(args.respond_within))
    except (MessageStateError, AddressError) as exc:
        raise UsageError(str(exc)) from exc
    nudge_recipients(recipients)
    print(f"#{result.message_id} {kind} → {', '.join(r.handle for r in recipients)}")
    print_wake_lines(conn, config, me, result.message_id, recipients, result.wake)
    return 0


def sync_tabs_on_handle_miss(conn: sqlite3.Connection, config: Config, handles: list[str]) -> None:
    """A tab opened moments ago has no handle until something reconciles, and only a miss pays for asking agterm."""
    try:
        for handle in handles:
            resolve_handle(conn, handle)
    except AddressError:
        reconcile(conn, config, now_ms())


def cmd_reply(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    me = current_party(conn)
    at = now_ms()
    row = conn.execute(
        "SELECT m.from_conversation, m.from_handle FROM messages m WHERE m.id = ?", (int(args.id),)
    ).fetchone()
    if row is None:
        raise UsageError(f"no message #{args.id}")
    try:
        with transaction(conn):
            original_sender = resolve_tab(conn, str(row["from_conversation"]), str(row["from_handle"]))
            if args.done:
                respond(conn, me, int(args.id), "done", at)
            result = send_message(conn, me, [original_sender], "reply", str(args.text), at, reply_to=int(args.id))
    except MessageStateError as exc:
        raise UsageError(str(exc)) from exc
    nudge_recipients([original_sender])
    print(f"#{result.message_id} reply → {original_sender.handle}" + (f" (closed #{args.id})" if args.done else ""))
    print_wake_lines(conn, config, me, result.message_id, result.recipients, result.wake)
    return 0


def cmd_respond(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    me = current_party(conn)
    at = now_ms()
    try:
        with transaction(conn):
            for message_id in args.ids:
                respond(conn, me, int(message_id), str(args.action), at)
    except MessageStateError as exc:
        raise UsageError(str(exc)) from exc
    print(f"{args.action}: {' '.join(f'#{i}' for i in args.ids)}")
    return 0


def cmd_cancel(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    me = current_party(conn)
    try:
        with transaction(conn):
            cancel(conn, me, int(args.id), now_ms())
    except MessageStateError as exc:
        raise UsageError(str(exc)) from exc
    print(f"cancelled #{args.id}")
    return 0


def cmd_inbox(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    me = current_party(conn)
    at = now_ms()
    with transaction(conn):
        flag_overdue(conn, at)
        rows = list_inbox(conn, me.conversation_id, bool(args.all))
        mark_seen_in_inbox(conn, me.conversation_id, [int(r["id"]) for r in rows], at)
    if not rows:
        print("inbox empty")
        return 0
    for row in rows:
        print(map_message_to_text(row))
        print()
    print('Respond: bridge ack N | bridge reply N "…" [--done] | bridge done N | bridge decline N')
    return 0


def cmd_show(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    row = conn.execute("SELECT thread_id FROM messages WHERE id = ?", (int(args.id),)).fetchone()
    if row is None:
        raise UsageError(f"no message #{args.id}")
    thread = conn.execute(
        """
        SELECT m.*, group_concat(r.handle, ', ') AS recipients FROM messages m
        LEFT JOIN message_recipients r ON r.message_id = m.id
        WHERE m.thread_id = ? GROUP BY m.id ORDER BY m.id
        """,
        (row["thread_id"],),
    ).fetchall()
    for message in thread:
        print(map_message_to_text(message, show_recipients=True))
        print()
    return 0


def cmd_post(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    me = current_party(conn)
    if args.kind == POST_RESOLVE:
        return _withdraw_post(conn, me, str(args.title))
    if args.kind in OPEN_KINDS and not args.body and me.conversation_id in find_lead_conversations(conn):
        raise UsageError("a question from a lead needs --body: what it is, why now, what each answer costs")
    body = sys.stdin.read() if args.body == "-" else str(args.body)
    try:
        links = [map_arg_to_link(str(link)) for link in args.link]
        with transaction(conn):
            item_id = add_post(
                conn, me, str(args.kind), str(args.title), body, links, list(args.option), now_ms(), bool(args.urgent)
            )
    except MessageStateError as exc:
        raise UsageError(str(exc)) from exc
    waits = " until the user answers; the answer comes back to this session" if args.kind in OPEN_KINDS else ""
    urgent = ", urgent" if args.urgent else ""
    print(f"post #{item_id} ({args.kind}{urgent}) is in the user's feed{waits}")
    return 0


def _withdraw_post(conn: sqlite3.Connection, me: Party, text: str) -> int:
    if not text.isdecimal():
        raise UsageError("bridge post resolve N: N is the post's number")
    try:
        with transaction(conn):
            withdraw_post(conn, me, int(text), now_ms())
    except MessageStateError as exc:
        raise UsageError(str(exc)) from exc
    print(f"post #{text} withdrawn")
    return 0


def cmd_tell_user(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    me = current_party(conn)
    try:
        with transaction(conn):
            item_id = add_post(conn, me, str(args.kind), str(args.text), "", [], [], now_ms())
    except MessageStateError as exc:
        raise UsageError(str(exc)) from exc
    print(f"post #{item_id} ({args.kind}) is in the user's feed")
    return 0


def cmd_board(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    me = current_party(conn)
    at = now_ms()
    if args.text:
        try:
            with transaction(conn):
                reject_locked_sender(conn, config, me)
                result = post_to_board(
                    conn, me, str(args.text), args.topic, at, config, args.type, args.thread, args.link
                )
                wake = [] if result.paused else find_leads_to_wake(conn, config, str(args.text), me)
        except MessageStateError as exc:
            raise UsageError(str(exc)) from exc
        nudge_recipients(result.recipients)
        spawn_typed_delivery(result.message_id, wake)
        if result.paused:
            print(
                f"board #{result.message_id}; thread paused after {MAX_AGENT_HOPS} agent hops: "
                "delivered to nobody until the user posts in it"
            )
            return 0
        reached = ", ".join(r.handle for r in result.recipients) or "nobody directly (mention @handle or a PR/issue)"
        print(f"board #{result.message_id}; delivered to: {reached}. The user and every session can read it.")
        return 0
    with transaction(conn):
        rows = list_board(conn)
        mark_seen_in_inbox(conn, me.conversation_id, [int(r["id"]) for r in rows], at)
    if not rows:
        print("board empty")
        return 0
    for row in reversed(rows):
        when = datetime.fromtimestamp(int(row["created_at"]) / 1000).strftime("%m-%d %H:%M")
        topic = f" [{row['topic']}]" if row["topic"] else ""
        replies = f" ({row['replies']} replies: bridge show {row['id']})" if row["replies"] else ""
        print(f"#{row['id']} {when} {row['from_handle']}{topic}{replies}\n{row['body']}\n")
    print('Answer a post: bridge reply N "…"')
    return 0


def print_wake_lines(
    conn: sqlite3.Connection,
    config: Config,
    sender: Party,
    message_id: int,
    recipients: list[Recipient],
    wake: list[Recipient],
) -> None:
    # A locked tab is out of the team: its message waits for the next hook, and it is told of no way to wake anyone.
    if is_conversation_locked(conn, config, sender.conversation_id):
        return
    leads, workers, assistants = wake_typed_recipients(conn, config, sender, message_id, recipients)
    for recipient in leads:
        print(f"{recipient.handle} is a team lead: this is typed into its tab as soon as it is idle.")
    for recipient in workers:
        print(f"{recipient.handle} is your worker: this is typed into its tab as soon as it is idle.")
    for recipient in assistants:
        print(f"{recipient.handle} is the assistant: this is typed into its tab as soon as it is idle.")
    typed = [*leads, *workers, *assistants]
    hinted = [r for r in wake if r not in typed and not is_conversation_locked(conn, config, r.conversation_id)]
    for line in map_wake_to_lines(hinted):
        print(line)


def wake_typed_recipients(
    conn: sqlite3.Connection, config: Config, sender: Party, message_id: int, recipients: list[Recipient]
) -> tuple[list[Recipient], list[Recipient], list[Recipient]]:
    """Runs after the commit: what is not typed still waits for the recipient's next hook."""
    workers = find_worker_recipients(conn, config, sender, recipients)
    assistants = [r for r in find_assistant_recipients(conn, config, sender, recipients) if r not in workers]
    leads = [
        r for r in find_lead_recipients(conn, config, sender, recipients) if r not in workers and r not in assistants
    ]
    typed = [*leads, *workers, *assistants]
    if typed:
        # deliver_typed admits a lead by any mark and the others only by their own, and it admits both of those.
        post_type = LEAD_TO_WORKER if workers else TO_ASSISTANT if assistants else TO_LEAD
        with transaction(conn):
            conn.execute("UPDATE messages SET post_type = ? WHERE id = ?", (post_type, message_id))
        spawn_typed_delivery(message_id, typed)
    return leads, workers, assistants


def find_worker_recipients(
    conn: sqlite3.Connection, config: Config, sender: Party, recipients: list[Recipient]
) -> list[Recipient]:
    session = find_conversation_tab(conn, sender.conversation_id)
    if not session or not is_lead_tab(conn, session):
        return []
    return find_own_workers(conn, recipients, session, read_tree(config))


def nudge_recipients(recipients: list[Recipient]) -> None:
    """The message is already committed; a failed hint only delays mid-turn delivery by the recheck interval."""
    for recipient in recipients:
        if not recipient.pid:
            continue
        try:
            write_hint(recipient.pid, 0)
        except OSError as exc:
            print(f"warning: could not flag {recipient.handle} for mid-turn delivery: {exc}", file=sys.stderr)


def map_wake_to_lines(wake: list[Recipient]) -> list[str]:
    lines: list[str] = []
    for recipient in wake:
        if recipient.conversation_id == USER_CONVERSATION:
            continue
        if recipient.peer_messaging and recipient.peer_name:
            lines.append(
                f"{recipient.handle} gets this on its next prompt or tool call. To wake it now, use the SendMessage "
                f'tool: to "{recipient.peer_name}", message "You have bridge messages: run `bridge inbox`".'
            )
        else:
            lines.append(f"{recipient.handle} cannot be woken (no peer messaging); it gets this on its next turn.")
    return lines


def map_message_to_text(row: sqlite3.Row, show_recipients: bool = False) -> str:
    when = datetime.fromtimestamp(int(row["created_at"]) / 1000).strftime("%H:%M")
    kind = str(row["kind"]).upper() if row["kind"] == "request" else str(row["kind"])
    target = f" → {row['recipients']}" if show_recipients and row["recipients"] else ""
    keys = row.keys()
    state = ""
    if "done_at" in keys:
        if row["done_at"]:
            state = " [done]"
        elif row["declined_at"]:
            state = " [declined]"
        elif row["acked_at"] and row["kind"] == "request":
            state = " [acked]"
    if row["cancelled_at"]:
        state = " [cancelled]"
    return f"#{row['id']} {kind} {when} from {row['from_handle']}{target}{state}\n{row['body']}"


def convert_duration_to_seconds(text: str) -> int:
    units = {"s": 1, "m": 60, "h": 3600}
    try:
        return int(text[:-1]) * units[text[-1]] if text[-1] in units else int(text) * 60
    except (ValueError, IndexError) as exc:
        raise argparse.ArgumentTypeError(f"bad duration {text!r}; use 15m, 2h or 90s") from exc
