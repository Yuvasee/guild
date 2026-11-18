import json
import sqlite3
from collections.abc import Generator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest
from conftest import BridgeEnv, SpawnProcess
from test_leads import Team

from bridge.asks import add_ask
from bridge.capacity import (
    BUSY,
    FINISHED,
    IDLE_FOR_MS,
    SIGNAL_REPEAT_MS,
    WAITS,
    assess_leads,
    convert_load_to_signal,
    convert_loads_to_team_line,
    map_chunk_to_verdict,
    park_room,
    read_owner_presence,
    record_owner_off,
    record_owner_on,
    send_capacity_signals,
    send_idle_work_nudges,
    send_stale_focus_nudges,
)
from bridge.config import Config
from bridge.db import connect
from bridge.leads import LeadWork, lead_dir, set_lead_domains, write_notes
from bridge.procinfo import ProcessRow
from bridge.reviews import PUBLISHED, WAITING_FOR_OWNER, WAITING_FOR_YOU
from bridge.workers import queue_worker_launch

CONFIG = Config(owner_name="the owner", assistant_handle="aide", work_timezone="UTC")
WORKDAY_NOON = int(datetime(2026, 11, 9, 12, 0, tzinfo=UTC).timestamp() * 1000)
WEEKEND_NOON = int(datetime(2026, 11, 7, 12, 0, tzinfo=UTC).timestamp() * 1000)
HOUR = 3_600_000


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[sqlite3.Connection]:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    c = connect(tmp_path / "bridge.db")
    yield c
    c.close()


def add_session(conn: sqlite3.Connection, handle: str, tab: str, status: str, since: int, pid: int) -> None:
    conn.execute(
        "INSERT INTO surfaces (surface, agterm_session, handle, updated_at) VALUES (?, ?, ?, 0)", (handle, tab, handle)
    )
    conn.execute(
        """
        INSERT INTO instances (instance_id, pid, proc_start_ms, conversation_id, surface, status, first_seen_at,
                               last_observed_at, last_activity_at)
        VALUES (?, ?, 0, ?, ?, ?, 0, 0, ?)
        """,
        (f"i-{handle}", pid, f"conv-{handle}", handle, status, since),
    )


def add_lead(conn: sqlite3.Connection, name: str, domains: list[str], status: str = "idle", since: int = 0) -> None:
    add_session(conn, name, f"TAB-{name}", status, since, pid=100 + len(name))
    conn.execute(
        "INSERT INTO leads (name, display_name, scope, linear_refs, tint, surface, created_at, domains) "
        "VALUES (?, ?, 'scope', '[]', 'blue', ?, 0, ?)",
        (name, name.capitalize(), name, json.dumps(domains)),
    )


def add_issue(conn: sqlite3.Connection, key: str, state_type: str, assignee: str | None = None) -> None:
    data = {"title": f"title {key}", "state": state_type.capitalize(), "state_type": state_type, "assignee": assignee}
    conn.execute(
        "INSERT INTO enrichment (ref, data, fetched_at, ok_at) VALUES (?, ?, 0, 0)",
        (f"issue:{key}", json.dumps(data)),
    )


def add_pr(conn: sqlite3.Connection, ref: str, state: str) -> None:
    conn.execute(
        "INSERT INTO enrichment (ref, data, fetched_at, ok_at) VALUES (?, ?, 0, 0)", (ref, json.dumps({"state": state}))
    )


def tree(workers: dict[str, int]) -> dict[str, object]:
    sessions = [
        {"id": f"W-{lead}-{n}", "name": f"task {n}", "parent": f"TAB-{lead}", "status": "completed"}
        for lead, count in workers.items()
        for n in range(count)
    ]
    return {"workspaces": [{"name": "TEAM", "sessions": sessions}]}


