"""The page check: what a page or review doc looks like once rendered, light and dark, in a headless browser that no
one sees: a main column across the whole window, a diagram whose background is not the page's, diagram text that is
hard to read. Plus the size estimates in days that the team never writes. Advice only: it never stops a page."""

import json
import mimetypes
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

from bridge.doc_nav import convert_doc_to_viewable, convert_page_to_themed, convert_served_page_to_styled
from bridge.review_doc_check import find_day_estimates, find_small_svg_text_problems, map_estimates_to_problem

BROWSER_ENV = "BRIDGE_BROWSER"
"""A headless-capable Chrome or Chromium to render with; set and empty, no page is rendered."""
WINDOW = (1600, 1000)
MIN_TEXT_PX = 14
"""Diagram text drawn smaller than this on screen is refused; the same floor as the static check's."""
MAX_COLUMN_SHARE = 0.85
"""A main column wider than this share of the window spans it: the shared style keeps one to 1100 px."""
MIN_CONTRAST = 2.5
"""Below WCAG's 3:1 for large text and graphics: a label at 2.9:1 still reads, and flagging it would be noise."""
RENDER_TIMEOUT_S = 40
SHOWN_TEXTS = 3
_BROWSERS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "chrome-headless-shell",
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
)
_CONTENT_TYPES = {".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}
_PROBE_OUT_RE = re.compile(r'<script type="application/json" id="bridge-probe-out">(.*?)</script>', re.S)
_BODY_END_RE = re.compile(rb"</body\s*>", re.I)


def find_browser() -> str | None:
    if (named := os.environ.get(BROWSER_ENV)) is not None:
        return named or None
    for candidate in _BROWSERS:
        found = shutil.which(candidate) or (candidate if os.path.isfile(candidate) else None)
        if found:
            return found
    return None


_BLOCK_TAGS = frozenset({
    "p", "div", "section", "article", "header", "footer", "main", "aside", "nav", "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "dt", "dd", "tr", "td", "th", "caption", "blockquote", "pre", "figure", "figcaption", "table", "ul", "ol",
    "dl", "br", "hr",
})  # fmt: skip
"""Tags whose text a reader takes as its own line: words on either side never run into one sentence."""


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._hidden += tag in ("script", "style", "template")
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "template"):
            self._hidden = max(0, self._hidden - 1)
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            self.parts.append(data)


def read_visible_text(page: str) -> str:
    reader = _VisibleText()
    try:
        reader.feed(page)
        reader.close()
    except (AssertionError, ValueError):
        pass
    lines = (" ".join(line.split()) for line in "".join(reader.parts).split("\n"))
    return "\n".join(line for line in lines if line)


def find_text_problems(page: bytes) -> list[str]:
    """What the page's words break, read without rendering it."""
    source = page.decode("utf-8", "replace")
    found = find_day_estimates(read_visible_text(source))
    return [*([map_estimates_to_problem(found)] if found else []), *find_small_svg_text_problems(source)]


@contextmanager
def _serve(page: bytes, assets: Path | None) -> Generator[str]:
    """The page at / on a loopback port for as long as the render takes, and the files beside it from its folder."""
    root = assets.resolve() if assets is not None else None

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            body: bytes | None = page if path == "/" else None
            if body is None and root is not None:
                found = (root / path.lstrip("/")).resolve()
                if found.is_relative_to(root) and found.is_file():
                    body = found.read_bytes()
            self.send_response(200 if body is not None else 404)
            # A module script runs only when served as JavaScript.
            kind = "text/html" if path == "/" else _CONTENT_TYPES.get(Path(path).suffix.lower())
            kind = kind or mimetypes.guess_type(path)[0] or "application/octet-stream"
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body or b"")))
            self.end_headers()
            self.wfile.write(body or b"")

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@contextmanager
def _dead_port() -> Generator[int]:
    """A loopback port that refuses every connection for as long as it is held: bound, never listening."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        yield held.getsockname()[1]


def render_probe(page: bytes, assets: Path | None, browser: str) -> dict[str, Any]:
    """The probe's reading of the page as a browser draws it; {"error": …} when it could not be rendered."""
    probe = files("bridge").joinpath("web/page-probe.js").read_text(encoding="utf-8")
    return render_reading(
        page,
        assets,
        browser,
        f"window.bridgeProbeMinContrast={MIN_CONTRAST};window.bridgeProbeMinTextPx={MIN_TEXT_PX};{probe}",
    )


