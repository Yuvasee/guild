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
    ASK_AUTOCLOSE_HOURS,
    TOLD_TO_VERIFY,
    Actor,
    AskError,
    add_ask,
    claim_ask_done,
    close_ask,
    close_settled_asks,
    drop_ask,
    find_asks_to_verify,
    find_overdue_asks,
    get_ask,
    is_ask_overdue,
    list_ask_events,
    list_asks,
    map_asks_to_owner_nags,
    map_db_to_asks_page,
    map_overdue_to_assistant_nag,
    mark_nagged,
    note_ask,
    reopen_ask,
    send_ask_nags,
    split_text_to_bodies,
    start_ask,
    tell_owner_of_reopen,
    verify_ask,
    wait_ask,
)
from bridge.board import Recipient, merge_user_messages, take_delivery
from bridge.capacity import read_owner_presence, record_owner_off, record_owner_on
from bridge.config import Config, map_toml_to_config
from bridge.db import connect
from bridge.registry import rename_handle

WORKER = Actor("work-1", "conv-w", "other")
LEAD = Actor("alex", "conv-l", "lead")
OTHER_LEAD = Actor("boris", "conv-b", "lead")
ASSISTANT = Actor("aide", "conv-x", "assistant")
EVIDENCE = "The status page at /status shows the new copy 'Ready' in the header, no stale banner"
HOW = "opened https://dash.example/status, screenshot ~/shots/status.png"


@pytest.fixture
def conn(tmp_path: Path) -> Generator[sqlite3.Connection]:
    c = connect(tmp_path / "bridge.db")
    yield c
    c.close()


def claimed(conn: sqlite3.Connection, by: Actor = WORKER) -> int:
    ask = add_ask(
        conn, "alex", "aide", "Make the status page show the new copy", "#12", ["https://x.example/pr/1"], 1000
    )
    start_ask(conn, ask.id, by, 2000)
    claim_ask_done(conn, ask.id, by, "Deployed PR 1 to the status page", 3000)
    return ask.id


def test_an_ask_goes_open_to_closed_at_verification_and_logs_each_step(conn: sqlite3.Connection) -> None:
    ask_id = claimed(conn)
    ask = verify_ask(conn, ask_id, LEAD, EVIDENCE, HOW, 4000, "Q#7")
    assert (ask.state, ask.closed_how, ask.evidence_by, ask.claim_by) == ("closed", "auto", "alex", "work-1")
    assert (ask.started_at, ask.done_at, ask.verified_at, ask.closed_at) == (2000, 3000, 4000, 4000)
    assert [e["state"] for e in list_ask_events(conn, ask_id)] == [
        "open", "in_progress", "done_claimed", "verified", "closed",
    ]  # fmt: skip
    closed = list_ask_events(conn, ask_id)[-1]
    assert (closed["by"], closed["note"]) == (
        "bridge", "closed at once on verification, by the owner's standing rule (Q#7)",
    )  # fmt: skip
    assert verify_ask(conn, claimed(conn), OTHER_LEAD, EVIDENCE, HOW, 4000).state == "closed"
    assert list_ask_events(conn, 2)[-1]["note"] == "closed at once on verification, by the owner's standing rule"
    assert list_asks(conn) == []
    assert [a.id for a in list_asks(conn, include_closed=True)] == [ask_id, 2]


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
        (["start", "done", "verify"], "note", "cannot note #1: it is closed"),
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
    assert verify_ask(conn, ask_id, OTHER_LEAD, EVIDENCE, HOW, 4000).state == "closed"


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
        ("Deployed PR 1 to the status page", HOW, "under 40"),
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
    ask = reopen_ask(conn, ask_id, "you", "The status page still shows the old copy", 5000)
    assert (ask.state, ask.claim, ask.evidence, ask.verified_at) == ("open", None, None, None)
    assert ask.reopen_why == "The status page still shows the old copy"
    notes = [e["note"] for e in list_ask_events(conn, ask_id)]
    assert any(str(n).startswith(EVIDENCE) for n in notes)
    assert notes[-1] == "reopened: The status page still shows the old copy"


def test_a_reopened_ask_refuses_its_earlier_claim_word_for_word_and_quotes_the_last_reopen(
    conn: sqlite3.Connection,
) -> None:
    ask_id = claimed(conn)
    reopen_ask(conn, ask_id, "you", "The banner still shows on mobile", 4000)

    with pytest.raises(AskError, match="reopened since: The banner still shows on mobile"):
        claim_ask_done(conn, ask_id, WORKER, "  Deployed PR 1 to   the status page ", 5000)
    again = claim_ask_done(conn, ask_id, WORKER, "Deployed PR 2: the banner is gone on mobile too", 6000)

    assert again.state == "done_claimed"


def test_dropped_closes_an_unfinished_ask_only_with_a_reason(conn: sqlite3.Connection) -> None:
    ask = add_ask(conn, "alex", "aide", "words", "tab", [], 1)
    with pytest.raises(AskError, match="why"):
        close_ask(conn, ask.id, "assistant", "aide", 2, dropped=" ")
    closed = close_ask(conn, ask.id, "assistant", "aide", 2, dropped="They no longer want it")
    assert closed.state == "closed"
    assert list_ask_events(conn, ask.id)[-1]["note"] == "dropped: They no longer want it"


def write_assistant(env: BridgeEnv, handle: str) -> None:
    config = env.root / "config.toml"
    config.write_text(config.read_text() + f'assistant_handle = "{handle}"\n')


