import argparse
import json
import sqlite3
import sys
import time
from dataclasses import asdict
from datetime import datetime

from bridge.asks import (
    WAIT_ON,
    Actor,
    Ask,
    AskError,
    add_ask,
    claim_ask_done,
    close_ask,
    convert_wait_to_text,
    get_ask,
    list_ask_events,
    list_asks,
    map_owner_name_to_wait_on,
    map_wait_on_arg_to_stored,
    note_ask,
    reopen_ask,
    start_ask,
    verify_ask,
    wait_ask,
)
from bridge.config import Config
from bridge.db import now_ms, transaction
from bridge.identity import current_party, ensure_current_instance
from bridge.leads import LeadError, find_lead_on_surface, get_lead, is_assistant_surface, is_surface_locked

WORDS_SHOWN_CHARS = 100


def add_ask_commands(sub: "argparse._SubParsersAction[argparse.ArgumentParser]", owner_name: str) -> None:  # pyright: ignore[reportPrivateUsage]
    ask = sub.add_parser("ask", help="the owner's asks, each taken to verified with evidence of what they would see")
    ask_sub = ask.add_subparsers(dest="ask_command", required=True)
    p = ask_sub.add_parser("add", help="record an ask in the owner's own words")
    p.add_argument("owner", help="the lead that answers for it, or the assistant's handle")
    p.add_argument("--words", help="their words verbatim; default: stdin")
    p.add_argument("--from", dest="from_id", type=int, metavar="N", help="quote message N's body as the words")
    p.add_argument("--ref", action="append", default=[], help="a PR, issue or page URL; repeat")
    p.set_defaults(func=cmd_ask_add)
    p = ask_sub.add_parser("list", help="asks not closed, oldest first")
    p.add_argument("--owner")
    p.add_argument("--all", action="store_true", help="closed ones too")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_ask_list)
    p = ask_sub.add_parser("show", help="one ask with its history")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_ask_show)
    p = ask_sub.add_parser("start", help="open → in_progress")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_ask_start)
    p = ask_sub.add_parser("note", help="news on an ask not done yet or not verified yet; it counts as an update")
    p.add_argument("id", type=int)
    p.add_argument("text")
    p.set_defaults(func=cmd_ask_note)
    p = ask_sub.add_parser(
        "wait",
        help="it cannot move: its reminders pause until its next start, note, done, verify or reopen, "
        "or until --until passes",
    )
    p.add_argument("id", type=int)
    owner_word = map_owner_name_to_wait_on(owner_name)
    ons = (owner_word, *WAIT_ON) if owner_word else WAIT_ON
    shown = "|".join(on for on in ons if on != "owner" or not owner_word)
    when = p.add_mutually_exclusive_group(required=True)
    when.add_argument(
        "--on",
        choices=ons,
        metavar=shown,
        help=f"who or what it waits on; --on {owner_word or 'owner'}: waiting on {owner_name}",
    )
    when.add_argument(
        "--until",
        metavar="YYYY-MM-DD[THH:MM]",
        help="its owner pauses it until then, local time (a date alone: its 00:00); reminders resume after",
    )
    p.add_argument("why", help="who exactly, and for what")
    p.set_defaults(func=cmd_ask_wait)
    p = ask_sub.add_parser("done", help="claim it done; another session verifies")
    p.add_argument("id", type=int)
    p.add_argument("--claim", required=True, help="what was done and where it lives")
    p.set_defaults(func=cmd_ask_done)
    p = ask_sub.add_parser("verify", help="done_claimed → verified, with evidence of what the owner would see")
    p.add_argument("id", type=int)
    p.add_argument("--evidence", required=True, help="what the owner would see, and where")
    p.add_argument("--how", required=True, help="a URL, a screenshot path, or a command and its output")
    p.set_defaults(func=cmd_ask_verify)
    p = ask_sub.add_parser("close", help="verified → closed (the assistant only, relaying the owner)")
    p.add_argument("id", type=int)
    p.add_argument("--dropped", metavar="WHY", help="close an unfinished ask the owner let go, and why")
    p.set_defaults(func=cmd_ask_close)
    p = ask_sub.add_parser("reopen", help="back to open: the result is not what they asked for")
    p.add_argument("id", type=int)
    p.add_argument("--why", required=True)
    p.set_defaults(func=cmd_ask_reopen)


