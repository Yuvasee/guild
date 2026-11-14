import http.client
import sqlite3
import urllib.parse
from collections.abc import Generator
from contextlib import closing
from pathlib import Path

import pytest
from conftest import BridgeEnv, SpawnProcess, http_request
from test_leads import Team

from bridge.db import MIGRATIONS, connect
from bridge.pages import (
    PageError,
    add_page,
    archive_page,
    build_republish_plan,
    find_page,
    is_page_path,
    list_pages,
    map_request_path_to_slug,
    map_unpublished_page_to_html,
    set_page_url,
    share_page,
)
from bridge.pinned import PinnedRequest, list_pinned_items, map_page_to_html, map_slice_to_page
from bridge.pins import PinError, add_pin

ARTIFACT = "https://claude.ai/artifact/Ex4mpleArtifactId00004"
CODE_ARTIFACT = "https://claude.ai/code/artifact/3f1c2a9e-7b4d-4e8a-9c1f-2d3e4f5a6b7c"


@pytest.fixture
def conn(tmp_path: Path) -> Generator[sqlite3.Connection]:
    c = connect(tmp_path / "bridge.db")
    yield c
    c.close()


def make_source(root: Path, name: str, *html_files: str) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    for file_name in html_files:
        (folder / file_name).write_text("<!doctype html><title>x</title>")
    return folder


