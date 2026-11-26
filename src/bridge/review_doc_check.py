"""The checks `guild review doc` runs on a review doc. Standard library only: a host without guild runs this file
as it is, `python3 review_doc_check.py <doc.html>`, from the copy bridge puts in its bridge skill folder."""

import argparse
import fnmatch
import html
import json
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser

IN_SHORT = "In short"
USER_STORY = "User story"
MIN_STORY_STEPS = 3
CHANGES_SINCE = "Changes since the last review"
_IN_SHORT_RE = re.compile(r"^\W*in short\b", re.I)
_USER_STORY_RE = re.compile(r"^\W*user story\b", re.I)
_CHANGES_SINCE_RE = re.compile(r"\bsince (?:the |your |my )?(?:last |previous )?review\b", re.I)
_WHERE_TO_LOOK_RE = re.compile(r"^\W*where to look\b", re.I)
_NOT_FROM_ROOT_RE = re.compile(r"^(?:/|~|…)|(?:^|/)\.{1,3}(?:/|$)")
_LINE_SUFFIX_RE = re.compile(r":\d[\d,\s\u2013-]*$")
_WHAT_CHANGES_RE = re.compile(r"^\W*what changes\b", re.I)
_CHECKS_RE = re.compile(r"^[\W\d]*(?:checks|verified)\b", re.I)
_VERIFIED_PART_RE = re.compile(r"^[\W\d]*verified\b", re.I)
_FIELDS_RE = re.compile(r"^[\W\d]*fields\b", re.I)
_FIELD_COLUMN_RE = re.compile(r"^\W*fields?\b", re.I)
_FIELD_COLUMNS = {
    "What": re.compile(r"\bwhat\b|\bmean|\bdescription\b", re.I),
    "Why": re.compile(r"\bwhy\b|\bwrit|\bread", re.I),
    "Limits": re.compile(r"\blimit|\ballowed\b|\bconstraint|\btype\b", re.I),
    "Example": re.compile(r"\bexamples?\b", re.I),
}
CHECKS = "Checks"
_OWN_CHECKS_RE = re.compile(r"^[\W\d]*checks\b", re.I)
_LOOSE_CHECK_RE = re.compile(r"^[\W\d]*(?:tests|reviews|verified)\b", re.I)
_SUMMARY_ID_RE = re.compile(r"^\W*[A-Z]{1,2}\d{1,3}(?:[.:)]|\s+[-\u2013\u2014])\s")
VERDICT_LABELS = {
    "Type": re.compile(r"^\W*type\b", re.I),
    "You decide": re.compile(r"\bdecide", re.I),
    "Could break": re.compile(r"\bbreak", re.I),
    "Main risk": re.compile(r"\brisk", re.I),
    "Checks": re.compile(r"^\W*checks?\b", re.I),
}
_CLOSE_BREAK = "What else could break"
_WORKS = "How it works"
_RISKS = "Costs and risks"
_CHANGES = "What changes and why"
_SECTION_RES = {
    _CHANGES: _WHAT_CHANGES_RE,
    _WORKS: re.compile(r"^[\W\d]*how it works\b", re.I),
    _RISKS: re.compile(r"^[\W\d]*costs? and risks?\b", re.I),
    "Fields": _FIELDS_RE,
    _CLOSE_BREAK: re.compile(r"^[\W\d]*what else could break\b", re.I),
    "Root cause": re.compile(r"^[\W\d]*root cause\b", re.I),
    "No behaviour change": re.compile(r"^[\W\d]*no behaviou?r change\b", re.I),
    "Rollout": re.compile(r"^[\W\d]*rollout\b", re.I),
    "How this will be used": re.compile(r"^[\W\d]*how this will be used\b", re.I),
    "Differences from the original": re.compile(r"^[\W\d]*differences from the original\b", re.I),
}
_REUSE_RE = re.compile(r"\breuse\s*:", re.I)
_TYPE_END_RE = re.compile(r"[,;:(\u00b7\u2013\u2014\d]| - ")
_INLINE_TAGS = frozenset(
    {
        "a",
        "abbr",
        "b",
        "code",
        "em",
        "i",
        "ins",
        "kbd",
        "mark",
        "s",
        "samp",
        "small",
        "span",
        "strong",
        "sub",
        "sup",
        "u",
    }
)
_FIRST_READER_PASSED_RE = re.compile(r"\bfirst[- ]reader\b(?![^.;]*\b(?:not|never|failed|pending)\b)[^.;]*\bpass", re.I)
_TOP_SECTIONS = ("short", "story", "changes")
_HIDDEN_TAGS = frozenset({"script", "style", "template", "textarea"})
_ROW_TAGS = ("li", "tr")
_UNREAD_TAGS = _HIDDEN_TAGS | {"svg", "pre"}
_BODY_UNREAD_TAGS = _UNREAD_TAGS | {"title"}
_BLOCK_TAG_RE = re.compile(
    r"p|li|[uo]l|t[dhr]|table|div|summary|details|fig(?:ure|caption)|blockquote|d[dt]|h[13-6]|section|hr"
)
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])[\"')\]\u201d\u2019]*\s+(?=\S)")
_ABBREVIATION_END_RE = re.compile(r"(?:\b(?:e\.g|i\.e|vs|etc|cf|approx|no|nr|incl)\.)$", re.I)
_WORD_RE = re.compile(r"\S*\w\S*")


@dataclass(frozen=True)
class PrType:
    name: str
    signal: re.Pattern[str]
    sections: tuple[str, ...]
    """The h2s a PR of this type needs, beyond In short, Where to look and Checks."""
    reuse: bool = False
    """Adds a model, helper or concept, so What else could break says which existing one looks alike."""


PR_TYPES = (
    PrType(
        "model / contract",
        re.compile(r"\bmodel|\bcontract|\bschema", re.I),
        (_CHANGES, "Fields", _CLOSE_BREAK, _RISKS),
        reuse=True,
    ),
    PrType("refactor", re.compile(r"\brefactor", re.I), (_CHANGES, "No behaviour change", _CLOSE_BREAK)),
    PrType("bug fix", re.compile(r"\bbug\b|\bfix", re.I), (_CHANGES, "Root cause", _CLOSE_BREAK, _RISKS)),
    PrType("new feature", re.compile(r"\bfeature", re.I), (_CHANGES, _CLOSE_BREAK, _WORKS, _RISKS), reuse=True),
    PrType("infra / chart", re.compile(r"\binfra|\bchart", re.I), (_CHANGES, "Rollout", _CLOSE_BREAK, _RISKS)),
    PrType("docs only", re.compile(r"\bdocs?\b", re.I), ()),
    PrType(
        "enabling",
        re.compile(r"\benabl", re.I),
        (_CHANGES, "How this will be used", "Rollout", _CLOSE_BREAK, _RISKS),
    ),
    PrType("backport", re.compile(r"\bbackport", re.I), ("Differences from the original",)),
)


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


