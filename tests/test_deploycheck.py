import json
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import VENV_BIN, BridgeEnv

from bridge import deploycheck
from bridge.cli import exit_on_signal
from bridge.config import Config
from bridge.db import connect
from bridge.deploycheck import end_check_claude
from bridge.workers import list_live_workers

SLEEP = "import time; time.sleep(30)"


def test_the_checks_claude_is_ended_and_no_other_process(tmp_path: Path) -> None:
    settings = str(tmp_path / "settings.json")
    ours = subprocess.Popen([sys.executable, "-c", SLEEP, settings])
    other = subprocess.Popen([sys.executable, "-c", SLEEP, "other"])
    try:
        (tmp_path / "ours.pid").write_text(f"{ours.pid}\n")
        (tmp_path / "other.pid").write_text(f"{other.pid}\n")
        end_check_claude(tmp_path / "other.pid", settings)
        end_check_claude(tmp_path / "ours.pid", settings)
        end_check_claude(tmp_path / "missing.pid", settings)
        assert ours.wait(timeout=5) != 0
        assert other.poll() is None
    finally:
        for process in (ours, other):
            process.kill()
            process.wait()


CHECK_NAMES = (deploycheck.CHECK_TAB_NAME, deploycheck.REWAKE_TAB_NAME, deploycheck.LONG_TAB_NAME)


class FakeAgterm:
    """agterm's tree as {id: name}; a close can be made to time out a number of times, a create to lose its reply."""

    def __init__(self, tabs: dict[str, str] | None = None, close_timeouts: int = 0, lose_create_reply: bool = False):
        self.tabs = dict(tabs or {})
        self.close_timeouts = close_timeouts
        self.lose_create_reply = lose_create_reply
        self.tree_failures_after_create = 0
        self.calls: list[list[str]] = []

    def __call__(self, config: Config, args: list[str]) -> str | None:
        self.calls.append(args)
        if args[0] == "tree" and self.tree_failures_after_create and ["session", "new"] in [c[:2] for c in self.calls]:
            self.tree_failures_after_create -= 1
            return None
        if args[0] == "tree":
            sessions = [{"id": tab, "name": name, "parent": "TAB-LEAD"} for tab, name in self.tabs.items()]
            return json.dumps({"ok": True, "result": {"tree": {"workspaces": [{"name": "w", "sessions": sessions}]}}})
        if args[:2] == ["session", "new"]:
            tab = f"TAB-{len(self.tabs) + 1}"
            self.tabs[tab] = args[args.index("--name") + 1]
            return None if self.lose_create_reply else json.dumps({"ok": True, "result": {"id": tab}})
        if args[:2] == ["session", "close"]:
            if self.close_timeouts:
                self.close_timeouts -= 1
                return None
            return "" if self.tabs.pop(args[args.index("--target") + 1], None) is not None else None
        return ""


@pytest.fixture
def agterm(monkeypatch: pytest.MonkeyPatch) -> FakeAgterm:
    fake = FakeAgterm()
    monkeypatch.setattr(deploycheck, "run_agterm", fake)
    monkeypatch.delenv("AGTERM_SESSION_ID", raising=False)
    return fake


def _fail_typing(config: Config, session: str) -> deploycheck.CheckResult:
    raise RuntimeError("the check broke")


def _pass_typing(config: Config, session: str) -> deploycheck.CheckResult:
    return deploycheck.CheckResult(True, "typed")


def _find_nothing(config: Config, name: str) -> str | None:
    return None


def _fail_in_claude(config: Config, session: str, work: Path, state: Path) -> deploycheck.CheckResult:
    raise RuntimeError("the check broke")


def test_a_failing_check_closes_its_tab_though_a_close_times_out(
    agterm: FakeAgterm, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deploycheck, "_type_and_see", _fail_typing)
    monkeypatch.setattr(deploycheck, "_wake_and_see", _fail_in_claude)
    monkeypatch.setattr(deploycheck, "_type_long_and_see", _fail_in_claude)

    for check in (
        lambda: deploycheck.check_typing(Config()),
        lambda: deploycheck.check_rewake(Config(), claude=sys.executable),
        lambda: deploycheck.check_long_message(Config(), claude=sys.executable),
    ):
        agterm.calls.clear()
        agterm.close_timeouts = 1
        with pytest.raises(RuntimeError):
            check()
        assert agterm.tabs == {}
        assert [call[:2] for call in agterm.calls].count(["session", "close"]) == 2


def test_a_tab_whose_create_reply_was_lost_is_found_by_name_and_closed(
    agterm: FakeAgterm, monkeypatch: pytest.MonkeyPatch
) -> None:
    agterm.lose_create_reply = True
    monkeypatch.setattr(deploycheck, "_find_session_named", _find_nothing)

    result = deploycheck.check_typing(Config())

    assert not result.ok and "did not create" in result.detail
    assert agterm.tabs == {}


def test_a_lost_tab_found_only_on_the_last_try_is_still_closed(
    agterm: FakeAgterm, monkeypatch: pytest.MonkeyPatch
) -> None:
    agterm.lose_create_reply = True
    agterm.tree_failures_after_create = deploycheck.CLOSE_TRIES - 1
    monkeypatch.setattr(deploycheck, "_find_session_named", _find_nothing)

    deploycheck.check_typing(Config())

    assert agterm.tabs == {}


