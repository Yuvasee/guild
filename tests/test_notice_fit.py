from pathlib import Path

import pytest
from test_reviews import add_session

from bridge.board import BODY_MAX_CHARS, fit_body
from bridge.db import connect, transaction
from bridge.outage import LOOP_FAILING_TELL_AFTER_MS, FailureSpell
from bridge.reviews import send_to_handle

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
    assert spell.recover(67 * MIN) == 67
    assert spell.recover(68 * MIN) is None
    assert spell.fail(70 * MIN) is None, "a new spell starts its own ten minutes"


def test_a_spell_nobody_heard_of_is_tried_again_and_ends_without_a_back_line() -> None:
    spell = FailureSpell()
    spell.fail(0)
    assert spell.fail(LOOP_FAILING_TELL_AFTER_MS) == 10
    assert spell.fail(LOOP_FAILING_TELL_AFTER_MS + MIN) == 11, "the notice did not go out: try again next round"
    assert spell.recover(20 * MIN) is None