class _FieldTables(HTMLParser):
    """The header cells (the first row) of each table under the Fields h2, and whether the table sits in a closed
    fold."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.headers: list[tuple[list[str], bool]] = []
        self._details: list[list[bool]] = []
        self._table_folded = False
        self._hidden = 0
        self._heading: list[str] | None = None
        self._in_fields = False
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._first_row = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _HIDDEN_TAGS:
            self._hidden += 1
        elif self._hidden:
            return
        elif tag == "h2":
            self._end_row()
            self._heading, self._in_fields = [], False
        elif tag == "details":
            self._details.append(["open" in dict(attrs), False])
        elif tag == "summary" and self._details:
            self._details[-1][1] = True
        elif not self._in_fields:
            return
        elif tag == "table":
            self._first_row = True
            self._table_folded = any(not opened and not in_summary for opened, in_summary in self._details)
        elif tag in ("tr", "tbody") and self._row is not None:
            self._end_row()
        elif tag == "tr" and self._first_row:
            self._row, self._first_row = [], False
        elif tag in ("th", "td") and self._row is not None:
            self._end_cell()
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in _HIDDEN_TAGS:
            self._hidden = max(0, self._hidden - 1)
        elif self._hidden:
            return
        elif tag == "h2" and self._heading is not None:
            self._in_fields = bool(_FIELDS_RE.match(" ".join("".join(self._heading).split())))
            self._heading = None
        elif tag in ("th", "td"):
            self._end_cell()
        elif tag in ("tr", "thead", "table"):
            self._end_row()
        elif tag == "summary" and self._details:
            self._details[-1][1] = False
        elif tag == "details" and self._details:
            self._details.pop()

    def handle_data(self, data: str) -> None:
        if self._hidden:
            return
        if self._heading is not None:
            self._heading.append(data)
        if self._cell is not None:
            self._cell.append(data)

    def close(self) -> None:
        super().close()
        self._end_row()

    def _end_row(self) -> None:
        self._end_cell()
        if self._row is not None:
            self.headers.append((self._row, self._table_folded))
            self._row = None

    def _end_cell(self) -> None:
        if self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
        self._cell = None


def find_field_table_problems(page: str) -> list[str]:
    """Each table of fields under Fields is visible and has What, Why, Limits and Example columns: the owner reads
    every changed field of every changed model there, and a realistic value tells what a field holds fastest."""
    tables = _FieldTables()
    tables.feed(page)
    tables.close()
    problems: list[str] = []
    fields = [(header, folded) for header, folded in tables.headers if any(map(_FIELD_COLUMN_RE.match, header))]
    if folded := sum(folded for _, folded in fields):
        problems.append(
            f"Fields has {folded} table(s) of fields inside a closed fold; every changed field of every changed model "
            "stays visible (only writer and reader file paths may fold)"
        )
    for header, _ in fields:
        missing = [name for name, column in _FIELD_COLUMNS.items() if not any(map(column.search, header))]
        if missing:
            problems.append(
                f"Fields has a table of fields with no {' / '.join(missing)} column (columns: {' | '.join(header)}); "
                "per field: what it is, why it is needed (who writes, who reads), its limits, and a realistic example "
                "value in its real shape, invented, never real customer data"
            )
            break
    return problems


@dataclass
class DocBody:
    """What the doc shows before anything is opened, and the text of each h2 section, folds included."""

    strip: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    """The verdict strip before the first h2: each label (dt) and its line (dd)."""
    visible_words: int = 0
    """Words outside closed folds (fold summaries count), diagrams, code blocks and Fields."""
    sections: dict[str, str] = field(default_factory=dict[str, str])
    summaries: list[str] = field(default_factory=list[str])


class _BodyReader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.body = DocBody()
        self._unread = 0
        self._details: list[list[bool]] = []
        self._heading: list[str] | None = None
        self._section: list[str] | None = None
        self._section_name = ""
        self._in_fields = False
        self._visible: list[str] = []
        self._summary: list[str] | None = None
        self._strip = 0
        self._label: list[str] | None = None
        self._line: list[str] | None = None
        self._line_label = ""

    def _folded(self) -> bool:
        return any(not opened and not in_summary for opened, in_summary in self._details)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BODY_UNREAD_TAGS:
            self._unread += 1
            return
        if self._unread:
            return
        if tag not in _INLINE_TAGS:
            self._space()
        if tag == "h2" and not self._folded():
            self._end_heading()
            self._end_section()
            self._strip = 0
            self._end_strip_line()
            self._heading = []
        elif tag == "details":
            self._details.append(["open" in dict(attrs), False])
        elif tag == "summary" and self._details:
            self._details[-1][1] = True
            self._summary = []
        elif tag == "dl" and (self._strip or self._opens_strip(attrs)):
            self._strip += 1
        elif tag == "dt" and self._strip == 1:
            self._end_strip_line()
            self._label = []
        elif tag == "dd" and self._strip == 1 and self._label is not None:
            self._line_label = " ".join("".join(self._label).split())
            self._label, self._line = None, []

    def handle_endtag(self, tag: str) -> None:
        if tag in _BODY_UNREAD_TAGS:
            self._unread = max(0, self._unread - 1)
            return
        if self._unread:
            return
        if tag not in _INLINE_TAGS:
            self._space()
        if tag == "h2":
            self._end_heading()
        elif tag == "dd" and self._strip == 1:
            self._end_strip_line()
        elif tag == "summary" and self._details:
            self._details[-1][1] = False
            if self._summary is not None:
                self.body.summaries.append(" ".join("".join(self._summary).split()))
                self._summary = None
        elif tag == "details" and self._details:
            self._details.pop()
        elif tag == "dl" and self._strip:
            self._strip -= 1
            if not self._strip:
                self._end_strip_line()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self._unread and tag not in _INLINE_TAGS:
            self._space()

    def handle_data(self, data: str) -> None:
        if self._unread:
            return
        for part in (self._heading, self._section, self._summary, self._label, self._line):
            if part is not None:
                part.append(data)
        if not self._folded() and not self._in_fields:
            self._visible.append(data)

    def close(self) -> None:
        super().close()
        self._end_heading()
        self._end_section()
        self._end_strip_line()
        self.body.visible_words = count_words("".join(self._visible))

    def _space(self) -> None:
        self.handle_data(" ")

    def _opens_strip(self, attrs: list[tuple[str, str | None]]) -> bool:
        """Only before the first h2 and outside closed folds: a strip further down or folded is not read first."""
        verdict = "verdict" in (dict(attrs).get("class") or "").split()
        return verdict and self._section is None and self._heading is None and not self._folded()

    def _end_heading(self) -> None:
        if self._heading is not None:
            self._section_name = " ".join("".join(self._heading).split())
            self._in_fields = bool(_FIELDS_RE.match(self._section_name))
            self._section, self._heading = [], None

    def _end_section(self) -> None:
        if self._section is not None:
            self.body.sections.setdefault(self._section_name, " ".join("".join(self._section).split()))
        self._section = None
        self._in_fields = False

    def _end_strip_line(self) -> None:
        if self._line is not None:
            self.body.strip.append((self._line_label, " ".join("".join(self._line).split())))
        self._label = self._line = None


def read_doc_body(page: str) -> DocBody:
    reader = _BodyReader()
    try:
        reader.feed(page)
        reader.close()
    except (AssertionError, ValueError):
        pass
    return reader.body


def find_strip_problems(body: DocBody, limits: "PlainLimits") -> list[str]:
    """The verdict strip tells in seconds whether to read on; its Type picks the sections the doc needs."""
    if not body.strip:
        return [
            f"it has no verdict strip before '{IN_SHORT}': a <dl class=\"verdict\"> with "
            f"{', '.join(VERDICT_LABELS)}, one short line each"
        ]
    problems = [
        f"the verdict strip has no '{label}' line"
        for label, pattern in VERDICT_LABELS.items()
        if not any(pattern.search(name) for name, _ in body.strip)
    ]
    for name, line in body.strip:
        if (words := count_words(line)) > limits.bullet_words:
            problems.append(f"the verdict strip's '{name}' line has {words} words, over {limits.bullet_words}")
    return problems


def read_pr_types(body: DocBody) -> list[PrType]:
    line = next((line for name, line in body.strip if VERDICT_LABELS["Type"].search(name)), "")
    declared = _TYPE_END_RE.split(line, maxsplit=1)[0]
    return [kind for kind in PR_TYPES if kind.signal.search(declared)]


def find_section_problems(body: DocBody, headings: list[str]) -> list[str]:
    """Sections follow the PR's type; Tests, Reviews and Verified sit folded under Checks."""
    problems: list[str] = []
    if any(VERDICT_LABELS["Type"].search(name) for name, _ in body.strip):
        kinds = read_pr_types(body)
        if not kinds:
            line = next(line for name, line in body.strip if VERDICT_LABELS["Type"].search(name))
            problems.append(
                f'the verdict strip\'s Type "{_shorten(line)}" names no PR type; use one or two of: '
                f"{', '.join(kind.name for kind in PR_TYPES)}"
            )
        missing: dict[str, str] = {}
        for kind in kinds:
            for needed in kind.sections:
                if not any(_SECTION_RES[needed].match(text) for text in headings):
                    missing.setdefault(needed, kind.name)
        problems.extend(
            f"{'an' if kind[0] in 'aeiou' else 'a'} {kind} PR has no '{needed}' h2" for needed, kind in missing.items()
        )
        if any(kind.reuse for kind in kinds):
            close = next((text for name, text in body.sections.items() if _SECTION_RES[_CLOSE_BREAK].match(name)), None)
            if close is not None and not _REUSE_RE.search(close):
                problems.append(
                    f"'{_CLOSE_BREAK}' has no Reuse line: a PR that adds a model, helper or concept names the existing "
                    "one closest to it, and whether it was reused or why not"
                )
    for text in headings:
        if _LOOSE_CHECK_RE.match(text):
            problems.append(
                f"'{text}' is an h2; Tests, Reviews and Verified are h3s inside the folded '{CHECKS}' section"
            )
    if not any(_WHERE_TO_LOOK_RE.match(text) for text in headings):
        problems.append("it has no 'Where to look' h2: the files in reading order, each with what to check there")
    if not any(_OWN_CHECKS_RE.match(text) for text in headings):
        problems.append(
            f"it has no '{CHECKS}' h2: Tests, Reviews and Verified sit in one fold under it, its summary line the "
            "verdict"
        )
    return problems


