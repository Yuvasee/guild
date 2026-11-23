import http.client
import sqlite3
import urllib.error
import urllib.request
from collections.abc import Callable, Generator
from email.message import Message
from pathlib import Path
from unittest import mock

import pytest
from conftest import BridgeEnv, SpawnProcess
from test_capacity import add_lead, add_session
from test_leads import Team
from test_reviews import CONFIG as REVIEW_CONFIG
from test_reviews import make_pr

from bridge import project_owner
from bridge.attention import Project, store_projects
from bridge.config import Config
from bridge.db import connect, transaction
from bridge.domains import PROJECT_FRESH_MS, find_domain_owner, find_issue_keys, refresh_issue_projects
from bridge.issue_sync import LinearApi, LinearError
from bridge.leads import get_lead
from bridge.project_owner import find_lead_of_caller, refresh_open_pr_projects, warn_on_taken_issues
from bridge.reviews import poll_review_queue, store_open_prs

CONFIG = Config(owner_name="the owner", assistant_handle="aide", work_timezone="UTC")
LINEAR = {"ACME-1": "Billing", "ACME-2": "Billing", "ACME-7": "Billing", "ACME-9": "Search", "ACME-20": "Search"}


class FakeLinear:
    def __init__(self, projects: dict[str, str]) -> None:
        self.projects = projects
        self.asked: list[list[str]] = []

    def fetch_issue_projects(self, keys: list[str]) -> dict[str, str | None]:
        self.asked.append(keys)
        return {k: self.projects.get(k) for k in keys}


class DownLinear:
    def fetch_issue_projects(self, keys: list[str]) -> dict[str, str | None]:
        raise LinearError("linear failed: TimeoutError")


def ignore(*_: object) -> None:
    pass


def as_instance(instance_id: str) -> Callable[[sqlite3.Connection], str]:
    return lambda _: instance_id


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[sqlite3.Connection]:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(project_owner, "spawn_typed_delivery", ignore)
    monkeypatch.setattr(project_owner, "nudge_recipients", ignore)
    c = connect(tmp_path / "bridge.db")
    add_lead(c, "anna", ["billing"])
    add_lead(c, "bert", ["search"])
    add_session(c, "aide", "TAB-aide", "idle", 0, pid=900)
    store_projects(c, [Project("billing", ("ACME-1", "ACME-2")), Project("search", ("ACME-9",))], "aide", 0)
    yield c
    c.close()


def inbox(conn: sqlite3.Connection, handle: str) -> list[str]:
    rows = conn.execute(
        "SELECT m.body FROM messages m JOIN message_recipients r ON r.message_id = m.id "
        "WHERE r.conversation_id = ? ORDER BY m.id",
        (f"conv-{handle}",),
    )
    return [str(r[0]) for r in rows]


