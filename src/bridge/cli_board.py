import argparse
import re
import sqlite3
import sys
from collections.abc import Sequence
from datetime import datetime

from bridge.asks import mark_ask_told
from bridge.board import (
    BODY_MAX_CHARS,
    DEFAULT_RESPOND_WITHIN_S,
    HINT_SENT,
    MAX_AGENT_HOPS,
    SYSTEM_CONVERSATION,
    USER_CONVERSATION,
    AddressError,
    MessageStateError,
    Party,
    Recipient,
    convert_item_id_to_answer_ref,
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
    ANSWER_KINDS,
    FEED_KINDS,
    LEAD_TO_WORKER,
    OPEN_KINDS,
    POST_TYPES,
    TO_ASSISTANT,
    TO_LEAD,
    WORKER_REPORT,
    add_post,
    batch_grace_ms,
    build_decision_body,
    find_leads_to_wake,
    find_oldest_typable,
    is_batch_ripe,
    map_arg_to_link,
    post_to_board,
    reject_locked_recipients,
    resolve_tab,
    spawn_typed_delivery,
    withdraw_post,
)
from bridge.identity import current_party
from bridge.leads import find_lead_conversations, find_lead_of, is_conversation_locked, is_lead_tab
from bridge.pages import NO_LINK_FLAG, find_unlinked_page_mention
from bridge.pins import pin_link_once
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
NOW_HELP = (
    "wake an idle recipient at once instead of with its next batch: urgent only (a prod alarm, or the user said so)"
)
NO_LINK_HELP = "the words page, analysis, explainer, write-up or doc here name no page; post without a --link"


def add_board_commands(sub: "argparse._SubParsersAction[argparse.ArgumentParser]", config: Config) -> None:  # pyright: ignore[reportPrivateUsage]
    p = sub.add_parser("send", help="message other sessions by handle (comma-separated)")
    p.add_argument("to", help="handle[,handle…]")
    p.add_argument("text")
    p.add_argument("--request", action="store_true", help="expects a response (ack/reply/done/decline)")
    p.add_argument("--respond-within", type=convert_duration_to_seconds, default=DEFAULT_RESPOND_WITHIN_S)
    p.add_argument("--now", action="store_true", help=NOW_HELP)
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("reply", help="reply to a message")
    p.add_argument("id", type=int)
    p.add_argument("text")
    p.add_argument("--done", action="store_true", help="also close the request")
    p.add_argument("--now", action="store_true", help=NOW_HELP)
    p.set_defaults(func=cmd_reply)

    for action, text in (
        ("ack", "seen, working on it"),
        ("done", "request handled"),
        ("decline", "will not do it"),
    ):
        p = sub.add_parser(action, help=f"mark message(s): {text}")
        p.add_argument("ids", type=int, nargs="+")
        p.set_defaults(func=cmd_respond, action=action)

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
    p.add_argument("--what", help="blocked/decision: what is being decided, in plain words; a line of the card")
    p.add_argument("--why", help="blocked/decision: why it is needed now; a line of the card")
    p.add_argument("--cost", action=_CostAction, default=[], help="what the --option before it costs; shown under it")
    p.add_argument("--pick", type=int, help="the answer you recommend, by its number (1 = the first --option)")
    p.add_argument("--pick-why", help="one line: why that answer")
    p.add_argument(
        "--urgent",
        action="store_true",
        help="blocked/decision only, when prod or users are hurting, money is leaking, or a deadline is today",
    )
    p.add_argument("--ask", type=lambda s: int(s.lstrip("#")), metavar="N", help="done/insight: the ask it answers")
    p.add_argument(
        "--pin", action="store_true", help="done/insight: also pin the first --link (an artifact) to the Pinned page"
    )
    p.add_argument(NO_LINK_FLAG, action="store_true", help=NO_LINK_HELP)
    p.set_defaults(func=cmd_post)

    aliases = [f"tell-{config.user_alias}"] if config.user_alias else []
    p = sub.add_parser("tell-user", aliases=aliases, help="one line for the user's feed (older form of post)")
    p.add_argument("text")
    severity = p.add_mutually_exclusive_group()
    severity.add_argument("--action", dest="kind", action="store_const", const="decision")
    severity.add_argument("--blocker", dest="kind", action="store_const", const="blocked")
    p.add_argument(NO_LINK_FLAG, action="store_true", help=NO_LINK_HELP)
    p.set_defaults(func=cmd_tell_user, kind="insight")

    p = sub.add_parser("board", help="the team board: post to everyone (no TEXT: read recent posts)")
    p.add_argument("text", nargs="?")
    p.add_argument("--topic", help="a short tag, e.g. a repo or an incident")
    p.add_argument("--type", choices=POST_TYPES, help="what kind of post this is")
    p.add_argument("--thread", help="an issue or PR id; posts with the same id form a thread")
    p.add_argument("--link", help="the PR, issue or doc this post points to")
    p.add_argument("--now", action="store_true", help=NOW_HELP)
    p.set_defaults(func=cmd_board)


