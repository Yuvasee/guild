import html
import re
import sqlite3
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from pathlib import Path

from bridge.errors import UsageError

SLUG_MAX_CHARS = 60
TITLE_MAX_CHARS = 200
_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")
_ARTIFACT_URL = re.compile(r"https://claude\.ai/(?:code/)?artifact/[A-Za-z0-9_-]{1,100}")
_PAGE_PATH = re.compile(rf"/p/(?:[a-z0-9-]{{1,{SLUG_MAX_CHARS}}}/?)?")
# The OS clears these, and the whole point of a registered source is to outlive the session that wrote it.
_TEMP_ROOTS = (Path("/tmp"), Path("/private/tmp"))
PAGE_TITLE_PLACEHOLDER = "<!--PAGE-TITLE-->"
PAGE_SUB_PLACEHOLDER = "<!--PAGE-SUB-->"
PAGE_BODY_PLACEHOLDER = "<!--PAGE-BODY-->"
ARTIFACT_SOURCE = "artifact"
"""The source of a link pin that a session's Artifact publish made, not a person."""


@dataclass(frozen=True)
class Page:
    slug: str
    title: str
    source_dir: str
    artifact_url: str | None
    shared: bool
    created_by: str
    updated_at: int
    archived_at: int | None


@dataclass(frozen=True)
class PlanLine:
    slug: str
    html_file: Path
    artifact_url: str | None


class PageError(UsageError):
    pass


def is_artifact_url(text: str) -> bool:
    return _ARTIFACT_URL.fullmatch(text) is not None


def is_page_path(text: str) -> bool:
    """A link to this bridge's own page registry: /p/ or /p/<slug>."""
    return _PAGE_PATH.fullmatch(text) is not None


def check_slug(slug: str) -> str:
    if len(slug) > SLUG_MAX_CHARS or _SLUG.fullmatch(slug) is None:
        raise PageError(
            f"a slug is lowercase letters, digits and dashes, no dash at either end, up to {SLUG_MAX_CHARS} chars: "
            f"{slug[:80]!r}"
        )
    return slug


def check_artifact_url(url: str) -> str:
    url = url.strip().removesuffix("/")
    if not is_artifact_url(url):
        raise PageError(
            "the URL is a claude.ai artifact link, https://claude.ai/artifact/<id> or "
            f"https://claude.ai/code/artifact/<id>: {url[:120]!r}"
        )
    return url


def check_source_dir(source: str) -> Path:
    """The source as an absolute path; it must be a lasting directory that holds the page's HTML file."""
    path = Path(source).expanduser().resolve()
    if any(c < " " for c in str(path)):
        raise PageError("the source path holds a tab, newline or other control character; rename the folder")
    if any(path == root or path.is_relative_to(root) for root in _TEMP_ROOTS):
        raise PageError(
            f"{path} is under /tmp, which the OS clears; keep the source in the pages folder, one subfolder per slug"
        )
    if not path.is_dir():
        raise PageError(f"{path} is not a directory")
    if find_html_file(path) is None:
        raise PageError(f"{path} holds neither index.html nor a single .html file")
    return path


def find_html_file(source_dir: Path) -> Path | None:
    """index.html, else the only .html file; None when there is neither."""
    try:
        index = source_dir / "index.html"
        if index.is_file():
            return index
        found = [p for p in source_dir.glob("*.html") if p.is_file()]
    except OSError:
        return None
    return found[0] if len(found) == 1 else None


