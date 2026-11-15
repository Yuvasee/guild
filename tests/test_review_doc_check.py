import sqlite3
import subprocess
from pathlib import Path

import pytest
from test_review_worktree import NOW, REVIEWS_CONFIG
from test_reviews import REF, setup_pr

from bridge.db import transaction
from bridge.review_doc_check import find_doc_problems
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


def make_doc(where: str = WHERE, first: str = "In short", last: str = "") -> bytes:
    sections = [first, "What changes and why", "How it works", "Costs and risks"]
    body = "".join(f"<h2>{title}</h2><p>text</p>" for title in sections)
    tail = "<h2>Tests</h2><h2>Reviews</h2><p>Codex, Claude</p><h2>Verified and not verified</h2>"
    return f"<html><body>{body}{where}{tail}{last}</body></html>".encode()


def problems(page: bytes, reviewed: bool = False, files: frozenset[str] | None = None) -> list[str]:
    return find_doc_problems(page, reviewed, "Sam", files)


FILES = frozenset({"src", "src/app", "src/app/run.py", "src/app/store.py", "tests", "tests/test_run.py"})
SINCE = "<h2>Changes since the last review</h2><details class='item'><summary>update</summary></details>"


def test_a_doc_in_the_specs_order_with_full_paths_passes() -> None:
    assert problems(make_doc(), files=FILES) == []
    assert problems(make_doc(last=SINCE), reviewed=True, files=FILES) == []


def test_the_doc_opens_with_in_short() -> None:
    assert problems(make_doc(first="Why")) == [
        "its first h2 is 'Why'; it opens with 'In short', with nothing before it"
    ]
    assert problems(b"<h1>Doc</h1><p>no sections</p>") == ["it has no h2 headings; it opens with the h2 'In short'"]
    hidden = b"<script>document.write('<h2>Top</h2>')</script>" + make_doc()
    assert problems(hidden) == [], "a heading inside a script is not the doc's"


def test_changes_since_the_last_review_waits_for_the_owners_review_and_comes_last() -> None:
    (refused,) = problems(make_doc(last=SINCE))
    assert "Sam has not reviewed this PR yet" in refused
    early = make_doc(first="In short</h2><h2>Changes since the last review")
    assert problems(early, reviewed=True) == [
        "'What changes and why' comes after 'Changes since the last review', which is always the last h2"
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
    assert len(problems(b"<h2>Why<h2>In short</h2>")) == 1, "a heading left open ends at the next one"
    assert len(problems(b"<h2>In short</h2><h2>Changes since the last review")) == 1, "or at the end of the file"
    unclosed = make_doc(WHERE.replace("<code>tests/test_run.py</code>", "<code>tests/missing.py"))
    assert len(problems(unclosed, files=FILES)) == 1, "a code left open ends with its row"
    nested = "<ul><li><code>src/app</code><ul><li>Check retries.</li></ul> then <code>retry()</code>.</li></ul>"
    assert problems(make_doc(WHERE + nested), files=FILES) == [], "the outer row already has its path"
    example = "<textarea><h2>Changes since the last review</h2></textarea><h2>Changes since startup</h2>"
    assert problems(make_doc(last=example)) == [], "a textarea's text and an app's own changes are not that section"
    assert problems(b"<h2>In short</h2><![bogus]>")[0].startswith("its HTML cannot be read past line 1")


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
