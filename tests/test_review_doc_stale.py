import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from test_review_worktree import NOW, REVIEWS_CONFIG, TOP, compare_docs, config_for, large
from test_reviews import REF, add_lead, add_session, add_work, make_pr, setup_pr

from bridge.config import Config
from bridge.db import transaction
from bridge.review_github import (
    UNKNOWN_DIFF,
    DocDiff,
    GitHubError,
    count_own_commits_since,
    fetch_doc_diff,
    is_comment_line,
    map_compare_to_doc_diff,
)
from bridge.review_post import DOC_ESCALATE_AFTER_MS, ask_for_doc_updates, tell_assistant_of_stale_docs
from bridge.review_worktree import map_doc_to_served, register_doc
from bridge.reviews import (
    find_last_pusher,
    list_docs_to_compare,
    map_db_to_reviews,
    poll_review_queue,
    store_open_prs,
)

DOC_COMMIT = "d0c" * 13 + "d"
HEAD = "e1e" * 13 + "e"


def compare(*files: dict[str, Any], status: str = "ahead", ahead_by: int = 2) -> dict[str, Any]:
    return {"status": status, "ahead_by": ahead_by, "files": list(files)}


def test_a_compare_of_only_comments_and_docs_leaves_the_doc_current() -> None:
    reworded = {"filename": "app/svc.py", "patch": "@@ -3,2 +3,2 @@\n-    # old why\n+    # new why\n \n+\n"}
    readme = {"filename": "README.md", "patch": "@@ -1 +1 @@\n-x = 1\n+x = 2"}
    jsdoc = {"filename": "web/a.ts", "patch": "@@ -1,3 +1,3 @@\n-/** old */\n+/** new */\n-// a\n+/* b\n+*/"}
    assert map_compare_to_doc_diff(compare(reworded, readme, jsdoc)) == DocDiff(2, 0, True)


def test_a_compare_with_any_code_line_makes_the_doc_stale_and_counts_only_code_lines() -> None:
    mixed = {"filename": "app/svc.py", "patch": "@@ -3,2 +3,3 @@\n-    # old why\n+    # new why\n+    retry()"}
    readme = {"filename": "docs/guide.md", "patch": "@@ -1 +1 @@\n-a\n+b"}
    assert map_compare_to_doc_diff(compare(mixed, readme)) == DocDiff(2, 1, False)


def test_when_unsure_the_doc_is_stale() -> None:
    binary = {"filename": "app/model.bin", "additions": 0, "deletions": 0}
    assert map_compare_to_doc_diff(compare(binary)).cosmetic is False, "no patch to read"
    unknown = {"filename": "app/run.xyz", "patch": "@@ -1 +1 @@\n-# a\n+# b"}
    assert map_compare_to_doc_diff(compare(unknown)) == DocDiff(2, 2, False), "a language it does not know"
    many = [{"filename": f"d{n}.md", "patch": "@@ -1 +1 @@\n-a\n+b"} for n in range(300)]
    assert map_compare_to_doc_diff(compare(*many)) == DocDiff(2, None, False), "GitHub may have cut the file list"
    assert map_compare_to_doc_diff(compare(status="diverged")) == UNKNOWN_DIFF, "rebased or force-pushed since"
    assert not is_comment_line("a.c", "*p = 1;"), "a C dereference is code"
    assert not is_comment_line("a.c", "* p = 1;"), "so is a block comment's middle line, which looks the same"
    assert not is_comment_line("a.ts", "/* why */ run();"), "code after a comment"
    moved = {"filename": "old.md", "previous_filename": "app.py", "status": "renamed", "additions": 0, "deletions": 0}
    assert map_compare_to_doc_diff(compare(moved)).cosmetic is False, "code renamed into a docs file"
    moved_docs = {**moved, "previous_filename": "notes.md"}
    assert map_compare_to_doc_diff(compare(moved_docs)).cosmetic is True
    assert not is_comment_line("a.py", '    """A docstring line."""'), "a line alone cannot tell it from a string"


