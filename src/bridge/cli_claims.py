import argparse
import json
import os
import sqlite3
from dataclasses import asdict

from bridge.board import Party, Recipient, add_user_item, send_message
from bridge.claims import (
    DEFAULT_TTL_S,
    ClaimError,
    ClaimOutcome,
    Holder,
    ResourceKey,
    acquire,
    find_holder,
    list_claims,
    map_row_to_holder,
    release,
    renew,
    resolve_resource,
)
from bridge.cli_board import convert_duration_to_seconds
from bridge.config import Config
from bridge.db import now_ms, transaction
from bridge.errors import UsageError
from bridge.gitinfo import read_git_info
from bridge.guard import sync_claims_flag
from bridge.identity import current_party
from bridge.registry import end_dead_instances, format_age

EXIT_HELD = 3


def add_claim_commands(sub: "argparse._SubParsersAction[argparse.ArgumentParser]", config: Config) -> None:  # pyright: ignore[reportPrivateUsage]
    p = sub.add_parser("claim", help="reserve a shared resource before mutating it")
    p.add_argument("resource", help="pr:<n>|pr:<owner>/<repo>#<n>|branch:<name>|env:<name>|path:<p>|issue:<KEY>")
    p.add_argument("--reason")
    p.add_argument("--ttl", type=convert_duration_to_seconds, default=DEFAULT_TTL_S)
    p.add_argument("--offline", action="store_true", help="skip GitHub PR/branch aliasing (may miss conflicts)")
    p.add_argument("--force", action="store_true", help="user only: take it from a live holder")
    p.set_defaults(func=cmd_claim)

    p = sub.add_parser("renew", help="extend a claim you hold")
    p.add_argument("resource")
    p.add_argument("--token", required=True)
    p.add_argument("--ttl", type=convert_duration_to_seconds, default=DEFAULT_TTL_S)
    p.set_defaults(func=cmd_renew)

    p = sub.add_parser("release", help="give a claim back")
    p.add_argument("resource")
    p.add_argument("--token", required=True)
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("who", help="who holds a resource")
    p.add_argument("resource")
    p.add_argument("--offline", action="store_true")
    p.set_defaults(func=cmd_who)

    p = sub.add_parser("claims", help="all claims, or yours with tokens")
    p.add_argument("--mine", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_claims)


def cmd_claim(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    me = current_party(conn)
    key = _resolve(conn, config, str(args.resource), bool(args.offline))
    at = now_ms()
    try:
        with transaction(conn):
            end_dead_instances(conn, at)
            outcome = acquire(conn, me, key, args.reason, int(args.ttl), at, force=bool(args.force))
            if outcome.status == "taken-over" and outcome.holder:
                _tell_previous_holder(conn, me, outcome.holder, key.canonical, at)
                if outcome.holder.live:
                    # Only the user may force; the item makes an agent doing it on its own visible.
                    text = f"{me.handle} forced a takeover of {key.canonical} from {outcome.holder.holder_handle}"
                    add_user_item(conn, me, "blocker", text[:140], at)
    except ClaimError as exc:
        raise UsageError(str(exc)) from exc
    sync_claims_flag(conn)
    for line in map_outcome_to_lines(outcome, at):
        print(line)
    return EXIT_HELD if outcome.status.startswith("held") else 0


def cmd_renew(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    key = _resolve(conn, config, str(args.resource), offline=True)
    try:
        with transaction(conn):
            expires = renew(conn, key.canonical, str(args.token), int(args.ttl), now_ms())
    except ClaimError as exc:
        raise UsageError(str(exc)) from exc
    print(f"renewed {key.canonical} for {format_age(expires - now_ms())}")
    return 0


def cmd_release(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    key = _resolve(conn, config, str(args.resource), offline=True)
    try:
        with transaction(conn):
            release(conn, key.canonical, str(args.token), now_ms())
    except ClaimError as exc:
        raise UsageError(str(exc)) from exc
    sync_claims_flag(conn)
    print(f"released {key.canonical}")
    return 0


def cmd_who(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    key = _resolve(conn, config, str(args.resource), bool(args.offline))
    with transaction(conn):
        end_dead_instances(conn, now_ms())
    holder = find_holder(conn, key, now_ms())
    print(map_holder_to_text(holder, now_ms()) if holder else f"{key.canonical} is free")
    return 0


def cmd_claims(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    conversation = current_party(conn).conversation_id if args.mine else None
    rows = list_claims(conn, conversation)
    at = now_ms()
    if args.json:
        print(json.dumps([{**asdict(map_row_to_holder(r)), "token": r["token"] if args.mine else None} for r in rows]))
        return 0
    if not rows:
        print("no claims")
    for row in rows:
        token = f"  token {row['token']}" if args.mine else ""
        print(map_holder_to_text(map_row_to_holder(row), at) + token)
    return 0


def map_outcome_to_lines(outcome: ClaimOutcome, at: int) -> list[str]:
    key = outcome.key
    names = key.canonical + (f" (= {key.alias})" if key.alias else "")
    notes = [f"note: {key.note}"] if key.note else []
    if outcome.status in ("acquired", "already-yours", "taken-over"):
        verb = {"acquired": "claimed", "already-yours": "already yours:", "taken-over": "taken over:"}[outcome.status]
        lines = [f"{verb} {names}  token {outcome.token}"]
        if outcome.status == "taken-over" and outcome.holder:
            lines.append(f"previous holder {outcome.holder.holder_handle} was told")
        lines.append(f"release with: bridge release {key.canonical} --token {outcome.token}")
        return lines + notes
    holder = outcome.holder
    assert holder is not None
    lines = [f"HELD: {map_holder_to_text(holder, at)}"]
    if outcome.status == "held-suspect":
        lines.append("Its claim has expired but the holder is still running: ask it, do not proceed.")
    lines.append(f'Ask: bridge send {holder.holder_handle} "need {key.canonical} for …, can you release?" --request')
    lines.append("Do not mutate the resource until you hold it.")
    return lines + notes


def map_holder_to_text(holder: Holder, at: int) -> str:
    state = "expired, " if holder.expires_at < at else ""
    life = "" if holder.live else ", holder session ended"
    reason = f" — {holder.reason}" if holder.reason else ""
    held_for = format_age(at - holder.acquired_at)
    return f"{holder.resource} held by {holder.holder_handle} ({state}{held_for} ago{life}){reason}"


def _resolve(conn: sqlite3.Connection, config: Config, text: str, offline: bool) -> ResourceKey:
    # Shorthand pr:N / branch:X refer to the repo of the directory the command runs in.
    git = read_git_info(os.getcwd())
    default_repo = config.pr_repo_for(git.repo if git else None)
    try:
        return resolve_resource(text, default_repo, offline)
    except ClaimError as exc:
        raise UsageError(str(exc)) from exc


def _tell_previous_holder(conn: sqlite3.Connection, me: Party, holder: Holder, resource: str, at: int) -> None:
    recipient = Recipient(holder.holder_handle, holder.holder_conversation, 0, "ended", None, False)
    send_message(conn, me, [recipient], "info", f"I took over your claim on {resource}.", at)
