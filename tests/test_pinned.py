import json
import sqlite3
from collections.abc import Generator
from contextlib import closing
from pathlib import Path

import pytest
from conftest import BridgeEnv, http_request

from bridge.db import connect
from bridge.pages import add_page, archive_page, set_page_url
from bridge.pinned import (
    PAGE_SIZE,
    PinnedRequest,
    list_pinned_items,
    map_query_string_to_request,
    map_slice_to_page,
)
from bridge.pins import add_pin, archive_pin

ARTIFACT = "https://claude.ai/artifact/Ex4mpleArtifactId00004"


@pytest.fixture
def conn(tmp_path: Path) -> Generator[sqlite3.Connection]:
    c = connect(tmp_path / "bridge.db")
    yield c
    c.close()


@pytest.fixture
def source(tmp_path: Path) -> str:
    folder = tmp_path / "src"
    folder.mkdir()
    (folder / "index.html").write_text("<html></html>")
    return str(folder)


def keys(conn: sqlite3.Connection, request: PinnedRequest) -> list[str]:
    return [item.key for item in list_pinned_items(conn, request).items]


def test_pins_and_pages_interleave_newest_first(conn: sqlite3.Connection, source: str) -> None:
    oldest = add_pin(conn, "Release", "link", "https://x.example/r", None, "alex", 1000)
    add_page(conn, "report", "Report", source, None, False, "alex", 2000)
    newest = add_pin(conn, "Logs", "cmd", "kubectl logs", None, "alex", 3000)
    add_page(conn, "gone", "Gone", source, None, False, "alex", 4000)
    archive_page(conn, "gone", 5000)
    unpinned = add_pin(conn, "Old", "note", "x", None, "alex", 6000)
    archive_pin(conn, unpinned.id, "alex", 7000)

    assert keys(conn, PinnedRequest()) == [f"pin-{newest.id}", "page-report", f"pin-{oldest.id}"]
    set_page_url(conn, "report", ARTIFACT, 8000)
    assert keys(conn, PinnedRequest()) == ["page-report", f"pin-{newest.id}", f"pin-{oldest.id}"]
    assert list_pinned_items(conn, PinnedRequest()).total == 3


def test_kind_filter_shows_only_pins_or_only_pages(conn: sqlite3.Connection, source: str) -> None:
    pin = add_pin(conn, "Release", "link", "https://x.example/r", None, "alex", 1000)
    add_page(conn, "report", "Report", source, None, False, "alex", 2000)

    assert keys(conn, PinnedRequest(kind="pin")) == [f"pin-{pin.id}"]
    assert keys(conn, PinnedRequest(kind="page")) == ["page-report"]


def test_search_is_case_insensitive_over_pin_and_page_fields(conn: sqlite3.Connection, source: str) -> None:
    by_title = add_pin(conn, "Prod LOGS", "cmd", "kubectl logs", None, "alex", 1000)
    by_body = add_pin(conn, "Tail", "cmd", "stern api-logs", None, "alex", 2000)
    by_source = add_pin(conn, "Runbook", "note", "wiki page", "Ask #64 logs", "alex", 3000)
    add_pin(conn, "Other", "note", "nothing here", "alex", "logs-by-nobody", 4000)
    add_page(conn, "weekly-logs", "Weekly", source, None, False, "alex", 5000)
    add_page(conn, "audit", "Access Logs audit", source, None, False, "alex", 6000)
    add_page(conn, "plain", "Plain", source, None, False, "logs-author", 7000)

    found = keys(conn, PinnedRequest(query="logs"))
    assert found == ["page-audit", "page-weekly-logs", f"pin-{by_source.id}", f"pin-{by_body.id}", f"pin-{by_title.id}"]
    assert keys(conn, PinnedRequest(query="LOGS", kind="page")) == ["page-audit", "page-weekly-logs"]
    assert keys(conn, PinnedRequest(query="no such thing")) == []


