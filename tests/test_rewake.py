import io
import json
import os
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from unittest import mock

import pytest
from conftest import VENV_BIN, BridgeEnv, SpawnProcess, wait_for
from test_leads import Team
from test_typing import FakeClaudeBox, _overdue_message_to  # pyright: ignore[reportPrivateUsage]

from bridge.config import Config
from bridge.feed import deliver_typed
from bridge.install import BUSY_MARK_COMMAND, REWAKE_SUMMARY, install_hooks, uninstall_hooks
from bridge.procinfo import process_start_ms
from bridge.rewake import UNHEARD, run_rewake, write_rewake
from bridge.rewake_marks import (
    WaiterDeferral,
    WaiterState,
    install_waiter_script,
    is_turn_running,
    prune_rewake_marks,
    read_waiter,
)
from bridge.tabinput import type_when_idle

FAKE_HOOK = """#!/bin/sh
echo "$1" >> "$FAKE_DIR/calls"
code=0
[ -f "$FAKE_DIR/code" ] && read -r code < "$FAKE_DIR/code"
echo "noise of attempt $code" >&2
[ "$code" = 2 ] && cat "$FAKE_DIR/mail" >&"$BRIDGE_REWAKE_FD"
exit "$code"
"""


class Waiter:
    """The installed waiter script run as Claude Code runs it, for a stand-in Claude process and a stand-in hook."""

    def __init__(self, tmp_path: Path, claude_pid: int, entrypoint: str = "cli") -> None:
        self.root = tmp_path
        self.state = tmp_path / "state"
        self.sessions = tmp_path / "sessions"
        self.fake = tmp_path / "fake"
        for d in (self.state / "hints", self.state / "rewake", self.sessions, self.fake):
            d.mkdir(parents=True, exist_ok=True)
        self.pid = claude_pid
        (self.sessions / f"{claude_pid}.json").write_text(json.dumps({"pid": claude_pid, "entrypoint": entrypoint}))
        self.hook = self.fake / "bridge-hook"
        self.hook.write_text(FAKE_HOOK)
        self.hook.chmod(0o755)
        self.script = install_waiter_script(tmp_path / "bin")
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "CLAUDE_PID": str(claude_pid),
            "BRIDGE_STATE_DIR": str(self.state),
            "BRIDGE_CLAUDE_SESSIONS_DIR": str(self.sessions),
            "BRIDGE_REWAKE_POLL_S": "0.1",
            "FAKE_DIR": str(self.fake),
        }

    @property
    def busy(self) -> Path:
        return self.state / "rewake" / f"{self.pid}.busy"

    @property
    def lock(self) -> Path:
        return self.state / "rewake" / f"{self.pid}.waiter"

    def start(self, event: str = "Stop", payload: str = "{}") -> subprocess.Popen[str]:
        proc = subprocess.Popen(
            ["sh", str(self.script), str(self.hook), event],
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdin is not None
        proc.stdin.write(payload)
        proc.stdin.close()
        proc.stdin = None
        return proc

    def hint(self, value: str) -> None:
        (self.state / "hints" / str(self.pid)).write_text(f"{value}\n")

    def read_hint(self) -> str:
        return (self.state / "hints" / str(self.pid)).read_text().strip()

    def calls(self) -> list[str]:
        path = self.fake / "calls"
        return path.read_text().split() if path.exists() else []

    def answer(self, code: int, mail: str = "") -> None:
        (self.fake / "code").write_text(f"{code}\n")
        (self.fake / "mail").write_text(mail)

    def listening(self) -> bool:
        return (self.lock / "pid").exists()


def _finish(proc: subprocess.Popen[str], timeout_s: float = 5) -> tuple[int, str]:
    try:
        _, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    return proc.returncode, err


def test_a_flagged_hint_wakes_an_idle_tab_with_the_hooks_mail_and_marks_the_turn_busy(
    tmp_path: Path, spawn_process: SpawnProcess
) -> None:
    waiter = Waiter(tmp_path, spawn_process())
    waiter.answer(2, "[bridge] From the user: look at #12\n")
    waiter.busy.write_text("")

    proc = waiter.start("Stop")
    assert wait_for(waiter.listening)
    waiter.hint("0")
    code, err = _finish(proc)

    assert (code, err) == (2, "[bridge] From the user: look at #12\n")
    assert waiter.calls() == ["rewake"]
    assert "noise of attempt 2" in (waiter.state / "errors.log").read_text()
    assert waiter.busy.exists()
    assert not waiter.lock.exists()


def test_a_busy_tab_and_a_hint_that_is_only_due_wake_nothing(tmp_path: Path, spawn_process: SpawnProcess) -> None:
    waiter = Waiter(tmp_path, spawn_process())
    waiter.answer(2, "mail\n")
    proc = waiter.start("SessionStart", '{"source":"startup"}')
    assert wait_for(waiter.listening)

    waiter.hint(str(int(time.time()) - 5))
    time.sleep(0.5)
    waiter.busy.write_text("")
    waiter.hint("0")
    time.sleep(0.5)
    held = waiter.calls()
    waiter.busy.unlink()
    code, _ = _finish(proc)

    assert held == []
    assert code == 2 and waiter.calls() == ["rewake"]


def test_nothing_to_deliver_is_asked_once_and_the_waiter_leaves_with_its_claude(tmp_path: Path) -> None:
    claude = subprocess.Popen(["sleep", "60"])
    waiter = Waiter(tmp_path, claude.pid)
    waiter.answer(0)
    proc = waiter.start()
    assert wait_for(waiter.listening)

    waiter.hint("0")
    assert wait_for(lambda: waiter.calls() == ["rewake"])
    time.sleep(0.5)
    asked = waiter.calls()
    hint = waiter.read_hint()
    waiter.busy.write_text("")
    claude.kill()
    claude.wait()
    code, err = _finish(proc)

    assert asked == ["rewake"]
    # Still due for a tool hook's check, no longer a call for this waiter.
    assert hint == "1"
    assert (code, err) == (0, "")
    assert not waiter.lock.exists() and not waiter.busy.exists()


def test_a_failed_attempt_leaves_nothing_in_the_turn_it_later_wakes(
    tmp_path: Path, spawn_process: SpawnProcess
) -> None:
    waiter = Waiter(tmp_path, spawn_process())
    waiter.answer(1)
    proc = waiter.start()
    assert wait_for(waiter.listening)

    waiter.hint("0")
    assert wait_for(lambda: waiter.calls() == ["rewake"])
    waiter.answer(2, "mail\n")
    waiter.hint("0")
    code, err = _finish(proc)

    assert (code, err) == (2, "mail\n")
    assert waiter.calls() == ["rewake", "rewake"]


def test_the_newest_waiter_takes_over_and_one_killed_outright_is_replaced(
    tmp_path: Path, spawn_process: SpawnProcess
) -> None:
    """After /restart the same Claude pid runs a new Claude Code, which no longer reads the older waiter."""
    waiter = Waiter(tmp_path, spawn_process())
    first = waiter.start("SessionStart")
    assert wait_for(waiter.listening)

    second = waiter.start("Stop")
    first_code, _ = _finish(first)
    owner_after_second = (waiter.lock / "pid").read_text().strip()
    second.kill()
    second.communicate()
    third = waiter.start("Stop")
    assert wait_for(lambda: (waiter.lock / "pid").read_text().strip() == str(third.pid))
    third.terminate()
    third_code, _ = _finish(third)

    assert first_code == 0 and owner_after_second == str(second.pid)
    assert third_code == 0
    assert not waiter.lock.exists()


def test_a_waiter_claude_code_no_longer_reads_leaves_at_once(tmp_path: Path, spawn_process: SpawnProcess) -> None:
    waiter = Waiter(tmp_path, spawn_process())
    waiter.answer(3)
    proc = waiter.start()
    assert wait_for(waiter.listening)

    waiter.hint("0")
    code, _ = _finish(proc)

    assert code == 0
    assert waiter.calls() == ["rewake"]
    assert not waiter.lock.exists(), "a typer finds no waiter and types at once"


def test_a_print_mode_claude_gets_no_waiter_and_a_compaction_keeps_the_turn_busy(
    tmp_path: Path, spawn_process: SpawnProcess
) -> None:
    printing = Waiter(tmp_path / "p", spawn_process(), entrypoint="sdk-cli")
    printing.busy.write_text("")
    printing_code, _ = _finish(printing.start("Stop"))

    compacting = Waiter(tmp_path / "c", spawn_process())
    compacting.busy.write_text("")
    proc = compacting.start("SessionStart", '{"session_id":"x","source":"compact"}')
    assert wait_for(compacting.listening)
    kept = compacting.busy.exists()
    proc.terminate()
    _finish(proc)

    assert printing_code == 0 and not printing.lock.exists() and not printing.busy.exists()
    assert kept


def test_a_session_start_waiter_waits_for_the_record_claude_code_writes_after_it(
    tmp_path: Path, spawn_process: SpawnProcess
) -> None:
    waiter = Waiter(tmp_path / "w", spawn_process())
    record = waiter.sessions / f"{waiter.pid}.json"
    written = record.read_text()
    record.unlink()
    proc = waiter.start("SessionStart", '{"session_id":"x","source":"startup"}')
    time.sleep(0.5)
    started_early = waiter.listening()
    record.write_text(written)
    listens = wait_for(waiter.listening)
    proc.terminate()
    _finish(proc)

    gone = Waiter(tmp_path / "g", spawn_process())
    (gone.sessions / f"{gone.pid}.json").unlink()
    gone.env["BRIDGE_REWAKE_RECORD_TRIES"] = "3"
    gone_code, _ = _finish(gone.start("SessionStart", "{}"))

    assert not started_early and listens
    assert gone_code == 0 and not gone.lock.exists(), "a Claude that never writes its record gets no waiter"


def test_a_prompt_marks_the_tab_busy_in_the_shell(tmp_path: Path) -> None:
    (tmp_path / "rewake").mkdir()
    env = {"PATH": os.environ["PATH"], "BRIDGE_STATE_DIR": str(tmp_path), "CLAUDE_PID": "4242"}

    done = subprocess.run(["sh", "-c", BUSY_MARK_COMMAND], env=env, capture_output=True, text=True, check=False)

    assert done.returncode == 0 and done.stdout == done.stderr == ""
    assert (tmp_path / "rewake" / "4242.busy").exists()


def test_install_adds_the_waiter_hooks_once_and_uninstall_takes_them_away(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    foreign = {"hooks": [{"type": "command", "command": "/x/notify.sh"}]}
    settings.write_text(json.dumps({"hooks": {"Stop": [foreign]}}))
    script = tmp_path / "bridge-rewake.sh"

    install_hooks(settings, "/u/bin/bridge-hook", waiter_script=script)
    install_hooks(settings, "/u/bin/bridge-hook", waiter_script=script)
    hooks = json.loads(settings.read_text())["hooks"]

    stop = [entry for group in hooks["Stop"] for entry in group["hooks"]]
    assert [e["command"] for e in stop] == [
        "/x/notify.sh",
        "/u/bin/bridge-hook Stop",
        f"sh {script} /u/bin/bridge-hook Stop",
    ]
    assert stop[2]["asyncRewake"] is True and stop[2]["rewakeSummary"] == REWAKE_SUMMARY
    assert [e["command"] for g in hooks["SessionStart"] for e in g["hooks"]][-1].endswith("bridge-hook SessionStart")
    assert [e["command"] for g in hooks["UserPromptSubmit"] for e in g["hooks"]][-1] == BUSY_MARK_COMMAND

    uninstall_hooks(settings)
    assert json.loads(settings.read_text()) == {"hooks": {"Stop": [foreign]}}


def _deferral(
    state: list[WaiterState], running: list[bool], now: list[float], nudged: list[int], grace_s: float = 10
) -> WaiterDeferral:
    return WaiterDeferral(
        7,
        "#1 to c",
        grace_s=grace_s,
        waiter=lambda _: state[0],
        turn_running=lambda _: running[0],
        nudge=nudged.append,
        clock=lambda: now[0],
    )


def test_the_deferral_flags_the_waiter_once_and_types_after_the_grace() -> None:
    now, nudged = [100.0], list[int]()
    state: list[WaiterState] = ["idle"]
    deferral = _deferral(state, [False], now, nudged)

    first = deferral.may_type()
    now[0] += 9
    within = deferral.may_type()
    now[0] += 2
    after = deferral.may_type()
    state[0] = "busy"
    still = deferral.may_type()

    assert (first, within, after, still) == (False, False, True, True)
    assert nudged == [7] and deferral.gave_up


def test_typing_waits_out_a_running_turn_and_not_one_whose_end_went_unreported() -> None:
    now, nudged = [0.0], list[int]()
    state: list[WaiterState] = ["absent"]
    running = [True]
    deferral = _deferral(state, running, now, nudged)

    absent = deferral.may_type()
    state[0] = "busy"
    during_turn = deferral.may_type()
    state[0] = "idle"
    after_turn = deferral.may_type()
    now[0] += 8
    state[0] = "busy"
    running[0] = False
    escaped = deferral.may_type()
    state[0] = "idle"
    now[0] += 8
    again = deferral.may_type()

    assert (absent, during_turn, after_turn, escaped, again) == (True, False, False, True, False)
    assert nudged == [7, 7] and not deferral.gave_up


def test_a_waiter_listens_only_while_its_beat_is_fresh_and_its_owner_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    lock = tmp_path / "rewake" / "55.waiter"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{os.getpid()}\n")
    beat = (lock / "pid").stat().st_mtime

    fresh = read_waiter(55, now=beat + 1)
    stale = read_waiter(55, now=beat + 30)
    (tmp_path / "rewake" / "55.busy").write_text("")
    busy = read_waiter(55, now=beat + 1)
    (lock / "pid").write_text("999999\n")
    woken = read_waiter(55)
    (tmp_path / "rewake" / "55.busy").unlink()
    gone = read_waiter(55)

    # A waking waiter leaves before its turn ends: the busy mark alone holds a typer back then.
    assert (fresh, stale, busy, woken, gone) == ("idle", "absent", "busy", "busy", "absent")


def test_claude_codes_own_record_says_whether_a_turn_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIDGE_CLAUDE_SESSIONS_DIR", str(tmp_path))

    def status(value: str) -> bool:
        (tmp_path / "77.json").write_text(json.dumps({"pid": 77, "sessionId": "s", "status": value}))
        return is_turn_running(77)

    assert [status(v) for v in ("busy", "waiting", "idle", "shell")] == [True, True, False, False]
    assert not is_turn_running(78)


def test_marks_of_gone_claudes_are_pruned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    marks = tmp_path / "rewake"
    gone = subprocess.Popen(["true"])
    gone.wait()
    (marks / f"{gone.pid}.waiter").mkdir(parents=True)
    (marks / f"{gone.pid}.waiter" / "pid").write_text("1\n")
    (marks / f"{gone.pid}.busy").write_text("")
    (marks / f"{os.getpid()}.busy").write_text("")

    prune_rewake_marks()

    assert sorted(p.name for p in marks.iterdir()) == [f"{os.getpid()}.busy"]


def _rewake(bridge_env: BridgeEnv, pid: int) -> subprocess.CompletedProcess[str]:
    env = {**bridge_env.env, "CLAUDE_PID": str(pid)}
    return subprocess.run(
        [str(VENV_BIN / "bridge-hook"), "rewake"], env=env, capture_output=True, text=True, timeout=30, check=False
    )


def _row(conn: sqlite3.Connection, message_id: int) -> tuple[object, ...]:
    return tuple(
        conn.execute(
            "SELECT offered_at IS NOT NULL, acked_at IS NOT NULL FROM message_recipients WHERE message_id = ?",
            (message_id,),
        ).fetchone()
    )


def test_rewake_hands_a_lead_its_mail_once_as_a_hook_would(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    message_id = _overdue_message_to(bridge_env, "alex", team.b)

    woke = _rewake(bridge_env, team.a)
    again = _rewake(bridge_env, team.a)
    with bridge_env.connect() as conn:
        row = _row(conn, message_id)
        status = conn.execute("SELECT status FROM instances WHERE pid = ?", (team.a,)).fetchone()[0]
    hint = (bridge_env.state_dir / "hints" / str(team.a)).read_text().strip()

    assert woke.returncode == 2 and woke.stdout == ""
    assert woke.stderr.startswith("[bridge] Peer messages") and "please look" in woke.stderr
    assert row == (1, 1)
    assert status == "active"
    assert int(hint) > time.time()
    assert (again.returncode, again.stderr) == (0, "")


def test_rewake_leaves_mail_no_typer_would_bring_and_mail_a_typer_holds(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    to_plain = bridge_env.cli("send", "old-b", "fyi", pid=team.a)
    assert to_plain.returncode == 0, to_plain.stderr
    message_id = _overdue_message_to(bridge_env, "alex", team.b)
    # Past a request's first reminder, well within a live typer's hold.
    claimed_at = int(time.time() * 1000) - 300_000
    with bridge_env.connect() as conn:
        conn.execute(
            "UPDATE message_recipients SET offered_at = ?1, typing_started_at = ?1, typing_pid = ?2,"
            " typing_pid_start_ms = ?3 WHERE message_id = ?4",
            (claimed_at, os.getpid(), process_start_ms(os.getpid()), message_id),
        )
        conn.execute("UPDATE messages SET kind = 'request' WHERE id = ?", (message_id,))

    plain = _rewake(bridge_env, team.b)
    typing = _rewake(bridge_env, team.a)
    other_id = _overdue_message_to(bridge_env, "alex", team.b)
    with_other = _rewake(bridge_env, team.a)
    with bridge_env.connect() as conn:
        claimed = conn.execute(
            "SELECT offered_at, typing_started_at FROM message_recipients WHERE message_id = ?", (message_id,)
        ).fetchone()
    with bridge_env.connect() as conn:
        plain_offered = conn.execute(
            "SELECT offered_at FROM message_recipients WHERE conversation_id = 'conv-b'"
        ).fetchone()[0]

    assert (plain.returncode, plain.stderr) == (0, "")
    assert plain_offered is None
    assert (typing.returncode, typing.stderr) == (0, "")
    # Another message wakes the tab; the request a typer holds, due for a reminder by its age, stays the typer's.
    assert with_other.returncode == 2 and f"#{other_id}" in with_other.stderr
    assert f"#{message_id}" not in with_other.stderr
    assert tuple(claimed) == (claimed_at, claimed_at)


def test_mail_a_closing_claude_cannot_read_stays_waiting(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    message_id = _overdue_message_to(bridge_env, "alex", team.b)

    class ClosedPipe(io.StringIO):
        def write(self, s: str) -> int:
            raise BrokenPipeError

    with bridge_env.connect() as conn, mock.patch.dict(os.environ, bridge_env.env):
        with pytest.raises(BrokenPipeError):
            write_rewake(conn, Config(), team.a, time.time_ns(), ClosedPipe())
        row = _row(conn, message_id)

    assert row == (0, 0)


def test_a_pipe_claude_closed_is_no_error_and_the_mail_still_waits(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    message_id = _overdue_message_to(bridge_env, "alex", team.b)
    read_end, write_end = os.pipe()
    os.close(read_end)
    env = {**bridge_env.env, "CLAUDE_PID": str(team.a), "BRIDGE_REWAKE_FD": str(write_end)}
    try:
        with mock.patch.dict(os.environ, env):
            code = run_rewake()
    finally:
        os.close(write_end)
    errors = bridge_env.state_dir / "errors.log"

    assert code == UNHEARD
    assert not errors.exists() or "rewake" not in errors.read_text()
    with bridge_env.connect() as conn:
        assert _row(conn, message_id) == (0, 0)


def _ready_waiter(bridge_env: BridgeEnv, pid: int) -> None:
    lock = bridge_env.state_dir / "rewake" / f"{pid}.waiter"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{os.getpid()}\n")


def _deliver(bridge_env: BridgeEnv, message_id: int, fake: FakeClaudeBox) -> str | None:
    def beat() -> None:
        # The waiter rewrites its beat every round.
        while not stop.is_set():
            for lock in (bridge_env.state_dir / "rewake").glob("*.waiter"):
                (lock / "pid").write_text(f"{os.getpid()}\n")
            time.sleep(0.5)

    stop = threading.Event()
    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    try:
        with (
            bridge_env.connect() as conn,
            mock.patch.dict(os.environ, bridge_env.env),
            mock.patch("bridge.tabinput.run_agterm", fake.run),
            mock.patch("bridge.feed.is_tab_selected", lambda *_: False),  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
        ):
            return deliver_typed(conn, Config(agtermctl="unused"), message_id, "conv-a")
    finally:
        stop.set()
        thread.join()


def test_a_typer_leaves_an_idle_tab_to_its_waiter(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    message_id = _overdue_message_to(bridge_env, "alex", team.b)
    _ready_waiter(bridge_env, team.a)
    hint = bridge_env.state_dir / "hints" / str(team.a)
    hint.write_text("99\n")

    def waiter_takes_it() -> None:
        assert wait_for(lambda: hint.read_text().strip() == "0", 10)
        assert _rewake(bridge_env, team.a).returncode == 2

    taker = threading.Thread(target=waiter_takes_it)
    taker.start()
    fake = FakeClaudeBox()
    result = _deliver(bridge_env, message_id, fake)
    taker.join()

    assert result == "unwanted"
    assert fake.typed == []


def test_a_typer_types_when_the_waiter_does_not_take_the_mail_and_says_so(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    message_id = _overdue_message_to(bridge_env, "alex", team.b)
    _ready_waiter(bridge_env, team.a)
    fake = FakeClaudeBox()

    started = time.monotonic()
    with mock.patch("bridge.rewake_marks.REWAKE_GRACE_S", 2.0):
        result = _deliver(bridge_env, message_id, fake)
    took = time.monotonic() - started
    errors = (bridge_env.state_dir / "errors.log").read_text()

    assert result == "typed"
    assert len(fake.submitted) == 1 and f"#{message_id}" in fake.submitted[0]
    assert took >= 2.0
    assert f"#{message_id} to conv-a: the waiter of Claude pid {team.a} did not take it in 2 s; typing it" in errors


def test_held_back_typing_presses_no_key_and_reads_no_screen(tmp_path: Path) -> None:
    fake = FakeClaudeBox()
    calls: list[list[str]] = []
    asked = [0]

    def run(config: Config, args: list[str]) -> str | None:
        calls.append(args)
        return fake.run(config, args)

    def may_type() -> bool:
        asked[0] += 1
        return False

    with mock.patch("bridge.tabinput.run_agterm", run), mock.patch("bridge.tabinput.state_dir", lambda: tmp_path):
        held = type_when_idle(Config(), "S", "P", "hello", lambda: asked[0] < 3, wait_s=10, may_type=may_type)

    assert held == "unwanted"
    assert calls == []


def test_a_typer_waits_out_a_running_turn_and_leaves_its_end_to_the_waiter(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    message_id = _overdue_message_to(bridge_env, "alex", team.b)
    _ready_waiter(bridge_env, team.a)
    busy = bridge_env.state_dir / "rewake" / f"{team.a}.busy"
    busy.write_text("")
    bridge_env.write_session_file(team.a, "conv-a", status="busy")
    hint = bridge_env.state_dir / "hints" / str(team.a)
    hint.write_text("99\n")

    def turn_ends_and_waiter_takes_it() -> None:
        time.sleep(3)
        bridge_env.write_session_file(team.a, "conv-a", status="idle")
        busy.unlink()
        assert wait_for(lambda: hint.read_text().strip() == "0", 10)
        assert _rewake(bridge_env, team.a).returncode == 2

    taker = threading.Thread(target=turn_ends_and_waiter_takes_it)
    taker.start()
    # A screen that looks idle mid-turn, as one streaming its answer does.
    fake = FakeClaudeBox()
    result = _deliver(bridge_env, message_id, fake)
    taker.join()

    assert result == "unwanted"
    assert fake.typed == []


def test_the_waiter_wakes_a_lead_only_once_its_mail_makes_a_batch(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    message_id = _overdue_message_to(bridge_env, "alex", team.b)
    batched = Config(batch_count=10, batch_age_min=5)

    with bridge_env.connect() as conn, mock.patch.dict(os.environ, bridge_env.env):
        now = int(time.time() * 1000)
        conn.execute("UPDATE messages SET created_at = ? WHERE id = ?", (now - 4 * 60_000, message_id))
        held_out = io.StringIO()
        held = write_rewake(conn, batched, team.a, time.time_ns(), held_out)
        conn.execute("UPDATE messages SET created_at = ? WHERE id = ?", (now - 5 * 60_000, message_id))
        ripe_out = io.StringIO()
        ripe = write_rewake(conn, batched, team.a, time.time_ns(), ripe_out)

    assert (held, held_out.getvalue()) == (False, "")
    assert ripe and f"#{message_id}" in ripe_out.getvalue()
