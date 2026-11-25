import sqlite3
from pathlib import Path
from unittest import mock

import pytest
from test_reviews import CONFIG, REF, comment, make_pr, setup_pr, store

from bridge.db import transaction
from bridge.review_github import FetchedComment, map_iso_to_ms
from bridge.reviews import (
    COMMENTS_LIST_LAG_MS,
    SETTLE_MS,
    UNCLASSIFIED_LOG_AFTER_MS,
    poll_review_queue,
    store_comments,
)

CHANGED = "2026-11-02T10:15:11Z"
CHANGED_MS = map_iso_to_ms(CHANGED) or 0
DAY_MS = 86_400_000


def read(conn: sqlite3.Connection, updated_at: str, *comments: FetchedComment) -> None:
    with transaction(conn):
        store_comments(conn, CONFIG, REF, updated_at, list(comments), CHANGED_MS + DAY_MS)


def test_a_comment_github_lists_late_is_stored_by_the_next_round_and_a_settled_pr_is_not_read_again(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = setup_pr(tmp_path, updated_at=CHANGED)
    late = comment("i1", at=CHANGED_MS, author="bot")
    with (
        mock.patch("bridge.reviews.fetch_open_prs", return_value=[make_pr(updated_at=CHANGED)]),
        mock.patch("bridge.reviews.fetch_comments", side_effect=[[], [late]]) as fetch,
        mock.patch("bridge.reviews.add_label"),
    ):
        poll_review_queue(conn, CONFIG, CHANGED_MS + 10_000, None, None)
        assert conn.execute("SELECT comments_read_for FROM review_prs").fetchone()[0] is None
        poll_review_queue(conn, CONFIG, CHANGED_MS + COMMENTS_LIST_LAG_MS + 10_000, None, None)
        poll_review_queue(conn, CONFIG, CHANGED_MS + 2 * COMMENTS_LIST_LAG_MS, None, None)
    assert fetch.call_count == 2
    assert [r[0] for r in conn.execute("SELECT comment_id FROM review_comments")] == ["i1"]
    assert conn.execute("SELECT comments_read_for FROM review_prs").fetchone()[0] == CHANGED
    assert "was not in the read" not in capsys.readouterr().out, "a read not yet counted missed nothing"


def test_a_comment_the_read_for_a_later_change_missed_is_logged_when_a_later_read_stores_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = setup_pr(tmp_path)
    read(conn, CHANGED)
    assert capsys.readouterr().out == "", "the first read of a PR has nothing to compare with"
    read(
        conn,
        "2026-11-02T12:18:38Z",
        comment("i1", at=CHANGED_MS, author="bot"),
        comment("p2", at=CHANGED_MS - 60_000, author="colleague", review="r3"),
        comment("r3", at=CHANGED_MS + 5_000, author="colleague", state="COMMENTED", body="See inline"),
    )
    out = capsys.readouterr().out
    assert out == (
        f"review comments of {REF}: i1 of 11-02 10:15:11 UTC was not in the read for the PR's change at "
        "11-02 10:15:11 UTC; stored only now, by the read for 2026-11-02T12:18:38Z\n"
    ), "the inline comment was drafted before its review came out, and the review came after that read"
    read(conn, "2026-11-02T12:20:00Z", comment("i1", at=CHANGED_MS, author="bot"))
    assert capsys.readouterr().out == "", "one already stored is not new"


def poll(conn: sqlite3.Connection, now: int, scanned_from: int | None) -> None:
    with (
        mock.patch("bridge.reviews.fetch_open_prs", return_value=[make_pr()]),
        mock.patch("bridge.reviews.fetch_comments", return_value=[]),
        mock.patch("bridge.reviews.add_label"),
    ):
        poll_review_queue(conn, CONFIG, now, None, scanned_from)


def test_a_round_logs_comments_still_without_a_verdict_three_minutes_past_their_settle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = setup_pr(tmp_path)
    store(conn, comment("p1", at=1000))
    due = 1000 + SETTLE_MS + UNCLASSIFIED_LOG_AFTER_MS
    poll(conn, due - 1, None)
    assert "review poll:" not in capsys.readouterr().out
    poll(conn, due + 60_000, None)
    assert (
        "review poll: 1 comment(s) have no verdict past their settle; the oldest, p1 of pr:Org/app#7, for 4 min; "
        "no transcript scan has finished since serve started\n"
    ) in capsys.readouterr().out
    poll(conn, due + 60_000, 500)
    assert "this round classified against the scan begun at 01-01 00:00:00 UTC" in capsys.readouterr().out
    poll(conn, due + 60_000, due)
    assert conn.execute("SELECT verdict FROM review_comments").fetchone()[0] is not None
    assert "review poll:" not in capsys.readouterr().out