def register(conn: sqlite3.Connection, tmp_path: Path, commit: str = DOC_COMMIT) -> None:
    source = tmp_path / "doc.html"
    source.write_text(f'<html><head><meta name="pr-head" content="{commit}"></head><body>{TOP}</body>')
    register_doc(conn, REVIEWS_CONFIG, REF, source, NOW)


def stale_pr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    """PR 7 with a doc at DOC_COMMIT, then a push to HEAD that the poll has not compared yet."""
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path / "state"))
    conn = setup_pr(tmp_path, head_oid=DOC_COMMIT, **large())
    with transaction(conn):
        register(conn, tmp_path)
        store_open_prs(conn, [make_pr(head_oid=HEAD, **large())], NOW)
    return conn


def view(conn: sqlite3.Connection, config: Config) -> Any:
    return map_db_to_reviews(conn, config, NOW)["prs"][0]  # type: ignore[index]


def test_a_comment_only_push_keeps_the_doc_current_and_asks_no_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = config_for(tmp_path)
    conn = stale_pr(tmp_path, monkeypatch)
    with transaction(conn):
        add_session(conn, "w", "fixer", branch="dev/x")
        assert [ref for ref, *_ in list_docs_to_compare(conn)] == [REF]
        compare_docs(conn, DocDiff(1, 0, True))
        assert list_docs_to_compare(conn) == [], "compared once per doc and head"
        assert ask_for_doc_updates(conn, config, NOW) == []
    assert (view(conn, config)["doc_stale"], view(conn, config)["doc_behind"]) == (False, None)
    served = map_doc_to_served(conn, "Org/app", 7)
    assert served is not None and b"older than code" not in served


def test_a_push_with_code_goes_stale_and_says_how_far_the_code_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = config_for(tmp_path)
    conn = stale_pr(tmp_path, monkeypatch)
    with transaction(conn):
        compare_docs(conn, DocDiff(3, 41, False))
    words = "doc older than code: 3 commits, 41 lines (doc at d0cd0cd0c, PR at e1ee1ee1e)"
    assert (view(conn, config)["doc_stale"], view(conn, config)["doc_behind"]) == (True, words)
    served = map_doc_to_served(conn, "Org/app", 7)
    assert served is not None and words.encode() + b"</div>" in served
    with transaction(conn):
        compare_docs(conn, UNKNOWN_DIFF)
        conn.execute("UPDATE review_prs SET doc_diff_for = NULL")
        compare_docs(conn, UNKNOWN_DIFF)
    assert view(conn, config)["doc_behind"] == "doc older than code (doc at d0cd0cd0c, PR at e1ee1ee1e)"


