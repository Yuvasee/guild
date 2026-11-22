import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from bridge.asks import send_bodies_from_bridge
from bridge.board import AddressError, MessageStateError, Recipient
from bridge.config import Config
from bridge.review_post import WORK_HOURS

CATCH_UP_DAYS = 1
"""This many days after the task's weekday it is still sent, once, when that day had none."""


class WeeklyTaskRefused(Exception):
    pass


def send_weekly_task(
    conn: sqlite3.Connection, config: Config, now: int
) -> tuple[str, list[tuple[int, Recipient]]] | None:
    """The week's task to its lead, once per week, from its weekday's morning or the next day's; a line saying what
    was sent and the messages, or None. A text bridge refuses, or an unknown lead, raises WeeklyTaskRefused with
    the reason and nothing is sent."""
    if config.weekly_task_day is None or not config.weekly_task_to or not config.weekly_task_file:
        return None
    zone = ZoneInfo(config.work_timezone) if config.work_timezone else None
    local = datetime.fromtimestamp(now / 1000, zone)
    # Not at midnight from bridge serve: from the start of the working day.
    if local.hour < WORK_HOURS[0]:
        return None
    today = local.date()
    due = today - timedelta(days=(today.weekday() - config.weekly_task_day) % 7)
    if (today - due).days > CATCH_UP_DAYS:
        return None
    key = f"weekly-task:{due.isoformat()}"
    if conn.execute("SELECT 1 FROM signal_posts WHERE key = ?", (key,)).fetchone():
        return None
    year, week, _ = due.isocalendar()
    text = Path(config.weekly_task_file).expanduser().read_text().replace("{week}", f"{year}-w{week:02d}").strip()
    try:
        bodies = send_bodies_from_bridge(conn, config, config.weekly_task_to, text, now, refused_raises=True)
    except (AddressError, MessageStateError) as exc:
        raise WeeklyTaskRefused(
            f"weekly task for {year}-w{week:02d} from {config.weekly_task_file} not sent to "
            f"{config.weekly_task_to}: {exc}"
        ) from exc
    sent = [(message_id, recipient) for _, message_id, recipient in bodies]
    if not sent:
        return None
    conn.execute("INSERT INTO signal_posts (key, message_id, posted_at) VALUES (?, ?, ?)", (key, sent[0][0], now))
    return f"weekly task for {year}-w{week:02d} sent to {config.weekly_task_to}", sent