def cmd_ask_add(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    me = current_actor(conn, config)
    owner = resolve_ask_owner(conn, config, str(args.owner))
    if args.from_id is not None:
        if args.words is not None:
            raise AskError("--from quotes a message; give --words or --from, not both")
        row = conn.execute("SELECT body FROM messages WHERE id = ?", (args.from_id,)).fetchone()
        if row is None:
            raise AskError(f"no message #{args.from_id}")
        words, source = str(row["body"]), f"#{args.from_id}"
    elif args.words is not None:
        words, source = str(args.words), "tab"
    else:
        if sys.stdin.isatty():
            raise AskError("give their words: --words '…', --from N, or stdin")
        words, source = sys.stdin.read(), "tab"
    for ref in args.ref:
        if not str(ref).startswith(("https://", "http://")):
            raise AskError(f"a ref is a URL: {ref!r}")
    with transaction(conn):
        ask = add_ask(conn, owner, me.handle, words, source, [str(r) for r in args.ref], now_ms())
    print(f"#{ask.id} open, owner {ask.owner}")
    return 0


def cmd_ask_list(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    owner = str(args.owner).strip().lstrip("@").lower() if args.owner else None
    asks = list_asks(conn, owner, include_closed=bool(args.all))
    if args.json:
        print(json.dumps([asdict(a) for a in asks], indent=2, ensure_ascii=False))
        return 0
    if not asks:
        print("no asks" if args.all else "no open asks")
    now = now_ms()
    for ask in asks:
        words = " ".join(ask.words.split())
        words = words if len(words) <= WORDS_SHOWN_CHARS else words[: WORDS_SHOWN_CHARS - 1] + "…"
        idle = convert_ms_to_age(now - ask.updated_at)
        on = convert_wait_to_text(ask, config.owner_name, now)
        wait = f", {on}" if on else ""
        print(f"#{ask.id:<4} {ask.owner:<10} {ask.state:<12} {convert_ms_to_age(now - ask.created_at):>4} old, "
              f"{idle} since update{wait}  {words}")  # fmt: skip
    return 0


def cmd_ask_show(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    ask = get_ask(conn, int(args.id))
    print(convert_ask_to_text(ask, now_ms(), config.owner_name))
    for event in list_ask_events(conn, ask.id):
        stamp = _stamp(int(event["at"]))
        what = event["state"] or "note"
        note = f": {event['note']}" if event["note"] else ""
        print(f"  {stamp} {event['by']} {what}{note}".replace("\n", "\n    "))
    return 0


def cmd_ask_start(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    actor = current_actor(conn, config)
    with transaction(conn):
        ask = start_ask(conn, int(args.id), actor, now_ms())
    print(f"#{ask.id} in progress")
    return 0


def cmd_ask_note(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    actor = current_actor(conn, config)
    with transaction(conn):
        ask = note_ask(conn, int(args.id), actor, str(args.text), now_ms())
    print(f"#{ask.id} noted ({ask.state})")
    return 0


def cmd_ask_wait(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    actor = current_actor(conn, config)
    now = now_ms()
    on = None if args.on is None else map_wait_on_arg_to_stored(str(args.on), config.owner_name)
    until = None if args.until is None else map_local_time_arg_to_ms(str(args.until))
    with transaction(conn):
        ask = wait_ask(conn, int(args.id), actor, on, str(args.why), now, until)
    after = "it moves" if until is None else "then, or until it moves"
    print(f"#{ask.id} {convert_wait_to_text(ask, config.owner_name, now)}; no reminders until {after}")
    return 0


def cmd_ask_done(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    actor = current_actor(conn, config)
    with transaction(conn):
        ask = claim_ask_done(conn, int(args.id), actor, str(args.claim), now_ms())
    print(f"#{ask.id} done_claimed by {actor.handle}; a different session verifies it with evidence")
    return 0


def cmd_ask_verify(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    actor = current_actor(conn, config)
    with transaction(conn):
        ask = verify_ask(conn, int(args.id), actor, str(args.evidence), str(args.how), now_ms())
    print(f"#{ask.id} verified by {actor.handle}")
    return 0


def cmd_ask_close(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    actor = current_actor(conn, config)
    if actor.role != "assistant":
        raise AskError(
            f"only {config.assistant_handle} closes an ask, relaying the owner; the page has their own button"
        )
    with transaction(conn):
        ask = close_ask(conn, int(args.id), "assistant", actor.handle, now_ms(), args.dropped)
    print(f"#{ask.id} closed")
    return 0


def cmd_ask_reopen(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    actor = current_actor(conn, config)
    with transaction(conn):
        ask = reopen_ask(conn, int(args.id), actor.handle, str(args.why), now_ms())
    print(f"#{ask.id} open again")
    return 0


def resolve_ask_owner(conn: sqlite3.Connection, config: Config, name: str) -> str:
    wanted = name.strip().lstrip("@").lower()
    if wanted == config.assistant_handle.lower():
        return config.assistant_handle
    try:
        lead = get_lead(conn, wanted)
    except LeadError as exc:
        raise AskError(f"an ask's owner is a lead or {config.assistant_handle}; {name!r} is neither") from exc
    if lead.retired_at is not None:
        raise AskError(f"{lead.display_name} is retired")
    return lead.name


def current_actor(conn: sqlite3.Connection, config: Config) -> Actor:
    me = current_party(conn)
    row = conn.execute(
        "SELECT surface FROM instances WHERE instance_id = ?", (ensure_current_instance(conn),)
    ).fetchone()
    surface = row["surface"] if row else None
    if is_surface_locked(conn, config, surface):
        raise AskError("this tab is locked, and Guild ignores locked tabs")
    if is_assistant_surface(conn, config, surface):
        role = "assistant"
    elif find_lead_on_surface(conn, surface) is not None:
        role = "lead"
    else:
        role = "other"
    return Actor(handle=me.handle, conversation_id=me.conversation_id, role=role)


def convert_ask_to_text(ask: Ask, now: int, owner_name: str) -> str:
    lines = [
        f"#{ask.id} {ask.state}, owner {ask.owner}, recorded by {ask.recorded_by} from {ask.source}, "
        f"{convert_ms_to_age(now - ask.created_at)} old",
        "  words: " + ask.words.replace("\n", "\n    "),
    ]
    if ask.refs:
        lines.append("  refs: " + " ".join(ask.refs))
    if ask.claim:
        lines.append(f"  claim ({ask.claim_by}): {ask.claim}")
    if ask.evidence:
        lines.append(f"  evidence ({ask.evidence_by}): {ask.evidence}\n  how: {ask.evidence_how}")
    on = convert_wait_to_text(ask, owner_name, now)
    if on and ask.wait_at is not None:
        lines.append(f"  {on} ({ask.wait_by}, {convert_ms_to_age(now - ask.wait_at)} ago): {ask.wait_why}")
    if ask.reopen_why:
        lines.append(f"  reopened: {ask.reopen_why}")
    if ask.closed_how:
        lines.append(f"  closed: {ask.closed_how}")
    return "\n".join(lines)


def convert_ms_to_age(ms: int) -> str:
    minutes = max(0, ms) // 60_000
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 48 * 60:
        return f"{minutes // 60}h"
    return f"{minutes // 1440}d"


def _stamp(at_ms: int) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(at_ms / 1000))


def map_local_time_arg_to_ms(value: str) -> int:
    for shape in ("%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(value, shape).timestamp() * 1000)
        except ValueError:
            continue
    raise AskError(f"--until {value!r}: YYYY-MM-DD or YYYY-MM-DDTHH:MM, local time")