def test_the_refresh_ask_goes_to_the_lead_of_the_session_that_pushed_last_not_the_recorded_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quinn's declared work names the PR, but a worker of Alex's made the last push and has ended since."""
    config = config_for(tmp_path)
    conn = stale_pr(tmp_path, monkeypatch)
    with transaction(conn):
        add_session(conn, "quinn-c", "quinn")
        add_lead(conn, "Quinn", "s-quinn")
        add_work(conn, "quinn", REF)
        add_session(conn, "alex-c", "alex")
        add_lead(conn, "Alex", "s-alex")
        add_session(conn, "early", "first-worker", branch="dev/x")
        add_session(conn, "w-c", "swap-worker", branch="dev/x", ended=99, session="tab-gone")
        conn.execute("UPDATE review_branch_writes SET at = 50 WHERE conversation_id = 'w-c'")
        conn.execute(
            "INSERT INTO closed_workers (conversation_id, tab_name, parent_handle, resume_command, closed_at,"
            " parent_lead) VALUES ('w-c', 'swap-worker', 'alex', 'claude --resume w-c', 99, 'alex')"
        )
        compare_docs(conn)
        told = ask_for_doc_updates(conn, config, NOW)
    assert [recipient.handle for _, recipient in told] == ["alex"]
    row = conn.execute("SELECT doc_update_to, doc_update_first_at FROM review_prs").fetchone()
    assert (row[0], row[1]) == ("alex", NOW)
    with transaction(conn):
        conn.execute("UPDATE instances SET ended_at = NULL WHERE conversation_id = 'w-c'")
    pusher = find_last_pusher(conn, config, "Org/app", "dev/x")
    assert pusher is not None and (pusher.handle, pusher.via) == ("swap-worker", "pushed"), "live, it refreshes"


def test_with_no_known_pusher_the_ask_goes_to_the_recorded_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = config_for(tmp_path)
    conn = stale_pr(tmp_path, monkeypatch)
    with transaction(conn):
        add_session(conn, "quinn-c", "quinn")
        add_lead(conn, "Quinn", "s-quinn")
        add_work(conn, "quinn", REF)
        add_session(conn, "other-repo", "elsewhere", branch="dev/x", repo="Org/other")
        compare_docs(conn)
        told = ask_for_doc_updates(conn, config, NOW)
    assert [recipient.handle for _, recipient in told] == ["quinn"]


def test_a_later_push_to_a_same_named_branch_in_another_repo_does_not_hide_the_prs_pusher(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "mine", "fixer", branch="dev/x")
        add_session(conn, "theirs", "elsewhere", branch="dev/x", repo="Org/other")
        conn.execute("UPDATE review_branch_writes SET at = 9 WHERE conversation_id = 'theirs'")
    pusher = find_last_pusher(conn, config, "Org/app", "dev/x")
    assert pusher is not None and pusher.handle == "fixer"


def test_a_doc_still_behind_an_hour_after_the_first_ask_is_told_to_the_assistant_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = config_for(tmp_path)
    conn = stale_pr(tmp_path, monkeypatch)
    with transaction(conn):
        add_session(conn, "w", "fixer", branch="dev/x")
        add_session(conn, "aide-c", config.assistant_handle)
        compare_docs(conn, DocDiff(2, 9, False))
        assert len(ask_for_doc_updates(conn, config, NOW)) == 1
        assert tell_assistant_of_stale_docs(conn, config, NOW + DOC_ESCALATE_AFTER_MS - 60_000) == []
        # Another push and ask in between do not move the hour.
        store_open_prs(conn, [make_pr(head_oid="f" * 40, **large())], NOW)
        compare_docs(conn, DocDiff(3, 12, False))
        assert len(ask_for_doc_updates(conn, config, NOW + 20 * 60_000)) == 1
        told = tell_assistant_of_stale_docs(conn, config, NOW + DOC_ESCALATE_AFTER_MS)
        assert [recipient.handle for _, recipient in told] == [config.assistant_handle]
        assert tell_assistant_of_stale_docs(conn, config, NOW + 5 * DOC_ESCALATE_AFTER_MS) == [], "once"
    body = conn.execute("SELECT body FROM messages ORDER BY id DESC").fetchone()[0]
    assert "https://github.com/Org/app/pull/7" in body and "the last ask went to fixer" in body
    assert "doc older than code: 3 commits, 12 lines" in body
    # A new registration starts over: behind again, it is asked and told about again.
    with transaction(conn):
        register(conn, tmp_path, commit=HEAD)
        compare_docs(conn, DocDiff(1, 2, False))
        assert len(ask_for_doc_updates(conn, config, NOW + 2 * DOC_ESCALATE_AFTER_MS)) == 1
        assert len(tell_assistant_of_stale_docs(conn, config, NOW + 3 * DOC_ESCALATE_AFTER_MS)) == 1


def test_no_notice_before_the_poll_compared_the_new_head_and_a_doc_current_again_starts_a_new_hour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = config_for(tmp_path)
    conn = stale_pr(tmp_path, monkeypatch)
    later = NOW + 2 * DOC_ESCALATE_AFTER_MS
    with transaction(conn):
        add_session(conn, "w", "fixer", branch="dev/x")
        add_session(conn, "aide-c", config.assistant_handle)
        compare_docs(conn)
        assert len(ask_for_doc_updates(conn, config, NOW)) == 1
        # A push that undoes the change: stored by the poll, not compared yet.
        store_open_prs(conn, [make_pr(head_oid="f" * 40, **large())], NOW)
        assert tell_assistant_of_stale_docs(conn, config, later) == []
        compare_docs(conn, DocDiff(2, 0, True))
        assert tell_assistant_of_stale_docs(conn, config, later) == []
        assert conn.execute("SELECT doc_update_first_at FROM review_prs").fetchone()[0] is None
        store_open_prs(conn, [make_pr(head_oid="a" * 40, **large())], NOW)
        compare_docs(conn)
        assert len(ask_for_doc_updates(conn, config, later)) == 1
        assert tell_assistant_of_stale_docs(conn, config, later + 60_000) == [], "a new hour from this ask"
        assert len(tell_assistant_of_stale_docs(conn, config, later + DOC_ESCALATE_AFTER_MS)) == 1


def test_a_current_doc_is_never_told_to_the_assistant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_for(tmp_path)
    conn = stale_pr(tmp_path, monkeypatch)
    with transaction(conn):
        add_session(conn, "w", "fixer", branch="dev/x")
        add_session(conn, "aide-c", config.assistant_handle)
        compare_docs(conn)
        assert len(ask_for_doc_updates(conn, config, NOW)) == 1
        register(conn, tmp_path, commit=HEAD)
        conn.execute("UPDATE review_prs SET doc_update_first_at = ?", (NOW,))
        assert tell_assistant_of_stale_docs(conn, config, NOW + 2 * DOC_ESCALATE_AFTER_MS) == []


def test_github_compares_the_docs_commit_with_the_head() -> None:
    answer = json.dumps(compare({"filename": "a.py", "patch": "@@ -1 +1 @@\n-# a\n+# b"}, ahead_by=1))
    with mock.patch("bridge.review_github.run_gh", return_value=answer) as gh:
        assert fetch_doc_diff("Org/app", DOC_COMMIT, HEAD) == DocDiff(1, 0, True)
    assert gh.call_args.args[0] == ["api", f"repos/Org/app/compare/{DOC_COMMIT}...{HEAD}"]


def test_the_poll_compares_each_doc_and_head_once_and_a_failed_compare_reads_as_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = config_for(tmp_path)
    conn = stale_pr(tmp_path, monkeypatch)
    prs = [make_pr(head_oid=HEAD, **large())]
    with (
        mock.patch("bridge.reviews.fetch_open_prs", return_value=prs),
        mock.patch("bridge.reviews.fetch_doc_diff", side_effect=GitHubError("gh failed: HTTP 404")) as fetched,
    ):
        poll_review_queue(conn, config, NOW, None, None)
        poll_review_queue(conn, config, NOW, None, None)
    assert fetched.call_args_list == [mock.call("Org/app", DOC_COMMIT, HEAD, 7)]
    assert view(conn, config)["doc_behind"] == "doc older than code (doc at d0cd0cd0c, PR at e1ee1ee1e)"


def test_a_merge_of_the_base_counts_nothing_against_the_doc() -> None:
    own = {"filename": "app/svc.py", "patch": "@@ -3,2 +3,3 @@\n+    retry()"}
    from_base = {"filename": "other/big.py", "patch": "@@ -1 +1,3 @@\n+a = 1\n+b = 2\n+c = 3"}
    diff = map_compare_to_doc_diff(compare(own, from_base, ahead_by=13), {"app/svc.py"}, 1)
    assert diff == DocDiff(1, 1, False), "the base's file and commits are not the PR's change"
    assert map_compare_to_doc_diff(compare(from_base, ahead_by=12), {"app/svc.py"}, 0) == DocDiff(0, 0, True)
    commits: list[dict[str, object]] = [
        {"sha": "d0c", "parents": [{}]},
        {"sha": "a11", "parents": [{}]},
        {"sha": "m1", "parents": [{}, {}]},
        {"sha": "b22", "parents": [{}]},
    ]
    assert count_own_commits_since(commits, "d0c") == 2, "merges left out"
    assert count_own_commits_since(commits, "zzz") is None