def test_the_night_and_morning_words_decide_and_the_work_clock_covers_silence(conn: sqlite3.Connection) -> None:
    assert not read_owner_presence(conn, CONFIG, WORKDAY_NOON).off
    assert read_owner_presence(conn, CONFIG, WEEKEND_NOON).off
    record_owner_off(conn, "guild dream", WORKDAY_NOON - HOUR)
    assert read_owner_presence(conn, CONFIG, WORKDAY_NOON).off
    record_owner_on(conn, "guild day", WORKDAY_NOON - 1)
    assert not read_owner_presence(conn, CONFIG, WORKDAY_NOON).off
    record_owner_off(conn, "guild dream --wait-all", WORKDAY_NOON)
    assert read_owner_presence(conn, CONFIG, WORKDAY_NOON + 1).off
    later_weekend = WEEKEND_NOON + 7 * 24 * HOUR
    presence = read_owner_presence(conn, CONFIG, later_weekend)
    assert presence.off and "no night or morning word in 24 h" in presence.why


def test_a_chunk_waits_only_when_no_ref_is_busy() -> None:
    queue: dict[str, dict[str, object]] = {
        "pr:o/r#1": {"group": WAITING_FOR_YOU, "state": WAITING_FOR_OWNER, "reviews_gap": None},
        "pr:o/r#2": {"group": PUBLISHED, "state": "published"},
        "pr:o/r#3": {"group": WAITING_FOR_YOU, "state": WAITING_FOR_OWNER, "reviews_gap": "Claude review missing"},
    }
    enrichment: dict[str, dict[str, object]] = {
        "pr:o/r#4": {"state": "MERGED"},
        "pr:o/r#5": {"state": "CLOSED"},
        "issue:PROJ-1": {"state": "In Progress", "state_type": "started", "linked_complete": True, "linked_prs": [
            {"ref": "pr:o/r#9", "status": "merged"}]},
        "issue:PROJ-4": {"state": "In Progress", "state_type": "started", "linked_complete": False, "linked_prs": [
            {"ref": "pr:o/r#9", "status": "merged"}]},
        "issue:PROJ-2": {"state": "Todo", "state_type": "unstarted", "linked_prs": []},
        "issue:PROJ-3": {"state": "Done", "state_type": "completed"},
    }  # fmt: skip

    def verdict(*refs: str, waited: dict[str, str] | None = None) -> str:
        return map_chunk_to_verdict(LeadWork("c", refs), queue, enrichment, waited or {}, CONFIG).verdict

    assert verdict("pr:o/r#1", "pr:o/r#2", "pr:o/r#4") == WAITS
    assert verdict("pr:o/r#1", "pr:o/r#3") == BUSY
    assert verdict("issue:PROJ-1") == WAITS
    assert verdict("issue:PROJ-4") == BUSY
    assert verdict("issue:PROJ-2") == BUSY
    assert verdict("issue:PROJ-2", waited={"issue:PROJ-2": "ask #4 waits on approval"}) == WAITS
    assert verdict("pr:o/r#5", "issue:PROJ-3") == FINISHED
    assert verdict("pr:o/r#77") == BUSY
    assert verdict() == BUSY


def test_an_idle_lead_under_the_planned_cap_gets_unclaimed_items_of_its_domains(conn: sqlite3.Connection) -> None:
    now = WORKDAY_NOON
    add_lead(conn, "alex", ["data", "P-PROJ-41"], since=now - IDLE_FOR_MS - 1)
    add_lead(conn, "charlie", ["data"], status="active", since=now)
    conn.execute(
        "INSERT INTO project_priority (position, name, issue_keys, set_at, set_by) VALUES "
        '(0, \'data\', \'["PROJ-1", "PROJ-2", "PROJ-3", "PROJ-4", "PROJ-5"]\', 0, \'aide\'), '
        "(1, 'other', '[\"PROJ-9\"]', 0, 'aide')"
    )
    add_issue(conn, "PROJ-1", "started", "Owner")
    add_issue(conn, "PROJ-2", "completed")
    add_issue(conn, "PROJ-3", "backlog")
    add_issue(conn, "PROJ-4", "unstarted")
    add_issue(conn, "PROJ-9", "backlog")
    conn.execute(
        "INSERT INTO lead_work (lead, name, refs, created_at, updated_at) VALUES ('charlie', 'x', ?, 0, 0)",
        (json.dumps(["issue:PROJ-4"]),),
    )
    loads = {load.lead.name: load for load in assess_leads(conn, CONFIG, now, tree({"alex": 3, "charlie": 1}))}
    alex = loads["alex"]
    assert alex.reason_key == "idle" and alex.workers == 3 and alex.load == "idle"
    assert [s.key for s in alex.suggestions] == ["PROJ-3", "PROJ-1"]
    assert alex.suggestions[0].shared_with == ("charlie",)
    assert any("P-PROJ-41" in note for note in alex.notes)
    assert loads["charlie"].reason_key is None and loads["charlie"].load == "working"
    text = convert_load_to_signal(alex)
    assert "3/5 workers" in text and 'PROJ-3 "title PROJ-3" (data, Backlog, unassigned; shared with charlie)' in text
    assert convert_loads_to_team_line(list(loads.values())) == ("team load: 4/10 worker slots, 40%; target 80%")
    at_planned_cap = assess_leads(conn, CONFIG, now, tree({"alex": 4}))[0]
    assert at_planned_cap.reason_key is None and "the last slot stays for urgent work" in str(
        at_planned_cap.not_flagged_why
    )
    assert assess_leads(conn, CONFIG, now, None)[0].reason_key is None
    queue_worker_launch({"parent": "TAB-alex", "name": "next", "cwd": "/", "command": "x", "requested_by": "alex"})
    assert assess_leads(conn, CONFIG, now, tree({"alex": 3}))[0].workers == 4


