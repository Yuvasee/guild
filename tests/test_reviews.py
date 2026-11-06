import json
from pathlib import Path
from typing import Any, cast
from unittest import mock

import pytest
from conftest import BridgeEnv

from bridge import reviews
from bridge.board import BODY_MAX_CHARS
from bridge.config import Config
from bridge.db import connect, transaction
from bridge.drivers import scan_transcripts
from bridge.review_github import (
    FetchedComment,
    GitHubError,
    OpenPr,
    OwnerThread,
    ReviewThreads,
    fetch_comments,
    has_review_by_others,
    is_approved_by_review_bot,
    map_files_to_loc,
    map_node_to_open_pr,
    map_reviews_to_approvers,
    map_threads_to_review_threads,
)
from bridge.reviews import (
    AGENT,
    COLLEAGUE,
    OLD,
    OWNER,
    READY,
    REVIEWER,
    UNCLEAR,
    Mark,
    classify_comment,
    classify_pending_comments,
    count_commits_since,
    find_owner,
    load_known_loc,
    load_marks,
    map_command_to_branch_writes,
    map_command_to_comment_targets,
    map_db_to_reviews,
    map_events_to_rounds,
    map_pr_to_threads_key,
    poll_review_queue,
    record_agent_marks,
    record_branch_writes,
    record_tab_parents,
    store_comments,
    store_merge_blocks,
    store_open_prs,
    store_review_threads,
    take_comment_wakes,
    tell_ready_to_merge,
)

PREFIX_RULE_AT = 1_000_000_000_000_000
"""A cutover after every fixture's own times, so the tests also cover comments from before the prefix rule."""


@pytest.fixture(autouse=True)
def prefix_rule_cutover(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reviews, "AGENT_PREFIX_FROM_MS", PREFIX_RULE_AT)


CONFIG = Config(review_org="Org", review_author="me", review_bot_logins=("bot",), work_timezone="UTC")
REF = "pr:Org/app#7"


def make_pr(**changes: Any) -> OpenPr:
    values: dict[str, Any] = {
        "ref": REF, "repo": "Org/app", "number": 7, "title": "T", "url": "https://github.com/Org/app/pull/7",
        "draft": False, "head": "dev/x", "head_oid": "a1", "base": "main",
        "stack_parent": None, "loc": 10, "checks": "SUCCESS", "requested": 0,
        "commits": (("a1", 1000),), "updated_at": "2026-11-02T10:00:00Z",
    }  # fmt: skip
    return OpenPr(**{**values, **changes})


def add_session(
    conn: Any,
    conversation: str,
    handle: str,
    branch: str = "other",
    repo: str = "Org/app",
    ended: int | None = None,
    surface: str | None = None,
    session: str | None = None,
    wrote: bool | None = None,
) -> None:
    """A session on a branch other than "other" committed to it, unless `wrote` says otherwise."""
    surface = surface or f"s-{handle}"
    conn.execute(
        "INSERT OR IGNORE INTO surfaces (surface, agterm_session, handle, updated_at) VALUES (?, ?, ?, 0)",
        (surface, session or f"tab-{handle}", handle),
    )
    instance = f"i-{conversation}"
    conn.execute(
        """
        INSERT INTO instances (instance_id, pid, proc_start_ms, conversation_id, surface, repo, branch, first_seen_at,
                               last_observed_at, last_activity_at, ended_at)
        VALUES (?, 1, 1, ?, ?, ?, ?, 0, 0, 5, ?)
        """,
        (instance, conversation, surface, repo, branch, ended),
    )
    conn.execute(
        "INSERT INTO conversations (conversation_id, instance_id, first_seen_at, last_seen_at) VALUES (?, ?, 0, 0)",
        (conversation, instance),
    )
    if wrote or (wrote is None and branch != "other"):
        conn.execute("INSERT INTO review_branch_writes VALUES (?, ?, 1)", (conversation, branch))


def add_lead(conn: Any, name: str, surface: str) -> None:
    conn.execute(
        "INSERT INTO leads (name, display_name, scope, linear_refs, tint, surface, created_at) VALUES (?, ?, 's', '[]', 'blue', ?, 0)",  # noqa: E501
        (name.lower(), name, surface),
    )


def test_loc_leaves_out_lockfiles_and_generated_files_by_base_name() -> None:
    files = [
        {"path": "app/uv.lock", "additions": 500, "deletions": 400},
        {"path": "web/src/api.gen.ts", "additions": 90, "deletions": 0},
        {"path": "app/main.py", "additions": 7, "deletions": 3},
    ]
    assert map_files_to_loc(597, 403, files, (*Config().review_loc_excluded, "*.gen.ts")) == 10
    assert map_files_to_loc(597, 403, files, Config().review_loc_excluded) == 100


def test_only_people_approve_and_their_latest_verdict_stands() -> None:
    def review(login: str, state: str, kind: str = "User") -> dict[str, object]:
        return {"state": state, "author": {"__typename": kind, "login": login}}

    nodes = [
        review("bot", "APPROVED"),
        review("me", "APPROVED"),
        review("copilot", "APPROVED", "Bot"),
        {"state": "APPROVED", "author": None},
        review("ann", "APPROVED"),
        review("ann", "COMMENTED"),
        review("bob", "APPROVED"),
        review("bob", "CHANGES_REQUESTED"),
        review("cat", "APPROVED"),
        review("cat", "DISMISSED"),
    ]
    assert map_reviews_to_approvers(nodes, CONFIG) == ("ann",)


def test_a_comment_by_a_person_counts_as_a_review_and_the_author_and_bots_do_not() -> None:
    def review(login: str, state: str = "COMMENTED", kind: str = "User") -> dict[str, object]:
        return {"state": state, "author": {"__typename": kind, "login": login}}

    assert (
        has_review_by_others([review("me"), review("bot", "APPROVED"), review("copilot", kind="Bot")], CONFIG) is False
    )
    assert has_review_by_others([{"state": "COMMENTED", "author": None}], CONFIG) is False
    assert has_review_by_others([review("Me"), review("BOT")], CONFIG) is False
    assert has_review_by_others([review("me"), review("kai")], CONFIG) is True


def test_a_review_once_seen_stays_seen(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path, others_reviewed=True)
    with transaction(conn):
        store_open_prs(conn, [make_pr(others_reviewed=False)], 600)
    assert conn.execute("SELECT others_reviewed FROM review_prs").fetchone()[0] == 1


def test_node_maps_to_pr_with_stack_parent_and_head_commit() -> None:
    node: Any = {
        "number": 7, "title": "T", "url": "u", "isDraft": True, "updatedAt": "U", "headRefName": "h",
        "headRefOid": "b2", "baseRefName": "h0", "additions": 5, "deletions": 1,
        "repository": {"nameWithOwner": "Org/app"},
        "baseRef": {"associatedPullRequests": {"nodes": [{"number": 6}]}},
        "commits": {"nodes": [
            {"commit": {"oid": "a1", "committedDate": "2026-11-02T10:00:00Z", "statusCheckRollup": None}},
            {"commit": {"oid": "b2", "committedDate": "2026-11-02T11:00:00Z",
                        "statusCheckRollup": {"state": "FAILURE"}}},
        ]},
        "files": {"pageInfo": {"hasNextPage": False}, "nodes": []},
        "reviewRequests": {"nodes": [{"requestedReviewer": {"__typename": "Team", "slug": "core"}}]},
        "reviews": {"nodes": []},
        "latestReviews": {"nodes": [{"author": {"__typename": "User", "login": "kai"}}]},
        "reviewThreads": {"nodes": [{"isResolved": False}, {"isResolved": True}, {"isResolved": False}]},
        "body": "Merge only after #5 is released.",
    }  # fmt: skip
    pr = map_node_to_open_pr(node, CONFIG)
    assert pr.depends_on == "Org/app#5"
    assert pr.others_reviewed and pr.approved_by == () and pr.open_threads == 2
    assert (pr.ref, pr.stack_parent, pr.checks, pr.loc, pr.requested) == (REF, "pr:Org/app#6", "FAILURE", 6, 1)
    assert pr.commit_oids == ("a1", "b2")
    assert pr.commits[1][1] > pr.commits[0][1] > 0


def test_the_pr_its_body_merges_after_is_stored_and_listed_with_its_label(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path, depends_on="Org/lib#3")
    prs: Any = map_db_to_reviews(conn, CONFIG, 600)["prs"]
    assert (prs[0]["depends_on"], prs[0]["depends_on_label"]) == ("pr:Org/lib#3", "lib#3")
    with transaction(conn):
        store_open_prs(conn, [make_pr()], 700)
    assert conn.execute("SELECT depends_on FROM review_prs").fetchone()[0] is None


