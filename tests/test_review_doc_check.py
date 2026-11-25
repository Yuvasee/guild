import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from test_review_worktree import NOW, REVIEWS_CONFIG
from test_reviews import REF, setup_pr

from bridge import review_doc_check
from bridge.config import Config, map_toml_to_config
from bridge.db import transaction
from bridge.review_doc_check import PlainLimits, find_doc_problems
from bridge.review_worktree import DocError, read_registered_doc, register_doc

WHERE = """
<h2>Where to look</h2>
<p>Worktree: <code>~/wt/app-7</code>.</p>
<ol>
<li><a href="#"><code>src/app/run.py</code></a> - <code>start</code>, then the retry loop.</li>
<li><code>src/app/store.py:40-52</code> - the new column.</li>
</ol>
<table><tr><th>File</th><th>What</th></tr>
<tr><td><code>tests/test_run.py</code></td><td>the retry tests</td></tr></table>
"""


PASSED = "<p>First reader with the reader profile: passed.</p>"
STORY = (
    "<h2>User story</h2><p>A support agent answers a refund ticket.</p><ol>"
    "<li>Today: they open the order. After: the same.</li>"
    "<li>Today: the refund button errors. After: it is greyed out with a reason.</li>"
    "<li>Today: they ask a colleague. After: they read the reason and move on.</li></ol>"
)


def strip(kind: str = "New feature", **lines: str) -> str:
    """The verdict strip; a keyword argument replaces a label's line, None drops it."""
    labels = {"Type": kind, "You decide": "Nothing", "Could break": "Nothing", "Main risk": "None", "Checks": "Pass"}
    labels.update({label.replace("_", " "): line for label, line in lines.items()})
    rows = "".join(f"<dt>{label}</dt><dd>{line}</dd>" for label, line in labels.items() if line != "None ")
    return f'<dl class="verdict">{rows}</dl>'


CLOSE = "<h2>What else could break</h2><p>Reuse: no existing helper looks alike.</p>"
CHECKS = (
    "<h2>Checks</h2><details><summary>All pass, 2 reviews, 0 open</summary>"
    f"<h3>Tests</h3><h3>Reviews</h3><p>Codex, Claude</p><h3>Verified and not verified</h3>{PASSED}</details>"
)


def make_doc(where: str = WHERE, first: str = "In short", last: str = "", kind: str = "New feature") -> bytes:
    sections = ["What changes and why", "How it works", "Costs and risks"]
    body = f"<h2>{first}</h2><p>text</p>{STORY}" + "".join(f"<h2>{title}</h2><p>text</p>" for title in sections)
    return f"<html><body>{strip(kind)}{body}{CLOSE}{where}{CHECKS}{last}</body></html>".encode()


DEFAULTS = Config()
LIMITS = PlainLimits(
    DEFAULTS.doc_in_short_max_bullets,
    DEFAULTS.doc_bullet_max_words,
    DEFAULTS.doc_sentence_max_words,
    DEFAULTS.doc_visible_max_words,
)


def problems(page: bytes, reviewed: bool = False, files: frozenset[str] | None = None) -> list[str]:
    return find_doc_problems(page, reviewed, "Sam", files, LIMITS, "main")


FILES = frozenset({"src", "src/app", "src/app/run.py", "src/app/store.py", "tests", "tests/test_run.py"})
SINCE = "<h2>Changes since the last review</h2><details class='item'><summary>update</summary></details>"


def test_a_doc_in_the_specs_order_with_full_paths_passes() -> None:
    assert problems(make_doc(), files=FILES) == []
    assert problems(make_doc(last=SINCE), reviewed=True, files=FILES) == []


def test_the_doc_opens_with_in_short() -> None:
    assert problems(make_doc(first="Why")) == [
        "its first h2 is 'Why'; it opens with 'In short', with only the verdict strip before it"
    ]
    assert (
        problems(strip().encode() + b"<h1>Doc</h1><p>no sections</p>")[0]
        == "it has no h2 headings; it opens with the h2 'In short'"
    )
    hidden = b"<script>document.write('<h2>Top</h2>')</script>" + make_doc()
    assert problems(hidden) == [], "a heading inside a script is not the doc's"


def test_changes_since_the_last_review_waits_for_the_owners_review_and_comes_last() -> None:
    (refused,) = problems(make_doc(last=SINCE))
    assert "Sam has not reviewed this PR yet" in refused
    early = make_doc(last=SINCE).replace(SINCE.encode(), b"").replace(b"<h2>How", SINCE.encode() + b"<h2>How")
    assert problems(early, reviewed=True) == [
        "'How it works' comes after 'Changes since the last review', which is always the last h2"
    ]
    assert problems(make_doc(last=SINCE + SINCE), reviewed=True) == [
        "'Changes since the last review' is there 2 times; it is one section, the last h2"
    ]
    top = make_doc(first="Since the last review: 4 November")
    assert len(problems(top, reviewed=True)) == 2, "not first, and not last"