def test_cli_records_quotes_and_verifies_across_sessions(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    assert team.enroll().returncode == 0
    write_assistant(bridge_env, "old-b")
    sent = bridge_env.cli("send", "alex", "Please make the status page show the new copy", pid=team.b)
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
    assert listed[0]["words"] == "Please make the status page show the new copy"
    assert [a["id"] for a in json.loads(team.guild("ask", "list", "--owner", "alex", "--json").stdout)] == [1]

    assert team.guild("ask", "start", "1", pid=team.a).returncode == 0
    assert team.guild("ask", "done", "1", "--claim", "Deployed PR 1 to the status page", pid=team.a).returncode == 0
    own = team.guild("ask", "verify", "1", "--evidence", EVIDENCE, "--how", HOW, pid=team.a)
    assert own.returncode == 2 and "cannot verify it" in own.stderr
    short = team.guild("ask", "verify", "1", "--evidence", "ok", "--how", "looked", pid=team.b)
    assert short.returncode == 2 and "a screenshot path" in short.stderr
    verified = team.guild("ask", "verify", "1", "--evidence", EVIDENCE, "--how", HOW, pid=team.b)
    assert verified.returncode == 0 and "verified by old-b and closed" in verified.stdout

    by_lead = team.guild("ask", "close", "1", pid=team.a)
    assert by_lead.returncode == 2 and "only old-b closes" in by_lead.stderr
    again = team.guild("ask", "close", "1", pid=team.b)
    assert again.returncode == 2 and "it is closed" in again.stderr
    shown = team.guild("ask", "show", "1").stdout
    assert "closed: auto" in shown and "verified" in shown
    assert "no open asks" not in team.guild("ask", "list").stdout
    assert [a["id"] for a in json.loads(team.guild("ask", "list", "--json").stdout)] == [2]


HOUR = 3_600_000
NAG_CONFIG = Config(assistant_handle="aide", owner_name="Theo")


def test_nags_follow_the_clock_and_repeat_once_per_stale_period(conn: sqlite3.Connection) -> None:
    start = 10 * HOUR
    stale = add_ask(conn, "alex", "aide", "Fix the export", "tab", [], start).id
    claimed_id = add_ask(conn, "alex", "aide", "Reload the status page copy", "tab", [], start).id
    claim_ask_done(conn, claimed_id, WORKER, "Deployed the new copy to the status page", start)
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


def map_overdue_to_texts(conn: sqlite3.Connection, now: int, unreached: list[str] | None = None) -> dict[str, str]:
    asks = find_overdue_asks(conn, now, 4 * HOUR, HOUR)
    texts = {nag.to: nag.text for nag in map_asks_to_owner_nags([a for a in asks if a.owner != "aide"], "Theo", now)}
    to_verify = find_asks_to_verify(conn, now, 24 * HOUR)
    to_assistant = map_overdue_to_assistant_nag(asks, to_verify, "aide", "Theo", now, unreached or [])
    return texts | ({"aide": to_assistant.text} if to_assistant else {})


def test_nag_texts_go_to_each_owner_and_the_assistant_hears_claims_after_a_day_and_unanswered_reminders(
    conn: sqlite3.Connection,
) -> None:
    lead_claimed = add_ask(conn, "alex", "aide", "Reload the status page copy", "tab", [], 0).id
    claim_ask_done(conn, lead_claimed, LEAD, "Deployed the new copy to the status page", 0)
    worker_claimed = add_ask(conn, "alex", "aide", "Fix the export", "tab", [], 0).id
    claim_ask_done(conn, worker_claimed, WORKER, "Export fixed in PR 3", 0)
    open_one = add_ask(conn, "boris", "aide", "Answer the vendor", "tab", [], 0).id
    own = add_ask(conn, "aide", "aide", "Log today's asks", "tab", [], 0).id
    texts = map_overdue_to_texts(conn, 5 * HOUR)
    assert set(texts) == {"alex", "boris", "aide"}
    assert f"#{lead_claimed} done_claimed" in texts["alex"]
    assert "you claimed it, so another lead or the assistant verifies it" in texts["alex"]
    assert f"guild ask verify {worker_claimed} --evidence" in texts["alex"]
    assert f"#{open_one} open, no update for 5h" in texts["boris"]
    assistant_lines = texts["aide"].splitlines()
    assert assistant_lines[0].startswith("Asks from Theo waiting on you")
    assert assistant_lines[1].startswith(f'#{own} open, no update for 5h: "Log today\'s asks"')
    assert len(assistant_lines) == 2

    mark_nagged(conn, [lead_claimed, worker_claimed, open_one, own], 5 * HOUR)
    later = map_overdue_to_texts(conn, 24 * HOUR)["aide"].splitlines()
    verify_at = later.index(
        "To verify, claimed done over 24h ago and still not verified, and no live lead can verify them; each is sent "
        "once (Theo's words):"
    )
    assert [line.split(" ")[0] for line in later[verify_at + 1 : verify_at + 3]] == [
        f"#{lead_claimed}",
        f"#{worker_claimed}",
    ]
    assert later[verify_at + 1].startswith(f"#{lead_claimed} alex done_claimed, claimed by alex 24h ago")
    assert (
        f"its owner claimed it, so you or another lead verify: `guild ask verify {lead_claimed}" in later[verify_at + 1]
    )
    assert "verify:" not in later[verify_at + 2]
    assert later[-2:] == [
        "Asks overdue: the owner's move, no update for 4h even after a reminder (Theo's words; details sent to each "
        "owner with a live session):",
        f"#{open_one} boris open, no update for 24h: Answer the vendor",
    ]


def test_an_owner_that_could_not_be_told_reaches_the_assistant_at_once(conn: sqlite3.Connection) -> None:
    open_one = add_ask(conn, "boris", "aide", "Answer the vendor", "tab", [], 0).id
    lines = map_overdue_to_texts(conn, 5 * HOUR, ["boris"])["aide"].splitlines()
    assert lines[0] == "Not told, no live session: boris"
    assert lines[1].startswith("Asks overdue:") and lines[2].startswith(f"#{open_one} boris open")


def test_a_first_reminder_to_a_lead_leaves_the_assistant_alone(conn: sqlite3.Connection) -> None:
    add_ask(conn, "boris", "aide", "Answer the vendor", "tab", [], 0)
    assert set(map_overdue_to_texts(conn, 5 * HOUR)) == {"boris"}


def test_an_ask_nobody_was_told_about_stays_due(conn: sqlite3.Connection) -> None:
    ask_id = add_ask(conn, "alex", "aide", "Fix the export", "tab", [], 0).id
    assert send_ask_nags(conn, NAG_CONFIG, 5 * HOUR, owner_off=False) == []
    assert (get_ask(conn, ask_id).last_nag_at, get_ask(conn, ask_id).nag_count) == (None, 0)
    assert due_ids(conn, 5 * HOUR + 1) == [ask_id]


def verified_before_close_at_once(conn: sqlite3.Connection, ask_id: int) -> None:
    """A row verified before verification closed the ask at once."""
    verify_ask(conn, ask_id, LEAD, EVIDENCE, HOW, 4000)
    conn.execute("UPDATE asks SET state = 'verified', closed_how = NULL, closed_at = NULL WHERE id = ?", (ask_id,))
    conn.execute("DELETE FROM ask_events WHERE ask_id = ? AND state = 'closed'", (ask_id,))


def test_older_verified_asks_close_by_themselves_after_the_window(conn: sqlite3.Connection) -> None:
    ask_id = claimed(conn)
    verified_before_close_at_once(conn, ask_id)
    assert close_settled_asks(conn, 4000 + 24 * HOUR - 1, 24 * HOUR) == []
    assert close_settled_asks(conn, 4000 + 24 * HOUR, 24 * HOUR, "Q#7") == [ask_id]
    ask = get_ask(conn, ask_id)
    assert (ask.state, ask.closed_how) == ("closed", "auto")
    assert list_ask_events(conn, ask_id)[-1]["note"] == (
        "closed automatically 24 h after verification, by the owner's standing rule (Q#7)"
    )


def test_the_service_sweep_closes_an_older_verified_ask_at_once(conn: sqlite3.Connection) -> None:
    ask_id = claimed(conn)
    verified_before_close_at_once(conn, ask_id)
    assert close_settled_asks(conn, 4000, ASK_AUTOCLOSE_HOURS * HOUR, "Q#7") == [ask_id]
    assert list_ask_events(conn, ask_id)[-1]["note"] == (
        "closed automatically after verification, by the owner's standing rule (Q#7)"
    )


def test_a_reopened_ask_is_not_closed_by_itself_and_no_decision_cites_none(conn: sqlite3.Connection) -> None:
    reopened, kept = claimed(conn), claimed(conn)
    for ask_id in (reopened, kept):
        verified_before_close_at_once(conn, ask_id)
    reopen_ask(conn, reopened, "aide", "the header still shows the old copy", 5000)
    assert close_settled_asks(conn, 4000 + 24 * HOUR, 24 * HOUR) == [kept]
    assert get_ask(conn, reopened).state == "open"
    assert (
        list_ask_events(conn, kept)[-1]["note"]
        == "closed automatically 24 h after verification, by the owner's standing rule"
    )


DAY = 24 * HOUR


def test_an_ask_quiet_for_three_days_is_checked_daily_waiting_or_not(conn: sqlite3.Connection) -> None:
    waits = add_ask(conn, "alex", "aide", "Add the bot to the org", "tab", [], 0).id
    wait_ask(conn, waits, LEAD, "owner", WAIT_WHY, HOUR)
    paused = add_ask(conn, "alex", "aide", "Sweep the weekend", "tab", [], 0).id
    wait_ask(conn, paused, LEAD, None, "Next sweep is on Sunday", HOUR, until=10 * DAY)
    assert due_ids(conn, HOUR + 3 * DAY - 1) == []
    remind_at(conn, waits, HOUR + 3 * DAY)
    assert due_ids(conn, HOUR + 4 * DAY - 1) == []
    remind_at(conn, waits, HOUR + 4 * DAY)
    text = map_overdue_to_texts(conn, HOUR + 5 * DAY)["alex"]
    assert f'#{waits} open, no update for 120h: "Add the bot to the org" (waiting on Theo: {WAIT_WHY})' in text
    assert "no update for 3 days: is it still current, done or obsolete?" in text
    for command in (f"guild ask note {waits} ", f"guild ask done {waits} --claim", f"guild ask drop {waits} '<why>'"):
        assert command in text
    assert f"#{paused} " not in text
    assert "Asks overdue" not in map_overdue_to_texts(conn, HOUR + 5 * DAY).get("aide", "")
    assert f"#{waits} alex open" in map_overdue_to_texts(conn, HOUR + 5 * DAY, ["alex"])["aide"]
    wait_ask(conn, waits, LEAD, "owner", "Theo grants the org admin rights on Friday", HOUR + 5 * DAY)
    assert due_ids(conn, HOUR + 8 * DAY - 1) == []


def test_a_stale_ask_gets_the_check_once_it_is_quiet_for_three_days(conn: sqlite3.Connection) -> None:
    ask_id = add_ask(conn, "alex", "aide", "Fix the export", "tab", [], 0).id
    assert "is it still current" not in map_overdue_to_texts(conn, 3 * DAY - 1)["alex"]
    assert "is it still current" in map_overdue_to_texts(conn, 3 * DAY)["alex"]
    assert is_ask_overdue(get_ask(conn, ask_id), 3 * DAY, 4 * HOUR)


def test_an_owner_session_drops_its_obsolete_ask_with_the_reason_in_history(conn: sqlite3.Connection) -> None:
    ask_id = add_ask(conn, "alex", "aide", "Fix the export", "tab", [], 0).id
    with pytest.raises(AskError, match="only alex or the assistant drops it"):
        drop_ask(conn, ask_id, OTHER_LEAD, "the export is gone", HOUR)
    with pytest.raises(AskError, match="says why"):
        drop_ask(conn, ask_id, LEAD, " no ", HOUR)
    dropped = drop_ask(conn, ask_id, LEAD, "the export was removed from the product", HOUR)
    assert (dropped.state, dropped.closed_how, dropped.closed_at) == ("closed", None, HOUR)
    last = list_ask_events(conn, ask_id)[-1]
    assert (last["by"], last["state"], last["note"]) == (
        "alex",
        "closed",
        "dropped: the export was removed from the product",
    )
    by_assistant = add_ask(conn, "alex", "aide", "Answer the vendor", "tab", [], 0).id
    assert drop_ask(conn, by_assistant, ASSISTANT, "the vendor withdrew", HOUR).closed_how == "assistant"
    claim = claimed(conn)
    with pytest.raises(AskError, match=f"it is claimed done; it is verified, or `guild ask reopen {claim}"):
        drop_ask(conn, claim, LEAD, "not needed any more", 4000)
    verify_ask(conn, claim, OTHER_LEAD, EVIDENCE, HOW, 4000)
    with pytest.raises(AskError, match=f"cannot drop #{claim}: it is closed"):
        drop_ask(conn, claim, LEAD, "not needed any more", 5000)


def test_cli_drop_closes_and_show_says_so(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = make_lead_and_assistant(bridge_env, spawn_process)
    assert team.guild("ask", "add", "alex", "--words", "Fix the export", pid=team.b).returncode == 0
    dropped = team.guild("ask", "drop", "1", "the export was removed from the product", pid=team.a)
    assert dropped.returncode == 0, dropped.stderr
    assert dropped.stdout.strip() == "#1 closed as dropped; old-b is told"
    told = bridge_env.connect().execute("SELECT body, post_type FROM messages WHERE from_handle = 'bridge'").fetchone()
    assert told[0].startswith(
        'alex dropped ask #1 as obsolete (the export was removed from the product): "Fix the export"'
    )
    assert "`guild ask reopen 1 --why '…'`" in told[0] and told[1] == "to-assistant"
    shown = team.guild("ask", "show", "1").stdout
    assert "closed: dropped by its owner session" in shown
    assert "alex closed: dropped: the export was removed from the product" in shown


def test_the_panel_lists_only_what_needs_attention_oldest_first_capped(conn: sqlite3.Connection) -> None:
    fresh = add_ask(conn, "alex", "aide", "Fresh one", "tab", [], 10 * DAY).id
    on_you = add_ask(conn, "alex", "aide", "Waits on the owner", "tab", [], DAY).id
    wait_ask(conn, on_you, LEAD, "owner", WAIT_WHY, 10 * DAY)
    approval = add_ask(conn, "alex", "aide", "Waits on approval", "tab", [], 2 * DAY).id
    wait_ask(conn, approval, LEAD, "approval", "Security has to approve the scope", 10 * DAY)
    old_wait = add_ask(conn, "boris", "aide", "Waits for days", "tab", [], 3 * DAY).id
    wait_ask(conn, old_wait, OTHER_LEAD, "release", "Ships with the next release", 3 * DAY)
    claim = add_ask(conn, "boris", "aide", "Claimed long ago", "tab", [], 4 * DAY).id
    claim_ask_done(conn, claim, WORKER, "Deployed the new copy to the status page", 5 * DAY)
    new_claim = add_ask(conn, "boris", "aide", "Claimed just now", "tab", [], 5 * DAY).id
    claim_ask_done(conn, new_claim, WORKER, "Deployed the new copy to the status page", 10 * DAY)
    stale = add_ask(conn, "alex", "aide", "No update today", "tab", [], 6 * DAY).id
    note_ask(conn, stale, LEAD, "Export fix in review", 10 * DAY - 5 * HOUR)

    def panel(config: Config) -> tuple[list[tuple[object, object]], object]:
        page = map_db_to_asks_page(conn, config, 10 * DAY)
        return [(a["id"], a["why"]) for a in cast(list[dict[str, object]], page["attention"])], page["more"]

    expected = [(on_you, "on_you"), (old_wait, "overdue"), (claim, "to_verify"), (stale, "overdue")]
    assert panel(NAG_CONFIG) == (expected, 0)
    assert panel(Config(assistant_handle="aide", owner_name="Theo", asks_panel_max=2)) == (expected[:2], 2)
    assert {fresh, approval, new_claim}.isdisjoint(i for i, _ in panel(NAG_CONFIG)[0])


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
    sent = send_ask_nags(conn, config, created + 4 * HOUR, owner_off=False)
    assert [recipient.handle for _, recipient in sent] == ["alex"]
    assert send_ask_nags(conn, config, created + 5 * HOUR, owner_off=False) == []
    sent = send_ask_nags(conn, config, created + 8 * HOUR, owner_off=False)
    assert [recipient.handle for _, recipient in sent] == ["alex", "old-b"]
    rows = conn.execute("SELECT body, post_type FROM messages WHERE from_handle = 'bridge' ORDER BY id").fetchall()
    assert "Asks from Theo waiting on you" in rows[0][0] and "Fix the export" in rows[0][0]
    assert rows[2][0].startswith("Asks overdue: the owner's move")
    assert [r[1] for r in rows] == [None, None, "to-assistant"]


def test_the_assistant_hears_last_who_was_not_told_and_long_batches_split(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    make_lead_and_assistant(bridge_env, spawn_process)
    conn = bridge_env.connect()
    for n in range(12):
        add_ask(conn, "alex", "old-b", f"Ask {n}: " + "the export must keep every column " * 6, "tab", [], 0)
    add_ask(conn, "boris", "old-b", "Answer the vendor", "tab", [], 0)
    add_ask(conn, "old-b", "old-b", "Log today's asks", "tab", [], 0)
    sent = send_ask_nags(conn, Config(assistant_handle="old-b", owner_name="Theo"), 5 * HOUR, owner_off=False)
    handles = [recipient.handle for _, recipient in sent]
    assert handles.count("alex") >= 2 and handles[-1] == "old-b" and "boris" not in handles
    bodies = {mid: str(conn.execute("SELECT body FROM messages WHERE id = ?", (mid,)).fetchone()[0]) for mid, _ in sent}
    assert max(len(body) for body in bodies.values()) <= 2000
    alex_text = "\n".join(bodies[mid] for mid, r in sent if r.handle == "alex")
    assert all(f"Ask {n}:" in alex_text for n in range(12))
    first_to_assistant = next(bodies[mid] for mid, r in sent if r.handle == "old-b")
    assert first_to_assistant.startswith("Not told, no live session: boris")
    boris_ask = conn.execute("SELECT id FROM asks WHERE owner = 'boris'").fetchone()[0]
    assert get_ask(conn, boris_ask).last_nag_at == 5 * HOUR


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
    assert send_ask_nags(conn, config, get_ask(conn, 1).created_at + 5 * HOUR, owner_off=False) == []
    assert map_db_to_asks_page(conn, config, 0)["by_owner"] == {}


def test_page_shows_asks_and_closes_or_reopens_them(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    team = make_lead_and_assistant(bridge_env, spawn_process)
    for words in ("Make the status page show the new copy", "Fix the export", "Answer the vendor"):
        assert team.guild("ask", "add", "alex", "--words", words, pid=team.b).returncode == 0
    for ask_id in ("1", "2"):
        assert (
            team.guild("ask", "done", ask_id, "--claim", "Deployed it to the status page", pid=team.a).returncode == 0
        )
        verified = team.guild("ask", "verify", ask_id, "--evidence", EVIDENCE, "--how", HOW, pid=team.b)
        assert verified.returncode == 0, verified.stderr
    assert team.guild("ask", "done", "3", "--claim", "Sent the vendor the answer", pid=team.a).returncode == 0

    page = json.loads(http_request(dashboard + "/api/guild"))
    assert page["asks"]["verified"] == []
    assert (page["asks"]["attention"], page["asks"]["more"]) == ([], 0)
    lead = next(lead for lead in page["leads"] if lead["name"] == "alex")
    assert (lead["asks"]["open"], [i["words"] for i in lead["asks"]["items"]]) == (1, ["Answer the vendor"])

    again = json.loads(http_request(dashboard + "/api/asks/1/close", post=True, data={}, expect=409))
    assert "it is closed" in again["detail"]
    why = "still old, see /p/no-such-page " + "x" * 7_800
    http_request(dashboard + "/api/asks/2/reopen", post=True, data={"why": why})
    http_request(dashboard + "/api/asks/2/reopen", post=True, data={"why": 3}, expect=400)

    conn = bridge_env.connect()
    assert (get_ask(conn, 1).state, get_ask(conn, 1).closed_how) == ("closed", "auto")
    assert (get_ask(conn, 2).state, get_ask(conn, 2).reopen_why) == ("open", "on the page: " + why)
    [told] = conn.execute("SELECT body FROM messages WHERE body LIKE '%reopened ask #2%'").fetchall()
    assert "reopened ask #2 (on the page: still old, see (page no-such-page, no local file) xxx" in told[0]
    assert "x" * 7_800 in told[0] and "Fix the export" in told[0], "the whole reason, and a dead page link no refusal"
    page = json.loads(http_request(dashboard + "/api/guild"))
    assert page["asks"]["verified"] == []
    assert next(lead for lead in page["leads"] if lead["name"] == "alex")["asks"]["open"] == 2


def test_a_reopen_reason_reaches_the_asks_owner_whole_as_the_owners_words(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = make_lead_and_assistant(bridge_env, spawn_process)
    assert team.guild("ask", "add", "alex", "--words", "Fix the export", pid=team.b).returncode == 0
    assert team.guild("ask", "done", "1", "--claim", "Deployed it to the status page", pid=team.a).returncode == 0
    assert team.guild("ask", "verify", "1", "--evidence", EVIDENCE, "--how", HOW, pid=team.b).returncode == 0
    conn = bridge_env.connect()
    why = "on the page: still old " + " ".join(f"w{i}x" for i in range(1_400))
    ask = reopen_ask(conn, 1, "user", why, 9_000)

    tell_owner_of_reopen(conn, Config(), ask, 9_000)

    delivery = take_delivery(conn, "conv-a", 9_000, False)
    given = "\n".join(p.text for p in merge_user_messages(delivery.user_messages))
    assert "the user's own words" in given and "…" not in given.replace('"…")', "")
    at = [given.find(f"w{i}x") for i in range(1_400)]
    assert -1 not in at and at == sorted(at), "every word of the reason, in order"


def test_a_reopen_nobody_could_be_told_of_says_so_in_its_answer(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    team = make_lead_and_assistant(bridge_env, spawn_process)
    assert team.guild("ask", "add", "alex", "--words", "Fix the export", pid=team.b).returncode == 0
    assert team.guild("ask", "done", "1", "--claim", "Deployed it to the status page", pid=team.a).returncode == 0
    assert team.guild("ask", "verify", "1", "--evidence", EVIDENCE, "--how", HOW, pid=team.b).returncode == 0
    bridge_env.hook("SessionEnd", {"session_id": "conv-a", "cwd": str(bridge_env.root), "reason": "other"}, team.a)

    reopened = json.loads(http_request(dashboard + "/api/asks/1/reopen", post=True, data={"why": "still old"}))

    assert reopened["ok"] is True
    assert reopened["detail"] == "open again, but alex was not told: its tab is locked or has no live session"


def test_a_note_on_a_claim_counts_as_an_update(conn: sqlite3.Connection) -> None:
    ask_id = claimed(conn)
    ask = note_ask(conn, ask_id, LEAD, "Verifying after the worktree is reopened", 5000)
    assert (ask.state, ask.updated_at) == ("done_claimed", 5000)


WAIT_WHY = "Org admin rights only the owner can grant"


def test_a_waiting_ask_is_not_nagged_until_it_moves(conn: sqlite3.Connection) -> None:
    open_id = add_ask(conn, "alex", "aide", "Add the bot to the org", "tab", [], 0).id
    claimed_id = add_ask(conn, "alex", "aide", "Reload the status page copy", "tab", [], 0).id
    claim_ask_done(conn, claimed_id, LEAD, "Deployed the new copy to the status page", 0)
    wait_ask(conn, open_id, LEAD, "owner", WAIT_WHY, HOUR)
    waiting = wait_ask(conn, claimed_id, LEAD, "verifier", "boris verifies once the worktree is back", HOUR)
    assert (waiting.wait_on, waiting.wait_by, waiting.wait_at) == ("verifier", "alex", HOUR)
    assert find_overdue_asks(conn, 30 * HOUR, 4 * HOUR, HOUR) == []
    page = map_db_to_asks_page(conn, NAG_CONFIG, 30 * HOUR)
    assert page["attention"] == [
        {
            "id": open_id, "owner": "alex", "words": "Add the bot to the org", "state": "open", "why": "on_you",
            "claim": None, "by": None, "at": 0, "done_at": None, "updated_at": HOUR,
            "wait": {"on": "owner", "why": WAIT_WHY, "by": "alex", "at": HOUR},
        },
        {
            "id": claimed_id, "owner": "alex", "words": "Reload the status page copy", "state": "done_claimed",
            "why": "to_verify", "claim": "Deployed the new copy to the status page", "by": "alex", "at": 0,
            "done_at": 0, "updated_at": HOUR,
            "wait": {"on": "verifier", "why": "boris verifies once the worktree is back", "by": "alex", "at": HOUR},
        },
    ]  # fmt: skip
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
    with pytest.raises(AskError, match="cannot wait #1: it is closed"):
        wait_ask(conn, ask_id, LEAD, "owner", WAIT_WHY, 5000)


def test_reminders_name_the_wait_command(conn: sqlite3.Connection) -> None:
    open_id = add_ask(conn, "alex", "aide", "Add the bot to the org", "tab", [], 0).id
    claimed_id = add_ask(conn, "aide", "aide", "Reload the status page copy", "tab", [], 0).id
    claim_ask_done(
        conn, claimed_id, Actor("aide", "conv-a", "assistant"), "Deployed the new copy to the status page", 0
    )
    texts = map_overdue_to_texts(conn, 5 * HOUR)
    assert f"waiting on Theo (review, decision, OK)? `guild ask wait {open_id} --on theo '…'`" in texts["alex"]
    assert f"guild ask wait {open_id} --on approval|release" in texts["alex"]
    assert f"guild ask wait {open_id} --until YYYY-MM-DD[THH:MM]" in texts["alex"]
    assert "aide" not in texts
    assert (
        f"you claimed it, so a lead verifies it: ask one, then `guild ask wait {claimed_id} --on verifier"
        in (map_overdue_to_texts(conn, 24 * HOUR)["aide"])
    )


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
    colleague = team.guild("ask", "wait", "1", "--on", "colleague", "Mira approves the schema", pid=team.a)
    assert "#1 waiting on a colleague; no reminders until it moves" in colleague.stdout, colleague.stderr
    assert ", waiting on a colleague" in team.guild("ask", "list").stdout


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


def test_a_claim_is_never_late_on_the_page(conn: sqlite3.Connection) -> None:
    ask_id = claimed(conn, LEAD)
    page = map_db_to_asks_page(conn, Config(assistant_handle="aide", owner_name="Theo"), 30 * HOUR)
    lead = cast(dict[str, dict[str, object]], page["by_owner"])["alex"]
    assert lead["overdue"] is False
    assert cast(list[dict[str, object]], lead["items"])[0]["overdue"] is False
    assert [(a["id"], a["why"]) for a in cast(list[dict[str, object]], page["attention"])] == [(ask_id, "to_verify")]


def page_item(page: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, dict[str, list[dict[str, object]]]], page["by_owner"])["xavi"]["items"][0]


def test_a_pause_holds_reminders_until_its_time_and_stays_off_the_owner(conn: sqlite3.Connection) -> None:
    ask_id = add_ask(conn, "xavi", "aide", "Sweep the weekend", "tab", [], 0).id
    paused = wait_ask(conn, ask_id, LEAD, None, "Next sweep is on Sunday", HOUR, until=40 * HOUR)
    assert (paused.wait_on, paused.wait_until, paused.wait_at) == (None, 40 * HOUR, HOUR)
    assert find_overdue_asks(conn, 39 * HOUR, 4 * HOUR, HOUR) == []
    page = map_db_to_asks_page(conn, NAG_CONFIG, 39 * HOUR)
    assert page["attention"] == []
    pause = {"on": None, "until": 40 * HOUR, "why": "Next sweep is on Sunday", "by": "alex", "at": HOUR}
    assert page_item(page)["wait"] == pause
    assert page_item(page)["overdue"] is False
    assert [a.id for a in find_overdue_asks(conn, 40 * HOUR, 4 * HOUR, HOUR)] == [ask_id]
    assert page_item(map_db_to_asks_page(conn, NAG_CONFIG, 40 * HOUR))["wait"] is None
    note_ask(conn, ask_id, LEAD, "Swept it on Sunday", 41 * HOUR)
    assert get_ask(conn, ask_id).wait_until is None


def test_a_pause_is_an_update_so_a_short_one_ends_into_a_full_stale_period(conn: sqlite3.Connection) -> None:
    ask_id = add_ask(conn, "xavi", "aide", "Sweep the weekend", "tab", [], 0).id
    mark_nagged(conn, [ask_id], 4 * HOUR)
    paused = wait_ask(conn, ask_id, LEAD, None, "Back in an hour", 10 * HOUR, until=11 * HOUR)
    assert (paused.updated_at, paused.last_nag_at, paused.nag_count) == (10 * HOUR, None, 0)
    assert find_overdue_asks(conn, 14 * HOUR - 1, 4 * HOUR, HOUR) == []
    assert [a.id for a in find_overdue_asks(conn, 14 * HOUR, 4 * HOUR, HOUR)] == [ask_id]


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


def due_ids(conn: sqlite3.Connection, now: int) -> list[int]:
    return [a.id for a in find_overdue_asks(conn, now, 4 * HOUR, HOUR)]


def remind_at(conn: sqlite3.Connection, ask_id: int, now: int) -> None:
    assert ask_id not in due_ids(conn, now - 1), now
    assert ask_id in due_ids(conn, now), now
    mark_nagged(conn, [ask_id], now)


def test_reminders_back_off_doubling_to_a_day_while_nothing_changes(conn: sqlite3.Connection) -> None:
    ask_id = add_ask(conn, "alex", "aide", "Fix the export", "tab", [], 0).id
    for at in (4, 8, 16, 32, 56, 80):
        remind_at(conn, ask_id, at * HOUR)
    assert get_ask(conn, ask_id).nag_count == 6


def test_a_note_resets_the_clock_and_the_back_off(conn: sqlite3.Connection) -> None:
    ask_id = add_ask(conn, "alex", "aide", "Fix the export", "tab", [], 0).id
    remind_at(conn, ask_id, 4 * HOUR)
    remind_at(conn, ask_id, 8 * HOUR)
    noted = note_ask(conn, ask_id, LEAD, "Export fix in review", 9 * HOUR)
    assert (noted.last_nag_at, noted.nag_count) == (None, 0)
    remind_at(conn, ask_id, 13 * HOUR)
    remind_at(conn, ask_id, 17 * HOUR)


def test_a_note_on_a_claimed_ask_resets_its_verify_reminder(conn: sqlite3.Connection) -> None:
    ask_id = claimed(conn)
    remind_at(conn, ask_id, 3000 + HOUR)
    note_ask(conn, ask_id, LEAD, "Boris checks it after lunch", 2 * HOUR)
    remind_at(conn, ask_id, 3 * HOUR)


def test_a_wait_or_a_claim_stops_the_stale_reminder(conn: sqlite3.Connection) -> None:
    waited = add_ask(conn, "alex", "aide", "Add the bot to the org", "tab", [], 0).id
    done = add_ask(conn, "alex", "aide", "Fix the export", "tab", [], 0).id
    remind_at(conn, waited, 4 * HOUR)
    remind_at(conn, done, 4 * HOUR)
    wait_ask(conn, waited, LEAD, "owner", WAIT_WHY, 5 * HOUR)
    claim_ask_done(conn, done, LEAD, "Export fixed in PR 3, deployed", 5 * HOUR)
    assert due_ids(conn, 6 * HOUR - 1) == []
    remind_at(conn, done, 6 * HOUR)
    assert get_ask(conn, done).state == "done_claimed"
    remind_at(conn, waited, 5 * HOUR + 72 * HOUR)


def test_the_night_holds_reminders_and_the_morning_gets_one(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    make_lead_and_assistant(bridge_env, spawn_process)
    conn = bridge_env.connect()
    config = Config(assistant_handle="old-b", owner_name="Theo")
    first = add_ask(conn, "alex", "old-b", "Fix the export", "tab", [], 0).id
    second = add_ask(conn, "alex", "old-b", "Reload the status page copy", "tab", [], HOUR).id
    record_owner_off(conn, "guild dream --wait-all", 2 * HOUR)
    for at in range(5, 14):
        assert send_ask_nags(conn, config, at * HOUR, read_owner_presence(conn, config, at * HOUR).off) == [], at
    assert get_ask(conn, first).last_nag_at is None
    record_owner_on(conn, "guild day", 14 * HOUR)
    sent = send_ask_nags(conn, config, 14 * HOUR, read_owner_presence(conn, config, 14 * HOUR).off)
    assert [recipient.handle for _, recipient in sent] == ["alex"]
    to_alex = str(conn.execute("SELECT body FROM messages WHERE id = ?", (sent[0][0],)).fetchone()[0])
    assert f"#{first} " in to_alex and f"#{second} " in to_alex
    assert send_ask_nags(conn, config, 17 * HOUR, read_owner_presence(conn, config, 17 * HOUR).off) == []
    assert [get_ask(conn, i).nag_count for i in (first, second)] == [1, 1]


def bodies_to(conn: sqlite3.Connection, sent: list[tuple[int, Recipient]], handle: str) -> str:
    ids = [message_id for message_id, recipient in sent if recipient.handle == handle]
    return "\n".join(str(conn.execute("SELECT body FROM messages WHERE id = ?", (i,)).fetchone()[0]) for i in ids)


def test_a_claim_reaches_its_lead_first_and_the_assistant_once_after_a_day(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    make_lead_and_assistant(bridge_env, spawn_process)
    conn = bridge_env.connect()
    config = Config(assistant_handle="old-b", owner_name="Theo")
    own_claim = add_ask(conn, "alex", "old-b", "Fix the export", "tab", [], 0).id
    claim_ask_done(conn, own_claim, LEAD, "Export fixed in PR 3, deployed", 0)
    waits = add_ask(conn, "alex", "old-b", "Reload the status page copy", "tab", [], 0).id
    claim_ask_done(conn, waits, WORKER, "Deployed the new copy to the status page", 0)
    wait_ask(conn, waits, LEAD, "verifier", "boris verifies once the worktree is back", HOUR)

    sent = send_ask_nags(conn, config, HOUR, owner_off=False)
    assert [recipient.handle for _, recipient in sent] == ["alex"]
    assert "you claimed it, so another lead or the assistant verifies it" in bodies_to(conn, sent, "alex")

    sent = send_ask_nags(conn, config, 24 * HOUR, owner_off=False)
    to_assistant = bodies_to(conn, sent, "old-b")
    assert to_assistant.startswith("To verify, claimed done over 24h ago")
    assert f"#{own_claim} alex done_claimed" in to_assistant and f"#{waits} alex done_claimed" in to_assistant
    assert [list_ask_events(conn, i)[-1]["note"] for i in (own_claim, waits)] == [TOLD_TO_VERIFY] * 2
    for at in (25, 48, 72, 200):
        assert "old-b" not in [r.handle for _, r in send_ask_nags(conn, config, at * HOUR, owner_off=False)], at

    reopen_ask(conn, own_claim, "aide", "the export still drops a column", 201 * HOUR)
    claim_ask_done(conn, own_claim, LEAD, "Export fixed again in PR 4, deployed", 202 * HOUR)
    note_ask(conn, own_claim, LEAD, TOLD_TO_VERIFY, 203 * HOUR)
    assert find_asks_to_verify(conn, 226 * HOUR - 1, 24 * HOUR) == []
    assert [a.id for a in find_asks_to_verify(conn, 226 * HOUR, 24 * HOUR)] == [own_claim]


def test_a_lead_hears_of_its_own_claim_when_the_assistant_cannot_be_told(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    make_lead_and_assistant(bridge_env, spawn_process)
    conn = bridge_env.connect()
    ask_id = add_ask(conn, "alex", "old-b", "Fix the export", "tab", [], 0).id
    claim_ask_done(conn, ask_id, LEAD, "Export fixed in PR 3, deployed", 0)
    sent = send_ask_nags(conn, Config(assistant_handle="nobody", owner_name="Theo"), HOUR, owner_off=False)
    assert [recipient.handle for _, recipient in sent] == ["alex"]
    body = str(conn.execute("SELECT body FROM messages WHERE id = ?", (sent[0][0],)).fetchone()[0])
    assert "you claimed it, so another lead or the assistant verifies it" in body
    assert get_ask(conn, ask_id).nag_count == 1


def test_a_wait_citing_an_unpublished_page_does_not_sink_the_daily_check(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    make_lead_and_assistant(bridge_env, spawn_process)
    conn = bridge_env.connect()
    ask_id = add_ask(conn, "alex", "old-b", "Fix the export", "tab", [], 0).id
    wait_ask(conn, ask_id, LEAD, "approval", "Security approves the scope on /p/missing-page", HOUR)
    sent = send_ask_nags(conn, Config(assistant_handle="old-b", owner_name="Theo"), HOUR + 3 * DAY, owner_off=False)
    assert [recipient.handle for _, recipient in sent] == ["alex"]
    assert "approves the scope on (page missing-page, no local file)" in bodies_to(conn, sent, "alex")


def test_words_citing_an_unpublished_page_do_not_sink_the_reminder(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    make_lead_and_assistant(bridge_env, spawn_process)
    conn = bridge_env.connect()
    cites = add_ask(conn, "alex", "old-b", "Make /p/missing-page show the spec", "tab", [], 0).id
    plain = add_ask(conn, "alex", "old-b", "Fix the export", "tab", [], 0).id
    sent = send_ask_nags(conn, Config(assistant_handle="old-b", owner_name="Theo"), 5 * HOUR, owner_off=False)
    assert [recipient.handle for _, recipient in sent] == ["alex"]
    body = str(conn.execute("SELECT body FROM messages WHERE id = ?", (sent[0][0],)).fetchone()[0])
    assert "Make (page missing-page, no local file) show the spec" in body
    assert [get_ask(conn, i).nag_count for i in (cites, plain)] == [1, 1]


@pytest.mark.parametrize("renamed", [False, True])
def test_a_locked_assistant_counts_as_not_told(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, renamed: bool
) -> None:
    team = make_lead_and_assistant(bridge_env, spawn_process)
    team.write_tree(workspace_b="🔒 Private")
    bridge_env.cli("sessions")
    conn = bridge_env.connect()
    if renamed:
        surface = str(conn.execute("SELECT surface FROM surfaces WHERE handle = 'old-b'").fetchone()[0])
        rename_handle(conn, surface, "old-b", "renamed-b", 0)
    ask_id = add_ask(conn, "alex", "old-b", "Fix the export", "tab", [], 0).id
    claim_ask_done(conn, ask_id, LEAD, "Export fixed in PR 3, deployed", 0)
    sent = send_ask_nags(conn, Config(assistant_handle="old-b", owner_name="Theo"), HOUR, owner_off=False)
    assert [recipient.handle for _, recipient in sent] == ["alex"]


def test_the_panel_shows_at_least_one_ask() -> None:
    assert map_toml_to_config({"asks_panel_max": 3}).asks_panel_max == 3
    with pytest.raises(ValueError, match="asks_panel_max must be at least 1"):
        map_toml_to_config({"asks_panel_max": 0})
