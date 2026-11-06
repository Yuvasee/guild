import json
import sqlite3
from collections.abc import Callable, Generator
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest
from conftest import BridgeEnv, SpawnProcess, http_request
from test_leads import Team

from bridge.asks import (
    Actor,
    AskError,
    add_ask,
    claim_ask_done,
    close_ask,
    close_settled_asks,
    find_overdue_asks,
    get_ask,
    list_ask_events,
    list_asks,
    map_db_to_asks_page,
    map_overdue_to_nags,
    mark_nagged,
    note_ask,
    reopen_ask,
    send_ask_nags,
    split_text_to_bodies,
    start_ask,
    verify_ask,
    wait_ask,
)
from bridge.config import Config
from bridge.db import connect

WORKER = Actor("work-1", "conv-w", "other")
LEAD = Actor("alex", "conv-l", "lead")
OTHER_LEAD = Actor("boris", "conv-b", "lead")
ASSISTANT = Actor("aide", "conv-x", "assistant")
EVIDENCE = "The TV at /office shows the new copy 'Ready' in the header, no stale banner"
HOW = "opened https://dash.example/office, screenshot ~/shots/tv.png"


@pytest.fixture
def conn(tmp_path: Path) -> Generator[sqlite3.Connection]:
    c = connect(tmp_path / "bridge.db")
    yield c
    c.close()


def claimed(conn: sqlite3.Connection, by: Actor = WORKER) -> int:
    ask = add_ask(conn, "alex", "aide", "Make the TV show the new copy", "#12", ["https://x.example/pr/1"], 1000)
    start_ask(conn, ask.id, by, 2000)
    claim_ask_done(conn, ask.id, by, "Deployed PR 1 to the office dashboard", 3000)
    return ask.id


def test_an_ask_goes_open_to_closed_and_logs_each_step(conn: sqlite3.Connection) -> None:
    ask_id = claimed(conn)
    verify_ask(conn, ask_id, LEAD, EVIDENCE, HOW, 4000)
    ask = close_ask(conn, ask_id, "owner", "you", 5000)
    assert (ask.state, ask.closed_how, ask.evidence_by, ask.claim_by) == ("closed", "owner", "alex", "work-1")
    assert (ask.started_at, ask.done_at, ask.verified_at, ask.closed_at) == (2000, 3000, 4000, 5000)
    assert [e["state"] for e in list_ask_events(conn, ask_id)] == [
        "open", "in_progress", "done_claimed", "verified", "closed",
    ]  # fmt: skip
    assert list_asks(conn) == []
    assert [a.id for a in list_asks(conn, include_closed=True)] == [ask_id]


Action = Callable[[sqlite3.Connection, int], object]
WRONG_ACTIONS: dict[str, Action] = {
    "verify": lambda c, i: verify_ask(c, i, LEAD, EVIDENCE, HOW, 9),
    "start": lambda c, i: start_ask(c, i, LEAD, 9),
    "close": lambda c, i: close_ask(c, i, "assistant", "aide", 9),
    "reopen": lambda c, i: reopen_ask(c, i, "aide", "still wrong", 9),
    "note": lambda c, i: note_ask(c, i, LEAD, "more news", 9),
    "done": lambda c, i: claim_ask_done(c, i, LEAD, "x" * 20, 9),
}


@pytest.mark.parametrize(
    ("steps", "action", "message"),
    [
        ([], "verify", "cannot verify #1: it is open"),
        (["start"], "start", "already in progress"),
        ([], "close", "not verified yet; --dropped"),
        ([], "reopen", "cannot reopen #1: it is open"),
        (["start", "done", "verify"], "note", "cannot note #1: it is verified"),
        (["start", "done"], "done", "cannot claim done"),
    ],
)
def test_wrong_transitions_say_why(conn: sqlite3.Connection, steps: list[str], action: str, message: str) -> None:
    ask = add_ask(conn, "alex", "aide", "words", "tab", [], 1)
    if "start" in steps:
        start_ask(conn, ask.id, WORKER, 2)
    if "done" in steps:
        claim_ask_done(conn, ask.id, WORKER, "Deployed PR 1 to the dashboard", 3)
    if "verify" in steps:
        verify_ask(conn, ask.id, LEAD, EVIDENCE, HOW, 4)
    with pytest.raises(AskError, match=message):
        WRONG_ACTIONS[action](conn, ask.id)