def test_where_to_look_names_each_file_once_by_its_path_from_the_repo_root() -> None:
    twice = WHERE.replace("tests/test_run.py", "src/app/store.py:90")
    assert problems(make_doc(twice)) == ["Where to look has src/app/store.py in 2 rows; one row per file"]
    for path in ("/srv/app/x.py", "~/wt/app-7", "./x.py", "../x.py", ".../app/x.py", "…/x.py", "src/../../x.py"):
        (refused,) = problems(make_doc(WHERE.replace("tests/test_run.py", path)))
        assert refused.startswith(f"Where to look has a row for {path}, which is not a path from the repo root")
    worktree_row = WHERE.replace(
        "<p>Worktree: <code>~/wt/app-7</code>.</p>", "<ul><li><code>~/wt/app-7</code></li></ul>"
    )
    assert len(problems(make_doc(worktree_row))) == 1, "the worktree path in a row instead of a paragraph"


def test_markup_a_browser_shows_is_read_as_it_shows_it() -> None:
    story = STORY.encode() + f"<h2>Where to look</h2><h2>Checks</h2><h3>Verified</h3>{PASSED}".encode()
    docs = strip("Docs only").encode()
    assert len(problems(docs + b"<h2>Why<h2>In short</h2>" + story)) == 2, "a heading left open ends at the next one"
    since = docs + b"<h2>In short</h2>" + story + b"<h2>Changes since the last review"
    assert len(problems(since)) == 1, "or at the end"
    unclosed = make_doc(WHERE.replace("<code>tests/test_run.py</code>", "<code>tests/missing.py"))
    assert len(problems(unclosed, files=FILES)) == 1, "a code left open ends with its row"
    nested = "<ul><li><code>src/app</code><ul><li>Check retries.</li></ul> then <code>retry()</code>.</li></ul>"
    assert problems(make_doc(WHERE + nested), files=FILES) == [], "the outer row already has its path"
    example = "<textarea><h2>Changes since the last review</h2></textarea><h2>Changes since startup</h2>"
    assert problems(make_doc(last=example)) == [], "a textarea's text and an app's own changes are not that section"
    assert problems(b"<h2>In short</h2>" + STORY.encode() + b"<![bogus]>")[0].startswith(
        "its HTML cannot be read past line 1"
    )


def test_a_path_the_commit_lacks_is_refused_and_without_the_commit_nothing_is_looked_up() -> None:
    bare = make_doc(WHERE.replace("src/app/run.py", "run.py"))
    (refused,) = problems(bare, files=FILES)
    assert refused.startswith("Where to look has a row for run.py, which is no file or folder at the commit")
    assert problems(bare, files=None) == []
    other_sections = make_doc().replace(b"<p>text</p>", b"<ul><li><code>run.py</code></li></ul>")
    assert problems(other_sections, files=FILES) == [], "rows are read from Where to look only"


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make_worktree(root: Path) -> tuple[Path, str]:
    """A worktree still at the first commit, whose repo has the second, which adds tests/test_run.py."""
    path = root / "wt"
    (path / "src" / "app").mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=path)
    git("config", "user.email", "t@e", cwd=path)
    git("config", "user.name", "t", cwd=path)
    for name in ("run.py", "store.py"):
        (path / "src" / "app" / name).write_text("x = 1\n")
    git("add", ".", cwd=path)
    git("commit", "-qm", "one", cwd=path)
    (path / "tests").mkdir()
    (path / "tests" / "test_run.py").write_text("x = 2\n")
    git("add", ".", cwd=path)
    git("commit", "-qm", "two", cwd=path)
    second = git("rev-parse", "HEAD", cwd=path)
    git("checkout", "-q", "HEAD~1", cwd=path)
    return path, second


def register(conn: sqlite3.Connection, tmp_path: Path, page: bytes) -> None:
    source = tmp_path / "doc.html"
    source.write_bytes(page)
    with transaction(conn):
        register_doc(conn, REVIEWS_CONFIG, REF, source, NOW)


def test_registration_checks_paths_at_the_docs_commit_in_the_review_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path / "state"))
    conn = setup_pr(tmp_path)
    worktree, second = make_worktree(tmp_path)
    with transaction(conn):
        conn.execute("UPDATE review_prs SET wt_path = ?", (str(worktree),))
    marked = make_doc().replace(b"<html>", f'<html><head><meta name="pr-head" content="{second}"></head>'.encode())
    register(conn, tmp_path, marked)
    assert read_registered_doc(conn, "Org/app", 7) == marked, "the checkout lags, the commit has the file"
    with pytest.raises(DocError) as refused:
        register(conn, tmp_path, marked.replace(b"src/app/run.py", b"run.py"))
    assert "run.py, which is no file or folder" in str(refused.value)
    unknown = marked.replace(second.encode(), b"0" * 40)
    register(conn, tmp_path, unknown.replace(b"src/app/run.py", b"run.py"))
    assert read_registered_doc(conn, "Org/app", 7) != marked, "a commit the repo lacks: paths are not looked up"