class _CostAction(argparse.Action):
    """Pairs each --cost with the --option given just before it."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[object] | None,
        option_string: str | None = None,
    ) -> None:
        options: list[str] = list(getattr(namespace, "option", None) or [])
        costs: list[str | None] = list(getattr(namespace, self.dest, None) or [])
        if not options:
            parser.error("--cost follows the --option it prices")
        costs += [None] * (len(options) - len(costs))
        if costs[-1] is not None:
            parser.error("one --cost per --option")
        costs[-1] = str(values)
        setattr(namespace, self.dest, costs)


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
            flag_overdue(conn, at, batch_grace_ms(config))
            result = send_message(
                conn, me, recipients, kind, str(args.text), at, int(args.respond_within), goes_now=bool(args.now)
            )
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
            result = send_message(
                conn, me, [original_sender], "reply", str(args.text), at, reply_to=int(args.id), goes_now=bool(args.now)
            )
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


def cmd_inbox(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    me = current_party(conn)
    at = now_ms()
    with transaction(conn):
        flag_overdue(conn, at, batch_grace_ms(config))
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


CARD_LINE_MAX_CHARS = 160
CARD_LINE_MAX_SENTENCES = 2
_SENTENCE_END_RE = re.compile(r"[.!?](?:\s+|$)")


def find_card_line_problem(words: str | None) -> str | None:
    """A long field with no line break or bullet reads as a wall on the card; length alone is fine (the owner's word:
    it is the layout, not the amount). A nudge, never a refusal."""
    text = str(words or "").strip()
    if "\n" in text:
        return None
    if len(text) > CARD_LINE_MAX_CHARS or len(_SENTENCE_END_RE.findall(text)) > CARD_LINE_MAX_SENTENCES:
        return f"is one block of {len(text)} characters"
    return None


def cmd_post(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    me = current_party(conn)
    if args.kind == POST_RESOLVE:
        return _withdraw_post(conn, me, str(args.title), config.assistant_handle)
    structured = bool(args.what or args.why or args.cost or args.pick is not None or args.pick_why)
    if structured and args.kind not in OPEN_KINDS:
        raise UsageError("--what, --why, --cost and --pick are for blocked and decision posts")
    if args.pin and args.kind not in ANSWER_KINDS:
        raise UsageError("--pin is for done and insight posts")
    if args.pin and not args.link:
        raise UsageError("--pin pins the first --link; give one")
    if (
        args.kind in OPEN_KINDS
        and not (args.body or args.what or args.why)
        and me.conversation_id in find_lead_conversations(conn)
    ):
        raise UsageError("a question from a lead needs --body, or --what and --why: what it is, why now, each cost")
    for flag, words in [
        ("--what", args.what),
        ("--why", args.why),
        ("--pick-why", args.pick_why),
        *(("--cost", c) for c in args.cost),
    ]:
        if problem := find_card_line_problem(words):
            print(
                f"warning: {flag} {problem}; break it into lines or '- ' bullets so the card reads well",
                file=sys.stderr,
            )
    body = sys.stdin.read() if args.body == "-" else str(args.body)
    shown = "\n".join([str(args.title), body, args.what or "", args.why or ""])
    in_buttons = [*args.option, *(c for c in args.cost if c), args.pick_why or ""]
    try:
        if structured:
            body = build_decision_body(
                body, args.what, args.why, list(args.option), list(args.cost), args.pick, args.pick_why
            )
        links = [map_arg_to_link(str(link)) for link in args.link]
    except MessageStateError as exc:
        raise UsageError(str(exc)) from exc
    urls = [link.url for link in links]
    if problem := find_unlinked_page_mention(conn, shown, urls, bool(args.no_link), in_buttons):
        raise UsageError(problem)
    if to := find_post_route(conn, config, me, str(args.kind), args.ask, bool(args.pin)):
        flags = [*(["urgent"] if args.urgent else []), *(["pin its link"] if args.pin else [])]
        fields = [
            *(f"Answer {n}: {option}" for n, option in enumerate(args.option, 1)),
            *(f"Link: {link.label}={link.url}" if link.label else f"Link: {link.url}" for link in links),
            *([f"Answers ask #{args.ask}"] if args.ask is not None else []),
        ]
        return send_draft(conn, config, me, to, str(args.kind), str(args.title), body, fields, flags)
    try:
        at = now_ms()
        pinned = None
        with transaction(conn):
            item_id = add_post(
                conn, me, str(args.kind), str(args.title), body, links, list(args.option), at, bool(args.urgent),
                args.ask,
            )  # fmt: skip
            if args.ask is not None and args.kind in ANSWER_KINDS:
                mark_ask_told(conn, int(args.ask), at)
            if args.pin:
                source = f"ask #{args.ask}" if args.ask is not None else convert_item_id_to_answer_ref(item_id)
                pinned = pin_link_once(conn, str(args.title), links[0].url, source, me.handle, at)
    except MessageStateError as exc:
        raise UsageError(str(exc)) from exc
    waits = " until the user answers; the answer comes back to this session" if args.kind in OPEN_KINDS else ""
    urgent = ", urgent" if args.urgent else ""
    pin = (f"; pinned as #{pinned.id}" if pinned else "; its link was already pinned") if args.pin else ""
    print(f"post #{item_id} ({args.kind}{urgent}) is in the user's feed{waits}{pin}")
    return 0


def find_post_route(
    conn: sqlite3.Connection, config: Config, me: Party, kind: str, ask: int | None, pin: bool
) -> str | None:
    """The handle a post goes to in place of the owner's feed, or None for the feed. Only the assistant puts questions
    in front of the owner, and results only when it chose them: a worker's post goes to its lead, a lead's question
    and a lead's result that answers no ask and pins nothing go to the assistant. A session under no lead posts as
    before."""
    if me.handle == config.assistant_handle or me.conversation_id == SYSTEM_CONVERSATION:
        return None
    if me.conversation_id in find_lead_conversations(conn):
        if kind in ANSWER_KINDS and (ask is not None or pin):
            return None
        return config.assistant_handle
    row: sqlite3.Row | None = conn.execute(
        "SELECT surface FROM instances WHERE conversation_id = ? AND ended_at IS NULL ORDER BY first_seen_at DESC",
        (me.conversation_id,),
    ).fetchone()
    lead_surface = find_lead_of(conn, str(row["surface"]) if row else None)[1]
    if lead_surface is None:
        return None
    lead: sqlite3.Row | None = conn.execute("SELECT handle FROM surfaces WHERE surface = ?", (lead_surface,)).fetchone()
    return str(lead["handle"]) if lead and lead["handle"] else config.assistant_handle


def split_at_spaces(text: str, room: int) -> list[str]:
    """Parts of at most `room` chars, each ending at a space or line break where one exists, so a link or a page
    citation is never cut in two (each part is checked on its own)."""
    parts: list[str] = []
    while len(text) > room:
        cut = max(text.rfind(" ", 0, room), text.rfind("\n", 0, room))
        cut = cut if cut > 0 else room
        parts.append(text[:cut])
        text = text[cut:].lstrip(" ")
    return [*parts, text] if text else parts


def split_draft(text: str) -> list[str]:
    """The draft as messages within the length limit, numbered when more than one, each cut at a space."""
    if len(text) <= BODY_MAX_CHARS:
        return [text]
    count = 2
    # The prefix grows with the part count, and splitting at spaces can need more parts than length alone says.
    while len(parts := split_at_spaces(text, BODY_MAX_CHARS - len(f"(part {count} of {count}) "))) > count:
        count = len(parts)
    return [f"(part {n} of {len(parts)}) {part}" for n, part in enumerate(parts, 1)]


def send_draft(
    conn: sqlite3.Connection,
    config: Config,
    me: Party,
    to: str,
    kind: str,
    title: str,
    body: str,
    fields: list[str],
    flags: list[str],
) -> int:
    """A post the owner should not see straight away, as a draft message to `to` with every field, delivered like any
    message so an idle recipient wakes; one too long for a message goes out in parts, never cut."""
    head = f"Draft {kind} post{' (' + ', '.join(flags) + ')' if flags else ''}, not shown to the owner: {title}"
    text = "\n".join(part for part in (head, body.strip(), "\n".join(fields)) if part)
    parts = split_draft(text)
    mark = TO_ASSISTANT if to == config.assistant_handle else WORKER_REPORT
    sent: list[int] = []
    try:
        with transaction(conn):
            reject_locked_sender(conn, config, me)
            recipients = [resolve_handle(conn, to)]
            reject_locked_recipients(conn, config, recipients)
            for part in parts:
                result = send_message(conn, me, recipients, "info", part, now_ms())
                conn.execute("UPDATE messages SET post_type = ? WHERE id = ?", (mark, result.message_id))
                sent.append(result.message_id)
    except (MessageStateError, AddressError) as exc:
        raise UsageError(str(exc)) from exc
    nudge_recipients(recipients)
    for message_id in sent:
        spawn_typed_delivery(message_id, recipients)
    print(
        f"sent to {to} as draft message #{', #'.join(map(str, sent))}, not the owner's feed: a worker's post goes to "
        "its lead; a lead's question, and a lead's result without --ask N or --pin, go to the assistant, who posts "
        "what the owner should see"
    )
    return 0


def _withdraw_post(conn: sqlite3.Connection, me: Party, text: str, assistant: str) -> int:
    if not text.isdecimal():
        raise UsageError("bridge post resolve N: N is the post's number")
    try:
        with transaction(conn):
            withdraw_post(conn, me, int(text), now_ms(), assistant)
    except MessageStateError as exc:
        raise UsageError(str(exc)) from exc
    print(f"post #{text} withdrawn")
    return 0


def cmd_tell_user(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    me = current_party(conn)
    if problem := find_unlinked_page_mention(conn, str(args.text), [], bool(args.no_link)):
        raise UsageError(f'{problem}. tell-user carries no link: use `bridge post {args.kind} "…" --link …`')
    if to := find_post_route(conn, config, me, str(args.kind), None, False):
        return send_draft(conn, config, me, to, str(args.kind), str(args.text), "", [], [])
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
                    conn, me, str(args.text), args.topic, at, config, args.type, args.thread, args.link, bool(args.now)
                )
                wake = [] if result.paused else find_leads_to_wake(conn, config, str(args.text), me)
        except MessageStateError as exc:
            raise UsageError(str(exc)) from exc
        nudge_recipients(result.recipients)
        spawn_when_ripe(conn, config, result.message_id, wake)
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
    leads, workers, assistants, held = wake_typed_recipients(conn, config, sender, message_id, recipients)
    for recipients_of, role in ((leads, "a team lead"), (workers, "your worker"), (assistants, "the assistant")):
        for recipient in recipients_of:
            if recipient in held:
                print(
                    f"{recipient.handle} gets this with its next batch "
                    f"({config.batch_count} waiting or {config.batch_age_min:g} min)."
                )
            else:
                print(f"{recipient.handle} is {role}: this is typed into its tab as soon as it is idle.")
    typed = [*leads, *workers, *assistants]
    hinted = [r for r in wake if r not in typed and not is_conversation_locked(conn, config, r.conversation_id)]
    for line in map_wake_to_lines(hinted):
        print(line)


def wake_typed_recipients(
    conn: sqlite3.Connection, config: Config, sender: Party, message_id: int, recipients: list[Recipient]
) -> tuple[list[Recipient], list[Recipient], list[Recipient], list[Recipient]]:
    """Runs after the commit: what is not typed still waits for the recipient's next hook. Last come the typed
    recipients whose batch holds it."""
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
    return leads, workers, assistants, spawn_when_ripe(conn, config, message_id, typed)


def spawn_when_ripe(
    conn: sqlite3.Connection, config: Config, message_id: int, recipients: list[Recipient]
) -> list[Recipient]:
    """Starts a typer for each recipient whose batch is ripe, of its oldest waiting message: that turn's hooks bring
    the rest after it, in order. Returns the others, whose batch holds it."""
    held: list[Recipient] = []
    for recipient in recipients:
        if not is_batch_ripe(conn, config, recipient.conversation_id, now_ms()):
            held.append(recipient)
            continue
        oldest = find_oldest_typable(conn, recipient.conversation_id) if config.batch_age_min else None
        spawn_typed_delivery(oldest or message_id, [recipient])
    return held


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
            write_hint(recipient.pid, HINT_SENT)
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
