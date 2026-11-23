import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from test_reviews import CONFIG, REF, add_lead, add_session, make_pr

from bridge.auto_merge import SANDBOX_BLOCK, map_db_to_dry_run, map_row_to_auto_merge_block, run_auto_merge
from bridge.config import Config
from bridge.db import connect, transaction
from bridge.review_github import GitHubError, LiveMergeFacts
from bridge.reviews import map_prs_to_dependencies, store_dependencies, store_open_prs

ON = replace(CONFIG, auto_merge="on", auto_merge_decision="Q#41", auto_merge_failures_to="lead-x", owner_name="Theo")
CLEAN = {"approved_by": ("mira",), "bot_cleared": True, "checks": "SUCCESS", "merge_state": "CLEAN"}
LIVE = LiveMergeFacts(
    draft=False, approvers=("mira",), bot_approved=False, bot_cleared=True, checks="SUCCESS", merge_state="CLEAN",
    his_threads=0, head_oid="a1", base="main", default_branch="main",
)  # fmt: skip
MERGED = json.dumps({"state": "MERGED", "mergeCommit": {"oid": "c0ffee1234"}})


def setup(tmp_path: Path, *prs: Any) -> Any:
    conn = connect(tmp_path / "t.db")
    with transaction(conn):
        store_open_prs(conn, list(prs) or [make_pr(**CLEAN)], 500)
        add_session(conn, "c1", "worker", branch="dev/x")
        add_session(conn, "c2", "lead-x", surface="s-lead")
    return conn


def row(conn: Any, ref: str = REF) -> Any:
    return conn.execute("SELECT * FROM review_prs WHERE ref = ?", (ref,)).fetchone()


