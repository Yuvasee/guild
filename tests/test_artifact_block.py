import json
import subprocess
from itertools import pairwise
from pathlib import Path

from conftest import BridgeEnv, SpawnProcess
from test_leads import Team

from bridge.install import convert_event_to_command, list_guard_globs

PUBLISH = {"action": "publish", "file_path": "/tmp/page.html"}


def decision(done: subprocess.CompletedProcess[str]) -> str:
    out: dict[str, dict[str, str]] = json.loads(done.stdout) if done.stdout.strip() else {}
    return out.get("hookSpecificOutput", {}).get("permissionDecision", "allow")


def publish(bridge_env: BridgeEnv, pid: int, conv: str, pane: str) -> subprocess.CompletedProcess[str]:
    payload = {"session_id": conv, "cwd": str(bridge_env.root), "tool_name": "Artifact", "tool_input": PUBLISH}
    return bridge_env.hook("PreToolUse", payload, pid, {"AGTERM_PANE_ID": pane})


def test_a_lead_and_its_worker_publish_locally_and_another_session_still_may(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    standalone = publish(bridge_env, team.b, "conv-b", "PANE-B")
    assert decision(standalone) == "allow", "a session under no lead (a personal one) keeps claude.ai pages"

    assert team.enroll().returncode == 0
    lead = publish(bridge_env, team.a, "conv-a", "PANE-A")
    with bridge_env.connect() as db:
        db.execute("INSERT INTO tab_parents (agterm_session, parent_session) VALUES ('TAB-B', 'TAB-A')")
    worker = publish(bridge_env, team.b, "conv-b", "PANE-B")

    assert decision(lead) == "deny" and decision(worker) == "deny"
    assert "bridge page add <slug>" in lead.stdout and "127.0.0.1:8765/p/<slug>" in lead.stdout


def test_the_shell_gate_hands_a_publish_to_python(tmp_path: Path) -> None:
    hook = tmp_path / "hook"
    hook.write_text("#!/bin/sh\ncat > /dev/null; echo started\n")
    hook.chmod(0o755)
    gate = convert_event_to_command(str(hook), "PreToolUse")
    publish = {"tool_name": "Artifact", "tool_input": PUBLISH}
    mention = {"tool_name": "Bash", "tool_input": {"command": "rg 'tool_name.*Artifact' src"}}
    for text, started in (
        (json.dumps(publish), True), (json.dumps(publish, separators=(",", ":")), True),
        (json.dumps({"tool_name": "Read"}), False), (json.dumps(mention), False),
    ):  # fmt: skip
        done = subprocess.run(
            ["sh", "-c", gate], input=text, capture_output=True, text=True,
            env={"BRIDGE_STATE_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
        )  # fmt: skip
        assert (done.stdout.strip() == "started") is started, text
    assert not [glob for glob in list_guard_globs() if "Artifact" in glob], "gbridge sends Artifact to the Mac itself"


def test_a_worker_nested_six_tabs_under_its_lead_is_refused_and_a_tab_cycle_ends_the_walk(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    assert team.enroll().returncode == 0
    chain = ["TAB-B", "TAB-5", "TAB-4", "TAB-3", "TAB-2", "TAB-1", "TAB-A"]
    with bridge_env.connect() as db:
        db.execute("INSERT INTO tab_parents (agterm_session, parent_session) VALUES ('TAB-X', 'TAB-Y')")
        db.execute("INSERT INTO tab_parents (agterm_session, parent_session) VALUES ('TAB-Y', 'TAB-X')")
        db.execute("INSERT INTO tab_parents (agterm_session, parent_session) VALUES ('TAB-B', 'TAB-X')")
    assert decision(publish(bridge_env, team.b, "conv-b", "PANE-B")) == "allow", "a cycle above no lead ends"

    with bridge_env.connect() as db:
        db.executemany(
            "INSERT OR REPLACE INTO tab_parents (agterm_session, parent_session) VALUES (?, ?)", pairwise(chain)
        )
    assert decision(publish(bridge_env, team.b, "conv-b", "PANE-B")) == "deny"


def test_a_lead_sharing_its_tab_with_another_pane_still_holds_its_worker(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    assert team.enroll().returncode == 0
    with bridge_env.connect() as db:
        for pane in ("PANE-0", "PANE-1", "PANE-2"):
            db.execute(
                "INSERT INTO surfaces (surface, agterm_session, tab_name, updated_at) VALUES (?, 'TAB-A', 'Old A', 0)",
                (pane,),
            )
        # Last in the table, so a lookup that reads one pane of the tab meets another first.
        db.execute("UPDATE surfaces SET rowid = (SELECT max(rowid) + 1 FROM surfaces) WHERE surface = 'PANE-A'")
        db.execute("INSERT INTO tab_parents (agterm_session, parent_session) VALUES ('TAB-B', 'TAB-A')")

    assert decision(publish(bridge_env, team.b, "conv-b", "PANE-B")) == "deny"


def test_a_session_in_a_locked_workspace_is_personal_and_keeps_claude_ai_pages(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    assert team.enroll().returncode == 0
    team.write_tree(workspace_b="\U0001f512 Private")
    bridge_env.cli("sessions")
    with bridge_env.connect() as db:
        db.execute("INSERT OR REPLACE INTO tab_parents (agterm_session, parent_session) VALUES ('TAB-B', 'TAB-A')")

    assert decision(publish(bridge_env, team.b, "conv-b", "PANE-B")) == "allow"
    assert decision(publish(bridge_env, team.a, "conv-a", "PANE-A")) == "deny", "only the locked tab is personal"


def test_a_worker_in_a_pane_of_its_leads_own_tab_is_refused(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    assert team.enroll().returncode == 0
    with bridge_env.connect() as db:
        db.execute("UPDATE surfaces SET agterm_session = 'TAB-A' WHERE surface = 'PANE-B'")

    assert decision(publish(bridge_env, team.b, "conv-b", "PANE-B")) == "deny"


def test_the_assistants_worker_is_refused_like_a_leads(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    assert bridge_env.cli("handle", "aide", pid=team.a).returncode == 0
    assert decision(publish(bridge_env, team.b, "conv-b", "PANE-B")) == "allow"

    with bridge_env.connect() as db:
        db.execute("INSERT INTO tab_parents (agterm_session, parent_session) VALUES ('TAB-B', 'TAB-A')")

    assert decision(publish(bridge_env, team.a, "conv-a", "PANE-A")) == "deny"
    assert decision(publish(bridge_env, team.b, "conv-b", "PANE-B")) == "deny"


def test_a_claude_nested_in_a_leads_pane_is_refused(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    assert team.enroll().returncode == 0
    agterm = {"AGTERM_SESSION_ID": "TAB-A", "AGTERM_PANE_ID": "PANE-A"}
    nested = spawn_process(env={**agterm, "CLAUDECODE": "1", "CLAUDE_PID": "1"})
    bridge_env.write_session_file(nested, "conv-n")
    bridge_env.hook("SessionStart", {"session_id": "conv-n", "source": "startup"}, nested, agterm)
    with bridge_env.connect() as db:
        row = db.execute("SELECT surface FROM instances WHERE conversation_id = 'conv-n'").fetchone()
    assert row is not None and row["surface"] is None, "a nested claude owns no pane"

    assert decision(publish(bridge_env, nested, "conv-n", "PANE-A")) == "deny"


def test_a_lead_may_read_and_list_artifacts_but_not_publish_or_delete(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    assert team.enroll().returncode == 0

    def call(tool_input: dict[str, str]) -> str:
        payload = {
            "session_id": "conv-a",
            "cwd": str(bridge_env.root),
            "tool_name": "Artifact",
            "tool_input": tool_input,
        }
        return decision(bridge_env.hook("PreToolUse", payload, team.a, {"AGTERM_PANE_ID": "PANE-A"}))

    url = "https://claude.ai/artifact/abc"
    assert [call({"action": "read", "url": url}), call({"action": "list"})] == ["allow", "allow"]
    assert call({"file_path": "/tmp/page.html"}) == "deny", "no action is a publish"
    assert [call({"action": "publish", "file_path": "/tmp/p.html"}), call({"action": "delete", "url": url})] == [
        "deny",
        "deny",
    ]
