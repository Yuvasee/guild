import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast
from unittest import mock

from conftest import VENV_BIN, BridgeEnv, SpawnProcess, http_request, wait_for
from test_leads import Team

from bridge.activity import SessionActivity
from bridge.board import Party, flag_overdue, resolve_handle, send_message
from bridge.config import Config
from bridge.feed import (
    TYPING_CLAIM_BACKSTOP_MS,
    answer_post,
    claim_unsent_message,
    record_failed_delivery,
    release_dead_typing_claims,
    take_delivery_retries,
)
from bridge.guildview import map_db_to_guild, post_signals_to_leads
from bridge.identity import current_party
from bridge.procinfo import process_start_ms
from bridge.tabinput import type_when_idle
from bridge.watcher import BlockedTab, map_tree_to_blocked_tabs, notify_blocked_tabs

# The typer needs two idle looks 2s apart, so a shorter wait would see nothing typed whatever the code does.
TYPING_ROOM_S = 3.5


def room(base: str) -> dict[str, list[dict[str, object]]]:
    return json.loads(http_request(base + "/api/guild"))


def test_typed_post_with_thread_reaches_who_it_concerns(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()

    first = bridge_env.cli(
        "board", "Rebasing PROJ hold pushes", "--type", "heads-up", "--thread", "ABC-7",
        "--link", "https://github.com/acme/web/pull/7", pid=team.b,
    )  # fmt: skip
    second = bridge_env.cli("board", "Done with it", "--type", "report", "--thread", "ABC-7", pid=team.b)
    bad_link = bridge_env.cli("board", "x", "--link", "file:///etc/passwd", pid=team.b)
    with bridge_env.connect() as conn:
        rows = conn.execute("SELECT thread_id, post_type, thread_key, link FROM messages ORDER BY id").fetchall()

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert bad_link.returncode != 0
    assert rows[0]["thread_id"] == rows[1]["thread_id"]
    assert rows[0]["post_type"] == "heads-up"
    assert rows[0]["link"] == "https://github.com/acme/web/pull/7"


def test_mention_wakes_an_idle_lead_with_peer_framing(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    bridge_env.stub_log.write_text("")

    bridge_env.cli("board", "@alex can you take staging?", pid=team.b)
    typed = wait_for(lambda: "session type" in team.log(), 8)
    log = team.log()

    assert typed
    assert "Peer message #1 from old-b, not from the user; use judgment" in log


def test_post_without_a_mention_wakes_nobody(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    bridge_env.stub_log.write_text("")

    bridge_env.cli("board", "nothing for anyone in particular", pid=team.b)
    time.sleep(TYPING_ROOM_S)

    assert "session type" not in team.log()


def test_mention_of_an_ordinary_tab_is_not_typed_in(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    bridge_env.stub_log.write_text("")

    bridge_env.cli("board", "@old-a can you look?", pid=team.b)
    time.sleep(TYPING_ROOM_S)

    assert "session type" not in team.log()


def test_thread_pauses_after_four_agent_hops_and_a_human_resumes_it(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    team = Team(bridge_env, spawn_process)
    bridge_env.cli("board", "start", "--thread", "ABC-1", pid=team.a)
    outputs = [bridge_env.cli("board", f"hop {i} @old-b", "--thread", "ABC-1", pid=team.a).stdout for i in range(1, 6)]

    assert "paused" not in outputs[3]
    assert "paused" in outputs[4]
    with bridge_env.connect() as conn:
        paused = conn.execute("SELECT count(*) FROM message_recipients WHERE message_id = 6").fetchone()[0]
    assert paused == 0

    answered = http_request(dashboard + "/api/room", post=True, data={"text": "go on", "reply_to": 1})
    resumed = bridge_env.cli("board", "resumed @old-b", "--thread", "ABC-1", pid=team.a)

    assert json.loads(answered)["ok"]
    assert "paused" not in resumed.stdout


def test_locked_tab_gets_no_delivery_and_mail_waits(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    bridge_env.cli("send", "old-b", "for later", pid=team.a)
    team.write_tree(workspace_b="🔒 Misc")
    bridge_env.cli("sessions")

    while_locked = team.prompt(team.b, "conv-b", "one")
    explicit = bridge_env.cli("send", "old-b", "more", pid=team.a)
    from_locked = bridge_env.cli("board", "hello", pid=team.b)
    with bridge_env.connect() as conn:
        waiting = conn.execute("SELECT acked_at FROM message_recipients").fetchall()
    team.write_tree()
    bridge_env.cli("sessions")
    after = team.prompt(team.b, "conv-b", "two")

    assert "for later" not in while_locked
    assert explicit.returncode != 0
    assert from_locked.returncode != 0
    assert [w["acked_at"] for w in waiting] == [None]
    assert "for later" in after


def test_claim_is_one_line_that_changes_state(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    team = Team(bridge_env, spawn_process)
    taken = bridge_env.cli("claim", "env:staging", "--reason", "load test", "--offline", pid=team.a)
    token = taken.stdout.split("token")[1].split()[0].strip(":")
    held = room(dashboard)
    bridge_env.cli("release", "env:staging", "--token", token, pid=team.a)
    released = room(dashboard)

    def lines(state: dict[str, list[dict[str, object]]]) -> list[object]:
        return [i["state"] for i in state["room"] if i["kind"] == "claim"]

    assert lines(held) == ["held"]
    assert [c["resource"] for c in held["claims"]] == ["env:staging"]
    assert lines(released) == ["released"]
    with bridge_env.connect() as conn:
        conn.execute("UPDATE claim_log SET taken_at = 1, ended_at = 1")
    assert [i for i in room(dashboard)["room"] if i["kind"] == "claim"] == []
    assert released["claims"] == []


def test_holder_of_an_expired_claim_is_asked_once(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    bridge_env.cli("claim", "env:staging", "--offline", pid=team.a)
    with bridge_env.connect() as conn:
        conn.execute("UPDATE claims SET expires_at = 1")

    asked = team.prompt(team.a, "conv-a", "one")
    quiet = team.prompt(team.a, "conv-a", "two")

    assert "env:staging is past its expiry" in asked
    assert "past its expiry" not in quiet


def test_needs_you_is_only_what_only_the_user_can_do(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    empty = room(dashboard)["needs"]
    bridge_env.cli("post", "done", "shipped it", pid=team.b)
    bridge_env.cli("post", "decision", "Ship?", "--option", "Yes", pid=team.b)
    no_context = bridge_env.cli("post", "decision", "Ship?", pid=team.a)
    bridge_env.cli("post", "decision", "Old one", pid=team.b)
    with bridge_env.connect() as conn:
        conn.execute("UPDATE user_items SET created_at = 1 WHERE text = 'Old one'")
    state = room(dashboard)
    assert no_context.returncode == 2
    assert "needs --body" in no_context.stderr
    lead = state["leads"][0]

    assert empty == []
    assert [(n["type"], n["text"]) for n in state["needs"]] == [("post", "Ship?")]
    assert lead["display_name"] == "Alex"


def test_room_post_from_the_user_is_typed_only_into_who_is_mentioned(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    bridge_env.stub_log.write_text("")

    plain = json.loads(http_request(dashboard + "/api/room", post=True, data={"text": "morning all"}))
    time.sleep(1)
    quiet = "session type" not in team.log()
    mention = json.loads(http_request(dashboard + "/api/room", post=True, data={"text": "@alex look at this"}))
    typed = wait_for(lambda: "look at this" in team.log(), 8)

    assert plain["ok"]
    assert quiet
    assert mention["detail"] == "alex"
    assert typed


def test_signal_reaches_the_owning_lead_once_with_no_page_open(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    signal: dict[str, object] = {
        "key": "ci:1", "kind": "CI red", "text": "web#7", "url": "https://x.example/7", "surface": "PANE-A",
    }  # fmt: skip
    other: dict[str, object] = {"key": "ci:2", "kind": "CI red", "text": "web#8", "url": "", "surface": "PANE-B"}
    config = Config()
    with bridge_env.connect() as conn:
        first = post_signals_to_leads(conn, config, [signal, other], 1000)
        second = post_signals_to_leads(conn, config, [signal, other], 2000)
        rows = conn.execute("SELECT from_handle, body, post_type, link FROM messages").fetchall()

    assert [r.handle for r in first] == ["alex"]
    assert second == []
    assert [tuple(r) for r in rows] == [("bridge", "web#7 (CI red)", "report", "https://x.example/7")]
    with bridge_env.connect() as conn:
        assert conn.execute("SELECT board FROM messages").fetchone()[0] == 0


def test_answer_relay_is_for_the_assistant_only(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    bridge_env.cli("post", "decision", "Ship?", pid=team.a)
    refused = team.guild("answer", "1", "yes", pid=team.b)
    assert bridge_env.cli("handle", "aide", pid=team.b).returncode == 0

    done = team.guild("answer", "1", "yes, ship", pid=team.b)
    delivered = team.prompt(team.a, "conv-a", "next")
    with bridge_env.connect() as conn:
        by = conn.execute("SELECT answered_by, answer FROM user_items").fetchone()

    assert refused.returncode == 2
    assert done.returncode == 0, done.stderr
    assert "Peer messages" in delivered
    assert "aide, relaying" in delivered
    assert tuple(by) == ("aide", "yes, ship")


def test_lapsed_claims_never_reach_the_page(bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str) -> None:
    team = Team(bridge_env, spawn_process)
    bridge_env.cli("claim", "env:staging", "--offline", pid=team.a)
    with bridge_env.connect() as conn:
        conn.execute("UPDATE claims SET expires_at = 1")

    assert room(dashboard)["claims"] == []


def test_page_opens_at_root_and_old_one_moves(dashboard: str) -> None:
    assert "<title>Guild</title>" in http_request(dashboard + "/")
    assert "<title>bridge</title>" in http_request(dashboard + "/dashboard")


def test_failed_delivery_of_a_user_answer_shows_and_is_retried(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    team = Team(bridge_env, spawn_process)
    bridge_env.cli("post", "decision", "Ship?", "--body", "why", pid=team.a)
    with bridge_env.connect() as conn:
        result = answer_post(conn, 1, "yes", "sam", 1000)
        record_failed_delivery(conn, result.message_id, "conv-a", 1000)

        needs = room(dashboard)["needs"]
        retries = take_delivery_retries(conn, 1000 + 700_000)
        again = take_delivery_retries(conn, 1000 + 700_001)
        conn.execute("UPDATE message_recipients SET acked_at = 5")
    after = room(dashboard)["needs"]

    assert [(n["type"], n["delivery_failed"]) for n in needs] == [("post", True)]
    assert [m for m, _ in retries] == [result.message_id]
    assert again == []
    assert after == []


SUGGESTION = "\u2500\u2500\n\u276f\u00a0park it\n\u2500\u2500\n  -- INSERT --"


def test_suggestion_in_the_box_does_not_block_typing_and_a_draft_stays(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    screens = {"text": SUGGESTION, "after": ""}

    def run(_: Config, args: list[str]) -> str | None:
        calls.append(args)
        if args[:2] == ["session", "type"] and args[-1] == ".":
            screens["text"] = screens["text"].replace("park it", screens["after"])
        if args[:2] == ["session", "type"] and args[-1] == "\x7f":
            screens["text"] = SUGGESTION
        if args[:2] == ["session", "type"] and args[-1] == "hello":
            screens["text"] = SUGGESTION.replace("park it", "hello")
        if args[:2] == ["session", "type"] and args[-1] == "\r":
            screens["text"] = SUGGESTION.replace("park it", "")
        return screens["text"]

    def attempt(after: str, wait_s: int) -> str:
        screens.update(text=SUGGESTION, after=after)
        calls.clear()
        with mock.patch("bridge.tabinput.run_agterm", run), mock.patch("bridge.tabinput.state_dir", lambda: tmp_path):
            return type_when_idle(Config(), "S", "P", "hello", lambda: True, wait_s=wait_s, may_probe_box=lambda: True)

    suggestion = attempt(".", 5)
    typed = [c[-1] for c in calls if c[:2] == ["session", "type"]]
    draft = attempt("park it.", 3)
    draft_typed = [c[-1] for c in calls if c[:2] == ["session", "type"]]

    assert suggestion == "typed"
    assert typed[:2] == [".", "\x7f"]
    assert typed[2:] == ["hello", "\r"]
    assert draft == "draft"
    assert draft_typed == [".", "\x7f"]


def test_probe_stops_touching_the_box_when_the_user_shows_up(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    screens = {"text": SUGGESTION}

    def run(_: Config, args: list[str]) -> str | None:
        calls.append(args)
        if args[:2] == ["session", "type"] and args[-1] == ".":
            screens["text"] = SUGGESTION.replace("park it", ".")
        if args[:2] == ["session", "type"] and args[-1] == "\x7f":
            screens["text"] = SUGGESTION.replace("park it", "typed by sam")
        return screens["text"]

    with mock.patch("bridge.tabinput.run_agterm", run), mock.patch("bridge.tabinput.state_dir", lambda: tmp_path):
        result = type_when_idle(Config(), "S", "P", "hello", lambda: True, wait_s=5, may_probe_box=lambda: True)

    assert result == "failed"
    assert [c[-1] for c in calls if c[:2] == ["session", "type"]] == [".", "\x7f"]


def test_reply_in_the_room_reaches_mentions_and_resumed_writers(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    team = Team(bridge_env, spawn_process)
    for i in range(6):
        bridge_env.cli("board", f"hop {i}", "--thread", "ABC-1", pid=team.a)
    done = json.loads(http_request(dashboard + "/api/room", post=True, data={"text": "@old-b go on", "reply_to": 1}))
    with bridge_env.connect() as conn:
        to = {r[0] for r in conn.execute("SELECT handle FROM message_recipients WHERE message_id = 7")}
        body = conn.execute("SELECT body FROM messages WHERE id = 7").fetchone()[0]

    assert done["ok"]
    assert to == {"old-a", "old-b"}
    assert "what was held back: bridge show 1" in body


def _blocked_tree(status: str = "blocked", workspace: str = "Work") -> dict[str, object]:
    tab = {"id": "W-1", "name": "Task", "parent": "TAB-A", "status": status}
    return {"workspaces": [{"name": workspace, "sessions": [tab]}]}


def test_blocked_tab_tells_its_parent_lead_and_repeats_until_clear(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    config = Config()
    blocked = map_tree_to_blocked_tabs(_blocked_tree(), config)
    locked = map_tree_to_blocked_tabs(_blocked_tree(workspace="🔒 Misc"), config)
    broken = map_tree_to_blocked_tabs({"workspaces": None}, config)
    with bridge_env.connect() as conn:
        early = notify_blocked_tabs(conn, config, blocked, {"W-1": "Allow this read?"}, 1_000_000)
        first = notify_blocked_tabs(conn, config, blocked, {"W-1": "Allow this read?"}, 1_121_000)
        quiet = notify_blocked_tabs(conn, config, blocked, {}, 1_200_000)
        again = notify_blocked_tabs(conn, config, blocked, {}, 1_800_000)
        cleared = notify_blocked_tabs(conn, config, [], {}, 1_900_000)
        body = conn.execute("SELECT body, post_type FROM messages ORDER BY id LIMIT 1").fetchone()
        left = conn.execute("SELECT count(*) FROM blocked_notices").fetchone()[0]

    assert early == []
    assert locked == []
    assert broken == []
    assert [r.handle for _, r in first] == ["alex"]
    assert quiet == []
    assert len(again) == 1
    assert cleared == []
    assert "Allow this read?" in body["body"]
    assert body["post_type"] == "blocked-tab"
    assert left == 0


def test_blocked_tab_without_a_parent_goes_to_the_assistant_and_is_typed_in(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    assert bridge_env.cli("handle", "helm", pid=team.b).returncode == 0
    tab = BlockedTab("W-1", "Task", None)
    config = Config(assistant_handle="helm")
    with bridge_env.connect() as conn:
        notify_blocked_tabs(conn, config, [tab], {}, 1_000_000)
        sent = notify_blocked_tabs(conn, config, [tab], {}, 1_200_000)
    bridge_env.stub_log.write_text("")
    bridge_env.cli("_deliver", str(sent[0][0]), "conv-b")

    assert [r.handle for _, r in sent] == ["helm"]
    assert "Peer message" in team.log()


def _nest_tab_b_under_tab_a(bridge_env: BridgeEnv) -> None:
    raw = json.loads(bridge_env.stub_tree.read_text())
    for workspace in raw["result"]["tree"]["workspaces"]:
        for tab in workspace["sessions"]:
            if tab["id"] == "TAB-B":
                tab["parent"] = "TAB-A"
    bridge_env.stub_tree.write_text(json.dumps(raw))


def _send_from_tab(bridge_env: BridgeEnv, tab: str, pid: int, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(bridge_env.env, CLAUDE_PID=str(pid), AGTERM_SESSION_ID=tab)
    return subprocess.run(
        [str(VENV_BIN / "bridge"), *args], env=env, capture_output=True, text=True, timeout=30, check=False
    )


def _send_from_tab_b(bridge_env: BridgeEnv, pid: int, *args: str) -> subprocess.CompletedProcess[str]:
    return _send_from_tab(bridge_env, "TAB-B", pid, *args)


def _queue_for_alex_without_typing(bridge_env: BridgeEnv, kind: str, body: str) -> int:
    with bridge_env.connect() as conn:
        alex = resolve_handle(conn, "alex")
        return send_message(conn, Party("conv-b", "old-b"), [alex], kind, body, int(time.time() * 1000)).message_id


def _break_tree(bridge_env: BridgeEnv) -> None:
    bridge_env.stub_tree.write_text("agterm is not answering")


def test_worker_message_is_typed_into_its_idle_lead_and_retried_quietly(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    _nest_tab_b_under_tab_a(bridge_env)
    bridge_env.stub_log.write_text("")

    sent = _send_from_tab_b(bridge_env, team.b, "send", "alex", "PR is up")

    assert sent.returncode == 0, sent.stderr
    assert "typed into its tab" in sent.stdout
    assert "SendMessage" not in sent.stdout
    wait_for(lambda: "Peer message" in team.log())
    with bridge_env.connect() as conn:
        message_id = conn.execute("SELECT max(id) FROM messages").fetchone()[0]
        conn.execute("UPDATE message_recipients SET acked_at = NULL WHERE message_id = ?", (message_id,))
        record_failed_delivery(conn, message_id, "conv-a", 1000)
        retries = take_delivery_retries(conn, 1000 + 700_000)
    assert [m for m, _ in retries] == [message_id]
    assert room(dashboard)["needs"] == []


def test_cancelled_message_is_neither_retried_nor_typed(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    message_id = _queue_for_alex_without_typing(bridge_env, "info", "withdrawn")
    with bridge_env.connect() as conn:
        conn.execute("UPDATE messages SET post_type = 'worker-report' WHERE id = ?", (message_id,))
        record_failed_delivery(conn, message_id, "conv-a", 1000)
        conn.execute("UPDATE messages SET cancelled_at = 2000 WHERE id = ?", (message_id,))
        retries = take_delivery_retries(conn, 1000 + 700_000)
    bridge_env.stub_log.write_text("")

    bridge_env.cli("_deliver", str(message_id), "conv-a")

    assert retries == []
    assert "Peer message" not in team.log()


def test_message_from_any_tab_is_typed_into_an_idle_lead_and_retried_quietly(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    bridge_env.stub_log.write_text("")

    sent = bridge_env.cli("send", "alex", "fyi", pid=team.b)

    assert "is a team lead" in sent.stdout
    assert "SendMessage" not in sent.stdout
    assert "cannot be woken" not in sent.stdout
    assert wait_for(lambda: "--target TAB-A" in team.log())
    with bridge_env.connect() as conn:
        assert conn.execute("SELECT post_type FROM messages").fetchone()[0] == "to-lead"
        conn.execute("UPDATE message_recipients SET acked_at = NULL")
        record_failed_delivery(conn, 1, "conv-a", 1000)
        retries = take_delivery_retries(conn, 1000 + 700_000)
    assert [m for m, _ in retries] == [1]
    assert room(dashboard)["needs"] == []


def test_lead_message_and_reply_to_another_lead_are_typed(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll("Alex", "old-a")
    team.enroll("Bob", "old-b")
    bridge_env.stub_log.write_text("")

    sent = _send_from_tab(bridge_env, "TAB-A", team.a, "send", "bob", "taking staging")
    assert "is a team lead" in sent.stdout
    assert wait_for(lambda: "--target TAB-B" in team.log())
    replied = _send_from_tab(bridge_env, "TAB-B", team.b, "reply", "1", "ok")

    assert "is a team lead" in replied.stdout
    assert wait_for(lambda: "--target TAB-A" in team.log())


def test_locked_lead_is_not_typed_into(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    bridge_env.cli("send", "old-b", "a question", pid=team.a)
    team.write_tree(workspace_a="🔒 Misc")
    bridge_env.cli("sessions")
    bridge_env.stub_log.write_text("")

    sent = bridge_env.cli("reply", "1", "an answer", pid=team.b)
    time.sleep(TYPING_ROOM_S)

    assert "is a team lead" not in sent.stdout
    assert "SendMessage" not in sent.stdout
    assert "cannot be woken" not in sent.stdout
    assert "session type" not in team.log()


def test_paused_reply_does_not_promise_a_lead_a_typed_message(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    bridge_env.cli("board", "a post", pid=team.a)
    with bridge_env.connect() as conn:
        conn.execute("UPDATE messages SET agent_hops = 4")
    bridge_env.stub_log.write_text("")

    sent = bridge_env.cli("reply", "1", "one hop too many", pid=team.b)
    time.sleep(TYPING_ROOM_S)

    assert sent.returncode == 0, sent.stderr
    assert "is a team lead" not in sent.stdout
    assert "session type" not in team.log()


def test_failed_typing_is_retried_and_then_delivered_once(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    message_id = _queue_for_alex_without_typing(bridge_env, "info", "the first attempt fails")
    fails = Path(bridge_env.env["STUB_TYPE_FAILS"])
    fails.write_text("")
    with bridge_env.connect() as conn:
        conn.execute("UPDATE messages SET post_type = 'to-lead' WHERE id = ?", (message_id,))
    bridge_env.stub_log.write_text("")

    bridge_env.cli("_deliver", str(message_id), "conv-a")

    with bridge_env.connect() as conn:
        failed = conn.execute("SELECT acked_at, offered_at, delivery_failed_at FROM message_recipients").fetchone()
        retries = take_delivery_retries(conn, int(failed["delivery_failed_at"]) + 700_000)
    assert failed["acked_at"] is None
    assert failed["offered_at"] is None
    assert [m for m, _ in retries] == [message_id]
    assert "Peer message" not in team.log()

    fails.unlink()
    bridge_env.cli("_deliver", str(message_id), "conv-a")

    assert team.log().count("Peer message") == 1
    with bridge_env.connect() as conn:
        assert conn.execute("SELECT acked_at FROM message_recipients").fetchone()[0] is not None


def test_mail_whose_typer_died_mid_claim_comes_by_the_next_hook(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    message_id = _queue_for_alex_without_typing(bridge_env, "info", "PROJ-77 moved to you")
    Path(bridge_env.env["STUB_TYPE_HANGS"]).write_text("")
    typer = subprocess.Popen(
        [str(VENV_BIN / "bridge"), "_deliver", str(message_id), "conv-a"],
        env=bridge_env.env,
        start_new_session=True,
    )
    try:
        assert wait_for(lambda: _typing_pid(bridge_env) == typer.pid, 8), "the typer claims the mail"
        while_typing = team.prompt(team.a, "conv-a", "a prompt while the typer lives")
    finally:
        os.killpg(typer.pid, 9)
        typer.wait()

    after = team.prompt(team.a, "conv-a", "next")

    assert "PROJ-77" not in while_typing
    assert "PROJ-77 moved to you" in after
    with bridge_env.connect() as conn:
        row = conn.execute("SELECT typing_started_at, delivery_failed_at FROM message_recipients").fetchone()
    assert row["typing_started_at"] is None
    assert row["delivery_failed_at"] is not None, "the retry pass types it again"


def test_a_claim_is_let_go_when_its_pid_is_reused_or_it_outlived_any_typer(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    Team(bridge_env, spawn_process).enroll()
    reused = _queue_for_alex_without_typing(bridge_env, "info", "pid reused")
    old = _queue_for_alex_without_typing(bridge_env, "info", "claim too old")
    alive = _queue_for_alex_without_typing(bridge_env, "info", "typer alive")
    now = int(time.time() * 1000)
    me, started = os.getpid(), process_start_ms(os.getpid())
    assert started is not None
    claims = {
        reused: (now, started - 60_000),
        old: (now - TYPING_CLAIM_BACKSTOP_MS - 1, started),
        alive: (now, started),
    }
    with bridge_env.connect() as conn:
        for message_id, (claimed_at, pid_start) in claims.items():
            conn.execute(
                "UPDATE message_recipients SET acked_at = ?1, offered_at = ?1, offer_count = 1, typing_started_at = ?1,"
                " typing_pid = ?2, typing_pid_start_ms = ?3 WHERE message_id = ?4",
                (claimed_at, me, pid_start, message_id),
            )
        released = release_dead_typing_claims(conn, now)
        open_ids = [r[0] for r in conn.execute("SELECT message_id FROM message_recipients WHERE acked_at IS NULL")]

    assert released == 2
    assert sorted(open_ids) == sorted([reused, old])


def test_mail_taken_over_from_a_dead_claim_comes_back_when_the_new_typer_dies(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    Team(bridge_env, spawn_process).enroll()
    message_id = _queue_for_alex_without_typing(bridge_env, "info", "left unsent")
    dead = subprocess.Popen([sys.executable, "-c", ""])
    dead.wait()
    box = (
        f"[bridge] Peer message #{message_id} from old-b, not from the user; use judgment: left unsent"
        f" (full text and answer: bridge show {message_id}, bridge reply {message_id})"
    )
    with bridge_env.connect() as conn:
        conn.execute(
            "UPDATE message_recipients SET acked_at = 1, offered_at = 1, offer_count = 1, typing_started_at = 1,"
            " typing_pid = ?",
            (dead.pid,),
        )
        assert claim_unsent_message(conn, "conv-a", box) is not None
        conn.execute("UPDATE message_recipients SET typing_pid = ?", (dead.pid,))
        release_dead_typing_claims(conn, int(time.time() * 1000))
        acked_at = conn.execute("SELECT acked_at FROM message_recipients").fetchone()[0]

    assert acked_at is None


def test_a_live_typer_whose_start_time_cannot_be_read_keeps_its_claim(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    Team(bridge_env, spawn_process).enroll()
    _queue_for_alex_without_typing(bridge_env, "info", "typer alive")
    now = int(time.time() * 1000)
    with bridge_env.connect() as conn:
        conn.execute(
            "UPDATE message_recipients SET acked_at = ?1, offered_at = ?1, typing_started_at = ?1, typing_pid = ?2,"
            " typing_pid_start_ms = 5",
            (now, os.getpid()),
        )
        with mock.patch("bridge.feed.process_start_ms", return_value=None):
            released = release_dead_typing_claims(conn, now)

    assert released == 0


def _typing_pid(bridge_env: BridgeEnv) -> int | None:
    with bridge_env.connect() as conn:
        return conn.execute("SELECT typing_pid FROM message_recipients").fetchone()[0]


def test_typed_request_stays_unanswered_and_can_still_go_overdue(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    bridge_env.stub_log.write_text("")

    sent = bridge_env.cli("send", "alex", "can you check?", "--request", "--respond-within", "1s", pid=team.b)
    assert "is a team lead" in sent.stdout
    assert wait_for(lambda: "Peer REQUEST #1" in team.log(), 8)
    log = team.log()
    assert "bridge decline 1" in log
    assert "overdue" in log or "respond within" in log

    with bridge_env.connect() as conn:
        row = conn.execute("SELECT acked_at, offered_at FROM message_recipients").fetchone()
        assert row["acked_at"] is None
        assert row["offered_at"] is not None
        assert flag_overdue(conn, int(time.time() * 1000) + 5000) == 1


def test_failed_typing_of_a_request_can_be_retried(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    message_id = _queue_for_alex_without_typing(bridge_env, "request", "the first attempt fails")
    fails = Path(bridge_env.env["STUB_TYPE_FAILS"])
    fails.write_text("")

    bridge_env.cli("_deliver", str(message_id), "conv-a")

    with bridge_env.connect() as conn:
        row = conn.execute("SELECT offered_at, offer_count FROM message_recipients").fetchone()
    assert row["offered_at"] is None
    assert row["offer_count"] == 0


def test_reply_to_a_status_line_is_still_a_message_on_the_page(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll("Alex", "old-a")
    team.enroll("Bob", "old-b")
    bridge_env.cli("board", "Restarting the service", "--type", "heads-up", pid=team.a)

    replied = bridge_env.cli("reply", "1", "Stop: the migration is unsafe", pid=team.b)

    assert "is a team lead" in replied.stdout
    with bridge_env.connect() as conn:
        assert conn.execute("SELECT post_type FROM messages WHERE id = 2").fetchone()[0] == "to-lead"
        state = map_db_to_guild(conn, Config(), int(time.time() * 1000), lambda ref: "", None)
    thread = next(i for i in cast(list[dict[str, Any]], state["room"]) if i["kind"] == "post")
    reply = thread["replies"][0]
    assert reply["level"] == "message"
    assert reply["post_type"] is None


def test_lead_replying_to_its_own_message_does_not_type_into_itself(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    bridge_env.cli("send", "old-b", "a question", pid=team.a)
    bridge_env.stub_log.write_text("")

    sent = bridge_env.cli("reply", "1", "and one more thing", pid=team.a)
    time.sleep(TYPING_ROOM_S)

    assert "is a team lead" not in sent.stdout
    assert "session type" not in team.log()


def test_lead_message_is_typed_into_its_idle_worker_and_retried_quietly(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    _nest_tab_b_under_tab_a(bridge_env)
    bridge_env.stub_log.write_text("")

    sent = _send_from_tab(bridge_env, "TAB-A", team.a, "send", "old-b", "use the other branch")

    assert sent.returncode == 0, sent.stderr
    assert "is your worker" in sent.stdout
    assert "SendMessage" not in sent.stdout
    assert wait_for(lambda: "Peer message" in team.log())
    assert "--target TAB-B" in team.log()
    with bridge_env.connect() as conn:
        message_id = conn.execute("SELECT max(id) FROM messages").fetchone()[0]
        conn.execute("UPDATE message_recipients SET acked_at = NULL WHERE message_id = ?", (message_id,))
        record_failed_delivery(conn, message_id, "conv-b", 1000)
        retries = take_delivery_retries(conn, 1000 + 700_000)
    assert [m for m, _ in retries] == [message_id]
    assert room(dashboard)["needs"] == []


def test_lead_reply_is_typed_into_its_idle_worker(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    _nest_tab_b_under_tab_a(bridge_env)
    _send_from_tab_b(bridge_env, team.b, "send", "alex", "which branch?")
    assert wait_for(lambda: "--target TAB-A" in team.log())
    bridge_env.stub_log.write_text("")

    sent = _send_from_tab(bridge_env, "TAB-A", team.a, "reply", "1", "the other one")

    assert sent.returncode == 0, sent.stderr
    assert "is your worker" in sent.stdout
    assert wait_for(lambda: "--target TAB-B" in team.log())


def test_lead_message_to_a_tab_that_is_not_its_worker_is_not_typed(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    bridge_env.stub_log.write_text("")

    sent = _send_from_tab(bridge_env, "TAB-A", team.a, "send", "old-b", "fyi")
    time.sleep(TYPING_ROOM_S)

    assert "is your worker" not in sent.stdout
    assert "session type" not in team.log()


def test_message_from_a_parent_that_is_not_a_lead_is_not_typed(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    _nest_tab_b_under_tab_a(bridge_env)
    bridge_env.stub_log.write_text("")

    sent = _send_from_tab(bridge_env, "TAB-A", team.a, "send", "old-b", "fyi")
    time.sleep(TYPING_ROOM_S)

    assert "is your worker" not in sent.stdout
    assert "session type" not in team.log()


def test_message_to_a_lead_needs_no_tree_read(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    _break_tree(bridge_env)
    bridge_env.stub_log.write_text("")

    sent = bridge_env.cli("send", "alex", "PR is up", pid=team.b)

    assert "typed into its tab" in sent.stdout
    assert wait_for(lambda: "--target TAB-A" in team.log())


def test_lead_message_wakes_its_worker_when_the_tree_read_fails(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    _nest_tab_b_under_tab_a(bridge_env)
    bridge_env.cli("sessions")
    _break_tree(bridge_env)
    bridge_env.stub_log.write_text("")

    sent = _send_from_tab(bridge_env, "TAB-A", team.a, "send", "old-b", "use the other branch")

    assert "is your worker" in sent.stdout
    assert wait_for(lambda: "--target TAB-B" in team.log())


def test_failed_tree_read_does_not_type_into_a_tab_that_is_not_the_lead_worker(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    bridge_env.cli("sessions")
    _break_tree(bridge_env)
    bridge_env.stub_log.write_text("")

    sent = _send_from_tab(bridge_env, "TAB-A", team.a, "send", "old-b", "fyi")
    time.sleep(TYPING_ROOM_S)

    assert "typed into its tab" not in sent.stdout
    assert "session type" not in team.log()


def test_tab_taken_out_from_under_its_lead_loses_the_stored_link(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    _nest_tab_b_under_tab_a(bridge_env)
    bridge_env.cli("sessions")
    team.write_tree(workspace_a="TEAM", tab_a="Alex · Data")
    bridge_env.cli("sessions")
    _break_tree(bridge_env)

    sent = _send_from_tab(bridge_env, "TAB-A", team.a, "send", "old-b", "fyi")

    assert "typed into its tab" not in sent.stdout


def test_live_read_that_disproves_the_link_outlasts_a_later_failed_read(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    _nest_tab_b_under_tab_a(bridge_env)
    bridge_env.cli("sessions")
    team.write_tree(workspace_a="TEAM", tab_a="Alex · Data")
    first = _send_from_tab(bridge_env, "TAB-A", team.a, "send", "old-b", "fyi")
    _break_tree(bridge_env)

    second = _send_from_tab(bridge_env, "TAB-A", team.a, "send", "old-b", "fyi again")

    assert "typed into its tab" not in first.stdout
    assert "typed into its tab" not in second.stdout


def _start_nested_agent(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, parent_pid: int, tab: str, pane: str
) -> int:
    agent = spawn_process(
        env={"AGTERM_PANE_ID": pane, "AGTERM_SESSION_ID": tab, "CLAUDE_PID": str(parent_pid)},
    )
    bridge_env.write_session_file(agent, "conv-nested")
    return agent


def test_nested_agent_in_a_lead_tab_does_not_wake_its_worker(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    _nest_tab_b_under_tab_a(bridge_env)
    agent = _start_nested_agent(bridge_env, spawn_process, team.a, "TAB-A", "PANE-A")
    bridge_env.stub_log.write_text("")

    sent = _send_from_tab(bridge_env, "TAB-A", agent, "send", "old-b", "fyi")
    time.sleep(TYPING_ROOM_S)

    assert sent.returncode == 0, sent.stderr
    assert "is your worker" not in sent.stdout
    assert "session type" not in team.log()


def test_nested_agent_in_a_tab_is_a_sender_like_any_other_for_a_lead(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    agent = _start_nested_agent(bridge_env, spawn_process, team.b, "TAB-B", "PANE-B")

    sent = _send_from_tab(bridge_env, "TAB-B", agent, "send", "alex", "fyi")

    assert sent.returncode == 0, sent.stderr
    assert "is a team lead" in sent.stdout


def test_agent_without_a_surface_does_not_read_the_tree_for_a_handle(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    agent = _start_nested_agent(bridge_env, spawn_process, team.a, "TAB-A", "PANE-A")
    env = dict(bridge_env.env, CLAUDE_PID=str(agent), AGTERM_SESSION_ID="TAB-A")

    with (
        mock.patch.dict(os.environ, env),
        mock.patch("bridge.identity.sync_tabs") as sync_tabs,
        bridge_env.connect() as conn,
    ):
        party = current_party(conn)

    sync_tabs.assert_not_called()
    assert party.handle == f"pid:{agent}"


def _add_tab_w(bridge_env: BridgeEnv, spawn_process: SpawnProcess, parent: str | None) -> None:
    worker = spawn_process(env={"AGTERM_PANE_ID": "PANE-W", "AGTERM_SESSION_ID": "TAB-W"})
    bridge_env.write_session_file(worker, "conv-w")
    payload = {"session_id": "conv-w", "cwd": str(bridge_env.root), "source": "startup"}
    bridge_env.hook("SessionStart", payload, worker, {"AGTERM_PANE_ID": "PANE-W", "AGTERM_SESSION_ID": "TAB-W"})
    raw = json.loads(bridge_env.stub_tree.read_text())
    tab = {"id": "TAB-W", "name": "Task", "parent": parent, "surfaces": [{"paneID": "PANE-W"}]}
    raw["result"]["tree"]["workspaces"][0]["sessions"].append(tab)
    bridge_env.stub_tree.write_text(json.dumps(raw))
    bridge_env.cli("sessions")


def _make_tab_b_the_assistant(bridge_env: BridgeEnv, pid: int) -> None:
    assert bridge_env.cli("handle", "aide", pid=pid).returncode == 0
    bridge_env.stub_log.write_text("")


def test_any_message_to_the_idle_assistant_is_typed_and_retried_quietly(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    team = Team(bridge_env, spawn_process)
    _make_tab_b_the_assistant(bridge_env, team.b)
    _break_tree(bridge_env)

    sent = bridge_env.cli("send", "aide", "look at this", pid=team.a)

    assert sent.returncode == 0, sent.stderr
    assert "is the assistant" in sent.stdout
    assert "SendMessage" not in sent.stdout
    assert "cannot be woken" not in sent.stdout
    assert wait_for(lambda: "Peer message" in team.log())
    assert "--target TAB-B" in team.log()
    with bridge_env.connect() as conn:
        message_id = conn.execute("SELECT max(id) FROM messages").fetchone()[0]
        conn.execute("UPDATE message_recipients SET acked_at = NULL WHERE message_id = ?", (message_id,))
        record_failed_delivery(conn, message_id, "conv-b", 1000)
        retries = take_delivery_retries(conn, 1000 + 700_000)
    assert [m for m, _ in retries] == [message_id]
    assert room(dashboard)["needs"] == []


def test_reply_to_the_idle_assistant_is_typed(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    _make_tab_b_the_assistant(bridge_env, team.b)
    bridge_env.cli("send", "old-a", "a question", pid=team.b)

    sent = bridge_env.cli("reply", "1", "an answer", pid=team.a)

    assert "is the assistant" in sent.stdout
    assert wait_for(lambda: "--target TAB-B" in team.log())


def test_message_to_the_assistant_and_a_worker_is_typed_into_both(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    _add_tab_w(bridge_env, spawn_process, "TAB-A")
    _make_tab_b_the_assistant(bridge_env, team.b)

    sent = _send_from_tab(bridge_env, "TAB-A", team.a, "send", "aide,task", "both of you")

    assert "is your worker" in sent.stdout
    assert "is the assistant" in sent.stdout
    assert wait_for(lambda: "--target TAB-B" in team.log() and "--target TAB-W" in team.log(), 8)


def test_message_to_the_assistant_and_another_tab_is_typed_only_into_the_assistant(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    _add_tab_w(bridge_env, spawn_process, None)
    _make_tab_b_the_assistant(bridge_env, team.b)

    sent = bridge_env.cli("send", "aide,task", "one of you", pid=team.a)
    assert wait_for(lambda: "--target TAB-B" in team.log())
    time.sleep(TYPING_ROOM_S)

    assert sent.stdout.count("is the assistant") == 1
    assert "--target TAB-W" not in team.log()


def test_locked_assistant_is_not_typed_into(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    _make_tab_b_the_assistant(bridge_env, team.b)
    bridge_env.cli("send", "old-a", "a question", pid=team.b)
    team.write_tree(workspace_b="🔒 Misc")
    bridge_env.cli("sessions")
    bridge_env.stub_log.write_text("")

    sent = bridge_env.cli("reply", "1", "an answer", pid=team.a)
    time.sleep(TYPING_ROOM_S)

    assert "is the assistant" not in sent.stdout
    assert "session type" not in team.log()


def test_locked_tab_does_not_wake_the_assistant(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    _make_tab_b_the_assistant(bridge_env, team.b)
    bridge_env.cli("send", "old-a", "a question", pid=team.b)
    team.write_tree(workspace_a="🔒 Misc")
    bridge_env.cli("sessions")
    bridge_env.stub_log.write_text("")

    sent = bridge_env.cli("reply", "1", "an answer", pid=team.a)
    time.sleep(TYPING_ROOM_S)

    assert "is the assistant" not in sent.stdout
    assert "SendMessage" not in sent.stdout
    assert "cannot be woken" not in sent.stdout
    assert "session type" not in team.log()


def test_request_a_hook_already_offered_is_not_typed_as_well(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    _queue_for_alex_without_typing(bridge_env, "request", "can you check?")
    assert "can you check?" in team.prompt(team.a, "conv-a", "working")
    bridge_env.stub_log.write_text("")

    bridge_env.cli("_deliver", "1", "conv-a")

    assert "Peer message" not in team.log()
    with bridge_env.connect() as conn:
        assert conn.execute("SELECT acked_at FROM message_recipients").fetchone()[0] is None


def test_paused_reply_does_not_promise_the_assistant_a_typed_message(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    _make_tab_b_the_assistant(bridge_env, team.b)
    bridge_env.cli("board", "a post", pid=team.b)
    with bridge_env.connect() as conn:
        conn.execute("UPDATE messages SET agent_hops = 4")

    sent = bridge_env.cli("reply", "1", "one hop too many", pid=team.a)
    time.sleep(TYPING_ROOM_S)

    assert sent.returncode == 0, sent.stderr
    assert "is the assistant" not in sent.stdout
    assert "session type" not in team.log()


def test_stored_link_survives_a_failed_reconcile(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    Team(bridge_env, spawn_process)
    _nest_tab_b_under_tab_a(bridge_env)
    bridge_env.cli("sessions")
    _break_tree(bridge_env)

    bridge_env.cli("sessions")

    with bridge_env.connect() as conn:
        rows = conn.execute("SELECT agterm_session, parent_session FROM tab_parents").fetchall()
    assert [tuple(r) for r in rows] == [("TAB-B", "TAB-A")]


def test_guild_worker_stores_the_parent_link_before_any_reconcile(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    with bridge_env.connect() as conn:
        conn.execute(
            "INSERT INTO leads (name, display_name, scope, linear_refs, tint, created_at) "
            "VALUES ('boris', 'Boris', 's', '[]', 'blue', 0)"
        )
    raw = json.loads(bridge_env.stub_tree.read_text())
    raw["result"]["tree"]["workspaces"][0]["sessions"].append({"id": "TAB-W", "name": "Task", "status": "active"})
    bridge_env.stub_tree.write_text(json.dumps(raw))
    env = dict(bridge_env.env, CLAUDE_PID=str(team.a), AGTERM_SESSION_ID="TAB-A")

    done = subprocess.run(
        [str(VENV_BIN / "guild"), "worker", "Task", "--prompt", "x"],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )  # fmt: skip

    assert done.returncode == 0, done.stderr
    with bridge_env.connect() as conn:
        rows = conn.execute("SELECT agterm_session, parent_session FROM tab_parents").fetchall()
    assert [tuple(r) for r in rows] == [("TAB-W", "TAB-A")]
    brief = next((bridge_env.state_dir / "worker-briefs").iterdir()).read_text()
    assert "starts with '🤖 Claude:'; in chat it never does" in brief
    assert "not Boris, Aide, worker, lead, Guild, bridge." in brief
    assert "through Aide" in brief and "never a sub-issue" in brief
    assert "No browser use" in brief
    assert "OBSERVED (it happened to a real user" in brief and "say who would hit it and when" in brief
    assert "review doc follows ~/.claude/skills/bridge/review-doc.md" in brief


def test_new_tab_first_message_carries_its_handle_not_its_pid(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    Team(bridge_env, spawn_process)
    worker = spawn_process(env={"AGTERM_PANE_ID": "PANE-W", "AGTERM_SESSION_ID": "TAB-W"})
    bridge_env.write_session_file(worker, "conv-w")
    payload = {"session_id": "conv-w", "cwd": str(bridge_env.root), "source": "startup"}
    bridge_env.hook("SessionStart", payload, worker, {"AGTERM_PANE_ID": "PANE-W", "AGTERM_SESSION_ID": "TAB-W"})
    raw = json.loads(bridge_env.stub_tree.read_text())
    tab = {"id": "TAB-W", "name": "Load Test", "surfaces": [{"paneID": "PANE-W"}]}
    raw["result"]["tree"]["workspaces"][0]["sessions"].append(tab)
    bridge_env.stub_tree.write_text(json.dumps(raw))

    sent = _send_from_tab(bridge_env, "TAB-W", worker, "send", "old-a", "first words")

    assert sent.returncode == 0, sent.stderr
    with bridge_env.connect() as conn:
        assert conn.execute("SELECT from_handle FROM messages").fetchone()[0] == "load-test"


def test_worker_tab_keeps_the_task_name_its_lead_gave_it(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    _nest_tab_b_under_tab_a(bridge_env)
    projects = bridge_env.root / "projects" / "-tmp"
    projects.mkdir(parents=True, exist_ok=True)
    (projects / "conv-b.jsonl").write_text(json.dumps({"type": "ai-title", "aiTitle": "Worker brief 0badcafe"}) + "\n")
    bridge_env.stub_log.write_text("")

    bridge_env.cli("sessions")

    assert "--target TAB-B" not in team.log()


def test_lead_row_leaves_out_answered_locked_and_cancelled_requests_and_stale_workers(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    bridge_env.cli("board", "Old report", "--type", "report", pid=team.a)
    bridge_env.cli("board", "Withdrawn report", "--type", "report", pid=team.a)
    bridge_env.cli("send", "old-b", "first?", "--request", pid=team.a)
    bridge_env.cli("send", "old-b", "second?", "--request", pid=team.a)
    _queue_for_alex_without_typing(bridge_env, "info", "unread one")
    now = int(time.time() * 1000)
    tree: dict[str, object] = {
        "workspaces": [
            {
                "name": "Alex · Data",
                "sessions": [
                    {"id": "W-1", "name": "Recent", "parent": "TAB-A", "status": "completed",
                     "statusChangedAt": (now - 60_000) / 1000},
                    {"id": "W-2", "name": "Old", "parent": "TAB-A", "status": "completed",
                     "statusChangedAt": (now - 3 * 3_600_000) / 1000},
                    {"id": "W-3", "name": "Stuck", "parent": "TAB-A", "status": "blocked",
                     "statusChangedAt": now / 1000},
                ],
            }
        ]
    }  # fmt: skip
    with bridge_env.connect() as conn:
        conn.execute("UPDATE messages SET cancelled_at = 1 WHERE body = 'Withdrawn report'")
        conn.execute(
            "UPDATE message_recipients SET acked_at = 1 WHERE message_id = "
            "(SELECT id FROM messages WHERE body = 'first?')"
        )
        activity = {team.a: SessionActivity("idle", (), None)}
        lead = cast(
            list[dict[str, Any]],
            map_db_to_guild(conn, Config(), now, lambda ref: "", tree, now, activity, now)["leads"],
        )[0]
        team.write_tree(workspace_b="🔒 Misc")
        bridge_env.cli("sessions")
        locked = cast(
            list[dict[str, Any]],
            map_db_to_guild(conn, Config(), now, lambda ref: "", tree, now, activity, now)["leads"],
        )[0]

    assert (lead["state"], lead["why"]) == ("waiting", "waiting on old-b")
    assert "last_report" not in lead and "unread" not in lead and "focus" not in lead
    assert "old-b" not in json.dumps(locked)
    workers = {w["task"]: w for w in lead["workers"]}
    assert "Old" not in workers, "a worker that rested over 2h is gone from the row"
    assert workers["Stuck"]["state"] == "waiting", "a dialog under 2 minutes is not stuck yet"


def test_worker_refuses_a_non_english_name(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    env = dict(bridge_env.env, CLAUDE_PID=str(team.a), AGTERM_SESSION_ID="TAB-A")
    done = subprocess.run(
        [str(VENV_BIN / "guild"), "worker", "Tâche", "--prompt", "x"],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )  # fmt: skip

    assert done.returncode == 2
    assert "English" in done.stderr


def test_lead_row_shows_declared_work_with_state_and_names_whom_it_waits_on(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    assert team.guild("work", "set", "Export worker", "ABC-7", "acme/web#9", pid=team.a).returncode == 0
    bridge_env.cli("focus", "Restoring the workspace", pid=team.a)
    bridge_env.cli("post", "decision", "Ship?", "--body", "why", pid=team.a)
    bridge_env.cli("send", "old-b", "can you check staging?", "--request", pid=team.a)
    now = int(time.time() * 1000)
    tree: dict[str, object] = {
        "workspaces": [
            {
                "name": "Alex · Data",
                "sessions": [
                    {"id": "TAB-A", "name": "Alex", "status": "completed"},
                    {"id": "W-1", "name": "Load test", "parent": "TAB-A", "status": "active"},
                    {"id": "W-2", "name": "Other", "parent": "TAB-B", "status": "active"},
                ],
            },
            {"name": "🔒 Misc", "sessions": [{"id": "W-3", "name": "Hidden", "parent": "TAB-A", "status": "active"}]},
        ]
    }
    with bridge_env.connect() as conn:
        conn.execute(
            "INSERT INTO enrichment (ref, data, fetched_at, ok_at) VALUES (?, ?, ?, ?)",
            ("pr:acme/web#9", json.dumps({"title": "Export", "state": "OPEN", "checks": "PENDING"}), now, now),
        )
        pid = team.a
        activity = {pid: SessionActivity("idle", (), None)}
        state = map_db_to_guild(
            conn, Config(user_alias="sam"), now, lambda ref: f"https://x/{ref}", tree, now, activity, now
        )
    lead = cast(list[dict[str, Any]], state["leads"])[0]

    assert lead["asks_you"] is True
    assert "focus" not in lead
    assert [c["name"] for c in lead["work"]] == ["Export worker"]
    links = lead["work"][0]["links"]
    assert [(k["kind"], k["label"], k["state"]) for k in links] == [
        ("issue", "ABC-7", None),
        ("pr", "web#9", "CI running"),
    ]
    assert links[1]["url"] == "https://x/pr:acme/web#9"
    assert [w["task"] for w in lead["workers"]] == ["Load test"], "hidden tabs and others' workers stay out"
    assert lead["workers"][0]["state"] == "unknown", "agterm says active but no process was found for the tab"
    assert lead["state"] == "waiting" and lead["why"] == "waiting for you"


def test_text_in_a_tabs_box_tells_the_assistant_once_and_is_never_cleared(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    from bridge.feed import tell_assistant_of_draft

    team = Team(bridge_env, spawn_process)
    team.enroll()
    with bridge_env.connect() as conn:
        sent = bridge_env.cli("send", "alex", "wake up", pid=team.b)
        assert sent.returncode == 0, sent.stderr
        message_id = int(conn.execute("SELECT max(id) FROM messages").fetchone()[0])
        config = Config(assistant_handle="old-b")
        told = tell_assistant_of_draft(conn, config, message_id, "conv-a", 1000)
        again = tell_assistant_of_draft(conn, config, message_id, "conv-a", 2000)
        body = conn.execute("SELECT body, post_type FROM messages WHERE id = ?", (told[0] if told else 0,)).fetchone()
    assert told is not None and told[1].handle == "old-b" and again is None
    assert body["post_type"] == "to-assistant"
    assert body["body"].startswith("alex's tab") and f"cannot deliver #{message_id} until it is cleared" in body["body"]
