import json
import os
import signal
import time

from conftest import BridgeEnv, SpawnProcess, wait_for

from bridge.procinfo import process_start_ms


def test_sessions_discovers_running_claude_without_hooks(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    agterm = {"AGTERM_SESSION_ID": "TAB-1", "AGTERM_PANE_ID": "PANE-1", "AGTERM_WORKSPACE_ID": "WS"}
    first = spawn_process(env=agterm)
    second = spawn_process(env={**agterm, "AGTERM_PANE_ID": "PANE-2"})
    bridge_env.write_session_file(first, "conv-1", status="busy")
    bridge_env.write_session_file(second, "conv-2")
    bridge_env.stub_tree.write_text(json.dumps(_tree([("HQ", ["PANE-1"]), ("(P12) Data", ["PANE-2"])])))

    listed = bridge_env.cli("sessions", "--json")

    assert listed.returncode == 0, listed.stderr
    sessions = {s["pid"]: s for s in json.loads(listed.stdout)}
    assert {pid: (s["handle"], s["status"], s["conversation_id"]) for pid, s in sessions.items()} == {
        first: ("hq", "active", "conv-1"),
        second: ("p12-data", "idle", "conv-2"),
    }
    assert sessions[first]["cwd"] == str(bridge_env.root)


def test_dead_process_is_marked_ended(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pid = spawn_process()
    bridge_env.write_session_file(pid, "conv-d")
    assert [s["pid"] for s in json.loads(bridge_env.cli("sessions", "--json").stdout)] == [pid]

    os.kill(pid, signal.SIGKILL)
    assert wait_for(lambda: process_start_ms(pid) is None or _is_zombie(pid))

    assert json.loads(bridge_env.cli("sessions", "--json").stdout) == []
    ended = json.loads(bridge_env.cli("sessions", "--json", "--all").stdout)
    assert [(s["pid"], s["status"]) for s in ended] == [(pid, "ended")]


def test_duplicate_tab_names_get_unique_handles_and_explicit_handle_sticks(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    pids = [spawn_process(env={"AGTERM_PANE_ID": f"PANE-{i}"}) for i in range(3)]
    for i, pid in enumerate(pids):
        bridge_env.write_session_file(pid, f"conv-{i}")
    bridge_env.stub_tree.write_text(json.dumps(_tree([("Work", ["PANE-0", "PANE-1"]), ("Other", ["PANE-2"])])))
    bridge_env.cli("sessions")

    renamed = bridge_env.cli("handle", "captain", pid=pids[2])
    listed = json.loads(bridge_env.cli("sessions", "--json").stdout)

    assert renamed.returncode == 0, renamed.stderr
    assert sorted(s["handle"] for s in listed) == ["captain", "work", "work-2"]


def test_whoami_outside_claude(bridge_env: BridgeEnv) -> None:
    result = bridge_env.cli("whoami")
    assert result.returncode == 2
    assert "not inside a Claude Code session" in result.stderr


def test_focus_rejects_long_text(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pid = spawn_process()
    bridge_env.write_session_file(pid, "conv-f")
    result = bridge_env.cli("focus", "x" * 200, pid=pid)
    assert result.returncode == 2
    assert "keep it under 120" in result.stderr
    assert bridge_env.cli("focus", "short and sweet", pid=pid).returncode == 0
    who = json.loads(bridge_env.cli("whoami", "--json", pid=pid).stdout)
    assert (who["focus"], who["conversation_id"]) == ("short and sweet", "conv-f")


def _tree(tabs: list[tuple[str, list[str]]]) -> dict[str, object]:
    sessions = [{"name": name, "surfaces": [{"paneID": p} for p in panes]} for name, panes in tabs]
    return {"ok": True, "result": {"tree": {"workspaces": [{"name": "WS", "sessions": sessions}]}}}


def _is_zombie(pid: int) -> bool:
    return os.waitpid(pid, os.WNOHANG) != (0, 0)


def test_explicit_handle_cannot_be_stolen_and_auto_handle_follows_rename(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    first = spawn_process(env={"AGTERM_PANE_ID": "PANE-A"})
    second = spawn_process(env={"AGTERM_PANE_ID": "PANE-B"})
    bridge_env.write_session_file(first, "conv-a")
    bridge_env.write_session_file(second, "conv-b")
    bridge_env.stub_tree.write_text(json.dumps(_tree([("Alpha", ["PANE-A"]), ("Beta", ["PANE-B"])])))
    bridge_env.cli("sessions")
    assert bridge_env.cli("handle", "captain", pid=first).returncode == 0

    stolen = bridge_env.cli("handle", "captain", pid=second)
    bridge_env.stub_tree.write_text(json.dumps(_tree([("Alpha", ["PANE-A"]), ("Renamed", ["PANE-B"])])))
    listed = json.loads(bridge_env.cli("sessions", "--json").stdout)

    assert stolen.returncode == 2 and "belongs to tab 'Alpha'" in stolen.stderr
    assert sorted((s["handle"], s["tab_name"]) for s in listed) == [("captain", "Alpha"), ("renamed", "Renamed")]


def test_renamed_tab_keeps_its_old_handle_as_alias_and_its_claims(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    first = spawn_process(env={"AGTERM_PANE_ID": "PANE-A"})
    second = spawn_process(env={"AGTERM_PANE_ID": "PANE-B"})
    bridge_env.write_session_file(first, "conv-a")
    bridge_env.write_session_file(second, "conv-b")
    bridge_env.stub_tree.write_text(json.dumps(_tree([("Alpha", ["PANE-A"]), ("Beta", ["PANE-B"])])))
    bridge_env.cli("sessions")
    assert bridge_env.cli("claim", "env:staging", pid=second).returncode == 0

    bridge_env.stub_tree.write_text(json.dumps(_tree([("Alpha", ["PANE-A"]), ("Gamma", ["PANE-B"])])))
    bridge_env.cli("sessions")
    sent = bridge_env.cli("send", "beta", "still reachable?", pid=first)
    refused = bridge_env.cli("claim", "env:staging", pid=first)

    assert sent.returncode == 0, sent.stderr
    assert "still reachable?" in bridge_env.cli("inbox", pid=second).stdout
    assert refused.returncode == 3 and "held by gamma" in refused.stdout


def test_claim_written_with_a_stale_handle_shows_the_current_one(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    pid = spawn_process(env={"AGTERM_PANE_ID": "PANE-A"})
    bridge_env.write_session_file(pid, "conv-a")
    bridge_env.stub_tree.write_text(json.dumps(_tree([("Alpha", ["PANE-A"])])))
    bridge_env.cli("sessions")
    assert bridge_env.cli("claim", "env:staging", pid=pid).returncode == 0
    conn = bridge_env.connect()
    conn.execute("UPDATE claims SET holder_handle = 'older'")
    conn.close()

    bridge_env.cli("sessions")

    assert bridge_env.cli("claims", pid=pid).stdout.startswith("env:staging held by alpha")


def test_focus_follows_conversation_after_missed_clear_hook(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pid = spawn_process()
    bridge_env.hook("UserPromptSubmit", {"session_id": "before-clear", "prompt": "x"}, pid)
    start = process_start_ms(pid)
    assert start is not None
    newer = {
        "pid": pid, "sessionId": "after-clear", "cwd": str(bridge_env.root), "startedAt": start,
        "kind": "interactive", "status": "idle", "statusUpdatedAt": int(time.time() * 1000) + 1000,
    }  # fmt: skip
    (bridge_env.sessions_dir / f"{pid}.json").write_text(json.dumps(newer))

    assert bridge_env.cli("focus", "new task", pid=pid).returncode == 0
    with bridge_env.connect() as conn:
        focus = dict(conn.execute("SELECT conversation_id, focus FROM conversations").fetchall())
    assert focus == {"before-clear": None, "after-clear": "new task"}


def test_sessions_backfill_last_turn_from_transcript(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pid = spawn_process()
    bridge_env.write_session_file(pid, "conv-t")
    project = bridge_env.root / "projects" / "-tmp-x"
    project.mkdir(parents=True)
    entries = [
        {"type": "user", "message": {"role": "user", "content": "fix the flaky test"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Looking."}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
        {"type": "user", "message": {"content": "<task-notification>done</task-notification>"}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Fixed and pushed."}]}},
    ]
    (project / "conv-t.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    [session] = json.loads(bridge_env.cli("sessions", "--json").stdout)

    assert (session["last_prompt"], session["last_reply"]) == ("fix the flaky test", "Fixed and pushed.")


def test_numbered_tab_renamed_to_plain_name_takes_the_plain_handle(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    pid = spawn_process(env={"AGTERM_PANE_ID": "PANE-A"})
    bridge_env.write_session_file(pid, "conv-a")
    bridge_env.stub_tree.write_text(json.dumps(_tree([("Work 1", ["PANE-A"])])))
    bridge_env.cli("sessions")

    bridge_env.stub_tree.write_text(json.dumps(_tree([("Work", ["PANE-A"])])))
    listed = json.loads(bridge_env.cli("sessions", "--json").stdout)

    assert [s["handle"] for s in listed] == ["work"]


def test_new_session_renames_its_tab_once_and_a_later_rename_by_hand_stays(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    first = spawn_process(env={"AGTERM_PANE_ID": "PANE-A"})
    second = spawn_process(env={"AGTERM_PANE_ID": "PANE-B"})
    bridge_env.write_session_file(first, "conv-a")
    bridge_env.write_session_file(second, "conv-b")
    projects = bridge_env.root / "projects" / "-tmp"
    projects.mkdir(parents=True)
    for conv, title in (("conv-a", "Fix flaky test"), ("conv-b", "Review export")):
        entries = [{"type": "ai-title", "aiTitle": "Draft"}, {"type": "ai-title", "aiTitle": title}]
        (projects / f"{conv}.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    _write_tabs(bridge_env, [("S-A", "✳ Fix flaky test", "PANE-A"), ("S-B", "Old work", "PANE-B")])

    bridge_env.cli("sessions")
    _write_tabs(bridge_env, [("S-A", "Fix flaky test", "PANE-A"), ("S-B", "My own name", "PANE-B")])
    listed = json.loads(bridge_env.cli("sessions", "--json").stdout)

    assert bridge_env.stub_log.read_text().splitlines() == [
        "session rename Fix flaky test --target S-A",
        "session rename Review export --target S-B",
    ]
    assert sorted((s["handle"], s["tab_name"]) for s in listed) == [
        ("fix-flaky-test", "Fix flaky test"),
        ("my-own-name", "My own name"),
    ]


def _write_tabs(bridge_env: BridgeEnv, tabs: list[tuple[str, str, str]]) -> None:
    sessions = [{"id": sid, "name": name, "title": "✳ x", "surfaces": [{"paneID": pane}]} for sid, name, pane in tabs]
    bridge_env.stub_tree.write_text(
        json.dumps({"ok": True, "result": {"tree": {"workspaces": [{"name": "WS", "sessions": sessions}]}}})
    )


def test_locked_tab_is_not_listed_and_not_renamed(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pid = spawn_process(env={"AGTERM_PANE_ID": "PANE-A"})
    bridge_env.write_session_file(pid, "conv-a")
    projects = bridge_env.root / "projects" / "-tmp"
    projects.mkdir(parents=True)
    (projects / "conv-a.jsonl").write_text(json.dumps({"type": "ai-title", "aiTitle": "Private"}) + "\n")
    _write_tabs(bridge_env, [("S-A", "mine 🔒", "PANE-A")])

    listed = json.loads(bridge_env.cli("sessions", "--json").stdout)

    assert listed == []
    assert not bridge_env.stub_log.exists()