def find_layer_problems(body: DocBody, limits: "PlainLimits") -> list[str]:
    problems: list[str] = []
    if body.visible_words > limits.visible_words:
        problems.append(
            f"it shows {body.visible_words} words before anything is opened (Fields left out), over "
            f"{limits.visible_words}; fold detail under one-line summaries and cut what the reader does not need"
        )
    ids = [match.group(0).strip() for summary in body.summaries if (match := _SUMMARY_ID_RE.match(summary))]
    if ids:
        problems.append(
            f"{len(ids)} fold summaries start with an id ({', '.join(ids[:4])}{', …' if len(ids) > 4 else ''}); start "
            "each with its point, the id after it if kept"
        )
    return problems


@dataclass
class VisibleTop:
    """What the owner reads before opening anything: In short, User story if any and What changes and why, without
    the bodies of folds, diagrams, code blocks and marks. `blocks` are paragraphs, list items, table cells and fold
    summaries as text."""

    blocks: list[str] = field(default_factory=list[str])
    bullets: list[str] = field(default_factory=list[str])
    """In short's top-level list items, each with all of its visible text."""
    story_steps: int | None = None
    """User story's visible top-level items of numbered lists; None when the second h2 is not User story."""


class _TopReader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.top = VisibleTop()
        self.verified: list[str] = []
        self._section = ""
        self._sections = 0
        self._in_heading = False
        self._heading: list[str] = []
        self._unread = 0
        self._marks = 0
        self._span_marks: list[bool] = []
        self._details: list[list[bool]] = []
        """Per details element we are in: [has the open attribute, inside its summary now]."""
        self._lists = 0
        self._ordered = False
        self._block: list[str] = []
        self._bullet: list[str] | None = None
        self._part: list[str] | None = None
        self._in_verified = False
        """Under Checks, only its Verified part says the first reader passed; an old Verified h2 says it anywhere."""

    def _reading(self) -> bool:
        return self._section in _TOP_SECTIONS and not self._unread and not self._marks and not self._folded()

    def _folded(self) -> bool:
        return any(not opened and not in_summary for opened, in_summary in self._details)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _UNREAD_TAGS:
            self._unread += 1
            return
        if self._unread:
            return
        if tag == "h3" and self._section == "verified":
            self._part = []
            return
        if tag == "h2" and not self._folded():
            if self._in_heading:
                self._end_heading()
            self._end_block()
            self._end_bullet()
            self._in_heading, self._heading = True, []
            return
        if tag == "br":
            self.handle_data(" ")
            return
        if tag == "span":
            is_mark = "mark" in (dict(attrs).get("class") or "").split()
            self._span_marks.append(is_mark)
            self._marks += is_mark
            return
        if _BLOCK_TAG_RE.fullmatch(tag):
            self._end_block()
        if tag == "details":
            self._details.append(["open" in dict(attrs), False])
        elif tag == "summary" and self._details:
            self._details[-1][1] = True
        elif tag in ("ul", "ol"):
            self._lists += 1
            if self._lists == 1:
                self._ordered = tag == "ol"
        elif tag == "li" and self._lists == 1 and not self._folded():
            if self._section == "short":
                self._end_bullet()
                self._bullet = []
            elif self._section == "story" and self._ordered and self.top.story_steps is not None:
                self.top.story_steps += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br" and not self._unread:
            self.handle_data(" ")
        elif _BLOCK_TAG_RE.fullmatch(tag) and not self._unread:
            self._end_block()

    def handle_endtag(self, tag: str) -> None:
        if tag in _UNREAD_TAGS:
            self._unread = max(0, self._unread - 1)
            return
        if self._unread:
            return
        if tag == "h2" and self._in_heading:
            self._end_heading()
            return
        if tag == "h3" and self._part is not None:
            self._in_verified = bool(_VERIFIED_PART_RE.match(" ".join("".join(self._part).split())))
            self._part = None
            return
        if tag == "span":
            if self._span_marks:
                self._marks -= self._span_marks.pop()
            return
        if _BLOCK_TAG_RE.fullmatch(tag):
            self._end_block()
        if tag == "summary" and self._details:
            self._details[-1][1] = False
        elif tag == "details" and self._details:
            self._details.pop()
        elif tag in ("ul", "ol") and self._lists:
            if self._lists == 1:
                self._end_bullet()
            self._lists -= 1
        elif tag == "li" and self._lists == 1:
            self._end_bullet()

    def handle_data(self, data: str) -> None:
        if self._unread:
            return
        if self._in_heading:
            self._heading.append(data)
        elif self._part is not None:
            self._part.append(data)
        elif self._section == "verified" and self._in_verified and not self._marks:
            self.verified.append(data)
        elif self._reading():
            self._block.append(data)
            if self._bullet is not None:
                self._bullet.append(data)

    def close(self) -> None:
        super().close()
        if self._in_heading:
            self._end_heading()
        self._end_block()
        self._end_bullet()

    def _end_heading(self) -> None:
        self._in_heading = False
        text = " ".join("".join(self._heading).split())
        self._sections += 1
        if self._sections == 1 and _IN_SHORT_RE.match(text):
            self._section = "short"
        elif self._sections == 2 and _USER_STORY_RE.match(text):
            self._section = "story"
            self.top.story_steps = 0
        elif self._section in _TOP_SECTIONS and _WHAT_CHANGES_RE.match(text):
            self._section = "changes"
        elif _CHECKS_RE.match(text):
            self._section = "verified"
            self._in_verified = bool(_VERIFIED_PART_RE.match(text))
        else:
            self._section = "done" if self._section in _TOP_SECTIONS else ""
        self._lists = 0
        self._details = []

    def _end_block(self) -> None:
        text = " ".join("".join(self._block).split())
        if text:
            self.top.blocks.append(text)
        self._block = []
        if self._bullet is not None:
            self._bullet.append(" ")
        if self._section == "verified":
            self.verified.append(" ")

    def _end_bullet(self) -> None:
        if self._bullet is not None:
            self.top.bullets.append(" ".join("".join(self._bullet).split()))
            self._bullet = None