def test_a_refused_doc_lists_every_problem_names_the_spec_and_is_not_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path / "state"))
    conn = setup_pr(tmp_path)
    bad = make_doc(WHERE.replace("tests/test_run.py", "~/x.py"), first="Why", last=SINCE)
    with pytest.raises(DocError) as refused:
        register(conn, tmp_path, bad)
    message = str(refused.value)
    assert message.startswith(f"{REF}: the doc was not registered, as ~/.claude/skills/bridge/review-doc.md asks:")
    assert message.count("\n- ") == 3 and "'Why'" in message and "~/x.py" in message
    assert read_registered_doc(conn, "Org/app", 7) is None
    register(conn, tmp_path, make_doc())
    with pytest.raises(DocError):
        register(conn, tmp_path, bad)
    assert read_registered_doc(conn, "Org/app", 7) == make_doc(), "a refused update keeps the doc registered before"
    with transaction(conn):
        conn.execute("UPDATE review_prs SET owner_reviewed_at = ?", (NOW,))
    register(conn, tmp_path, make_doc(last=SINCE))
    assert read_registered_doc(conn, "Org/app", 7) == make_doc(last=SINCE), "after the owner's review, and last"


def steps(count: int, tag: str = "ol") -> str:
    return f"<h2>User story</h2><{tag}>" + "".join(f"<li>Step {n}.</li>" for n in range(count)) + f"</{tag}>"


def make_top(
    short: str, changes: str = "<p>One plain line.</p>", verified: str = PASSED, story: str = steps(3)
) -> bytes:
    rest = "<h2>How it works</h2><p>" + "word " * 60 + "ends here.</p><h2>Costs and risks</h2>"
    return (
        f"<html><body><h1>Title</h1>{strip()}<p>{'lead ' * 40}</p><h2>In short</h2>{short}{story}"
        f"<h2>What changes and why</h2>{changes}{rest}{CLOSE}<h2>Where to look</h2><h2>Checks</h2><h3>Verified</h3>"
        f"{verified}</body></html>"
    ).encode()


def words(count: int, end: str = ".") -> str:
    return " ".join(["Alpha", *["beta"] * (count - 1)]) + end


def bullets(*sizes: int) -> str:
    return "<ul>" + "".join(f"<li>{words(size)}</li>" for size in sizes) + "</ul>"


def test_in_short_holds_at_most_five_bullets_of_at_most_25_words() -> None:
    assert problems(make_top(bullets(25, 25, 25, 25, 25))) == []
    assert problems(make_top(bullets(5, 5, 5, 5, 5, 5))) == ["In short has 6 bullets; at most 5"]
    (refused,) = problems(make_top(bullets(5, 26)))
    assert refused.startswith('an In short bullet has 26 words, over 25; keep the point and fold the rest: "Alpha')
    nested = "<ul><li>Short point.<ul><li>" + words(30) + "</li></ul></li><li>Second.</li></ul>"
    (nested_refused,) = problems(make_top(nested))
    assert "bullet has 32 words" in nested_refused, "a nested list is part of its bullet, and not a bullet itself"
    folded = "<ul><li>Short point.<details><summary>Why</summary>" + words(80) + "</details></li></ul>"
    assert problems(make_top(folded)) == [], "a fold's body is not read"


def test_no_sentence_above_the_folds_runs_over_30_words() -> None:
    assert problems(make_top("<p>" + words(30) + " " + words(30) + "</p>")) == []
    (refused,) = problems(make_top("<p>Fine.</p>", "<p>" + words(31) + "</p>"))
    assert refused.startswith('a sentence above the folds has 31 words, over 30; split it or fold it: "Alpha')
    hidden = (
        "<details class='item'><summary>Short summary, D1</summary><p>" + words(60) + "</p></details>"
        "<pre>" + words(60) + "</pre><svg><text>" + words(60) + "</text></svg>"
        "<p>" + words(29, "") + ' <span class="mark observed">observed</span></p>'
    )
    assert problems(make_top("<p>Fine.</p>", hidden)) == [], "folds, code blocks, diagrams and marks are not read"
    (summary,) = problems(make_top("<p>Fine.</p>", "<details><summary>" + words(31) + "</summary></details>"))
    assert "31 words" in summary, "a fold's summary line shows"
    (opened,) = problems(
        make_top("<p>Fine.</p>", "<details open><summary>S</summary><p>" + words(31) + "</p></details>")
    )
    assert "31 words" in opened, "an open fold shows all it holds"
    cells = "<table><tr><td>" + words(20) + "</td><td>" + words(20) + "</td></tr></table>"
    assert problems(make_top("<p>Fine.</p>", cells)) == [], "each table cell is its own block"
    identifier = "<p>" + words(16) + " bridge_cli now " + words(14) + "</p>"
    assert problems(make_top("<p>Fine.</p>", identifier)) == [], "a sentence may start with a lowercase name"
    broken = "<p>" + words(16, "") + "<br>" + words(15) + "</p>"
    assert len(problems(make_top("<p>Fine.</p>", broken))) == 1, "a line break does not end a sentence"
    folded_heading = "<details><summary>More</summary><h2>Background</h2></details><p>" + words(31) + "</p>"
    assert len(problems(make_top(folded_heading))) == 1, "a heading in a fold starts no section"
    unclosed = (
        strip("Docs only").encode()
        + b"<h2>In short"
        + STORY.encode()
        + b"<h2>What changes and why</h2><p>"
        + words(31).encode()
        + b"</p>"
    ) + f"<h2>Where to look</h2><h2>Checks</h2><h3>Verified</h3>{PASSED}".encode()
    assert len(problems(unclosed)) == 1, "a heading left open ends at the next one"
    abbreviations = "<p>Seen in docs, e.g. Alpha and Beta, vs. Gamma, and " + words(22) + "</p>"
    assert len(problems(make_top("<p>Fine.</p>", abbreviations))) == 1, "e.g. and vs. end no sentence"


