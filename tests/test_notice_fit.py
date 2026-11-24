from pathlib import Path
from unittest import mock

import pytest
from test_reviews import CONFIG, add_session, comment, make_pr, setup_pr, store

from bridge.board import (
    BODY_MAX_CHARS,
    SYSTEM_CONVERSATION,
    USER_ITEM_MAX_CHARS,
    MessageStateError,
    Party,
    fit_body,
    resolve_handle,
    send_message,
)
from bridge.db import connect, transaction
from bridge.feed import add_post
from bridge.outage import LOOP_FAILING_TELL_AFTER_MS, FailureSpell
from bridge.reviews import poll_review_queue, send_to_handle

MIN = 60_000


def test_a_notice_within_the_limit_is_left_as_it_is() -> None:
    text = "x" * BODY_MAX_CHARS
    assert fit_body(text) == text


def test_a_notice_over_the_limit_is_cut_to_it_and_the_cut_is_logged(capsys: pytest.CaptureFixture[str]) -> None:
    fitted = fit_body("New: " + "y" * BODY_MAX_CHARS)
    assert len(fitted) == BODY_MAX_CHARS and fitted.endswith("…")
    assert f"notice cut from {BODY_MAX_CHARS + 5} to {BODY_MAX_CHARS} chars: 'New: " in capsys.readouterr().out


def test_a_review_notice_a_few_chars_too_long_is_sent_cut_where_it_rolled_back_the_poll(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.db")
    with transaction(conn):
        add_session(conn, "c2", "lead-x", surface="s-lead")
        send_to_handle(conn, "lead-x", "z" * (BODY_MAX_CHARS + 9), 1000)
    body = conn.execute("SELECT body FROM messages").fetchone()["body"]
    assert len(body) == BODY_MAX_CHARS


def test_a_failing_loop_is_told_once_after_ten_minutes_and_once_when_it_works_again() -> None:
    spell = FailureSpell()
    assert spell.fail(0) is None
    assert spell.fail(LOOP_FAILING_TELL_AFTER_MS - 1) is None, "a lock or a GitHub hiccup clears in a few minutes"
    assert spell.fail(LOOP_FAILING_TELL_AFTER_MS) == 10
    spell.told = True
    assert spell.fail(30 * MIN) is None, "told once per spell"
    assert spell.recovered(67 * MIN) == 67
    assert spell.recovered(68 * MIN) == 68, "a back line that did not go out is tried again"
    spell.reset()
    assert spell.recovered(69 * MIN) is None
    assert spell.fail(70 * MIN) is None, "a new spell starts its own ten minutes"


def test_a_spell_nobody_heard_of_is_tried_again_and_ends_without_a_back_line() -> None:
    spell = FailureSpell()
    spell.fail(0)
    assert spell.fail(LOOP_FAILING_TELL_AFTER_MS) == 10
    assert spell.fail(LOOP_FAILING_TELL_AFTER_MS + MIN) == 11, "the notice did not go out: try again next round"
    assert spell.recovered(20 * MIN) is None, "nobody heard of it, so no back line"


def test_a_5000_char_review_notice_goes_out_cut_and_the_poll_round_commits(tmp_path: Path) -> None:
    title = "t" * 5000
    conn = setup_pr(tmp_path, title=title)
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
    store(conn, comment())
    with (
        mock.patch("bridge.reviews.fetch_open_prs", return_value=[make_pr(title=title)]),
        mock.patch("bridge.reviews.fetch_comments", return_value=[]),
        mock.patch("bridge.reviews.add_label"),
    ):
        poll_review_queue(conn, CONFIG, 200_000, None, 200_000)
    bodies = [str(r["body"]) for r in conn.execute("SELECT body FROM messages")]
    assert bodies and all(len(b) <= BODY_MAX_CHARS for b in bodies)
    assert any(b.startswith("New on https://github.com/Org/app/pull/7 (ttt") for b in bodies)
    assert conn.execute("SELECT verdict FROM review_comments").fetchone()["verdict"] is not None


def test_any_notice_of_bridges_own_is_cut_where_another_sender_is_refused(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.db")
    with transaction(conn):
        add_session(conn, "c2", "lead-x", surface="s-lead")
        recipient = resolve_handle(conn, "lead-x")
        send_message(conn, Party(SYSTEM_CONVERSATION, "bridge"), [recipient], "info", "n" * 5000, 1000)
        add_post(conn, Party(SYSTEM_CONVERSATION, "bridge"), "insight", "T" * 500, "b" * 5000, [], [], 1000)
        with pytest.raises(MessageStateError, match="keep it under"):
            send_message(conn, Party("c2", "lead-x"), [recipient], "info", "n" * 5000, 1000)
    assert len(conn.execute("SELECT body FROM messages").fetchone()["body"]) == BODY_MAX_CHARS
    item = conn.execute("SELECT text, body FROM user_items").fetchone()
    assert (len(item["text"]), len(item["body"])) == (USER_ITEM_MAX_CHARS, BODY_MAX_CHARS)
