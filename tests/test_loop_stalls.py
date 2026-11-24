import inspect
import io
import sqlite3
import threading
import time
from pathlib import Path
from unittest import mock

import pytest
from test_reviews import add_session, setup_pr
from test_stall_guard import FAILURES, GUARDED

from bridge.db import connect, transaction
from bridge.errlog import StampedLines
from bridge.outage import LoopStalls
from bridge.serve import LOOP_LOOK_EVERY_MS, LOOP_STALL_AFTER_MIN, DashboardState, _format_loop_stack  # pyright: ignore[reportPrivateUsage]

MIN = 60_000
START = 1_000 * MIN


class Stop(Exception):
    pass


def test_every_serve_line_starts_with_its_utc_time_a_print_and_a_traceback_alike() -> None:
    out = io.StringIO()
    stream = StampedLines(out, clock=lambda: 90_061.0)
    print("transcript scan failed: x", file=stream)
    stream.write("Traceback (most recent call last):\n  File 'a.py'\nValueError")
    stream.write(": bad\n")
    stamp = time.strftime("%Y-%m-%dT", time.gmtime(0))
    assert out.getvalue().splitlines() == [
        f"{stamp[:8]}02T01:01:01Z transcript scan failed: x",
        f"{stamp[:8]}02T01:01:01Z Traceback (most recent call last):",
        f"{stamp[:8]}02T01:01:01Z   File 'a.py'",
        f"{stamp[:8]}02T01:01:01Z ValueError: bad",
    ]


def test_a_loop_past_its_limit_is_told_once_and_once_more_when_it_finishes_again() -> None:
    loops = LoopStalls(START, limits={"scan": 10 * MIN})
    loops.finished["scan"] = START + MIN
    assert loops.take(START + 11 * MIN) == ([], [])
    assert loops.take(START + 11 * MIN + 1) == ([("scan", START + MIN)], [])
    loops.finished["scan"] = START + 12 * MIN  # finished while the notice was on its way: that is its recovery
    loops.mark_told("scan", START + MIN)
    assert loops.take(START + 12 * MIN) == ([], [("scan", 11)])
    loops.finished["scan"] = START + 70 * MIN
    assert loops.take(START + 70 * MIN) == ([], [("scan", 11)]), "from the first round back, however late it is told"
    loops.mark_back("scan")
    assert loops.take(START + 80 * MIN) == ([], []), "told once"
    assert loops.take(START + 80 * MIN + 1) == ([("scan", START + 70 * MIN)], []), "a new stall is a new notice"


def test_a_loop_back_and_stuck_again_before_its_recovery_was_told_is_a_new_stall() -> None:
    loops = LoopStalls(START, limits={"scan": 10 * MIN})
    assert loops.take(START + 11 * MIN) == ([("scan", None)], [])
    loops.mark_told("scan", None)
    loops.finished["scan"] = START + 12 * MIN
    assert loops.take(START + 12 * MIN) == ([], [("scan", 12)]), "its recovery notice, which then fails"
    assert loops.take(START + 50 * MIN) == ([("scan", START + 12 * MIN)], [])
    loops.mark_told("scan", START + 12 * MIN)
    assert loops.take(START + 51 * MIN) == ([], []), "told once"


def test_a_loop_that_never_finished_counts_from_the_start_and_a_sleeping_mac_from_its_wake() -> None:
    loops = LoopStalls(START, limits={"scan": 10 * MIN})
    assert loops.take(START + 10 * MIN + 1) == ([("scan", None)], [])
    assert loops.take(START + 10 * MIN + 1, awake_since=START + 5 * MIN) == ([], [])


def test_every_serve_loop_has_a_stall_limit_and_rests_under_its_own_name() -> None:
    loops = [name for name, _ in inspect.getmembers(DashboardState, inspect.isfunction) if name.endswith("_forever")]
    assert sorted(loops) == sorted(LOOP_STALL_AFTER_MIN)
    for name in loops:
        assert f'self._rest("{name}"' in inspect.getsource(getattr(DashboardState, name)), name


def told_to(tmp_path: Path, handle: str) -> list[str]:
    conn = connect(tmp_path / "t.db")
    return [
        str(r[0])
        for r in conn.execute(
            "SELECT m.body FROM messages m JOIN message_recipients r ON r.message_id = m.id WHERE r.handle = ?",
            (handle,),
        )
    ]