def test_the_limits_come_from_config() -> None:
    page = make_top(bullets(8, 8, 8), "<p>" + words(12) + "</p>")
    assert problems(page) == []
    strict = PlainLimits(bullets=2, bullet_words=7, sentence_words=10, visible_words=150)
    assert [
        line.split(";")[0].split(",")[0] for line in find_doc_problems(page, False, "Sam", None, strict, "main")
    ] == [
        "In short has 3 bullets",
        "an In short bullet has 8 words",
        "an In short bullet has 8 words",
        "an In short bullet has 8 words",
        "a sentence above the folds has 12 words",
        "it shows 195 words before anything is opened (Fields left out)",
    ]
    assert map_toml_to_config({"doc_visible_max_words": 600}).doc_visible_max_words == 600


def test_a_doc_whose_first_reader_did_not_pass_is_refused() -> None:
    assert problems(make_top("<p>x.</p>")) == []
    (refused,) = problems(make_top("<p>x.</p>", verified="<p>Unit tests pass. A reader read it.</p>"))
    assert refused.startswith("Checks does not say the first reader (with the reader profile) passed")
    assert "the diff, and explains back what changes and why" in refused
    assert len(problems(make_top("<p>x.</p>", verified="<p>The first reader did not pass.</p>"))) == 1
    cells = "<table><tr><td>First reader</td><td>passed</td></tr></table>"
    assert problems(make_top("<p>x.</p>", verified=cells)) == []


def test_the_limits_must_be_at_least_one() -> None:
    assert map_toml_to_config({"doc_sentence_max_words": 40}).doc_sentence_max_words == 40
    with pytest.raises(ValueError, match="doc_sentence_max_words must be at least 1"):
        map_toml_to_config({"doc_sentence_max_words": 0})


def test_a_user_story_is_optional_and_one_that_is_there_has_three_numbered_steps_right_after_in_short() -> None:
    assert problems(make_top("<p>Fine.</p>", story=steps(3))) == []
    assert problems(make_top("<p>Fine.</p>", story="")) == [], "a PR no person meets differently has no story"
    (short,) = problems(make_top("<p>Fine.</p>", story=steps(2)))
    assert short.startswith("'User story' has 2 numbered steps; at least 3")
    late = make_top("<p>Fine.</p>", story="").replace(
        b"<h2>How it works</h2>", steps(3).encode() + b"<h2>How it works</h2>"
    )
    assert problems(late) == ["'User story' comes after 'What changes and why'; it is the h2 right after 'In short'"]


def test_only_visible_top_level_items_of_a_numbered_list_count_as_steps() -> None:
    (bulleted,) = problems(make_top("<p>Fine.</p>", story=steps(3, "ul")))
    assert "has 0 numbered steps" in bulleted
    nested = "<h2>User story</h2><ol><li>One.<ol><li>a</li><li>b</li></ol></li><li>Two.</li></ol>"
    assert "has 2 numbered steps" in problems(make_top("<p>Fine.</p>", story=nested))[0]
    folded = "<h2>User story</h2><details><summary>Steps</summary>" + steps(3)[19:] + "</details>"
    assert "has 0 numbered steps" in problems(make_top("<p>Fine.</p>", story=folded))[0]
    assert problems(make_top("<p>Fine.</p>", story=folded.replace("<details>", "<details open>"))) == []
    hidden = "<details><summary>More</summary>" + steps(3) + "</details>"
    assert problems(make_top("<p>Fine.</p>", story=hidden)) == [
        "'User story' is inside a fold; it is a visible h2, right after 'In short'"
    ]


def test_without_a_user_story_what_changes_and_why_keeps_the_plain_doc_limits() -> None:
    (refused,) = problems(make_top("<p>Fine.</p>", "<p>" + words(31) + "</p>", story=""))
    assert refused.startswith("a sentence above the folds has 31 words")


