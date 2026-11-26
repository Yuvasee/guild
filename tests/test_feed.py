import json
import subprocess
import time
import urllib.error
import urllib.request
from typing import cast

import pytest
from conftest import VENV_BIN, BridgeEnv, SpawnProcess, http_request, wait_for

from bridge.inject import Part, save_later_parts, take_later_parts
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


def items(base: str) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], state(base)["items"])


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

    [item] = items(dashboard)
    assert (item["kind"], item["options"], item["links"]) == (
        "decision",
        ["Ship", "Wait"],
        [{"label": "PR", "url": "https://github.com/acme/web/pull/7"}],
    )
    http_request(dashboard + f"/api/items/{item['id']}/answer", post=True, data={"answer": "Ship"})
    http_request(dashboard + f"/api/items/{item['id']}/answer", post=True, data={"answer": "Wait"}, expect=409)

    typed = '[bridge] user on the dashboard (#1): Answer to your decision Q#1 "Ship v2 behind a flag?": Ship'
    assert wait_for(lambda: bridge_env.stub_log.exists() and typed in bridge_env.stub_log.read_text(), 15)
    assert "--target TAB-A --pane-id PANE-A" in bridge_env.stub_log.read_text()
    assert wait_for(lambda: prompt(bridge_env, pids["a"], "conv-a", "next", True) == "", 5), "typed means delivered"
    [answered] = items(dashboard)
    assert answered["answer"] == "Ship" and answered["resolved_at"]