def test_replay_a_hung_transcript_scan_is_told_by_another_loop_with_its_stack(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The scan thread stops inside a round and never logs a thing; the loops beside it keep resting."""
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "aide-c", GUARDED.assistant_handle)
        add_session(conn, "ops-c", FAILURES)
    conn.close()
    now = {"ms": START}
    release = threading.Event()

    def hang() -> None:
        release.wait(10)

    scan = threading.Thread(target=hang, name="Thread-9 (scan_transcripts_forever)", daemon=True)
    scan.start()
    with (
        mock.patch("bridge.serve.db_path", return_value=tmp_path / "t.db"),
        mock.patch("bridge.serve.now_ms", side_effect=lambda: now["ms"]),
        mock.patch("bridge.serve.spawn_typed_delivery"),
        mock.patch("bridge.serve.time.sleep"),
    ):
        state = DashboardState(GUARDED)
        state._wake.counted_from = lambda: 0  # pyright: ignore[reportPrivateUsage]
        loops = state._loops  # pyright: ignore[reportPrivateUsage]
        loops.started_at = START
        loops.limits.update(scan_transcripts_forever=10 * MIN, read_tree_forever=10 * MIN)
        rest = state._rest  # pyright: ignore[reportPrivateUsage]
        now["ms"] = START + 10 * MIN
        rest("read_tree_forever", 10)
        assert told_to(tmp_path, FAILURES) == []
        now["ms"] = START + 11 * MIN
        rest("read_tree_forever", 10)
        now["ms"] = START + 11 * MIN + LOOP_LOOK_EVERY_MS
        rest("read_tree_forever", 10)
        log = capsys.readouterr().out
        assert told_to(tmp_path, FAILURES) == [
            "bridge: serve's loop scan_transcripts_forever has not finished a round for 11 min (last: none since "
            "serve started), so what it does waits. serve.log has its stack as it was then. bridge says once "
            "when it finishes again."
        ], "once, to whoever hears bridge's failures only"
        assert told_to(tmp_path, GUARDED.assistant_handle) == []
        assert log.count("has not finished a round") == 1
        assert "Thread-9 (scan_transcripts_forever) is at:" in log and "in hang" in log
        now["ms"] = START + 13 * MIN
        rest("scan_transcripts_forever", 30)
    assert told_to(tmp_path, FAILURES)[-1] == (
        "bridge: serve's loop scan_transcripts_forever finishes rounds again, after 13 min without one."
    )
    release.set()
    scan.join(5)


def test_a_stall_nobody_could_be_told_of_logs_its_stack_again_when_it_comes_back(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    setup_pr(tmp_path).close()
    now = {"ms": START + 11 * MIN}
    with (
        mock.patch("bridge.serve.db_path", return_value=tmp_path / "t.db"),
        mock.patch("bridge.serve.now_ms", side_effect=lambda: now["ms"]),
        mock.patch("bridge.serve.time.sleep"),
    ):
        state = DashboardState(GUARDED)
        state._wake.counted_from = lambda: 0  # pyright: ignore[reportPrivateUsage]
        loops = state._loops  # pyright: ignore[reportPrivateUsage]
        loops.started_at = START
        loops.limits.update(scan_transcripts_forever=10 * MIN)
        state._rest("read_tree_forever", 10)  # pyright: ignore[reportPrivateUsage]
        now["ms"] += LOOP_LOOK_EVERY_MS
        state._rest("read_tree_forever", 10)  # pyright: ignore[reportPrivateUsage]
        loops.finished["scan_transcripts_forever"] = now["ms"]
        now["ms"] += 11 * MIN
        state._rest("read_tree_forever", 10)  # pyright: ignore[reportPrivateUsage]
    log = capsys.readouterr().out
    assert log.count("no thread runs scan_transcripts_forever") == 2, "once per stall, each with its own stack"


def test_a_wall_clock_set_back_does_not_hold_the_next_look(tmp_path: Path) -> None:
    setup_pr(tmp_path).close()
    now = {"ms": START + 60 * MIN}
    with (
        mock.patch("bridge.serve.db_path", return_value=tmp_path / "t.db"),
        mock.patch("bridge.serve.now_ms", side_effect=lambda: now["ms"]),
        mock.patch("bridge.serve.time.sleep"),
        mock.patch.object(LoopStalls, "take", return_value=([], [])) as took,
    ):
        state = DashboardState(GUARDED)
        state._rest("read_tree_forever", 10)  # pyright: ignore[reportPrivateUsage]
        now["ms"] = START
        state._rest("read_tree_forever", 10)  # pyright: ignore[reportPrivateUsage]
    assert took.call_count == 2


def test_a_loop_whose_thread_ended_is_named_as_ended() -> None:
    assert _format_loop_stack("nag_asks_forever") == "no thread runs nag_asks_forever: it ended"


def test_a_scan_that_raises_anything_goes_on_and_counts_as_no_finished_round(tmp_path: Path) -> None:
    """Before, an error other than a database or file one ended the scan thread until restart."""
    rests: list[bool] = []
    with (
        mock.patch("bridge.serve.db_path", return_value=tmp_path / "t.db"),
        mock.patch("bridge.serve.scan_transcripts", side_effect=[ValueError("odd entry"), 0]),
        mock.patch("bridge.serve.log_error") as logged,
        mock.patch("bridge.serve.time.sleep", side_effect=[None, Stop()]),
        pytest.raises(Stop),
    ):
        state = DashboardState(GUARDED)
        original = state._rest  # pyright: ignore[reportPrivateUsage]

        def rest(loop: str, seconds: float, finished: bool = True) -> None:
            rests.append(finished)
            original(loop, seconds, finished)

        state._rest = rest  # pyright: ignore[reportPrivateUsage]
        state.scan_transcripts_forever()
    assert rests == [False, True]
    assert logged.call_args[0][0] == "transcript scan"


def test_a_failing_look_at_the_loops_never_ends_the_loop_resting(tmp_path: Path) -> None:
    with (
        mock.patch("bridge.serve.db_path", return_value=tmp_path / "t.db"),
        mock.patch.object(DashboardState, "_tell_of_stalled_loops", side_effect=sqlite3.OperationalError("locked")),
        mock.patch("bridge.serve.log_error") as logged,
        mock.patch("bridge.serve.time.sleep") as slept,
    ):
        DashboardState(GUARDED)._rest("read_tree_forever", 10)  # pyright: ignore[reportPrivateUsage]
    assert slept.called and logged.call_args[0][0] == "loop stall watch"
