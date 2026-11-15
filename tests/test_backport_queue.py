import dataclasses
from pathlib import Path
from typing import Any, cast

import pytest
from test_reviews import CONFIG, add_session, make_pr

from bridge.attention import QueuedPr, map_page_to_ranked_page, map_streams_to_ranking
from bridge.backport import BackportVerdict
from bridge.db import connect, transaction
from bridge.review_post import ReviewError, advance_posts, ask_for_docs, ask_to_mark_drafts_ready, refuse_backport_post
from bridge.review_worktree import is_worth_preparing, pick_worktree_job
from bridge.reviews import (
    BACKPORT_RECHECK_MS,
    BACKPORTS,
    WAITING_FOR_OWNER,
    WAITING_FOR_YOU,
    list_backports_to_check,
    map_db_to_reviews,
    store_backport_check,
    store_open_prs,
)

BACKPORTING = dataclasses.replace(CONFIG, backport_bases=("release/*",))
BACKPORT = "pr:Org/app#9"
NOW = 200_000


def setup(tmp_path: Path) -> Any:
    conn = connect(tmp_path / "t.db")
    with transaction(conn):
        store_open_prs(
            conn,
            [
                make_pr(loc=400, updated_at="2099-01-01T00:00:00Z"),
                make_pr(ref="pr:Org/app#8", number=8, url="https://github.com/Org/app/pull/8", head="dev/y", loc=10),
                make_pr(ref=BACKPORT, number=9, url="https://github.com/Org/app/pull/9", head="dev/bp",
                        base="release/1.13.x", loc=400, updated_at="2099-01-01T00:00:00Z"),
            ],
            NOW,
        )  # fmt: skip
        add_session(conn, "c1", "worker", branch="dev/x")
        add_session(conn, "c2", "porter", branch="dev/bp")
    return conn


def view(conn: Any, config: Any = BACKPORTING) -> tuple[dict[str, dict[str, Any]], list[str]]:
    shown: Any = map_page_to_ranked_page({"needs": []}, map_db_to_reviews(conn, config, NOW), [])
    prs = cast(list[dict[str, Any]], shown["reviews"]["prs"])
    return {pr["ref"]: pr for pr in prs}, [item["key"] for item in shown["attention"]["ranking"] if item["mark"]]


def test_a_backport_sits_in_its_own_group_unranked_with_no_post_doc_or_worktree(tmp_path: Path) -> None:
    conn = setup(tmp_path)
    prs, ranked = view(conn)
    backport = prs[BACKPORT]
    assert backport["group"] == BACKPORTS and "a backport onto release/1.13.x" in backport["group_why"]
    assert not backport["can_publish"] and backport["reviews_gap"] is None and backport["backport_check"] is None
    assert ranked == ["pr:Org/app#8"]
    assert any(
        "Backports (lead merges)" in rule
        for rule in cast(list[str], map_db_to_reviews(conn, BACKPORTING, NOW)["rules"])
    )
    rows = {r["ref"]: r for r in conn.execute("SELECT * FROM review_prs")}
    assert not is_worth_preparing(rows[BACKPORT], BACKPORTING, NOW)
    assert is_worth_preparing(rows["pr:Org/app#7"], BACKPORTING, NOW)
    with transaction(conn):
        asked = ask_for_docs(conn, BACKPORTING, NOW)
        with pytest.raises(ReviewError, match="a backport gets no Slack post"):
            refuse_backport_post(conn, BACKPORTING, BACKPORT)
        refuse_backport_post(conn, BACKPORTING, "pr:Org/app#7")
    assert (
        len(asked) == 1
        and conn.execute("SELECT doc_asked_at FROM review_prs WHERE ref = ?", (BACKPORT,)).fetchone()[0] is None
    )
    # Off unless configured: the same PR is an ordinary one in the owner's queue.
    plain, _ = view(conn, CONFIG)
    assert plain[BACKPORT]["group"] == WAITING_FOR_YOU and plain[BACKPORT]["can_publish"]


def test_a_backport_with_only_the_owners_answered_threads_left_still_gets_no_number() -> None:
    def queued(ref: str, group: str) -> QueuedPr:
        return QueuedPr(ref, ref, group, WAITING_FOR_OWNER, 10, 1, None, None, (), resolve_threads=1)

    ranking = map_streams_to_ranking([], [queued("a", WAITING_FOR_YOU), queued("b", BACKPORTS)], [])
    assert [item.key for item in ranking] == ["a"]


def test_a_backport_published_before_keeps_no_post_moving_and_no_worktree_refresh(tmp_path: Path) -> None:
    conn = setup(tmp_path)
    with transaction(conn):
        conn.execute("UPDATE review_prs SET reviewed_at = 1, post_state = 'waiting-sentence', post_detail = NULL")
        conn.execute("UPDATE review_prs SET wt_path = '/tmp/wt', wt_oid = 'old', wt_state = 'ready'")
        to_send, _ = advance_posts(conn, BACKPORTING, NOW)
        posts = dict(conn.execute("SELECT ref, sentence_asked_at FROM review_prs").fetchall())
    assert BACKPORT not in to_send and posts[BACKPORT] is None and posts["pr:Org/app#7"] is not None
    prs, _ = view(conn)
    assert not prs[BACKPORT]["can_retry"] and not prs[BACKPORT]["can_repost"]
    with transaction(conn):
        conn.execute("UPDATE review_prs SET wt_path = NULL WHERE ref != ?", (BACKPORT,))
    job = pick_worktree_job(conn, BACKPORTING, NOW)
    assert job is not None and job.action == "remove", "never refreshed: the owner's review checkout goes"


