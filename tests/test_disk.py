import json
import sqlite3
import subprocess
import time
from collections.abc import Generator
from pathlib import Path
from typing import cast

import pytest
from conftest import http_request
from test_capacity import add_session

from bridge import disk
from bridge.config import Config
from bridge.db import connect
from bridge.disk import (
    LOW_FREE_BYTES,
    DiskReading,
    convert_docker_df_to_text,
    map_reading_to_dict,
    measure_disk,
    send_disk_notice,
)

CONFIG = Config(assistant_handle="aide")
GB = 10**9


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[sqlite3.Connection]:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    c = connect(tmp_path / "bridge.db")
    yield c
    c.close()


def reading(free_gb: float, total_gb: int = 500, at: int = 0) -> DiskReading:
    return DiskReading(at, total_gb * GB, int(free_gb * GB), folders=(("~/acme-co/_wt", 21 * GB),), docker="images 6GB")


def notices(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT body FROM messages ORDER BY id")]


def test_the_line_is_40_gb_free_or_over_90_percent_used() -> None:
    assert not reading(40, total_gb=300).low
    assert reading(39.9, total_gb=300).low, "87% used, yet under 40 GB free"
    assert reading(45).low, "45 of 500 GB free is 91% used"
    assert not reading(45, total_gb=440).low
    assert reading(41, total_gb=420).low, "90.2% used trips the line though it shows as 90%"
    assert LOW_FREE_BYTES == 40 * GB


def test_the_assistant_hears_once_per_crossing_and_again_after_space_came_back(conn: sqlite3.Connection) -> None:
    add_session(conn, "aide", "TAB-X", "idle", 0, pid=999)
    assert send_disk_notice(conn, CONFIG, reading(91)) == []
    assert len(send_disk_notice(conn, CONFIG, reading(38, at=1))) == 1
    body = notices(conn)[0]
    assert body.startswith("Disk space on this Mac is low: 38 GB free of 500 GB (92% used")
    assert "~/acme-co/_wt: 21 GB" in body and "Docker: images 6GB" in body
    assert send_disk_notice(conn, CONFIG, reading(30, at=2)) == [], "still low an hour later: no second notice"
    assert send_disk_notice(conn, CONFIG, reading(55, at=3)) == []
    assert len(send_disk_notice(conn, CONFIG, reading(39, at=4))) == 1, "a new crossing after recovery is told"
    assert len(notices(conn)) == 2
    assert conn.execute("SELECT post_type FROM messages").fetchone()[0] != "info", "marked for the assistant's tab"


def test_a_notice_that_reached_nobody_is_tried_again(conn: sqlite3.Connection) -> None:
    assert send_disk_notice(conn, CONFIG, reading(10)) == []
    add_session(conn, "aide", "TAB-X", "idle", 0, pid=999)
    assert len(send_disk_notice(conn, CONFIG, reading(10, at=1))) == 1


def test_folders_and_docker_are_measured_with_timeouts_and_missing_ones_left_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "wt").mkdir()
    (tmp_path / "slow").mkdir()

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["timeout"]
        if args[0] == "docker":
            raise subprocess.TimeoutExpired(args, 10)
        if args[-1].endswith("slow"):
            raise subprocess.TimeoutExpired(args, 300)
        return subprocess.CompletedProcess(args, 1, f"4096\t{args[-1]}\n", "du: unreadable")

    monkeypatch.setattr(disk.subprocess, "run", run)
    folders = (str(tmp_path / "wt"), str(tmp_path / "slow"), str(tmp_path / "absent"))
    measured = measure_disk(DiskReading(0, 500 * GB, 91 * GB), folders)
    assert measured.folders == ((str(tmp_path / "wt"), 4096 * 1024), (str(tmp_path / "slow"), None))
    assert measured.docker is None
    shown = map_reading_to_dict(measured)
    assert shown["free"] == 91 * GB and shown["used_pct"] == 82 and shown["low"] is False
    assert map_reading_to_dict(DiskReading(0, 500 * GB, 91 * GB))["folders"] is None, "not measured yet"


def test_docker_df_names_only_what_holds_space() -> None:
    out = "Images\t6.215GB\nContainers\t282.6kB\nLocal Volumes\t1.291GB\nBuild Cache\t0B\n"
    assert convert_docker_df_to_text(out) == "images 6.215GB, containers 282.6kB, local volumes 1.291GB"
    assert convert_docker_df_to_text("Build Cache\t0B\n") == "nothing stored"


def test_the_guild_page_carries_the_reading_without_waiting_on_du(dashboard: str) -> None:
    def disk_now() -> dict[str, object] | None:
        return json.loads(http_request(dashboard + "/api/guild"))["disk"]

    deadline = time.monotonic() + 5
    while (shown := disk_now()) is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert shown is not None
    assert cast(int, shown["free"]) > 0 and cast(int, shown["total"]) >= cast(int, shown["free"])
    assert shown["docker"] is None, "the test's docker stub fails, so the page says docker did not answer"
