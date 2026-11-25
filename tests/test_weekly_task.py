import argparse
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from test_reviews import CONFIG, add_session

from bridge.asks import WORDS_MAX_CHARS, list_asks
from bridge.cli_guild import cmd_day
from bridge.config import map_toml_to_config
from bridge.db import connect, transaction
from bridge.serve import DashboardState
from bridge.weekly_task import send_weekly_task

SUNDAY_9 = int(datetime.fromisoformat("2026-11-22T09:00:00+00:00").timestamp() * 1000)
DAY = 86_400_000


def setup(tmp_path: Path) -> tuple[Any, Any]:
    conn = connect(tmp_path / "t.db")
    with transaction(conn):
        add_session(conn, "c1", "lead-x")
    task = tmp_path / "task.md"
    task.write_text("Write the dreams analysis for {week}.\n")
    return conn, replace(CONFIG, weekly_task_day=6, weekly_task_to="lead-x", weekly_task_file=str(task))


def sent(conn: Any, config: Any, now: int) -> str | None:
    with transaction(conn):
        weekly = send_weekly_task(conn, config, now)
    return weekly[0] if weekly else None


def bodies(conn: Any) -> list[str]:
    return [r[0] for r in conn.execute("SELECT body FROM messages ORDER BY id")]


def task(week: str, ask_id: int = 1) -> str:
    return (
        f"Write the dreams analysis for {week}.\n\nThis is ask #{ask_id}: `guild ask done {ask_id} --claim '…'` with "
        f"what you made, and another session verifies it; a week with nothing to do is `guild ask drop {ask_id} '…'` "
        "with why."
    )


def test_the_weekly_task_goes_once_on_its_weekday(tmp_path: Path) -> None:
    conn, config = setup(tmp_path)
    assert sent(conn, config, SUNDAY_9 - DAY) is None, "Saturday is not its day"
    assert sent(conn, config, SUNDAY_9) == "weekly task for 2026-w47 sent to lead-x as ask #1"
    assert sent(conn, config, SUNDAY_9 + 3_600_000) is None, "once per week"
    assert sent(conn, config, SUNDAY_9 + DAY) is None, "the Monday catch-up sees it sent"
    assert bodies(conn) == [task("2026-w47")]
    assert sent(conn, config, SUNDAY_9 + 7 * DAY) == "weekly task for 2026-w48 sent to lead-x as ask #2"
    assert bodies(conn)[1] == task("2026-w48", 2)


def test_a_sunday_with_no_morning_report_sends_it_the_next_day_and_never_later(tmp_path: Path) -> None:
    conn, config = setup(tmp_path)
    assert sent(conn, config, SUNDAY_9 + DAY) == "weekly task for 2026-w47 sent to lead-x as ask #1"
    (tmp_path / "b").mkdir()
    other, config = setup(tmp_path / "b")
    assert sent(other, config, SUNDAY_9 + 2 * DAY) is None


def test_before_nine_in_the_work_timezone_it_waits_for_the_morning(tmp_path: Path) -> None:
    conn, config = setup(tmp_path)
    assert sent(conn, config, SUNDAY_9 - 60_000) is None, "08:59 on its day"
    assert sent(conn, replace(config, work_timezone="Asia/Tokyo"), SUNDAY_9 - 9 * 3_600_000) == (
        "weekly task for 2026-w47 sent to lead-x as ask #1"
    ), "09:00 in Tokyo is 00:00 UTC"
    assert bodies(conn) == [task("2026-w47")]


def test_a_monday_catch_up_also_waits_for_nine(tmp_path: Path) -> None:
    conn, config = setup(tmp_path)
    assert sent(conn, config, SUNDAY_9 + DAY - 3_600_000) is None, "Monday 08:00"
    assert sent(conn, config, SUNDAY_9 + DAY) == "weekly task for 2026-w47 sent to lead-x as ask #1"


def test_bridge_serve_sends_it_on_its_day_with_no_guild_day(tmp_path: Path) -> None:
    conn, config = setup(tmp_path)
    conn.close()
    state = DashboardState(config)
    typed: list[int] = []

    def type_into_tab(message_id: int, recipients: object) -> None:
        typed.append(message_id)

    with (
        mock.patch("bridge.serve.db_path", return_value=tmp_path / "t.db"),
        mock.patch("bridge.serve.spawn_typed_delivery", type_into_tab),
    ):
        state.send_weekly_task_once(SUNDAY_9 - 60_000)
        assert typed == []
        state.send_weekly_task_once(SUNDAY_9)
        state.send_weekly_task_once(SUNDAY_9 + 60_000)
    conn = connect(tmp_path / "t.db")
    assert bodies(conn) == [task("2026-w47")]
    assert typed == [r[0] for r in conn.execute("SELECT id FROM messages")]


