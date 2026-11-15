import re
from collections import Counter
from html.parser import HTMLParser

IN_SHORT = "In short"
CHANGES_SINCE = "Changes since the last review"
_IN_SHORT_RE = re.compile(r"^\W*in short\b", re.I)
_CHANGES_SINCE_RE = re.compile(r"\bsince (?:the |your |my )?(?:last |previous )?review\b", re.I)
_WHERE_TO_LOOK_RE = re.compile(r"^\W*where to look\b", re.I)
_NOT_FROM_ROOT_RE = re.compile(r"^(?:/|~|…)|(?:^|/)\.{1,3}(?:/|$)")
_LINE_SUFFIX_RE = re.compile(r":\d[\d,\s\u2013-]*$")
_HIDDEN_TAGS = frozenset({"script", "style", "template", "textarea"})
_ROW_TAGS = ("li", "tr")


class _DocOutline(HTMLParser):
    """The doc's h2 texts, and the path in each row (li or tr) of Where to look: the row's first <code>. A missing end
    tag ends its element where a browser would, at the next heading, row or the end of the file."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.headings: list[str] = []
        self.rows: list[str | None] = []
        self._hidden = 0
        self._heading: list[str] | None = None
        self._open_rows: list[int] = []
        self._code: list[str] | None = None
        self._code_row = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _HIDDEN_TAGS:
            self._hidden += 1
        elif self._hidden:
            return
        elif tag == "h2":
            self._end_code()
            self._end_heading()
            self._open_rows = []
            self._heading = []
        elif not self._in_where_to_look():
            return
        elif tag in _ROW_TAGS:
            self._end_code()
            self.rows.append(None)
            self._open_rows.append(len(self.rows) - 1)
        elif tag == "code" and self._code is None and self._open_rows and self.rows[self._open_rows[-1]] is None:
            self._code = []
            self._code_row = self._open_rows[-1]

    def handle_endtag(self, tag: str) -> None:
        if tag in _HIDDEN_TAGS:
            self._hidden = max(0, self._hidden - 1)
        elif self._hidden:
            return
        elif tag == "h2":
            self._end_heading()
        elif tag == "code":
            self._end_code()
        elif tag in _ROW_TAGS and self._open_rows:
            self._end_code()
            self._open_rows.pop()

    def handle_data(self, data: str) -> None:
        if self._hidden:
            return
        if self._heading is not None:
            self._heading.append(data)
        if self._code is not None:
            self._code.append(data)

    def close(self) -> None:
        super().close()
        self._end_code()
        self._end_heading()

    def _end_heading(self) -> None:
        if self._heading is not None:
            self.headings.append(" ".join("".join(self._heading).split()))
            self._heading = None

    def _end_code(self) -> None:
        if self._code is not None:
            self.rows[self._code_row] = "".join(self._code).strip() or None
            self._code = None

    def _in_where_to_look(self) -> bool:
        return self._heading is None and bool(self.headings) and bool(_WHERE_TO_LOOK_RE.match(self.headings[-1]))


def map_row_to_path(text: str) -> str:
    """A row may point at lines (path:12-40); the file is what it names."""
    return _LINE_SUFFIX_RE.sub("", text).rstrip("/")


def find_doc_problems(page: bytes, owner_reviewed: bool, owner: str, files: frozenset[str] | None) -> list[str]:
    """What breaks the spec's order and Where to look rules, one plain line each. `files` holds every file and
    directory of the commit the doc describes; None when that commit cannot be read, so paths are not looked up."""
    outline = _DocOutline()
    problems: list[str] = []
    try:
        outline.feed(page.decode("utf-8", "replace"))
        outline.close()
    except (AssertionError, ValueError) as exc:
        line, _ = outline.getpos()
        return [f"its HTML cannot be read past line {line} ({exc}); fix the markup there"]
    headings = outline.headings
    if not headings:
        problems.append(f"it has no h2 headings; it opens with the h2 '{IN_SHORT}'")
    elif not _IN_SHORT_RE.match(headings[0]):
        problems.append(f"its first h2 is '{headings[0]}'; it opens with '{IN_SHORT}', with nothing before it")
    since = [index for index, text in enumerate(headings) if _CHANGES_SINCE_RE.search(text)]
    if since and not owner_reviewed:
        problems.append(
            f"it has '{headings[since[0]]}', but {owner} has not reviewed this PR yet: what changed is told in the "
            "sections themselves, and that section comes only after their review"
        )
    elif len(since) > 1:
        problems.append(f"'{CHANGES_SINCE}' is there {len(since)} times; it is one section, the last h2")
    elif since and since[0] != len(headings) - 1:
        problems.append(f"'{headings[since[0] + 1]}' comes after '{CHANGES_SINCE}', which is always the last h2")
    paths = [path for path in (map_row_to_path(row) for row in outline.rows if row) if path]
    for path, count in Counter(paths).items():
        if count > 1:
            problems.append(f"Where to look has {path} in {count} rows; one row per file")
    for path in dict.fromkeys(paths):
        if _NOT_FROM_ROOT_RE.search(path):
            problems.append(
                f"Where to look has a row for {path}, which is not a path from the repo root; the worktree path goes "
                "in a paragraph, not a row"
            )
        elif files is not None and path not in files:
            problems.append(
                f"Where to look has a row for {path}, which is no file or folder at the commit the doc describes: "
                "write the full path from the repo root"
            )
    return problems