def test_a_round_is_owner_comments_then_a_commit_after_them() -> None:
    assert map_events_to_rounds([], [5]) == (0, False)
    assert map_events_to_rounds([10], [5]) == (0, True)
    assert map_events_to_rounds([10, 11], [5, 20]) == (1, False)
    assert map_events_to_rounds([10, 30], [5, 20]) == (1, True)
    assert map_events_to_rounds([10, 30], [5, 20, 40, 50]) == (2, False)


def test_commits_since_counts_from_the_recorded_commit() -> None:
    assert count_commits_since('["a","b","c"]', "a") == 2
    assert count_commits_since('["a","b","c"]', "c") == 0
    assert count_commits_since('["a"]', None) is None
    assert count_commits_since('["x"]', "a") is None


def test_real_comment_commands_are_recognised() -> None:
    replies = 'gh api -X POST repos/Org/app/pulls/43/comments/44/replies -f body="Fixed in 93e"'
    assert map_command_to_comment_targets(replies) == [("Org/app", 43)]
    assert map_command_to_comment_targets('gh api repos/Org/app/pulls/7/comments/44/replies -f body="x"') == [
        ("Org/app", 7)
    ]
    assert map_command_to_comment_targets('gh api -X POST repos/Org/app/issues/7/comments -f body="x"') == [
        ("Org/app", 7)
    ]
    assert map_command_to_comment_targets("cd w; gh pr comment 205 --body-file /tmp/r.md") == [(None, 205)]
    assert map_command_to_comment_targets('gh pr comment https://github.com/Org/app/pull/9 -b "x"') == [("Org/app", 9)]
    assert map_command_to_comment_targets('gh pr review 7 --comment -b "x"') == [(None, 7)]
    assert map_command_to_comment_targets('gh pr comment -b "from the branch"') == [(None, None)]
    loop = "for n in 1 2; do gh api -X POST repos/Org/app/issues/$n/comments -f body=hi; done"
    assert map_command_to_comment_targets(loop) == [("Org/app", None)]
    graphql = "gh api graphql -f query='mutation { addPullRequestReviewThread(input: {}) { thread { id } } }'"
    assert map_command_to_comment_targets(graphql) == [(None, None)]


def test_reading_comments_is_not_a_mark() -> None:
    assert map_command_to_comment_targets("gh api repos/Org/app/pulls/7/comments --jq '.[].body'") == []
    assert map_command_to_comment_targets("gh pr view 7 --comments") == []
    assert map_command_to_comment_targets("gh api graphql -f query='query { viewer { login } }'") == []


def classify(
    body: str = "Please rename",
    at: int = 1_000_000,
    marks: list[Mark] | None = None,
    parent: str | None = None,
    parent_author: str | None = None,
    parent_verdict: str | None = None,
    remote: bool = False,
) -> str:
    return classify_comment(at, body, parent, parent_author, parent_verdict, "me", marks or [], remote)[0]


def test_a_top_level_comment_with_no_session_near_it_is_owner() -> None:
    assert classify() == OWNER
    far_earlier = Mark(1_000_000 - 700_000, "gh pr comment 7 --body-file x.md")
    assert classify(marks=[far_earlier]) == OWNER


def test_a_workers_reply_never_reads_as_owner() -> None:
    inline = Mark(1_000_000 - 2000, 'gh api repos/O/R/pulls/7/comments/4/replies -f body="Fixed in \\"93e\\" today"')
    assert classify('Fixed in "93e" today', marks=[inline]) == AGENT
    from_file = Mark(1_000_000 - 300_000, "gh pr comment 7 --body-file /tmp/r.md")
    assert classify(marks=[from_file]) == AGENT
    variable = Mark(1_000_000 - 300_000, 'gh api repos/O/R/issues/7/comments -f body="$TEXT"')
    assert classify(marks=[variable]) == AGENT


def test_a_session_posting_other_text_right_then_leaves_it_unclear_and_later_owner() -> None:
    other = 'gh api repos/O/R/issues/7/comments -f body="something else"'
    assert classify(marks=[Mark(1_000_000 - 30_000, other)]) == UNCLEAR
    assert classify(marks=[Mark(1_000_000 - 200_000, other)]) == OWNER


def test_replies_on_threads_the_owner_did_not_start_stay_unclear() -> None:
    assert classify(parent="p1", parent_author="bot") == UNCLEAR
    assert classify(parent="p1", parent_author="me", parent_verdict=AGENT) == UNCLEAR
    assert classify(parent="p1", parent_author="me", parent_verdict=OWNER) == OWNER


def test_a_devbox_session_on_the_pr_leaves_it_unclear() -> None:
    assert classify(remote=True) == UNCLEAR


def setup_pr(tmp_path: Path, **changes: Any) -> Any:
    conn = connect(tmp_path / "t.db")
    with transaction(conn):
        store_open_prs(conn, [make_pr(**changes)], 500)
    return conn


def store(conn: Any, *comments: FetchedComment) -> None:
    with transaction(conn):
        store_comments(conn, CONFIG, REF, "U", list(comments))


def comment(
    comment_id: str = "p1",
    at: int = 1000,
    body: str = "Please rename",
    author: str = "me",
    state: str | None = None,
    commit: str | None = None,
    by_app: bool = False,
    review: str | None = None,
) -> FetchedComment:
    return FetchedComment(
        comment_id,
        author,
        at,
        body,
        f"https://x/{comment_id}",
        state=state,
        commit_id=commit,
        by_app=by_app,
        review_id=review,
    )


