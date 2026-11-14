import sqlite3
from collections.abc import Generator
from dataclasses import replace
from pathlib import Path

import pytest

import bridge.issue_sync as issue_sync
from bridge.cli import main
from bridge.config import Config
from bridge.db import connect, transaction
from bridge.issue_sync import (
    Evidence,
    GitHubPr,
    LinearError,
    LinearIssue,
    Move,
    PrEvidence,
    Skip,
    TeamState,
    decide_move,
    log_path,
    map_pr_to_issue_keys,
    run_issue_sync,
)

CONFIG = Config(
    issue_prefixes=("PROJ", "OPS", "WEB"),
    review_org="Org",
    review_author="Me",
    grooming_states=("grooming", "technical design"),
)
NOW = 1_800_000_000_000
PREFIXES = CONFIG.issue_prefixes
PROJ_STATES = tuple(
    TeamState(f"s-{name}", name, kind)
    for name, kind in (
        ("Backlog", "backlog"),
        ("Triage", "triage"),
        ("Grooming", "unstarted"),
        ("Technical Design", "unstarted"),
        ("Blocked", "unstarted"),
        ("Todo", "unstarted"),
        ("In Progress", "started"),
        ("In Review", "started"),
        ("QA", "started"),
        ("Done", "completed"),
        ("Canceled", "canceled"),
        ("Duplicate", "duplicate"),
    )
)


def state(name: str, states: tuple[TeamState, ...] = PROJ_STATES) -> TeamState:
    return next(s for s in states if s.name == name)


def issue(name: str, key: str = "PROJ-1", states: tuple[TeamState, ...] = PROJ_STATES) -> LinearIssue:
    return LinearIssue(id=f"id-{key}", key=key, state=state(name, states), team_states=states)


def pr_evidence(state_: str, draft: bool = False, closes: bool = True, into_default: bool = True) -> PrEvidence:
    return PrEvidence(
        ref="pr:Org/app#1",
        url="https://github.com/Org/app/pull/1",
        state=state_,
        draft=draft,
        closes=closes,
        into_default=state_ == "MERGED" and into_default,
    )


def target_of(decision: Move | Skip | None) -> str | None:
    assert decision is None or isinstance(decision, Move), decision
    return decision.to_state.name if decision else None


def github_pr(
    number: int = 1,
    title: str = "Some change",
    head: str = "dev/x",
    body: str = "",
    state_: str = "OPEN",
    draft: bool = False,
    base: str = "main",
    author: str = "Me",
) -> GitHubPr:
    return GitHubPr(
        repo="Org/app",
        number=number,
        title=title,
        url=f"https://github.com/Org/app/pull/{number}",
        state=state_,
        draft=draft,
        body=body,
        head=head,
        base=base,
        default_branch="main",
        author=author,
    )


def test_a_merged_pr_that_fixes_the_issue_moves_it_to_done() -> None:
    evidence = Evidence(prs=[pr_evidence("MERGED")])
    assert target_of(decide_move(issue("Backlog"), evidence)) == "Done"


def test_an_open_ready_pr_moves_it_to_in_review_or_in_progress_where_the_team_has_no_review_state() -> None:
    evidence = Evidence(prs=[pr_evidence("OPEN")])
    assert target_of(decide_move(issue("Todo"), evidence)) == "In Review"
    no_review = tuple(s for s in PROJ_STATES if s.name != "In Review")
    assert target_of(decide_move(issue("Todo", states=no_review), evidence)) == "In Progress"


def test_a_draft_pr_or_a_live_session_on_its_branch_moves_it_to_in_progress() -> None:
    assert target_of(decide_move(issue("Backlog"), Evidence(prs=[pr_evidence("OPEN", draft=True)]), False)) == (
        "In Progress"
    )
    on_branch = Evidence(sessions=["a live session on branch dev/proj-1"])
    assert target_of(decide_move(issue("Blocked"), on_branch)) == "In Progress"
    assert target_of(decide_move(issue("Todo"), on_branch)) == "In Progress"


def test_a_leads_work_label_alone_moves_nothing_as_with_proj_57() -> None:
    """PROJ-57 went from Todo to In Progress because a lead's open work named it, while nobody worked on it."""
    assert decide_move(issue("Todo"), Evidence(labels=["charlie's live work 'permission rollout'"])) is None
    assert decide_move(issue("Triage"), Evidence(labels=["alex's live work 'x'"])) is None