def read_visible_top(page: bytes) -> tuple[VisibleTop, str]:
    """The visible top, and the text of the Checks section, folds included."""
    reader = _TopReader()
    try:
        reader.feed(page.decode("utf-8", "replace"))
        reader.close()
    except (AssertionError, ValueError):
        pass
    return reader.top, " ".join("".join(reader.verified).split())


def split_into_sentences(block: str) -> list[str]:
    sentences: list[str] = []
    start = 0
    for match in _SENTENCE_END_RE.finditer(block):
        if _ABBREVIATION_END_RE.search(block[start : match.start()]):
            continue
        sentences.append(block[start : match.start()].strip())
        start = match.end()
    sentences.append(block[start:].strip())
    return [sentence for sentence in sentences if sentence]


def count_words(text: str) -> int:
    return len(_WORD_RE.findall(text))


def list_top_sentences(top: VisibleTop) -> list[str]:
    return [sentence for block in top.blocks for sentence in split_into_sentences(block)]


@dataclass(frozen=True)
class PlainLimits:
    bullets: int
    bullet_words: int
    sentence_words: int
    visible_words: int


DEFAULT_LIMITS = PlainLimits(bullets=5, bullet_words=25, sentence_words=30, visible_words=900)


def find_plainness_problems(top: VisibleTop, limits: PlainLimits) -> list[str]:
    problems: list[str] = []
    if len(top.bullets) > limits.bullets:
        problems.append(f"{IN_SHORT} has {len(top.bullets)} bullets; at most {limits.bullets}")
    for bullet in top.bullets:
        words = count_words(bullet)
        if words > limits.bullet_words:
            problems.append(
                f"an {IN_SHORT} bullet has {words} words, over {limits.bullet_words}; keep the point and fold the "
                f'rest: "{_shorten(bullet)}"'
            )
    for sentence in list_top_sentences(top):
        words = count_words(sentence)
        if words > limits.sentence_words:
            problems.append(
                f"a sentence above the folds has {words} words, over {limits.sentence_words}; split it or fold it: "
                f'"{_shorten(sentence)}"'
            )
    return problems


def find_story_problems(headings: list[str], steps: int | None) -> list[str]:
    """`headings` are every h2, folded ones too; `steps` is None when the second visible h2 is not User story. A doc
    without a User story passes: only a PR that changes what a person does or sees has one."""
    if steps is None:
        story = [index for index, text in enumerate(headings) if _USER_STORY_RE.match(text)]
        if not story or story[0] == 0:
            return []
        if story[0] == 1:
            return [f"'{USER_STORY}' is inside a fold; it is a visible h2, right after '{IN_SHORT}'"]
        return [f"'{USER_STORY}' comes after '{headings[story[0] - 1]}'; it is the h2 right after '{IN_SHORT}'"]
    if steps < MIN_STORY_STEPS:
        return [
            f"'{USER_STORY}' has {steps} numbered steps; at least {MIN_STORY_STEPS}, as items of an <ol> outside "
            "closed folds, each what the person does and sees today and after this PR"
        ]
    return []


def _shorten(text: str, words: int = 14) -> str:
    parts = text.split()
    return " ".join(parts[:words]) + (" …" if len(parts) > words else "")


_QUOTE_RE = re.compile(r"[\"\u201c\u201d\u00ab\u00bb]")
_OWNER_WORDS_RE = re.compile(r"Q#\d+")
_PRODUCT_LINE_RE = re.compile(r"^\W*product decisions?\b\W*(?P<rest>.*)$", re.I)
_NONE_RE = re.compile(r"^\W*(?:none|no\b|nothing|n/a)", re.I)


class _DecidedBlocks(HTMLParser):
    """Each folded block with a decided mark, as its summary and whether it carries the owner's words (a Q#N, a
    quotation) or a technical mark."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.unbacked: list[str] = []
        self._open: list[dict[str, object]] = []
        self._spans: list[bool] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _HIDDEN_TAGS:
            self._hidden += 1
        if self._hidden:
            return
        if tag == "details":
            self._open.append({"summary": [], "in_summary": False, "decided": False, "backed": False})
        elif tag == "summary" and self._open:
            self._open[-1]["in_summary"] = True
        elif tag in ("blockquote", "q"):
            self._back()
        if tag == "span":
            classes = (dict(attrs).get("class") or "").split()
            self._spans.append("mark" in classes)
            if "mark" in classes and self._open:
                if "decided" in classes:
                    self._open[-1]["decided"] = True
                if "technical" in classes:
                    self._back()

    def handle_endtag(self, tag: str) -> None:
        if tag in _HIDDEN_TAGS:
            self._hidden = max(0, self._hidden - 1)
            return
        if self._hidden:
            return
        if tag == "summary" and self._open:
            self._open[-1]["in_summary"] = False
        elif tag == "span" and self._spans:
            self._spans.pop()
        elif tag == "details" and self._open:
            block = self._open.pop()
            if block["decided"] and not block["backed"]:
                self.unbacked.append(" ".join("".join(block["summary"]).split()))  # type: ignore[arg-type]

    def handle_data(self, data: str) -> None:
        if self._hidden or not self._open:
            return
        if self._open[-1]["in_summary"] and not any(self._spans):
            self._open[-1]["summary"].append(data)  # type: ignore[union-attr]
        if _OWNER_WORDS_RE.search(data) or _QUOTE_RE.search(data):
            self._back()

    def _back(self) -> None:
        for block in self._open:
            block["backed"] = True


def find_decision_problems(page: str, blocks: "list[_Block]", owner: str) -> list[str]:
    """A decided mark or a Product decisions line says the owner decided: it carries their words (in quotes) or the
    Q#N of their answer. A technical decision the author made is marked `technical` as well."""
    reader = _DecidedBlocks()
    try:
        reader.feed(page)
        reader.close()
    except (AssertionError, ValueError):
        pass
    problems = [
        f"the decided block '{_shorten(summary)}' has no Q#N and no quoted words of {owner}: a product decision "
        f"(what a user, an admin or an operator does or sees) is decided only on {owner}'s words; "
        "add them, or if it is a technical choice you made yourself add the mark technical"
        for summary in reader.unbacked
    ]
    for block in blocks:
        line = _PRODUCT_LINE_RE.match(" ".join("".join(block.text).split()))
        if (
            line
            and not _NONE_RE.match(line["rest"])
            and not (_OWNER_WORDS_RE.search(line["rest"]) or _QUOTE_RE.search(line["rest"]))
        ):
            problems.append(
                f"the Product decisions line '{_shorten(line['rest'])}' has no Q#N and no quoted words of {owner}"
            )
    return problems


