import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from test_auto_merge import CLEAN, LIVE, ON, run
from test_auto_merge import row as auto_row
from test_auto_merge import setup as auto_setup
from test_reviews import CONFIG, REF, comment, make_pr, ready_view, review_thread, setup_reviewed_pr, store

from bridge.auto_merge import map_row_to_auto_merge_block
from bridge.db import transaction
from bridge.guard import (
    COLLEAGUE_COMMENTS_OPEN,
    GITHUB_STALE_S,
    find_merge_blocks,
    find_standing_rule_block,
    map_command_to_targets,
    map_merge_blocks_to_lines,
)
from bridge.review_github import ReviewThreads, map_threads_to_review_threads
from bridge.reviews import list_refs_with_new_activity, map_pr_to_threads_key, store_review_threads


def waiting(*threads: Any) -> tuple[str, ...]:
    return map_threads_to_review_threads(list(threads), "me", ("bot",)).colleagues


def test_a_colleagues_thread_waits_until_resolved_or_answered_from_our_account() -> None:
    assert waiting(review_thread("mira")) == ("mira",)
    assert waiting(review_thread("mira", replies=[(9, "mira", "nit: a blank line here")])) == ("mira",)
    assert waiting(review_thread("mira", resolved=True)) == ()
    # The owner's approved draft posted as a reply carries no prefix: a reply from the account is the answer.
    assert waiting(review_thread("mira", replies=[(9, "me", "Done in a1b2c3d.")])) == ()
    assert waiting(review_thread("mira", replies=[(9, "me", "🤖 Claude: Renamed.")])) == ()
    assert waiting(review_thread("mira", replies=[(9, "me", "my draft")], last_pending=True)) == ("mira",)
    assert waiting(review_thread("mira", replies=[(9, "me", "Done."), (10, "mira", "Not quite.")])) == ("mira",)


def test_threads_of_the_owners_account_the_review_bot_and_apps_are_left_to_their_own_rules() -> None:
    app = review_thread("ci-helper")
    app["comments"]["nodes"][0]["author"]["__typename"] = "Bot"
    assert waiting(review_thread("me"), review_thread("Bot"), app) == ()


def set_threads(conn: Any, *colleagues: str) -> None:
    pr = make_pr(approved_by=("mira",), open_threads=2, thread_flags="oo")
    with transaction(conn):
        store_review_threads(conn, REF, map_pr_to_threads_key(pr), ReviewThreads(len(colleagues), (), colleagues))


def test_an_approval_with_open_threads_is_not_ready_until_each_is_answered_or_resolved(tmp_path: Path) -> None:
    conn = setup_reviewed_pr(tmp_path, approved_by=("mira",), open_threads=2, thread_flags="oo")
    store(conn, comment("r1", at=2000, body="", author="mira", state="APPROVED"))
    set_threads(conn, "mira", "mira")
    view, sent = ready_view(conn)
    assert view["merge_path"] is None and sent == []
    assert view["approval_left"] == "colleague's comments open"
    assert view["colleague_block"] == "2 threads neither resolved nor answered (mira)"
    assert (
        view["merge_ok_needs"]
        == "a colleague's comments are not processed: 2 threads neither resolved nor answered (mira)"
    )
    set_threads(conn, "mira")
    assert ready_view(conn)[0]["merge_path"] is None, "a nit left open still waits"
    set_threads(conn)
    view, _ = ready_view(conn)
    assert view["merge_path"] == "ready" and view["colleague_block"] is None


def test_a_colleagues_comment_or_review_text_waits_for_a_word_from_our_account_after_it(tmp_path: Path) -> None:
    conn = setup_reviewed_pr(tmp_path, approved_by=("mira",))
    store(
        conn,
        comment("i1", at=2000, body="Why not reuse the cache?", author="kai"),
        comment("r1", at=2200, body="nit: naming", author="mira", state="COMMENTED"),
        comment("r2", at=2600, body="Please split this", author="kai", state="CHANGES_REQUESTED"),
        comment("i2", at=2300, body="Findings: none", author="bot"),
    )
    assert ready_view(conn)[0]["colleague_block"] == "3 comments unanswered (kai, mira)"
    store(conn, comment("i3", at=2400, body="🤖 Claude: The cache is per request."))
    assert ready_view(conn)[0]["colleague_block"] == "1 comment unanswered (kai)"
    store(conn, comment("i4", at=2800, body="🤖 Claude: Split into two PRs."))
    view, _ = ready_view(conn)
    assert view["colleague_block"] is None and view["merge_path"] == "ready"