def test_an_open_pr_next_to_a_merged_one_keeps_the_issue_out_of_done() -> None:
    evidence = Evidence(prs=[pr_evidence("MERGED"), pr_evidence("OPEN")])
    assert target_of(decide_move(issue("Backlog"), evidence)) == "In Review"


def test_it_never_moves_backwards() -> None:
    assert decide_move(issue("In Review"), Evidence(prs=[pr_evidence("OPEN", draft=True)]), False) is None
    assert decide_move(issue("In Progress"), Evidence(sessions=["w"]), False) is None
    assert decide_move(issue("In Review"), Evidence(prs=[pr_evidence("OPEN")]), False) is None


@pytest.mark.parametrize("name", ["Done", "Canceled", "Duplicate"])
def test_done_canceled_and_duplicate_issues_are_never_moved(name: str) -> None:
    assert decide_move(issue(name), Evidence(prs=[pr_evidence("OPEN")], labels=["w"], sessions=["s"]), True) is None


def test_merged_prs_that_are_only_part_of_it_do_not_make_it_done() -> None:
    evidence = Evidence(prs=[pr_evidence("MERGED", closes=False), pr_evidence("CLOSED")])
    assert target_of(decide_move(issue("Backlog"), evidence)) == "In Progress"


def test_a_pr_merged_into_another_branch_does_not_make_it_done() -> None:
    evidence = Evidence(prs=[pr_evidence("MERGED", into_default=False)])
    assert target_of(decide_move(issue("Todo"), evidence)) == "In Progress"


def test_only_closed_unmerged_prs_move_nothing() -> None:
    assert decide_move(issue("Backlog"), Evidence(prs=[pr_evidence("CLOSED")]), False) is None


def test_a_work_label_or_a_live_branch_session_holds_done_back() -> None:
    labelled = Evidence(prs=[pr_evidence("MERGED")], labels=["alex's live work 'sizing'"])
    on_branch = Evidence(prs=[pr_evidence("MERGED")], sessions=["a live session on branch dev/proj-1"])
    assert target_of(decide_move(issue("Todo"), labelled)) == "In Progress", "the merged closing PR started it"
    assert target_of(decide_move(issue("Todo"), on_branch)) == "In Progress"
    assert target_of(decide_move(issue("Todo"), Evidence(prs=[pr_evidence("MERGED")]))) == "Done"


@pytest.mark.parametrize("name", ["Grooming", "Technical Design"])
def test_grooming_issues_are_never_moved(name: str) -> None:
    evidence = Evidence(prs=[pr_evidence("OPEN")])
    skipped = decide_move(issue(name), evidence, grooming_states=CONFIG.grooming_states)
    assert isinstance(skipped, Skip) and skipped.grooming and skipped.reason.startswith("skipped (grooming rule)")


def test_an_issue_set_back_by_hand_after_a_sync_move_is_left_alone() -> None:
    evidence = Evidence(prs=[pr_evidence("OPEN")])
    skipped = decide_move(issue("Todo"), evidence, moved_before="In Review")
    assert isinstance(skipped, Skip) and "set back by hand" in skipped.reason
    assert target_of(decide_move(issue("In Progress"), evidence, moved_before="In Progress")) == "In Review"
    merged = Evidence(prs=[pr_evidence("MERGED")])
    assert target_of(decide_move(issue("Todo"), merged, moved_before="In Review")) == "Done", "new evidence"


def test_a_state_with_no_place_in_the_order_is_skipped() -> None:
    skipped = decide_move(issue("QA"), Evidence(prs=[pr_evidence("MERGED")]), False)
    assert isinstance(skipped, Skip)


def test_a_pr_names_issues_by_branch_title_and_linked_body_words_and_finishes_only_those_after_a_closing_word() -> None:
    pr = github_pr(
        title="PROJ-1: the change",
        head="me/proj-2-the-change",
        body="Fixes [PROJ-3](https://linear.app/x/issue/PROJ-3/slug), PROJ-4\n"
        "Part of PROJ-5. Refs: OPS-6\nDeferred to PROJ-7; relevant to WEB-8.",
    )
    assert map_pr_to_issue_keys(pr, PREFIXES) == {
        "PROJ-1": False,
        "PROJ-2": False,
        "PROJ-3": True,
        "PROJ-4": True,
        "PROJ-5": False,
        "OPS-6": False,
    }