def render_reading(
    page: bytes, assets: Path | None, browser: str, script: str, window: tuple[int, int] = WINDOW
) -> dict[str, Any]:
    """What `script`, run at the end of the page's body, wrote as JSON into #bridge-probe-out once the page loaded."""
    tag = f"<script>{script}</script>".encode()
    # The last one: a script on the page may hold the words in a string.
    end = max((m.start() for m in _BODY_END_RE.finditer(page)), default=len(page))
    dom, error = render_dom(page[:end] + tag + page[end:], assets, browser, window)
    if error:
        return {"error": error}
    found = _PROBE_OUT_RE.search(dom)
    if found is None:
        return {"error": f"{Path(browser).name} rendered no reading"}
    try:
        return cast("dict[str, Any]", json.loads(found.group(1)))
    except ValueError:
        return {"error": "the reading was not JSON"}


def render_dom(
    page: bytes, assets: Path | None, browser: str, window: tuple[int, int] = WINDOW
) -> tuple[str, str | None]:
    """The page's DOM once it loaded and its scripts ran, as the browser would save it; and why there is none."""
    with (
        tempfile.TemporaryDirectory(prefix="bridge-page-check-", ignore_cleanup_errors=True) as profile,
        _serve(page, assets) as origin,
        _dead_port() as dead,
    ):
        command = [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--hide-scrollbars",
            "--mute-audio",
            # A fresh profile otherwise asks macOS for a keychain, and that dialog lands on the owner's screen.
            "--use-mock-keychain",
            "--password-store=basic",
            f"--user-data-dir={profile}",
            f"--window-size={window[0]},{window[1]}",
            # Only the page's own server, by its scheme, host and port: every other request, to the web or to another
            # local port, goes to a proxy that refuses it, and this proxy replaces any the system has set.
            f"--proxy-server=http://127.0.0.1:{dead}",
            f"--proxy-bypass-list=<-loopback>;http://{origin}",
            "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--disable-background-networking",
            "--disable-component-update",
            "--no-pings",
            "--virtual-time-budget=5000",
            "--dump-dom",
            f"http://{origin}/",
        ]
        try:
            chrome = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            return "", f"{Path(browser).name} did not render it ({type(exc).__name__})"
        try:
            out, _ = chrome.communicate(timeout=RENDER_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            return "", f"{Path(browser).name} did not render it ({type(exc).__name__})"
        finally:
            _kill_group(chrome)
    dom = out.decode("utf-8", "replace")
    return dom, None if dom.strip() else f"{Path(browser).name} rendered nothing (exit {chrome.returncode})"


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    # Chrome's helpers share its group: a timeout, error or interrupted check must not leave them running.
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()
    if process.stdout:
        process.stdout.close()


def find_layout_problems(reading: dict[str, Any]) -> list[str]:
    """How a probe reading's page is laid out wrong, whatever its theme."""
    problems: list[str] = []
    column = cast("dict[str, int]", reading.get("column") or {})
    span, window = column.get("right", 0) - column.get("left", 0), column.get("window", 0)
    if window and span > window * MAX_COLUMN_SHARE:
        problems.append(
            f"its main column spans {span} of a {window} px window; leave its width to the shared style (text in a "
            "~75ch column, the page at most 1100 px) or give the page a max-width"
        )
    if window and column.get("page", 0) > window + 1:
        problems.append(
            f"the page scrolls sideways ({column['page']} px wide in a {window} px window); put the wide part in a "
            "box that scrolls on its own"
        )
    return problems


def find_render_problems(reading: dict[str, Any], theme: str) -> list[str]:
    """What a probe reading shows wrong in its colours, one plain line each."""
    if error := reading.get("error"):
        return [f"rendered {theme}: the check could not read the page ({error})"]
    problems: list[str] = []
    page = str(reading.get("page", ""))
    for area in cast("list[dict[str, Any]]", reading.get("areas") or []):
        where = f"the diagram '{area.get('label') or 'without a label'}'" + (
            f" under '{area['heading']}'" if area.get("heading") else ""
        )
        samples, off = int(area.get("samples", 0)), int(area.get("off", 0))
        if samples and off * 2 > samples:
            problems.append(
                f"rendered {theme}: {where} has its own background ({area.get('bg')}) on a {page} page; colour it "
                "with the page's theme (var(--ds-tint) or a transparent fill), not a fixed colour"
            )
        small = cast("list[dict[str, Any]]", area.get("small") or [])
        if small:
            shown = "; ".join(f"'{t['text']}' {t['px']:g}px" for t in small[:SHOWN_TEXTS])
            more = f" and {len(small) - SHOWN_TEXTS} more" if len(small) > SHOWN_TEXTS else ""
            problems.append(
                f"rendered {theme}: text in {where} is drawn under {MIN_TEXT_PX}px on screen: {shown}{more}; "
                f"set it to {MIN_TEXT_PX}px or more as drawn (a viewBox wider than the diagram's width shrinks it)"
            )
        texts = cast("list[dict[str, Any]]", area.get("texts") or [])
        if texts:
            shown = "; ".join(f"'{t['text']}' {t['ratio']}:1 ({t['fg']} on {t['bg']})" for t in texts[:SHOWN_TEXTS])
            more = f" and {len(texts) - SHOWN_TEXTS} more" if len(texts) > SHOWN_TEXTS else ""
            problems.append(
                f"rendered {theme}: text in {where} is hard to read, under {MIN_CONTRAST:g}:1 contrast: {shown}{more}; "
                "colour it with the theme (currentColor, var(--ds-fg) or var(--ds-muted))"
            )
    return problems


def check_rendered(
    serve: Callable[[str], bytes], assets: Path | None, browser: str | None = None
) -> tuple[list[str], str | None]:
    """The problems a light and a dark render show, and why nothing was rendered (no browser), else None. `serve` gives
    the page as bridge serves it to a reader who chose the given theme."""
    browser = browser or find_browser()
    if browser is None:
        return [], f"no Chrome or Chromium found (set {BROWSER_ENV}), so it was not rendered"
    # One after the other in this thread: an interrupt lands in the render that runs, which stops its browser.
    light, dark = (render_probe(serve(theme), assets, browser) for theme in ("light", "dark"))
    laid_out = find_layout_problems(light)
    # A layout fault both themes show is told once; one only the dark page shows is told as dark.
    only_dark = [f"rendered dark: {p}" for p in find_layout_problems(dark) if p not in laid_out]
    return laid_out + only_dark + find_render_problems(light, "light") + find_render_problems(dark, "dark"), None


def serve_with_theme(served: bytes) -> Callable[[str], bytes]:
    return lambda theme: convert_page_to_themed(served, theme)


def check_page(html_file: Path) -> tuple[list[str], str | None]:
    """A registered page's problems as /p/ serves it, and why it was not rendered, else None."""
    page = html_file.read_bytes()
    served = convert_served_page_to_styled(page)
    problems, skipped = check_rendered(serve_with_theme(served), html_file.parent)
    return find_text_problems(page) + problems, skipped


def check_review_doc(page: bytes) -> tuple[list[str], str | None]:
    """A review doc's render problems as the review page serves it, and why it was not rendered, else None."""
    return check_rendered(serve_with_theme(convert_doc_to_viewable(page)), None)
