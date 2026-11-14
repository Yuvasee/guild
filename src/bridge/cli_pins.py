import argparse
import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from bridge.artifacts import backfill_artifact_publishes, scan_transcripts
from bridge.cli_asks import convert_ms_to_age
from bridge.config import Config
from bridge.db import now_ms, transaction
from bridge.identity import current_party
from bridge.pins import add_pin, archive_pin, get_pin, list_pins

DAY_MS = 86_400_000


def add_pin_commands(sub: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:  # pyright: ignore[reportPrivateUsage]
    p = sub.add_parser(
        "pin",
        help="pin something the owner needs again to the Pinned page (/pinned): a command, a link or a note; "
        "never a secret, only where it lives",
    )
    p.add_argument("title")
    what = p.add_mutually_exclusive_group(required=True)
    what.add_argument("--cmd", help="a command; the page gives it a Copy button")
    what.add_argument("--link", help="an http(s) URL")
    what.add_argument("--note", help="e.g. where a credential lives: \"1Password item 'Prod DB admin'\"")
    p.add_argument("--source", help="which lead or ask it came from, e.g. alex or 'ask #64'")
    p.set_defaults(func=cmd_pin)

    p = sub.add_parser("unpin", help="take a pin off the Pinned page; it is kept, hidden")
    p.add_argument("id", type=lambda s: int(s.lstrip("#")))
    p.set_defaults(func=cmd_unpin)

    p = sub.add_parser("pins", help="the pins on the Pinned page, newest first")
    p.add_argument("--all", action="store_true", help="unpinned ones too")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_pins)

    p = sub.add_parser("_backfill-artifacts", help=argparse.SUPPRESS)
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--projects", default=str(Path.home() / ".claude" / "projects"), help="Claude Code transcripts")
    p.set_defaults(func=cmd_backfill_artifacts)


def cmd_pin(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    kind, body = next((k, v) for k in ("cmd", "link", "note") if (v := getattr(args, k)) is not None)
    by = current_party(conn).handle
    with transaction(conn):
        pin = add_pin(conn, str(args.title), kind, str(body), args.source, by, now_ms())
    print(pin.id)
    return 0


def cmd_unpin(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    by = current_party(conn).handle
    with transaction(conn):
        pin = archive_pin(conn, int(args.id), by, now_ms())
    print(f"#{pin.id} unpinned: {pin.title}")
    return 0


def cmd_pins(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    pins = list_pins(conn, include_archived=bool(args.all))
    if args.json:
        print(json.dumps([asdict(p) for p in pins], indent=2, ensure_ascii=False))
        return 0
    if not pins:
        print("nothing pinned")
    now = now_ms()
    for pin in pins:
        source = f", from {pin.source}" if pin.source else ""
        gone = f", unpinned by {pin.archived_by}" if pin.archived_at is not None else ""
        age = convert_ms_to_age(now - pin.created_at)
        print(f"#{pin.id:<4} {age:>4} ago, by {pin.pinned_by}{source}{gone}  {pin.title}")
        print(f"      {pin.kind}: " + pin.body.replace("\n", "\n" + " " * 12))
    return 0


def cmd_backfill_artifacts(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    """Pins the Artifact publishes of the last days that happened before the hook did, as the hook would have."""
    scan = scan_transcripts(Path(args.projects), now_ms() - int(args.days) * DAY_MS)
    with transaction(conn):
        result = backfill_artifact_publishes(conn, config, scan.publishes)
    pins = {pin.id: pin for _, pin in result.stored}
    for pin_id in sorted(pins):
        pin = get_pin(conn, pin_id)
        first = datetime.fromtimestamp(pin.created_at / 1000).strftime("%Y-%m-%d %H:%M")
        last = datetime.fromtimestamp(pin.shown_at / 1000).strftime("%Y-%m-%d %H:%M")
        print(f"#{pin.id:<4} {first} .. {last}  by {pin.pinned_by:<10} {pin.title}  {pin.body}")
    for line in result.refused:
        print(f"refused {line}")
    print(
        f"{len(scan.publishes)} publishes found, {len(result.stored)} pinned as {len(pins)} pins; left out: "
        f"{result.locked} from private tabs, {result.unknown_tab} from tabs bridge does not know, "
        f"{result.held_by_page} shown as a registered page; {scan.undated} results with no time skipped"
    )
    return 0
