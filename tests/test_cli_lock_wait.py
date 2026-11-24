import sqlite3
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
from conftest import BridgeEnv, SpawnProcess

from bridge import cli, db


@contextmanager
def _lock_held_after_first_try(path: Path, held_s: float, monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """Held from the start, and let go held_s after the command first asks for it, however long it took to get there."""
    asked = threading.Event()
    begin = db._begin_immediate  # pyright: ignore[reportPrivateUsage]

    def asking(conn: sqlite3.Connection, retry_busy_timeout_ms: int) -> bool:
        asked.set()
        return begin(conn, retry_busy_timeout_ms)

    monkeypatch.setattr(db, "_begin_immediate", asking)
    holder = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")

    def release() -> None:
        asked.wait(30)
        time.sleep(held_s)
        holder.execute("ROLLBACK")

    releaser = threading.Thread(target=release)
    releaser.start()
    try:
        yield
    finally:
        asked.set()
        releaser.join()
        holder.close()


def _registered(bridge_env: BridgeEnv, spawn_process: SpawnProcess, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in bridge_env.env.items():
        monkeypatch.setenv(key, value)
    pid = spawn_process()
    bridge_env.write_session_file(pid, "conv-lock-wait")
    monkeypatch.setenv("CLAUDE_PID", str(pid))
    assert cli.main(["focus", "before the lock"]) == 0


def test_a_typed_command_outlasts_a_lock_held_past_its_old_wait(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registered(bridge_env, spawn_process, monkeypatch)
    held_s = db.CLI_BUSY_TIMEOUT_MS / 1000 + 0.5
    assert held_s * 1000 < db.CLI_LOCK_WAIT_MS

    with _lock_held_after_first_try(bridge_env.db, held_s, monkeypatch):
        started = time.monotonic()
        code = cli.main(["focus", "after the lock"])
        took_s = time.monotonic() - started

    assert code == 0
    assert took_s >= held_s
    conn = bridge_env.connect()
    assert conn.execute("SELECT focus FROM conversations").fetchone()[0] == "after the lock"
    conn.close()


def test_a_lock_held_past_the_wait_ends_the_command_with_one_line(
    bridge_env: BridgeEnv,
    spawn_process: SpawnProcess,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _registered(bridge_env, spawn_process, monkeypatch)
    capsys.readouterr()
    monkeypatch.setattr(db, "CLI_LOCK_WAIT_MS", 1000)

    with _lock_held_after_first_try(bridge_env.db, 2.5, monkeypatch):
        code = cli.main(["focus", "never written"])

    assert code == 1
    out = capsys.readouterr()
    assert out.err == "bridge: busy, its database stayed locked for 1 s; try again\n"
    assert out.out == ""
    assert "bridge cmd_focus\nthe database stayed locked for 1 s" in (bridge_env.state_dir / "errors.log").read_text()
    conn = bridge_env.connect()
    assert conn.execute("SELECT focus FROM conversations").fetchone()[0] == "before the lock"
    conn.close()


def test_an_error_naming_a_locked_word_is_no_lock() -> None:
    conn = sqlite3.connect(":memory:")
    with pytest.raises(sqlite3.OperationalError) as raised:
        conn.execute("SELECT * FROM blocked_notices")
    conn.close()
    assert not db.is_lock_error(raised.value)