def test_busy_session_gets_the_users_message_at_its_next_hook(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    pids = start(bridge_env, spawn_process, agterm_tabs=False)
    http_request(dashboard + "/api/messages", post=True, data={"to": ["b"], "text": "Stop the deploy.\nWait for me."})
    context = prompt(bridge_env, pids["b"], "conv-b", "next")
    assert "From the user, written on the bridge dashboard" in context
    assert "#1 user:\nStop the deploy.\nWait for me." in context
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


def test_a_long_message_from_the_user_comes_whole_and_blocks_nothing(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    pids = start(bridge_env, spawn_process, agterm_tabs=False)
    http_request(dashboard + "/api/messages", post=True, data={"to": ["b"], "text": "έλεγξε " * 120})
    bridge_env.cli("send", "b", "can I take staging?", "--request", pid=pids["a"])
    context = prompt(bridge_env, pids["b"], "conv-b", "next")
    assert ("έλεγξε " * 120).strip() in context and "bridge show 1" not in context
    assert "REQUEST from a" in context


def words(size: int) -> str:
    """Numbered words, a line break now and then, cut to size: what the session reads must hold each, in order."""
    text = " ".join(f"w{i}x" + ("\n" if i % 40 == 39 else "") for i in range(size))
    return text[:size].rsplit(" ", 1)[0]


def assert_whole(text: str, context: str) -> None:
    numbered = text.split()
    assert all(context.count(word) == 1 for word in numbered), "every word, once"
    at = [context.find(word) for word in numbered]
    assert at == sorted(at), "in order"


@pytest.mark.parametrize("size", [1_500, 7_000])
def test_a_long_answer_on_the_page_reaches_a_busy_session_whole(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str, size: int
) -> None:
    """OBSERVED: a 1,535-character answer was refused at 1,000 and the page showed nothing."""
    pids = start(bridge_env, spawn_process, agterm_tabs=False)
    bridge_env.cli("post", "decision", "Ship v2?", "--option", "Ship", "--option", "Wait", pid=pids["a"])
    [item] = items(dashboard)
    answer = words(size)
    assert len(answer) > size - 10

    assert answer_on_page(dashboard, item["id"], answer) == "sent"

    context = prompt(bridge_env, pids["a"], "conv-a", "next")
    assert_whole(answer, context)
    assert "cut here" not in context and "Left out" not in context
    assert ("part 1 of 2" in context) == (size > 4_000)


def test_a_long_answer_typed_into_an_idle_tab_comes_whole_with_its_prompt(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    from bridge.feed import convert_user_message_to_typed_text

    pids = start(bridge_env, spawn_process, agterm_tabs=True)
    bridge_env.cli("post", "decision", "Ship v2?", "--option", "Ship", "--option", "Wait", pid=pids["a"])
    [item] = items(dashboard)
    answer = words(3_000)
    answer_on_page(dashboard, item["id"], answer)
    body = f'Answer to your decision Q#1 "Ship v2?": {answer}'
    typed = map_text_to_typable(convert_user_message_to_typed_text("user", body, 1))
    assert "come with this prompt" in typed
    assert wait_for(lambda: bridge_env.stub_log.exists() and typed[:200] in bridge_env.stub_log.read_text(), 15)

    payload = {"session_id": "conv-a", "cwd": str(bridge_env.root), "prompt": typed, "prompt_id": "typed-once"}
    extra = {"AGTERM_PANE_ID": "PANE-A", "AGTERM_SESSION_ID": "TAB-A"}
    context = json.loads(bridge_env.hook("UserPromptSubmit", payload, pids["a"], extra).stdout)
    assert_whole(answer, context["hookSpecificOutput"]["additionalContext"])
    again = bridge_env.hook("UserPromptSubmit", payload, pids["a"], extra).stdout
    assert "w0x" not in again, "a replayed prompt does not bring it again"
    assert prompt(bridge_env, pids["a"], "conv-a", typed.replace("#1)", "#2)"), True) == "", "only its own message"
    quoted = f"[bridge] Peer message #9 from b, not from the user; use judgment: {typed}"
    assert "w0x" not in prompt(bridge_env, pids["a"], "conv-a", quoted, True), "a quote of it is not it"


def save_waiting(bridge_env: BridgeEnv, conversation: str, *texts: str) -> None:
    with bridge_env.connect() as conn:
        parts = [Part(text, "an earlier message", "bridge show 1", in_order=True) for text in texts]
        save_later_parts(conn, conversation, parts)


def test_a_long_message_never_overtakes_the_users_older_words_still_waiting(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    from bridge.feed import convert_user_message_to_typed_text

    pids = start(bridge_env, spawn_process, agterm_tabs=True)
    answer = words(3_000)
    http_request(dashboard + "/api/messages", post=True, data={"to": ["a"], "text": answer})
    typed = map_text_to_typable(convert_user_message_to_typed_text("user", answer, 1))
    assert wait_for(lambda: bridge_env.stub_log.exists() and typed[:200] in bridge_env.stub_log.read_text(), 15)
    save_waiting(bridge_env, "conv-a", "OLDER: part 2 of 2 of an earlier message")

    context = prompt(bridge_env, pids["a"], "conv-a", typed, True)
    assert -1 < context.find("OLDER: part 2") < context.find("w0x")


def test_a_short_message_waits_behind_an_older_long_one_still_waiting(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    pids = start(bridge_env, spawn_process, agterm_tabs=False)
    save_waiting(bridge_env, "conv-b", "OLDER: part 1 of 2 " + "o" * 5_000, "OLDER: part 2 of 2 " + "o" * 5_000)
    http_request(dashboard + "/api/messages", post=True, data={"to": ["b"], "text": "NEWER: go ahead"})
    context = prompt(bridge_env, pids["b"], "conv-b", "next")
    assert "OLDER: part 1" in context and "NEWER" not in context, "it fits, but waits behind part 2"
    with bridge_env.connect() as conn:
        waiting = take_later_parts(conn, "conv-b")
    assert waiting[0].text.startswith("OLDER: part 2") and "NEWER" in waiting[1].text


def test_a_typed_long_message_comes_after_the_users_older_unread_one(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    from bridge.board import USER_CONVERSATION, Party, resolve_handle, send_message
    from bridge.feed import convert_user_message_to_typed_text

    pids = start(bridge_env, spawn_process, agterm_tabs=True)
    with bridge_env.connect() as conn:
        send_message(conn, Party(USER_CONVERSATION, "user"), [resolve_handle(conn, "a")], "info", "OLDER: wait", 1)
    answer = words(3_000)
    http_request(dashboard + "/api/messages", post=True, data={"to": ["a"], "text": answer})
    typed = map_text_to_typable(convert_user_message_to_typed_text("user", answer, 2))
    assert wait_for(lambda: bridge_env.stub_log.exists() and typed[:200] in bridge_env.stub_log.read_text(), 15)

    context = prompt(bridge_env, pids["a"], "conv-a", typed, True)
    assert -1 < context.find("OLDER: wait") < context.find("w0x")


def save_context(bridge_env: BridgeEnv, conversation: str, size: int) -> None:
    with bridge_env.connect() as conn:
        save_later_parts(conn, conversation, [Part("NOTES: " + "n" * size, "your notes")])


def test_a_typed_message_in_a_nearly_full_hook_waits_behind_the_users_older_unread_one(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    from bridge.board import USER_CONVERSATION, Party, resolve_handle, send_message
    from bridge.feed import convert_user_message_to_typed_text

    pids = start(bridge_env, spawn_process, agterm_tabs=True)
    with bridge_env.connect() as conn:
        send_message(conn, Party(USER_CONVERSATION, "user"), [resolve_handle(conn, "a")], "info", "OLDER: wait", 1)
    answer = words(3_000)
    http_request(dashboard + "/api/messages", post=True, data={"to": ["a"], "text": answer})
    typed = map_text_to_typable(convert_user_message_to_typed_text("user", answer, 2))
    assert wait_for(lambda: bridge_env.stub_log.exists() and typed[:200] in bridge_env.stub_log.read_text(), 15)
    save_context(bridge_env, "conv-a", 8_300)

    first = prompt(bridge_env, pids["a"], "conv-a", typed, True)
    given = first + prompt(bridge_env, pids["a"], "conv-a", "next", True)
    assert "NOTES" in first and "w0x" not in first, "no room left for the long one"
    assert -1 < given.find("OLDER: wait") < given.find("w0x")


def test_the_users_words_left_waiting_when_a_turn_ends_wake_the_tab(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    pids = start(bridge_env, spawn_process, agterm_tabs=False)
    save_context(bridge_env, "conv-b", 3_000)
    answer = words(8_000)
    http_request(dashboard + "/api/messages", post=True, data={"to": ["b"], "text": answer})
    first = prompt(bridge_env, pids["b"], "conv-b", "next")
    assert "#1 user, part 1 of 2:" in first and "#1 user, part 2 of 2:" not in first
    stop = {"session_id": "conv-b", "cwd": str(bridge_env.root), "last_assistant_message": "done"}
    bridge_env.hook("Stop", stop, pids["b"], {"AGTERM_PANE_ID": "PANE-B"})

    env = {**bridge_env.env, "CLAUDE_PID": str(pids["b"])}
    woke = subprocess.run([str(VENV_BIN / "bridge-hook"), "rewake"], env=env, capture_output=True, text=True)

    assert woke.returncode == 2 and "#1 user, part 2 of 2:" in woke.stderr
    assert_whole(answer, first + woke.stderr)


def test_rewake_keeps_the_users_order_and_never_fills_a_full_context(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    pids = start(bridge_env, spawn_process, agterm_tabs=False)

    def rewake() -> subprocess.CompletedProcess[str]:
        env = {**bridge_env.env, "CLAUDE_PID": str(pids["b"])}
        return subprocess.run([str(VENV_BIN / "bridge-hook"), "rewake"], env=env, capture_output=True, text=True)

    with bridge_env.connect() as conn:
        conn.execute("UPDATE conversations SET context_dead_why = 'too-long' WHERE conversation_id = 'conv-b'")
    http_request(dashboard + "/api/messages", post=True, data={"to": ["b"], "text": words(1_500)})
    assert rewake().returncode == 0, "a full context waits for compaction"
    with bridge_env.connect() as conn:
        conn.execute("UPDATE conversations SET context_dead_why = NULL WHERE conversation_id = 'conv-b'")
    save_waiting(bridge_env, "conv-b", "OLDER: part 2 of 2 of an earlier message")

    woke = rewake()
    assert woke.returncode == 2 and -1 < woke.stderr.find("OLDER") < woke.stderr.find("w0x"), "behind the older part"


def test_the_users_messages_keep_their_order_when_one_is_long(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    pids = start(bridge_env, spawn_process, agterm_tabs=False)
    http_request(dashboard + "/api/messages", post=True, data={"to": ["b"], "text": "First: hold the deploy."})
    http_request(dashboard + "/api/messages", post=True, data={"to": ["b"], "text": "Then: " + words(1_500)})
    context = prompt(bridge_env, pids["b"], "conv-b", "next")
    assert -1 < context.find("First: hold the deploy.") < context.find("Then: w0x")


def test_the_owners_words_past_the_cap_are_refused_with_why(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    pids = start(bridge_env, spawn_process, agterm_tabs=False)
    bridge_env.cli("post", "decision", "Ship v2?", "--option", "Ship", "--option", "Wait", pid=pids["a"])
    [item] = items(dashboard)
    over = "y" * 8_001
    why = "your text is 8,001 characters; the most is 8,000"

    refusable: list[tuple[str, dict[str, object]]] = [
        (f"/api/items/{item['id']}/answer", {"answer": over}),
        (f"/api/items/{item['id']}/answer", {"answer": over, "ask": True}),
        ("/api/room", {"text": over}),
        ("/api/messages", {"to": ["a"], "text": over}),
    ]
    for path, data in refusable:
        refused = json.loads(http_request(dashboard + path, post=True, data=data, expect=409))
        assert refused == {"ok": False, "detail": why}, path
    [still] = items(dashboard)
    assert still["answered_at"] is None
    huge = json.loads(http_request(dashboard + "/api/room", post=True, data={"text": "ж" * 40_000}, expect=413))
    assert huge["ok"] is False and "too long to send" in huge["detail"]
    assert answer_on_page(dashboard, item["id"], "y" * 8_000) == "sent"


def test_malformed_dashboard_requests_are_refused(dashboard: str) -> None:
    http_request(dashboard + "/api/messages", post=True, data={"to": "b", "text": "x @a"}, expect=400)
    http_request(dashboard + "/api/messages", post=True, data={"to": [], "text": "x", "reply_to": "1"}, expect=400)
    http_request(dashboard + "/api/items/1/answer", post=True, data={"answer": 3}, expect=400)
    http_request(dashboard + "/api/items/99999999999999999999/answer", post=True, data={"answer": "x"}, expect=404)


def test_only_the_dashboard_page_answers_a_card_not_a_shell_with_the_header(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    pids = start(bridge_env, spawn_process, agterm_tabs=False)
    bridge_env.cli("post", "decision", "Ship v2?", "--option", "Ship", "--option", "Wait", pid=pids["a"])
    [item] = items(dashboard)
    answer, reply = f"/api/items/{item['id']}/answer", f"/api/items/{item['id']}/reply"
    page = {"Origin": dashboard, "Sec-Fetch-Site": "same-origin"}

    for path, data in ((answer, {"answer": "Ship"}), (reply, {"text": "go"})):
        body = json.dumps(data).encode()
        curl = {"X-Bridge": "1", "Content-Type": "application/json"}
        for headers in (curl, {**curl, "Origin": dashboard}, {**curl, **page, "Sec-Fetch-Site": "same-site"}):
            request = urllib.request.Request(dashboard + path, data=body, method="POST", headers=headers)
            with pytest.raises(urllib.error.HTTPError) as refused:
                urllib.request.urlopen(request, timeout=5)
            assert refused.value.code == 403, (path, headers)
    assert items(dashboard)[0]["answered_at"] is None

    http_request(dashboard + answer, post=True, headers=page, data={"answer": "Ship"})
    assert items(dashboard)[0]["answer"] == "Ship"


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


def test_a_long_user_message_is_typed_as_its_start_and_where_to_read_it_all() -> None:
    """Typed whole, 2,000 characters became a paste chip in the box and never got its Enter."""
    from typing import Any

    from bridge.board import USER_CONVERSATION
    from bridge.feed import TYPED_USER_MAX_CHARS, _convert_message_to_typed_text  # pyright: ignore[reportPrivateUsage]

    def typed(body: str) -> str:
        row: Any = {
            "from_conversation": USER_CONVERSATION,
            "from_handle": "user",
            "body": body,
            "kind": "info",
            "post_type": None,
        }
        return _convert_message_to_typed_text(row, 7, 0)

    short = "Send it.\n\nFinal text:\nfine as it is"
    assert typed(short) == "[bridge] user on the dashboard (#7): Send it. Final text: fine as it is"
    long = typed("word " * 400)
    assert len(long) < TYPED_USER_MAX_CHARS and long.endswith("with bridge show 7 before you act)")
    assert long.startswith("[bridge] user on the dashboard (#7): word word")


def guild_needs(base: str) -> list[dict[str, object]]:
    return json.loads(http_request(base + "/api/guild"))["needs"]


def answer_on_page(base: str, item_id: object, answer: str, ask: bool = False) -> str:
    data: dict[str, object] = {"answer": answer, "ask": ask}
    sent = json.loads(http_request(base + f"/api/items/{item_id}/answer", post=True, data=data))
    return str(sent["detail"])


def test_free_text_on_the_card_answers_and_closes_it_even_with_a_question_mark(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    pids = start(bridge_env, spawn_process, agterm_tabs=False)
    bridge_env.cli("post", "decision", "Ship v2?", "--option", "Ship", "--option", "Wait", pid=pids["a"])
    [item] = items(dashboard)

    assert answer_on_page(dashboard, item["id"], "Ship, but why not Friday? Fine either way.") == "sent"
    context = prompt(bridge_env, pids["a"], "conv-a", "next")
    assert 'Answer to your decision Q#1 "Ship v2?": Ship, but why not Friday? Fine either way.' in context
    [answered] = items(dashboard)
    assert answered["answer"] and answered["resolved_at"]


def test_ask_back_on_the_card_reaches_the_session_and_keeps_the_card_open(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    pids = start(bridge_env, spawn_process, agterm_tabs=False)
    bridge_env.cli("post", "decision", "Ship v2?", "--option", "Ship", "--option", "Wait", pid=pids["a"])
    [item] = items(dashboard)

    asked = answer_on_page(dashboard, item["id"], "Why does it happen? Keep it simple.", ask=True)
    context = prompt(bridge_env, pids["a"], "conv-a", "next")
    [card] = guild_needs(dashboard)

    assert asked == "asked"
    assert 'Question on your decision Q#1 "Ship v2?"' in context
    assert "Why does it happen? Keep it simple." in context
    assert (card["id"], card["question_ref"], "delivering" in card) == (1, "Q#1", False)
    [still_open] = items(dashboard)
    assert still_open["answer"] is None and still_open["resolved_at"] is None

    [thread] = [p for p in json.loads(http_request(dashboard + "/api/guild"))["room"] if p["kind"] == "post"]
    replied = bridge_env.cli("reply", str(thread["id"]), "A retiring worker hangs.", pid=pids["a"])
    assert replied.returncode == 0, replied.stderr
    [thread] = [p for p in json.loads(http_request(dashboard + "/api/guild"))["room"] if p["kind"] == "post"]
    assert "Question on your decision Q#1" in thread["body"]
    assert [r["body"] for r in thread["replies"]] == ["A retiring worker hangs."], "the page shows the answer"

    assert answer_on_page(dashboard, item["id"], "Ship") == "sent"
    assert 'Answer to your decision Q#1 "Ship v2?": Ship' in prompt(bridge_env, pids["a"], "conv-a", "again")
    [answered] = items(dashboard)
    assert answered["answer"] == "Ship" and answered["resolved_at"]


def test_an_option_with_a_question_mark_still_answers(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    pids = start(bridge_env, spawn_process, agterm_tabs=False)
    bridge_env.cli("post", "blocked", "Token?", "--option", "Use the staging token?", pid=pids["a"])

    assert answer_on_page(dashboard, 1, "Use the staging token?") == "sent"
    [answered] = items(dashboard)
    assert answered["answer"] == "Use the staging token?" and answered["resolved_at"]
