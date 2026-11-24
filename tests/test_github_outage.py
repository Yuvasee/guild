import sqlite3
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from test_reviews import add_session, setup_pr
from test_stall_guard import FAILURES, GUARDED

from bridge.db import connect, transaction
from bridge.outage import LOOP_FAILING_TELL_AFTER_MS, FailureSpell
from bridge.reviews import record_poll_error
from bridge.serve import DashboardState
from bridge.wake import WakeWatch

MIN = 60_000
GITHUB_DOWN = "gh failed: HTTP 502: Server Error"


class Stop(Exception):
    pass


def run_rounds(tmp_path: Path, rounds: list[str], sleeps: dict[int, int] | None = None) -> list[str]:
    """Each round at the next minute: 'github' fails its GitHub read as poll_review_queue does, 'bridge' raises in
    bridge, 'ok' reads GitHub, 'ok-bridge' reads GitHub and then raises in bridge. sleeps: the Mac slept this many
    minutes before the round of that index, which moves the wall clock and not the monotonic one. Returns the bodies
    told to the assistant."""
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "aide-c", GUARDED.assistant_handle)
        add_session(conn, "ops-c", FAILURES)
    conn.close()
    slept = sleeps or {}
    starts = [(i + sum(m for at, m in slept.items() if at <= i)) * MIN for i in range(len(rounds))]
    clock = iter(starts)
    now = {"ms": 0}
    awake_ms = {"ms": 0}
    outcomes = iter(rounds)

    def tick() -> int:
        return now["ms"]

    def poll(conn: sqlite3.Connection, *_: Any) -> list[object]:
        before = now["ms"]
        now["ms"] = next(clock)
        awake_ms["ms"] += min(now["ms"] - before, MIN)
        outcome = next(outcomes)
        if outcome == "bridge":
            raise sqlite3.OperationalError("database is locked")
        with transaction(conn):
            if outcome == "github":
                record_poll_error(conn, GITHUB_DOWN, now["ms"])
            else:
                conn.execute("UPDATE review_poll SET ok_at = ?, error = NULL, error_at = NULL", (now["ms"],))
        if outcome == "ok-bridge":
            raise sqlite3.OperationalError("database is locked")
        return []

    with (
        mock.patch("bridge.serve.db_path", return_value=tmp_path / "t.db"),
        mock.patch("bridge.serve.now_ms", side_effect=tick),
        mock.patch("bridge.serve.poll_review_queue", side_effect=poll),
        mock.patch.object(DashboardState, "_advance_reviews"),
        mock.patch("bridge.serve.spawn_typed_delivery"),
        mock.patch("bridge.serve.time.sleep", side_effect=[*[None] * (len(rounds) - 1), Stop()]),
        pytest.raises(Stop),
    ):
        state = DashboardState(GUARDED)
        state._wake = WakeWatch(wall=lambda: now["ms"] / 1000, monotonic=lambda: awake_ms["ms"] / 1000)  # pyright: ignore[reportPrivateUsage]
        state.poll_reviews_forever()
    conn = connect(tmp_path / "t.db")
    return [
        str(r[0])
        for r in conn.execute(
            """
            SELECT m.body FROM messages m JOIN message_recipients r ON r.message_id = m.id
            WHERE r.handle = ? ORDER BY m.id
            """,
            (GUARDED.assistant_handle,),
        )
    ]


def test_github_failing_ten_minutes_is_told_once_as_github_with_its_error_and_once_when_it_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    failing = LOOP_FAILING_TELL_AFTER_MS // MIN
    told = run_rounds(tmp_path, ["ok", *["github"] * (failing + 3), "ok", "ok"])
    assert len(told) == 2, told
    assert told[0].startswith(f"GitHub is failing: the review poll has not read it for {failing} min")
    assert f"Last error: {GITHUB_DOWN}." in told[0]
    assert told[1] == f"GitHub answers the review poll again after {failing + 3} min."
    assert not any("review-queue poll" in body for body in told), "not a bridge failure"


def test_bridge_failing_still_counts_as_a_bridge_failure_and_never_as_github(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    failing = LOOP_FAILING_TELL_AFTER_MS // MIN
    told = run_rounds(tmp_path, ["bridge"] * (failing + 2) + ["ok"])
    assert len(told) == 2, told
    assert told[0].startswith(f"bridge: the review-queue poll has failed for {failing} min")
    assert "database is locked" in told[0]
    assert told[1] == f"bridge: the review-queue poll works again after {failing + 2} min."


def test_a_short_github_hiccup_is_told_to_no_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    assert run_rounds(tmp_path, ["github"] * 5 + ["ok"]) == []


def test_a_good_github_read_in_a_round_bridge_then_failed_ends_the_github_spell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two short outages with a good read between them are not one long one, though that round raised later."""
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    told = run_rounds(tmp_path, ["github"] * 6 + ["ok-bridge"] + ["github"] * 6)
    assert not any(body.startswith("GitHub") for body in told), told


def test_a_mac_asleep_through_failed_reads_tells_no_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A closed lid: a read fails as the network goes, the dark wakes every ~15 min fail, and so does the first read
    after waking, before the network is back."""
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    rounds = ["ok", "github", "github", "github", "github", "github", "ok"]
    assert run_rounds(tmp_path, rounds, sleeps={2: 15, 3: 15, 4: 15, 5: 15}) == []


def test_github_failing_ten_awake_minutes_after_a_sleep_is_told_counted_from_the_wake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    failing = LOOP_FAILING_TELL_AFTER_MS // MIN
    told = run_rounds(tmp_path, ["ok", *["github"] * (failing + 4)], sleeps={3: 60})
    assert len(told) == 1, told
    assert told[0].startswith(f"GitHub is failing: the review poll has not read it for {failing} min")


def test_a_wall_clock_set_back_does_not_put_off_the_notice() -> None:
    spell = FailureSpell()
    spell.fail(100 * MIN)
    assert spell.fail(70 * MIN) is None
    assert spell.fail(80 * MIN) == LOOP_FAILING_TELL_AFTER_MS // MIN, "counted from the clock's new now, not 100 min"