def test_a_draft_backport_is_not_asked_to_be_marked_ready(tmp_path: Path) -> None:
    conn = setup(tmp_path)
    with transaction(conn):
        conn.execute("UPDATE review_prs SET draft = 1")
        told = ask_to_mark_drafts_ready(conn, BACKPORTING, NOW)
    assert len(told) == 1
    assert view(conn)[0][BACKPORT]["group"] == BACKPORTS


def test_the_poller_checks_each_backport_head_once_and_a_refusal_again_later(tmp_path: Path) -> None:
    conn = setup(tmp_path)
    assert list_backports_to_check(conn, CONFIG, NOW) == []
    assert list_backports_to_check(conn, BACKPORTING, NOW) == [(BACKPORT, "Org/app", 9, "a1 release/1.13.x")]
    with transaction(conn):
        store_backport_check(
            conn, BACKPORT, "a1 release/1.13.x", BackportVerdict(True, "backport of #4, identical diff"), NOW
        )
    assert list_backports_to_check(conn, BACKPORTING, NOW + BACKPORT_RECHECK_MS * 10) == []
    assert view(conn)[0][BACKPORT]["backport_check"] == {
        "waived": True, "reason": "backport of #4, identical diff", "current": True, "at": NOW,
    }  # fmt: skip
    with transaction(conn):
        conn.execute("UPDATE review_prs SET base = 'release/1.14.x' WHERE ref = ?", (BACKPORT,))
    assert list_backports_to_check(conn, BACKPORTING, NOW) == [(BACKPORT, "Org/app", 9, "a1 release/1.14.x")]
    with transaction(conn):
        conn.execute("UPDATE review_prs SET head_oid = 'b2' WHERE ref = ?", (BACKPORT,))
    assert list_backports_to_check(conn, BACKPORTING, NOW) == [(BACKPORT, "Org/app", 9, "b2 release/1.14.x")]
    assert view(conn)[0][BACKPORT]["backport_check"]["current"] is False
    with transaction(conn):
        store_backport_check(conn, BACKPORT, "b2 release/1.14.x", BackportVerdict(False, "#4 is not merged"), NOW)
    assert list_backports_to_check(conn, BACKPORTING, NOW + BACKPORT_RECHECK_MS - 1) == []
    assert list_backports_to_check(conn, BACKPORTING, NOW + BACKPORT_RECHECK_MS) == [
        (BACKPORT, "Org/app", 9, "b2 release/1.14.x")
    ]


def test_a_backport_never_reads_waiting_for_you_shows_no_doc_and_loses_its_review_worktree(tmp_path: Path) -> None:
    conn = setup(tmp_path)
    with transaction(conn):
        conn.execute(
            "UPDATE review_prs SET doc_file = 'd.html', wt_path = '/tmp/wt-9', wt_state = 'ready', "
            "checks = 'SUCCESS', merge_state = 'CLEAN' WHERE ref = ?",
            (BACKPORT,),
        )
    prs, _ = view(conn)
    backport = prs[BACKPORT]
    assert backport["state"] == "lead merges: waiting on the owner's word, an approving review or a waiver"
    assert backport["doc_url"] is None
    job = pick_worktree_job(conn, BACKPORTING, NOW)
    assert job is not None and job.ref == BACKPORT and job.action == "remove"


def test_a_waiver_from_an_answered_decision_stands_in_for_the_approval(tmp_path: Path) -> None:
    from bridge.reviews import clear_approval_waiver, record_approval_waiver

    conn = setup(tmp_path)
    with transaction(conn):
        conn.execute("UPDATE review_prs SET checks = 'SUCCESS', merge_state = 'CLEAN' WHERE ref = ?", (BACKPORT,))
        conn.execute(
            "INSERT INTO user_items (id, created_at, severity, text, source, kind) "
            "VALUES (24, 0, 'action', 'merge #9?', 'agent', 'decision')"
        )
    with pytest.raises(ValueError, match="no answer"):
        record_approval_waiver(conn, BACKPORT, 24, "aide", NOW)
    with transaction(conn):
        conn.execute("UPDATE user_items SET answer = 'Go' WHERE id = 24")
        assert record_approval_waiver(conn, BACKPORT, 24, "aide", NOW) == "Go"
    prs, _ = view(conn)
    assert prs[BACKPORT]["state"] == "lead merges: ready"
    assert prs[BACKPORT]["approval_waived"] == "waived by the owner (Q#24)"
    with transaction(conn):
        clear_approval_waiver(conn, BACKPORT)
    prs, _ = view(conn)
    assert prs[BACKPORT]["approval_waived"] is None