def test_the_claimer_cannot_verify_its_own_claim(conn: sqlite3.Connection) -> None:
    ask_id = claimed(conn, by=LEAD)
    with pytest.raises(AskError, match="claimed done by alex"):
        verify_ask(conn, ask_id, LEAD, EVIDENCE, HOW, 4000)
    same_conversation = Actor("alex-renamed", "conv-l", "lead")
    with pytest.raises(AskError, match="cannot verify it"):
        verify_ask(conn, ask_id, same_conversation, EVIDENCE, HOW, 4000)
    assert verify_ask(conn, ask_id, OTHER_LEAD, EVIDENCE, HOW, 4000).state == "verified"


def test_a_worker_cannot_verify(conn: sqlite3.Connection) -> None:
    ask_id = claimed(conn, by=LEAD)
    with pytest.raises(AskError, match="only a lead or the assistant verifies"):
        verify_ask(conn, ask_id, Actor("work-2", "conv-w2", "other"), EVIDENCE, HOW, 4000)
    assert verify_ask(conn, ask_id, ASSISTANT, EVIDENCE, HOW, 4000).evidence_by == "aide"


@pytest.mark.parametrize(
    ("evidence", "how", "message"),
    [
        ("works", HOW, "--evidence is 5 characters"),
        (EVIDENCE, "tests pass", "--how is 10 characters"),
        ("Deployed PR 1 to the office dashboard", HOW, "under 40"),
        (EVIDENCE, EVIDENCE, "the same text"),
    ],
)
def test_trivial_evidence_is_refused_with_the_hint(
    conn: sqlite3.Connection, evidence: str, how: str, message: str
) -> None:
    ask_id = claimed(conn)
    with pytest.raises(AskError, match=message) as raised:
        verify_ask(conn, ask_id, LEAD, evidence, how, 4000)
    assert "what the owner would see" in str(raised.value)
    assert get_ask(conn, ask_id).state == "done_claimed"


def test_reopen_clears_the_claim_and_keeps_history(conn: sqlite3.Connection) -> None:
    ask_id = claimed(conn)
    verify_ask(conn, ask_id, LEAD, EVIDENCE, HOW, 4000)
    ask = reopen_ask(conn, ask_id, "you", "The TV still shows the old copy", 5000)
    assert (ask.state, ask.claim, ask.evidence, ask.verified_at) == ("open", None, None, None)
    assert ask.reopen_why == "The TV still shows the old copy"
    notes = [e["note"] for e in list_ask_events(conn, ask_id)]
    assert any(str(n).startswith(EVIDENCE) for n in notes)
    assert notes[-1] == "reopened: The TV still shows the old copy"


def test_dropped_closes_an_unfinished_ask_only_with_a_reason(conn: sqlite3.Connection) -> None:
    ask = add_ask(conn, "alex", "aide", "words", "tab", [], 1)
    with pytest.raises(AskError, match="why"):
        close_ask(conn, ask.id, "assistant", "aide", 2, dropped=" ")
    closed = close_ask(conn, ask.id, "assistant", "aide", 2, dropped="He no longer wants it")
    assert closed.state == "closed"
    assert list_ask_events(conn, ask.id)[-1]["note"] == "dropped: He no longer wants it"


def write_assistant(env: BridgeEnv, handle: str) -> None:
    config = env.root / "config.toml"
    config.write_text(config.read_text() + f'assistant_handle = "{handle}"\n')