_TEST_PATH_RE = re.compile(
    r"(?:^|/)(?:tests?|__tests__|e2e|__snapshots__)/|(?:^|/)test_[^/]*$|[._]tests?\.\w+$|\.spec\.\w+$"
)


RASTER_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")


class _VisibleImages(HTMLParser):
    """The src of every <img> the doc shows when it opens: outside any <details> that is closed."""

    def __init__(self) -> None:
        super().__init__()
        self.srcs: list[str] = []
        self._details: list[bool] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "details":
            self._details.append("open" in values)
        elif tag == "img" and all(self._details) and values.get("src"):
            self.srcs.append(str(values["src"]))

    def handle_endtag(self, tag: str) -> None:
        if tag == "details" and self._details:
            self._details.pop()


def read_visible_image_names(page: str) -> set[str]:
    reader = _VisibleImages()
    try:
        reader.feed(page)
        reader.close()
    except (AssertionError, ValueError):
        pass
    return set(reader.srcs)


def find_screenshot_problems(
    page: str, changed: list[str] | None, ui_paths: tuple[str, ...], shots: list[str]
) -> list[str]:
    """A PR that changes UI code (a file under a configured glob that is not a test) ships a doc with a screenshot: a
    raster image (`shots`, the doc's png/jpg/jpeg/webp files that exist beside it). `changed` is the PR's changed
    files; None or empty when they cannot be read, which refuses, as UI code cannot be ruled out."""
    if not ui_paths:
        return []
    if not changed:
        return [
            "the PR's changed files cannot be read (it has no review worktree yet, or the worktree lacks its commits), "
            "so the doc cannot be checked for the screenshots a UI change needs: register it again once the PR has "
            "its review worktree"
        ]
    ui = [
        path
        for path in changed
        if not _TEST_PATH_RE.search(path) and any(fnmatch.fnmatchcase(path, glob) for glob in ui_paths)
    ]
    if not ui:
        return []
    if shots and not set(shots) & read_visible_image_names(page):
        return [
            f"the PR changes UI code ({ui[0]}{f' and {len(ui) - 1} more' if len(ui) > 1 else ''}) and the doc's "
            "screenshots are all inside a closed <details>: put at least one outside any fold, visible on opening"
        ]
    if shots:
        return []
    return [
        f"the PR changes UI code ({ui[0]}{f' and {len(ui) - 1} more' if len(ui) > 1 else ''}) but the doc has no "
        "screenshot: take them of the changed screens, today and after, on the sandbox or locally with Playwright in "
        "a hidden window, save them as png, jpg or webp files beside the doc and show them with "
        '<img src="name.png"> (an svg, a data: image or a missing file does not count)'
    ]


MIN_SVG_TEXT_PX = 14
_CSS_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_SVG_TEXT_SELECTOR_RE = re.compile(r"(?:^|[\s>+~])(?:text|tspan)(?![\w-])", re.I)
_FONT_PX_RE = re.compile(r"(?<![\w-])font(?:-size)?\s*:\s*(?:[^;]*?\s)?(\d+(?:\.\d+)?)px", re.I)
_FONT_SIZE_ATTR_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)(?:px)?\s*$")


class _SvgTextSizes(HTMLParser):
    """Every px size a doc gives its SVG text, from its own style rules and from <text> elements' attributes."""

    def __init__(self) -> None:
        super().__init__()
        self.sized: list[tuple[str, float]] = []
        self._in_style = False
        self._svg_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "svg":
            self._svg_depth += 1
        elif tag == "style":
            self._in_style = True
        elif tag in ("text", "tspan") and self._svg_depth:
            given = {name: value or "" for name, value in attrs}
            if (m := _FONT_SIZE_ATTR_RE.match(given.get("font-size", ""))) or (
                m := _FONT_PX_RE.search(given.get("style", ""))
            ):
                self.sized.append((f"<{tag}> with font-size {m.group(1)}px", float(m.group(1))))

    def handle_endtag(self, tag: str) -> None:
        if tag == "svg":
            self._svg_depth = max(0, self._svg_depth - 1)
        elif tag == "style":
            self._in_style = False

    def handle_data(self, data: str) -> None:
        if not self._in_style:
            return
        for selector, declarations in _CSS_RULE_RE.findall(data):
            parts = [p.strip() for p in selector.split(",") if _SVG_TEXT_SELECTOR_RE.search(p.strip())]
            if parts and (m := _FONT_PX_RE.search(declarations)):
                self.sized.append((f"the rule '{parts[0]}' sets {m.group(1)}px", float(m.group(1))))


def find_small_svg_text_problems(page: str) -> list[str]:
    """Diagram labels under MIN_SVG_TEXT_PX: the shared style keeps a page's own px sizes, so a doc that sets its SVG
    text small stays small however big the rest of the page is."""
    reader = _SvgTextSizes()
    reader.feed(page)
    small = list(dict.fromkeys(where for where, px in reader.sized if px < MIN_SVG_TEXT_PX))
    return [
        f"SVG text is under {MIN_SVG_TEXT_PX}px: {where}; set it to {MIN_SVG_TEXT_PX}px or more and widen the diagram"
        for where in small
    ]


ROUNDS_SUFFIX = ".reader-rounds"
"""A review doc's first-reader rounds sit beside it, one JSON line per round:
{"round": 2, "terms": ["..."], "explained": "right"}."""


