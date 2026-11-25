import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest
from conftest import BridgeEnv, SpawnProcess
from test_asks import EVIDENCE, HOW
from test_leads import Team

from bridge.asks import (
    NO_CARD,
    SPOT_CHECKED,
    VERIFY_RULE,
    Actor,
    AskError,
    add_ask,
    assign_verifiers,
    claim_ask_done,
    find_overdue_asks,
    get_ask,
    link_card_to_ask,
    list_ask_events,
    list_unassigned_claims,
    pick_verifier,
    reopen_ask,
    send_ask_nags,
    send_card_notices,
    send_spot_check,
    verify_ask,
    wait_ask,
)
from bridge.board import Party, Recipient
from bridge.config import Config
from bridge.db import connect
from bridge.feed import add_post, withdraw_post
from bridge.leads import retire_lead

HOUR = 3_600_000
DAY = 24 * HOUR
CONFIG = Config(assistant_handle="aide", owner_name="Theo")
ALEX = Actor("alex", "conv-alex", "lead")
WORKER_OF_BORIS = Actor("work-b", "conv-work-b", "other")
CLAIM = "Export fixed in PR 3, deployed"


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[sqlite3.Connection]:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    c = connect(tmp_path / "bridge.db")
    yield c
    c.close()


def add_session(conn: sqlite3.Connection, handle: str, pid: int, parent_tab: str | None = None) -> None:
    conn.execute(
        "INSERT INTO surfaces (surface, agterm_session, handle, updated_at) VALUES (?, ?, ?, 0)",
        (handle, f"TAB-{handle}", handle),
    )
    conn.execute(
        """
        INSERT INTO instances (instance_id, pid, proc_start_ms, conversation_id, surface, status, first_seen_at,
                               last_observed_at, last_activity_at)
        VALUES (?, ?, 0, ?, ?, 'idle', 0, 0, 0)
        """,
        (f"i-{handle}", pid, f"conv-{handle}", handle),
    )
    conn.execute(
        "INSERT INTO conversations (conversation_id, instance_id, first_seen_at, last_seen_at) VALUES (?, ?, 0, 0)",
        (f"conv-{handle}", f"i-{handle}"),
    )
    if parent_tab is not None:
        conn.execute(
            "INSERT INTO tab_parents (agterm_session, parent_session) VALUES (?, ?)", (f"TAB-{handle}", parent_tab)
        )


def add_lead(conn: sqlite3.Connection, name: str, pid: int) -> None:
    add_session(conn, name, pid)
    conn.execute(
        "INSERT INTO leads (name, display_name, scope, linear_refs, tint, surface, created_at, domains) "
        "VALUES (?, ?, 'scope', '[]', 'blue', ?, 0, '[]')",
        (name, name.capitalize(), name),
    )


def end_session(conn: sqlite3.Connection, handle: str) -> None:
    conn.execute("UPDATE instances SET ended_at = 1 WHERE surface = ?", (handle,))