def test_cli_records_quotes_and_verifies_across_sessions(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    assert team.enroll().returncode == 0
    write_assistant(bridge_env, "old-b")
    sent = bridge_env.cli("send", "alex", "Please make the TV show the new copy", pid=team.b)
    assert sent.returncode == 0, sent.stderr
    with bridge_env.connect() as db:
        message_id = int(db.execute("SELECT max(id) FROM messages").fetchone()[0])

    bad_owner = team.guild("ask", "add", "nobody", "--words", "x", pid=team.b)
    assert bad_owner.returncode == 2 and "is neither" in bad_owner.stderr
    added = team.guild("ask", "add", "Alex", "--from", str(message_id), "--ref", "https://x.example/1", pid=team.b)
    assert added.returncode == 0, added.stderr
    from_stdin = team.guild("ask", "add", "old-b", pid=team.a, stdin="Check the nightly report\n")
    assert from_stdin.returncode == 0, from_stdin.stderr

    listed = json.loads(team.guild("ask", "list", "--json").stdout)
    assert [(a["owner"], a["source"], a["recorded_by"]) for a in listed] == [
        ("alex", f"#{message_id}", "old-b"), ("old-b", "tab", "alex"),
    ]  # fmt: skip
    assert listed[0]["words"] == "Please make the TV show the new copy"
    assert [a["id"] for a in json.loads(team.guild("ask", "list", "--owner", "alex", "--json").stdout)] == [1]

    assert team.guild("ask", "start", "1", pid=team.a).returncode == 0
    assert team.guild("ask", "done", "1", "--claim", "Deployed PR 1 to the office TV", pid=team.a).returncode == 0
    own = team.guild("ask", "verify", "1", "--evidence", EVIDENCE, "--how", HOW, pid=team.a)
    assert own.returncode == 2 and "cannot verify it" in own.stderr
    short = team.guild("ask", "verify", "1", "--evidence", "ok", "--how", "looked", pid=team.b)
    assert short.returncode == 2 and "a screenshot path" in short.stderr
    assert team.guild("ask", "verify", "1", "--evidence", EVIDENCE, "--how", HOW, pid=team.b).returncode == 0

    by_lead = team.guild("ask", "close", "1", pid=team.a)
    assert by_lead.returncode == 2 and "only old-b closes" in by_lead.stderr
    assert team.guild("ask", "close", "1", pid=team.b).returncode == 0
    shown = team.guild("ask", "show", "1").stdout
    assert "closed: assistant" in shown and "verified" in shown
    assert "no open asks" not in team.guild("ask", "list").stdout
    assert [a["id"] for a in json.loads(team.guild("ask", "list", "--json").stdout)] == [2]


HOUR = 3_600_000
NAG_CONFIG = Config(assistant_handle="aide", owner_name="Theo", ask_stale_hours=4, ask_verify_hours=1)


def test_nags_follow_the_clock_and_repeat_once_per_stale_period(conn: sqlite3.Connection) -> None:
    start = 10 * HOUR
    stale = add_ask(conn, "alex", "aide", "Fix the export", "tab", [], start).id
    claimed_id = add_ask(conn, "alex", "aide", "Reload the TV copy", "tab", [], start).id
    claim_ask_done(conn, claimed_id, WORKER, "Deployed the new copy to the TV", start)
    own = add_ask(conn, "aide", "aide", "Log today's asks", "tab", [], start).id

    def due(now: int) -> list[int]:
        return [a.id for a in find_overdue_asks(conn, now, 4 * HOUR, HOUR)]

    assert due(start + HOUR - 1) == []
    assert due(start + HOUR) == [claimed_id]
    mark_nagged(conn, [claimed_id], start + HOUR)
    assert due(start + 4 * HOUR) == [own, stale]
    mark_nagged(conn, [stale, own], start + 4 * HOUR)
    assert due(start + 5 * HOUR - 1) == []
    assert due(start + 5 * HOUR) == [claimed_id]
    mark_nagged(conn, [claimed_id], start + 5 * HOUR)
    note_ask(conn, stale, LEAD, "Export fix in review", start + 5 * HOUR)
    assert due(start + 8 * HOUR) == [own]
    verify_ask(conn, claimed_id, LEAD, EVIDENCE, HOW, start + 8 * HOUR)
    assert claimed_id not in due(start + 20 * HOUR)


def test_nag_texts_go_to_each_owner_and_one_line_each_to_the_assistant(conn: sqlite3.Connection) -> None:
    lead_claimed = add_ask(conn, "alex", "aide", "Reload the TV copy", "tab", [], 0).id
    claim_ask_done(conn, lead_claimed, LEAD, "Deployed the new copy to the TV", 0)
    worker_claimed = add_ask(conn, "alex", "aide", "Fix the export", "tab", [], 0).id
    claim_ask_done(conn, worker_claimed, WORKER, "Export fixed in PR 3", 0)
    open_one = add_ask(conn, "boris", "aide", "Answer the vendor", "tab", [], 0).id
    own = add_ask(conn, "aide", "aide", "Log today's asks", "tab", [], 0).id
    nags = map_overdue_to_nags(find_overdue_asks(conn, 5 * HOUR, 4 * HOUR, HOUR), "aide", "Theo", 5 * HOUR)
    texts = {nag.to: nag.text for nag in nags}
    assert set(texts) == {"alex", "boris", "aide"}
    assert f"#{lead_claimed} done_claimed, claimed by alex 5h ago" in texts["alex"]
    assert "you claimed it, so another lead or the assistant verifies it" in texts["alex"]
    assert f"guild ask verify {worker_claimed} --evidence" in texts["alex"]
    assert f"#{open_one} open, no update for 5h" in texts["boris"]
    assistant_lines = texts["aide"].splitlines()
    assert assistant_lines[0].startswith("Asks from Theo waiting on you")
    assert assistant_lines[1].startswith(f'#{own} open, no update for 5h: "Log today\'s asks"')
    assert assistant_lines[2].startswith("Asks overdue with their owners")
    assert [line.split(" ")[0] for line in assistant_lines[3:]] == [
        f"#{lead_claimed}",
        f"#{worker_claimed}",
        f"#{open_one}",
    ]


def test_unreached_owners_are_named_to_the_assistant_and_still_marked(conn: sqlite3.Connection) -> None:
    ask_id = add_ask(conn, "alex", "aide", "Fix the export", "tab", [], 0).id
    assert send_ask_nags(conn, NAG_CONFIG, 5 * HOUR) == []
    assert get_ask(conn, ask_id).last_nag_at == 5 * HOUR


def test_verified_asks_close_by_themselves_after_the_window(conn: sqlite3.Connection) -> None:
    ask_id = claimed(conn)
    verify_ask(conn, ask_id, LEAD, EVIDENCE, HOW, 4000)
    assert close_settled_asks(conn, 4000 + 24 * HOUR - 1, 24 * HOUR) == []
    assert close_settled_asks(conn, 4000 + 24 * HOUR, 24 * HOUR) == [ask_id]
    ask = get_ask(conn, ask_id)
    assert (ask.state, ask.closed_how) == ("closed", "auto")


def make_lead_and_assistant(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> Team:
    team = Team(bridge_env, spawn_process)
    assert team.enroll().returncode == 0
    write_assistant(bridge_env, "old-b")
    return team


def test_nags_reach_the_lead_and_the_assistant_as_messages(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = make_lead_and_assistant(bridge_env, spawn_process)
    assert team.guild("ask", "add", "alex", "--words", "Fix the export", pid=team.b).returncode == 0
    conn = bridge_env.connect()
    created = get_ask(conn, 1).created_at
    config = Config(assistant_handle="old-b", owner_name="Theo")
    sent = send_ask_nags(conn, config, created + 4 * HOUR)
    assert sorted(recipient.handle for _, recipient in sent) == ["alex", "old-b"]
    assert send_ask_nags(conn, config, created + 5 * HOUR) == []
    rows = conn.execute("SELECT body, post_type FROM messages WHERE from_handle = 'bridge' ORDER BY id").fetchall()
    assert "Asks from Theo waiting on you" in rows[0][0] and "Fix the export" in rows[0][0]
    assert rows[1][0].startswith("Asks overdue with their owners")
    assert [r[1] for r in rows] == [None, "to-assistant"]


def test_the_assistant_hears_last_who_was_not_told_and_long_batches_split(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    make_lead_and_assistant(bridge_env, spawn_process)
    conn = bridge_env.connect()
    for n in range(12):
        add_ask(conn, "alex", "old-b", f"Ask {n}: " + "the export must keep every column " * 6, "tab", [], 0)
    add_ask(conn, "boris", "old-b", "Answer the vendor", "tab", [], 0)
    add_ask(conn, "old-b", "old-b", "Log today's asks", "tab", [], 0)
    sent = send_ask_nags(conn, Config(assistant_handle="old-b", owner_name="Theo"), 5 * HOUR)
    handles = [recipient.handle for _, recipient in sent]
    assert handles.count("alex") >= 2 and handles[-1] == "old-b" and "boris" not in handles
    bodies = {mid: str(conn.execute("SELECT body FROM messages WHERE id = ?", (mid,)).fetchone()[0]) for mid, _ in sent}
    assert max(len(body) for body in bodies.values()) <= 2000
    alex_text = "\n".join(bodies[mid] for mid, r in sent if r.handle == "alex")
    assert all(f"Ask {n}:" in alex_text for n in range(12))
    first_to_assistant = next(bodies[mid] for mid, r in sent if r.handle == "old-b")
    assert first_to_assistant.startswith("Not told, no live session: boris")


def test_split_text_keeps_lines_whole_under_the_limit() -> None:
    assert split_text_to_bodies("a\nbb\nccc", 5) == ["a\nbb", "ccc"]
    assert split_text_to_bodies("x" * 9, 5) == ["xxxx…"]


def test_a_locked_tab_cannot_touch_asks(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = make_lead_and_assistant(bridge_env, spawn_process)
    assert team.guild("ask", "add", "alex", "--words", "Fix the export", pid=team.a).returncode == 0
    team.write_tree(workspace_b="🔒 Misc")
    bridge_env.cli("sessions")
    for args in (("add", "alex", "--words", "x y z"), ("start", "1"), ("note", "1", "some news")):
        refused = team.guild("ask", *args, pid=team.b)
        assert refused.returncode == 2 and "locked" in refused.stderr, args
    assert team.guild("ask", "list", "--owner", "Alex").stdout.count("#") == 1


def test_a_locked_owner_is_neither_shown_nor_nagged(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = make_lead_and_assistant(bridge_env, spawn_process)
    assert team.guild("ask", "add", "alex", "--words", "Fix the export", pid=team.b).returncode == 0
    team.write_tree(workspace_a="🔒 Private")
    bridge_env.cli("sessions")
    conn = bridge_env.connect()
    config = Config(assistant_handle="old-b", owner_name="Theo")
    assert send_ask_nags(conn, config, get_ask(conn, 1).created_at + 5 * HOUR) == []
    assert map_db_to_asks_page(conn, config, 0)["by_owner"] == {}


def test_page_shows_asks_and_closes_or_reopens_them(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    team = make_lead_and_assistant(bridge_env, spawn_process)
    for words in ("Make the TV show the new copy", "Fix the export", "Answer the vendor"):
        assert team.guild("ask", "add", "alex", "--words", words, pid=team.b).returncode == 0
    for ask_id in ("1", "2"):
        assert team.guild("ask", "done", ask_id, "--claim", "Deployed it to the office TV", pid=team.a).returncode == 0
        verified = team.guild("ask", "verify", ask_id, "--evidence", EVIDENCE, "--how", HOW, pid=team.b)
        assert verified.returncode == 0, verified.stderr
    assert team.guild("ask", "done", "3", "--claim", "Sent the vendor the answer", pid=team.a).returncode == 0

    page = json.loads(http_request(dashboard + "/api/guild"))
    assert [a["id"] for a in page["asks"]["verified"]] == [1, 2]
    assert [(a["id"], a["by"]) for a in page["asks"]["waiting"]] == [(3, "alex")]
    lead = next(lead for lead in page["leads"] if lead["name"] == "alex")
    assert (lead["asks"]["open"], [i["words"] for i in lead["asks"]["items"]]) == (1, ["Answer the vendor"])

    closed = json.loads(http_request(dashboard + "/api/asks/1/close", post=True, data={}))
    assert closed == {"ok": True, "detail": "closed"}
    again = json.loads(http_request(dashboard + "/api/asks/1/close", post=True, data={}, expect=409))
    assert "it is closed" in again["detail"]
    http_request(dashboard + "/api/asks/2/reopen", post=True, data={"why": "still old " + "x" * 1900})
    http_request(dashboard + "/api/asks/2/reopen", post=True, data={"why": 3}, expect=400)

    conn = bridge_env.connect()
    assert (get_ask(conn, 1).state, get_ask(conn, 1).closed_how) == ("closed", "owner")
    assert (get_ask(conn, 2).state, get_ask(conn, 2).reopen_why) == ("open", "on the page: still old " + "x" * 1900)
    told = conn.execute("SELECT body FROM messages WHERE from_handle = 'bridge' ORDER BY id DESC LIMIT 1").fetchone()
    assert "reopened ask #2 (on the page: still old xxx" in told[0] and "Fix the export" in told[0]
    page = json.loads(http_request(dashboard + "/api/guild"))
    assert page["asks"]["verified"] == []
    assert next(lead for lead in page["leads"] if lead["name"] == "alex")["asks"]["open"] == 2


def test_a_note_on_a_claim_counts_as_an_update(conn: sqlite3.Connection) -> None:
    ask_id = claimed(conn)
    ask = note_ask(conn, ask_id, LEAD, "Verifying after the worktree is reopened", 5000)
    assert (ask.state, ask.updated_at) == ("done_claimed", 5000)


WAIT_WHY = "Org admin rights only the owner can grant"


def test_a_waiting_ask_is_not_nagged_until_it_moves(conn: sqlite3.Connection) -> None:
    open_id = add_ask(conn, "alex", "aide", "Add the bot to the org", "tab", [], 0).id
    claimed_id = add_ask(conn, "alex", "aide", "Reload the TV copy", "tab", [], 0).id
    claim_ask_done(conn, claimed_id, LEAD, "Deployed the new copy to the TV", 0)
    wait_ask(conn, open_id, LEAD, "owner", WAIT_WHY, HOUR)
    waiting = wait_ask(conn, claimed_id, LEAD, "verifier", "boris verifies once the worktree is back", HOUR)
    assert (waiting.wait_on, waiting.wait_by, waiting.wait_at) == ("verifier", "alex", HOUR)
    assert find_overdue_asks(conn, 30 * HOUR, 4 * HOUR, HOUR) == []
    page = map_db_to_asks_page(conn, NAG_CONFIG, 30 * HOUR)
    assert page["waiting"] == [
        {
            "id": claimed_id, "owner": "alex", "words": "Reload the TV copy",
            "claim": "Deployed the new copy to the TV", "by": "alex", "at": 0, "overdue": False,
            "wait": {"on": "verifier", "why": "boris verifies once the worktree is back", "by": "alex", "at": HOUR},
        }
    ]  # fmt: skip
    assert [(a["id"], a["state"], a["at"]) for a in cast(list[dict[str, object]], page["on_you"])] == [
        (open_id, "open", 0)
    ]
    assert list_ask_events(conn, open_id)[-1]["note"] == f"waiting on owner: {WAIT_WHY}"

    start_ask(conn, open_id, LEAD, 30 * HOUR)
    assert get_ask(conn, open_id).wait_on is None
    assert [a.id for a in find_overdue_asks(conn, 34 * HOUR, 4 * HOUR, HOUR)] == [open_id]


Clear = Callable[[sqlite3.Connection, int], object]
CLEARS: dict[str, Clear] = {
    "note": lambda c, i: note_ask(c, i, LEAD, "news on it", 9),
    "verify": lambda c, i: verify_ask(c, i, OTHER_LEAD, EVIDENCE, HOW, 9),
    "reopen": lambda c, i: reopen_ask(c, i, "aide", "still wrong", 9),
    "drop": lambda c, i: close_ask(c, i, "assistant", "aide", 9, dropped="let go of it"),
}


@pytest.mark.parametrize("action", sorted(CLEARS))
def test_any_later_step_ends_the_wait(conn: sqlite3.Connection, action: str) -> None:
    ask_id = claimed(conn)
    wait_ask(conn, ask_id, LEAD, "release", "Ships with Thursday's release", 5)
    CLEARS[action](conn, ask_id)
    ask = get_ask(conn, ask_id)
    assert (ask.wait_on, ask.wait_why, ask.wait_by, ask.wait_at) == (None, None, None, None)


def test_done_ends_the_wait(conn: sqlite3.Connection) -> None:
    ask_id = add_ask(conn, "alex", "aide", "Add the bot to the org", "tab", [], 0).id
    wait_ask(conn, ask_id, LEAD, "approval", "Security has to approve the scope", 1)
    assert claim_ask_done(conn, ask_id, LEAD, "The bot is in the org now", 2).wait_on is None


@pytest.mark.parametrize(
    ("on", "why", "message"),
    [("someone", WAIT_WHY, "--on is one of owner, approval"), ("owner", " x ", "who or what it waits on")],
)
def test_a_wait_needs_a_known_kind_and_a_reason(conn: sqlite3.Connection, on: str, why: str, message: str) -> None:
    ask_id = add_ask(conn, "alex", "aide", "words", "tab", [], 0).id
    with pytest.raises(AskError, match=message):
        wait_ask(conn, ask_id, LEAD, on, why, 1)


def test_a_verified_ask_cannot_wait(conn: sqlite3.Connection) -> None:
    ask_id = claimed(conn)
    verify_ask(conn, ask_id, LEAD, EVIDENCE, HOW, 4000)
    with pytest.raises(AskError, match="cannot wait #1: it is verified"):
        wait_ask(conn, ask_id, LEAD, "owner", WAIT_WHY, 5000)


def test_reminders_name_the_wait_command(conn: sqlite3.Connection) -> None:
    open_id = add_ask(conn, "alex", "aide", "Add the bot to the org", "tab", [], 0).id
    claimed_id = add_ask(conn, "alex", "aide", "Reload the TV copy", "tab", [], 0).id
    claim_ask_done(conn, claimed_id, LEAD, "Deployed the new copy to the TV", 0)
    text = map_overdue_to_nags(find_overdue_asks(conn, 5 * HOUR, 4 * HOUR, HOUR), "aide", "Theo", 5 * HOUR)[0].text
    assert f"guild ask wait {open_id} --on theo|approval|release" in text
    assert f"guild ask wait {open_id} --until YYYY-MM-DD[THH:MM]" in text
    assert f"guild ask wait {claimed_id} --on verifier" in text


def test_cli_wait_shows_in_list_and_show(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = make_lead_and_assistant(bridge_env, spawn_process)
    config = bridge_env.root / "config.toml"
    config.write_text(config.read_text() + 'owner_name = "Theo"\n')
    assert team.guild("ask", "add", "alex", "--words", "Add the bot to the org", pid=team.b).returncode == 0
    assert "--on theo: waiting on Theo" in " ".join(team.guild("ask", "wait", "-h").stdout.split())
    bad = team.guild("ask", "wait", "1", "--on", "nobody", WAIT_WHY, pid=team.a)
    assert bad.returncode == 2 and "invalid choice" in bad.stderr
    waited = team.guild("ask", "wait", "1", "--on", "theo", WAIT_WHY, pid=team.a)
    assert waited.returncode == 0, waited.stderr
    assert "#1 waiting on Theo; no reminders until it moves" in waited.stdout
    assert get_ask(bridge_env.connect(), 1).wait_on == "owner"
    assert ", waiting on Theo" in team.guild("ask", "list").stdout
    shown = team.guild("ask", "show", "1").stdout
    assert f"waiting on Theo (alex, 0m ago): {WAIT_WHY}" in shown
    assert f"note: waiting on owner: {WAIT_WHY}" in shown
    assert team.guild("ask", "wait", "1", "--on", "owner", WAIT_WHY, pid=team.a).returncode == 0


def test_cli_pause_until_a_day(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = make_lead_and_assistant(bridge_env, spawn_process)
    assert team.guild("ask", "add", "alex", "--words", "Sweep the weekend", pid=team.b).returncode == 0
    both = team.guild("ask", "wait", "1", "--on", "owner", "--until", "2099-01-04", WAIT_WHY, pid=team.a)
    assert both.returncode == 2 and "not allowed with" in both.stderr
    bad = team.guild("ask", "wait", "1", "--until", "Sunday", WAIT_WHY, pid=team.a)
    assert bad.returncode == 2 and "YYYY-MM-DD or YYYY-MM-DDTHH:MM" in bad.stderr
    past = team.guild("ask", "wait", "1", "--until", "2020-01-05", WAIT_WHY, pid=team.a)
    assert past.returncode == 2 and "a time still ahead" in past.stderr
    paused = team.guild("ask", "wait", "1", "--until", "2099-01-04T09:30", "Next sweep is on Sunday", pid=team.a)
    assert paused.returncode == 0, paused.stderr
    assert "#1 paused until Sun 4 Jan 09:30; no reminders until then" in paused.stdout
    ask = get_ask(bridge_env.connect(), 1)
    assert ask.wait_on is None and ask.wait_until == int(datetime(2099, 1, 4, 9, 30).timestamp() * 1000)
    assert ", paused until Sun 4 Jan 09:30" in team.guild("ask", "list").stdout
    assert "note: paused until Sun 4 Jan 09:30: Next sweep is on Sunday" in team.guild("ask", "show", "1").stdout
    assert team.guild("ask", "wait", "1", "--until", "2099-01-04", WAIT_WHY, pid=team.a).stdout.startswith(
        "#1 paused until Sun 4 Jan;"
    )


def page_item(page: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, dict[str, list[dict[str, object]]]], page["by_owner"])["xavi"]["items"][0]


def test_a_pause_holds_reminders_until_its_time_and_stays_off_the_owner(conn: sqlite3.Connection) -> None:
    ask_id = add_ask(conn, "xavi", "aide", "Sweep the weekend", "tab", [], 0).id
    paused = wait_ask(conn, ask_id, LEAD, None, "Next sweep is on Sunday", HOUR, until=40 * HOUR)
    assert (paused.wait_on, paused.wait_until, paused.wait_at) == (None, 40 * HOUR, HOUR)
    assert find_overdue_asks(conn, 39 * HOUR, 4 * HOUR, HOUR) == []
    page = map_db_to_asks_page(conn, NAG_CONFIG, 39 * HOUR)
    assert page["on_you"] == []
    pause = {"on": None, "until": 40 * HOUR, "why": "Next sweep is on Sunday", "by": "alex", "at": HOUR}
    assert page_item(page)["wait"] == pause
    assert page_item(page)["overdue"] is False
    assert [a.id for a in find_overdue_asks(conn, 40 * HOUR, 4 * HOUR, HOUR)] == [ask_id]
    assert page_item(map_db_to_asks_page(conn, NAG_CONFIG, 40 * HOUR))["wait"] is None
    note_ask(conn, ask_id, LEAD, "Swept it on Sunday", 41 * HOUR)
    assert get_ask(conn, ask_id).wait_until is None


def test_a_stale_ask_is_reminded_about_as_soon_as_its_pause_ends(conn: sqlite3.Connection) -> None:
    ask_id = add_ask(conn, "xavi", "aide", "Sweep the weekend", "tab", [], 0).id
    assert wait_ask(conn, ask_id, LEAD, None, "Back in an hour", 10 * HOUR, until=11 * HOUR).updated_at == 0
    assert find_overdue_asks(conn, 11 * HOUR - 1, 4 * HOUR, HOUR) == []
    assert [a.id for a in find_overdue_asks(conn, 11 * HOUR, 4 * HOUR, HOUR)] == [ask_id]


@pytest.mark.parametrize(
    ("on", "until", "message"),
    [(None, None, "waits on someone"), ("owner", 9 * HOUR, "waits on someone"), (None, HOUR, "a time still ahead")],
)
def test_a_wait_is_on_someone_or_until_a_time_ahead(
    conn: sqlite3.Connection, on: str | None, until: int | None, message: str
) -> None:
    ask_id = add_ask(conn, "alex", "aide", "words", "tab", [], 0).id
    with pytest.raises(AskError, match=message):
        wait_ask(conn, ask_id, LEAD, on, WAIT_WHY, HOUR, until=until)