def test_the_user_storys_sentences_keep_the_plain_doc_limits() -> None:
    long_step = "<h2>User story</h2><ol><li>" + words(31) + "</li><li>Two.</li><li>Three.</li></ol>"
    (refused,) = problems(make_top("<p>Fine.</p>", story=long_step))
    assert refused.startswith("a sentence above the folds has 31 words")


def test_each_table_of_fields_is_visible_with_what_why_limits_and_example_columns() -> None:
    header = "<tr><th>Field</th><th>What it is</th><th>Written by → read by</th><th>Limits</th>{}</tr>"
    row = "<tr><td><code>refund_reason</code></td><td>why</td><td>form → report</td><td>text</td>{}</td></tr>"
    bare = f"<h2>Fields</h2><table>{header.format('')}{row.format('')}</table>"
    with_example = (
        f"<h2>Fields</h2><table>{header.format('<th>Example</th>')}{row.format('<td>arrived damaged')}</table>"
    )
    other = "<h2>Fields</h2><table><tr><th>Reader</th><th>Reads</th></tr></table>"
    later = f"<h2>How it works</h2><table>{header.format('')}</table>"

    found = problems(make_doc(last=bare), files=FILES)

    assert found == [
        "Fields has a table of fields with no Example column (columns: Field | What it is | Written by → read by "
        "| Limits); per field: what it is, why it is needed (who writes, who reads), its limits, and a realistic "
        "example value in its real shape, invented, never real customer data"
    ]
    assert problems(make_doc(last=with_example), files=FILES) == []
    assert problems(make_doc(last=other), files=FILES) == [], "a table that lists no fields is not checked"
    assert problems(make_doc(last=later), files=FILES) == [], "only tables under Fields are checked"
    loose = "<h2>3. Fields</h2><table><tr><th>Field<th>Meaning<tr><td>mode<td>example</table>"
    assert len(problems(make_doc(last=loose), files=FILES)) == 1, "a numbered heading, rows with no end tags"
    assert len(problems(make_doc(last="<h2>Fields</h2><table><tr><th>Field<th>Meaning"), files=FILES)) == 1
    (no_why,) = problems(make_doc(last=with_example.replace("Written by → read by", "Notes")), files=FILES)
    assert "with no Why column" in no_why
    folded = with_example.replace("<table>", "<details><summary>All fields</summary><table>") + "</details>"
    assert problems(make_doc(last=folded), files=FILES) == [
        "Fields has 1 table(s) of fields inside a closed fold; every changed field of every changed model stays "
        "visible (only writer and reader file paths may fold)"
    ]
    assert problems(make_doc(last=folded.replace("<details>", "<details open>")), files=FILES) == []
    record = "<pre>{&quot;refund_reason&quot;: &quot;arrived damaged&quot;}</pre>"
    assert problems(make_doc(last=with_example.replace("<table>", record + "<table>")), files=FILES) == []


def test_a_verdict_strip_with_every_line_comes_before_in_short() -> None:
    page = make_doc()
    assert problems(page.replace(strip().encode(), b"")) == [
        "it has no verdict strip before 'In short': a <dl class=\"verdict\"> with Type, You decide, Could break, "
        "Main risk, Checks, one short line each"
    ]
    assert problems(page.replace(strip().encode(), strip(Main_risk="None ").encode())) == [
        "the verdict strip has no 'Main risk' line"
    ]
    (long_line,) = problems(page.replace(strip().encode(), strip(You_decide=words(26)).encode()))
    assert long_line == "the verdict strip's 'You decide' line has 26 words, over 25"
    later = page.replace(strip().encode(), b"").replace(b"<h2>How it works", strip().encode() + b"<h2>How it works")
    assert problems(later)[0].startswith("it has no verdict strip before 'In short'"), "a strip further down is not one"


def test_the_prs_type_picks_the_sections_it_needs() -> None:
    assert problems(make_doc(kind="Docs only")) == []
    (unknown,) = problems(make_doc(kind="Misc"))
    assert unknown.startswith('the verdict strip\'s Type "Misc" names no PR type; use one or two of: model / contract')
    assert problems(make_doc(kind="Bug fix")) == ["a bug fix PR has no 'Root cause' h2"]
    root_cause = make_doc(kind="Bug fix", where="<h2>Root cause</h2><p>x</p>" + WHERE)
    assert problems(root_cause) == []
    assert problems(make_doc(kind="Model / contract + enabling")) == [
        "a model / contract PR has no 'Fields' h2",
        "an enabling PR has no 'How this will be used' h2",
        "an enabling PR has no 'Rollout' h2",
    ]
    assert problems(make_doc(kind="Refactor")) == ["a refactor PR has no 'No behaviour change' h2"]
    assert problems(make_doc(kind="Backport")) == ["a backport PR has no 'Differences from the original' h2"]
    no_close = make_doc(kind="Infra / chart").replace(CLOSE.encode(), b"<h2>Rollout</h2><p>x</p>")
    assert problems(no_close) == ["an infra / chart PR has no 'What else could break' h2"]
    no_reuse = make_doc().replace(b"Reuse: no existing helper looks alike.", b"Callers: none.")
    assert problems(no_reuse) == [
        "'What else could break' has no Reuse line: a PR that adds a model, helper or concept names the existing "
        "one closest to it, and whether it was reused or why not"
    ]
    assert problems(make_doc(kind="Refactor").replace(b"Reuse", b"Callers")) == [
        "a refactor PR has no 'No behaviour change' h2"
    ], "only a PR that adds something needs a Reuse line"


