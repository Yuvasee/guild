import html
import os
import re
import sqlite3
import stat
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from pathlib import Path

from bridge.doc_nav import DOC_STYLE_ID, convert_page_to_styled
from bridge.errors import UsageError

DEFAULT_PORT = 8765
PAGES_BASE_URL = f"http://127.0.0.1:{DEFAULT_PORT}/p/"
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


def add_doc_style_to_page(source: str) -> str | None:
    """Writes the current shared doc style into the page's HTML file, so the published artifact carries it; what it
    did, or None when the page already had it."""
    html_file = find_html_file(Path(source))
    if html_file is None:
        raise PageError(f"{source} holds neither index.html nor a single .html file")
    try:
        text = html_file.read_text(encoding="utf-8")
        mode = stat.S_IMODE(html_file.stat().st_mode)
    except UnicodeDecodeError:
        return f"left {html_file} without the shared doc style: it is not UTF-8"
    except OSError as error:
        raise PageError(f"cannot read {html_file}: {error}") from error
    styled = convert_page_to_styled(text)
    if styled is None:
        return None
    did = "updated the shared doc style in" if DOC_STYLE_ID in text else "added the shared doc style to"
    tmp = html_file.with_name(f".{html_file.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(styled)
        os.chmod(tmp, mode)
        os.replace(tmp, html_file)
    except OSError as error:
        tmp.unlink(missing_ok=True)
        raise PageError(f"cannot write {html_file}: {error}") from error
    return f"{did} {html_file}"


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


_LOCAL_ORIGIN = rf"https?://(?:127\.0\.0\.1|localhost):{DEFAULT_PORT}"
_OTHER_URL = re.compile(rf"(?!{_LOCAL_ORIGIN}/)https?://\S+")
_LOCAL_CITATION = re.compile(rf"(?:{_LOCAL_ORIGIN}|(?<![\w.:/-]))/p/([a-z0-9-]{{1,{SLUG_MAX_CHARS}}})(?![\w-])")


def remove_other_urls(text: str) -> str:
    """The text without other sites' URLs: a /p/ path, slug or title in one, query and fragment too, is theirs."""
    return _OTHER_URL.sub(" ", text)


def find_local_citations(text: str) -> list[str]:
    """The slugs a text cites on this dashboard: /p/<slug> on its own or on the dashboard's address."""
    return list(dict.fromkeys(_LOCAL_CITATION.findall(remove_other_urls(text))))


def find_unready_citations(conn: sqlite3.Connection, text: str) -> list[str]:
    """Slugs a text links to on this dashboard (/p/<slug>, not another site's /p/) that would not open a page:
    unknown, archived, or with no local HTML file."""
    unready: list[str] = []
    for slug in find_local_citations(text):
        page = find_page(conn, slug)
        if page is None or page.archived_at is not None or find_html_file(Path(page.source_dir).expanduser()) is None:
            unready.append(slug)
    return unready


def map_unready_citations_to_error(slugs: list[str]) -> str:
    return (
        f"/p/{slugs[0]} opens nothing: register it with `bridge page add` and put its index.html in the source "
        "folder, check the link opens, then send it"
        + (f" (also not ready: {', '.join('/p/' + s for s in slugs[1:])})" if slugs[1:] else "")
    )


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# Not a part of a file name, path or compound such as pages.py or doc-style.
_PAGE_WORDS = re.compile(
    r"(?<![\w./-])(?:page|analysis|explainer|write-?up|doc|document)(?![\w/-]|\.\w)", re.IGNORECASE
)
_WEB_URL = re.compile(r"https?://\S")
_CODE_SPAN = re.compile(r"`[^`\n]+`")
NO_LINK_FLAG = "--no-link"


def map_url_to_page_slug(url: str) -> str | None:
    """The slug of a link to this Mac's dashboard /p/<slug>; None for any other link, another local server's too."""
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or parts.hostname not in _LOOPBACK_HOSTS or port != DEFAULT_PORT:
        return None
    return map_request_path_to_slug(parts.path) or None


def find_unlinked_page_mention(
    conn: sqlite3.Connection, text: str, link_urls: list[str], no_link: bool, options: Sequence[str] = ()
) -> str | None:
    """Why a post that names a page must carry its link, or None. A /p/<slug> needs its own --link; a live page's
    title (two words or more) or dashed slug needs one unless --no-link; so do the words page, analysis, explainer,
    write-up or doc(ument) when the post has no --link, nor a URL in its text (title and body) that the card shows
    as a link: one in an answer button or a `code` span is not."""
    if unready := find_unready_citations(conn, "\n".join([text, *options, *link_urls])):
        return map_unready_citations_to_error(unready)
    linked = {slug for url in link_urls if (slug := map_url_to_page_slug(url))}
    artifacts = set(link_urls)
    clickable = _WEB_URL.search(_CODE_SPAN.sub(" ", text))
    text = remove_other_urls("\n".join([text, *options]))
    for slug in find_local_citations(text):
        if slug not in linked:
            return (
                f"the post names /p/{slug} but no --link opens it; add --link 'Title={PAGES_BASE_URL}{slug}' "
                "so the owner's card shows the page as a link"
            )
    if no_link:
        return None
    escape = f"; if the words name no page, add {NO_LINK_FLAG}"
    lowered = text.casefold()
    for page in list_pages(conn):
        if page.slug in linked or page.artifact_url in artifacts:
            continue
        by_slug = "-" in page.slug and re.search(rf"(?<![\w-]){re.escape(page.slug)}(?![\w-])", text)
        title = page.title.casefold()
        by_title = len(title.split()) > 1 and re.search(rf"(?<!\w){re.escape(title)}(?!\w)", lowered)
        if by_slug or by_title:
            return (
                f"the post names the page {page.title!r} but carries no link to it; add "
                f"--link '{page.title}={PAGES_BASE_URL}{page.slug}'{escape}"
            )
    if not link_urls and not clickable and (word := _PAGE_WORDS.search(text)):
        return (
            f"the post says {word.group(0)!r} but carries no link; add --link 'Title=https://…' (a page of ours: "
            f"--link 'Title={PAGES_BASE_URL}<slug>', `bridge pages` lists them){escape}"
        )
    return None


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


def map_request_path_to_page_file(path: str) -> tuple[str, str] | None:
    """(slug, file path inside the page's folder) for /p/<slug>/<file>; None for anything else."""
    bare = urllib.parse.unquote(urllib.parse.urlsplit(path).path)
    match = re.fullmatch(r"/p/([^/]+)/(.+)", bare)
    if match is None or _SLUG.fullmatch(match.group(1)) is None or len(match.group(1)) > SLUG_MAX_CHARS:
        return None
    return match.group(1), match.group(2)


def find_page_file(page: Page, inner: str) -> Path | None:
    """A file inside the page's own folder; None for anything outside it, such as '../'."""
    try:
        root = Path(page.source_dir).expanduser().resolve()
        found = (root / inner).resolve()
    except (OSError, RuntimeError):
        return None
    return found if found.is_relative_to(root) and found.is_file() else None


def map_request_path_to_slug(path: str) -> str | None:
    """'' for /p or /p/ (now Pinned), the slug for /p/<slug>, None for anything else under /p/."""
    bare = urllib.parse.urlsplit(path).path
    if bare in ("/p", "/p/"):
        return ""
    match = re.fullmatch(r"/p/([^/]*)/?", bare)
    if match is None or len(match.group(1)) > SLUG_MAX_CHARS or _SLUG.fullmatch(match.group(1)) is None:
        return None
    return match.group(1)


def map_sourceless_page_to_html(page: Page) -> str:
    e = html.escape
    at = datetime.fromtimestamp(page.updated_at / 1000)
    body = (
        f'<article class="pin"><h2>No local file to show</h2>'
        f"<p>Source: <code>{e(page.source_dir)}</code> holds neither index.html nor a single .html file.</p>"
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
