import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest
from test_reviews import CONFIG, REF, add_session, comment, setup_pr, store

from bridge.db import transaction
from bridge.review_post import WAITING_SENTENCE, advance_posts, record_publish
from bridge.review_worktree import REVIEW_LARGE_LOC
from bridge.reviews import HIS, SETTLE_MS
from bridge.serve import DashboardState
from bridge.stall_guard import (
    COMMENT_STALL_AFTER_MS,
    DOC_STALL_AFTER_MS,
    OWNER_STALL_AFTER_MS,
    POST_STALL_AFTER_MS,
    READY_STALL_AFTER_MS,
    tell_of_stalls,
)

FAILURES = "ops-watch"
GUARDED = replace(CONFIG, auto_merge_failures_to=FAILURES)
UPDATED_MS = int(datetime(2026, 11, 2, 10, tzinfo=UTC).timestamp() * 1000)
"""make_pr's updated_at: a doc is wanted only for a PR fresh at the time the guard looks."""


def guarded(tmp_path: Path, **changes: object) -> sqlite3.Connection:
    conn = setup_pr(tmp_path, **changes)
    with transaction(conn):
        add_session(conn, "aide-c", GUARDED.assistant_handle)
        add_session(conn, "ops-c", FAILURES)
    return conn


def alert(conn: sqlite3.Connection, now: int) -> list[str]:
    with transaction(conn):
        return [recipient.handle for _, recipient in tell_of_stalls(conn, GUARDED, now)]


def last_body(conn: sqlite3.Connection) -> str:
    return str(conn.execute("SELECT body FROM messages ORDER BY id DESC").fetchone()[0])


def keys(conn: sqlite3.Connection) -> list[str]:
    return [str(r[0]) for r in conn.execute("SELECT key FROM signal_posts ORDER BY key")]


def test_a_comment_with_no_verdict_alerts_once_after_its_settle_and_ten_minutes(tmp_path: Path) -> None:
    conn = guarded(tmp_path)
    store(conn, comment("p1", at=1000))
    due = 1000 + SETTLE_MS + COMMENT_STALL_AFTER_MS
    assert alert(conn, due - 1) == []
    assert alert(conn, due) == [GUARDED.assistant_handle, FAILURES]
    assert alert(conn, due + 10 * COMMENT_STALL_AFTER_MS) == [], "once"
    body = last_body(conn)
    assert body.startswith("https://github.com/Org/app/pull/7 (T): a comment has waited")
    assert "1 comment(s) have no verdict" in body and "serve.log" in body
    # Routed: the state moved on, so a later stall is a new one.
    with transaction(conn):
        conn.execute("UPDATE review_comments SET verdict = ?, told_at = ?", (HIS, due))
    assert alert(conn, due + 1) == [] and keys(conn) == []
    store(conn, comment("p2", at=due + 2))
    later = due + 2 + SETTLE_MS + COMMENT_STALL_AFTER_MS
    assert alert(conn, later - 1) == []
    assert len(alert(conn, later)) == 2


def test_the_owners_comment_not_told_ten_minutes_after_it_alerts_and_names_the_missing_owner(tmp_path: Path) -> None:
    conn = guarded(tmp_path)
    store(conn, comment("p1", at=1000))
    with transaction(conn):
        conn.execute("UPDATE review_comments SET verdict = ?", (HIS,))
    due = 1000 + COMMENT_STALL_AFTER_MS
    assert alert(conn, due - 1) == []
    assert len(alert(conn, due)) == 2
    assert alert(conn, due + 1) == []
    body = last_body(conn)
    assert "comment(s) wait for an owner" in body and f"bridge claim {REF}" in body


