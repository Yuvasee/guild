import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

from bridge.board import flag_overdue
from bridge.db import HOOK_BUSY_TIMEOUT_MS, MIGRATE_BUSY_TIMEOUT_MS, MIGRATIONS, connect
from bridge.reviews import record_branch_writes


def _hold_and_connect(path: Path, wal: bool) -> tuple[sqlite3.Connection, float]:
    holder = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    if wal:
        holder.execute("PRAGMA journal_mode = WAL")
    holder.execute("BEGIN IMMEDIATE")
    held_s = 1.0
    assert held_s * 1000 > HOOK_BUSY_TIMEOUT_MS and held_s * 1000 < MIGRATE_BUSY_TIMEOUT_MS
    release = threading.Timer(held_s, lambda: holder.execute("COMMIT"))
    release.start()
    started = time.monotonic()
    try:
        conn = connect(path, HOOK_BUSY_TIMEOUT_MS)
    finally:
        release.join()
        holder.close()
    return conn, time.monotonic() - started


def test_a_hook_waits_for_a_migration_another_process_holds(tmp_path: Path) -> None:
    """As right after a deploy: serve holds the write lock to migrate while a hook connects with its 200 ms wait."""
    conn, waited = _hold_and_connect(tmp_path / "bridge.db", wal=True)

    assert waited >= 0.9
    assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == len(MIGRATIONS)
    assert int(conn.execute("PRAGMA busy_timeout").fetchone()[0]) == HOOK_BUSY_TIMEOUT_MS, "its own wait is back"
    conn.close()


def test_a_new_database_waits_for_the_lock_to_switch_to_wal(tmp_path: Path) -> None:
    conn, waited = _hold_and_connect(tmp_path / "bridge.db", wal=False)

    assert waited >= 0.9
    assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]) == "wal"
    conn.close()


def _map_queries_to_scans(conn: sqlite3.Connection, run: Callable[[], object]) -> list[str]:
    queries: list[str] = []
    conn.set_trace_callback(queries.append)
    try:
        run()
    finally:
        conn.set_trace_callback(None)
    plans = [
        str(row[3])
        for query in queries
        if query.lstrip().upper().startswith("SELECT")
        for row in conn.execute(f"EXPLAIN QUERY PLAN {query}")
    ]
    return [step for step in plans if step.startswith("SCAN ")]


def test_serve_lock_holders_look_rows_up_by_index(tmp_path: Path) -> None:
    """These run under serve's write lock every pass; a full scan there grows with the message and instance tables
    and every hook waits it out."""
    conn = connect(tmp_path / "bridge.db")
    conn.row_factory = sqlite3.Row

    scans = _map_queries_to_scans(conn, lambda: flag_overdue(conn, 1))
    scans += _map_queries_to_scans(conn, lambda: record_branch_writes(conn, "c1", [("git push", 1)]))

    assert scans == []