def test_with_no_weekday_configured_nothing_is_sent(tmp_path: Path) -> None:
    conn, config = setup(tmp_path)
    assert sent(conn, replace(config, weekly_task_day=None), SUNDAY_9) is None
    assert bodies(conn) == []


def test_the_weekday_must_be_0_to_6() -> None:
    assert map_toml_to_config({"weekly_task_day": 6}).weekly_task_day == 6
    for bad in (7, -1, True, "sunday"):
        with pytest.raises(ValueError, match="weekly_task_day"):
            map_toml_to_config({"weekly_task_day": bad})


def test_bridge_serve_says_once_why_a_refused_weekly_task_was_not_sent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conn, config = setup(tmp_path)
    (tmp_path / "task.md").write_text("Write the dreams analysis for {week} on /p/dreams-{week}.\n")
    conn.close()
    state = DashboardState(config)
    logged: list[str] = []

    def log_error(context: str, detail: str) -> None:
        logged.append(detail)

    with (
        mock.patch("bridge.serve.db_path", return_value=tmp_path / "t.db"),
        mock.patch("bridge.serve.spawn_typed_delivery"),
        mock.patch("bridge.serve.log_error", log_error),
    ):
        for minute in range(3):
            state.send_weekly_task_once(SUNDAY_9 + minute * 60_000)
    lines = [line for line in capsys.readouterr().out.splitlines() if "weekly task" in line]
    assert len(lines) == 1, lines
    assert "/p/dreams-2026-w47 opens nothing" in lines[0] and str(tmp_path / "task.md") in lines[0]
    assert len(logged) == 1
    assert bodies(connect(tmp_path / "t.db")) == []


def test_guild_day_prints_why_the_weekly_task_was_not_sent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    conn, config = setup(tmp_path)
    (tmp_path / "task.md").write_text("Write the dreams analysis on /p/dreams-w47.\n")
    with (
        mock.patch("bridge.cli_guild.now_ms", return_value=SUNDAY_9),
        mock.patch("bridge.cli_guild.read_tree", return_value=None),
        mock.patch("bridge.cli_guild.assess_leads", return_value=[]),
    ):
        assert cmd_day(argparse.Namespace(), conn, config) == 0
    assert "/p/dreams-w47 opens nothing" in capsys.readouterr().out
    assert bodies(conn) == []
    assert conn.execute("SELECT 1 FROM signal_posts").fetchone() is None


def test_the_weekly_task_is_an_ask_of_its_lead_so_only_a_verification_or_a_drop_ends_it(tmp_path: Path) -> None:
    conn, config = setup(tmp_path)

    assert sent(conn, config, SUNDAY_9) == "weekly task for 2026-w47 sent to lead-x as ask #1"

    [ask] = list_asks(conn)
    assert (ask.owner, ask.recorded_by, ask.state) == ("lead-x", "bridge", "open")
    assert ask.words == "Write the dreams analysis for 2026-w47." and ask.source == "weekly task 2026-w47"


def test_a_weekly_task_sent_to_an_alias_is_an_ask_of_the_lead_it_reaches(tmp_path: Path) -> None:
    conn, config = setup(tmp_path)
    with transaction(conn):
        conn.execute("INSERT INTO handle_aliases (handle, surface, retired_at) VALUES ('verifier', 's-lead-x', 0)")

    assert sent(conn, replace(config, weekly_task_to="verifier"), SUNDAY_9) is not None

    [ask] = list_asks(conn)
    assert ask.owner == "lead-x", "its lead's ask list shows it and its `guild ask drop` works"


def test_a_refused_weekly_task_leaves_no_ask_behind(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    conn, config = setup(tmp_path)
    with (
        mock.patch("bridge.cli_guild.now_ms", return_value=SUNDAY_9),
        mock.patch("bridge.cli_guild.read_tree", return_value=None),
        mock.patch("bridge.cli_guild.assess_leads", return_value=[]),
    ):
        assert cmd_day(argparse.Namespace(), conn, replace(config, weekly_task_to="nobody")) == 0
        (tmp_path / "task.md").write_text("\n")
        assert cmd_day(argparse.Namespace(), conn, config) == 0
        (tmp_path / "task.md").write_text("x" * (WORDS_MAX_CHARS + 1))
        assert cmd_day(argparse.Namespace(), conn, config) == 0
        (tmp_path / "task.md").write_text("Write the dreams analysis on /p/dreams-w47.\n")
        assert cmd_day(argparse.Namespace(), conn, config) == 0

    out = capsys.readouterr().out
    assert "not sent to nobody" in out and "an ask needs the owner's words" in out
    assert "over 4000 characters" in out, "too long for an ask: refused whole, never stored cut"
    assert "/p/dreams-w47 opens nothing" in out, "the ask was added, then the send was refused"
    assert list_asks(conn, include_closed=True) == [] and bodies(conn) == []
    assert conn.execute("SELECT 1 FROM signal_posts").fetchone() is None
