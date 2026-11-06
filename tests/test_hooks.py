import json
import os
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from conftest import VENV_BIN, BridgeEnv, SpawnProcess, wait_for

from bridge import db
from bridge.config import load_config
from bridge.hooks import handle_hook
from bridge.procinfo import process_start_ms
from bridge.start_context import (
    PendingStart,
    finish_taken_start,
    has_pending_start,
    put_back_taken_start,
    take_pending_start,
    write_pending_start,
)


def test_turn_lifecycle_updates_registry(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pid = spawn_process()
    repo = _github_repo(bridge_env.root / "repo", "dev/abc-42-thing")
    bridge_env.write_session_file(pid, "conv-1")
    agterm = {"AGTERM_PANE_ID": "PANE-1", "AGTERM_WORKSPACE_ID": "WS-1"}
    base = {"session_id": "conv-1", "cwd": str(repo), "transcript_path": "/t/conv-1.jsonl"}

    started = bridge_env.hook("SessionStart", {**base, "source": "startup"}, pid, agterm)
    prompt = bridge_env.hook("UserPromptSubmit", {**base, "prompt": "look at ABC-7 and PR #31"}, pid, agterm)
    with bridge_env.connect() as conn:
        mid_turn = conn.execute("SELECT status FROM instances").fetchone()["status"]
    stop = bridge_env.hook("Stop", {**base, "last_assistant_message": "Done; opened acme/web#1900."}, pid, agterm)

    assert [r.returncode for r in (started, prompt, stop)] == [0, 0, 0]
    assert (started.stdout, prompt.stdout, stop.stdout) == ("", "", "")
    assert mid_turn == "active"
    with bridge_env.connect() as conn:
        instance = conn.execute("SELECT * FROM instances").fetchone()
        conversation = conn.execute("SELECT * FROM conversations").fetchone()
        refs = {(r["ref"], r["source"]) for r in conn.execute("SELECT ref, source FROM refs")}
        surface = conn.execute("SELECT * FROM surfaces").fetchone()
    assert instance["instance_id"] == f"{pid}@{process_start_ms(pid)}"
    assert (instance["status"], instance["branch"], instance["repo"]) == (
        "idle",
        "dev/abc-42-thing",
        "acme/web",
    )
    assert (instance["surface"], instance["kind"], instance["peer_name"]) == (
        "PANE-1",
        "interactive",
        f"peer-{pid}",
    )
    assert (conversation["start_source"], conversation["turns"]) == ("startup", 1)
    assert conversation["last_prompt"] == "look at ABC-7 and PR #31"
    assert conversation["last_reply"] == "Done; opened acme/web#1900."
    assert refs == {
        ("issue:ABC-7", "prompt"),
        ("pr:acme/web#31", "prompt"),
        ("issue:ABC-42", "branch"),
        ("pr:acme/web#1900", "reply"),
    }
    assert (surface["surface"], surface["agterm_workspace"]) == ("PANE-1", "WS-1")
    assert not (bridge_env.state_dir / "errors.log").exists()


def test_focus_nudge_after_min_turns_then_throttled(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pid = spawn_process()
    payload = {"session_id": "conv-n", "cwd": str(bridge_env.root), "prompt": "hi"}

    outputs = [bridge_env.hook("UserPromptSubmit", payload, pid).stdout for _ in range(3)]

    assert outputs[0] == ""
    context = json.loads(outputs[1])["hookSpecificOutput"]
    assert context["hookEventName"] == "UserPromptSubmit"
    assert "focus line is not set" in context["additionalContext"]
    assert outputs[2] == ""
    assert bridge_env.cli("focus", "bridge phase 1", pid=pid).returncode == 0
    with bridge_env.connect() as conn:
        assert conn.execute("SELECT focus FROM conversations").fetchone()["focus"] == "bridge phase 1"


def test_out_of_order_event_does_not_regress_status(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pid = spawn_process()
    config = load_config()
    env = {"CLAUDE_PID": str(pid)}
    payload: dict[str, object] = {"session_id": "conv-o", "cwd": str(bridge_env.root)}
    now = time.time_ns()
    with bridge_env.connect() as conn:
        handle_hook(conn, "UserPromptSubmit", {**payload, "prompt": "x"}, env, config, now)
        handle_hook(conn, "Stop", payload, env, config, now - 1_000_000)
        status = conn.execute("SELECT status FROM instances").fetchone()["status"]
    assert status == "active"


def test_session_end_on_clear_keeps_instance_live(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pid = spawn_process()
    cwd = str(bridge_env.root)
    bridge_env.hook("SessionStart", {"session_id": "conv-a", "cwd": cwd, "source": "startup"}, pid)
    bridge_env.hook("SessionEnd", {"session_id": "conv-a", "cwd": cwd, "reason": "clear"}, pid)
    bridge_env.hook("SessionStart", {"session_id": "conv-b", "cwd": cwd, "source": "clear"}, pid)
    with bridge_env.connect() as conn:
        live = conn.execute("SELECT conversation_id, ended_at FROM instances").fetchone()
    bridge_env.hook("SessionEnd", {"session_id": "conv-b", "cwd": cwd, "reason": "prompt_input_exit"}, pid)
    with bridge_env.connect() as conn:
        ended = conn.execute("SELECT status, ended_at FROM instances").fetchone()
        conversations = [r[0] for r in conn.execute("SELECT conversation_id FROM conversations ORDER BY 1")]
    assert (live["conversation_id"], live["ended_at"]) == ("conv-b", None)
    assert ended["status"] == "ended" and ended["ended_at"] is not None
    assert conversations == ["conv-a", "conv-b"]


def test_hook_fails_open(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    no_pid = bridge_env.hook("UserPromptSubmit", {"session_id": "x", "prompt": "hi"}, None)
    env = {**bridge_env.env, "CLAUDE_PID": str(spawn_process())}
    garbage = subprocess.run(
        [str(VENV_BIN / "bridge-hook"), "UserPromptSubmit"],
        input="not json",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert (no_pid.returncode, no_pid.stdout, no_pid.stderr) == (0, "", "")
    assert (garbage.returncode, garbage.stdout, garbage.stderr) == (0, "", "")
    assert "JSONDecodeError" in (bridge_env.state_dir / "errors.log").read_text()


def test_session_start_pins_restore_command(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    agterm = {"AGTERM_SESSION_ID": "TAB-1", "AGTERM_PANE_ID": "PANE-9", "AGTERM_SOCKET": "/tmp/s.sock"}
    pid = spawn_process(sys.executable, "-c", "import time; time.sleep(300)", env=agterm)
    bridge_env.write_session_file(pid, "conv-p")

    result = bridge_env.hook("SessionStart", {"session_id": "conv-p", "source": "startup"}, pid, agterm)

    assert result.returncode == 0
    assert wait_for(lambda: bridge_env.stub_log.exists())
    assert bridge_env.stub_log.read_text() == (
        f"session restore {sys.executable} --resume conv-p --target TAB-1 --pane-id PANE-9 --socket /tmp/s.sock\n"
    )
    with bridge_env.connect() as conn:
        assert wait_for(
            lambda: (
                conn.execute("SELECT restore_command FROM instances").fetchone()[0]
                == f"{sys.executable} --resume conv-p"
            )
        )


def test_non_interactive_session_is_not_pinned(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    agterm = {"AGTERM_SESSION_ID": "TAB-1", "AGTERM_PANE_ID": "PANE-9"}
    pid = spawn_process("/bin/sleep", "300", env=agterm)
    bridge_env.write_session_file(pid, "conv-q", kind="print")

    bridge_env.hook("SessionStart", {"session_id": "conv-q", "source": "startup"}, pid, agterm)

    assert not wait_for(lambda: bridge_env.stub_log.exists(), timeout_s=1.5)


def test_concurrent_hooks_from_many_sessions(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pids = [spawn_process() for _ in range(15)]

    def turn(pid: int) -> list[int]:
        payload = {"session_id": f"conv-{pid}", "cwd": str(bridge_env.root), "prompt": "ABC-1"}
        codes: list[int] = []
        for _ in range(4):
            codes.append(bridge_env.hook("UserPromptSubmit", payload, pid).returncode)
            codes.append(bridge_env.hook("Stop", {**payload, "last_assistant_message": "ok"}, pid).returncode)
        return codes

    with ThreadPoolExecutor(max_workers=15) as pool:
        codes = [c for result in pool.map(turn, pids) for c in result]

    assert set(codes) == {0}
    with bridge_env.connect() as conn:
        runs = conn.execute("SELECT count(*) FROM hook_runs").fetchone()[0]
        turns = [r[0] for r in conn.execute("SELECT turns FROM conversations")]
    errors = bridge_env.state_dir / "errors.log"
    assert not errors.exists(), errors.read_text()
    assert runs == 15 * 8
    assert turns == [4] * 15


def test_hook_latency(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pid = spawn_process()
    payload = {"session_id": "conv-l", "cwd": str(bridge_env.root), "prompt": "ABC-1 PR #12"}
    bridge_env.hook("SessionStart", {**payload, "source": "startup"}, pid)
    wall: list[float] = []
    for _ in range(30):
        started = time.perf_counter()
        bridge_env.hook("UserPromptSubmit", payload, pid)
        wall.append((time.perf_counter() - started) * 1000)
    with bridge_env.connect() as conn:
        rows = conn.execute("SELECT duration_us FROM hook_runs WHERE event = 'UserPromptSubmit'")
        in_process = [r[0] / 1000 for r in rows]
    print(f"hook wall p50={statistics.median(wall):.1f}ms, in-process p50={statistics.median(in_process):.1f}ms")
    # Recorded durations run from process start, so they include interpreter start-up and lock waits.
    assert statistics.median(in_process) < 100


def _github_repo(path: Path, branch: str) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", branch], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/acme/web.git"], cwd=path, check=True)
    return path


def test_nested_claude_neither_pins_nor_claims_the_tab(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    agterm = {"AGTERM_SESSION_ID": "TAB-1", "AGTERM_PANE_ID": "PANE-9"}
    nested_env = {**agterm, "CLAUDECODE": "1", "CLAUDE_PID": "1"}
    pid = spawn_process(sys.executable, "-c", "import time; time.sleep(300)", env=nested_env)
    bridge_env.write_session_file(pid, "conv-nested")

    bridge_env.hook("SessionStart", {"session_id": "conv-nested", "source": "startup"}, pid, agterm)

    assert not wait_for(lambda: bridge_env.stub_log.exists(), timeout_s=1.5)
    with bridge_env.connect() as conn:
        assert conn.execute("SELECT surface FROM instances").fetchone()["surface"] is None


def test_older_event_cannot_reattach_previous_conversation(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pid = spawn_process()
    config = load_config()
    env = {"CLAUDE_PID": str(pid)}
    now = time.time_ns()
    with bridge_env.connect() as conn:
        handle_hook(conn, "UserPromptSubmit", {"session_id": "old", "prompt": "x", "prompt_id": "p1"}, env, config, now)
        handle_hook(conn, "SessionStart", {"session_id": "new", "source": "clear"}, env, config, now + 2_000_000)
        handle_hook(conn, "Stop", {"session_id": "old", "last_assistant_message": "late"}, env, config, now + 1_000_000)
        handle_hook(conn, "SessionEnd", {"session_id": "old", "reason": "other"}, env, config, now + 1_500_000)
        instance = conn.execute("SELECT conversation_id, status, ended_at FROM instances").fetchone()
        old_reply = conn.execute("SELECT last_reply FROM conversations WHERE conversation_id = 'old'").fetchone()[0]
    assert (instance["conversation_id"], instance["status"], instance["ended_at"]) == ("new", "idle", None)
    assert old_reply == "late"


def test_duplicate_prompt_counts_once(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pid = spawn_process()
    payload = {"session_id": "conv-d", "cwd": str(bridge_env.root), "prompt": "same", "prompt_id": "p-1"}
    bridge_env.hook("UserPromptSubmit", payload, pid)
    bridge_env.hook("UserPromptSubmit", payload, pid)
    bridge_env.hook("UserPromptSubmit", {**payload, "prompt_id": "p-2"}, pid)
    with bridge_env.connect() as conn:
        assert conn.execute("SELECT turns FROM conversations").fetchone()[0] == 2


def test_compaction_keeps_working_session_active(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pid = spawn_process()
    base = {"session_id": "conv-c", "cwd": str(bridge_env.root)}
    bridge_env.hook("UserPromptSubmit", {**base, "prompt": "long task", "prompt_id": "p"}, pid)
    bridge_env.hook("SessionStart", {**base, "source": "compact"}, pid)
    with bridge_env.connect() as conn:
        assert conn.execute("SELECT status FROM instances").fetchone()[0] == "active"


def test_fork_pins_restore_command(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    agterm = {"AGTERM_SESSION_ID": "TAB-1", "AGTERM_PANE_ID": "PANE-9"}
    pid = spawn_process(sys.executable, "-c", "import time; time.sleep(300)", env=agterm)
    bridge_env.write_session_file(pid, "forked")

    bridge_env.hook("SessionStart", {"session_id": "forked", "source": "fork"}, pid, agterm)

    assert wait_for(lambda: bridge_env.stub_log.exists())
    assert "--resume forked" in bridge_env.stub_log.read_text()


def test_superseded_pin_job_does_nothing(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    agterm = {"AGTERM_SESSION_ID": "TAB-1", "AGTERM_PANE_ID": "PANE-9"}
    pid = spawn_process(sys.executable, "-c", "import time; time.sleep(300)", env=agterm)
    bridge_env.write_session_file(pid, "second")
    bridge_env.hook("UserPromptSubmit", {"session_id": "second", "prompt": "x"}, pid)
    instance_id = f"{pid}@{process_start_ms(pid)}"

    stale = bridge_env.cli("_pin-restore", instance_id, "first")

    assert stale.returncode == 0
    assert not bridge_env.stub_log.exists()


def test_hook_gives_up_quickly_when_database_is_locked(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pid = spawn_process()
    payload = {"session_id": "conv-lock", "cwd": str(bridge_env.root), "prompt": "x"}
    bridge_env.hook("UserPromptSubmit", payload, pid)
    blocker = bridge_env.connect()
    blocker.execute("BEGIN IMMEDIATE")
    try:
        started = time.perf_counter()
        result = bridge_env.hook("UserPromptSubmit", {**payload, "prompt_id": "p2"}, pid)
        elapsed_ms = (time.perf_counter() - started) * 1000
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert (result.returncode, result.stdout) == (0, "")
    assert elapsed_ms < 3000
    assert "database is locked" in (bridge_env.state_dir / "errors.log").read_text()


def test_hook_outlasts_a_lock_held_past_its_first_wait(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(bridge_env.state_dir))
    pid = spawn_process()
    env = {"CLAUDE_PID": str(pid)}
    payload: dict[str, object] = {"session_id": "conv-lock", "cwd": str(bridge_env.root), "prompt": "x"}
    conn = db.connect(bridge_env.db, db.HOOK_BUSY_TIMEOUT_MS)
    handle_hook(conn, "UserPromptSubmit", payload, env, load_config(), time.time_ns())
    locked = threading.Event()
    first_begin = threading.Event()
    conn.set_trace_callback(lambda sql: first_begin.set() if sql == "BEGIN IMMEDIATE" else None)

    def hold_lock() -> None:
        blocker = bridge_env.connect()
        blocker.execute("BEGIN IMMEDIATE")
        locked.set()
        first_begin.wait(10)
        # Released after the hook's first wait has run out, as by a holder starved of CPU.
        time.sleep(db.HOOK_BUSY_TIMEOUT_MS / 1000 * 2)
        blocker.execute("ROLLBACK")
        blocker.close()

    blocker = threading.Thread(target=hold_lock)
    blocker.start()
    locked.wait()
    second: dict[str, object] = {**payload, "prompt": "second", "prompt_id": "p2"}
    handle_hook(conn, "UserPromptSubmit", second, env, load_config(), time.time_ns())
    blocker.join()
    assert conn.execute("SELECT last_prompt FROM conversations").fetchone()[0] == "second"
    conn.close()
    assert "db lock retried" in (bridge_env.state_dir / "errors.log").read_text()


def test_a_lock_held_too_long_is_logged_with_its_caller(bridge_env: BridgeEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(bridge_env.state_dir))
    conn = bridge_env.connect()
    clock = iter([0.0, 1.0, 1.5, 1.5])
    monkeypatch.setattr(db.time, "monotonic", lambda: next(clock))
    with db.transaction(conn):
        pass
    assert not (bridge_env.state_dir / "errors.log").exists()
    clock = iter([0.0, 0.0, 1.0, 1.0])
    with db.transaction(conn):
        pass
    monkeypatch.undo()
    conn.close()
    logged = (bridge_env.state_dir / "errors.log").read_text()
    assert "db lock\n" in logged and "test_hooks.py" in logged and "test_a_lock_held_too_long" in logged


def test_stale_and_replayed_events(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pid = spawn_process()
    config = load_config()
    env = {"CLAUDE_PID": str(pid)}

    def hook(conn: sqlite3.Connection, event: str, t: int, conv: str = "c", **payload: object) -> None:
        handle_hook(conn, event, {"session_id": conv, **payload}, env, config, BASE_NS + t * 1_000_000)

    def instance(conn: sqlite3.Connection) -> sqlite3.Row:
        return conn.execute("SELECT conversation_id, status, ended_at FROM instances").fetchone()

    with bridge_env.connect() as conn:
        hook(conn, "SessionStart", 10, source="startup")
        hook(conn, "SessionEnd", 30, reason="prompt_input_exit")
        hook(conn, "SessionStart", 20, source="resume")
        assert instance(conn)["ended_at"] is not None, "a stale start must not revive an ended session"

    bridge_env.db.unlink()
    with bridge_env.connect() as conn:
        hook(conn, "SessionStart", 10, conv="old", source="startup")
        hook(conn, "SessionStart", 30, conv="new", source="clear")
        hook(conn, "Stop", 40, conv="old", last_assistant_message="late")
        assert tuple(instance(conn))[:2] == ("new", "idle"), "a late Stop of the old conversation must not reattach it"

    bridge_env.db.unlink()
    with bridge_env.connect() as conn:
        hook(conn, "UserPromptSubmit", 10, prompt_id="p1", prompt="first")
        hook(conn, "UserPromptSubmit", 30, prompt_id="p2", prompt="second")
        hook(conn, "Stop", 35)
        hook(conn, "UserPromptSubmit", 40, prompt_id="p1", prompt="first")
        turns, last_prompt = conn.execute("SELECT turns, last_prompt FROM conversations").fetchone()
        assert (turns, last_prompt, instance(conn)["status"]) == (2, "second", "idle")


def test_replayed_prompt_and_late_end_keep_new_conversation(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pid = spawn_process()
    config = load_config()
    env = {"CLAUDE_PID": str(pid)}

    def hook(conn: sqlite3.Connection, event: str, t: int, conv: str, **payload: object) -> None:
        handle_hook(conn, event, {"session_id": conv, **payload}, env, config, BASE_NS + t * 1_000_000)

    with bridge_env.connect() as conn:
        hook(conn, "UserPromptSubmit", 10, "old", prompt_id="p1", prompt="x")
        hook(conn, "SessionStart", 20, "new", source="clear")
        hook(conn, "UserPromptSubmit", 30, "old", prompt_id="p1", prompt="x")
        hook(conn, "SessionEnd", 40, "old", reason="other")
        row = conn.execute("SELECT conversation_id, ended_at FROM instances").fetchone()
    assert (row["conversation_id"], row["ended_at"]) == ("new", None)


BASE_NS = time.time_ns()


def test_a_taken_start_marker_never_replaces_a_newer_one_and_a_dead_takers_comes_back(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(bridge_env.state_dir))
    write_pending_start("conv-m", PendingStart("compact", 1, True, []))
    first = take_pending_start("conv-m")
    write_pending_start("conv-m", PendingStart("compact", 2, True, []))
    assert first is not None
    put_back_taken_start("conv-m", first)
    newest = take_pending_start("conv-m")
    assert newest is not None and newest.pending.at_ms == 2, "the rollback did not bring the older start back"
    finish_taken_start(newest)
    assert not has_pending_start("conv-m")

    write_pending_start("conv-m", PendingStart("resume", 3, False, []))
    taken = take_pending_start("conv-m")
    assert taken is not None
    dead = spawn_process()
    os.kill(dead, 9)
    os.waitpid(dead, 0)
    orphan = taken.path.with_name(f"conv-m.json.taken-{dead}-1")
    taken.path.rename(orphan)
    assert has_pending_start("conv-m"), "a start whose taker was killed before commit is still pending"
    again = take_pending_start("conv-m")
    assert again is not None and again.pending.source == "resume"
