import html
import json
import re

REVIEW_BOT_NAME = "ReviewBot"
REVIEW_NAMES = ("Codex", "Claude", REVIEW_BOT_NAME)
REQUIRED_REVIEWS = ("Codex", "Claude")
"""Two independent reviews of a large PR: Codex and a fresh Claude session that did not write it."""
_NAME_RES = {
    "Codex": re.compile(r"\bcodex\b", re.I),
    "Claude": re.compile(r"\bclaude\b", re.I),
    REVIEW_BOT_NAME: re.compile(r"\breview\s*bot\b", re.I),
}
_HTML_HIDDEN_RE = re.compile(rb"<!--.*?(?:-->|$)|<(script|style|template)\b.*?(?:</\1\s*>|$)", re.I | re.S)
MD_HIDDEN_RE = re.compile(r"<!--.*?(?:-->|\Z)|^ {0,3}(```|~~~).*?(?:^ {0,3}\1[^\n]*$|\Z)", re.S | re.M)
_HTML_HEADING_RE = re.compile(rb"<h([1-6])\b[^>]*>(.*?)</h\1\s*>", re.I | re.S)
_TAG_RE = re.compile(rb"<[^>]+>")
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_MD_LABEL_RE = re.compile(r"^\s*(?:\*\*|__)?reviews(?:\*\*|__)?\s*:?\s*(?:\*\*|__)?\s*$", re.I)
_REVIEWS_TITLE_RE = re.compile(r"^\W*reviews\b", re.I)


def map_html_to_reviews(page: bytes) -> tuple[str, ...] | None:
    """The reviews a doc's Reviews section names; None when it has no heading that starts with Reviews. Comments and
    scripts are not the doc's visible text."""
    page = _HTML_HIDDEN_RE.sub(b" ", page)
    headings = list(_HTML_HEADING_RE.finditer(page))
    for index, heading in enumerate(headings):
        if not _REVIEWS_TITLE_RE.match(_convert_html_to_text(heading.group(2))):
            continue
        level = int(heading.group(1))
        end = next((h.start() for h in headings[index + 1 :] if int(h.group(1)) <= level), len(page))
        return _find_names(_convert_html_to_text(page[heading.end() : end]))
    return None


def map_markdown_to_reviews(body: str) -> tuple[str, ...] | None:
    """The same from a PR body: a heading or a lone label line that starts with Reviews, up to the next heading.
    Comments and fenced code are examples, not the body's sections."""
    lines = MD_HIDDEN_RE.sub("", body).splitlines()
    for index, line in enumerate(lines):
        heading = _MD_HEADING_RE.match(line)
        if heading and _REVIEWS_TITLE_RE.match(heading.group(2)):
            level = len(heading.group(1))
        elif _MD_LABEL_RE.match(line):
            level = 0
        else:
            continue
        section: list[str] = []
        for rest in lines[index + 1 :]:
            following = _MD_HEADING_RE.match(rest)
            if following and (level == 0 or len(following.group(1)) <= level):
                break
            section.append(rest)
        return _find_names("\n".join(section))
    return None


def convert_reviews_to_text(reviews: tuple[str, ...] | None) -> str:
    """Stored as JSON: null for no Reviews section, else the names it gives."""
    return json.dumps(None if reviews is None else list(reviews))


def map_text_to_reviews(text: str | None) -> tuple[str, ...] | None:
    value = json.loads(text) if text else None
    return None if value is None else tuple(str(name) for name in value)


def _find_names(text: str) -> tuple[str, ...]:
    return tuple(name for name in REVIEW_NAMES if _NAME_RES[name].search(text))


def _convert_html_to_text(fragment: bytes) -> str:
    return html.unescape(_TAG_RE.sub(b" ", fragment).decode("utf-8", "replace"))