def add_page(
    conn: sqlite3.Connection,
    slug: str,
    title: str,
    source: str,
    artifact_url: str | None,
    shared: bool,
    by: str,
    now: int,
) -> Page:
    """A slug that was archived is taken over by the new page."""
    slug = check_slug(slug)
    title = " ".join(title.split())
    if not title:
        raise PageError("a page needs a title")
    if len(title) > TITLE_MAX_CHARS:
        raise PageError(f"title is {len(title)} chars; keep it under {TITLE_MAX_CHARS}")
    source_dir = check_source_dir(source)
    url = check_artifact_url(artifact_url) if artifact_url is not None else None
    existing = find_page(conn, slug)
    if existing is not None and existing.archived_at is None:
        raise PageError(
            f"page {slug} is already registered; change it with `bridge page set-url` or `bridge page share`"
        )
    conn.execute(
        """
        INSERT INTO pages (slug, title, source_dir, artifact_url, shared, created_by, updated_at, archived_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
        ON CONFLICT (slug) DO UPDATE SET title = excluded.title, source_dir = excluded.source_dir,
          artifact_url = excluded.artifact_url, shared = excluded.shared, created_by = excluded.created_by,
          updated_at = excluded.updated_at, archived_at = NULL
        """,
        (slug, title, str(source_dir), url, int(shared), by, now),
    )
    _archive_published_pin(conn, url, slug, now)
    return get_live_page(conn, slug)


def set_page_url(conn: sqlite3.Connection, slug: str, artifact_url: str, now: int) -> Page:
    url = check_artifact_url(artifact_url)
    get_live_page(conn, slug)
    conn.execute("UPDATE pages SET artifact_url = ?, updated_at = ? WHERE slug = ?", (url, now, slug))
    _archive_published_pin(conn, url, slug, now)
    return get_live_page(conn, slug)


def _archive_published_pin(conn: sqlite3.Connection, url: str | None, slug: str, now: int) -> None:
    """The publish hook pins an artifact before its page is registered; from now on /pinned shows it as the page."""
    conn.execute(
        "UPDATE pins SET archived_at = ?, archived_by = ? WHERE kind = 'link' AND body = ? AND source = ? "
        "AND archived_at IS NULL",
        (now, f"page {slug}", url, ARTIFACT_SOURCE),
    )


def share_page(conn: sqlite3.Connection, slug: str, shared: bool, now: int) -> Page:
    get_live_page(conn, slug)
    conn.execute("UPDATE pages SET shared = ?, updated_at = ? WHERE slug = ?", (int(shared), now, slug))
    return get_live_page(conn, slug)


def archive_page(conn: sqlite3.Connection, slug: str, now: int) -> Page:
    get_live_page(conn, slug)
    conn.execute("UPDATE pages SET archived_at = ?, updated_at = ? WHERE slug = ?", (now, now, slug))
    page = find_page(conn, slug)
    assert page is not None
    return page


def find_page(conn: sqlite3.Connection, slug: str) -> Page | None:
    row = conn.execute("SELECT * FROM pages WHERE slug = ?", (slug,)).fetchone()
    return _map_row_to_page(row) if row is not None else None


def get_live_page(conn: sqlite3.Connection, slug: str) -> Page:
    page = find_page(conn, slug)
    if page is None:
        raise PageError(f"no page {slug[:80]!r}; `bridge pages` lists them")
    if page.archived_at is not None:
        raise PageError(f"page {slug} is archived; `bridge page add` registers it again")
    return page


_CITED_PAGE = re.compile(rf"/p/([a-z0-9-]{{1,{SLUG_MAX_CHARS}}})")


def find_unpublished_citations(conn: sqlite3.Connection, text: str) -> list[str]:
    """Slugs a text links to under /p/ that would not open a page: unknown, archived, or with no artifact link yet."""
    unready: list[str] = []
    for slug in dict.fromkeys(_CITED_PAGE.findall(text)):
        page = find_page(conn, slug)
        if page is None or page.archived_at is not None or not page.artifact_url:
            unready.append(slug)
    return unready


def map_unpublished_citations_to_error(slugs: list[str]) -> str:
    return (
        f"/p/{slugs[0]} is not published yet: publish the page, run `bridge page set-url {slugs[0]} <artifact url>`, "
        "check the link opens, then send it"
        + (f" (also not ready: {', '.join('/p/' + s for s in slugs[1:])})" if slugs[1:] else "")
    )