def test_signals_go_to_the_assistant_once_per_reason_and_never_while_the_owner_is_off(
    conn: sqlite3.Connection,
) -> None:
    now = WORKDAY_NOON
    add_session(conn, "aide", "TAB-X", "idle", 0, pid=999)
    add_lead(conn, "alex", [], since=now - IDLE_FOR_MS)

    def send(at: int, workers: int = 0) -> int:
        return len(send_capacity_signals(conn, CONFIG, at, assess_leads(conn, CONFIG, at, tree({"alex": workers}))))

    record_owner_off(conn, "guild dream", now - HOUR)
    assert send(now) == 0
    record_owner_on(conn, "guild day", now - 1)
    assert send(now) == 1
    body = conn.execute("SELECT body FROM messages ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert body.startswith("Alex has room for planned work") and "guild lead domains alex" in body
    assert send(now + HOUR) == 0
    park_room(conn, "alex", now + 72 * HOUR)
    assert send(now + 48 * HOUR) == 0, "a lead parked on purpose gets no room notice"
    park_room(conn, "alex", None)
    conn.execute(
        "INSERT INTO lead_work (lead, name, refs, created_at, updated_at) VALUES ('alex', 'ended', ?, 0, 0)",
        (json.dumps(["issue:PROJ-3"]),),
    )
    add_issue(conn, "PROJ-3", "completed")
    assert send(now + HOUR) == 0, "a finished chunk alone is no new reason"
    conn.execute(
        "INSERT INTO lead_work (lead, name, refs, created_at, updated_at) VALUES ('alex', 'shipped', ?, 0, 0)",
        (json.dumps(["pr:o/r#4"]),),
    )
    conn.execute(
        "INSERT INTO enrichment (ref, data, fetched_at, ok_at) VALUES ('pr:o/r#4', '{\"state\": \"MERGED\"}', 0, 0)"
    )
    assert send(now + HOUR) == 1
    assert send(now + HOUR + SIGNAL_REPEAT_MS - 1) == 0
    conn.execute("UPDATE enrichment SET data = '{\"state\": \"CLOSED\"}' WHERE ref = 'pr:o/r#4'")
    conn.execute(
        "INSERT INTO enrichment (ref, data, fetched_at, ok_at) VALUES ('pr:o/r#5', '{\"state\": \"MERGED\"}', 0, 0)"
    )
    conn.execute("UPDATE lead_work SET refs = '[\"pr:o/r#5\"]' WHERE name = 'shipped'")
    assert send(now + HOUR + SIGNAL_REPEAT_MS - 1) == 1, "another wait cause is a changed reason"
    assert send(now + 4 * HOUR + SIGNAL_REPEAT_MS) == 1
    assert send(now + 9 * HOUR, workers=4) == 0


def test_an_active_lead_whose_chunks_all_ended_has_no_room(conn: sqlite3.Connection) -> None:
    add_lead(conn, "alex", [], status="active", since=WORKDAY_NOON)
    add_issue(conn, "PROJ-3", "completed")
    conn.execute(
        "INSERT INTO lead_work (lead, name, refs, created_at, updated_at) VALUES ('alex', 'ended', ?, 0, 0)",
        (json.dumps(["issue:PROJ-3"]),),
    )
    load = assess_leads(conn, CONFIG, WORKDAY_NOON, tree({"alex": 0}))[0]
    assert load.reason_key is None and load.load == "working"


def test_domains_are_set_by_the_lead_or_assistant_and_shown_to_anyone(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    assert team.enroll().returncode == 0
    done = team.guild("lead", "domains", "alex", "data", "P-PROJ-41", pid=team.a)
    assert done.returncode == 0 and "Alex: data, P-PROJ-41" in done.stdout
    assert "'data' is neither a priority project" in done.stderr
    refused = team.guild("lead", "domains", "alex", "other", pid=team.b)
    assert refused.returncode == 2 and "only aide or Alex" in refused.stderr
    shown = team.guild("lead", "domains", "alex", pid=team.b)
    assert "Alex: data, P-PROJ-41" in shown.stdout
    day = team.guild("day")
    assert "team load: 0/5 worker slots, 0%; target 80%" in day.stdout
    assert "Alex: " in day.stdout and "0/5 workers; next: nothing to suggest" in day.stdout
    with bridge_env.connect() as conn:
        assert conn.execute("SELECT owner_on_by FROM owner_presence").fetchone()[0] == "guild day"
    capacity = team.guild("capacity")
    assert capacity.returncode == 0 and "dry run: nothing is sent" in capacity.stdout


def test_domains_are_capped(conn: sqlite3.Connection) -> None:
    add_lead(conn, "alex", [])
    assert set_lead_domains(conn, "alex", [" data  readiness ", "data readiness"]).domains == ("data readiness",)
    with pytest.raises(Exception, match="at most"):
        set_lead_domains(conn, "alex", ["x" * 65])


def test_a_domain_the_config_names_a_parallel_stream_reads_so_with_its_own_backlog(
    conn: sqlite3.Connection,
) -> None:
    now = WORKDAY_NOON
    add_lead(conn, "eli", ["tooling"], since=now - IDLE_FOR_MS - 1)
    add_lead(conn, "quinn", ["Tooling", "data"], since=now - IDLE_FOR_MS - 1)
    add_lead(conn, "fay", ["infra"], since=now - IDLE_FOR_MS - 1)
    conn.execute(
        "INSERT INTO project_priority (position, name, issue_keys, set_at, set_by) VALUES "
        "(0, 'data', '[\"PROJ-3\"]', 0, 'aide')"
    )
    add_issue(conn, "PROJ-3", "backlog")
    queue = "QUEUE after these: enabling-PR check; judge dream rules; " + "more items; " * 40
    write_notes(lead_dir("eli"), f"# Eli\nRules: keep it small.\n{queue}\nOther line\n")

    add_lead(conn, "denis", ["partner-export"], since=now - IDLE_FOR_MS - 1)
    config = replace(CONFIG, parallel_streams=("tooling", "infra"))

    loads = {load.lead.name: load for load in assess_leads(conn, config, now, tree({}))}
    eli, quinn, fay, denis = (convert_load_to_signal(loads[n]) for n in ("eli", "quinn", "fay", "denis"))

    assert "'partner-export' is not a priority project" in denis, "only a named stream is one"
    assert "parallel stream" not in denis
    assert "tooling is a parallel stream outside the cycle's priority projects" in eli
    assert "Its own backlog (from its notes): QUEUE after these: enabling-PR check; judge dream rules;" in eli
    backlog = eli.split("Its own backlog (from its notes): ", 1)[1]
    assert len(backlog) <= 300 and backlog.endswith("…")
    assert "Next in its domains: 1) PROJ-3" in quinn and "Tooling is a parallel stream" in quinn
    assert "It loads itself from its own backlog; ask it for its list." in fay
    assert all("not a priority project" not in text for text in (eli, quinn, fay))


def test_an_idle_lead_with_work_that_waits_on_nobody_is_told_once_per_idle_spell(conn: sqlite3.Connection) -> None:
    now = WORKDAY_NOON
    add_lead(conn, "alex", [], since=now - IDLE_FOR_MS)

    def nudge(at: int, workers: int = 0) -> list[str]:
        sent = send_idle_work_nudges(conn, CONFIG, at, assess_leads(conn, CONFIG, at, tree({"alex": workers})))
        return [str(conn.execute("SELECT body FROM messages WHERE id = ?", (mid,)).fetchone()[0]) for mid, _ in sent]

    assert nudge(now) == [], "idle with nothing queued is not told"
    ask = add_ask(conn, "alex", "aide", "words", "tab", [], 1)
    assert nudge(now - 1) == [], "idle under 20 minutes"
    unknown = send_idle_work_nudges(conn, CONFIG, now, assess_leads(conn, CONFIG, now, None))
    assert unknown == [], "with agterm silent its workers may be busy on the ask"
    conn.execute("UPDATE instances SET status = 'active' WHERE surface = 'alex'")
    loads = assess_leads(conn, CONFIG, now, tree({"alex": 0}))
    conn.execute("UPDATE instances SET status = 'idle' WHERE surface = 'alex'")
    assert send_idle_work_nudges(conn, CONFIG, now, [replace(loads[0], load="idle", idle_for_ms=IDLE_FOR_MS - 5)]) == []
    suite = [
        ProcessRow(500, 104, "/bin/zsh -c source ~/.claude/shell-snapshots/snapshot-zsh-1.sh && pytest"),
        ProcessRow(501, 500, "python -m pytest -q"),
    ]
    with mock.patch("bridge.capacity.read_process_table", return_value=suite):
        assert nudge(now) == [], "waiting on its own background run, such as a test suite, is not idle"
    assert nudge(now) == [
        f"You have been idle 20m with work waiting: asks #{ask.id} wait on nobody (`guild ask list`). Start the next "
        "item, or mark what blocks it (`guild ask wait`)."
    ]
    assert nudge(now + HOUR) == [], "once per idle spell"
    conn.execute("UPDATE asks SET wait_on = 'owner'")
    conn.execute("UPDATE instances SET last_activity_at = ? WHERE surface = 'alex'", (now + 2 * HOUR,))
    assert nudge(now + 3 * HOUR) == [], "an ask that waits on someone is no work for it"
    write_notes(lead_dir("alex"), "- QUEUE: PROJ-8 export, then PROJ-9\n")
    (queued,) = nudge(now + 3 * HOUR)
    assert "with work waiting: your notes' - QUEUE: PROJ-8 export, then PROJ-9." in queued
    conn.execute("UPDATE instances SET last_activity_at = ? WHERE surface = 'alex'", (now + 4 * HOUR,))
    record_owner_off(conn, "guild dream", now + 4 * HOUR)
    assert nudge(now + 5 * HOUR) == [], "held while the owner is off"


def test_a_leads_focus_line_six_hours_old_with_turns_since_is_told_once_per_focus(conn: sqlite3.Connection) -> None:
    now = WORKDAY_NOON
    add_lead(conn, "alex", [])
    conn.execute(
        "INSERT INTO conversations (conversation_id, focus, focus_at, first_seen_at, last_seen_at) "
        "VALUES ('conv-alex', 'PROJ-8 export', ?, 0, ?)",
        (now - 7 * HOUR, now - 2 * HOUR),
    )

    def nudge(at: int) -> list[str]:
        sent = send_stale_focus_nudges(conn, CONFIG, at)
        return [str(conn.execute("SELECT body FROM messages WHERE id = ?", (mid,)).fetchone()[0]) for mid, _ in sent]

    assert nudge(now) == [], "no turn ended after it went stale"
    conn.execute("UPDATE conversations SET last_seen_at = ?", (now - HOUR,))
    assert nudge(now) == [
        'Your focus line is 7 h old: "PROJ-8 export". If it is not what you do now: '
        '`bridge focus "<your current task>"`.'
    ]
    conn.execute("UPDATE conversations SET last_seen_at = ?", (now,))
    assert nudge(now + HOUR) == [], "once per focus line"
    conn.execute("UPDATE conversations SET focus = 'PROJ-9', focus_at = ?", (now - 6 * HOUR - 1,))
    assert len(nudge(now + HOUR)) == 1, "a new focus line that goes stale is told again"
