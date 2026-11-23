import sqlite3
import subprocess
from pathlib import Path

import pytest
from test_review_worktree import NOW, REVIEWS_CONFIG
from test_reviews import REF, setup_pr

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


def make_doc(where: str = WHERE, first: str = "In short", last: str = "") -> bytes:
    sections = ["What changes and why", "How it works", "Costs and risks"]
    body = f"<h2>{first}</h2><p>text</p>{STORY}" + "".join(f"<h2>{title}</h2><p>text</p>" for title in sections)
    tail = f"<h2>Tests</h2><h2>Reviews</h2><p>Codex, Claude</p><h2>Verified and not verified</h2>{PASSED}"
    return f"<html><body>{body}{where}{tail}{last}</body></html>".encode()


DEFAULTS = Config()
LIMITS = PlainLimits(DEFAULTS.doc_in_short_max_bullets, DEFAULTS.doc_bullet_max_words, DEFAULTS.doc_sentence_max_words)


def problems(page: bytes, reviewed: bool = False, files: frozenset[str] | None = None) -> list[str]:
    return find_doc_problems(page, reviewed, "Sam", files, LIMITS)


FILES = frozenset({"src", "src/app", "src/app/run.py", "src/app/store.py", "tests", "tests/test_run.py"})
SINCE = "<h2>Changes since the last review</h2><details class='item'><summary>update</summary></details>"


def test_a_doc_in_the_specs_order_with_full_paths_passes() -> None:
    assert problems(make_doc(), files=FILES) == []
    assert problems(make_doc(last=SINCE), reviewed=True, files=FILES) == []


def test_the_doc_opens_with_in_short() -> None:
    assert problems(make_doc(first="Why")) == [
        "its first h2 is 'Why'; it opens with 'In short', with nothing before it"
    ]
    assert problems(b"<h1>Doc</h1><p>no sections</p>")[0] == "it has no h2 headings; it opens with the h2 'In short'"
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
    story = STORY.encode() + f"<h2>Verified</h2>{PASSED}".encode()
    assert len(problems(b"<h2>Why<h2>In short</h2>" + story)) == 2, "a heading left open ends at the next one"
    assert len(problems(b"<h2>In short</h2>" + story + b"<h2>Changes since the last review")) == 1, "or at the end"
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
    rest = "<h2>How it works</h2><p>" + "word " * 60 + "ends here.</p>"
    return (
        f"<html><body><h1>Title</h1><p>{'lead ' * 40}</p><h2>In short</h2>{short}{story}"
        f"<h2>What changes and why</h2>{changes}{rest}<h2>Verified and not verified</h2>{verified}</body></html>"
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
        "<details class='item'><summary>D1. Short summary</summary><p>" + words(60) + "</p></details>"
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
        b"<h2>In short" + STORY.encode() + b"<h2>What changes and why</h2><p>" + words(31).encode() + b"</p>"
    ) + f"<h2>Verified</h2>{PASSED}".encode()
    assert len(problems(unclosed)) == 1, "a heading left open ends at the next one"
    abbreviations = "<p>Seen in docs, e.g. Alpha and Beta, vs. Gamma, and " + words(22) + "</p>"
    assert len(problems(make_top("<p>Fine.</p>", abbreviations))) == 1, "e.g. and vs. end no sentence"


def test_the_limits_come_from_config() -> None:
    page = make_top(bullets(8, 8, 8), "<p>" + words(12) + "</p>")
    assert problems(page) == []
    strict = find_doc_problems(page, False, "Sam", None, PlainLimits(bullets=2, bullet_words=7, sentence_words=10))
    assert [line.split(";")[0].split(",")[0] for line in strict] == [
        "In short has 3 bullets",
        "an In short bullet has 8 words",
        "an In short bullet has 8 words",
        "an In short bullet has 8 words",
        "a sentence above the folds has 12 words",
    ]


def test_a_doc_whose_first_reader_did_not_pass_is_refused() -> None:
    assert problems(make_top("<p>x.</p>")) == []
    (refused,) = problems(make_top("<p>x.</p>", verified="<p>Unit tests pass. A reader read it.</p>"))
    assert refused.startswith("Verified does not say the first reader (with the reader profile) passed")
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
