import json
import subprocess

from conftest import BridgeEnv, SpawnProcess, http_request


def test_state_has_links_attention_and_counts(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    pid = spawn_process(env={"AGTERM_PANE_ID": "PANE-1", "AGTERM_SESSION_ID": "TAB-1"})
    bridge_env.write_session_file(pid, "conv-s")
    repo = bridge_env.root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "feat/abc-9"], cwd=repo, check=True)
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/web.git"], cwd=repo, check=True)
    payload = {"session_id": "conv-s", "cwd": str(repo), "prompt": "<b>fix</b> ABC-7 and PR #12", "prompt_id": "p"}
    bridge_env.hook("UserPromptSubmit", payload, pid)
    bridge_env.hook("Notification", {"session_id": "conv-s", "cwd": str(repo), "message": "allow?"}, pid)

    state = json.loads(http_request(dashboard + "/api/state"))

    [session] = state["sessions"]
    assert session["last_prompt"] == "<b>fix</b> ABC-7 and PR #12"
    assert session["attention"] == "permission"
    assert {link["url"] for link in session["links"]} == {
        "https://github.com/acme/web/tree/feat/abc-9",
        "https://github.com/acme/web/pull/12",
        "https://tracker.example/issue/ABC-7",
        "https://tracker.example/issue/ABC-9",
    }
    page = http_request(dashboard + "/dashboard")
    assert "<title>bridge</title>" in page and "innerHTML" not in page

    bridge_env.hook("Stop", {"session_id": "conv-s", "cwd": str(repo), "last_assistant_message": "done"}, pid)
    cleared = json.loads(http_request(dashboard + "/api/state"))["sessions"][0]
    assert (cleared["attention"], cleared["status"]) == (None, "idle")


def test_locked_tabs_and_workspaces_are_left_out(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    tabs = {"PANE-1": ("Work", "Visible"), "PANE-2": ("🔒 Private", "Inside"), "PANE-3": ("Work", "🔐 Mine")}
    workspaces: dict[str, list[dict[str, object]]] = {}
    for pane, (workspace, tab) in tabs.items():
        workspaces.setdefault(workspace, []).append({"name": tab, "surfaces": [{"paneID": pane}]})
    tree = [{"name": name, "sessions": sessions} for name, sessions in workspaces.items()]
    bridge_env.stub_tree.write_text(json.dumps({"ok": True, "result": {"tree": {"workspaces": tree}}}))
    pids: dict[str, int] = {}
    for n, pane in enumerate(tabs, start=1):
        pid = pids[pane] = spawn_process(env={"AGTERM_PANE_ID": pane})
        bridge_env.write_session_file(pid, f"conv-{n}")
        bridge_env.hook("UserPromptSubmit", {"session_id": f"conv-{n}", "prompt": "x", "prompt_id": "p"}, pid)
    assert bridge_env.cli("tell-user", "from a locked tab", pid=pids["PANE-2"]).returncode == 0
    bridge_env.cli("sessions")

    state = json.loads(http_request(dashboard + "/api/state"))

    assert [s["tab_name"] for s in state["sessions"]] == ["Visible"]
    assert state["items"] == []


def test_items_need_explicit_resolve_and_can_be_undone(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    pid = spawn_process()
    bridge_env.write_session_file(pid, "conv-i")
    bridge_env.hook("UserPromptSubmit", {"session_id": "conv-i", "prompt": "x"}, pid)
    bridge_env.cli("tell-user", "PR ready to merge", "--action", pid=pid)

    resolved = http_request(dashboard + "/api/items/1/resolve", post=True)
    again = http_request(dashboard + "/api/items/1/resolve", post=True, expect=409)
    after_resolve = json.loads(http_request(dashboard + "/api/state"))["items"]
    http_request(dashboard + "/api/items/1/unresolve", post=True)
    after_undo = json.loads(http_request(dashboard + "/api/state"))["items"]

    assert json.loads(resolved) == {"ok": True}
    assert json.loads(again) == {"ok": False}
    assert after_resolve[0]["resolved_at"] is not None, "handled items stay visible for undo"
    assert after_undo[0]["resolved_at"] is None


def test_jump_selects_the_agterm_tab(bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str) -> None:
    agterm = {"AGTERM_PANE_ID": "PANE-7", "AGTERM_SESSION_ID": "TAB-7"}
    pid = spawn_process(env=agterm)
    bridge_env.write_session_file(pid, "conv-j")
    bridge_env.hook("UserPromptSubmit", {"session_id": "conv-j", "prompt": "x"}, pid, agterm)

    jumped = json.loads(http_request(dashboard + "/api/sessions/PANE-7/jump", post=True))
    unknown = http_request(dashboard + "/api/sessions/NOPE/jump", post=True, expect=409)

    assert jumped["ok"] is True
    assert "session select --target TAB-7" in bridge_env.stub_log.read_text()
    assert json.loads(unknown)["ok"] is False


def test_foreign_host_origin_and_missing_header_are_refused(dashboard: str) -> None:
    port = dashboard.rsplit(":", 1)[1]
    http_request(dashboard + "/api/state", headers={"Host": f"evil.example:{port}"}, expect=403)
    http_request(dashboard + "/api/items/1/resolve", post=True, headers={"Origin": "http://evil.example"}, expect=403)
    http_request(dashboard + "/api/items/1/resolve", post=True, headers={"X-Bridge": ""}, expect=403)