def list_pages(conn: sqlite3.Connection) -> list[Page]:
    """Live pages by slug."""
    rows = conn.execute("SELECT * FROM pages WHERE archived_at IS NULL ORDER BY slug").fetchall()
    return [_map_row_to_page(row) for row in rows]


def build_republish_plan(pages: list[Page]) -> tuple[list[PlanLine], list[str]]:
    """What to publish for each live page, and one problem per page whose source no longer holds its HTML file."""
    lines: list[PlanLine] = []
    problems: list[str] = []
    for page in pages:
        html_file = find_html_file(Path(page.source_dir))
        if html_file is None:
            problems.append(f"{page.slug}: {page.source_dir} holds neither index.html nor a single .html file")
        else:
            lines.append(PlanLine(page.slug, html_file, page.artifact_url))
    return lines, problems


def _map_row_to_page(row: sqlite3.Row) -> Page:
    return Page(
        slug=str(row["slug"]),
        title=str(row["title"]),
        source_dir=str(row["source_dir"]),
        artifact_url=row["artifact_url"],
        shared=bool(row["shared"]),
        created_by=str(row["created_by"]),
        updated_at=int(row["updated_at"]),
        archived_at=row["archived_at"],
    )


def find_redirect(page: Page | None) -> str | None:
    """Where /p/<slug> sends the browser; re-checked here because the Location header comes from the database."""
    if page is None or page.archived_at is not None or page.artifact_url is None:
        return None
    return page.artifact_url if is_artifact_url(page.artifact_url) else None


def map_request_path_to_slug(path: str) -> str | None:
    """'' for /p or /p/ (now Pinned), the slug for /p/<slug>, None for anything else under /p/."""
    bare = urllib.parse.urlsplit(path).path
    if bare in ("/p", "/p/"):
        return ""
    match = re.fullmatch(r"/p/([^/]*)/?", bare)
    if match is None or len(match.group(1)) > SLUG_MAX_CHARS or _SLUG.fullmatch(match.group(1)) is None:
        return None
    return match.group(1)


def map_unpublished_page_to_html(page: Page) -> str:
    e = html.escape
    html_file = find_html_file(Path(page.source_dir))
    file_line = (
        f"<p>Publish <code>{e(str(html_file))}</code> with the Artifact tool, then "
        f"<code>bridge page set-url {e(page.slug)} &lt;url&gt;</code>.</p>"
        if html_file is not None
        else '<p class="todo">The source holds neither index.html nor a single .html file.</p>'
    )
    at = datetime.fromtimestamp(page.updated_at / 1000)
    body = (
        f'<article class="pin"><h2>Not published yet</h2>'
        f"<p>Source: <code>{e(page.source_dir)}</code></p>{file_line}"
        f'<div class="meta">registered by {e(page.created_by)} · updated '
        f'<time datetime="{at.isoformat(timespec="seconds")}">{e(at.strftime("%b %-d, %H:%M"))}</time></div></article>'
    )
    return _fill_template(page.title, f"/p/{page.slug}", body)


def map_missing_page_to_html(slug: str | None, archived: bool) -> str:
    shown = f"/p/{slug}" if slug else "This address"
    what = "was archived" if archived else "is not a registered page"
    body = f'<p class="empty">{html.escape(shown)} {what}. <a href="/pinned?kind=page">All pages</a></p>'
    return _fill_template("No such page", "", body)


def _fill_template(title: str, sub: str, body: str) -> str:
    template = files("bridge").joinpath("web/pages.html").read_text()
    return (
        template.replace(PAGE_TITLE_PLACEHOLDER, html.escape(title))
        .replace(PAGE_SUB_PLACEHOLDER, html.escape(sub), 1)
        .replace(PAGE_BODY_PLACEHOLDER, body, 1)
    )
