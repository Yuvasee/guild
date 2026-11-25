import json
import subprocess
from pathlib import Path

from conftest import BridgeEnv, SpawnProcess
from test_artifact_block import decision
from test_leads import Team

from bridge.install import convert_event_to_command, install_hooks

REFUSAL = (
    "Leads and the assistant route and review: give this to a worker (guild worker). Allowed here: bridge, agterm, pod."
)


def load(bridge_env: BridgeEnv, pid: int, conv: str, skill: str) -> subprocess.CompletedProcess[str]:
    payload = {"session_id": conv, "cwd": str(bridge_env.root), "tool_name": "Skill", "tool_input": {"skill": skill}}
    return bridge_env.hook("PreToolUse", payload, pid, {"AGTERM_PANE_ID": "PANE-A" if conv == "conv-a" else "PANE-B"})


def reason(done: subprocess.CompletedProcess[str]) -> str:
    return json.loads(done.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


def test_a_lead_loads_only_routing_skills_and_its_worker_any(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    assert decision(load(bridge_env, team.a, "conv-a", "simplify")) == "allow", "no lead yet"
    assert team.enroll().returncode == 0
    with bridge_env.connect() as db:
        db.execute("INSERT INTO tab_parents (agterm_session, parent_session) VALUES ('TAB-B', 'TAB-A')")

    refused = load(bridge_env, team.a, "conv-a", "simplify")
    assert decision(refused) == "deny" and reason(refused) == REFUSAL
    assert decision(load(bridge_env, team.a, "conv-a", "anthropic-skills:pdf")) == "deny"
    for skill in ("bridge", "plugin:bridge", "apps/web:agterm", "pod"):
        assert decision(load(bridge_env, team.a, "conv-a", skill)) == "allow", skill
    assert decision(load(bridge_env, team.b, "conv-b", "simplify")) == "allow", "a worker keeps every skill"

    with bridge_env.connect() as db:
        db.execute("UPDATE surfaces SET agterm_session = 'TAB-A' WHERE surface = 'PANE-B'")
    assert decision(load(bridge_env, team.b, "conv-b", "simplify")) == "allow", "a worker in a pane of its lead's tab"


def test_the_assistant_loads_only_routing_skills_even_in_a_locked_tab(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    assert bridge_env.cli("handle", "aide", pid=team.a).returncode == 0
    with bridge_env.connect() as db:
        db.execute("INSERT INTO tab_parents (agterm_session, parent_session) VALUES ('TAB-B', 'TAB-A')")

    assert reason(load(bridge_env, team.a, "conv-a", "simplify")) == REFUSAL
    assert decision(load(bridge_env, team.a, "conv-a", "bridge")) == "allow"
    assert decision(load(bridge_env, team.b, "conv-b", "simplify")) == "allow"

    team.write_tree(workspace_a="\U0001f512 Private")
    bridge_env.cli("sessions")
    assert decision(load(bridge_env, team.a, "conv-a", "simplify")) == "deny"


def test_the_allowed_list_comes_from_config(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    assert team.enroll().returncode == 0
    config = Path(bridge_env.env["BRIDGE_CONFIG"])
    config.write_text(config.read_text() + 'lead_skills = ["bridge", "simplify"]\n')

    assert decision(load(bridge_env, team.a, "conv-a", "simplify")) == "allow"
    assert reason(load(bridge_env, team.a, "conv-a", "pod")).endswith("Allowed here: bridge, simplify.")


def test_install_matches_the_skill_tool_and_the_shell_gate_hands_it_to_python(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    install_hooks(settings, "/home/u/.local/bin/bridge-hook")
    assert "Skill" in json.loads(settings.read_text())["hooks"]["PreToolUse"][0]["matcher"].split("|")

    hook = tmp_path / "hook"
    hook.write_text("#!/bin/sh\ncat > /dev/null; echo started\n")
    hook.chmod(0o755)
    gate = convert_event_to_command(str(hook), "PreToolUse")
    call = {"tool_name": "Skill", "tool_input": {"skill": "simplify"}}
    for text in (json.dumps(call), json.dumps(call, separators=(",", ":"))):
        done = subprocess.run(
            ["sh", "-c", gate], input=text, capture_output=True, text=True,
            env={"BRIDGE_STATE_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
        )  # fmt: skip
        assert done.stdout.strip() == "started", text
