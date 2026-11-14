import html
import json
import re

_HTML_HIDDEN_RE = re.compile(rb"<!--.*?(?:-->|$)|<(script|style|template)\b.*?(?:</\1\s*>|$)", re.I | re.S)
MD_HIDDEN_RE = re.compile(r"<!--.*?(?:-->|\Z)|^ {0,3}(```|~~~).*?(?:^ {0,3}\1[^\n]*$|\Z)", re.S | re.M)
_HTML_HEADING_RE = re.compile(rb"<h([1-6])\b[^>]*>(.*?)</h\1\s*>", re.I | re.S)
_TAG_RE = re.compile(rb"<[^>]+>")
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_MD_LABEL_RE = re.compile(r"^\s*(?:\*\*|__)?reviews(?:\*\*|__)?\s*:?\s*(?:\*\*|__)?\s*$", re.I)
_REVIEWS_TITLE_RE = re.compile(r"^\W*reviews\b", re.I)
_ENABLING_RE = re.compile(
    r"\b(?:off|disabled) by default\b|\bopt[- ]in (?:flag|value|setting|switch|option|toggle|chart)"
    r"|\bbehind a (?:feature[- ])?flag\b|\bfeature[- ]flag\b"
    r"|\bno behaviou?r change\b|\binert until\b|\b(?:step|part) \d+ of \d+\b"
    r"|\bnot yet (?:enabled|switched on|wired)\b",
    re.I,
)
_USAGE_TITLE_RE = re.compile(r"^\W*how this will be used\b", re.I)
_USAGE_LABEL_RE = re.compile(r"^\s*(?:\*\*|__)?how this will be used\b", re.I)
_NOT_ENABLING_RE = re.compile(r"^\s*(?:\*\*|__)?not an enabling pr:(?:\*\*|__)?\s*\S", re.I | re.M)


def map_html_to_reviews(page: bytes, names: tuple[str, ...]) -> tuple[str, ...] | None:
    """Which of the names a doc's Reviews section holds; None when it has no heading that starts with Reviews.
    Comments and scripts are not the doc's visible text."""
    page = _HTML_HIDDEN_RE.sub(b" ", page)
    headings = list(_HTML_HEADING_RE.finditer(page))
    for index, heading in enumerate(headings):
        if not _REVIEWS_TITLE_RE.match(_convert_html_to_text(heading.group(2))):
            continue
        level = int(heading.group(1))
        end = next((h.start() for h in headings[index + 1 :] if int(h.group(1)) <= level), len(page))
        return _find_names(_convert_html_to_text(page[heading.end() : end]), names)
    return None


def map_markdown_to_reviews(body: str, names: tuple[str, ...]) -> tuple[str, ...] | None:
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
        return _find_names("\n".join(section), names)
    return None


def find_missing_usage_section(title: str, body: str) -> str | None:
    """The words by which a PR says it only enables something, when its body has no section headed How this will be
    used and no 'Not an enabling PR:' line with a reason; None otherwise."""
    visible = MD_HIDDEN_RE.sub("", body)
    said = _ENABLING_RE.search(f"{title}\n{visible}")
    if said is None or _NOT_ENABLING_RE.search(visible):
        return None
    for line in visible.splitlines():
        heading = _MD_HEADING_RE.match(line)
        if (heading and _USAGE_TITLE_RE.match(heading.group(2))) or _USAGE_LABEL_RE.match(line):
            return None
    return said.group(0)


def convert_reviews_to_text(reviews: tuple[str, ...] | None) -> str:
    """Stored as JSON: null for no Reviews section, else the names it gives."""
    return json.dumps(None if reviews is None else list(reviews))


def map_text_to_reviews(text: str | None) -> tuple[str, ...] | None:
    value = json.loads(text) if text else None
    return None if value is None else tuple(str(name) for name in value)


def _find_names(text: str, names: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(name for name in names if map_name_to_pattern(name).search(text))


def map_name_to_pattern(name: str) -> re.Pattern[str]:
    """Any case, and spaced or not between its capitalised words: ReviewBot also matches "review bot"."""
    words = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z0-9]+", name) or [name]
    return re.compile(r"\b" + r"\s*".join(re.escape(w) for w in words) + r"\b", re.I)


def _convert_html_to_text(fragment: bytes) -> str:
    return html.unescape(_TAG_RE.sub(b" ", fragment).decode("utf-8", "replace"))
