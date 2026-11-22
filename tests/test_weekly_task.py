from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from test_reviews import CONFIG, add_session

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


def test_the_weekly_task_goes_once_on_its_weekday(tmp_path: Path) -> None:
    conn, config = setup(tmp_path)
    assert sent(conn, config, SUNDAY_9 - DAY) is None, "Saturday is not its day"
    assert sent(conn, config, SUNDAY_9) == "weekly task for 2026-w47 sent to lead-x"
    assert sent(conn, config, SUNDAY_9 + 3_600_000) is None, "once per week"
    assert sent(conn, config, SUNDAY_9 + DAY) is None, "the Monday catch-up sees it sent"
    assert bodies(conn) == ["Write the dreams analysis for 2026-w47."]
    assert sent(conn, config, SUNDAY_9 + 7 * DAY) == "weekly task for 2026-w48 sent to lead-x"


def test_a_sunday_with_no_morning_report_sends_it_the_next_day_and_never_later(tmp_path: Path) -> None:
    conn, config = setup(tmp_path)
    assert sent(conn, config, SUNDAY_9 + DAY) == "weekly task for 2026-w47 sent to lead-x"
    (tmp_path / "b").mkdir()
    other, config = setup(tmp_path / "b")
    assert sent(other, config, SUNDAY_9 + 2 * DAY) is None


def test_before_nine_in_the_work_timezone_it_waits_for_the_morning(tmp_path: Path) -> None:
    conn, config = setup(tmp_path)
    assert sent(conn, config, SUNDAY_9 - 60_000) is None, "08:59 on its day"
    assert sent(conn, replace(config, work_timezone="Asia/Tokyo"), SUNDAY_9 - 9 * 3_600_000) == (
        "weekly task for 2026-w47 sent to lead-x"
    ), "09:00 in Tokyo is 00:00 UTC"
    assert bodies(conn) == ["Write the dreams analysis for 2026-w47."]


def test_a_monday_catch_up_also_waits_for_nine(tmp_path: Path) -> None:
    conn, config = setup(tmp_path)
    assert sent(conn, config, SUNDAY_9 + DAY - 3_600_000) is None, "Monday 08:00"
    assert sent(conn, config, SUNDAY_9 + DAY) == "weekly task for 2026-w47 sent to lead-x"


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
    assert bodies(conn) == ["Write the dreams analysis for 2026-w47."]
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