class Gh:
    """A stub gh: records each call, merges unless told to fail."""

    def __init__(self, fail: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail

    def __call__(self, args: list[str], timeout_s: float = 60) -> str:
        self.calls.append(args)
        if args[:2] == ["pr", "merge"] and self.fail:
            raise GitHubError(self.fail)
        return MERGED if args[:2] == ["pr", "view"] else ""

    @property
    def merges(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["pr", "merge"]]


def run(conn: Any, config: Any = ON, live: LiveMergeFacts = LIVE, gh: Gh | None = None, now: int = 1000) -> Gh:
    gh = gh or Gh()
    with (
        mock.patch("bridge.auto_merge.read_live_merge_facts", return_value=live),
        mock.patch("bridge.auto_merge.run_gh", side_effect=gh),
    ):
        run_auto_merge(conn, config, now)
    return gh


@pytest.mark.parametrize(
    ("changes", "column", "reason"),
    [
        ({"stack_parent": "pr:Org/app#6"}, None, "stacked"),
        ({"draft": True}, None, "a draft"),
        ({}, ("held_at", 1), "held for Theo's review"),
        ({}, ("merge_block", "1 review thread is unresolved"), "Theo's comments are open"),
        ({"approved_by": (), "bot_approved": True}, None, "no colleague's approval"),
        ({"bot_cleared": False}, None, "has not reviewed its newest commit"),
        ({"checks": "FAILURE"}, None, "CI is failure"),
        ({"checks": "PENDING"}, None, "CI is pending"),
        ({"merge_state": "BLOCKED"}, None, "not CLEAN"),
        ({"merge_state": "UNSTABLE"}, None, "not CLEAN"),
        ({}, ("dep_blockers", '["waits for Org/lib#3 to merge"]'), "waits for Org/lib#3 to merge"),
        ({}, ("auto_merge_oid", "a1"), "already ran"),
        ({"persist_sandbox": True}, None, SANDBOX_BLOCK),
    ],
)
def test_each_condition_of_the_standing_rule_blocks_alone(
    tmp_path: Path, changes: dict[str, Any], column: tuple[str, Any] | None, reason: str
) -> None:
    conn = setup(tmp_path, make_pr(**{**CLEAN, **changes}))
    if column:
        with transaction(conn):
            conn.execute(f"UPDATE review_prs SET {column[0]} = ?", (column[1],))
    assert reason in (map_row_to_auto_merge_block(ON, row(conn)) or "")
    assert run(conn).merges == []


def test_a_clean_pr_merges_once_with_a_feed_line_and_its_lead_told_merged_not_deployed(tmp_path: Path) -> None:
    conn = setup(tmp_path)
    assert map_row_to_auto_merge_block(ON, row(conn)) is None
    gh = run(conn)
    assert gh.merges == [["pr", "merge", "https://github.com/Org/app/pull/7", "--squash", "--match-head-commit", "a1"]]
    item = conn.execute("SELECT kind, text, links FROM user_items").fetchone()
    assert (item["kind"], item["text"]) == ("done", "merged Org/app#7 T as c0ffee1 (rule Q#41)")
    assert "https://github.com/Org/app/commit/c0ffee1234" in item["links"]
    told = conn.execute("SELECT m.body, r.handle FROM messages m JOIN message_recipients r ON r.message_id = m.id")
    body, handle = told.fetchone()
    assert handle == "worker" and "is merged as c0ffee1" in body and "Merged is not deployed" in body
    assert run(conn).merges == [], "never the same head twice"


def test_at_most_one_merge_per_round(tmp_path: Path) -> None:
    other = make_pr(**CLEAN, ref="pr:Org/app#8", number=8, url="https://github.com/Org/app/pull/8", head="dev/y")
    conn = setup(tmp_path, make_pr(**CLEAN), other)
    assert len(run(conn).merges) == 1
    assert len(run(conn, live=replace(LIVE, head_oid="a1")).merges) == 1, "the next round takes the next one"


def test_a_failed_merge_is_reported_once_and_not_tried_again_at_that_head(tmp_path: Path) -> None:
    conn = setup(tmp_path)
    assert len(run(conn, gh=Gh(fail="gh failed: Pull request is not mergeable")).merges) == 1
    told = conn.execute("SELECT m.body, r.handle FROM messages m JOIN message_recipients r ON r.message_id = m.id")
    rows = told.fetchall()
    assert [r["handle"] for r in rows] == ["lead-x"] and "not mergeable" in rows[0]["body"]
    assert "not mergeable" in str(row(conn)["auto_merge_error"])
    assert run(conn).merges == []
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM user_items").fetchone()[0] == 0, "no feed line for a failure"

    with transaction(conn):
        store_open_prs(conn, [make_pr(**CLEAN, head_oid="b2", commits=(("a1", 1000), ("b2", 2000)))], 900)
    assert len(run(conn, live=replace(LIVE, head_oid="b2")).merges) == 1, "a new head is tried"


@pytest.mark.parametrize(
    "live",
    [
        replace(LIVE, approvers=(), bot_approved=True),
        replace(LIVE, bot_cleared=False),
        replace(LIVE, checks="PENDING"),
        replace(LIVE, merge_state="BLOCKED"),
        replace(LIVE, merge_state="BEHIND"),
        replace(LIVE, head_oid="b2"),
        replace(LIVE, base="dev/parent"),
        replace(LIVE, his_threads=1),
        replace(LIVE, draft=True),
    ],
)
def test_the_live_check_refuses_what_the_queue_copy_did_not_see(tmp_path: Path, live: LiveMergeFacts) -> None:
    conn = setup(tmp_path)
    assert run(conn, live=live).merges == []
    assert row(conn)["auto_merge_oid"] is None, "a refusal is not an attempt: it is checked again next round"


def test_github_unreadable_merges_nothing(tmp_path: Path) -> None:
    conn = setup(tmp_path)
    with (
        mock.patch("bridge.auto_merge.read_live_merge_facts", side_effect=GitHubError("gh failed: timeout")),
        mock.patch("bridge.auto_merge.run_gh") as gh,
    ):
        run_auto_merge(conn, ON, 1000)
    gh.assert_not_called()


@pytest.mark.parametrize(("state", "merges"), [("OPEN", 0), (None, 0), ("MERGED", 1)])
def test_the_body_is_read_again_right_before_merging_and_each_pr_it_merges_after_now(
    tmp_path: Path, state: str | None, merges: int
) -> None:
    conn = setup(tmp_path)
    with mock.patch("bridge.auto_merge.fetch_pr_states", return_value={"Org/lib#3": state}) as states:
        assert len(run(conn, live=replace(LIVE, body="Depends on Org/lib#3.")).merges) == merges
    assert states.call_args.args[0] == ["Org/lib#3"]


def test_a_pr_the_body_only_names_is_no_wait_right_before_merging(tmp_path: Path) -> None:
    conn = setup(tmp_path)
    with mock.patch("bridge.auto_merge.fetch_pr_states") as states:
        assert len(run(conn, live=replace(LIVE, body="Goes with Org/lib#3.")).merges) == 1
    states.assert_not_called()


def test_a_merge_precondition_written_since_the_poll_stops_the_merge_and_editing_it_out_lets_it_go(
    tmp_path: Path,
) -> None:
    conn = setup(tmp_path)
    assert run(conn, live=replace(LIVE, body="Merge after the warp drive is deployed.")).merges == []
    assert len(run(conn, live=replace(LIVE, body="Docs for the warp drive.")).merges) == 1


def test_persist_sandbox_added_since_the_poll_stops_the_merge(tmp_path: Path) -> None:
    conn = setup(tmp_path)
    assert run(conn, live=replace(LIVE, body="<!-- PERSIST_SANDBOX -->")).merges == []


def test_a_stale_queue_merges_nothing(tmp_path: Path) -> None:
    conn = setup(tmp_path)
    assert run(conn, now=500 + 301_000).merges == []


def test_a_hold_placed_while_github_is_read_stops_the_merge(tmp_path: Path) -> None:
    conn = setup(tmp_path)

    def hold_then_answer(*_: Any) -> LiveMergeFacts:
        with transaction(conn):
            conn.execute("UPDATE review_prs SET held_at = 900")
        return LIVE

    gh = Gh()
    with (
        mock.patch("bridge.auto_merge.read_live_merge_facts", side_effect=hold_then_answer),
        mock.patch("bridge.auto_merge.run_gh", side_effect=gh),
    ):
        run_auto_merge(conn, ON, 1000)
    assert gh.merges == [] and row(conn)["auto_merge_oid"] is None


def test_persist_sandbox_tells_the_lead_once_per_head_and_never_merges(tmp_path: Path) -> None:
    conn = setup(tmp_path, make_pr(**CLEAN, persist_sandbox=True))
    assert run(conn).merges == [] and run(conn).merges == []
    bodies = [r[0] for r in conn.execute("SELECT body FROM messages")]
    assert len(bodies) == 1 and "ready to auto-merge except PERSIST_SANDBOX in its body" in bodies[0]
    with transaction(conn):
        store_open_prs(conn, [make_pr(**CLEAN, persist_sandbox=True, head_oid="b2")], 900)
    run(conn)
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 2


@pytest.mark.parametrize("mode", ["off", "dry-run"])
def test_off_and_dry_run_write_nothing_and_merge_nothing(tmp_path: Path, mode: str) -> None:
    conn = setup(tmp_path, make_pr(**CLEAN), make_pr(**CLEAN, ref="pr:Org/app#8", number=8, persist_sandbox=True))
    before = conn.total_changes
    gh = run(conn, config=replace(ON, auto_merge=mode))
    assert gh.calls == [] and conn.total_changes == before


def test_the_dry_run_lists_each_open_pr_with_its_verdict(tmp_path: Path) -> None:
    draft = make_pr(**CLEAN, ref="pr:Org/app#8", number=8, draft=True)
    conn = setup(tmp_path, make_pr(**CLEAN), draft)
    before = conn.total_changes
    with mock.patch("bridge.auto_merge.read_live_merge_facts", return_value=LIVE):
        assert map_db_to_dry_run(conn, ON) == [(REF, None), ("pr:Org/app#8", "a draft")]
    with mock.patch("bridge.auto_merge.read_live_merge_facts", return_value=replace(LIVE, approvers=())):
        assert "not approved" in str(map_db_to_dry_run(conn, ON)[0][1])
    assert conn.total_changes == before


def sibling(number: int, named: tuple[str, ...], **changes: Any) -> Any:
    return make_pr(**{**CLEAN, "ref": f"pr:Org/app#{number}", "number": number, "head": f"dev/{number}",
                      "url": f"https://github.com/Org/app/pull/{number}", "named_prs": named, **changes})  # fmt: skip


def setup_siblings(tmp_path: Path, *prs: Any) -> Any:
    conn = setup(tmp_path, *prs)
    with transaction(conn):
        store_dependencies(conn, map_prs_to_dependencies(list(prs), {}))
    return conn


def test_siblings_merge_one_per_round_only_once_all_are_ready(tmp_path: Path) -> None:
    waiting = setup_siblings(tmp_path, sibling(7, ("Org/app#8",)), sibling(8, ("Org/app#7",), approved_by=()))
    assert "goes with Org/app#8, which is not ready" in str(map_db_to_dry_run(waiting, ON)[0][1])
    assert run(waiting).merges == []

    conn = setup_siblings(tmp_path / "ready", sibling(7, ("Org/app#8",)), sibling(8, ("Org/app#7",)))

    def read_live_merge_facts(config: Config, repo: str, number: int, timeout_s: float) -> LiveMergeFacts:
        return replace(LIVE, approvers=()) if number == 8 else LIVE

    not_approved_now = mock.patch("bridge.auto_merge.read_live_merge_facts", side_effect=read_live_merge_facts)
    with not_approved_now, mock.patch("bridge.auto_merge.run_gh", side_effect=(gh := Gh())):
        run_auto_merge(conn, ON, 1000)
    assert gh.merges == [], "a sibling is read live too"
    with mock.patch("bridge.auto_merge.fetch_pr_states") as states:
        first = run(conn).merges
        assert len(first) == 1
        with transaction(conn):
            conn.execute("UPDATE review_prs SET closed_at = 1100 WHERE url = ?", (first[0][2],))
        second = run(conn).merges
    assert len(second) == 1 and second != first
    assert not states.called, "a sibling is not read as a dependency"


def test_a_sibling_merged_after_goes_second_and_a_cycle_never_merges(tmp_path: Path) -> None:
    conn = setup_siblings(tmp_path, sibling(7, ("Org/app#8",), must_merge=("Org/app#8",)), sibling(8, ("Org/app#7",)))
    assert run(conn).merges[0][2] == "https://github.com/Org/app/pull/8"

    cycle = setup_siblings(
        tmp_path / "cycle",
        sibling(7, ("Org/app#8",), must_merge=("Org/app#8",)),
        sibling(8, ("Org/app#7",), must_merge=("Org/app#7",)),
    )
    assert all("a cycle no merge can start" in str(why) for _, why in map_db_to_dry_run(cycle, ON))
    assert run(cycle).merges == []


class StackGh(Gh):
    """GitHub refuses the plain merge for a PR in one of its stacks; merge-async answers from `replies` in turn."""

    def __init__(self, *replies: dict[str, Any]) -> None:
        super().__init__(fail="gh failed: GraphQL: This pull request is part of a stack and must be merged using ...")
        self.replies = [json.dumps(r) for r in replies]

    def __call__(self, args: list[str], timeout_s: float = 60) -> str:
        if args[0] == "api":
            self.calls.append(args)
            return self.replies.pop(0)
        return super().__call__(args, timeout_s)


def test_a_pr_in_a_github_stack_merges_through_merge_async_at_its_head(tmp_path: Path) -> None:
    conn = setup(tmp_path)
    pending = {"status": "pending", "details": {"uuid": "u-1", "message": "queued"}}
    gh = StackGh(pending, {"status": "merged", "details": {"sha": "c0ffee1234"}})
    with mock.patch("bridge.auto_merge.time.sleep"):
        run(conn, gh=gh)
    apis = [c for c in gh.calls if c[0] == "api"]
    assert apis == [
        ["api", "-X", "PUT", "repos/Org/app/pulls/7/merge-async", "-f", "sha=a1", "-f", "merge_method=squash",
         "-f", "merge_action=direct_merge"],
        ["api", "repos/Org/app/pulls/7/merge-async/u-1"],
    ]  # fmt: skip
    assert conn.execute("SELECT kind FROM user_items").fetchone()["kind"] == "done"


def test_a_failed_stacked_merge_is_told_as_a_failure_with_githubs_reason(tmp_path: Path) -> None:
    conn = setup(tmp_path)
    run(conn, gh=StackGh({"status": "failed", "details": {"message": "head moved"}}))
    assert row(conn)["auto_merge_error"] == "GitHub's stacked merge ended failed: head moved"
    assert conn.execute("SELECT COUNT(*) FROM user_items WHERE kind = 'done'").fetchone()[0] == 0


def test_a_leads_claim_keeps_its_pr_from_merging_and_a_workers_claim_does_not(tmp_path: Path) -> None:
    conn = setup(tmp_path)

    def claim(handle: str, conversation: str) -> None:
        with transaction(conn):
            conn.execute("DELETE FROM claims")
            conn.execute(
                "INSERT INTO claims (resource, token, holder_conversation, holder_handle, reason, acquired_at,"
                " renewed_at, expires_at) VALUES (?, 't', ?, ?, 'keep until the release', 0, 0, 10000000)",
                (REF.lower(), conversation, handle),
            )

    with transaction(conn):
        add_lead(conn, "Lead-X", "s-lead")
    claim("lead-x", "c2")
    assert run(conn).merges == [], "the lead keeps it"
    claim("worker", "c1")
    assert len(run(conn).merges) == 1, "a worker's claim is its work, not a hold"