@pytest.fixture
def team(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Leads alex, boris and cleo, the assistant aide, and boris's worker work-b, all live: boris is the busiest."""
    for n, name in enumerate(("alex", "boris", "cleo"), start=101):
        add_lead(conn, name, n)
    add_session(conn, "aide", 110)
    add_session(conn, "work-b", 111, parent_tab="TAB-boris")
    return conn


def claim(conn: sqlite3.Connection, owner: str, by: Actor, at: int = 0, words: str = "Fix the export") -> int:
    ask_id = add_ask(conn, owner, "aide", words, "tab", ["https://x.example/pr/3"], at).id
    claim_ask_done(conn, ask_id, by, "Export fixed in PR 3, deployed", at)
    return ask_id


def pick(conn: sqlite3.Connection, ask_id: int, config: Config = CONFIG) -> str | None:
    lead = pick_verifier(conn, config, get_ask(conn, ask_id))
    return lead.name if lead else None


def bodies_to(conn: sqlite3.Connection, sent: list[tuple[int, Recipient]], handle: str) -> list[str]:
    return [
        str(conn.execute("SELECT body FROM messages WHERE id = ?", (mid,)).fetchone()[0])
        for mid, r in sent
        if r.handle == handle
    ]


def test_the_verifier_is_neither_owner_claimer_nor_the_claimers_lead(team: sqlite3.Connection) -> None:
    assert pick(team, claim(team, "alex", ALEX)) == "cleo"
    assert pick(team, claim(team, "alex", WORKER_OF_BORIS)) == "cleo"
    assert pick(team, claim(team, "cleo", WORKER_OF_BORIS)) == "alex"
    end_session(team, "alex")
    assert pick(team, claim(team, "cleo", WORKER_OF_BORIS)) is None


def test_fewest_open_verifications_first_then_fewest_workers(team: sqlite3.Connection) -> None:
    by_aide = Actor("aide", "conv-aide", "assistant")
    first = claim(team, "aide", by_aide)
    assert pick(team, first) == "alex"
    team.execute("UPDATE asks SET verifier = 'alex' WHERE id = ?", (first,))
    assert pick(team, claim(team, "aide", by_aide)) == "cleo"
    team.execute("UPDATE asks SET state = 'closed' WHERE id = ?", (first,))
    assert pick(team, claim(team, "aide", by_aide)) == "alex"
    add_session(team, "work-a", 112, parent_tab="TAB-alex")
    assert pick(team, claim(team, "aide", by_aide)) == "cleo"


def test_the_configured_verifier_lead_goes_first_while_it_fits(team: sqlite3.Connection) -> None:
    config = Config(assistant_handle="aide", owner_name="Theo", verifier_lead="cleo")
    ask_id = claim(team, "alex", ALEX)
    team.execute("UPDATE asks SET verifier = 'cleo' WHERE id = ?", (claim(team, "boris", ALEX),))
    assert pick(team, ask_id, config) == "cleo"
    _, sent = assign_verifiers(team, config, [get_ask(team, ask_id)], 0)
    (body,) = bodies_to(team, sent, "cleo")
    assert "bridge picked you as the configured verifier." in body and "fewest" not in body
    assert pick(team, claim(team, "cleo", ALEX), config) == "boris"
    assert pick(team, claim(team, "alex", Actor("cleo", "conv-cleo", "lead")), config) == "boris"
    end_session(team, "cleo")
    assert pick(team, ask_id, config) == "boris"


def test_a_locked_lead_is_not_picked(team: sqlite3.Connection) -> None:
    config = Config(assistant_handle="aide", owner_name="Theo", verifier_lead="boris")
    team.execute("UPDATE surfaces SET workspace_name = '🔒 Private' WHERE surface = 'boris'")
    assert pick(team, claim(team, "alex", ALEX), config) == "cleo"


def test_a_claim_tells_its_verifier_once_with_the_ask_the_claim_and_the_verify_line(team: sqlite3.Connection) -> None:
    ask_id = claim(team, "alex", ALEX, words="Make /status show the new copy")
    picked, sent = assign_verifiers(team, CONFIG, [get_ask(team, ask_id)], 0)
    assert [(a.id, lead and lead.name) for a, lead in picked] == [(ask_id, "cleo")]
    (body,) = bodies_to(team, sent, "cleo")
    assert body.startswith("You verify a claim on Theo's asks: bridge picked you")
    assert VERIFY_RULE in body
    assert f"#{ask_id} (alex's ask), Theo's words: \"Make /status show the new copy\"" in body
    assert "claimed done by alex: Export fixed in PR 3, deployed" in body and "refs: https://x.example/pr/3" in body
    assert f"`guild ask verify {ask_id} --evidence '<what Theo would see, and where>' --how '" in body
    assert get_ask(team, ask_id).verifier == "cleo"
    assert list_ask_events(team, ask_id)[-1]["note"] == "verifier: cleo"


def test_assigning_many_sends_each_verifier_one_message(team: sqlite3.Connection) -> None:
    ids = [claim(team, "alex", ALEX, words=f"Ask {n}") for n in range(4)]
    assert [a.id for a in list_unassigned_claims(team)] == ids
    picked, sent = assign_verifiers(team, CONFIG, list_unassigned_claims(team), 0)
    assert [lead and lead.name for _, lead in picked] == ["cleo", "boris", "cleo", "boris"]
    assert [r.handle for _, r in sent] == ["cleo", "boris"]
    (to_boris,) = bodies_to(team, sent, "boris")
    assert "You verify 2 claims" in to_boris and f"#{ids[1]} " in to_boris and f"#{ids[3]} " in to_boris
    assert list_unassigned_claims(team) == []


def test_no_live_lead_leaves_the_claim_to_the_assistant(team: sqlite3.Connection) -> None:
    for lead in ("boris", "cleo"):
        end_session(team, lead)
    ask_id = claim(team, "alex", ALEX)
    picked, sent = assign_verifiers(team, CONFIG, [get_ask(team, ask_id)], 0)
    assert (picked, sent) == ([(get_ask(team, ask_id), None)], [])
    assert get_ask(team, ask_id).verifier is None


def test_a_verifier_that_could_not_be_told_is_not_kept(team: sqlite3.Connection) -> None:
    team.execute(
        "INSERT INTO instances (instance_id, pid, proc_start_ms, conversation_id, surface, status, first_seen_at, "
        "last_observed_at) VALUES ('i-cleo-2', 120, 1, 'conv-cleo-2', 'cleo', 'idle', 0, 0)"
    )
    ask_id = claim(team, "alex", ALEX)
    picked, sent = assign_verifiers(team, CONFIG, [get_ask(team, ask_id)], 0)
    assert (picked[0][1], sent) == (None, [])
    assert get_ask(team, ask_id).verifier is None
    assert list_ask_events(team, ask_id)[-1]["note"] == "verifier cleo could not be told; none now"


def test_a_claim_with_a_verifier_does_not_remind_its_owner(team: sqlite3.Connection) -> None:
    with_verifier = claim(team, "alex", WORKER_OF_BORIS)
    assign_verifiers(team, CONFIG, [get_ask(team, with_verifier)], 0)
    without = claim(team, "alex", WORKER_OF_BORIS)
    assert [a.id for a in find_overdue_asks(team, HOUR, 4 * HOUR, HOUR)] == [without]


def test_a_gone_verifier_is_replaced_at_once_and_a_day_later_the_verifier_is_reminded_once(
    team: sqlite3.Connection,
) -> None:
    kept = claim(team, "alex", ALEX)
    gone = claim(team, "alex", ALEX)
    assign_verifiers(team, CONFIG, [get_ask(team, kept), get_ask(team, gone)], 0)
    assert [get_ask(team, i).verifier for i in (kept, gone)] == ["cleo", "boris"]
    end_session(team, "boris")

    sent = send_ask_nags(team, CONFIG, HOUR, owner_off=False)
    (told,) = bodies_to(team, sent, "cleo")
    assert told.startswith("You verify a claim") and f"#{gone} " in told and f"#{kept} " not in told
    assert get_ask(team, gone).verifier == "cleo"
    assert [e["note"] for e in list_ask_events(team, gone)][-2:] == [
        "verifier boris's tab is gone or locked", "verifier: cleo",
    ]  # fmt: skip
    assert send_ask_nags(team, CONFIG, 2 * HOUR, owner_off=False) == []

    sent = send_ask_nags(team, CONFIG, DAY, owner_off=False)
    (body,) = bodies_to(team, sent, "cleo")
    assert body.startswith("To verify, claimed done over 24h ago and still not verified; you are their verifier")
    assert f"#{kept} " in body and f"#{gone} " not in body
    assert send_ask_nags(team, CONFIG, DAY + HOUR - 1, owner_off=False) == []
    (body,) = bodies_to(team, send_ask_nags(team, CONFIG, DAY + HOUR, owner_off=False), "cleo")
    assert f"#{gone} " in body and f"#{kept} " not in body
    assert list_ask_events(team, gone)[-1]["note"] == "sent to cleo to verify"
    assert send_ask_nags(team, CONFIG, DAY + 2 * HOUR, owner_off=False) == []
    assert send_ask_nags(team, CONFIG, 3 * DAY, owner_off=False) == []


def test_a_verifier_between_sessions_keeps_its_claims_for_a_while(team: sqlite3.Connection) -> None:
    ask_id = claim(team, "alex", ALEX)
    assign_verifiers(team, CONFIG, [get_ask(team, ask_id)], 0)
    team.execute("UPDATE instances SET ended_at = ? WHERE surface = 'cleo'", (HOUR,))
    assert send_ask_nags(team, CONFIG, HOUR + 9 * 60_000, owner_off=False) == []
    assert get_ask(team, ask_id).verifier == "cleo"
    assert [r.handle for _, r in send_ask_nags(team, CONFIG, HOUR + 10 * 60_000, owner_off=False)] == ["boris"]


def test_a_paused_claim_keeps_its_gone_verifier_until_the_pause_ends(team: sqlite3.Connection) -> None:
    ask_id = claim(team, "alex", ALEX)
    assign_verifiers(team, CONFIG, [get_ask(team, ask_id)], 0)
    wait_ask(team, ask_id, ALEX, None, "the release ships on Monday", HOUR, until=2 * DAY)
    end_session(team, "cleo")
    assert send_ask_nags(team, CONFIG, DAY, owner_off=False) == []
    assert get_ask(team, ask_id).verifier == "cleo"


def test_a_verifier_gone_after_its_reminder_is_still_replaced_or_the_owner_hears_again(
    team: sqlite3.Connection,
) -> None:
    ask_id = claim(team, "alex", ALEX)
    assign_verifiers(team, CONFIG, [get_ask(team, ask_id)], 0)
    assert [r.handle for _, r in send_ask_nags(team, CONFIG, DAY, owner_off=False)] == ["cleo"]
    end_session(team, "cleo")
    assert [r.handle for _, r in send_ask_nags(team, CONFIG, DAY + HOUR, owner_off=False)] == ["boris"]
    assert get_ask(team, ask_id).verifier == "boris"
    end_session(team, "boris")
    sent = send_ask_nags(team, CONFIG, DAY + 2 * HOUR, owner_off=False)
    assert get_ask(team, ask_id).verifier is None
    assert [r.handle for _, r in sent] == ["alex"]
    assert f"#{ask_id} done_claimed" in bodies_to(team, sent, "alex")[0]
    sent = send_ask_nags(team, CONFIG, 2 * DAY + HOUR, owner_off=False)
    assert "no live lead can verify them" in bodies_to(team, sent, "aide")[0]
    assert list_ask_events(team, ask_id)[-1]["note"] == "sent to the assistant to verify"


def test_a_reminder_that_reaches_no_one_leaves_no_verifier(team: sqlite3.Connection) -> None:
    ask_id = claim(team, "alex", ALEX)
    assign_verifiers(team, CONFIG, [get_ask(team, ask_id)], 0)
    team.execute(
        "INSERT INTO instances (instance_id, pid, proc_start_ms, conversation_id, surface, status, first_seen_at, "
        "last_observed_at) VALUES ('i-cleo-2', 120, 1, 'conv-cleo-2', 'cleo', 'idle', 0, 0)"
    )
    sent = send_ask_nags(team, CONFIG, DAY, owner_off=False)
    assert get_ask(team, ask_id).verifier is None
    assert "aide" in [r.handle for _, r in sent]


def test_a_day_later_with_no_lead_live_the_assistant_has_it_as_before(team: sqlite3.Connection) -> None:
    ask_id = claim(team, "alex", ALEX)
    assign_verifiers(team, CONFIG, [get_ask(team, ask_id)], 0)
    for lead in ("boris", "cleo"):
        end_session(team, lead)
    sent = send_ask_nags(team, CONFIG, DAY, owner_off=False)
    assert [r.handle for _, r in sent] == ["alex", "aide"]
    assert "no live lead can verify them" in bodies_to(team, sent, "aide")[0]
    assert get_ask(team, ask_id).verifier is None
    assert list_ask_events(team, ask_id)[-1]["note"] == "sent to the assistant to verify"


def test_a_reopen_clears_the_verifier_and_a_new_claim_picks_again(team: sqlite3.Connection) -> None:
    ask_id = claim(team, "alex", ALEX)
    assign_verifiers(team, CONFIG, [get_ask(team, ask_id)], 0)
    verify_ask(team, ask_id, Actor("cleo", "conv-cleo", "lead"), EVIDENCE, HOW, HOUR)
    assert reopen_ask(team, ask_id, "aide", "the export still drops a column", 2 * HOUR).verifier is None
    claim_ask_done(team, ask_id, ALEX, "Export fixed again in PR 4", 3 * HOUR)
    assert [a.id for a in list_unassigned_claims(team)] == [ask_id]


def owner_wait(
    conn: sqlite3.Connection, ask_id: int, at: int, on: str = "owner", why: str = "Theo picks the plan"
) -> None:
    wait_ask(conn, ask_id, ALEX, on, why, at)


def test_an_ask_waiting_on_the_owner_with_no_card_is_told_to_the_assistant_once_per_wait(
    team: sqlite3.Connection,
) -> None:
    no_card = add_ask(team, "alex", "aide", "Pick the export format", "tab", [], 0).id
    owner_wait(team, no_card, HOUR)
    approval = add_ask(team, "alex", "aide", "Ship the new copy", "tab", [], 0).id
    owner_wait(team, approval, HOUR, "approval", "theo approves the wording")
    others = add_ask(team, "alex", "aide", "Open the firewall port", "tab", [], 0).id
    owner_wait(team, others, HOUR, "approval", "Security approves the port")
    carded = add_ask(team, "alex", "aide", "Choose the vendor", "tab", [], 0).id
    owner_wait(team, carded, HOUR)
    add_post(team, Party("conv-alex", "alex"), "decision",
             f"Ask #{carded}: which vendor?", "", [], ["A", "B"], HOUR)  # fmt: skip

    assert send_card_notices(team, CONFIG, HOUR + 14 * 60_000) == []
    sent = send_card_notices(team, CONFIG, HOUR + 15 * 60_000)
    (body,) = bodies_to(team, sent, "aide")
    assert body.startswith("Asks waiting on Theo with no open card on Theo's feed: post one")
    assert [line.split(" ")[0] for line in body.splitlines()[1:]] == [f"#{no_card}", f"#{approval}"]
    assert list_ask_events(team, no_card)[-1]["note"] == NO_CARD
    assert send_card_notices(team, CONFIG, 2 * HOUR) == []

    owner_wait(team, no_card, 3 * HOUR, why="Theo picks the format after the call")
    sent = send_card_notices(team, CONFIG, 4 * HOUR)
    assert [line.split(" ")[0] for line in bodies_to(team, sent, "aide")[0].splitlines()[1:]] == [f"#{no_card}"]


def test_a_card_older_than_the_questions_column_shows_does_not_count(team: sqlite3.Connection) -> None:
    ask_id = add_ask(team, "alex", "aide", "Choose the vendor", "tab", [], 0).id
    owner_wait(team, ask_id, 4 * DAY)
    add_post(team, Party("conv-alex", "alex"), "decision", f"Ask #{ask_id}: which vendor?", "", [], [], 0)
    assert [r.handle for _, r in send_card_notices(team, CONFIG, 4 * DAY + HOUR)] == ["aide"]


def test_a_withdrawn_card_no_longer_counts(team: sqlite3.Connection) -> None:
    ask_id = add_ask(team, "alex", "aide", "Choose the vendor", "tab", [], 0).id
    owner_wait(team, ask_id, 0)
    party = Party("conv-alex", "alex")
    card = add_post(team, party, "blocked", "Vendor", f"Needs Theo for ask #{ask_id}", [], [], 0)
    assert send_card_notices(team, CONFIG, HOUR) == []
    withdraw_post(team, party, card, HOUR)
    assert [r.handle for _, r in send_card_notices(team, CONFIG, 2 * HOUR)] == ["aide"]


def verified_by(conn: sqlite3.Connection, verifier: Actor, at: int, n: int) -> list[int]:
    ids: list[int] = []
    for _ in range(n):
        ask_id = claim(conn, "alex", ALEX, at)
        verify_ask(conn, ask_id, verifier, EVIDENCE, HOW, at)
        ids.append(ask_id)
    return ids


def test_the_assistant_gets_one_daily_sample_of_three_asks_leads_verified(team: sqlite3.Connection) -> None:
    by_lead = verified_by(team, Actor("boris", "conv-boris", "lead"), DAY, 4)
    verified_by(team, Actor("aide", "conv-aide", "assistant"), DAY, 1)
    old = verified_by(team, Actor("boris", "conv-boris", "lead"), 0, 1)
    sent = send_spot_check(team, CONFIG, DAY + HOUR)
    (body,) = bodies_to(team, sent, "aide")
    assert body.startswith("Spot-check: 3 of the 4 asks leads verified in the last day, picked at random")
    sampled = [int(line.split(" ")[0][1:]) for line in body.splitlines() if line.startswith("#")]
    assert len(sampled) == 3 and set(sampled) <= set(by_lead) and old[0] not in sampled
    assert f"  evidence: {EVIDENCE}" in body and f"  how: {HOW}" in body
    assert all(list_ask_events(team, i)[-1]["note"] == SPOT_CHECKED for i in sampled)
    verified_by(team, Actor("boris", "conv-boris", "lead"), DAY + 2 * HOUR, 1)
    assert send_spot_check(team, CONFIG, 2 * DAY) == []
    assert [r.handle for _, r in send_spot_check(team, CONFIG, 2 * DAY + HOUR)] == ["aide"]
    assert send_spot_check(team, CONFIG, 5 * DAY) == []


def test_the_spot_check_leaves_out_evidence_from_a_locked_tab(team: sqlite3.Connection) -> None:
    verified_by(team, Actor("boris", "conv-boris", "lead"), DAY, 2)
    team.execute("UPDATE surfaces SET workspace_name = '🔒 Private' WHERE surface = 'boris'")
    assert send_spot_check(team, CONFIG, DAY + HOUR) == []


def test_cli_done_tells_the_verifier_and_assign_verifiers_takes_the_rest(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    assert team.enroll().returncode == 0
    assert team.enroll("Boris", "old-b").returncode == 0
    for words in ("Fix the export", "Answer the vendor"):
        assert team.guild("ask", "add", "alex", "--words", words, pid=team.a).returncode == 0
    with bridge_env.connect() as db:
        claim_ask_done(db, 2, Actor("alex", "conv-a", "lead"), "Sent the vendor the answer", 0)

    done = team.guild("ask", "done", "1", "--claim", "Export fixed in PR 3, deployed", pid=team.a)
    assert done.returncode == 0, done.stderr
    assert "boris verifies it and is told" in done.stdout
    with bridge_env.connect() as db:
        body = str(db.execute("SELECT body FROM messages WHERE from_handle = 'bridge' ORDER BY id DESC").fetchone()[0])
    assert body.startswith("You verify a claim") and "guild ask verify 1 --evidence" in body
    assert "verifier: boris" in team.guild("ask", "show", "1").stdout

    assigned = team.guild("ask", "assign-verifiers", pid=team.a)
    assert (assigned.returncode, assigned.stdout) == (0, "#2 → boris\n"), assigned.stderr
    assert team.guild("ask", "assign-verifiers", pid=team.a).stdout == "no claim without a verifier\n"
    assert "verify" in team.guild("ask", "--help").stdout


def test_cli_assign_verifiers_is_for_a_lead_or_the_assistant(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    refused = team.guild("ask", "assign-verifiers", pid=team.b)
    assert refused.returncode == 2 and "only a lead or the assistant assigns verifiers" in refused.stderr


def lock(conn: sqlite3.Connection, surface: str) -> None:
    conn.execute("UPDATE surfaces SET workspace_name = '🔒 Private' WHERE surface = ?", (surface,))


def all_bodies(conn: sqlite3.Connection, sent: list[tuple[int, Recipient]]) -> str:
    return "\n".join(str(conn.execute("SELECT body FROM messages WHERE id = ?", (m,)).fetchone()[0]) for m, _ in sent)


def test_a_locked_claimants_claim_never_reaches_a_verifier(team: sqlite3.Connection) -> None:
    shown = claim(team, "alex", WORKER_OF_BORIS)
    _, sent = assign_verifiers(team, CONFIG, [get_ask(team, shown)], 0)
    assert CLAIM in all_bodies(team, sent)
    hidden = claim(team, "alex", WORKER_OF_BORIS, words="Fix the import")
    lock(team, "work-b")
    assert assign_verifiers(team, CONFIG, [get_ask(team, hidden)], 0) == ([], [])
    assert get_ask(team, hidden).verifier is None
    sent = send_ask_nags(team, CONFIG, DAY, owner_off=False)
    assert sent and CLAIM not in all_bodies(team, sent)
    assert "cleo" not in [r.handle for _, r in sent]


@pytest.mark.parametrize("locked", [False, True])
def test_the_sample_checks_the_verifying_conversation_after_its_lead_retires(
    team: sqlite3.Connection, locked: bool
) -> None:
    verified_by(team, Actor("boris", "conv-boris", "lead"), DAY, 1)
    retire_lead(team, "boris", DAY)
    if locked:
        lock(team, "boris")
    sent = send_spot_check(team, CONFIG, DAY + HOUR)
    assert (EVIDENCE in all_bodies(team, sent)) is not locked


@pytest.mark.parametrize("state", ["unlocked", "locked", "unknown"])
def test_a_card_notice_passes_on_the_wait_reason_only_from_a_known_unlocked_tab(
    team: sqlite3.Connection, state: str
) -> None:
    ask_id = add_ask(team, "alex", "aide", "Pick the export format", "tab", [], 0).id
    wait_ask(team, ask_id, WORKER_OF_BORIS, "owner", "the private reason from the worker", 0)
    if state == "locked":
        lock(team, "work-b")
    if state == "unknown":
        team.execute("UPDATE asks SET wait_conversation = NULL WHERE id = ?", (ask_id,))
    (body,) = bodies_to(team, send_card_notices(team, CONFIG, HOUR), "aide")
    assert f"#{ask_id} alex" in body
    assert ("the private reason from the worker" in body) is (state == "unlocked")


def test_the_sample_leaves_out_an_ask_verified_before_its_verifying_conversation_was_stored(
    team: sqlite3.Connection,
) -> None:
    old, new = verified_by(team, Actor("boris", "conv-boris", "lead"), DAY, 2)
    team.execute("UPDATE asks SET evidence_conversation = NULL WHERE id = ?", (old,))
    body = all_bodies(team, send_spot_check(team, CONFIG, DAY + HOUR))
    assert f"#{new} " in body and f"#{old} " not in body


def test_at_the_reminder_hour_a_verifier_between_sessions_keeps_its_claim_for_a_while(
    team: sqlite3.Connection,
) -> None:
    ask_id = claim(team, "alex", ALEX)
    assign_verifiers(team, CONFIG, [get_ask(team, ask_id)], 0)
    team.execute("UPDATE instances SET ended_at = ? WHERE surface = 'cleo'", (DAY - 60_000,))
    assert send_ask_nags(team, CONFIG, DAY, owner_off=False) == []
    assert get_ask(team, ask_id).verifier == "cleo"
    sent = send_ask_nags(team, CONFIG, DAY - 60_000 + 10 * 60_000, owner_off=False)
    assert "boris" in [r.handle for _, r in sent] and get_ask(team, ask_id).verifier == "boris"


@pytest.mark.parametrize("retired", [False, True])
def test_an_ask_of_a_retired_owner_is_neither_flagged_nor_sampled(team: sqlite3.Connection, retired: bool) -> None:
    waits = add_ask(team, "alex", "aide", "Pick the export format", "tab", [], 0).id
    owner_wait(team, waits, 0)
    verified_by(team, Actor("boris", "conv-boris", "lead"), DAY, 1)
    if retired:
        retire_lead(team, "alex", 0)
    notice = all_bodies(team, send_card_notices(team, CONFIG, HOUR))
    sample = all_bodies(team, send_spot_check(team, CONFIG, DAY + HOUR))
    assert ("Pick the export format" in notice) is not retired
    assert ("Fix the export" in sample) is not retired


def queue_pr(conn: sqlite3.Connection, repo: str, number: int) -> None:
    conn.execute(
        "INSERT INTO review_prs (ref, repo, number, title, url, draft, head, head_oid, base, loc, updated_at, "
        "first_seen_at, fetched_at) VALUES (?, ?, ?, 't', ?, 0, 'b', 'o', 'main', 1, 0, 0, 0)",
        (f"pr:{repo}#{number}", repo, number, f"https://github.com/{repo}/pull/{number}"),
    )


def test_a_card_tied_by_ask_or_by_link_counts_and_a_queued_pr_review_needs_none(team: sqlite3.Connection) -> None:
    def ask(words: str, why: str = "Theo picks", refs: list[str] | None = None) -> int:
        ask_id = add_ask(team, "alex", "aide", words, "tab", refs or [], 0).id
        owner_wait(team, ask_id, HOUR, "approval", why)
        return ask_id

    by_flag = ask("Pick the export format")
    add_post(team, Party("conv-aide", "aide"), "decision", "Which format?", "", [], ["A", "B"], HOUR, False, by_flag)
    linked = ask("Choose the vendor")
    card = add_post(team, Party("conv-aide", "aide"), "decision", "Which vendor?", "", [], ["A", "B"], HOUR)
    link_card_to_ask(team, linked, card)
    queue_pr(team, "Acme/Web", 12)
    queue_pr(team, "Acme/Infra", 3)
    reviewed = ask(
        "Delete the old gate",
        "Theo reviews https://github.com/Acme/Web/pull/12 and https://github.com/Acme/Infra/pull/3",
    )
    by_ref = ask("Ship the fix", "Theo reviews it", ["https://github.com/Acme/Web/pull/12"])
    missing = ask(
        "Drop the flag", "Theo reviews https://github.com/Acme/Web/pull/12 and https://github.com/Acme/Web/pull/99"
    )
    plain = ask("Approve the copy")

    sent = send_card_notices(team, CONFIG, HOUR + 15 * 60_000)
    flagged = [line.split(" ")[0] for line in bodies_to(team, sent, "aide")[0].splitlines()[1:]]
    assert flagged == [f"#{missing}", f"#{plain}"], "tied cards and fully queued PR reviews are not flagged"
    assert by_flag not in (linked, reviewed, by_ref)

    resolved = add_post(team, Party("conv-aide", "aide"), "decision", "Old?", "", [], ["A", "B"], HOUR)
    team.execute("UPDATE user_items SET resolved_at = 1 WHERE id = ?", (resolved,))
    with pytest.raises(AskError, match="no open question or blocker card"):
        link_card_to_ask(team, plain, resolved)