def test_tests_reviews_and_verified_sit_folded_under_checks() -> None:
    loose = make_doc().replace(CHECKS.encode(), f"<h2>Tests</h2><h2>Reviews</h2><h2>Verified</h2>{PASSED}".encode())
    assert problems(loose) == [
        "'Tests' is an h2; Tests, Reviews and Verified are h3s inside the folded 'Checks' section",
        "'Reviews' is an h2; Tests, Reviews and Verified are h3s inside the folded 'Checks' section",
        "'Verified' is an h2; Tests, Reviews and Verified are h3s inside the folded 'Checks' section",
        "it has no 'Checks' h2: Tests, Reviews and Verified sit in one fold under it, its summary line the verdict",
    ]


def test_the_visible_layer_has_a_word_budget_that_leaves_fields_out() -> None:
    long_story = "<details open><summary>More</summary>" + ("<p>" + words(20) + "</p>") * 40 + "</details>"
    (refused,) = problems(make_doc(where=long_story + WHERE))
    assert refused.startswith("it shows 9")
    assert "words before anything is opened (Fields left out), over 900; fold detail" in refused
    assert problems(make_doc(where=long_story.replace(" open", "") + WHERE)) == [], (
        "a closed fold's body is not counted"
    )
    row = "<tr><td>f</td><td>" + words(20) + "</td><td>w</td><td>l</td><td>e</td></tr>"
    fields = "<h2>Fields</h2><table><tr><th>Field</th><th>What</th><th>Why</th><th>Limits</th><th>Example</th></tr>"
    assert problems(make_doc(last=fields + row * 60 + "</table>")) == [], "every field stays visible, uncounted"


def test_a_folds_summary_starts_with_the_point_not_an_id() -> None:
    fold = "<details class='item'><summary>{}</summary><p>body</p></details>"
    ids = "".join(fold.format(f"D{n}. Metrics capped at 12") for n in range(1, 6))
    assert problems(make_doc(where=ids + WHERE)) == [
        "5 fold summaries start with an id (D1., D2., D3., D4., …); start each with its point, the id after it if kept"
    ]
    for fine in ("Metrics capped at 12 (D3)", "S3 bucket kept", "2. Plan"):
        assert problems(make_doc(where=fold.format(fine) + WHERE)) == [], fine


def test_markup_that_hides_or_breaks_a_part_is_read_as_a_browser_shows_it() -> None:
    page = make_doc()
    only_tests = page.replace(b"<h3>Verified and not verified</h3>" + PASSED.encode(), b"<h3>Verified</h3><p>x</p>")
    only_tests = only_tests.replace(b"<h3>Tests</h3>", b"<h3>Tests</h3>" + PASSED.encode())
    assert problems(only_tests)[0].startswith("Checks does not say the first reader"), "only its Verified part says it"
    assert problems(page.replace(strip().encode(), f"<details><summary>V</summary>{strip()}</details>".encode()))[
        0
    ].startswith("it has no verdict strip"), "a folded strip is not seen first"
    unclosed_strip = page.replace(b"</dl>", b"")
    assert problems(unclosed_strip) == [], "each line ends with its dd, and the strip at the first h2"
    reuse_word = page.replace(b"Reuse: no existing helper looks alike.", b"Other services reuse this key.")
    assert problems(reuse_word)[0].startswith("'What else could break' has no Reuse line")
    open_heading = page.replace(CLOSE.encode(), b"<h2>What else could break")
    assert problems(open_heading)[0].startswith("'What else could break' has no Reuse line"), "an unclosed h2"
    titled = page.replace(b"<html>", b"<html><head><title>" + b"word " * 900 + b"</title></head>")
    assert problems(titled) == [], "the page title is not shown in the doc"
    fold = "<details class='item'><summary>{}</summary><p>body</p></details>"
    for refused in ("D3 — Metrics capped", "D100. Metrics capped"):
        assert problems(make_doc(where=fold.format(refused) + WHERE))[0].startswith("1 fold summaries start"), refused