def test_offset_and_limit_page_through_the_stream(conn: sqlite3.Connection, source: str) -> None:
    for n in range(PAGE_SIZE + 5):
        add_pin(conn, f"Pin {n}", "note", "x", None, "alex", 1000 + 2 * n)
    add_page(conn, "report", "Report", source, None, False, "alex", 1000 + 2 * (PAGE_SIZE + 2) + 1)

    first = list_pinned_items(conn, PinnedRequest())
    rest = list_pinned_items(conn, PinnedRequest(offset=PAGE_SIZE))
    assert len(first.items) == PAGE_SIZE and len(rest.items) == 6 and first.total == rest.total == PAGE_SIZE + 6
    every = [item.key for item in first.items + rest.items]
    assert len(set(every)) == PAGE_SIZE + 6 and every.index("page-report") == 2
    assert keys(conn, PinnedRequest(offset=1, limit=2)) == every[1:3]
    assert keys(conn, PinnedRequest(offset=500)) == []


@pytest.mark.parametrize(
    ("query_string", "expected"),
    [
        ("", PinnedRequest()),
        ("q=+Logs+&kind=page&offset=30&limit=60", PinnedRequest("Logs", "page", 30, 60)),
        ("kind=&q=", PinnedRequest()),
        ("kind=pin&kind=page", PinnedRequest(kind="page")),
    ],
)
def test_query_strings_map_to_requests(query_string: str, expected: PinnedRequest) -> None:
    assert map_query_string_to_request(query_string) == expected


@pytest.mark.parametrize(
    "query_string",
    ["kind=note", "offset=-1", "limit=0", "limit=1001", "offset=x", "limit=٣", "q=" + "x" * 201],
)
def test_bad_query_strings_are_refused(query_string: str) -> None:
    with pytest.raises(ValueError):
        map_query_string_to_request(query_string)


def test_page_cards_and_pins_escape_agent_text(conn: sqlite3.Connection, source: str) -> None:
    add_page(conn, "xss", "<img src=x onerror=alert(1)>", source, None, True, "<b>a</b>", 1000)
    add_pin(conn, "<script>alert(1)</script>", "note", "</p><i>", "<u>s</u>", "a&b", 2000)
    request = map_query_string_to_request("q=%3C")
    page = map_slice_to_page(list_pinned_items(conn, request), request)

    assert "<img" not in page and "<script>alert" not in page and "<b>a</b>" not in page and "<i>" not in page
    assert "&lt;img src=x onerror=alert(1)&gt;" in page and "by &lt;b&gt;a&lt;/b&gt;" in page
    assert '<span class="badge">Page</span><a class="link" href="/p/xss">' in page
    assert 'data-total="2"' in page


def test_empty_text_follows_the_request(conn: sqlite3.Connection) -> None:
    for request, text in (
        (PinnedRequest(), "Nothing pinned yet."),
        (PinnedRequest(kind="page"), "No pages yet."),
        (PinnedRequest(query="x"), "Nothing matches."),
    ):
        assert f'<p id="empty" class="empty">{text}</p>' in map_slice_to_page(list_pinned_items(conn, request), request)


def test_pinned_api_and_page_are_served(bridge_env: BridgeEnv, dashboard: str, tmp_path: Path) -> None:
    folder = tmp_path / "src"
    folder.mkdir()
    (folder / "index.html").write_text("<html></html>")
    with closing(bridge_env.connect()) as db:
        pin = add_pin(db, "<i>Release</i>", "cmd", "make release", "ask #64", "alex", 1000)
        add_page(db, "report", "Weekly report", str(folder), ARTIFACT, True, "alex", 2000)

    data = json.loads(http_request(dashboard + "/api/pinned?q=release&limit=5"))
    assert data["total"] == 1 and [item["key"] for item in data["items"]] == [f"pin-{pin.id}"]
    assert "&lt;i&gt;Release&lt;/i&gt;" in data["items"][0]["html"] and "data-copy" in data["items"][0]["html"]
    both = json.loads(http_request(dashboard + "/api/pinned"))
    assert [item["key"] for item in both["items"]] == ["page-report", f"pin-{pin.id}"]
    assert "kind is one of" in http_request(dashboard + "/api/pinned?kind=x", expect=400)

    page = http_request(dashboard + "/pinned?q=weekly")
    assert 'id="page-report"' in page and f'id="pin-{pin.id}"' not in page and 'id="search"' in page