def test_an_approvals_text_is_a_verdict_and_needs_no_answer(tmp_path: Path) -> None:
    conn = setup_reviewed_pr(tmp_path, approved_by=("mira",))
    store(
        conn,
        comment("r1", at=2000, body="", author="mira", state="APPROVED"),
        comment("r2", at=3000, body="All addressed, thanks", author="mira", state="APPROVED"),
    )
    view, _ = ready_view(conn)
    assert view["colleague_block"] is None and view["merge_path"] == "ready"


def test_a_pr_read_before_this_rule_waits_until_its_comments_are_read_again(tmp_path: Path) -> None:
    conn = setup_reviewed_pr(tmp_path, approved_by=("mira",))
    with transaction(conn):
        conn.execute("UPDATE review_prs SET comments_read_for = updated_at, colleague_comments = NULL")
    assert [r[0] for r in list_refs_with_new_activity(conn)] == [REF]
    assert ready_view(conn)[0]["colleague_block"] == "the colleagues' comments are not read yet"
    store(conn)
    assert ready_view(conn)[0]["merge_path"] == "ready"


def test_the_owners_own_rule_is_unchanged_beside_it(tmp_path: Path) -> None:
    conn = setup_reviewed_pr(tmp_path, approved_by=("mira",))
    store(conn, comment("i1", at=2000, body="Why this?", author="kai"), comment("i2", at=3000, body="Rename it"))
    view, _ = ready_view(conn)
    assert view["merge_block"] == "the last comment has no answer and no commit after it"
    assert view["colleague_block"] is None, "the owner's comment after it counts as our side's word"


def guard_setup(tmp_path: Path) -> tuple[Any, Any]:
    from test_guard import repo

    conn = setup_reviewed_pr(tmp_path, ref="pr:Acme/Web#12", repo="Acme/Web", number=12, head="feat/x")
    with transaction(conn):
        conn.execute(
            """UPDATE review_prs SET approved_by = '["mira"]', colleague_threads = '["mira"]',
               colleague_comments = '[]'"""
        )
        conn.execute("INSERT OR REPLACE INTO review_poll (id, ok_at) VALUES (1, 1000)")
    return conn, map_command_to_targets("gh pr merge 12", str(repo(tmp_path / "web")))


def test_the_guard_reads_the_threads_live_and_lets_a_thread_resolved_since_the_cache_through(tmp_path: Path) -> None:
    conn, targets = guard_setup(tmp_path)
    fresh = 1000 + GITHUB_STALE_S * 1000
    with mock.patch(
        "bridge.guard.fetch_merge_facts", return_value=(False, True, None, "CLEAN", 0, "a1", ("mira",), ())
    ):
        [refused] = find_merge_blocks(conn, CONFIG, targets, fresh)
    assert refused.waits == (COLLEAGUE_COMMENTS_OPEN,)
    [line] = map_merge_blocks_to_lines([refused], "the owner", "The owner")
    assert "a colleague's comments are not processed: 1 thread neither resolved nor answered (mira)" in line
    with mock.patch("bridge.guard.fetch_merge_facts", return_value=(False, True, None, "CLEAN", 0, "a1", (), ())):
        assert find_merge_blocks(conn, CONFIG, targets, fresh) == []


def test_the_auto_merger_waits_on_a_colleagues_comments_in_the_queue_copy_and_live(tmp_path: Path) -> None:
    conn = auto_setup(tmp_path, make_pr(**CLEAN))
    with transaction(conn):
        conn.execute("""UPDATE review_prs SET colleague_comments = '["kai"]'""")
    assert (
        map_row_to_auto_merge_block(ON, auto_row(conn))
        == "a colleague's comments are not processed: 1 comment unanswered (kai)"
    )
    assert run(conn).merges == []
    with transaction(conn):
        conn.execute("UPDATE review_prs SET colleague_comments = '[]'")
    assert map_row_to_auto_merge_block(ON, auto_row(conn)) is None
    live = replace(LIVE, colleague_threads=("mira",))
    refusal = find_standing_rule_block(ON, auto_row(conn), live)
    assert refusal is not None and refusal.waits == (COLLEAGUE_COMMENTS_OPEN,)
    assert run(conn, live=live).merges == []
    assert len(run(conn).merges) == 1