def test_ready_to_merge_for_thirty_minutes_alerts_once_and_again_after_it_left_and_came_back(tmp_path: Path) -> None:
    conn = guarded(tmp_path)
    with transaction(conn):
        conn.execute("UPDATE review_prs SET merge_path = 'ready'")
    assert alert(conn, 1000) == [], "first seen"
    assert alert(conn, 1000 + READY_STALL_AFTER_MS - 1) == []
    assert len(alert(conn, 1000 + READY_STALL_AFTER_MS)) == 2
    assert alert(conn, 1000 + 5 * READY_STALL_AFTER_MS) == []
    body = last_body(conn)
    assert "ready to merge for 30 min, and it is still open" in body and "no owner was told" in body
    with transaction(conn):
        conn.execute("UPDATE review_prs SET merge_path = NULL")
    assert alert(conn, 2 * READY_STALL_AFTER_MS) == [] and keys(conn) == []
    with transaction(conn):
        conn.execute("UPDATE review_prs SET merge_path = 'ready', merge_told_at = ?", (UPDATED_MS,))
    back = 3 * READY_STALL_AFTER_MS
    assert alert(conn, back) == []
    assert alert(conn, back + READY_STALL_AFTER_MS - 1) == []
    assert len(alert(conn, back + READY_STALL_AFTER_MS)) == 2
    assert "its owner was told at 11-02 10:00" in last_body(conn)
    assert "gh pr merge https://github.com/Org/app/pull/7" in last_body(conn)


def test_a_doc_asked_an_hour_ago_and_not_registered_alerts_once_while_the_pr_still_wants_one(tmp_path: Path) -> None:
    conn = guarded(tmp_path, loc=REVIEW_LARGE_LOC)
    with transaction(conn):
        conn.execute("UPDATE review_prs SET doc_asked_at = ?", (UPDATED_MS,))
    due = UPDATED_MS + DOC_STALL_AFTER_MS
    assert alert(conn, due - 1) == []
    assert len(alert(conn, due)) == 2
    assert alert(conn, due + 1) == []
    body = last_body(conn)
    assert "bridge asked for its review doc at 11-02 10:00, 60 min ago" in body
    assert "guild review doc Org/app#7 /path/file.html" in body
    with transaction(conn):
        conn.execute("UPDATE review_prs SET doc_file = '/d.html', doc_at = ?", (due,))
    assert alert(conn, due + 2) == [] and keys(conn) == []
    with transaction(conn):
        conn.execute("UPDATE review_prs SET doc_file = NULL, doc_at = NULL, draft = 1")
    assert alert(conn, due + 3) == [], "a draft wants no doc"


def test_an_owner_known_only_from_the_last_mention_alerts_after_fifteen_minutes(tmp_path: Path) -> None:
    conn = guarded(tmp_path)
    with transaction(conn):
        add_session(conn, "mention-c", "passerby")
        conn.execute("INSERT INTO refs VALUES ('mention-c', ?, 'prompt', 0, 9, 0, 1, NULL, NULL)", (REF,))
    assert alert(conn, 1000) == []
    assert alert(conn, 1000 + OWNER_STALL_AFTER_MS - 1) == []
    assert len(alert(conn, 1000 + OWNER_STALL_AFTER_MS)) == 2
    assert alert(conn, 1000 + 2 * OWNER_STALL_AFTER_MS) == []
    body = last_body(conn)
    assert "its owner has been only a guess for 15 min" in body
    assert "its notices go to passerby only because that live session mentioned it last" in body
    # The real owner writes to the branch: confirmed, so the state ends.
    with transaction(conn):
        add_session(conn, "owner-c", "builder", branch="dev/x")
    assert alert(conn, 1000 + 3 * OWNER_STALL_AFTER_MS) == [] and keys(conn) == []


def test_each_recipient_is_told_once_and_one_unreachable_is_tried_again(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "ops-c", FAILURES)
        conn.execute("UPDATE review_prs SET merge_path = 'ready'")
    assert alert(conn, 0) == []
    assert alert(conn, READY_STALL_AFTER_MS) == [FAILURES]
    assert alert(conn, READY_STALL_AFTER_MS + 1) == []
    with transaction(conn):
        add_session(conn, "aide-c", GUARDED.assistant_handle)
    assert alert(conn, READY_STALL_AFTER_MS + 2) == [GUARDED.assistant_handle]
    assert alert(conn, READY_STALL_AFTER_MS + 3) == []


def test_a_long_error_and_a_dead_page_link_still_leave_one_sendable_message_with_the_unblock(tmp_path: Path) -> None:
    conn = guarded(tmp_path, title="Fix the /p/gone-page link")
    with transaction(conn):
        conn.execute("UPDATE review_prs SET merge_path = 'ready', auto_merge_error = ?", ("x" * 3000,))
    alert(conn, 0)
    assert len(alert(conn, READY_STALL_AFTER_MS)) == 2
    body = last_body(conn)
    assert "(page gone-page, no local file)" in body and "To unblock: " in body and body.endswith("this state.")