def test_a_check_tab_that_will_not_close_is_reported(agterm: FakeAgterm, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deploycheck, "_type_and_see", _pass_typing)
    agterm.close_timeouts = deploycheck.CLOSE_TRIES

    result = deploycheck.check_typing(Config())

    assert not result.ok and "did not close: close it by hand" in result.detail


def test_only_a_check_tab_older_than_any_check_is_closed_at_the_next_start(agterm: FakeAgterm) -> None:
    now = time.time()
    old = int(now - deploycheck.CHECK_TAB_STALE_S - 60)
    young = int(now - 60)
    agterm.tabs = {
        "TAB-OLD": f"{deploycheck.REWAKE_TAB_NAME} {old}-abc123",
        "TAB-OLD-TYPING": f"{deploycheck.CHECK_TAB_NAME} {old}-abc123",
        "TAB-YOUNG": f"{deploycheck.LONG_TAB_NAME} {young}-abc123",
        "TAB-WORKER": f"Fix the {deploycheck.REWAKE_TAB_NAME} {old}-abc123",
        "TAB-UNDATED": f"{deploycheck.REWAKE_TAB_NAME} {old % 1_000_000:06d}",
    }
    agterm.close_timeouts = 1

    notes = deploycheck.close_stale_check_tabs(Config(), now)

    assert set(agterm.tabs) == {"TAB-YOUNG", "TAB-WORKER", "TAB-UNDATED"}
    assert len(notes) == 2 and all(note.startswith("closed the leftover check tab") for note in notes)


def test_a_deploy_check_closes_a_leftover_tab_and_its_own_when_killed(bridge_env: BridgeEnv) -> None:
    old = int(time.time() - deploycheck.CHECK_TAB_STALE_S - 60)
    leftover = {"id": "TAB-OLD", "name": f"{deploycheck.REWAKE_TAB_NAME} {old}-abc123", "parent": "TAB-LEAD"}
    tree = {"ok": True, "result": {"tree": {"workspaces": [{"name": "w", "sessions": [leftover]}]}}}
    bridge_env.stub_tree.write_text(json.dumps(tree))
    stub = bridge_env.root / "agtermctl"
    watched = bridge_env.root / "watched"
    # The check reads its tab's screen only once it holds the tab's id: then a kill must close that tab.
    stub.write_text(
        stub.read_text().replace("#!/bin/sh\n", f'#!/bin/sh\n[ "$1 $2" = "session text" ] && touch "{watched}"\n', 1)
    )
    check = subprocess.Popen([str(VENV_BIN / "bridge"), "deploy-check"], env=bridge_env.env)
    try:
        deadline = time.monotonic() + 20
        while not watched.exists():
            assert time.monotonic() < deadline, "the check opened no tab"
            time.sleep(0.1)
        check.send_signal(signal.SIGTERM)
        assert check.wait(timeout=20) != 0
    finally:
        check.kill()
        check.wait()

    log = bridge_env.stub_log.read_text()
    assert "session close --target TAB-OLD" in log
    assert "session close --target TAB-W" in log


@pytest.mark.parametrize(("signum", "ends_with"), [(signal.SIGTERM, SystemExit), (signal.SIGINT, KeyboardInterrupt)])
def test_a_kill_during_cleanup_waits_for_the_tab_to_close(
    agterm: FakeAgterm, monkeypatch: pytest.MonkeyPatch, signum: int, ends_with: type[BaseException]
) -> None:
    monkeypatch.setattr(deploycheck, "_type_and_see", _pass_typing)
    real = agterm.__call__

    def kill_on_close(config: Config, args: list[str]) -> str | None:
        if args[:2] == ["session", "close"]:
            signal.raise_signal(signum)
        return real(config, args)

    monkeypatch.setattr(deploycheck, "run_agterm", kill_on_close)
    previous = {held: signal.getsignal(held) for held in (signal.SIGTERM, signal.SIGHUP)}
    signal.signal(signal.SIGTERM, exit_on_signal)
    try:
        with pytest.raises(ends_with):
            deploycheck.check_typing(Config())
    finally:
        for held, handler in previous.items():
            signal.signal(held, handler)

    assert agterm.tabs == {}


def test_cleanup_restores_the_callers_signal_mask(agterm: FakeAgterm, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deploycheck, "_type_and_see", _pass_typing)
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGHUP})
    try:
        deploycheck.check_typing(Config())
        assert signal.SIGHUP in signal.pthread_sigmask(signal.SIG_BLOCK, set())
    finally:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGHUP})


def test_the_worker_cap_leaves_out_check_tabs(tmp_path: Path) -> None:
    conn = connect(tmp_path / "bridge.db")
    opened = int(time.time())
    sessions = [
        {"id": "TAB-W1", "name": "Task one", "status": "active", "parent": "TAB-LEAD"},
        *(
            {"id": f"TAB-C{n}", "name": f"{name} {opened}-abc123", "status": "active", "parent": "TAB-LEAD"}
            for n, name in enumerate(CHECK_NAMES)
        ),
    ]
    tree: dict[str, object] = {"workspaces": [{"name": "w", "sessions": sessions}]}

    assert [worker.name for worker in list_live_workers(conn, tree, "TAB-LEAD")] == ["Task one"]