def test_add_stores_the_absolute_source_and_lists_by_slug(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_source(tmp_path, "b-report", "index.html")
    make_source(tmp_path, "a-explainer", "explainer.html")
    monkeypatch.chdir(tmp_path)
    b = add_page(conn, "b-report", "  Weekly\n report ", "b-report", None, False, "alex", 1000)
    a = add_page(
        conn, "a-explainer", "Explainer", str(tmp_path / "a-explainer"), CODE_ARTIFACT + "/", True, "eli", 2000
    )

    assert b.source_dir == str((tmp_path / "b-report").resolve()) and Path(b.source_dir).is_absolute()
    assert (b.title, b.artifact_url, b.shared, b.created_by) == ("Weekly report", None, False, "alex")
    assert (a.artifact_url, a.shared) == (CODE_ARTIFACT, True)
    assert [p.slug for p in list_pages(conn)] == ["a-explainer", "b-report"]


@pytest.mark.parametrize(
    ("slug", "url", "error"),
    [
        ("Upper", None, "lowercase"),
        ("-dash", None, "lowercase"),
        ("dash-", None, "lowercase"),
        ("under_score", None, "lowercase"),
        ("a" * 61, None, "up to 60"),
        ("ok", "https://claude.ai/chat/abc", "claude.ai artifact link"),
        ("ok", "http://claude.ai/artifact/abc", "claude.ai artifact link"),
        ("ok", "https://evil.example/claude.ai/artifact/abc", "claude.ai artifact link"),
        ("ok", "https://claude.ai/artifact/abc?x=1", "claude.ai artifact link"),
        ("ok", "https://claude.ai/artifact/abc\r\nSet-Cookie: x", "claude.ai artifact link"),
    ],
)
def test_bad_slug_or_url_is_refused(
    conn: sqlite3.Connection, tmp_path: Path, slug: str, url: str | None, error: str
) -> None:
    source = make_source(tmp_path, "src", "index.html")
    with pytest.raises(PageError, match=error):
        add_page(conn, slug, "T", str(source), url, False, "alex", 1000)
    assert list_pages(conn) == []


def test_a_60_char_slug_is_fine(conn: sqlite3.Connection, tmp_path: Path) -> None:
    source = make_source(tmp_path, "src", "index.html")
    assert add_page(conn, "a" * 60, "T", str(source), None, False, "alex", 1000).slug == "a" * 60


def test_source_must_last_and_hold_one_page(conn: sqlite3.Connection, tmp_path: Path) -> None:
    two = make_source(tmp_path, "two", "a.html", "b.html")
    empty = make_source(tmp_path, "empty")
    (tmp_path / "file.html").write_text("x")
    with pytest.raises(PageError, match=r"neither index\.html nor a single \.html"):
        add_page(conn, "two", "T", str(two), None, False, "alex", 1000)
    with pytest.raises(PageError, match=r"neither index\.html nor a single \.html"):
        add_page(conn, "empty", "T", str(empty), None, False, "alex", 1000)
    with pytest.raises(PageError, match="not a directory"):
        add_page(conn, "f", "T", str(tmp_path / "file.html"), None, False, "alex", 1000)
    with pytest.raises(PageError, match="not a directory"):
        add_page(conn, "f", "T", str(tmp_path / "missing"), None, False, "alex", 1000)
    with pytest.raises(PageError, match="under /tmp"):
        add_page(conn, "t", "T", "/tmp/whatever", None, False, "alex", 1000)
    with pytest.raises(PageError, match="a page needs a title"):
        add_page(conn, "t", "  ", str(two), None, False, "alex", 1000)
    tabbed = make_source(tmp_path, "week\tbackup", "index.html")
    with pytest.raises(PageError, match="control character"):
        add_page(conn, "tab", "T", str(tabbed), None, False, "alex", 1000)
    (two / "index.html").write_text("x")
    assert add_page(conn, "two", "T", str(two), None, False, "alex", 1000).slug == "two"


def test_set_url_share_archive_and_readd(conn: sqlite3.Connection, tmp_path: Path) -> None:
    source = make_source(tmp_path, "src", "index.html")
    add_page(conn, "report", "Report", str(source), None, False, "alex", 1000)
    with pytest.raises(PageError, match="already registered"):
        add_page(conn, "report", "Again", str(source), None, False, "alex", 1500)

    assert set_page_url(conn, "report", ARTIFACT, 2000).artifact_url == ARTIFACT
    assert share_page(conn, "report", True, 3000).shared
    assert not share_page(conn, "report", False, 3500).shared
    with pytest.raises(PageError, match=r"claude\.ai artifact link"):
        set_page_url(conn, "report", "https://example.com/x", 4000)
    with pytest.raises(PageError, match="no page 'nope'"):
        set_page_url(conn, "nope", ARTIFACT, 4000)

    archived = archive_page(conn, "report", 5000)
    assert archived.archived_at == 5000 and list_pages(conn) == []
    for change in (
        lambda: set_page_url(conn, "report", ARTIFACT, 6000),
        lambda: share_page(conn, "report", True, 6000),
        lambda: archive_page(conn, "report", 6000),
    ):
        with pytest.raises(PageError, match="archived"):
            change()

    again = add_page(conn, "report", "Report v2", str(source), None, True, "boris", 7000)
    assert (again.title, again.artifact_url, again.shared, again.archived_at) == ("Report v2", None, True, None)


def test_republish_plan_names_each_live_page_file_and_flags_a_lost_source(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    alpha = make_source(tmp_path, "alpha", "index.html", "extra.html")
    beta = make_source(tmp_path, "beta", "beta.html")
    gone = make_source(tmp_path, "gone", "index.html")
    retired = make_source(tmp_path, "retired", "index.html")
    add_page(conn, "alpha", "A", str(alpha), ARTIFACT, True, "alex", 1000)
    add_page(conn, "beta", "B", str(beta), None, False, "alex", 1000)
    add_page(conn, "gone", "G", str(gone), None, False, "alex", 1000)
    add_page(conn, "retired", "R", str(retired), None, False, "alex", 1000)
    archive_page(conn, "retired", 2000)
    (gone / "index.html").unlink()

    lines, problems = build_republish_plan(list_pages(conn))

    assert [(line.slug, line.html_file, line.artifact_url) for line in lines] == [
        ("alpha", alpha.resolve() / "index.html", ARTIFACT),
        ("beta", beta.resolve() / "beta.html", None),
    ]
    assert len(problems) == 1 and problems[0].startswith("gone: ")

    beta.chmod(0)
    try:
        lines, problems = build_republish_plan(list_pages(conn))
        assert [line.slug for line in lines] == ["alpha"] and [p.split(":")[0] for p in problems] == ["beta", "gone"]
        assert "neither index.html" in map_unpublished_page_to_html(list_pages(conn)[1])
    finally:
        beta.chmod(0o755)


def test_migration_63_creates_pages_and_survives_reconnect(tmp_path: Path) -> None:
    source = make_source(tmp_path, "src", "index.html")
    with closing(connect(tmp_path / "bridge.db")) as first:
        add_page(first, "report", "Report", str(source), None, False, "alex", 1000)
    with closing(connect(tmp_path / "bridge.db")) as again:
        assert [p.slug for p in list_pages(again)] == ["report"]
        assert again.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    assert "CREATE TABLE pages" in MIGRATIONS[62]


@pytest.mark.parametrize(
    ("path", "slug"),
    [
        ("/p", ""),
        ("/p/", ""),
        ("/p/?x=1", ""),
        ("/p/report", "report"),
        ("/p/report/", "report"),
        ("/p/report?utm=1", "report"),
        ("/p/Report", None),
        ("/p/a/b", None),
        ("/p/%3Cscript%3E", None),
        ("/p/" + "a" * 61, None),
    ],
)
def test_request_paths_map_to_slugs(path: str, slug: str | None) -> None:
    assert map_request_path_to_slug(path) == slug


def test_list_and_unpublished_pages_escape_agent_text(conn: sqlite3.Connection, tmp_path: Path) -> None:
    source = make_source(tmp_path, "<b>dir</b>", "index.html")
    page = add_page(conn, "xss", "<script>alert(1)</script>", str(source), None, True, "a&b", 1000)

    card = map_page_to_html(page)
    single = map_unpublished_page_to_html(page)

    for text in (card, single):
        assert "<script>alert" not in text and "<b>dir</b>" not in text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text and "&lt;b&gt;dir&lt;/b&gt;" in text
        assert "<!--PAGE-" not in text
    assert 'href="/p/xss"' in card and "shared" in card and "not published" in card and "by a&amp;b" in card
    assert '<span class="badge">Page</span>' in card and 'class="pin page"' in card
    assert "Not published yet" in single and "registered by a&amp;b" in single
    assert "bridge page set-url xss &lt;url&gt;" in single
    assert "<title>&lt;script&gt;alert(1)&lt;/script&gt;</title>" in single


def test_pin_links_accept_bridge_page_urls(conn: sqlite3.Connection, tmp_path: Path) -> None:
    assert is_page_path("/p/report") and is_page_path("/p/") and not is_page_path("/p/x y")
    source = str(make_source(tmp_path, "src", "index.html"))
    for slug in ("weekly-report", "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6"):
        add_page(conn, slug, slug, source, ARTIFACT, False, "alex", 500)
    add_pin(conn, "Report", "link", "http://127.0.0.1:8765/p/weekly-report", None, "alex", 1000)
    add_pin(conn, "Report short", "link", "/p/weekly-report", None, "alex", 2000)
    add_pin(conn, "Random slug", "link", "/p/a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6", None, "alex", 2500)
    with pytest.raises(PinError, match="/p/<slug>"):
        add_pin(conn, "Bad", "link", "/pinned", None, "alex", 3000)
    page = map_slice_to_page(list_pinned_items(conn, PinnedRequest()), PinnedRequest())
    assert 'href="http://127.0.0.1:8765/p/weekly-report"' in page and 'href="/p/weekly-report"' in page


def test_cli_registers_lists_and_plans(bridge_env: BridgeEnv, spawn_process: SpawnProcess, tmp_path: Path) -> None:
    pid = spawn_process()
    bridge_env.write_session_file(pid, "conv-pages")
    source = make_source(tmp_path, "pages/weekly", "index.html")
    other = make_source(tmp_path, "pages/notes", "notes.html")

    added = bridge_env.cli("page", "add", "weekly", "--title", "Weekly", "--source", str(source), pid=pid)
    assert added.returncode == 0, added.stderr
    assert "http://127.0.0.1:8765/p/weekly → not published yet" in added.stdout
    shared = bridge_env.cli(
        "page", "add", "notes", "--title", "Notes", "--source", str(other), "--url", ARTIFACT, "--shared", pid=pid
    )
    assert shared.returncode == 0, shared.stderr
    tmp = bridge_env.cli("page", "add", "t", "--title", "T", "--source", "/tmp", pid=pid)
    assert tmp.returncode == 2 and "under /tmp" in tmp.stderr
    outside = bridge_env.cli("page", "add", "x", "--title", "X", "--source", str(source))
    assert outside.returncode == 2 and "not inside a Claude Code session" in outside.stderr

    table = bridge_env.cli("pages").stdout.splitlines()
    assert table[0].split() == ["SLUG", "TITLE", "URL", "SHARED", "SOURCE"]
    assert table[1].split() == ["notes", "Notes", ARTIFACT, "yes", str(other.resolve())]
    assert table[2].split() == ["weekly", "Weekly", "not", "published", "no", str(source.resolve())]

    plan = bridge_env.cli("pages", "--republish-plan")
    assert plan.returncode == 0, plan.stderr
    assert plan.stdout.splitlines() == [
        f"notes\t{other.resolve() / 'notes.html'}\t{ARTIFACT}",
        f"weekly\t{source.resolve() / 'index.html'}\t-",
    ]

    assert bridge_env.cli("page", "set-url", "weekly", CODE_ARTIFACT).returncode == 0
    bad = bridge_env.cli("page", "set-url", "weekly", "https://example.com/x")
    assert bad.returncode == 2 and "claude.ai artifact link" in bad.stderr
    assert bridge_env.cli("page", "share", "notes", "--off").stdout.strip() == "notes: private"
    assert bridge_env.cli("page", "archive", "notes").returncode == 0
    (source / "index.html").unlink()
    broken = bridge_env.cli("pages", "--republish-plan")
    assert broken.returncode == 1 and broken.stdout == "" and "cannot republish weekly: " in broken.stderr


def request_without_redirect(url: str) -> tuple[int, str | None, str]:
    parts = urllib.parse.urlsplit(url)
    connection = http.client.HTTPConnection(parts.netloc, timeout=5)
    try:
        connection.request("GET", parts.path + (f"?{parts.query}" if parts.query else ""))
        response = connection.getresponse()
        return response.status, response.getheader("Location"), response.read().decode()
    finally:
        connection.close()


def test_routes_redirect_list_and_explain(bridge_env: BridgeEnv, dashboard: str, tmp_path: Path) -> None:
    source = make_source(tmp_path, "src", "index.html")
    with closing(bridge_env.connect()) as db:
        add_page(db, "live", "Live <i>page</i>", str(source), ARTIFACT, True, "alex", 1000)
        add_page(db, "draft", "Draft", str(source), None, False, "alex", 1000)
        add_page(db, "old", "Old", str(source), ARTIFACT, False, "alex", 1000)
        archive_page(db, "old", 2000)
        assert find_page(db, "old") is not None

    assert request_without_redirect(dashboard + "/p/live") == (302, ARTIFACT, "")
    assert request_without_redirect(dashboard + "/p/live/")[:2] == (302, ARTIFACT)
    status, location, draft = request_without_redirect(dashboard + "/p/draft")
    assert (status, location) == (200, None)
    assert "Not published yet" in draft and str(source.resolve()) in draft
    status, _, old = request_without_redirect(dashboard + "/p/old")
    assert status == 410 and "was archived" in old
    status, _, missing = request_without_redirect(dashboard + "/p/%3Cscript%3E")
    assert status == 404 and "<script>" not in missing and "is not a registered page" in missing

    for listing in ("/p", "/p/", "/p/?x=1"):
        assert request_without_redirect(dashboard + listing) == (302, "/pinned?kind=page", "")
    pinned = http_request(dashboard + "/pinned?kind=page")
    assert "<title>Pinned</title>" in pinned and "Live &lt;i&gt;page&lt;/i&gt;" in pinned
    assert 'href="/p/draft"' in pinned and 'href="/p/old"' not in pinned
    assert 'href="/pinned?kind=page">All pages</a>' in missing
    guild = http_request(dashboard + "/guild")
    assert 'url: "/pinned"' in guild and 'url: "/p/"' not in guild and "PAGES_LINK" not in guild


def test_a_message_post_or_pin_citing_a_page_that_would_not_open_is_refused(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, tmp_path: Path
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    source = make_source(tmp_path, "src", "index.html")
    with closing(bridge_env.connect()) as db:
        add_page(db, "draft", "Draft", str(source), None, False, "alex", 1000)
        add_page(db, "live", "Live", str(source), ARTIFACT, False, "alex", 1000)

    refused = bridge_env.cli("send", "alex", "see http://127.0.0.1:8765/p/draft and /p/nope", pid=team.b)
    posted = bridge_env.cli("post", "done", "report: /p/draft", pid=team.b)
    sent = bridge_env.cli("send", "alex", "see /p/live", pid=team.b)

    assert refused.returncode != 0 and "/p/draft is not published yet" in refused.stderr
    assert "set-url draft" in refused.stderr and "/p/nope" in refused.stderr
    assert posted.returncode != 0 and "/p/draft is not published yet" in posted.stderr
    assert sent.returncode == 0, sent.stderr
    with closing(bridge_env.connect()) as db, pytest.raises(PinError, match="not published yet"):
        add_pin(db, "Draft", "link", "/p/draft", None, "xavi", 2000)


def test_opening_an_unpublished_page_tells_its_owner_once(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str, tmp_path: Path
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    with closing(bridge_env.connect()) as db:
        add_page(db, "draft", "Draft", str(make_source(tmp_path, "src", "index.html")), None, False, "alex", 1000)

    for _ in range(2):
        assert request_without_redirect(dashboard + "/p/draft")[0] == 200

    with closing(bridge_env.connect()) as db:
        told = db.execute(
            "SELECT m.body FROM messages m JOIN message_recipients r ON r.message_id = m.id"
            " WHERE m.from_handle = 'bridge' AND r.handle = 'alex' AND m.body LIKE '%''draft''%'"
        ).fetchall()
    assert len(told) == 1 and "set-url draft" in told[0][0]