def test_the_type_is_read_from_the_start_of_its_line_and_a_folded_fields_is_folded() -> None:
    assert problems(make_doc(kind="New feature, 120 lines plus model tests")) == []
    assert problems(make_doc(kind="Docs only, 40 lines on a bug fix")) == []
    table = "<table><tr><th>Field</th><th>What</th><th>Why</th><th>Limits</th><th>Example</th></tr></table>"
    folded = f"<details><summary>More</summary><h2>Fields</h2>{table}</details>"
    assert problems(make_doc(kind="Model / contract", last=folded))[0].startswith(
        "Fields has 1 table(s) of fields inside a closed fold"
    )
    hidden_heading = "<details><summary>More</summary><h2>Fields</h2></details><p>" + "word " * 1000 + "</p>"
    assert problems(make_doc(where=hidden_heading + WHERE))[0].startswith("it shows 1"), "a folded h2 exempts nothing"


TABLE = (
    "<table><tr><th></th><th>Before</th><th>After</th></tr><tr><td>Retries</td><td>none</td><td>three</td></tr></table>"
)
SVG = "<svg><text>Before</text><text>After</text></svg>"


def baseline(changes: str, base: str = "main") -> list[str]:
    page = make_top("<p>Fine.</p>", changes)
    return find_doc_problems(page, False, "Sam", None, LIMITS, base)


def test_a_before_with_no_baseline_near_it_is_refused() -> None:
    assert baseline(f"<p>The retry loop.</p>{TABLE}<p>Three tries now.</p>") == [
        "a table under 'What changes and why' shows a Before without saying what it is; say it there or in the "
        "paragraph or caption next to it: 'main today', 'branch <name>' (the name in code), or "
        "'hypothetical'"
    ]
    assert len(baseline(f"<figure>{SVG}<figcaption>Retries.</figcaption></figure>")) == 1, "an svg label"
    mermaid = '<pre class="mermaid">flowchart LR\n  A["Before: one try"] --> B["After: three"]</pre>'
    assert len(baseline(mermaid)) == 1, "a Mermaid label"
    assert len(baseline('<img src="a.png" alt="Before: one try">')) == 1, "an image's alt text"
    assert len(baseline("<h3>Before and after</h3><p>One try, then three.</p>")) == 1, "a heading"
    assert len(baseline("<p><b>Before:</b> one try.</p>")) == 1, "a bold label"
    assert len(baseline("<ul><li>Before: one try.</li></ul>")) == 1, "a labelled line"
    assert len(baseline(f"<p>Today on main.</p><p>Gap.</p>{TABLE}")) == 1, "two blocks away is not near"
    assert len(baseline("<p>Before: it fails on main.py.</p>")) == 1, "a file named main is no baseline"
    assert len(baseline("<p>Retry count.<br>Before: one try.<br>After: three.</p>")) == 1, "a line after a break"
    assert len(baseline("<p>Before: this branch - it retries once.</p>")) == 1, "a dash is no branch name"
    assert len(baseline("<figure><h3>Before</h3><p>One try.</p></figure>")) == 1, "a heading inside a figure"
    assert len(baseline("<figure><p>Retries.</p><p>Before: one try.</p></figure>")) == 1, "a line inside a figure"
    wrapper = '<div class="mermaid-wrapper"><p>Main today.</p><p>Gap.</p><p>Before: one try.</p></div>'
    assert len(baseline(wrapper)) == 1, "only the class mermaid is a diagram"


def test_a_before_that_names_main_today_a_branch_or_a_hypothetical_passes() -> None:
    assert baseline(f"<p>Before is main today.</p>{TABLE}") == []
    assert baseline(TABLE.replace("<th>Before</th>", "<th>Before (on main)</th>")) == []
    assert baseline(f"{TABLE}<p>Before is branch <code>feature/retry</code>, not merged.</p>") == []
    assert baseline("<p>Before: branch feature.retry has one try.</p>") == []
    assert baseline(f"<figure>{SVG}<figcaption>Before: the <code>retry-v2</code> branch.</figcaption></figure>") == []
    assert baseline(f"{TABLE}<p>Before is hypothetical: the design we did not take.</p>") == []
    assert baseline(f"<p>Before is release-2 today.</p>{TABLE}", base="release-2") == []
    assert len(baseline(f"<p>Before is release-2 today.</p>{TABLE}")) == 1, "only the PR's base names today"
    assert baseline("<p>Before (main): one try.</p>") == []
    assert baseline("<p>Before: main\n today.</p>") == [], "as a browser shows the words"
    assert baseline("<table><tr><th>Before</th><td>main today</td></tr></table>") == []
    assert baseline(f"<figure>{SVG}<figcaption>main today</figcaption></figure>") == []
    assert baseline("<p>Before and after: it fails on <code>origin/main</code> and passes here.</p>") == []
    assert baseline("<p>Before: <code>31e1f06e8</code>. After: <code>16b443814</code>.</p>") == []


def test_a_doc_without_a_before_passes() -> None:
    prose = (
        "<p>Before registering, run the tests.</p><p><b>Before you merge</b>, read it.</p>"
        "<p>Before/after timings of four shapes.</p><p>It lands <b>before this PR merges</b>.</p>"
    )
    assert baseline(prose + "<svg><text>retry before timeout</text></svg>") == []


