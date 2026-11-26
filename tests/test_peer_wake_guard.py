import json
import subprocess
from pathlib import Path

from conftest import BridgeEnv, SpawnProcess
from test_artifact_block import decision
from test_leads import Team

from bridge.hooks import PEER_WAKE_REFUSAL
from bridge.install import convert_event_to_command, install_hooks


def send_message(bridge_env: BridgeEnv, pid: int, to: str) -> subprocess.CompletedProcess[str]:
    payload = {
        "session_id": "conv-a",
        "cwd": str(bridge_env.root),
        "tool_name": "SendMessage",
        "tool_input": {"to": to, "message": "You have bridge messages: run `bridge inbox`"},
    }
    return bridge_env.hook("PreToolUse", payload, pid, {"AGTERM_PANE_ID": "PANE-A"})


def test_send_message_to_another_session_is_refused_and_other_targets_pass(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    with bridge_env.connect() as db:
        db.execute("UPDATE instances SET peer_name = 'bob' WHERE conversation_id = 'conv-b'")
        db.execute("UPDATE instances SET peer_name = 'amy' WHERE conversation_id = 'conv-a'")

    refused = send_message(bridge_env, team.a, "bob")
    assert decision(refused) == "deny"
    assert json.loads(refused.stdout)["hookSpecificOutput"]["permissionDecisionReason"] == PEER_WAKE_REFUSAL
    assert decision(send_message(bridge_env, team.a, "my-subagent")) == "allow"
    assert decision(send_message(bridge_env, team.a, "amy")) == "allow", "its own name is no other session"

    with bridge_env.connect() as db:
        db.execute("UPDATE instances SET ended_at = 1 WHERE conversation_id = 'conv-b'")
    assert decision(send_message(bridge_env, team.a, "bob")) == "allow", "a session that ended is no one to wake"


def test_install_matches_send_message_and_the_shell_gate_hands_it_to_python(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    install_hooks(settings, "/home/u/.local/bin/bridge-hook")
    assert "SendMessage" in json.loads(settings.read_text())["hooks"]["PreToolUse"][0]["matcher"].split("|")

    hook = tmp_path / "hook"
    hook.write_text("#!/bin/sh\ncat > /dev/null; echo started\n")
    hook.chmod(0o755)
    gate = convert_event_to_command(str(hook), "PreToolUse")
    call = {"tool_name": "SendMessage", "tool_input": {"to": "bob", "message": "hi"}}
    for text in (json.dumps(call), json.dumps(call, separators=(",", ":"))):
        done = subprocess.run(
            ["sh", "-c", gate], input=text, capture_output=True, text=True,
            env={"BRIDGE_STATE_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
        )  # fmt: skip
        assert done.stdout.strip() == "started", text