def test_resetting_one_state_keeps_other_states_and_other_signals(tmp_path: Path) -> None:
    conn = guarded(tmp_path, loc=REVIEW_LARGE_LOC)
    with transaction(conn):
        conn.execute("UPDATE review_prs SET merge_path = 'ready', doc_asked_at = ?", (UPDATED_MS,))
        for key in (f"bot-wait:{REF}:owner", "permission:c1:abc:seen", "watcher:w1"):
            conn.execute("INSERT INTO signal_posts (key, message_id, posted_at) VALUES (?, 0, 1)", (key,))
    alert(conn, UPDATED_MS)
    assert len(alert(conn, UPDATED_MS + DOC_STALL_AFTER_MS)) == 4
    with transaction(conn):
        conn.execute("UPDATE review_prs SET merge_path = NULL")
    alert(conn, UPDATED_MS + DOC_STALL_AFTER_MS + 1)
    assert keys(conn) == sorted(
        [
            f"bot-wait:{REF}:owner",
            "permission:c1:abc:seen",
            f"stall:doc:{REF}",
            f"stall:doc:{REF}>to:{FAILURES}",
            "watcher:w1",
        ]
    )


def test_with_no_one_to_tell_nothing_is_recorded_and_the_next_round_tries_again(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        conn.execute("UPDATE review_prs SET merge_path = 'ready'")
    assert alert(conn, 0) == []
    assert alert(conn, READY_STALL_AFTER_MS) == []
    with transaction(conn):
        add_session(conn, "aide-c", GUARDED.assistant_handle)
    assert alert(conn, READY_STALL_AFTER_MS + 1) == [GUARDED.assistant_handle]


def test_replay_a_poll_that_rolled_back_every_round_leaves_comments_without_a_verdict(tmp_path: Path) -> None:
    """Comments were stored, but the round's last step, which classifies them, rolled back on a notice over the
    message limit: the poll's GitHub read still counted as good."""
    conn = guarded(tmp_path)
    with transaction(conn):
        add_session(conn, "owner-c", "builder", branch="dev/x")
    store(conn, comment("p1", at=1000), comment("p2", at=2000, author="colleague"))
    due = 1000 + SETTLE_MS + COMMENT_STALL_AFTER_MS
    with transaction(conn):
        conn.execute("UPDATE review_poll SET ok_at = ?", (due,))
    assert len(alert(conn, due)) == 2
    body = last_body(conn)
    assert "2 comment(s) have no verdict" in body and "no round has classified it" in body


def test_github_failing_is_named_beside_why_comments_wait_not_as_its_cause(tmp_path: Path) -> None:
    """A round that cannot read GitHub still classifies the comments it already has."""
    conn = guarded(tmp_path)
    store(conn, comment("p1", at=1000))
    due = 1000 + SETTLE_MS + COMMENT_STALL_AFTER_MS
    with transaction(conn):
        conn.execute("UPDATE review_poll SET ok_at = 900, error = 'rate limited', error_at = ?", (due,))
    assert len(alert(conn, due)) == 2
    body = last_body(conn)
    assert "no round has classified it" in body
    assert "GitHub is failing too: the review poll's last read of it failed at 01-01 00:10: rate limited" in body


def test_replay_a_pr_listed_under_a_lead_that_only_mentioned_it(tmp_path: Path) -> None:
    """Another lead's session pasted the PR's link; nothing tied it to its real owner, so its notices went there."""
    conn = guarded(tmp_path, head="feature/export")
    with transaction(conn):
        add_session(conn, "other-lead-c", "other-lead")
        conn.execute("INSERT INTO refs VALUES ('other-lead-c', ?, 'pr-link', 0, 9, 0, 1, NULL, NULL)", (REF,))
    assert alert(conn, 0) == []
    assert len(alert(conn, OWNER_STALL_AFTER_MS)) == 2
    assert "its notices go to other-lead only because" in last_body(conn)
    assert f"bridge claim {REF}" in last_body(conn)


def test_replay_a_post_waiting_for_a_description_asked_of_the_wrong_owner(tmp_path: Path) -> None:
    """Publish asked the guessed owner for the description; it never came, so the post sat in waiting-sentence."""
    conn = guarded(tmp_path)
    with transaction(conn):
        add_session(conn, "other-lead-c", "other-lead")
        conn.execute("INSERT INTO refs VALUES ('other-lead-c', ?, 'prompt', 0, 9, 0, 1, NULL, NULL)", (REF,))
        record_publish(conn, REF, "owner-x", 1000)
        advance_posts(conn, GUARDED, 1000)
    assert conn.execute("SELECT post_state FROM review_prs").fetchone()[0] == WAITING_SENTENCE
    assert "go-ahead" in last_body(conn), "the description was asked of the guessed owner"
    assert alert(conn, 1000) == []
    told = alert(conn, 1000 + POST_STALL_AFTER_MS)
    assert len(told) == 4, "the post and the guessed owner, each to both"
    bodies = [str(r[0]) for r in conn.execute("SELECT body FROM messages ORDER BY id DESC LIMIT 4")]
    assert any("still not out (waiting-sentence)" in b and "bridge asked its owner" in b for b in bodies)
    assert any("its notices go to other-lead only because" in b for b in bodies)
    post, owner = f"post-stall:{REF}:{WAITING_SENTENCE}", f"stall:owner:{REF}"
    assert keys(conn) == sorted([post, f"{post}>to:{FAILURES}", owner, f"{owner}>to:{FAILURES}", f"{owner}:seen"])


def test_a_closed_pr_is_never_a_stall_and_its_keys_are_forgotten(tmp_path: Path) -> None:
    conn = guarded(tmp_path)
    with transaction(conn):
        conn.execute("UPDATE review_prs SET merge_path = 'ready'")
    alert(conn, 0)
    assert len(alert(conn, READY_STALL_AFTER_MS)) == 2
    with transaction(conn):
        conn.execute("UPDATE review_prs SET closed_at = 1")
    assert alert(conn, 2 * READY_STALL_AFTER_MS) == [] and keys(conn) == []


def test_a_failed_review_round_still_runs_the_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The round raised before _advance_reviews, so only the guard's own run can tell of a stall."""
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    conn = guarded(tmp_path)
    with transaction(conn):
        conn.execute("UPDATE review_prs SET merge_path = 'ready'")
        conn.execute(
            "INSERT INTO signal_posts (key, message_id, posted_at) VALUES (?, 0, 0)", (f"stall:ready:{REF}:seen",)
        )
    conn.close()

    class Stop(Exception):
        pass

    state = DashboardState(GUARDED)
    with (
        mock.patch("bridge.serve.db_path", return_value=tmp_path / "t.db"),
        mock.patch("bridge.serve.poll_review_queue", side_effect=sqlite3.OperationalError("body too long")),
        mock.patch("bridge.serve.spawn_typed_delivery") as delivered,
        mock.patch("bridge.serve.time.sleep", side_effect=Stop),
        pytest.raises(Stop),
    ):
        state.poll_reviews_forever()
    assert delivered.call_count == 2


def test_a_comment_without_a_verdict_names_when_the_transcript_scan_last_finished(tmp_path: Path) -> None:
    """A scan that hangs leaves every later comment without a verdict while the poll itself works."""
    conn = guarded(tmp_path)
    store(conn, comment("p1", at=1000))
    due = 1000 + SETTLE_MS + COMMENT_STALL_AFTER_MS
    with transaction(conn):
        told = tell_of_stalls(conn, GUARDED, due, scan_finished_at=600_000)
    assert told
    assert "the transcript scan last finished at 01-01 00:10" in last_body(conn)


def test_no_transcript_scan_since_serve_started_is_named_as_such(tmp_path: Path) -> None:
    conn = guarded(tmp_path)
    store(conn, comment("p1", at=1000))
    with transaction(conn):
        assert tell_of_stalls(conn, GUARDED, 1000 + SETTLE_MS + COMMENT_STALL_AFTER_MS, scan_finished_at=0)
    assert "no transcript scan has finished since serve started" in last_body(conn)
