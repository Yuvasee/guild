import json
import time

from conftest import BridgeEnv, SpawnProcess, http_request, wait_for

from bridge.tabinput import map_text_to_typable

TABS = {"a": ("PANE-A", "TAB-A"), "b": ("PANE-B", "TAB-B")}


def start(bridge_env: BridgeEnv, spawn_process: SpawnProcess, agterm_tabs: bool) -> dict[str, int]:
    """Two sessions with handles a and b; with agterm_tabs bridge can type into them (the stub shows them idle)."""
    pids: dict[str, int] = {}
    for handle, (pane, tab) in TABS.items():
        env = {"AGTERM_PANE_ID": pane, **({"AGTERM_SESSION_ID": tab} if agterm_tabs else {})}
        pids[handle] = spawn_process(env=env)
        bridge_env.write_session_file(pids[handle], f"conv-{handle}")
    sessions = [{"name": h.upper(), "surfaces": [{"paneID": pane}]} for h, (pane, _) in TABS.items()]
    bridge_env.stub_tree.write_text(
        json.dumps({"ok": True, "result": {"tree": {"workspaces": [{"name": "W", "sessions": sessions}]}}})
    )
    for handle, pid in pids.items():
        prompt(bridge_env, pid, f"conv-{handle}", "start", agterm_tabs)
    bridge_env.cli("sessions")
    return pids


def prompt(bridge_env: BridgeEnv, pid: int, conv: str, text: str, agterm_tab: bool = False) -> str:
    payload = {"session_id": conv, "cwd": str(bridge_env.root), "prompt": text, "prompt_id": f"{text}-{time.time_ns()}"}
    pane, tab = TABS[conv.removeprefix("conv-")]
    extra = {"AGTERM_PANE_ID": pane, **({"AGTERM_SESSION_ID": tab} if agterm_tab else {})}
    out = bridge_env.hook("UserPromptSubmit", payload, pid, extra_env=extra).stdout
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out.strip() else ""


def state(base: str) -> dict[str, object]:
    return json.loads(http_request(base + "/api/state"))