def test_a_lead_taking_another_leads_issue_is_warned_and_the_owner_and_assistant_told_once(
    conn: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    bert = get_lead(conn, "bert")
    linear = FakeLinear(LINEAR)
    warn_on_taken_issues(conn, CONFIG, bert, ["issue:ACME-7"], linear)
    err = capsys.readouterr().err
    assert "warning: ACME-7 is in Billing, Anna's project: agree with them first" in err
    assert inbox(conn, "anna") == ["Bert took ACME-7 from your project Billing."]
    assert inbox(conn, "aide") == ["Bert took ACME-7 from Anna's project Billing."]
    assert linear.asked == [["ACME-7", "ACME-1", "ACME-2", "ACME-9"]]
    # Its worker's claim on the same issue counts as Bert's: nothing more, and Linear is not asked again.
    warn_on_taken_issues(conn, CONFIG, bert, ["issue:ACME-7"], linear)
    assert capsys.readouterr().err == "" and len(linear.asked) == 1
    assert len(inbox(conn, "anna")) == 1 and len(inbox(conn, "aide")) == 1


def test_an_issue_a_priority_project_lists_is_placed_without_linear(
    conn: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    warn_on_taken_issues(conn, CONFIG, get_lead(conn, "bert"), ["issue:ACME-2"], DownLinear())
    assert capsys.readouterr().err == "warning: ACME-2 is in billing, Anna's project: agree with them first\n"
    assert inbox(conn, "anna") == ["Bert took ACME-2 from your project billing."]


def test_a_lead_on_its_own_or_an_unowned_issue_hears_nothing(
    conn: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    linear = FakeLinear({**LINEAR, "ACME-30": "Docs"})
    warn_on_taken_issues(conn, CONFIG, get_lead(conn, "anna"), ["issue:ACME-1", "issue:ACME-7"], linear)
    warn_on_taken_issues(conn, CONFIG, get_lead(conn, "anna"), ["issue:ACME-30", "issue:ACME-404", "pr:o/r#3"], linear)
    assert capsys.readouterr().err == ""
    assert inbox(conn, "anna") == inbox(conn, "bert") == inbox(conn, "aide") == []
    # ACME-1 is listed in Anna's own project, so Linear was asked only for the others and the samples.
    assert linear.asked == [["ACME-7", "ACME-1", "ACME-2", "ACME-9"], ["ACME-30", "ACME-404"]]


def test_linear_down_leaves_a_note_and_sends_nothing(
    conn: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    bert = get_lead(conn, "bert")
    warn_on_taken_issues(conn, CONFIG, bert, ["issue:ACME-7"], DownLinear())
    err = capsys.readouterr().err
    assert err == "note: Linear not reached (linear failed: TimeoutError): ACME-7 placed by the priority lists only\n"
    assert inbox(conn, "anna") == inbox(conn, "aide") == []
    # Not recorded, so the next time Linear answers it is checked.
    warn_on_taken_issues(conn, CONFIG, bert, ["issue:ACME-7"], FakeLinear(LINEAR))
    assert len(inbox(conn, "anna")) == 1


def test_linear_down_still_warns_on_a_listed_issue(
    conn: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    warn_on_taken_issues(conn, CONFIG, get_lead(conn, "bert"), ["issue:ACME-2", "issue:ACME-7"], DownLinear())
    err = capsys.readouterr().err
    assert "note: Linear not reached (linear failed: TimeoutError): ACME-2, ACME-7" in err
    assert "warning: ACME-2 is in billing, Anna's project" in err
    assert inbox(conn, "anna") == ["Bert took ACME-2 from your project billing."]


def test_no_linear_key_is_a_note(conn: sqlite3.Connection, capsys: pytest.CaptureFixture[str]) -> None:
    warn_on_taken_issues(conn, CONFIG, get_lead(conn, "bert"), ["issue:ACME-7"])
    assert "note: Linear not reached (no Linear API key)" in capsys.readouterr().err


def test_a_domain_named_as_the_linear_project_matches_it(
    conn: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    add_lead(conn, "cleo", ["Docs"])
    warn_on_taken_issues(conn, CONFIG, get_lead(conn, "bert"), ["issue:ACME-30"], FakeLinear({"ACME-30": "docs"}))
    assert "ACME-30 is in docs, Cleo's project" in capsys.readouterr().err


def test_a_priority_project_takes_the_linear_project_most_of_its_issues_are_in(
    conn: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    # Billing lists a blocker from Search; Search issues still read as Bert's, not Anna's.
    store_projects(conn, [Project("billing", ("ACME-1", "ACME-2", "ACME-20")), Project("search", ("ACME-9",))], "a", 0)
    linear = FakeLinear({**LINEAR, "ACME-21": "Search"})
    warn_on_taken_issues(conn, CONFIG, get_lead(conn, "anna"), ["issue:ACME-21"], linear)
    assert "ACME-21 is in Search, Bert's project" in capsys.readouterr().err
    assert inbox(conn, "anna") == []


def test_a_priority_project_split_evenly_between_linear_projects_names_neither(
    conn: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    store_projects(conn, [Project("billing", ("ACME-1", "ACME-20")), Project("search", ("ACME-9",))], "aide", 0)
    linear = FakeLinear({"ACME-1": "Payments", "ACME-20": "Search", "ACME-9": "Search", "ACME-21": "Search",
                         "ACME-22": "Payments"})  # fmt: skip
    warn_on_taken_issues(conn, CONFIG, get_lead(conn, "anna"), ["issue:ACME-21"], linear)
    assert "ACME-21 is in Search, Bert's project" in capsys.readouterr().err
    warn_on_taken_issues(conn, CONFIG, get_lead(conn, "bert"), ["issue:ACME-22"], linear)
    assert capsys.readouterr().err == ""


def test_overlapping_domains_place_an_issue_with_no_one(conn: sqlite3.Connection) -> None:
    add_lead(conn, "cleo", ["Billing"])
    assert find_domain_owner(conn, "ACME-2") is None


def test_known_projects_are_not_asked_again_for_hours(conn: sqlite3.Connection) -> None:
    linear = FakeLinear(LINEAR)
    everything = ["ACME-7", "ACME-1", "ACME-2", "ACME-9"]
    assert refresh_issue_projects(conn, ["ACME-7"], 1_000, linear.fetch_issue_projects) == everything
    assert refresh_issue_projects(conn, ["ACME-7", "ACME-2"], 2_000, linear.fetch_issue_projects) == []
    later = 1_000 + PROJECT_FRESH_MS + 1
    assert refresh_issue_projects(conn, ["ACME-7"], later, linear.fetch_issue_projects) == everything
    found = find_domain_owner(conn, "acme-7")
    assert found is not None and (found.lead.name, found.project) == ("anna", "Billing")


def test_a_workers_claim_counts_as_its_leads(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    add_session(conn, "worker-1", "TAB-worker-1", "active", 0, pid=950)
    conn.execute("INSERT INTO tab_parents (agterm_session, parent_session) VALUES ('TAB-worker-1', 'TAB-bert')")
    monkeypatch.setattr(project_owner, "ensure_current_instance", as_instance("i-worker-1"))
    lead = find_lead_of_caller(conn)
    assert lead is not None and lead.name == "bert"
    monkeypatch.setattr(project_owner, "ensure_current_instance", as_instance("i-aide"))
    assert find_lead_of_caller(conn) is None


def test_an_owner_without_a_session_is_named_to_the_assistant(
    conn: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    conn.execute("UPDATE instances SET ended_at = 1, status = 'ended' WHERE instance_id = 'i-anna'")
    warn_on_taken_issues(conn, CONFIG, get_lead(conn, "bert"), ["issue:ACME-7"], FakeLinear(LINEAR))
    assert inbox(conn, "aide") == ["Bert took ACME-7 from Anna's project Billing. Anna was not told: no live session."]
    assert conn.execute("SELECT count(*) FROM issue_takes").fetchone()[0] == 1


def test_a_take_nobody_heard_of_is_tried_again(conn: sqlite3.Connection, capsys: pytest.CaptureFixture[str]) -> None:
    conn.execute("UPDATE instances SET ended_at = 1, status = 'ended' WHERE instance_id IN ('i-anna', 'i-aide')")
    warn_on_taken_issues(conn, CONFIG, get_lead(conn, "bert"), ["issue:ACME-7"], FakeLinear(LINEAR))
    assert "ACME-7 is in Billing" in capsys.readouterr().err
    assert conn.execute("SELECT count(*) FROM issue_takes").fetchone()[0] == 0


def test_a_failing_check_never_fails_the_command(conn: sqlite3.Connection, capsys: pytest.CaptureFixture[str]) -> None:
    class Broken:
        def fetch_issue_projects(self, keys: list[str]) -> dict[str, str | None]:
            raise ValueError("truncated answer")

    warn_on_taken_issues(conn, CONFIG, get_lead(conn, "bert"), ["issue:ACME-7"], Broken())
    assert "note: the check of whose project this is failed: ValueError" in capsys.readouterr().err


def test_the_project_lookup_asks_linear_for_the_project_and_raises_on_a_failed_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[str] = []
    answers: list[object] = [
        {"data": {"i0": {"project": {"name": "Billing"}}, "i1": {"project": None}}},
        {"data": None, "errors": [{"path": ["i0"], "message": "rate limited", "extensions": {"code": "RATE"}}]},
    ]

    def post(_: LinearApi, payload: dict[str, object]) -> object:
        asked.append(str(payload["query"]))
        return answers.pop(0)

    monkeypatch.setattr(LinearApi, "_post", post)
    assert LinearApi("key").fetch_issue_projects(["ACME-7", "ACME-8"]) == {"ACME-7": "Billing", "ACME-8": None}
    assert asked == [
        'query { i0: issue(id: "ACME-7") { project { name } } i1: issue(id: "ACME-8") { project { name } } }'
    ]
    with pytest.raises(LinearError, match="rate limited"):
        LinearApi("key").fetch_issue_projects(["ACME-7"])


def test_a_truncated_linear_answer_is_a_linear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class Truncated:
        def __enter__(self) -> "Truncated":
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def read(self) -> bytes:
            raise http.client.IncompleteRead(b"", 100)

    def urlopen(*_: object, **__: object) -> Truncated:
        return Truncated()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert LinearApi("key").fetch_issue_nodes(["ACME-7"], "id") == {"ACME-7": "linear failed: IncompleteRead"}


def test_overlapping_domains_warn_and_still_save(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    assert team.enroll().returncode == 0
    assert team.guild("lead", "add", "Bert", "--tab", "old-b", "--scope", "Search", "--label", "S").returncode == 0
    assert team.guild("lead", "domains", "alex", "billing", pid=team.a).returncode == 0
    done = team.guild("lead", "domains", "bert", "Billing", "search", pid=team.b)
    assert done.returncode == 0 and "Bert: Billing, search" in done.stdout
    assert "warning: Billing is already Alex's domain: one lead per project, the assistant decides" in done.stderr


def test_work_set_with_linear_unreachable_still_works(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    assert team.enroll().returncode == 0
    assert team.guild("lead", "add", "Bert", "--tab", "old-b", "--scope", "Search", "--label", "S").returncode == 0
    assert team.guild("lead", "domains", "alex", "billing", pid=team.a).returncode == 0
    done = team.guild("work", "set", "Fix totals", "ABC-7", pid=team.b)
    assert done.returncode == 0 and "Fix totals  [issue:ABC-7]" in done.stdout
    assert "note: Linear not reached (no Linear API key): ABC-7" in done.stderr
    with bridge_env.connect() as conn:
        assert conn.execute("SELECT count(*) FROM issue_takes").fetchone()[0] == 0


def test_a_leads_issue_claim_is_checked_and_still_claims(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    assert team.enroll().returncode == 0
    assert team.guild("lead", "add", "Bert", "--tab", "old-b", "--scope", "Search", "--label", "S").returncode == 0
    assert team.guild("lead", "domains", "alex", "billing", pid=team.a).returncode == 0
    claimed = bridge_env.cli("claim", "issue:ABC-7", pid=team.b)
    assert claimed.returncode == 0 and "claimed issue:ABC-7" in claimed.stdout
    assert "note: Linear not reached (no Linear API key): ABC-7" in claimed.stderr


def test_the_review_poll_reads_the_projects_of_issues_its_open_prs_name(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_file = tmp_path / "linear-key"
    key_file.write_text("key\n")
    config = Config(assistant_handle="aide", issue_prefixes=("ACME",), linear_api_key_file=str(key_file))
    linear = FakeLinear(LINEAR)

    def open_api(_: Config) -> FakeLinear:
        return linear

    monkeypatch.setattr(project_owner, "_open_api", open_api)
    with transaction(conn):
        store_open_prs(conn, [make_pr(title="ACME-7: totals", head="dev/acme-2-x")], 500)
    refresh_open_pr_projects(conn, config, 1_000)
    assert linear.asked == [["ACME-7", "ACME-1", "ACME-2", "ACME-9"]]
    refresh_open_pr_projects(conn, config, 2_000)
    assert len(linear.asked) == 1
    refresh_open_pr_projects(conn, Config(assistant_handle="aide", issue_prefixes=("ACME",)), 1_000 + PROJECT_FRESH_MS)
    assert len(linear.asked) == 1, "no Linear key configured: nothing is asked"


def test_a_new_pr_has_its_issue_projects_read_before_its_notices_are_routed(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.db")
    seen: list[list[str]] = []

    def after_store(c: sqlite3.Connection) -> None:
        assert not c.in_transaction
        seen.append([str(r[0]) for r in c.execute("SELECT ref FROM review_prs")])

    with (
        mock.patch("bridge.reviews.fetch_open_prs", return_value=[make_pr(title="ACME-7: totals")]),
        mock.patch("bridge.reviews.add_label"),
    ):
        poll_review_queue(conn, REVIEW_CONFIG, 900, None, None, after_store)
    assert seen == [["pr:Org/app#7"]]


def test_a_priority_project_is_placed_by_all_its_listed_issues_not_the_first_few(
    conn: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    blockers = [f"ACME-{n}" for n in range(40, 43)]
    own = [f"ACME-{n}" for n in range(50, 58)]
    store_projects(conn, [Project("billing", (*blockers, *own)), Project("search", ("ACME-9",))], "aide", 0)
    linear = FakeLinear({**dict.fromkeys(blockers, "Search"), **dict.fromkeys(own, "Billing"), "ACME-9": "Search",
                         "ACME-60": "Search"})  # fmt: skip
    warn_on_taken_issues(conn, CONFIG, get_lead(conn, "anna"), ["issue:ACME-60"], linear)
    assert "ACME-60 is in Search, Bert's project" in capsys.readouterr().err


def test_a_prs_issue_key_is_read_as_the_cache_reads_it(conn: sqlite3.Connection) -> None:
    config = Config(assistant_handle="aide", issue_prefixes=("AC2",))
    assert find_issue_keys(config, "Totals dev/ac2-7_fix") == {"AC2-7"}
    assert find_issue_keys(Config(assistant_handle="aide"), "ACME-7: totals dev/x") == {"ACME-7"}


def test_a_truncated_linear_error_answer_is_a_linear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class Truncated502(urllib.error.HTTPError):
        def __init__(self) -> None:
            super().__init__("https://linear.example", 502, "Bad Gateway", Message(), None)

        def read(self, n: int = -1) -> bytes:
            raise http.client.IncompleteRead(b"", 100)

    def urlopen(*_: object, **__: object) -> object:
        raise Truncated502()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert LinearApi("key").fetch_issue_nodes(["ACME-7"], "id") == {"ACME-7": "linear HTTP 502"}