@pytest.mark.parametrize("state", ["COMMENTED", "CHANGES_REQUESTED"])
def test_a_review_with_text_only_and_no_inline_comment_still_counts(tmp_path: Path, state: str) -> None:
    conn = setup_reviewed_pr(tmp_path, approved_by=("mira",))
    store(conn, comment("r1", at=2000, body="Looks off", author="kai", state=state))
    assert ready_view(conn)[0]["colleague_block"] == "1 comment unanswered (kai)"


def test_our_inline_replies_answer_the_review_text_they_came_after_and_an_empty_comment_does_not(
    tmp_path: Path,
) -> None:
    conn = setup_reviewed_pr(tmp_path, approved_by=("mira",))
    store(
        conn,
        comment("r1", at=2000, body="Looks safe; three nits inline", author="kai", state="COMMENTED"),
        comment("i1", at=2500, body="  "),
    )
    assert ready_view(conn)[0]["colleague_block"] == "1 comment unanswered (kai)"
    store(
        conn,
        comment("r9", at=3000, body="", state="COMMENTED"),
        comment("p9", at=3000, body="🤖 Claude: Fixed.", review="r9"),
    )
    assert ready_view(conn)[0]["colleague_block"] is None


def test_a_pr_whose_threads_were_read_before_this_rule_waits_until_they_are_read_again(tmp_path: Path) -> None:
    conn = setup_reviewed_pr(tmp_path, approved_by=("mira",))
    store(conn)
    with transaction(conn):
        conn.execute("UPDATE review_prs SET colleague_threads = NULL")
    assert ready_view(conn)[0]["colleague_block"] == "the colleagues' comments are not read yet"


def test_the_live_read_finds_a_colleagues_comment_posted_since_the_queues_copy(tmp_path: Path) -> None:
    from bridge.review_github import read_live_merge_facts

    def node(login: str, at: str, body: str, state: str | None = None, kind: str = "User") -> dict[str, Any]:
        return {"author": {"__typename": kind, "login": login}, "body": body, "createdAt": at, "submittedAt": at,
                "state": state, "commit": {"oid": "a1"}}  # fmt: skip

    reviews = [
        node("mira", "2026-11-02T10:00:00Z", "nit: naming", "COMMENTED"),
        node("mira", "2026-11-02T10:05:00Z", "All good", "APPROVED"),
    ]
    comments = [
        node("me", "2026-11-02T10:02:00Z", "Renamed."),
        node("kai", "2026-11-02T10:03:00Z", "One more thing?"),
        node("ci-helper", "2026-11-02T10:04:00Z", "Coverage 90%", kind="Bot"),
        node("bot", "2026-11-02T10:04:00Z", "Findings: none"),
    ]
    pr: dict[str, object] = {
        "isDraft": False, "mergeStateStatus": "CLEAN",
        "commits": {"nodes": [{"commit": {"oid": "a1", "statusCheckRollup": None}}]},
        "reviews": {"nodes": [{k: v for k, v in r.items() if k != "createdAt"} for r in reviews]},
        "comments": {"nodes": [{k: v for k, v in c.items() if k in ("author", "body", "createdAt")} for c in comments]},
        "reviewThreads": {"nodes": []},
    }  # fmt: skip
    answer = json.dumps({"data": {"repository": {"pullRequest": pr}}})
    with mock.patch("bridge.review_github.run_gh", return_value=answer):
        live = read_live_merge_facts(CONFIG, "Org/app", 7)
    assert (live.colleague_threads, live.colleague_comments) == ((), ("kai",))
    conn = auto_setup(tmp_path, make_pr(**CLEAN))
    assert run(conn, live=replace(LIVE, colleague_comments=("kai",))).merges == []
