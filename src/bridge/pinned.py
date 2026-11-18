import html
import sqlite3
import urllib.parse
from dataclasses import dataclass
from importlib.resources import files

from bridge.pages import Page, list_pages
from bridge.pins import Pin, list_pins, map_ms_to_clock_html, map_ms_to_day, map_pin_to_html

PAGE_SIZE = 30
MAX_LIMIT = 1000
QUERY_MAX_CHARS = 200
KINDS = ("pin", "page")
ITEMS_PLACEHOLDER = "<!--PINS-->"
TOTAL_PLACEHOLDER = "<!--TOTAL-->"
EMPTY_PLACEHOLDER = "<!--EMPTY-->"


@dataclass(frozen=True)
class PinnedRequest:
    query: str = ""
    kind: str = ""
    offset: int = 0
    limit: int = PAGE_SIZE


@dataclass(frozen=True)
class PinnedItem:
    key: str
    html: str


@dataclass(frozen=True)
class PinnedSlice:
    items: list[PinnedItem]
    total: int


def map_query_string_to_request(query_string: str) -> PinnedRequest:
    """ValueError names the first parameter that is out of range."""
    params = urllib.parse.parse_qs(query_string, keep_blank_values=True)

    def last(name: str) -> str:
        values = params.get(name)
        return values[-1] if values else ""

    query = last("q").strip()
    if len(query) > QUERY_MAX_CHARS:
        raise ValueError(f"q is longer than {QUERY_MAX_CHARS} chars")
    kind = last("kind")
    if kind not in ("", *KINDS):
        raise ValueError(f"kind is one of {', '.join(KINDS)}")
    offset = _parse_count("offset", last("offset") or "0", 0, 10**9)
    limit = _parse_count("limit", last("limit") or str(PAGE_SIZE), 1, MAX_LIMIT)
    return PinnedRequest(query, kind, offset, limit)


def _parse_count(name: str, text: str, low: int, high: int) -> int:
    if not text.isascii() or not text.isdigit() or not low <= int(text) <= high:
        raise ValueError(f"{name} is a whole number from {low} to {high}")
    return int(text)


def list_pinned_items(conn: sqlite3.Connection, request: PinnedRequest) -> PinnedSlice:
    """Live pins by their last publish or pin time and live pages by their last update, newest first, matching the
    query."""
    needle = request.query.casefold()
    entries: list[tuple[tuple[int, int, int, str], Pin | Page]] = []
    if request.kind != "page":
        for pin in list_pins(conn):
            if needle in f"{pin.title}\n{pin.body}\n{pin.source or ''}".casefold():
                entries.append(((-pin.shown_at, 0, -pin.id, ""), pin))
    if request.kind != "pin":
        for page in list_pages(conn):
            if needle in f"{page.slug}\n{page.title}".casefold():
                entries.append(((-page.updated_at, 1, 0, page.slug), page))
    entries.sort(key=lambda entry: entry[0])
    chosen = entries[request.offset : request.offset + request.limit]
    return PinnedSlice([_map_entry_to_item(entry) for _, entry in chosen], len(entries))


def _map_entry_to_item(entry: Pin | Page) -> PinnedItem:
    if isinstance(entry, Pin):
        return PinnedItem(f"pin-{entry.id}", map_pin_to_html(entry))
    return PinnedItem(f"page-{entry.slug}", map_page_to_html(entry))


def map_page_to_html(page: Page) -> str:
    e = html.escape
    meta = [
        f'<span class="id" title="source: {e(page.source_dir)}">/p/{e(page.slug)}</span>',
        "shared" if page.shared else "private",
    ]
    meta.append(f"by {e(page.created_by)}")
    meta.append(f"updated {map_ms_to_clock_html(page.updated_at)}")
    return (
        f'<article class="pin page" id="page-{e(page.slug)}" data-day="{e(map_ms_to_day(page.updated_at))}">'
        '<h2><span class="badge">Page</span>'
        f'<a class="link" href="/p/{e(page.slug)}">{e(page.title)}</a></h2>'
        f'<div class="meta">{" · ".join(meta)}</div></article>'
    )


def map_slice_to_json(pinned: PinnedSlice) -> dict[str, object]:
    return {"items": [{"key": item.key, "html": item.html} for item in pinned.items], "total": pinned.total}


def map_slice_to_page(pinned: PinnedSlice, request: PinnedRequest) -> str:
    template = files("bridge").joinpath("web/pinned.html").read_text()
    hidden = " hidden" if pinned.items else ""
    empty = f'<p id="empty" class="empty"{hidden}>{_map_request_to_empty_text(request)}</p>'
    return (
        template.replace(ITEMS_PLACEHOLDER, "\n".join(item.html for item in pinned.items), 1)
        .replace(TOTAL_PLACEHOLDER, str(pinned.total), 1)
        .replace(EMPTY_PLACEHOLDER, empty, 1)
    )


def _map_request_to_empty_text(request: PinnedRequest) -> str:
    if request.query:
        return "Nothing matches."
    return "No pages yet." if request.kind == "page" else "Nothing pinned yet."