def test_a_body_part_of_overrides_the_title_and_branch() -> None:
    pr = github_pr(title="PROJ-1: first step", head="me/proj-1-first", body="Part of PROJ-1.")
    assert map_pr_to_issue_keys(pr, PREFIXES) == {"PROJ-1": False}


def test_one_of_several_prs_is_part_of_the_issue_and_the_last_one_finishes_it() -> None:
    first = github_pr(head="theo/proj-1-gate", body="🤖 Claude: First of four PRs for PROJ-1. This one adds the gate.")
    last = github_pr(head="theo/proj-1-pages", body="Last of four PRs for PROJ-1.")
    assert map_pr_to_issue_keys(first, PREFIXES) == {"PROJ-1": False}
    assert map_pr_to_issue_keys(last, PREFIXES) == {"PROJ-1": True}


def test_an_open_ready_pr_that_is_only_part_of_the_issue_keeps_it_in_progress() -> None:
    evidence = Evidence(prs=[pr_evidence("OPEN", closes=False)])
    assert target_of(decide_move(issue("Todo"), evidence)) == "In Progress"
    assert decide_move(issue("In Progress"), evidence) is None


def test_a_closing_list_ends_at_the_sentence_or_line_end() -> None:
    pr = github_pr(
        body="Fixes PROJ-1.\n\nPROJ-2 remains deferred. Closes PROJ-3; PROJ-4 is next.\nFixes PROJ-5\nPROJ-6"
    )
    assert map_pr_to_issue_keys(pr, PREFIXES) == {"PROJ-1": True, "PROJ-3": True, "PROJ-5": True}


def test_linear_reports_not_found_only_when_it_says_so() -> None:
    assert issue_sync.map_linear_error_to_result({"message": "Entity not found: Issue"}) == issue_sync.NOT_FOUND
    failure = {"message": "Internal server error", "extensions": {"code": "INTERNAL_SERVER_ERROR"}}
    assert issue_sync.map_linear_error_to_result(failure).startswith("linear error: INTERNAL_SERVER_ERROR")


def test_keys_need_a_boundary_and_title_keys_are_case_sensitive() -> None:
    pr = github_pr(title="openai-4 and proj-9 in prose", head="dev/x", body="Fixes PROJ-12")
    assert map_pr_to_issue_keys(pr, PREFIXES) == {"PROJ-12": True}


class FakeLinear:
    def __init__(
        self,
        issues: dict[str, LinearIssue | str],
        fail_moves: bool = False,
        later: dict[str, LinearIssue | str] | None = None,
    ) -> None:
        self.issues = issues
        self.fail_moves = fail_moves
        self.later = later
        """What every read after the first sees: someone changed Linear in between."""
        self.moves: list[tuple[str, str]] = []

    def fetch_issues(self, keys: list[str]) -> dict[str, LinearIssue | str]:
        found = {k: self.issues.get(k, issue_sync.NOT_FOUND) for k in keys}
        if self.later is not None:
            self.issues = self.later
        return found

    def move(self, issue_id: str, state_id: str) -> str:
        if self.fail_moves:
            raise LinearError("linear HTTP 500")
        self.moves.append((issue_id, state_id))
        return state_id


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[sqlite3.Connection]:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("BRIDGE_CONFIG", str(tmp_path / "config.toml"))
    c = connect(tmp_path / "bridge.db")
    with transaction(c):
        c.execute(
            "INSERT INTO surfaces (surface, agterm_session, handle, updated_at) VALUES ('s-alex', 't', 'alex', 0)"
        )
        c.execute(
            "INSERT INTO instances (instance_id, pid, proc_start_ms, conversation_id, surface, branch, status, "
            "first_seen_at, last_observed_at) "
            "VALUES ('i-alex', 4242, 1, 'c-alex', 's-alex', 'me/proj-2-sizing', 'idle', 0, 0)"
        )
        c.execute(
            "INSERT INTO conversations (conversation_id, instance_id, first_seen_at, last_seen_at) "
            "VALUES ('c-alex', 'i-alex', 0, 0)"
        )
        c.execute(
            "INSERT INTO leads (name, display_name, scope, linear_refs, tint, surface, created_at) "
            "VALUES ('alex', 'Alex', 's', '[]', 'blue', 's-alex', 0)"
        )
        c.execute(
            "INSERT INTO lead_work (lead, name, refs, created_at, updated_at) "
            "VALUES ('alex', 'Sizing', '[\"issue:PROJ-2\"]', 0, 0)"
        )
    yield c
    c.close()


