import json
import os
import signal
import subprocess

from conftest import VENV_BIN, BridgeEnv, SpawnProcess, wait_for
from test_cli import _is_zombie  # pyright: ignore[reportPrivateUsage]
from test_leads import Team

from bridge.procinfo import process_start_ms


def run_guild(bridge_env: BridgeEnv, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(VENV_BIN / "guild"), *args], env=bridge_env.env, capture_output=True, text=True, timeout=60, check=False
    )


def write_tab(bridge_env: BridgeEnv, sid: str, name: str, *panes: str) -> None:
    session = {"id": sid, "name": name, "surfaces": [{"paneID": p} for p in panes]}
    workspaces = [{"id": "WS", "name": "Work", "sessions": [session]}]
    bridge_env.stub_tree.write_text(json.dumps({"ok": True, "result": {"tree": {"workspaces": workspaces}}}))


def handles(bridge_env: BridgeEnv) -> list[str]:
    return [s["handle"] for s in json.loads(bridge_env.cli("sessions", "--json").stdout)]


def test_sync_names_tints_moves_and_pins_right_hands_tab(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pid = spawn_process(env={"AGTERM_PANE_ID": "PANE-R", "AGTERM_SESSION_ID": "TAB-R"})
    bridge_env.write_session_file(pid, "conv-r")
    assert "no running Claude in an agterm tab has the handle aide" in run_guild(bridge_env, "assistant", "sync").stderr
    write_tab(bridge_env, "TAB-R", "aide", "PANE-R")
    bridge_env.cli("sessions")

    synced = run_guild(bridge_env, "assistant", "sync")
    write_tab(bridge_env, "TAB-R", "Aide · Right Hand", "PANE-R")

    assert synced.returncode == 0, synced.stderr
    log = bridge_env.stub_log.read_text().splitlines()
    assert {
        "workspace new \U0001f441 GUILD --json",
        "session move WS-NEW --target TAB-R",
        "session rename Aide · Right Hand --target TAB-R",
        "session tint yellow --on icon,name --target TAB-R",
    } <= set(log)
    assert handles(bridge_env) == ["aide"]


def test_right_hand_reopened_in_a_new_tab_takes_its_pinned_handle_back(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    old = spawn_process(env={"AGTERM_PANE_ID": "PANE-R", "AGTERM_SESSION_ID": "TAB-R"})
    bridge_env.write_session_file(old, "conv-r")
    write_tab(bridge_env, "TAB-R", "aide", "PANE-R")
    bridge_env.cli("sessions")
    assert run_guild(bridge_env, "assistant", "sync").returncode == 0
    os.kill(old, signal.SIGKILL)
    assert wait_for(lambda: process_start_ms(old) is None or _is_zombie(old))
    new = spawn_process(env={"AGTERM_PANE_ID": "PANE-N", "AGTERM_SESSION_ID": "TAB-N"})
    bridge_env.write_session_file(new, "conv-n")
    write_tab(bridge_env, "TAB-N", "aide", "PANE-N")

    before = handles(bridge_env)
    synced = run_guild(bridge_env, "assistant", "sync")

    assert (before, synced.returncode, handles(bridge_env)) == (["aide-2"], 0, ["aide"]), synced.stderr
    assert "session rename Aide · Right Hand --target TAB-N" in bridge_env.stub_log.read_text()


def test_a_second_claude_in_right_hands_tab_does_not_rename_it(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    right_hand = spawn_process(env={"AGTERM_PANE_ID": "PANE-R", "AGTERM_SESSION_ID": "TAB-R"})
    split = spawn_process(env={"AGTERM_PANE_ID": "PANE-S", "AGTERM_SESSION_ID": "TAB-R"})
    bridge_env.write_session_file(right_hand, "conv-r")
    bridge_env.write_session_file(split, "conv-s")
    write_tab(bridge_env, "TAB-R", "aide", "PANE-R", "PANE-S")
    bridge_env.cli("sessions")
    assert run_guild(bridge_env, "assistant", "sync").returncode == 0
    write_tab(bridge_env, "TAB-R", "Aide · Right Hand", "PANE-R", "PANE-S")
    projects = bridge_env.root / "projects" / "-tmp"
    projects.mkdir(parents=True)
    (projects / "conv-s.jsonl").write_text(json.dumps({"type": "ai-title", "aiTitle": "Side quest"}) + "\n")

    bridge_env.cli("sessions")

    assert "Side quest" not in bridge_env.stub_log.read_text()


def test_a_lead_cannot_take_right_hands_yellow(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)

    refused = team.enroll("Alex", "old-a", "--tint", "yellow")

    assert (refused.returncode, "tint must be one of blue, green, orange, purple, red, gray") == (
        2,
        refused.stderr.strip().removeprefix("guild: "),
    )