def read_reader_rounds(doc: str) -> list[tuple[int, list[str], str]]:
    """The recorded rounds of the first reader for the doc at this path, oldest first; none when nothing is recorded."""
    try:
        with open(doc + ROUNDS_SUFFIX, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    rounds: list[tuple[int, list[str], str]] = []
    for line in lines:
        try:
            entry = json.loads(line)
            rounds.append(
                (int(entry["round"]), [str(term) for term in entry["terms"]], str(entry.get("explained", "")))
            )
        except (ValueError, KeyError, TypeError):
            continue
    return rounds


def record_reader_round(doc: str, number: int, terms: list[str], explained: str) -> None:
    with open(doc + ROUNDS_SUFFIX, "a", encoding="utf-8") as f:
        f.write(json.dumps({"round": number, "terms": terms, "explained": explained}) + "\n")


FREE_ROUNDS = 3
"""Rounds a doc may record without an allowance; more readers than this mean the doc's top needs rework."""


def read_round_allowance(doc: str) -> int:
    """The highest round recorded as allowed for this doc, FREE_ROUNDS when none."""
    allowed = FREE_ROUNDS
    try:
        with open(doc + ROUNDS_SUFFIX, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return allowed
    for line in lines:
        try:
            allowed = max(allowed, int(json.loads(line)["allow_rounds"]))
        except (ValueError, KeyError, TypeError):
            continue
    return allowed


def record_round_allowance(doc: str, up_to: int, reason: str) -> None:
    with open(doc + ROUNDS_SUFFIX, "a", encoding="utf-8") as f:
        f.write(json.dumps({"allow_rounds": up_to, "reason": reason}) + "\n")


_CLICK_TERM_RE = re.compile(r"<ins\b[^>]*>.*?</ins>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]*>")


def find_bare_terms(text: str, terms: list[str]) -> list[str]:
    """The terms the doc still has outside a click term (<ins title=...>)."""
    plain = html.unescape(_TAG_RE.sub(" ", _CLICK_TERM_RE.sub(" ", text))).casefold()
    return [term for term in terms if term.casefold() in plain]


def find_first_reader_problems(verified: str, rounds: list[tuple[int, list[str], str]], text: str) -> list[str]:
    if _FIRST_READER_PASSED_RE.search(verified):
        if not rounds:
            return [
                f"{CHECKS} says the first reader passed, but no round is recorded: after each reader, run "
                "review_doc_check.py <doc> --reader-round N [--term T ...] with every term it flagged"
            ]
        number, terms, explained = rounds[-1]
        if explained != "right":
            return [
                f"{CHECKS} says the first reader passed, but round {number} is not recorded as explained right "
                "(--explained right): rewrite, ask a new reader and record its round"
            ]
        if bare := find_bare_terms(text, terms):
            return [
                f"{CHECKS} says the first reader passed, but round {number} flagged terms the doc still uses bare "
                f"({', '.join(bare[:3])}): wrap each as <ins title=...>term</ins> or drop it"
            ]
        return []
    return [
        f"{CHECKS} does not say the first reader (with the reader profile) passed: before registering, a fresh reader "
        "gets the reader profile, the verdict strip, In short, User story if any, What changes and why, Fields if any, "
        "What else could break and the diff, and explains back what changes and why; wrong or vague means rewrite, "
        "and the doc says once it passed"
    ]


_BEFORE_LABEL_RE = re.compile(
    r"^\W*before(?:\s*/\s*after|\s+(?:and|vs\.?|&)\s+after|\s+this\s+(?:PR|change))?\b"
    r"(?:\W*$|\s*[:(\u2013\u2014]|\s+-\s)",
    re.I,
)
_COMMIT_RE = re.compile(r"`[0-9a-f]{7,40}`")
_DIAGRAM_SPLIT_RE = re.compile(r"[\[\](){}\"|\n;]|-->|---|==>|\bsubgraph\b")
_WHOLE_TAGS = frozenset({"table", "figure", "svg"})
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_BRANCH_RE = re.compile(
    r"\bbranch\s+(?:`[^`]+`|['\"\u2018\u201c][^'\"\u2019\u201d\s]+|\w+(?:[/_.-]\w+)+|\w*\d[\w./-]*)|`[^`\s]+`\s+branch\b",
    re.I,
)
_HYPOTHETICAL_RE = re.compile(r"\bhypothetical", re.I)


_GLOSSARY_HEADING_RE = re.compile(r"^[\W\d]*(?:words used|(?:key )?terms|glossary|definitions)\b", re.I)
_TERM_PAIR_RE = re.compile(
    r"^(?!(?:scenario|who|handling|today|before|after)\b)[^\s:=]+(?:\s+[^\s:=]+){0,3}?\s*(?::\s|\s[\u2013\u2014-]\s|\s=\s)\S",
    re.I,
)
"""A 'Term: meaning.' sentence; the labels of a risk's parts and of a story step are not terms."""
MIN_GLOSSARY_PAIRS = 2
_LIST_TAGS = frozenset({"ul", "ol", "dl"})
_BR = "\x00"


@dataclass
class _Block:
    text: list[str] = field(default_factory=list[str])
    before: str = ""
    """Where its first Before is, as the refusal names it; empty when it shows none."""


class _BlockReader(HTMLParser):
    """The doc as blocks in reading order, the same blocks the plainness check reads (paragraphs, list items,
    headings, summaries), except that a table, a figure, an inline svg and a Mermaid source are one block each with
    everything inside them: a caption, an image's alt text and a diagram's labels belong to their figure. Inside a
    figure, each paragraph and heading is still read as its own line."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[_Block] = []
        self.glossaries: list[str] = []
        self._section = ""
        self._hidden = 0
        self._whole: list[tuple[str, bool]] = []
        """Open tables, figures and diagrams, each with whether it is a diagram."""
        self._block: _Block | None = None
        self._line: list[str] = []
        self._line_tag = ""
        """The tag that opened the line, when it is outside lists and tables: only a paragraph is read as prose."""
        self._lists = 0
        self._heading: list[str] | None = None
        self._terms: tuple[str, int, bool] | None = None
        """Inside a section titled like a glossary: its heading, the term pairs in its paragraphs, whether one
        paragraph was already refused on its own."""
        self._bold: list[str] | None = None
        self._rows: list[list[int]] = []
        """Per open table: [rows seen, cells seen in the current row]."""
        self._cell: list[str] | None = None
        self._header_cell = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _HIDDEN_TAGS:
            self._hidden += 1
            return
        if self._hidden:
            return
        classes = (dict(attrs).get("class") or "").split()
        mermaid = any(name == "mermaid" or name.endswith("-mermaid") for name in classes)
        if tag in _WHOLE_TAGS or mermaid:
            if not self._whole:
                self._start_block()
            self._whole.append((tag, tag == "svg" or mermaid))
        elif _is_block(tag):
            if not self._whole:
                self._start_block()
            self._end_line(tag)
        if tag not in _INLINE_TAGS:
            self._add(" ")
        self._lists += tag in _LIST_TAGS
        if tag in _HEADING_TAGS:
            self._end_terms()
            self._heading = []
        elif tag in ("b", "strong"):
            self._bold = []
        elif tag == "br":
            self._line.append(_BR)
        elif tag == "code":
            self._add("`")
        elif tag == "img":
            alt = dict(attrs).get("alt") or ""
            self._add(f" {alt} ")
            self._check_diagram(alt)
        elif tag == "table":
            self._rows.append([0, 0])
        elif tag == "tr" and self._rows:
            self._end_cell()
            self._rows[-1] = [self._rows[-1][0] + 1, 0]
        elif tag in ("th", "td") and self._rows:
            self._end_cell()
            self._rows[-1][1] += 1
            self._header_cell = tag == "th" or self._rows[-1][0] <= 1 or self._rows[-1][1] == 1
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in _HIDDEN_TAGS:
            self._hidden = max(0, self._hidden - 1)
            return
        if self._hidden:
            return
        if tag not in _INLINE_TAGS:
            self._add(" ")
        if tag in _HEADING_TAGS and self._heading is not None:
            text = " ".join("".join(self._heading).split())
            if tag == "h2":
                self._section = text
            if _BEFORE_LABEL_RE.match(text):
                self._mark(f"the heading '{text}'")
            if _GLOSSARY_HEADING_RE.match(text):
                self._terms = (text, 0, False)
            self._heading = None
        elif tag in ("b", "strong") and self._bold is not None:
            if _BEFORE_LABEL_RE.match(" ".join("".join(self._bold).split())):
                self._mark(self._where("a line"))
            self._bold = None
        elif tag == "code":
            self._add("`")
        elif tag in ("th", "td", "tr"):
            self._end_cell()
        if _is_block(tag) or tag in _WHOLE_TAGS:
            self._end_line("")
        if tag in _LIST_TAGS:
            self._lists = max(0, self._lists - 1)
        if any(opened == tag for opened, _ in self._whole):
            while self._whole and self._whole.pop()[0] != tag:
                pass
            if tag == "table" and self._rows:
                self._end_cell()
                self._rows.pop()
            if not self._whole:
                self._end_block()
        elif _is_block(tag) and not self._whole:
            self._end_block()

    def handle_data(self, data: str) -> None:
        if self._hidden:
            return
        self._add(data)
        for part in (self._line, self._heading, self._bold, self._cell):
            if part is not None:
                part.append(data)
        if any(diagram for _, diagram in self._whole):
            self._check_diagram(data)

    def close(self) -> None:
        super().close()
        self._end_cell()
        self._end_line("")
        self._end_block()
        self._end_terms()

    def _add(self, data: str) -> None:
        if self._block is None:
            self._block = _Block()
        self._block.text.append(data)

    def _mark(self, where: str) -> None:
        if self._block is None:
            self._block = _Block()
        self._block.before = self._block.before or where

    def _where(self, what: str) -> str:
        return f"{what} under '{self._section}'" if self._section else f"{what} before the first h2"

    def _check_diagram(self, text: str) -> None:
        if any(_BEFORE_LABEL_RE.match(piece.strip()) for piece in _DIAGRAM_SPLIT_RE.split(text)):
            self._mark(self._where("a diagram or image"))

    def _end_cell(self) -> None:
        if (
            self._cell is not None
            and self._header_cell
            and _BEFORE_LABEL_RE.match(" ".join("".join(self._cell).split()))
        ):
            self._mark(self._where("a table"))
        self._cell = None

    def _end_line(self, next_tag: str) -> None:
        """A line is the text from one block tag to the next: a paragraph, a list item, a caption."""
        lines = [" ".join(line.split()) for line in "".join(self._line).split(_BR)]
        text = " ".join(line for line in lines if line)
        if any(_BEFORE_LABEL_RE.match(line) for line in lines):
            self._mark(self._where("a line"))
        if self._line_tag == "p":
            pairs = sum(
                bool(_TERM_PAIR_RE.match(sentence)) for line in lines for sentence in split_into_sentences(line)
            )
            alone = pairs >= MIN_GLOSSARY_PAIRS
            if alone:
                self.glossaries.append(
                    f'{self._where("a paragraph")} ("{_shorten(text, 6)}") is a term list written as prose, with '
                    f"{pairs} 'term: meaning' pairs; make it a list (<ul> or <dl>) or a two-column table"
                )
            if self._terms is not None:
                heading, count, refused = self._terms
                self._terms = (heading, count + pairs, refused or alone)
        self._line = []
        self._line_tag = "" if self._lists or self._whole else next_tag

    def _end_terms(self) -> None:
        if self._terms is not None:
            heading, pairs, refused = self._terms
            if pairs >= MIN_GLOSSARY_PAIRS and not refused:
                self.glossaries.append(
                    f"'{heading}' defines {pairs} terms in paragraphs; make it a list (<ul> or <dl>) or a two-column "
                    "table"
                )
        self._terms = None

    def _start_block(self) -> None:
        self._end_block()
        self._block = _Block()

    def _end_block(self) -> None:
        if self._block is None:
            return
        if " ".join("".join(self._block.text).split()):
            self.blocks.append(self._block)
        self._block = None


def _is_block(tag: str) -> bool:
    return bool(_BLOCK_TAG_RE.fullmatch(tag)) or tag in _HEADING_TAGS


def read_blocks(page: str) -> _BlockReader:
    reader = _BlockReader()
    try:
        reader.feed(page)
        reader.close()
    except (AssertionError, ValueError):
        pass
    return reader


_NUMBER = r"(?:\d+(?:[.,]\d+)?|an?|one|two|three|four|five|six|a few|a couple of|several|half an?)"
_SPAN = rf"{_NUMBER}(?:\s*(?:[-\u2013]|to|or)\s*{_NUMBER})?"
_UNIT = r"(?:hours?|days?|weeks?|months?|sprints?)"
_PEOPLE = r"(?:engineers?|devs?|developers?|persons?|people|man|men|programmers?)"
_LABOR_RE = re.compile(rf"\b(?:{_SPAN}\s+)?(?:engineer|person|man|dev|developer|people|staff)[- ]?{_UNIT}\b", re.I)
"""A unit of someone's time: always a size, unless the sentence names the rule or tells what happened."""
_TIME_RE = re.compile(
    rf"\b{_SPAN}[- ]?{_UNIT}\s+(?:of\s+(?:\w+\s+)?(?:work|effort)\s+)?(?:for|per)\s+(?:{_NUMBER}|each|a single)?\s*"
    rf"(?:\w+\s+)?{_PEOPLE}\b"
    rf"|\b{_SPAN}[- ]?{_UNIT}\s+of\s+(?:dev(?:elopment)?\s+|engineering\s+|coding\s+)?(?:work|effort)\b",
    re.I,
)
"""Time for people, which is a size only when the sentence estimates work to do; else a schedule or a fact."""
_ESTIMATE_CUE_RE = re.compile(
    r"(?:~|\b(?:estimates?|estimated|est\.|sizing|size|takes?|needs?|requires?|effort|scope|"
    r"budget|eta|roughly|about|around|approx(?:imately)?|to (?:build|implement|ship|finish|deliver|do|write|fix|port|"
    r"migrate|add|make|land))\b)",
    re.I,
)
_RULE_WORDS_RE = re.compile(r"\b(?:never|not|no|instead of|rather than|avoid|don'?t)\b[^.,;:\n]{0,25}$", re.I)
"""Words just before a match that name the rule rather than size work ('in kLOC, never engineer-days')."""
_PAST_RE = re.compile(
    r"\b(?:lost|spent|took|wasted|burned|burnt|was|were|had|did|ago|yesterday|last (?:week|month|sprint|quarter))\b",
    re.I,
)
_FUTURE_RE = re.compile(r"\b(?:will|would|'ll|going to)\b", re.I)
_CLAUSE_END_RE = re.compile(r"(?<=[.!?;])\s+|\n+")
_PART_RE = re.compile(r"[,;]|\s(?:and|but|then)\s", re.I)
"""Where one part of a sentence ends: what happened and what will happen are told apart by their own part."""
SIZE_RULE = "the team sizes work in kLOC, never in days or weeks of someone's time"


def find_day_estimates(text: str) -> list[str]:
    """Each size of work to do written in someone's time ('3 engineer-days', 'would take 2 weeks for one person'), as
    written; not what happened ('lost 3 days of work'), a schedule ('on call 2 weeks for one engineer') or the rule
    itself ('in kLOC, never engineer-days')."""
    found: list[str] = []
    for sentence in _CLAUSE_END_RE.split(text):
        for pattern, needs_cue in ((_LABOR_RE, False), (_TIME_RE, True)):
            for match in pattern.finditer(sentence):
                if _RULE_WORDS_RE.search(sentence[: match.start()]):
                    continue
                starts = [m.end() for m in _PART_RE.finditer(sentence, 0, match.start())]
                ends = [m.start() for m in _PART_RE.finditer(sentence, match.end())]
                part = sentence[(starts[-1] if starts else 0) : (ends[0] if ends else len(sentence))]
                if _PAST_RE.search(part) and not _FUTURE_RE.search(part):
                    continue
                if needs_cue and not _ESTIMATE_CUE_RE.search(sentence):
                    continue
                found.append(" ".join(match.group(0).split()))
    return list(dict.fromkeys(found))


def map_estimates_to_problem(found: list[str]) -> str:
    quoted = ", ".join(f"'{estimate}'" for estimate in found[:3])
    return f"it sizes work in time ({quoted}); {SIZE_RULE}: say how many kLOC it adds or changes"


# A PR's head names real code as surely as a branch does: "#12 today", "PR #12 today", "acme/web#12 today".
_PR_TODAY_RE = re.compile(r"(?<![\w/])(?:PR\s*)?(?:[\w.-]+/[\w.-]+)?#\d+`? today\b", re.I)


def find_baseline_problems(reader: _BlockReader, base: str) -> list[str]:
    """Every Before says what it is: the base branch today, a named branch, or a hypothetical. A design alternative
    shown as Before reads as today's code. Its block, or the block just before or after it (the paragraph that
    introduces a table, a figure's caption under it), names the baseline."""
    names = "|".join(re.escape(name) for name in dict.fromkeys((base, "main")) if name)
    after_name = r"(?![\w/-]|\.\w)"
    today = re.compile(
        rf"(?<![\w/-])`?(?:{names})`? today\b|\bon `?(?:origin/)?(?:{names}){after_name}|\(`?(?:{names})`?\)", re.I
    )
    problems: list[str] = []
    blocks = reader.blocks
    for index, block in enumerate(blocks):
        if not block.before:
            continue
        near = " ".join(" ".join("".join(part.text).split()) for part in blocks[max(0, index - 1) : index + 2])
        if any(pattern.search(near) for pattern in (today, _PR_TODAY_RE, _BRANCH_RE, _COMMIT_RE, _HYPOTHETICAL_RE)):
            continue
        problems.append(
            f"{block.before} shows a Before without saying what it is; say it there or in the paragraph or caption "
            f"next to it: '{base or 'main'} today', 'PR #N today', 'branch <name>' (the name in code), or "
            "'hypothetical'"
        )
    return list(dict.fromkeys(problems))


def map_row_to_path(text: str) -> str:
    """A row may point at lines (path:12-40); the file is what it names."""
    return _LINE_SUFFIX_RE.sub("", text).rstrip("/")


def find_doc_problems(
    page: bytes,
    owner_reviewed: bool,
    owner: str,
    files: frozenset[str] | None,
    limits: PlainLimits,
    base: str,
    rounds: list[tuple[int, list[str], str]],
) -> list[str]:
    """What breaks the spec's order, plainness and Where to look rules, one plain line each. `files` holds every file
    and directory of the commit the doc describes; None when that commit cannot be read, so paths are not looked up."""
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
        problems.append(
            f"its first h2 is '{headings[0]}'; it opens with '{IN_SHORT}', with only the verdict strip before it"
        )
    text = page.decode("utf-8", "replace")
    body = read_doc_body(text)
    problems.extend(find_strip_problems(body, limits))
    problems.extend(find_section_problems(body, headings))
    top, verified = read_visible_top(page)
    problems.extend(find_story_problems(headings, top.story_steps))
    problems.extend(find_first_reader_problems(verified, rounds, text))
    problems.extend(find_field_table_problems(text))
    problems.extend(find_small_svg_text_problems(text))
    blocks = read_blocks(text)
    problems.extend(find_baseline_problems(blocks, base))
    problems.extend(find_decision_problems(text, blocks.blocks, owner))
    problems.extend(blocks.glossaries)
    if estimates := find_day_estimates("\n".join(" ".join(block.text) for block in blocks.blocks)):
        problems.append(map_estimates_to_problem(estimates))
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
    problems.extend(find_plainness_problems(top, limits))
    problems.extend(find_layer_problems(body, limits))
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


_META_RE = re.compile(rb"<meta\b[^>]*>", re.I)
_PR_HEAD_NAME_RE = re.compile(rb"""\bname\s*=\s*["']?pr-head\b""", re.I)
_CONTENT_SHA_RE = re.compile(rb"""\bcontent\s*=\s*["']?([0-9a-fA-F]{7,40})\b""")


def read_doc_head(html: bytes) -> str:
    """The commit the doc says it was checked against, from <meta name="pr-head" content="<sha>">; empty without one."""
    for tag in _META_RE.findall(html):
        sha = _CONTENT_SHA_RE.search(tag)
        if _PR_HEAD_NAME_RE.search(tag) and sha:
            return sha.group(1).decode().lower()
    return ""


def list_files_at(repo: str, commit: str) -> frozenset[str] | None:
    try:
        listed = subprocess.run(
            ["git", "-C", repo, "ls-tree", "-r", "-t", "--name-only", "-z", commit],
            capture_output=True, stdin=subprocess.DEVNULL, timeout=60, check=False,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return None
    if listed.returncode != 0:
        return None
    return frozenset(name for name in listed.stdout.decode("utf-8", "replace").split("\0") if name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check a review doc as `guild review doc` does, with its default limits; exit 1 on any problem."
    )
    parser.add_argument("file", help="the review doc, an HTML file")
    parser.add_argument("--base", default="main", help="the PR's base branch (default: main)")
    parser.add_argument(
        "--repo", default=".", help="a checkout that has the PR's commits, for Where to look paths (default: .)"
    )
    parser.add_argument(
        "--owner-reviewed", action="store_true", help="the owner has reviewed the PR: allows the Changes since section"
    )
    parser.add_argument("--reader-round", type=int, metavar="N", help="record first reader round N and exit")
    parser.add_argument("--term", action="append", default=[], help="a term that round flagged (repeat per term)")
    parser.add_argument(
        "--allow-rounds", type=int, metavar="N", help="allow reader rounds up to N for this doc and exit"
    )
    parser.add_argument("--reason", default="", help="why more rounds are needed (with --allow-rounds)")
    parser.add_argument(
        "--explained",
        choices=("right", "wrong"),
        help="whether that round's explain-back was right (with --reader-round)",
    )
    args = parser.parse_args(argv)
    if args.allow_rounds is not None:
        if not args.reason.strip():
            parser.error("--allow-rounds needs --reason")
        record_round_allowance(args.file, args.allow_rounds, args.reason)
        print(f"allowed reader rounds up to {args.allow_rounds}")
        return 0
    if args.reader_round is not None:
        if args.explained is None:
            parser.error("--reader-round needs --explained right|wrong")
        if args.reader_round > read_round_allowance(args.file):
            print(
                f"round {args.reader_round} is over the {FREE_ROUNDS} readers a doc gets: rework the top, "
                f"not more readers; if more are needed, run --allow-rounds {args.reader_round} --reason '...' first",
                file=sys.stderr,
            )
            return 1
        record_reader_round(args.file, args.reader_round, args.term, args.explained)
        print(f"recorded round {args.reader_round}: {len(args.term)} flagged terms")
        return 0
    try:
        with open(args.file, "rb") as f:
            page = f.read()
    except OSError as exc:
        print(f"cannot read {args.file}: {exc}", file=sys.stderr)
        return 2
    # As registration does: the paths at the commit the doc names, else at the PR's head, here the checkout's.
    commit = read_doc_head(page) or "HEAD"
    files = list_files_at(args.repo, commit)
    problems = find_doc_problems(
        page, args.owner_reviewed, "the owner", files, DEFAULT_LIMITS, args.base, read_reader_rounds(args.file)
    )
    for problem in problems:
        print(f"- {problem}")
    if files is None:
        print(f"note: {args.repo} has no commit {commit}, so Where to look paths were not looked up", file=sys.stderr)
    if problems:
        return 1
    print("passes the checks guild review doc runs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