def glossary(changes: str) -> list[str]:
    return [line for line in baseline(changes) if "term" in line]


def test_a_term_list_written_as_prose_is_refused() -> None:
    prose = "<p>Run: one pass of the scan. Window: the hour it covers. Cursor: where the last run stopped.</p>"
    assert glossary(prose) == [
        "a paragraph under 'What changes and why' (\"Run: one pass of the scan. …\") is a term list written as "
        "prose, with 3 'term: meaning' pairs; make it a list (<ul> or <dl>) or a two-column table"
    ]
    lines = "<p>Run \u2014 one pass.<br>Window \u2014 the hour.<br>Cursor = where it stopped</p>"
    assert len(glossary(lines)) == 1, "pairs on lines of their own"
    assert len(glossary("<p>Run:\none pass. Window:\nthe hour. Cursor:\nwhere it stopped.</p>")) == 1
    section = "<h3>Words used here</h3><p>Run: one pass.</p><p>Window: the hour.</p><p>Cursor: where it stopped.</p>"
    assert glossary(section) == [
        "'Words used here' defines 3 terms in paragraphs; make it a list (<ul> or <dl>) or a two-column table"
    ]


def test_a_term_list_as_a_list_or_a_table_and_a_paragraph_with_one_colon_pass() -> None:
    terms = [("Run", "one pass."), ("Window", "the hour."), ("Cursor", "where it stopped.")]
    dl = "<h3>Terms</h3><dl>" + "".join(f"<dt>{term}</dt><dd>{meaning}</dd>" for term, meaning in terms) + "</dl>"
    ul = "<ul>" + "".join(f"<li>{term}: {meaning}</li>" for term, meaning in terms) + "</ul>"
    table = "<table>" + "".join(f"<tr><td>{term}</td><td>{meaning}</td></tr>" for term, meaning in terms) + "</table>"
    paragraphs = (
        "<h3>Terms</h3><ul>" + "".join(f"<li><p>{term}: {meaning}</p></li>" for term, meaning in terms) + "</ul>"
    )
    assert glossary(dl) == glossary(ul) == glossary(table) == glossary(paragraphs) == []
    assert glossary("<p>The scan has one job: it reads each run. It stops at 12:30. It writes the cursor.</p>") == []
    risk = "<p>Scenario: a scan fails. Who: no one yet. Handling: accepted; the next run retries.</p>"
    assert glossary(risk) == [], "a risk's parts, as Costs and risks asks for them"


def run_standalone(tmp_path: Path, page: bytes, *args: str) -> subprocess.CompletedProcess[str]:
    """As a host without guild runs it: the lone file copied elsewhere, no site-packages, no bridge package."""
    checker = tmp_path / "skill" / "review_doc_check.py"
    checker.parent.mkdir(exist_ok=True)
    shutil.copy(Path(review_doc_check.__file__), checker)
    doc = tmp_path / "doc.html"
    doc.write_bytes(page)
    return subprocess.run(
        [sys.executable, "-I", "-S", str(checker), str(doc), *args],
        cwd=tmp_path, capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip


def test_the_checks_run_as_a_lone_script_find_what_registration_finds(tmp_path: Path) -> None:
    repo, _ = make_worktree(tmp_path)
    git("checkout", "-q", "main", cwd=repo)
    bare = make_doc(WHERE.replace("src/app/run.py", "run.py"), first="Summary")

    done = run_standalone(tmp_path, bare, "--repo", str(repo))

    assert done.returncode == 1, done.stderr
    expected = find_doc_problems(bare, False, "the owner", FILES, review_doc_check.DEFAULT_LIMITS, "main")
    assert len(expected) == 2
    assert done.stdout.splitlines() == [f"- {problem}" for problem in expected]
    done = run_standalone(tmp_path, make_doc(), "--repo", str(repo))
    assert (done.returncode, done.stdout.strip()) == (0, "passes the checks guild review doc runs")


def test_the_lone_script_looks_paths_up_at_the_commit_the_doc_names(tmp_path: Path) -> None:
    repo, second = make_worktree(tmp_path)
    page = make_doc()

    assert run_standalone(tmp_path, page, "--repo", str(repo)).returncode == 1, "tests/test_run.py is not at HEAD"
    named = page.replace(b"<html>", f'<html><meta name="pr-head" content="{second}">'.encode())
    assert run_standalone(tmp_path, named, "--repo", str(repo)).returncode == 0


def test_the_lone_script_without_a_checkout_skips_paths_and_says_so(tmp_path: Path) -> None:
    done = run_standalone(tmp_path, make_doc(WHERE.replace("src/app/run.py", "run.py")), "--repo", str(tmp_path))

    assert done.returncode == 0
    assert "has no commit HEAD, so Where to look paths were not looked up" in done.stderr


def test_the_lone_scripts_limits_are_configs_defaults() -> None:
    assert LIMITS == review_doc_check.DEFAULT_LIMITS
