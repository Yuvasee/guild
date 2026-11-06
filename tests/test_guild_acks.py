import json
import time
from typing import Any, cast

from conftest import BridgeEnv, SpawnProcess, http_request
from test_leads import Team

from bridge.board import MAX_AGENT_HOPS
from bridge.config import Config
from bridge.feed import post_from_user_to_room
from bridge.guildview import ROOM_POSTS, map_db_to_guild


def two_leads(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> Team:
    team = Team(bridge_env, spawn_process)
    assert team.enroll().returncode == 0
    assert team.enroll("Boris", "old-b").returncode == 0
    return team


def room_of(bridge_env: BridgeEnv) -> list[dict[str, Any]]:
    with bridge_env.connect() as conn:
        state = map_db_to_guild(conn, Config(), int(time.time() * 1000), lambda ref: "")
    return [i for i in cast(list[dict[str, Any]], state["room"]) if i["kind"] == "post"]


def recipients_of(bridge_env: BridgeEnv, body: str) -> list[str]:
    with bridge_env.connect() as conn:
        rows = conn.execute(
            "SELECT r.handle FROM message_recipients r JOIN messages m ON m.id = r.message_id WHERE m.body = ?", (body,)
        ).fetchall()
    return [str(r[0]) for r in rows]


def test_lead_post_reaches_every_other_lead_without_a_wake(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = two_leads(bridge_env, spawn_process)
    bridge_env.stub_log.write_text("")

    done = bridge_env.cli("board", "Restarting the service", "--type", "heads-up", pid=team.a)
    time.sleep(1.5)
    delivered = team.prompt(team.b, "conv-b", "next")

    assert done.returncode == 0, done.stderr
    assert recipients_of(bridge_env, "Restarting the service") == ["boris"]
    assert "session type" not in team.log()
    assert 'board post from alex: "Restarting the service"' in delivered


def test_mention_still_wakes_and_a_post_by_someone_else_is_not_broadcast(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    bridge_env.stub_log.write_text("")

    bridge_env.cli("board", "From a tab outside the team", pid=team.b)

    assert recipients_of(bridge_env, "From a tab outside the team") == []


def test_user_post_reaches_every_lead_and_a_locked_lead_stays_out(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    team = two_leads(bridge_env, spawn_process)
    bridge_env.stub_log.write_text("")

    http_request(dashboard + "/api/room", post=True, data={"text": "Morning, everyone"})
    time.sleep(1.5)
    typed = "session type" in team.log()
    team.write_tree(workspace_a="Alex · Data", workspace_b="🔒 Misc")
    bridge_env.cli("sessions")
    http_request(dashboard + "/api/room", post=True, data={"text": "Second post"})

    assert sorted(recipients_of(bridge_env, "Morning, everyone")) == ["alex", "boris"]
    assert not typed
    assert recipients_of(bridge_env, "Second post") == ["alex"]


def test_seen_by_comes_from_delivery_records_and_acks_are_kept_apart(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = two_leads(bridge_env, spawn_process)
    bridge_env.cli("board", "Export shipped", "--type", "report", pid=team.a)

    before = room_of(bridge_env)[-1]
    team.prompt(team.b, "conv-b", "next")
    after = room_of(bridge_env)[-1]
    acked = team.guild("ack", str(before["id"]), "nothing for me", pid=team.b)
    with_ack = room_of(bridge_env)[-1]

    assert before["seen_by"] == []
    assert after["seen_by"] == ["Boris"]
    assert acked.returncode == 0, acked.stderr
    assert with_ack["acks"] == [{"who": "Boris", "body": "nothing for me"}]
    assert with_ack["replies"] == [], "an ack is not a reply on the page"
    assert with_ack["seen_by"] == ["Boris"]


def test_status_line_rises_to_a_message_only_when_an_ack_says_something(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = two_leads(bridge_env, spawn_process)
    bridge_env.cli("board", "Restarting", "--type", "heads-up", pid=team.a)
    bridge_env.cli("board", "Deploying", "--type", "heads-up", pid=team.a)
    restarting, deploying = room_of(bridge_env)[-2:]
    team.guild("ack", str(restarting["id"]), "Nothing for me.", pid=team.b)
    team.guild("ack", str(deploying["id"]), "my worker rebases after", pid=team.b)

    levels = {str(item["body"]): item["level"] for item in room_of(bridge_env)}

    assert levels == {"Restarting": "line", "Deploying": "message"}


def test_follow_up_to_a_user_post_wakes_only_a_mention_and_skips_a_locked_lead(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = two_leads(bridge_env, spawn_process)
    now = int(time.time() * 1000)

    def post_in_room(text: str, reply_to: int | None) -> tuple[list[str], list[str], int]:
        with bridge_env.connect() as conn:
            result, typed = post_from_user_to_room(conn, Config(), "sam", text, now, reply_to)
        return sorted(r.handle for r in result.recipients), [r.handle for r in typed], result.message_id

    reached, typed, post = post_in_room("Morning, everyone", None)
    follow_up = post_in_room("One more thing", post)
    mention = post_in_room("@boris yours", post)
    team.write_tree(workspace_a="Alex · Data", workspace_b="🔒 Misc")
    bridge_env.cli("sessions")
    locked = post_in_room("Last thing", post)

    assert (reached, typed) == (["alex", "boris"], [])
    assert follow_up[:2] == (["alex", "boris"], [])
    assert mention[:2] == (["alex", "boris"], ["boris"])
    assert locked[:2] == (["alex"], [])


def test_unnamed_worker_receipt_does_not_read_as_its_lead(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    bridge_env.cli("board", "@old-b look", pid=team.a)
    team.prompt(team.b, "conv-b", "next")
    now = int(time.time() * 1000)
    tab: dict[str, object] = {"id": "TAB-B", "name": "Worker brief 1", "parent": "TAB-A", "status": "active"}
    tree: dict[str, object] = {"workspaces": [{"name": "Alex · Data", "sessions": [tab]}]}
    with bridge_env.connect() as conn:
        room = cast(
            list[dict[str, Any]], map_db_to_guild(conn, Config(), now, lambda ref: "", tree, now, {}, now)["room"]
        )

    assert [i["seen_by"] for i in room if i["kind"] == "post"] == [["old-b"]]


def test_a_late_ack_does_not_push_a_newer_thread_out_of_the_room(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = two_leads(bridge_env, spawn_process)
    with bridge_env.connect() as conn:
        for number in range(ROOM_POSTS + 1):
            conn.execute(
                "INSERT INTO messages (created_at, from_conversation, from_handle, kind, body, board) "
                "VALUES (?, 'conv-a', 'alex', 'info', ?, 1)",
                (1000 + number, f"post {number}"),
            )
        conn.execute("UPDATE messages SET thread_id = id")
        oldest = conn.execute("SELECT min(id) FROM messages").fetchone()[0]
    assert team.guild("ack", str(oldest), "nothing for me", pid=team.b).returncode == 0

    bodies = [item["body"] for item in room_of(bridge_env)]

    assert "post 0" not in bodies
    assert f"post {ROOM_POSTS}" in bodies and "post 1" in bodies


def test_ack_stays_single_after_clear(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = two_leads(bridge_env, spawn_process)
    bridge_env.cli("board", "Export shipped", pid=team.a)
    post = room_of(bridge_env)[-1]["id"]
    assert team.guild("ack", str(post), "nothing for me", pid=team.b).returncode == 0
    team.start(team.b, "conv-b2", "clear")

    again = team.guild("ack", str(post), "nothing for me", pid=team.b)

    assert again.returncode == 2


def test_seen_by_names_a_lead_once_across_its_conversations(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = two_leads(bridge_env, spawn_process)
    bridge_env.cli("board", "Export shipped", pid=team.a)
    team.prompt(team.b, "conv-b", "next")
    post = room_of(bridge_env)[-1]["id"]
    with bridge_env.connect() as conn:
        conn.execute(
            "INSERT INTO message_recipients (message_id, conversation_id, handle, offered_at) "
            "VALUES (?, 'conv-older', 'boris', 1)",
            (post,),
        )

    assert room_of(bridge_env)[-1]["seen_by"] == ["Boris"]


def test_ack_refuses_own_post_an_ack_a_repeat_a_question_a_mention_and_non_leads(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = two_leads(bridge_env, spawn_process)
    bridge_env.cli("board", "Export shipped", pid=team.a)
    post = room_of(bridge_env)[-1]["id"]
    bridge_env.cli("send", "alex", "a direct line", pid=team.b)
    with bridge_env.connect() as conn:
        direct = conn.execute("SELECT id FROM messages WHERE body = 'a direct line'").fetchone()[0]

    own = team.guild("ack", str(post), "mine", pid=team.a)
    question = team.guild("ack", str(post), "is this ours?", pid=team.b)
    mention = team.guild("ack", str(post), "tell @alex", pid=team.b)
    not_board = team.guild("ack", str(direct), "fine", pid=team.a)
    first = team.guild("ack", str(post), "nothing for me", pid=team.b)
    again = team.guild("ack", str(post), "still nothing", pid=team.b)
    with bridge_env.connect() as conn:
        ack_id = conn.execute("SELECT id FROM messages WHERE post_type = 'ack'").fetchone()[0]
    of_an_ack = team.guild("ack", str(ack_id), "noted", pid=team.a)
    team.guild("lead", "retire", "boris", "--force")
    not_a_lead = team.guild("ack", str(post), "fine", pid=team.b)

    assert first.returncode == 0, first.stderr
    for refused in (own, question, mention, not_board, again, of_an_ack, not_a_lead):
        assert refused.returncode == 2, refused.stdout


def test_acks_cannot_loop_they_reach_nobody_and_spend_no_hops(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = two_leads(bridge_env, spawn_process)
    bridge_env.stub_log.write_text("")
    bridge_env.cli("board", "Export shipped", pid=team.a)
    post = room_of(bridge_env)[-1]["id"]
    delivered = team.prompt(team.b, "conv-b", "first turn")

    # A lead following its role to the letter: every board post delivered gets an ack, until nothing is delivered.
    rounds = 0
    while "board post from" in delivered and rounds < 5:
        assert team.guild("ack", str(post), "nothing for me", pid=team.b).returncode == 0
        delivered = team.prompt(team.a, "conv-a", "turn") + team.prompt(team.b, "conv-b", "turn")
        rounds += 1
    time.sleep(1.5)
    with bridge_env.connect() as conn:
        ack = conn.execute(
            "SELECT m.id, m.agent_hops, m.paused, "
            "(SELECT count(*) FROM message_recipients r WHERE r.message_id = m.id) "
            "FROM messages m WHERE m.post_type = 'ack'"
        ).fetchall()

    assert rounds == 1, "one delivery, one ack, then silence"
    assert len(ack) == 1 and tuple(ack[0])[1:] == (0, 0, 0)
    assert "session type" not in team.log(), "an ack wakes nobody"


def test_four_hop_pause_counts_real_replies_only_however_many_acks_come_between(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = two_leads(bridge_env, spawn_process)
    bridge_env.cli("board", "Export shipped", pid=team.a)
    post = int(room_of(bridge_env)[-1]["id"])

    last = post
    for hop in range(MAX_AGENT_HOPS + 1):
        if hop == 2:
            assert team.guild("ack", str(post), "nothing for me", pid=team.b).returncode == 0
        pid = team.b if hop % 2 == 0 else team.a
        done = bridge_env.cli("reply", str(last), f"real reply {hop}", pid=pid)
        assert done.returncode == 0, done.stderr
        with bridge_env.connect() as conn:
            last = int(conn.execute("SELECT max(id) FROM messages WHERE post_type IS NOT 'ack'").fetchone()[0])
    with bridge_env.connect() as conn:
        rows = conn.execute(
            "SELECT agent_hops, paused FROM messages WHERE body LIKE 'real reply%' ORDER BY id"
        ).fetchall()

    assert [r[0] for r in rows] == list(range(1, MAX_AGENT_HOPS + 2))
    assert [r[1] for r in rows] == [0] * MAX_AGENT_HOPS + [1], "the pause stays, and an ack did not bring it closer"


def test_ack_cost_charges_what_was_delivered_at_its_own_length(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    team = two_leads(bridge_env, spawn_process)
    http_request(dashboard + "/api/room", post=True, data={"text": "z" * 800})
    untouched = json.loads(team.guild("ack-cost", "--json").stdout)
    team.prompt(team.a, "conv-a", "next")
    post = room_of(bridge_env)[-1]["id"]
    team.guild("ack", str(post), "seen before its hook", pid=team.b)
    after = json.loads(team.guild("ack-cost", "--json").stdout)

    assert (untouched["addressed"], untouched["not_yet_seen"], untouched["estimated_tokens"]) == (2, 2, 0)
    assert after["not_yet_seen"] == 0, "an ack before the hook counts as seen"
    assert after["estimated_tokens"] == (800 + 40 + len("seen before its hook")) // 4 + 150


def test_ack_cost_counts_posts_deliveries_and_acks(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = two_leads(bridge_env, spawn_process)
    bridge_env.cli("board", "x" * 400, pid=team.a)
    bridge_env.cli("board", "Second", pid=team.a)
    post = room_of(bridge_env)[-1]["id"]
    team.prompt(team.b, "conv-b", "next")
    team.guild("ack", str(post), "y" * 40, pid=team.b)

    cost = json.loads(team.guild("ack-cost", "--json").stdout)
    human = team.guild("ack-cost", "--hours", "1").stdout

    assert (cost["posts"], cost["addressed"], cost["not_yet_seen"], cost["acks"]) == (2, 2, 0, 1)
    assert cost["estimated_tokens"] == (240 + 46 + 40) // 4 + 150
    assert "not counted" in human