def sweep_prs() -> list[GitHubPr]:
    return [
        github_pr(1, title="PROJ-1: ready", head="me/proj-1", body="Fixes PROJ-1"),
        github_pr(2, title="Someone else's", body="Fixes PROJ-3", author="Other"),
        github_pr(3, title="Unknown", body="Fixes PROJ-404"),
    ]


def test_a_sweep_moves_logs_and_tells_the_owning_lead_once(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        conn.execute('UPDATE lead_work SET refs = \'["issue:PROJ-1", "issue:PROJ-2"]\'')
    linear = FakeLinear({"PROJ-1": issue("Backlog", "PROJ-1"), "PROJ-2": issue("Todo", "PROJ-2")})
    report = run_issue_sync(conn, CONFIG, NOW, dry_run=False, fetch_prs=lambda _c, _n: sweep_prs(), linear=linear)
    assert linear.moves == [("id-PROJ-1", "s-In Review"), ("id-PROJ-2", "s-In Progress")]
    assert not report.errors, "PROJ-404 is not in Linear: nothing to log, and PROJ-3's PR is not ours"
    log = log_path().read_text()
    assert "PROJ-1 Backlog -> In Review | open PR https://github.com/Org/app/pull/1" in log
    assert "PROJ-2 Todo -> In Progress" in log
    bodies = [str(r[0]) for r in conn.execute("SELECT body FROM messages")]
    assert len(bodies) == 1 and "PROJ-1: Backlog → In Review" in bodies[0] and "PROJ-2: Todo → In Progress" in bodies[0]
    again = run_issue_sync(
        conn,
        CONFIG,
        NOW,
        dry_run=False,
        fetch_prs=lambda _c, _n: sweep_prs(),
        linear=FakeLinear({"PROJ-1": issue("Backlog", "PROJ-1"), "PROJ-2": issue("In Progress", "PROJ-2")}),
    )
    assert [s.key for s in again.skips] == ["PROJ-1"], "moved back by hand after the first sweep"


def test_a_grooming_issue_with_work_is_never_moved_and_its_lead_hears_once(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        conn.execute("UPDATE lead_work SET refs = '[\"issue:PROJ-2\"]'")
    for _ in range(2):
        linear = FakeLinear({"PROJ-2": issue("Grooming", "PROJ-2")})
        report = run_issue_sync(conn, CONFIG, NOW, dry_run=False, fetch_prs=lambda _c, _n: [], linear=linear)
        assert linear.moves == [] and [s.key for s in report.skips] == ["PROJ-2"]
    bodies = [str(r[0]) for r in conn.execute("SELECT body FROM messages")]
    assert len(bodies) == 1 and "in a grooming state" in bodies[0] and "- PROJ-2" in bodies[0]


def test_an_issue_assigned_to_a_colleague_is_never_moved_nor_told_about(conn: sqlite3.Connection) -> None:
    """PROJ-77's shape: a colleague's issue that the owner's merged PRs fix, in Technical Design or Backlog."""
    with transaction(conn):
        conn.execute('UPDATE lead_work SET refs = \'["issue:PROJ-1", "issue:PROJ-2"]\'')
    kai = {"assignee": "Kai Doe", "assignee_is_me": False}
    for _ in range(2):
        linear = FakeLinear(
            {
                "PROJ-1": replace(issue("Backlog", "PROJ-1"), **kai),
                "PROJ-2": replace(issue("Technical Design", "PROJ-2"), **kai),
            }
        )
        report = run_issue_sync(conn, CONFIG, NOW, dry_run=False, fetch_prs=lambda _c, _n: sweep_prs(), linear=linear)
        assert linear.moves == [] and not report.moves
        assert {s.key: s.grooming for s in report.skips} == {"PROJ-1": False, "PROJ-2": False}
        assert all("assigned to Kai Doe" in s.reason for s in report.skips)
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0, "no moved-issues or 'work exists' note"


def test_a_move_and_a_grooming_note_reach_the_lead_above_the_worker_that_wrote_the_prs_branch(
    conn: sqlite3.Connection,
) -> None:
    """The PR is not in the review queue and its branch does not name the issue: only its head finds the writer."""
    with transaction(conn):
        conn.execute("DELETE FROM lead_work")
        conn.execute(
            "INSERT INTO surfaces (surface, agterm_session, handle, updated_at) VALUES ('s-w', 't-w', 'worker', 0)"
        )
        conn.execute(
            "INSERT INTO instances (instance_id, pid, proc_start_ms, conversation_id, surface, repo, branch, status, "
            "first_seen_at, last_observed_at) VALUES ('i-w', 4343, 1, 'c-w', 's-w', 'Org/app', 'dev/q', 'idle', 0, 0)"
        )
        conn.execute(
            "INSERT INTO conversations (conversation_id, instance_id, first_seen_at, last_seen_at) "
            "VALUES ('c-w', 'i-w', 0, 0)"
        )
        conn.execute("INSERT INTO review_branch_writes VALUES ('c-w', 'dev/q', 1)")
        conn.execute("INSERT INTO tab_parents (agterm_session, parent_session) VALUES ('t-w', 't')")
    prs = [github_pr(5, head="dev/q", body="Fixes PROJ-5"), github_pr(6, head="dev/q", body="Fixes PROJ-6")]
    linear = FakeLinear({"PROJ-5": issue("Todo", "PROJ-5"), "PROJ-6": issue("Grooming", "PROJ-6")})
    report = run_issue_sync(conn, CONFIG, NOW, dry_run=False, fetch_prs=lambda _c, _n: prs, linear=linear)
    assert [m.key for m in report.applied] == ["PROJ-5"]
    recipients = [str(r[0]) for r in conn.execute("SELECT handle FROM message_recipients")]
    bodies = " ".join(str(r[0]) for r in conn.execute("SELECT body FROM messages"))
    assert recipients == ["alex", "alex"] and "PROJ-5: Todo" in bodies and "- PROJ-6" in bodies


def test_an_issue_assigned_to_the_owner_or_to_no_one_moves_as_before() -> None:
    evidence = Evidence(prs=[pr_evidence("OPEN")])
    mine = replace(issue("Todo"), assignee="Theo", assignee_is_me=True)
    assert target_of(decide_move(mine, evidence)) == "In Review"
    assert target_of(decide_move(issue("Todo"), evidence)) == "In Review"
    unread = replace(issue("Todo"), assignee="Kai Doe")
    assert isinstance(decide_move(unread, evidence), Skip), "an assignee not known to be the owner is a colleague"


def test_linears_answer_says_whose_issue_it_is() -> None:
    node: dict[str, object] = {
        "id": "x", "state": {"id": "s", "name": "Todo", "type": "unstarted"},
        "team": {"states": {"nodes": [{"id": "s", "name": "Todo", "type": "unstarted"}]}},
    }  # fmt: skip
    theirs = issue_sync.map_node_to_linear_issue({**node, "assignee": {"name": "Kai", "isMe": False}}, "PROJ-1")
    mine = issue_sync.map_node_to_linear_issue({**node, "assignee": {"name": "Theo", "isMe": True}}, "PROJ-1")
    nobody = issue_sync.map_node_to_linear_issue({**node, "assignee": None}, "PROJ-1")
    assert [(i.assignee, i.assignee_is_me) for i in (theirs, mine, nobody) if isinstance(i, LinearIssue)] == [
        ("Kai", False),
        ("Theo", True),
        (None, False),
    ]
    assert "assignee { name isMe }" in issue_sync._ISSUE_FIELDS  # pyright: ignore[reportPrivateUsage]


def test_a_failed_move_is_logged_and_told_to_no_one(conn: sqlite3.Connection) -> None:
    linear = FakeLinear({"PROJ-1": issue("Backlog", "PROJ-1")}, fail_moves=True)
    report = run_issue_sync(conn, CONFIG, NOW, dry_run=False, fetch_prs=lambda _c, _n: sweep_prs(), linear=linear)
    assert [e.key for e in report.errors] == ["PROJ-1"] and not report.applied
    assert "ERROR issue sync: PROJ-1 not moved to In Review: linear HTTP 500" in log_path().read_text()
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_a_state_changed_since_the_read_is_not_overwritten(conn: sqlite3.Connection) -> None:
    linear = FakeLinear(
        {"PROJ-1": issue("Backlog", "PROJ-1"), "PROJ-2": issue("Todo", "PROJ-2")},
        later={"PROJ-1": issue("Done", "PROJ-1"), "PROJ-2": issue("Todo", "PROJ-2")},
    )
    report = run_issue_sync(conn, CONFIG, NOW, dry_run=False, fetch_prs=lambda _c, _n: sweep_prs(), linear=linear)
    assert linear.moves == [("id-PROJ-2", "s-In Progress")] and [m.key for m in report.applied] == ["PROJ-2"]


def test_a_sweep_moves_nothing_on_a_leads_work_label_alone(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        conn.execute("UPDATE instances SET branch = 'main'")
    linear = FakeLinear({"PROJ-2": issue("Todo", "PROJ-2")})
    report = run_issue_sync(conn, CONFIG, NOW, dry_run=False, fetch_prs=lambda _c, _n: [], linear=linear)
    assert not report.moves and not linear.moves


def test_a_sweep_already_running_makes_another_move_nothing(conn: sqlite3.Connection) -> None:
    linear = FakeLinear({"PROJ-1": issue("Backlog", "PROJ-1")})
    with issue_sync.hold_sweep_lock() as held:
        assert held
        report = run_issue_sync(conn, CONFIG, NOW, dry_run=False, fetch_prs=lambda _c, _n: sweep_prs(), linear=linear)
    assert not linear.moves and "another issue sync" in report.note


def test_a_linear_read_error_skips_that_issue_only(conn: sqlite3.Connection) -> None:
    linear = FakeLinear({"PROJ-1": "linear failed: TimeoutError", "PROJ-2": issue("Backlog", "PROJ-2")})
    report = run_issue_sync(conn, CONFIG, NOW, dry_run=False, fetch_prs=lambda _c, _n: sweep_prs(), linear=linear)
    assert [e.key for e in report.errors] == ["PROJ-1"]
    assert linear.moves == [("id-PROJ-2", "s-In Progress")]


def test_a_github_error_moves_nothing(conn: sqlite3.Connection) -> None:
    def failing(_c: Config, _n: int) -> list[GitHubPr]:
        raise issue_sync.GitHubError("gh failed: HTTP 502")

    linear = FakeLinear({"PROJ-2": issue("Backlog", "PROJ-2")})
    report = run_issue_sync(conn, CONFIG, NOW, dry_run=False, fetch_prs=failing, linear=linear)
    assert report.failed and not linear.moves


def test_a_dry_run_writes_nothing(conn: sqlite3.Connection) -> None:
    linear = FakeLinear({"PROJ-1": issue("Backlog", "PROJ-1"), "PROJ-2": issue("Todo", "PROJ-2")}, fail_moves=True)
    report = run_issue_sync(conn, CONFIG, NOW, dry_run=True, fetch_prs=lambda _c, _n: sweep_prs(), linear=linear)
    assert [(m.key, m.to_state.name) for m in report.moves] == [("PROJ-1", "In Review"), ("PROJ-2", "In Progress")]
    assert not report.errors and not log_path().exists()
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_cli_dry_run_prints_the_moves(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "config.toml").write_text(
        'issue_prefixes = ["PROJ"]\nreview_org = "Org"\nreview_author = "Me"\nlinear_api_key_file = "/nonexistent"\n'
        'grooming_states = ["Grooming"]\n'
    )
    linear = FakeLinear({"PROJ-1": issue("Backlog", "PROJ-1"), "PROJ-2": issue("Grooming", "PROJ-2")}, fail_moves=True)

    def fetch_prs(_config: Config, _now: int) -> list[GitHubPr]:
        return sweep_prs()

    def read_key(_config: Config) -> tuple[str | None, str | None]:
        return "k" * 40, None

    def make_linear(_key: str) -> FakeLinear:
        return linear

    monkeypatch.setattr(issue_sync, "fetch_my_prs", fetch_prs)
    monkeypatch.setattr(issue_sync, "read_linear_key", read_key)
    monkeypatch.setattr(issue_sync, "LinearApi", make_linear)
    assert main(["issue-sync", "--dry-run"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "would move PROJ-1: Backlog -> In Review | open PR https://github.com/Org/app/pull/1",
        "skip PROJ-2: skipped (grooming rule): Grooming → In Progress; a live session on branch me/proj-2-sizing",
        "1 would move, 1 skipped, 0 errors",
    ]
    assert not log_path().exists()