def test_apps_and_the_review_bots_approvals_are_not_kept_and_people_and_its_findings_are(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    store(
        conn,
        comment("i1", author="colleague"),
        comment("i2", author="ci-app", by_app=True),
        comment("r3", author="bot", body="", state="APPROVED"),
        comment("r4", author="bot", body="Two findings", state="CHANGES_REQUESTED"),
        comment("p5", author="bot"),
        comment("r6", author="colleague", body="", state="DISMISSED"),
        comment("r7", author="colleague", body="", state="COMMENTED"),
        comment("r8", author="me", body="", state="COMMENTED"),
    )
    kept = [r[0] for r in conn.execute("SELECT comment_id FROM review_comments ORDER BY comment_id")]
    assert kept == ["i1", "p5", "r4", "r8"]


def test_no_verdict_before_a_scan_that_began_after_the_comment(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    store(conn, comment(at=1000))
    with transaction(conn):
        classify_pending_comments(conn, CONFIG, 1000 + 10_000)
    assert conn.execute("SELECT verdict FROM review_comments").fetchone()[0] is None
    with transaction(conn):
        classify_pending_comments(conn, CONFIG, 1000 + 40_000)
    assert conn.execute("SELECT verdict FROM review_comments").fetchone()[0] == OWNER


def test_comments_from_before_the_queue_saw_the_pr_wake_nobody(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    store(conn, comment(at=100))
    with transaction(conn):
        classify_pending_comments(conn, CONFIG, 100_000)
    assert conn.execute("SELECT verdict FROM review_comments").fetchone()[0] == OLD


def wake(conn: Any, now: int = 200_000) -> list[Any]:
    with transaction(conn):
        classify_pending_comments(conn, CONFIG, now)
        return take_comment_wakes(conn, CONFIG, now)


def test_owner_comment_wakes_the_owner_once_with_text_and_link(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
    store(conn, comment())
    sent = wake(conn)
    assert [r.handle for _, r in sent] == ["worker"]
    row = conn.execute("SELECT body, post_type FROM messages WHERE id = ?", (sent[0][0],)).fetchone()
    assert "Please rename" in row["body"] and "https://x/p1" in row["body"] and row["post_type"] == "review-queue"
    assert wake(conn) == []


def test_a_comment_with_no_owner_waits_for_one(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    store(conn, comment())
    assert wake(conn) == []
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
    assert len(wake(conn)) == 1


def test_owner_comments_past_the_cap_wait_out_the_hour_and_go_in_one_message(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
    for n in range(3):
        store(conn, comment(f"i{n}", at=1000 + n))
        assert len(wake(conn, 200_000 + n)) == 1
    store(conn, comment("i8", at=1008, body="First"), comment("i9", at=1009, body="Second"))
    assert wake(conn, 200_100) == []
    assert wake(conn, 200_200) == []
    assert conn.execute("SELECT count(*) FROM review_comments WHERE verdict = ?", (UNCLEAR,)).fetchone()[0] == 0
    assert len(wake(conn, 200_000 + 3_600_001)) == 1
    body = conn.execute("SELECT body FROM messages ORDER BY id DESC").fetchone()[0]
    assert "First" in body and "Second" in body and "https://x/i8" in body and "https://x/i9" in body


def test_owner_prefers_the_live_session_on_the_branch(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "c1", "mentions")
        conn.execute("INSERT INTO refs VALUES ('c1', ?, 'prompt', 0, 9, 0, 1)", (REF,))
        add_session(conn, "c2", "onbranch", branch="dev/x")
    owner = find_owner(conn, CONFIG, REF, "Org/app", "dev/x")
    assert owner is not None and (owner.handle, owner.via) == ("onbranch", "branch")


def test_a_session_that_only_reads_the_pr_or_checks_out_its_branch_owns_nothing(tmp_path: Path) -> None:
    """A worker that only read the PR and checked out its branch owns nothing; the lead whose work names it does."""
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "reader", "proj-7-worker", branch="dev/x", wrote=False)
        conn.execute("INSERT INTO refs VALUES ('reader', ?, 'pr-link', 0, 9, 0, 1)", (REF,))
        add_session(conn, "lead-c", "quinn")
        add_lead(conn, "Quinn", "s-quinn")
        add_work(conn, "quinn", REF)
    owner = find_owner(conn, CONFIG, REF, "Org/app", "dev/x")
    assert owner is not None and (owner.handle, owner.via) == ("quinn", "lead")
    with transaction(conn):
        conn.execute("UPDATE lead_work SET done_at = 1")
        conn.execute("DELETE FROM refs")
    assert find_owner(conn, CONFIG, REF, "Org/app", "dev/x") is None
    with transaction(conn):
        add_session(conn, "writer", "fixer", branch="dev/x")
    owner = find_owner(conn, CONFIG, REF, "Org/app", "dev/x")
    assert owner is not None and (owner.handle, owner.via) == ("fixer", "branch")


def test_the_session_holding_the_prs_claim_owns_it_before_a_lead_its_branch_names(tmp_path: Path) -> None:
    """The session holding the PR's claim owns it, even while the PR's branch names another lead's issue."""
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "lead-c", "quinn")
        add_lead(conn, "Quinn", "s-quinn")
        add_work(conn, "quinn", REF)
        add_session(conn, "bp", "backporter")
        conn.execute(
            "INSERT INTO claims (resource, token, holder_conversation, holder_handle, acquired_at, renewed_at,"
            " expires_at) VALUES (?, 't', 'bp', 'backporter', 1, 2, 9999999999999)",
            (REF.lower(),),
        )
    owner = find_owner(conn, CONFIG, REF, "Org/app", "dev/x")
    assert owner is not None and (owner.handle, owner.via) == ("backporter", "claim")
    with transaction(conn):
        conn.execute("UPDATE instances SET ended_at = 5 WHERE conversation_id = 'bp'")
    owner = find_owner(conn, CONFIG, REF, "Org/app", "dev/x")
    assert owner is not None and owner.handle == "quinn"


def test_a_leads_declared_work_comes_before_the_session_that_pushed_the_branch(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "w", "worker", branch="dev/x")
        add_session(conn, "lead-c", "quinn")
        add_lead(conn, "Quinn", "s-quinn")
        add_work(conn, "quinn", REF)
    owner = find_owner(conn, CONFIG, REF, "Org/app", "dev/x")
    assert owner is not None and owner.handle == "quinn"


def test_an_ended_reader_of_the_branch_does_not_route_the_pr_to_its_lead(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "lead-c", "alex", session="tab-lead")
        add_lead(conn, "Alex", "s-alex")
        add_session(conn, "w-c", "worker", branch="dev/x", ended=99, session="tab-worker", wrote=False)
        conn.execute("INSERT INTO tab_parents (agterm_session, parent_session) VALUES ('tab-worker', 'tab-lead')")
    assert find_owner(conn, CONFIG, REF, "Org/app", "dev/x") is None


def test_git_commit_and_push_name_the_branches_a_command_writes() -> None:
    assert map_command_to_branch_writes('git add -A && git commit -m "x"') == [None]
    assert map_command_to_branch_writes("git push -u origin dev/x") == ["dev/x"]
    assert map_command_to_branch_writes("git push origin HEAD:refs/heads/dev/y --force-with-lease") == ["dev/y"]
    assert map_command_to_branch_writes("git -C ../wt push") == [None]
    assert map_command_to_branch_writes("git push origin HEAD") == [None]
    assert map_command_to_branch_writes("git push --dry-run origin x; git log --oneline; gh pr view 7") == []
    assert map_command_to_branch_writes("git checkout dev/x && git pull && git show HEAD") == []


def test_branch_writes_come_from_scanned_commands_on_the_sessions_branch(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x", wrote=False)
        record_branch_writes(conn, "c1", [("git checkout dev/x", 5), ("git commit -am fix", 7), ("git push", 9)])
        record_branch_writes(conn, "c1", [("git push origin HEAD:other-branch", 11)])
    rows = conn.execute("SELECT branch, at FROM review_branch_writes ORDER BY branch").fetchall()
    assert [tuple(r) for r in rows] == [("dev/x", 9), ("other-branch", 11)]


def test_owner_is_the_most_recent_session_with_the_ref_when_none_is_on_the_branch(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "c1", "older")
        add_session(conn, "c2", "newer")
        conn.execute("INSERT INTO refs VALUES ('c1', ?, 'prompt', 0, 5, 0, 1)", (REF,))
        conn.execute("INSERT INTO refs VALUES ('c2', ?, 'prompt', 0, 9, 0, 1)", (REF,))
    owner = find_owner(conn, CONFIG, REF, "Org/app", "dev/x")
    assert owner is not None and owner.handle == "newer"


def test_owner_is_the_parent_lead_after_the_worker_ended(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "lead-c", "alex", session="tab-lead")
        add_lead(conn, "Alex", "s-alex")
        add_session(conn, "w-c", "worker", branch="dev/x", ended=99, session="tab-worker")
    tree: Any = {"workspaces": [{"sessions": [{"id": "tab-worker", "parent": "tab-lead"}]}]}
    with transaction(conn):
        record_tab_parents(conn, tree)
    owner = find_owner(conn, CONFIG, REF, "Org/app", "dev/x")
    assert owner is not None and (owner.handle, owner.lead, owner.via) == ("alex", "Alex", "lead")


def test_a_locked_tab_is_no_owner(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "c1", "secret", branch="dev/x")
        conn.execute("UPDATE surfaces SET tab_name = '🔒 mine' WHERE handle = 'secret'")
    assert find_owner(conn, CONFIG, REF, "Org/app", "dev/x") is None


def test_the_view_shows_state_and_rounds_from_owner_comments_and_commits(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    assert map_db_to_reviews(conn, CONFIG, 0)["prs"][0]["state"] == "waiting for you"  # type: ignore[index]
    store(conn, comment(at=2000))
    wake(conn)
    view: Any = map_db_to_reviews(conn, CONFIG, 0)
    assert (view["prs"][0]["state"], view["prs"][0]["rounds"]) == ("comments open", 0)
    with transaction(conn):
        store_open_prs(conn, [make_pr(head_oid="b2", commits=(("a1", 1000), ("b2", 3000)))], 600)
    view = map_db_to_reviews(conn, CONFIG, 0)
    assert (view["prs"][0]["state"], view["prs"][0]["rounds"]) == ("fixed", 1)


def test_reviewers_on_a_pr_do_not_make_it_published(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path, requested=1, approved_by=("ann",))
    view: Any = map_db_to_reviews(conn, CONFIG, 0)
    row = view["prs"][0]
    assert (row["state"], row["can_publish"], row["group"]) == ("waiting for you", True, "waiting")
    assert row["approved_by"] == ["ann"]


def test_a_failed_poll_keeps_rows_and_reads_nothing_as_closed(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with mock.patch("bridge.reviews.fetch_open_prs", side_effect=GitHubError("gh failed: 502")):
        poll_review_queue(conn, CONFIG, 900, None, None)
    view: Any = map_db_to_reviews(conn, CONFIG, 0)
    assert len(view["prs"]) == 1 and view["error"] == "gh failed: 502"


def test_a_pr_missing_from_search_is_closed_only_when_github_says_so(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with (
        mock.patch("bridge.reviews.fetch_open_prs", return_value=[]),
        mock.patch("bridge.reviews.fetch_prs", return_value={REF: "not found or no access"}),
    ):
        poll_review_queue(conn, CONFIG, 900, None, None)
    assert conn.execute("SELECT closed_at FROM review_prs").fetchone()[0] is None
    status = mock.Mock(spec=["state"])
    status.state = "MERGED"
    with (
        mock.patch("bridge.reviews.fetch_open_prs", return_value=[]),
        mock.patch("bridge.reviews.fetch_prs", return_value={REF: status}),
        mock.patch("bridge.reviews.PrStatus", mock.Mock),
    ):
        poll_review_queue(conn, CONFIG, 900, None, None)
    assert conn.execute("SELECT closed_at FROM review_prs").fetchone()[0] == 900


def test_comments_are_read_only_when_github_says_the_pr_changed(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with (
        mock.patch("bridge.reviews.fetch_open_prs", return_value=[make_pr()]),
        mock.patch("bridge.reviews.fetch_comments", return_value=[]) as fetched,
    ):
        poll_review_queue(conn, CONFIG, 900, None, None)
        poll_review_queue(conn, CONFIG, 990, None, None)
    assert fetched.call_count == 1


def test_all_three_comment_sources_are_read(tmp_path: Path) -> None:
    def row(number: int, login: str, body: str = "b", **more: object) -> dict[str, object]:
        at = f"2026-11-02T10:00:0{number}Z"
        user = {"login": login, "type": "Bot" if login.endswith("[bot]") else "User"}
        return {"id": number, "user": user, "created_at": at, "body": body, "html_url": "u", **more}

    pages = {
        "pulls/7/comments": [[row(1, "bot"), row(2, "me", in_reply_to_id=1, pull_request_review_id=5)]],
        "issues/7/comments": [[row(3, "me")]],
        "pulls/7/reviews": [
            [
                row(4, "me", "sum", submitted_at="2026-11-02T10:00:04Z", state="COMMENTED"),
                row(5, "me", "", submitted_at="2026-11-02T10:00:05Z", state="COMMENTED"),
                row(8, "me", "", state="PENDING"),
                row(6, "mira", "", submitted_at="2026-11-02T10:00:06Z", state="APPROVED", commit_id="a1"),
                row(7, "ci[bot]", "", submitted_at="2026-11-02T10:00:07Z", state="COMMENTED"),
            ]
        ],
    }

    def fake(args: list[str]) -> str:
        return json.dumps(next(v for k, v in pages.items() if k in args[-1]))

    with mock.patch("bridge.review_github.run_gh", side_effect=fake):
        comments = fetch_comments("Org/app", 7)
    assert [(c.comment_id, c.parent_author) for c in comments] == [
        ("p1", None),
        ("p2", "bot"),
        ("i3", None),
        ("r4", None),
        ("r5", None),
        ("r6", None),
        ("r7", None),
    ]
    assert comments[1].review_id == "r5"
    assert (comments[5].state, comments[5].commit_id, comments[5].by_app) == ("APPROVED", "a1", False)
    assert comments[6].by_app


def write_transcript(path: Path, command: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": "2026-11-02T10:00:00Z",
        "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": command}}]},
    }
    path.write_text(json.dumps(entry) + "\n")


def test_the_transcript_scan_marks_comments_from_a_session_and_its_subagents(
    tmp_path: Path, bridge_env: BridgeEnv
) -> None:
    conn = bridge_env.connect()
    transcript = tmp_path / "c1.jsonl"
    write_transcript(transcript, "gh pr comment 7 --body-file r.md")
    write_transcript(tmp_path / "c1" / "subagents" / "agent-a.jsonl", 'gh api repos/O/R/issues/8/comments -f body="x"')
    with transaction(conn):
        add_session(conn, "c1", "worker")
        conn.execute("UPDATE conversations SET transcript_path = ? WHERE conversation_id = 'c1'", (str(transcript),))
    scan_transcripts(conn, (), 1_800_000_000_000)
    marks = {(r["repo"], r["number"]) for r in conn.execute("SELECT repo, number FROM review_agent_marks")}
    assert marks == {(None, 7), ("O/R", 8)}
    scan_transcripts(conn, (), 1_800_000_000_000)
    assert conn.execute("SELECT count(*) FROM review_agent_marks").fetchone()[0] == 2


def test_the_queue_is_off_without_config_so_serve_calls_no_gh() -> None:
    assert not Config().review_org
    assert Config().review_post_dry_run is True


def test_a_batch_of_long_comments_fits_one_message_with_every_link(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
    store(conn, *[comment(f"i{n}", at=1000 + n, body="x" * 1500) for n in range(4)])
    assert len(wake(conn)) == 1
    body = conn.execute("SELECT body FROM messages").fetchone()[0]
    assert all(f"https://x/i{n}" in body for n in range(4))


def test_a_multiline_command_and_post_graphql_are_recognised() -> None:
    multiline = 'gh api \\\n  -X POST repos/Org/app/issues/7/comments \\\n  -f body="Fixed"'
    assert map_command_to_comment_targets(multiline) == [("Org/app", 7)]
    graphql = "gh api -X POST graphql -f query='mutation { addComment(input: {}) { clientMutationId } }'"
    assert map_command_to_comment_targets(graphql) == [(None, None)]


def test_a_mark_matches_whatever_the_repos_casing(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        record_agent_marks(conn, "c1", [("gh api -X POST repos/org/APP/issues/7/comments -f body='x'", 5)])
    assert len(load_marks(conn, "Org/app", 7)) == 1


def test_an_old_exact_match_is_still_the_agents() -> None:
    late = Mark(1_000_000 - 900_000, 'gh pr checks 7 --watch && gh pr comment 7 -b "Please rename"')
    assert classify(marks=[late]) == AGENT


def test_a_command_that_waited_on_ci_leaves_a_later_comment_unclear() -> None:
    late = Mark(1_000_000 - 900_000, "gh pr checks 7 --watch && gh pr comment 7 --body-file r.md")
    assert classify(marks=[late]) == UNCLEAR
    plain = Mark(1_000_000 - 900_000, "gh pr comment 7 --body-file r.md")
    assert classify(marks=[plain]) == OWNER


def test_until_in_a_comments_text_is_not_a_command_that_waited() -> None:
    held = Mark(1_000_000 - 4_600_000, 'gh pr comment 7 -b "Held on purpose: until then it would be wrong."')
    assert classify(marks=[held]) == OWNER


def test_editing_a_comment_is_not_a_mark_and_a_numberless_path_keeps_its_repo() -> None:
    edit = 'gh api -X PATCH repos/Org/docs/issues/comments/66 -f body="x"'
    assert map_command_to_comment_targets(edit) == []
    assert map_command_to_comment_targets("gh api --method DELETE repos/Org/app/pulls/comments/4") == []
    reaction = "gh api -X POST repos/Org/docs/issues/comments/66/reactions -f content=+1"
    assert map_command_to_comment_targets(reaction) == [("Org/docs", None)]


def test_owner_comment_wakes_the_owner_after_a_session_edited_a_comment_elsewhere_with_until_in_it(
    tmp_path: Path,
) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
    created = 2_000_000_000
    edit = (
        'guild ack 6 "Nothing for me"\n'
        'gh api -X PATCH repos/Org/docs/issues/comments/66 -f body="🤖 Claude: Approved, held until it ships." '
        "--jq .id\n"
        "for n in 6 7; do gh pr view $n --repo Org/app --json body --jq .body | grep -noiE worker; done"
    )
    with transaction(conn):
        record_agent_marks(conn, "c1", [(edit, created - 4_654_000)])
    store(conn, comment("p44", at=created, body="Why is this value fixed?"))
    sent = wake(conn, created + 120_000)
    assert [r.handle for _, r in sent] == ["worker"]
    assert conn.execute("SELECT verdict FROM review_comments").fetchone()[0] == OWNER


def test_a_comment_in_the_agents_prefix_is_an_agents_whatever_the_marks() -> None:
    assert classify("🤖 Claude: Fixed in 93e") == AGENT


def test_after_the_prefix_rule_a_reply_without_it_or_a_mark_is_owner() -> None:
    later = PREFIX_RULE_AT + 1000
    assert classify(at=later, parent="p1", parent_author="bot") == OWNER
    assert classify(at=later, parent="p1", parent_author="me", parent_verdict=AGENT) == OWNER
    assert classify(at=PREFIX_RULE_AT - 1000, parent="p1", parent_author="bot") == UNCLEAR


def test_a_colleagues_comment_wakes_the_owner_with_the_ownership_rule(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
    store(conn, comment("p1", author="mira", body="This leaks the token"))
    sent = wake(conn)
    assert [r.handle for _, r in sent] == ["worker"]
    body = conn.execute("SELECT body FROM messages").fetchone()[0]
    assert "mira commented" in body and "This leaks the token" in body and "mira: https://x/p1" in body
    assert "Fix what this PR introduced" in body and "The owner commented" not in body
    assert conn.execute("SELECT verdict FROM review_comments").fetchone()[0] == COLLEAGUE
    assert wake(conn) == []


def test_the_review_bots_change_request_wakes_the_owner_and_an_agents_reply_does_not(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
    store(
        conn,
        comment("r1", author="bot", body="A negative limit skips the check", state="CHANGES_REQUESTED", commit="a1"),
        comment("p2", at=1001, body="🤖 Claude: Fixed in 93e"),
    )
    assert len(wake(conn)) == 1
    body = conn.execute("SELECT body FROM messages").fetchone()[0]
    assert "bot (review bot) requested changes" in body and "negative limit" in body and "93e" not in body
    verdicts = dict(conn.execute("SELECT comment_id, verdict FROM review_comments").fetchall())
    assert verdicts == {"r1": REVIEWER, "p2": AGENT}


def test_a_colleagues_approval_wakes_with_the_merge_rule_once_per_head(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path, approved_by=("mira",))
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
    store(conn, comment("r1", author="mira", body="", state="APPROVED", commit="a1"))
    assert len(wake(conn, 200_000)) == 1
    assert (
        "mira approved: apply the merge rule (bridge skill)" in conn.execute("SELECT body FROM messages").fetchone()[0]
    )
    store(conn, comment("r2", at=1001, author="mira", body="", state="APPROVED", commit="a1"))
    assert wake(conn, 200_001) == []
    store(conn, comment("r3", at=1002, author="mira", body="", state="APPROVED", commit="b2"))
    assert len(wake(conn, 200_002)) == 1


def test_a_review_bot_exchange_is_capped_and_a_colleague_waits_out_the_cap(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
    for n in range(3):
        store(conn, comment(f"p{n}", at=1000 + n, author="bot"))
        assert len(wake(conn, 200_000 + n)) == 1
    store(conn, comment("p8", at=1008, author="bot"), comment("p9", at=1009, author="mira"))
    assert wake(conn, 200_100) == []
    verdicts = dict(conn.execute("SELECT comment_id, verdict FROM review_comments WHERE told_at IS NULL").fetchall())
    assert verdicts == {"p8": UNCLEAR, "p9": COLLEAGUE}
    assert len(wake(conn, 200_000 + 3_600_001)) == 1
    assert "mira commented" in conn.execute("SELECT body FROM messages ORDER BY id DESC").fetchone()[0]


def test_others_comments_from_before_bridge_read_them_wake_nobody_but_a_standing_approval_does(
    tmp_path: Path,
) -> None:
    conn = setup_pr(tmp_path, approved_by=("mira",))
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
        conn.execute("UPDATE review_prs SET others_from = 5000")
    store(
        conn,
        comment("p1", at=3000, author="kai"),
        comment("r2", at=3000, author="kai", body="", state="APPROVED"),
        comment("r3", at=3001, author="mira", body="", state="APPROVED"),
    )
    assert len(wake(conn)) == 1
    verdicts = dict(conn.execute("SELECT comment_id, verdict FROM review_comments").fetchall())
    assert verdicts == {"p1": OLD, "r2": OLD, "r3": COLLEAGUE}


def test_open_review_threads_show_on_the_row(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path, open_threads=5)
    with transaction(conn):
        store_review_threads(conn, REF, "k", ReviewThreads(4, ()))
    assert map_db_to_reviews(conn, CONFIG, 1000)["prs"][0]["open_threads"] == 4  # type: ignore[index]


def test_a_long_batch_of_review_bot_findings_still_fits_one_message(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
    long_id = "p" + "9" * 60
    store(conn, *[comment(f"{long_id}{n}", at=1000 + n, author="bot", body="finding " * 40) for n in range(25)])
    assert len(wake(conn)) == 1
    body = conn.execute("SELECT body FROM messages").fetchone()[0]
    assert len(body) <= BODY_MAX_CHARS and "more on https://github.com/Org/app/pull/7" in body
    assert conn.execute("SELECT count(*) FROM review_comments WHERE told_at IS NULL").fetchone()[0] == 0


def test_owner_account_is_owner_whatever_the_casing(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    store(conn, comment("p1", author="ME", body="🤖 Claude: Fixed"))
    assert wake(conn) == []
    assert conn.execute("SELECT verdict FROM review_comments").fetchone()[0] == AGENT


def test_an_approval_dismissed_before_its_wake_wakes_nobody(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path, approved_by=("mira",))
    store(conn, comment("r1", author="mira", body="", state="APPROVED", commit="a1"))
    assert wake(conn) == []
    with transaction(conn):
        store_open_prs(conn, [make_pr()], 600)
        add_session(conn, "c1", "worker", branch="dev/x")
    assert wake(conn) == []
    assert conn.execute("SELECT verdict, reason FROM review_comments").fetchone()[:] == (OLD, "no longer approved")


def test_a_method_inside_a_comments_text_is_prose_and_a_quoted_method_is_read() -> None:
    posted = 'gh api -X POST repos/Org/app/issues/7/comments -f body="Use -X GET for this endpoint"'
    assert map_command_to_comment_targets(posted) == [("Org/app", 7)]
    assert map_command_to_comment_targets('gh api -X "PATCH" repos/Org/app/issues/comments/4 -f body="x"') == []


def test_a_wait_inside_a_script_for_bash_still_counts() -> None:
    late = Mark(1_000_000 - 900_000, "bash -lc 'gh pr checks 7 --watch && gh pr comment 7 --body-file r.md'")
    assert classify(marks=[late]) == UNCLEAR


def test_owner_review_with_no_text_is_owner_round_and_adds_nothing_to_the_message(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path, commits=(("a1", 900), ("b2", 1500)))
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
    store(
        conn,
        comment("p1", at=1000, body="Why is this hardcoded?", review="r9"),
        comment("r9", at=1200, body="", state="COMMENTED"),
    )
    assert len(wake(conn)) == 1
    body = conn.execute("SELECT body FROM messages").fetchone()[0]
    assert "https://x/p1" in body and "https://x/r9" not in body
    verdicts = dict(conn.execute("SELECT comment_id, verdict FROM review_comments").fetchall())
    assert verdicts == {"p1": OWNER, "r9": OWNER}
    views: Any = map_db_to_reviews(conn, CONFIG, 2000)["prs"]
    view = views[0]
    assert (view["owner_last_at"], view["commits_after_owner"], view["rounds"], view["state"]) == (1200, 1, 1, "fixed")


def test_a_review_wrapping_an_agents_reply_is_the_agents_and_waits_for_its_comments(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    store(conn, comment("r9", at=1000, body="", state="COMMENTED"))
    with transaction(conn):
        conn.execute("UPDATE review_comments SET created_at = 1000")
    store(conn, comment("p1", at=1000, body="🤖 Claude: Fixed in 93e", review="r9"))
    assert wake(conn) == []
    verdicts = dict(conn.execute("SELECT comment_id, verdict FROM review_comments").fetchall())
    assert verdicts["p1"] == AGENT
    wake(conn)
    assert conn.execute("SELECT verdict FROM review_comments WHERE comment_id = 'r9'").fetchone()[0] == AGENT


def test_a_comment_stored_before_reviews_were_read_learns_its_review(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    store(conn, comment("p1", at=1000, body="Why is this hardcoded?"))
    with transaction(conn):
        conn.execute("UPDATE review_comments SET author = NULL, review_id = NULL")
    store(
        conn,
        comment("p1", at=1000, body="Why is this hardcoded?", review="r9"),
        comment("r9", at=1200, body="", state="COMMENTED"),
    )
    wake(conn)
    assert conn.execute("SELECT verdict FROM review_comments WHERE comment_id = 'r9'").fetchone()[0] == OWNER


def test_comments_reread_after_the_marking_fix_become_owner_without_a_wake(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
    store(conn, comment("p1", at=1100, body="Why is this hardcoded?"))
    with transaction(conn):
        conn.execute("UPDATE review_comments SET verdict = NULL, reason = NULL, quiet = 1")
    assert wake(conn) == []
    row = conn.execute("SELECT verdict, told_at FROM review_comments").fetchone()
    assert row["verdict"] == OWNER and row["told_at"] is not None
    assert map_db_to_reviews(conn, CONFIG, 2000)["prs"][0]["state"] == "comments open"  # type: ignore[index]


def test_comments_reread_after_the_fix_do_not_use_up_the_wake_cap(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
    store(conn, *[comment(f"p{n}", at=150_000 + n, body=f"Old {n}") for n in range(3)])
    with transaction(conn):
        conn.execute("UPDATE review_comments SET quiet = 1")
    assert wake(conn, 200_000) == []
    store(conn, comment("p9", at=160_000, body="New"))
    assert len(wake(conn, 200_100)) == 1


def test_since_the_prefix_rule_the_prefix_alone_decides_and_nothing_shows_as_unclear(tmp_path: Path) -> None:
    later = PREFIX_RULE_AT + 60_000
    near = Mark(later - 1000, 'gh pr comment 7 -b "Please rename"')
    assert classify(at=later, marks=[near], remote=True, parent="p1", parent_author="bot") == OWNER
    assert classify("🤖 Claude: Please rename", at=later) == AGENT
    assert classify(marks=[Mark(1_000_000 - 30_000, 'gh pr comment 7 -b "x"')]) == UNCLEAR
    conn = setup_pr(tmp_path)
    store(conn, comment("p1", body="x"))
    with transaction(conn):
        conn.execute("UPDATE review_comments SET verdict = ?", (UNCLEAR,))
    view = map_db_to_reviews(conn, CONFIG, 2000)["prs"][0]  # type: ignore[index]
    assert not [key for key in view if "unclear" in key]  # type: ignore[union-attr]
    assert "unclear" not in (Path(__file__).parent.parent / "src/bridge/web/guild.html").read_text()


def test_what_bridge_cannot_place_goes_to_the_owner_once_and_never_to_the_session(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
        record_agent_marks(conn, "c2", [('gh pr comment 7 -b "other"', 990)])
    store(conn, comment("p1", at=1000, body="Is this right?"), comment("p2", at=1001, body="And this?", author="ME"))
    sent = wake(conn)
    assert len(sent) == 1
    body = conn.execute("SELECT body FROM messages").fetchone()[0]
    assert "cannot tell whether these comments" in body and "https://x/p1" in body and "Is this right?" in body
    assert "The owner commented" not in body
    assert wake(conn) == []


def test_comments_reread_and_still_unplaced_ask_nobody(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
        record_agent_marks(conn, "c2", [('gh pr comment 7 -b "other"', 990)])
    store(conn, comment("p1", at=1000, body="Is this right?"))
    with transaction(conn):
        conn.execute("UPDATE review_comments SET quiet = 1")
    assert wake(conn) == []
    assert conn.execute("SELECT verdict FROM review_comments").fetchone()[0] == UNCLEAR


def test_owner_open_thread_or_unanswered_comment_blocks_the_merge_whatever_the_approvals(tmp_path: Path) -> None:
    pr = make_pr(approved_by=("bot",), open_threads=1, thread_flags="o")
    conn = setup_pr(tmp_path, **{"approved_by": ("bot",), "open_threads": 1, "thread_flags": "o"})
    with transaction(conn):
        store_review_threads(conn, REF, map_pr_to_threads_key(pr), ReviewThreads(1, (thread(1, replies=0),)))
        store_merge_blocks(conn)
    view = map_db_to_reviews(conn, CONFIG, 2000)["prs"][0]  # type: ignore[index]
    assert view["merge_block"] == "1 review thread is unresolved"  # type: ignore[index]
    with transaction(conn):
        store_open_prs(conn, [make_pr(approved_by=("bot",))], 600)
        store_merge_blocks(conn)
    assert conn.execute("SELECT merge_block FROM review_prs").fetchone()[0] is None
    store(conn, comment("i1", at=2000, body="Why this?"))
    with transaction(conn):
        classify_pending_comments(conn, CONFIG, 100_000)
        store_merge_blocks(conn)
    assert "no answer and no commit after it" in conn.execute("SELECT merge_block FROM review_prs").fetchone()[0]
    store(conn, comment("i2", at=2500, body="🤖 Claude: Because the API needs it."))
    with transaction(conn):
        classify_pending_comments(conn, CONFIG, 100_000)
        store_merge_blocks(conn)
    assert conn.execute("SELECT merge_block FROM review_prs").fetchone()[0] is None


def review_thread(
    login: str,
    resolved: bool = False,
    url: str = "u",
    replies: list[tuple[int, str, str]] | None = None,
    pending: bool = False,
    last_pending: bool = False,
) -> Any:
    first = {
        "author": {"login": login},
        "url": url,
        "pullRequestReview": {"state": "PENDING" if pending else "COMMENTED"},
    }
    last_id, last_login, body = (replies or [(1, login, "first words")])[-1]
    last = {
        "databaseId": last_id, "author": {"login": last_login}, "body": body,
        "pullRequestReview": {"state": "PENDING" if last_pending or pending else "COMMENTED"},
    }  # fmt: skip
    return {
        "isResolved": resolved,
        "comments": {"nodes": [first]},
        "lastComment": {"totalCount": 1 + len(replies or []), "nodes": [last]},
    }


def test_owner_unresolved_threads_are_counted_from_who_started_them() -> None:
    threads = map_threads_to_review_threads(
        [review_thread("Me"), review_thread("me", resolved=True), review_thread("bot")], "me"
    )
    assert (threads.open, len(threads.theirs)) == (2, 1)


def test_owner_thread_carries_its_link_and_its_last_reply() -> None:
    threads = map_threads_to_review_threads(
        [
            review_thread("me", url="https://github.com/Org/app/pull/7#r1", replies=[(9, "me", "🤖 Claude: Renamed.")]),
            review_thread("me", url="https://github.com/Org/app/pull/7#r2"),
        ],
        "me",
    )
    assert threads.theirs == (
        OwnerThread("https://github.com/Org/app/pull/7#r1", 1, "p9", "me", "🤖 Claude: Renamed."),
        OwnerThread("https://github.com/Org/app/pull/7#r2", 0, None, "me", "first words"),
    )


def test_an_unsubmitted_review_stays_out_and_its_words_are_never_kept() -> None:
    threads = map_threads_to_review_threads(
        [
            review_thread("me", pending=True),
            review_thread("me", replies=[(9, "kai", "Fixed."), (10, "me", "my draft reply")], last_pending=True),
        ],
        "me",
    )
    assert threads.open == 1
    assert threads.theirs == (OwnerThread("u", 2, None, "me", ""),)


def thread(n: int, replies: int = 1, author: str = "me", body: str = "🤖 Claude: Done, renamed.") -> OwnerThread:
    return OwnerThread(f"https://github.com/Org/app/pull/7#discussion_r{n}", replies, f"p{n}9", author, body)


def resolve_view(conn: Any, *threads: OwnerThread, **changes: Any) -> Any:
    pr = make_pr(
        **{"approved_by": ("bot",), "open_threads": len(threads), "thread_flags": "o" * len(threads), **changes}
    )
    with transaction(conn):
        store_open_prs(conn, [pr], 600)
        store_review_threads(conn, REF, map_pr_to_threads_key(pr), ReviewThreads(pr.open_threads, threads))
        store_merge_blocks(conn)
    return map_db_to_reviews(conn, CONFIG, 2000)["prs"][0]  # type: ignore[index]


def test_resolving_is_all_that_is_left_only_on_a_ready_approved_green_pr_without_other_threads(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    assert resolve_view(conn, thread(1))["resolve_is_last"] is True
    assert resolve_view(conn, thread(1), checks=None)["resolve_is_last"] is True
    for changes in (
        {"checks": "FAILURE"},
        {"checks": "PENDING"},
        {"approved_by": ()},
        {"draft": True},
        {"merge_state": "DIRTY"},
        {"open_threads": 2, "thread_flags": "oo"},
    ):
        view = resolve_view(conn, thread(1), **changes)
        assert view["resolve_is_last"] is False, changes
        assert len(view["resolve_threads"]) == 1, changes


def test_threads_not_read_since_they_changed_block_the_merge_and_offer_nothing_to_resolve(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    resolve_view(conn, thread(1))
    with transaction(conn):
        store_open_prs(conn, [make_pr(approved_by=("bot",), open_threads=2, thread_flags="oo")], 700)
        store_merge_blocks(conn)
    view = map_db_to_reviews(conn, CONFIG, 2000)["prs"][0]  # type: ignore[index]
    assert "could not be read yet" in view["merge_block"]  # type: ignore[index]
    assert view["resolve_threads"] == []  # type: ignore[index]


def test_owner_answered_thread_on_an_approved_green_pr_is_owner_to_resolve(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    view = resolve_view(conn, thread(1))
    assert view["merge_block"] == "1 review thread is unresolved"
    assert view["resolve_threads"] == [
        {"url": "https://github.com/Org/app/pull/7#discussion_r1", "quote": "Done, renamed."}
    ]
    assert resolve_view(conn, thread(1), thread(2, author="kai", body="x" * 200))["resolve_threads"][1]["quote"] == (
        "x" * 139 + "…"
    )


def test_a_thread_still_waiting_on_its_owner_keeps_the_ball_with_the_owner(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    assert resolve_view(conn, thread(1), thread(2, replies=0, body="Why?"))["resolve_threads"] == []
    assert resolve_view(conn, thread(1, body="And this too?"))["resolve_threads"] == []
    store(conn, comment("i1", at=2000, body="One more thing."))
    with transaction(conn):
        classify_pending_comments(conn, CONFIG, 100_000)
    assert resolve_view(conn, thread(1))["resolve_threads"] == []


def test_a_reply_under_owner_account_before_the_prefix_counts_by_its_verdict(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    store(conn, comment("p19", at=1500, body="Renamed it."))
    with transaction(conn):
        conn.execute("UPDATE review_comments SET verdict = ?", (AGENT,))
    assert len(resolve_view(conn, thread(1, body="Renamed it."))["resolve_threads"]) == 1


def test_threads_are_read_only_when_the_pr_or_its_open_count_changed(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    reads: list[int] = []

    def fetch(_config: Config, _repo: str, number: int) -> ReviewThreads:
        reads.append(number)
        return ReviewThreads(1, (thread(1),))

    def poll(pr: OpenPr) -> None:
        with (
            mock.patch("bridge.reviews.fetch_open_prs", return_value=[pr]),
            mock.patch("bridge.reviews.fetch_comments", return_value=[]),
            mock.patch("bridge.reviews.fetch_review_threads", side_effect=fetch),
        ):
            poll_review_queue(conn, CONFIG, 5000, None, None)

    poll(make_pr())
    poll(make_pr(open_threads=1, thread_flags="o"))
    poll(make_pr(open_threads=1, thread_flags="o"))
    assert reads == [7]
    assert conn.execute("SELECT owner_open_threads FROM review_prs").fetchone()[0] == 1
    poll(make_pr(open_threads=1, thread_flags="o", updated_at="2026-11-02T11:00:00Z"))
    assert reads == [7, 7]
    poll(make_pr(open_threads=1, thread_flags="ro", updated_at="2026-11-02T11:00:00Z"))
    assert reads == [7, 7, 7]
    poll(make_pr(open_threads=0, thread_flags="rr", updated_at="2026-11-02T11:00:00Z"))
    assert reads == [7, 7, 7]
    assert conn.execute("SELECT owner_open_threads, owner_threads FROM review_prs").fetchone()[:] == (0, "[]")


def test_marks_outlive_an_outage_until_comments_are_classified(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    created = 2_000_000_000
    with transaction(conn):
        record_agent_marks(conn, "c1", [("gh pr comment 7 --body-file r.md", created)])
    store(conn, comment("i1", at=created))
    later = created + 25 * 3_600_000
    with (
        mock.patch("bridge.reviews.fetch_open_prs", return_value=[make_pr()]),
        mock.patch("bridge.reviews.fetch_comments", return_value=[]),
    ):
        poll_review_queue(conn, CONFIG, later, None, later)
    assert conn.execute("SELECT verdict FROM review_comments").fetchone()[0] == AGENT


def test_a_session_that_ended_right_after_posting_still_gets_scanned(tmp_path: Path, bridge_env: BridgeEnv) -> None:
    conn = bridge_env.connect()
    transcript = tmp_path / "c1.jsonl"
    write_transcript(transcript, "gh pr comment 7 --body-file r.md")
    now = 1_800_000_000_000
    with transaction(conn):
        add_session(conn, "c1", "worker", ended=now - 1000)
        conn.execute(
            "UPDATE conversations SET transcript_path = ?, last_seen_at = ? WHERE conversation_id = 'c1'",
            (str(transcript), now - 1000),
        )
    scan_transcripts(conn, (), now)
    assert conn.execute("SELECT count(*) FROM review_agent_marks").fetchone()[0] == 1


def test_a_closed_pr_wakes_nobody(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    store(conn, comment())
    assert wake(conn) == []
    with transaction(conn):
        conn.execute("UPDATE review_prs SET closed_at = 1")
        add_session(conn, "c1", "worker", branch="dev/x")
    assert wake(conn) == []


def test_an_ended_worker_in_another_repo_does_not_name_an_owner(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "lead-c", "alex", session="tab-lead")
        add_lead(conn, "Alex", "s-alex")
        add_session(conn, "w-c", "worker", branch="dev/x", repo="Org/other", ended=99, session="tab-worker")
        conn.execute("INSERT INTO tab_parents (agterm_session, parent_session) VALUES ('tab-worker', 'tab-lead')")
    assert find_owner(conn, CONFIG, REF, "Org/app", "dev/x") is None


def test_a_locked_worker_does_not_route_work_to_its_lead(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "lead-c", "alex", session="tab-lead")
        add_lead(conn, "Alex", "s-alex")
        add_session(conn, "w-c", "worker", session="tab-worker")
        conn.execute("INSERT INTO tab_parents (agterm_session, parent_session) VALUES ('tab-worker', 'tab-lead')")
        conn.execute("UPDATE surfaces SET tab_name = '🔒 w' WHERE handle = 'worker'")
        conn.execute("INSERT INTO refs VALUES ('w-c', ?, 'prompt', 0, 9, 0, 1)", (REF,))
    assert find_owner(conn, CONFIG, REF, "Org/app", "dev/x") is None


def test_two_fixes_between_polls_are_two_rounds(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    store(conn, comment("i1", at=2000), comment("i2", at=4000))
    wake(conn)
    with transaction(conn):
        store_open_prs(conn, [make_pr(head_oid="c3", commits=(("a1", 1000), ("b2", 3000), ("c3", 5000)))], 600)
    view: Any = map_db_to_reviews(conn, CONFIG, 0)
    assert (view["prs"][0]["state"], view["prs"][0]["rounds"]) == ("fixed", 2)


def test_a_many_file_pr_lists_its_files_only_for_a_new_head() -> None:
    node: Any = {
        "number": 7, "title": "T", "url": "u", "isDraft": False, "updatedAt": "U", "headRefName": "h",
        "headRefOid": "b2", "baseRefName": "main", "additions": 5, "deletions": 1,
        "repository": {"nameWithOwner": "Org/app"}, "baseRef": None, "commits": {"nodes": []},
        "files": {"pageInfo": {"hasNextPage": True}, "nodes": []},
        "reviewRequests": {"nodes": []}, "reviews": {"nodes": []}, "latestReviews": {"nodes": []},
    }  # fmt: skip
    with mock.patch("bridge.review_github.fetch_all_files", return_value=[]) as listed:
        assert map_node_to_open_pr(node, CONFIG, {(REF, "b2"): 42}).loc == 42
        listed.assert_not_called()
        assert map_node_to_open_pr(node, CONFIG, {(REF, "a1"): 42}).loc == 6
        listed.assert_called_once()


def test_rows_sort_waiting_then_published_then_drafts(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.db")
    prs = [
        make_pr(ref="pr:Org/app#1", number=1, draft=True),
        make_pr(ref="pr:Org/app#2", number=2),
        make_pr(ref="pr:Org/app#3", number=3),
    ]
    with transaction(conn):
        store_open_prs(conn, prs, 500)
        conn.execute("UPDATE review_prs SET reviewed_at = 1, reviewed_by = 'y' WHERE number = 2")
    view: Any = map_db_to_reviews(conn, CONFIG, 0)
    assert [p["number"] for p in view["prs"]] == [3, 2, 1]
    assert [p["group"] for p in view["prs"]] == ["waiting", "published", "drafts"]
    assert [p["can_publish"] for p in view["prs"]] == [True, False, False]


def test_merge_state_shows_on_the_row(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path, merge_state="BEHIND")
    view: Any = map_db_to_reviews(conn, CONFIG, 0)
    assert view["prs"][0]["merge_state"] == "BEHIND"
    with transaction(conn):
        store_open_prs(conn, [make_pr(in_merge_queue=True)], 600)
    view = map_db_to_reviews(conn, CONFIG, 0)
    assert view["prs"][0]["merge_state"] == "in queue"


def add_work(conn: Any, lead: str, *refs: str) -> None:
    conn.execute(
        "INSERT INTO lead_work (lead, name, refs, created_at, updated_at) VALUES (?, 'w', ?, 0, 0)",
        (lead, json.dumps(refs)),
    )


def test_the_assistant_owns_no_pr_however_often_it_mentions_one(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    assistant = Config(assistant_handle="helm", review_org="Org", review_author="me")
    with transaction(conn):
        add_session(conn, "z", "helm", branch="dev/x")
        conn.execute("INSERT INTO refs VALUES ('z', ?, 'prompt', 0, 9, 0, 1)", (REF,))
    assert find_owner(conn, assistant, REF, "Org/app", "dev/x") is None


def test_a_leads_declared_work_comes_before_session_refs(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "lead-c", "alex")
        add_lead(conn, "Alex", "s-alex")
        add_work(conn, "alex", REF)
        add_session(conn, "c1", "mentions")
        conn.execute("INSERT INTO refs VALUES ('c1', ?, 'prompt', 0, 9, 0, 1)", (REF,))
    owner = find_owner(conn, CONFIG, REF, "Org/app", "dev/x")
    assert owner is not None and (owner.handle, owner.lead, owner.via) == ("alex", "Alex", "lead")


def test_an_issue_in_the_title_or_branch_finds_the_lead_by_work_then_scope(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path, title="PROJ-33: per-team alerts")
    with transaction(conn):
        add_session(conn, "lead-c", "alex")
        add_lead(conn, "Alex", "s-alex")
        conn.execute("UPDATE leads SET scope = 'Search: indexing, PROJ-33' WHERE name = 'alex'")
    owner = find_owner(conn, CONFIG, REF, "Org/app", "dev/x")
    assert owner is not None and owner.handle == "alex"
    with transaction(conn):
        add_session(conn, "lead-b", "boris")
        add_lead(conn, "Boris", "s-boris")
        add_work(conn, "boris", "issue:PROJ-77")
    owner = find_owner(conn, CONFIG, REF, "Org/app", "dev/proj-77-thing")
    assert owner is not None and owner.handle == "boris"


def test_an_assistant_enrolled_as_a_lead_still_owns_nothing(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    assistant = Config(assistant_handle="helm", review_org="Org", review_author="me")
    with transaction(conn):
        add_session(conn, "z", "helm")
        add_lead(conn, "Helm", "s-helm")
        add_work(conn, "helm", REF)
    assert find_owner(conn, assistant, REF, "Org/app", "dev/x") is None


def test_a_leads_linear_refs_name_its_issues(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path, title="PROJ-77: x")
    with transaction(conn):
        add_session(conn, "lead-c", "alex")
        add_lead(conn, "Alex", "s-alex")
        conn.execute("""UPDATE leads SET linear_refs = '["PROJ-77"]' WHERE name = 'alex'""")
    owner = find_owner(conn, CONFIG, REF, "Org/app", "dev/x")
    assert owner is not None and owner.handle == "alex"


def test_finished_work_names_nobody(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "lead-c", "alex")
        add_lead(conn, "Alex", "s-alex")
        add_work(conn, "alex", REF)
        conn.execute("UPDATE lead_work SET done_at = 1")
    assert find_owner(conn, CONFIG, REF, "Org/app", "dev/x") is None


def ready_view(conn: Any, now: int = 200_000) -> tuple[dict[str, Any], list[Any]]:
    with transaction(conn):
        classify_pending_comments(conn, CONFIG, now)
        store_merge_blocks(conn)
        sent = tell_ready_to_merge(conn, CONFIG, now)
    prs = cast(list[dict[str, Any]], map_db_to_reviews(conn, CONFIG, now)["prs"])
    return prs[0], sent


def test_an_approved_green_pr_is_ready_for_its_lead_without_owner_review_and_its_owner_is_told_once(
    tmp_path: Path,
) -> None:
    conn = setup_pr(tmp_path, approved_by=("mira",))
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
    view, sent = ready_view(conn)
    assert view["merge_path"] == READY and view["group"] == READY and view["state"] == READY
    assert view["approvers"] == ["mira"] and view["approval_left"] == "ready to merge (lead)"
    assert view["can_publish"]
    assert [r.handle for _, r in sent] == ["worker"]
    body = conn.execute("SELECT body FROM messages WHERE id = ?", (sent[0][0],)).fetchone()[0]
    assert "https://github.com/Org/app/pull/7" in body and "is ready to merge: approved" in body
    assert "approved on GitHub by mira" in body
    assert ready_view(conn)[1] == []


def test_the_review_bots_approval_counts_and_owner_settled_comments_free_the_merge(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path, bot_approved=True)
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
    store(conn, comment("p1", at=2000, body="Too complex, refactor"))
    view, sent = ready_view(conn)
    assert view["merge_path"] is None and view["group"] == "waiting" and sent == []
    assert view["approvers"] == ["ReviewBot"] and view["approval_left"] == "your comments open"
    store(conn, comment("p2", at=2500, body="🤖 Claude: Refactored."))
    view, sent = ready_view(conn)
    assert view["merge_path"] == READY and view["approval_left"] == "ready to merge (lead)" and len(sent) == 1
    store(conn, comment("p3", at=3000, body="One more thing"))
    view, sent = ready_view(conn)
    assert view["merge_path"] is None and view["approval_left"] == "your comments open" and sent == []
    store(conn, comment("p4", at=3500, body="🤖 Claude: Done."))
    view, sent = ready_view(conn)
    assert view["merge_path"] == READY and len(sent) == 1
    assert ready_view(conn)[1] == []


@pytest.mark.parametrize(
    ("changes", "left"),
    [
        ({"checks": "FAILURE"}, "CI red"),
        ({"checks": "ERROR"}, "CI red"),
        ({"checks": "PENDING"}, "CI pending"),
        ({"checks": "EXPECTED"}, "CI pending"),
        ({"merge_state": "DIRTY"}, "conflicts"),
        ({"merge_state": "BLOCKED"}, "blocked on GitHub"),
        ({"checks": None}, "ready to merge (lead)"),
    ],
)
def test_an_approved_pr_waits_for_green_ci_and_no_conflicts_and_no_ci_counts_as_green(
    tmp_path: Path, changes: dict[str, Any], left: str
) -> None:
    conn = setup_pr(tmp_path, approved_by=("mira",), **changes)
    view, _ = ready_view(conn)
    assert view["approval_left"] == left
    assert (view["merge_path"] == READY) == (left == "ready to merge (lead)")


def test_owner_open_thread_keeps_an_approved_pr_from_merging(tmp_path: Path) -> None:
    pr = make_pr(bot_approved=True, open_threads=1, thread_flags="o")
    conn = setup_pr(tmp_path, bot_approved=True, open_threads=1, thread_flags="o")
    store(conn, comment("p1", at=2000, body="Rename this"), comment("p2", at=2500, body="🤖 Claude: Renamed."))
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
        store_review_threads(conn, REF, map_pr_to_threads_key(pr), ReviewThreads(1, (thread(1),)))
    view, sent = ready_view(conn)
    assert view["merge_block"] == "1 review thread is unresolved"
    assert view["merge_path"] is None and view["group"] == "waiting" and sent == []
    assert view["approval_left"] == "resolve your threads"


def test_a_published_ready_pr_sits_with_the_ready_ones_and_offers_no_second_post(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path, bot_approved=True)
    with transaction(conn):
        conn.execute("UPDATE review_prs SET reviewed_at = 2600, merge_path = 'published'")
    view, _ = ready_view(conn)
    assert view["merge_path"] == READY and view["group"] == READY and not view["can_publish"]


def test_an_unapproved_or_draft_pr_has_no_approval_words(tmp_path: Path) -> None:
    view, _ = ready_view(setup_pr(tmp_path))
    assert view["approvers"] == [] and view["approval_left"] is None and view["merge_path"] is None
    assert view["group"] == "waiting"
    (tmp_path / "d").mkdir()
    draft = ready_view(setup_pr(tmp_path / "d", draft=True, approved_by=("mira",), bot_approved=True))[0]
    assert draft["approvers"] == ["mira", "ReviewBot"] and draft["approval_left"] is None
    assert draft["merge_path"] is None and draft["group"] == "drafts"


def test_only_the_review_bots_latest_verdict_counts_as_its_approval() -> None:
    def review(login: str, state: str) -> dict[str, object]:
        return {"state": state, "author": {"__typename": "User", "login": login}}

    assert is_approved_by_review_bot([review("Bot", "APPROVED"), review("bot", "COMMENTED")], CONFIG)
    assert not is_approved_by_review_bot([review("bot", "APPROVED"), review("bot", "CHANGES_REQUESTED")], CONFIG)
    assert not is_approved_by_review_bot([review("mira", "APPROVED")], CONFIG)


def test_docs_and_images_are_not_lines_of_code_so_a_docs_only_pr_gets_no_document() -> None:
    files = [
        {"path": "docs/README.md", "additions": 400, "deletions": 50},
        {"path": "docs/images/prs.PNG", "additions": 0, "deletions": 0},
        {"path": "docs/diagram.svg", "additions": 120, "deletions": 0},
    ]
    assert map_files_to_loc(520, 50, files, Config().review_loc_excluded) == 0
    code = [*files, {"path": "app/main.py", "additions": 7, "deletions": 3}]
    assert map_files_to_loc(527, 53, code, Config().review_loc_excluded) == 10


def test_a_line_count_made_under_an_older_rule_is_made_again_for_the_same_head(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path, loc=1010)
    assert load_known_loc(conn) == {(REF, "a1"): 1010}
    with transaction(conn):
        conn.execute("UPDATE review_prs SET loc_rule = NULL")
    assert load_known_loc(conn) == {}
