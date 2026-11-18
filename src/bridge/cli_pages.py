import argparse
import sqlite3
import sys
from pathlib import Path

from bridge.config import Config
from bridge.db import now_ms, transaction
from bridge.identity import current_party
from bridge.pages import (
    Page,
    add_doc_style_to_page,
    add_page,
    archive_page,
    build_republish_plan,
    find_html_file,
    list_pages,
    set_page_url,
    share_page,
)
from bridge.serve import DEFAULT_PORT

PAGES_BASE_URL = f"http://127.0.0.1:{DEFAULT_PORT}/p/"


def add_page_commands(sub: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:  # pyright: ignore[reportPrivateUsage]
    page = sub.add_parser(
        "page",
        help="register a page published as a claude.ai artifact under a stable bridge link, /p/<slug>",
    )
    actions = page.add_subparsers(dest="page_action", required=True)

    p = actions.add_parser("add", help="register a page by its lasting source folder (never /tmp)")
    p.add_argument("slug", help="lowercase letters, digits and dashes, up to 60")
    p.add_argument("--title", required=True)
    p.add_argument("--source", required=True, help="the folder that holds index.html or a single .html file")
    p.add_argument("--url", help="its claude.ai artifact link, when already published")
    p.add_argument("--shared", action="store_true", help="colleagues have the claude.ai link")
    p.set_defaults(func=cmd_page_add)

    p = actions.add_parser("set-url", help="the page's claude.ai artifact link, after a (re)publish")
    p.add_argument("slug")
    p.add_argument("url")
    p.set_defaults(func=cmd_page_set_url)

    p = actions.add_parser("share", help="mark the page as shared with colleagues; --off to unmark")
    p.add_argument("slug")
    p.add_argument("--off", action="store_true")
    p.set_defaults(func=cmd_page_share)

    p = actions.add_parser("archive", help="retire the page: /p/<slug> stops redirecting and the plan skips it")
    p.add_argument("slug")
    p.set_defaults(func=cmd_page_archive)

    p = sub.add_parser("pages", help="the registered pages; --republish-plan after an Anthropic account switch")
    p.add_argument(
        "--republish-plan",
        action="store_true",
        help="one line per live page, tab-separated: slug, the HTML file to publish, current url or '-'",
    )
    p.set_defaults(func=cmd_pages)


def cmd_page_add(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    by = current_party(conn).handle
    with transaction(conn):
        page = add_page(
            conn, str(args.slug), str(args.title), str(args.source), args.url, bool(args.shared), by, now_ms()
        )
        styled = add_doc_style_to_page(page.source_dir)
    if styled is not None:
        print(styled)
    html_file = find_html_file(Path(page.source_dir).expanduser())
    if html_file is not None:
        print(f"{PAGES_BASE_URL}{page.slug} → {html_file}")
    else:
        print(
            f"{PAGES_BASE_URL}{page.slug} → no index.html in {page.source_dir} yet. The link opens nothing until it "
            "is there; bridge refuses messages, posts and pins citing it before then."
        )
    return 0


def cmd_page_set_url(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    with transaction(conn):
        page = set_page_url(conn, str(args.slug), str(args.url), now_ms())
    print(f"{PAGES_BASE_URL}{page.slug} → {page.artifact_url}")
    return 0


def cmd_page_share(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    with transaction(conn):
        page = share_page(conn, str(args.slug), not args.off, now_ms())
    print(f"{page.slug}: {'shared' if page.shared else 'private'}")
    return 0


def cmd_page_archive(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    with transaction(conn):
        page = archive_page(conn, str(args.slug), now_ms())
    print(f"{page.slug} archived: {page.title}")
    return 0


def cmd_pages(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    pages = list_pages(conn)
    if args.republish_plan:
        return _print_republish_plan(pages)
    if not pages:
        print("no pages registered")
        return 0
    rows = [("SLUG", "TITLE", "URL", "SHARED", "SOURCE"), *map(_map_page_to_row, pages)]
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=False)) + "  " + row[4])
    return 0


def _map_page_to_row(page: Page) -> tuple[str, str, str, str, str]:
    title = page.title if len(page.title) <= 40 else page.title[:39] + "…"
    return (page.slug, title, page.artifact_url or "not published", "yes" if page.shared else "no", page.source_dir)


def _print_republish_plan(pages: list[Page]) -> int:
    lines, problems = build_republish_plan(pages)
    for line in lines:
        print(f"{line.slug}\t{line.html_file}\t{line.artifact_url or '-'}")
    for problem in problems:
        print(f"bridge: cannot republish {problem}", file=sys.stderr)
    return 1 if problems else 0