def test_decision_answer_is_typed_into_the_idle_tab(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    pids = start(bridge_env, spawn_process, agterm_tabs=True)
    posted = bridge_env.cli(
        "post", "decision", "Ship v2 behind a flag?", "--body", "Flag costs a day.",
        "--option", "Ship", "--option", "Wait", "--link", "PR=https://github.com/acme/web/pull/7",
        pid=pids["a"],
    )  # fmt: skip
    assert posted.returncode == 0 and "until the user answers" in posted.stdout, posted.stderr
    rejected = bridge_env.cli("post", "decision", "x", "--link", "file:///etc/passwd", pid=pids["a"])
    assert rejected.returncode != 0 and "not a web link" in rejected.stderr

    [item] = state(dashboard)["items"]  # type: ignore[misc]
    assert (item["kind"], item["options"], item["links"]) == (
        "decision",
        ["Ship", "Wait"],
        [{"label": "PR", "url": "https://github.com/acme/web/pull/7"}],
    )
    http_request(dashboard + f"/api/items/{item['id']}/answer", post=True, data={"answer": "Ship"})
    http_request(dashboard + f"/api/items/{item['id']}/answer", post=True, data={"answer": "Wait"}, expect=409)

    typed = '[bridge] user on the dashboard (#1): Answer to your decision "Ship v2 behind a flag?": Ship'
    assert wait_for(lambda: bridge_env.stub_log.exists() and typed in bridge_env.stub_log.read_text(), 15)
    assert "--target TAB-A --pane-id PANE-A" in bridge_env.stub_log.read_text()
    assert wait_for(lambda: prompt(bridge_env, pids["a"], "conv-a", "next", True) == "", 5), "typed means delivered"
    [answered] = state(dashboard)["items"]  # type: ignore[misc]
    assert answered["answer"] == "Ship" and answered["resolved_at"]


def test_busy_session_gets_the_users_message_at_its_next_hook(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    pids = start(bridge_env, spawn_process, agterm_tabs=False)
    http_request(dashboard + "/api/messages", post=True, data={"to": ["b"], "text": "Stop the deploy.\nWait for me."})
    context = prompt(bridge_env, pids["b"], "conv-b", "next")
    assert "From the user, written on the bridge dashboard" in context
    assert '#1 user: "Stop the deploy. Wait for me."' in context
    assert "not from the user" not in context

    replied = bridge_env.cli("reply", "1", "stopped", pid=pids["b"])
    assert replied.returncode == 0, replied.stderr
    reply = next(m for m in state(dashboard)["messages"] if m["id"] == 2)  # type: ignore[union-attr]
    assert reply["unread_for_user"] == 1
    http_request(dashboard + "/api/messages/2/read", post=True)
    reply = next(m for m in state(dashboard)["messages"] if m["id"] == 2)  # type: ignore[union-attr]
    assert reply["unread_for_user"] == 0


def test_board_posts_reach_mentioned_sessions_and_everyone_can_read(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    pids = start(bridge_env, spawn_process, agterm_tabs=False)
    posted = bridge_env.cli("board", "@b I am taking the staging deploy for ABC-7", "--topic", "staging", pid=pids["a"])
    assert posted.returncode == 0 and "delivered to: b" in posted.stdout, posted.stderr
    assert '#1 board post from a: "@b I am taking the staging deploy for ABC-7"' in prompt(
        bridge_env, pids["b"], "conv-b", "x"
    )
    lonely = bridge_env.cli("board", "lunch break", pid=pids["b"])
    assert "delivered to: nobody directly" in lonely.stdout
    assert "lunch break" not in prompt(bridge_env, pids["a"], "conv-a", "x"), "an unaddressed post is not pushed"

    sent = json.loads(
        http_request(dashboard + "/api/messages", post=True, data={"to": [], "text": "Freeze merges until 18:00"})
    )
    assert sent["detail"] == "team:2:0", "the user's post reaches the whole team"
    assert "Freeze merges until 18:00" in prompt(bridge_env, pids["b"], "conv-b", "y")
    read = bridge_env.cli("board", pid=pids["a"])
    assert "[staging]" in read.stdout and "lunch break" in read.stdout and "Freeze merges" in read.stdout
    messages = state(dashboard)["messages"]
    by_id = {m["id"]: m for m in messages}  # type: ignore[union-attr]
    assert by_id[3]["board"] == 1 and by_id[3]["from_user"] == 1 and by_id[1]["topic"] == "staging"


def test_typed_text_is_one_printable_line() -> None:
    assert map_text_to_typable("a\nb\x1b[201~\x15c\td") == "a b [201~ c d"


def test_a_long_message_from_the_user_is_cut_to_fit_and_blocks_nothing(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    pids = start(bridge_env, spawn_process, agterm_tabs=False)
    http_request(dashboard + "/api/messages", post=True, data={"to": ["b"], "text": "ünïcödé " * 120})
    bridge_env.cli("send", "b", "can I take staging?", "--request", pid=pids["a"])
    context = prompt(bridge_env, pids["b"], "conv-b", "next")
    assert "(full text: bridge show 1)" in context
    assert "REQUEST from a" in prompt(bridge_env, pids["b"], "conv-b", "again")


def test_malformed_dashboard_requests_are_refused(dashboard: str) -> None:
    http_request(dashboard + "/api/messages", post=True, data={"to": "b", "text": "x @a"}, expect=400)
    http_request(dashboard + "/api/messages", post=True, data={"to": [], "text": "x", "reply_to": "1"}, expect=400)
    http_request(dashboard + "/api/items/1/answer", post=True, data={"answer": 3}, expect=400)
    http_request(dashboard + "/api/items/99999999999999999999/answer", post=True, data={"answer": "x"}, expect=404)


def test_a_team_post_is_typed_only_where_it_concerns_the_session(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    pids = start(bridge_env, spawn_process, agterm_tabs=True)
    prompt(bridge_env, pids["a"], "conv-a", "review acme/web#100 please", True)
    sent = json.loads(http_request(dashboard + "/api/messages", post=True, data={"to": [], "text": "100 merged"}))
    assert sent["detail"] == "team:2:1"
    typed = "[bridge] user on the dashboard (#1): 100 merged"
    assert wait_for(lambda: bridge_env.stub_log.exists() and typed in bridge_env.stub_log.read_text(), 15)
    time.sleep(1)
    log = bridge_env.stub_log.read_text()
    assert "--target TAB-A" in log and "--target TAB-B" not in log
    assert "100 merged" in prompt(bridge_env, pids["b"], "conv-b", "next", True), "b reads it at its next turn"
