import re
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
_VERIFIED_RE = re.compile(r"^\W*verified\b", re.I)
_FIRST_READER_PASSED_RE = re.compile(r"\bfirst[- ]reader\b(?![^.;]*\b(?:not|never|failed|pending)\b)[^.;]*\bpass", re.I)
_TOP_SECTIONS = ("short", "story", "changes")
_HIDDEN_TAGS = frozenset({"script", "style", "template", "textarea"})
_ROW_TAGS = ("li", "tr")
_UNREAD_TAGS = _HIDDEN_TAGS | {"svg", "pre"}
_BLOCK_TAG_RE = re.compile(
    r"p|li|[uo]l|t[dhr]|table|div|summary|details|fig(?:ure|caption)|blockquote|d[dt]|h[13-6]|section|hr"
)
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])[\"')\]\u201d\u2019]*\s+(?=\S)")
_ABBREVIATION_END_RE = re.compile(r"(?:\b(?:e\.g|i\.e|vs|etc|cf|approx|no|nr|incl)\.)$", re.I)
_WORD_RE = re.compile(r"\S*\w\S*")


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


@dataclass
class VisibleTop:
    """What the owner reads before opening anything: In short, User story and What changes and why, without the bodies
    of folds, diagrams, code blocks and marks. `blocks` are paragraphs, list items, table cells and fold summaries as
    text."""

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
        elif self._section == "verified" and not self._marks:
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
        elif _VERIFIED_RE.match(text):
            self._section = "verified"
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
    """The visible top, and the text of the Verified and not verified section."""
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
    """`headings` are every h2, folded ones too; `steps` is None when the second visible h2 is not User story."""
    if steps is None and headings:
        story = [index for index, text in enumerate(headings) if _USER_STORY_RE.match(text)]
        if not story:
            return [
                f"it has no '{USER_STORY}' h2: right after '{IN_SHORT}', one person's story as numbered steps (an "
                "<ol>), each what they do and see today and after this PR"
            ]
        if story[0] == 0:
            return []
        if story[0] == 1:
            return [f"'{USER_STORY}' is inside a fold; it is a visible h2, right after '{IN_SHORT}'"]
        return [f"'{USER_STORY}' comes after '{headings[story[0] - 1]}'; it is the h2 right after '{IN_SHORT}'"]
    if steps is not None and steps < MIN_STORY_STEPS:
        return [
            f"'{USER_STORY}' has {steps} numbered steps; at least {MIN_STORY_STEPS}, as items of an <ol> outside "
            "closed folds, each what the person does and sees today and after this PR"
        ]
    return []


def _shorten(text: str, words: int = 14) -> str:
    parts = text.split()
    return " ".join(parts[:words]) + (" …" if len(parts) > words else "")


def find_first_reader_problems(verified: str) -> list[str]:
    if _FIRST_READER_PASSED_RE.search(verified):
        return []
    return [
        "Verified does not say the first reader (with the reader profile) passed: before registering, a fresh reader "
        "gets the reader profile, In short, User story, What changes and why and the diff, and explains back what "
        "changes and why; wrong or vague means rewrite, and the doc says once it passed"
    ]


def map_row_to_path(text: str) -> str:
    """A row may point at lines (path:12-40); the file is what it names."""
    return _LINE_SUFFIX_RE.sub("", text).rstrip("/")


def find_doc_problems(
    page: bytes, owner_reviewed: bool, owner: str, files: frozenset[str] | None, limits: PlainLimits
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
        problems.append(f"its first h2 is '{headings[0]}'; it opens with '{IN_SHORT}', with nothing before it")
    top, verified = read_visible_top(page)
    problems.extend(find_story_problems(headings, top.story_steps))
    problems.extend(find_first_reader_problems(verified))
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
