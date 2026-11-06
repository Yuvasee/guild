import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast
from unittest import mock

from conftest import BridgeEnv, SpawnProcess, http_request, wait_for
from test_leads import Team
from test_reviews import CONFIG, REF, add_lead, add_session, make_pr

from bridge.attention import (
    ESTIMATE_RULE,
    PriorityError,
    Project,
    Question,
    QueuedPr,
    map_arg_to_project,
    map_page_to_ranked_page,
    map_review_to_queued_pr,
    map_streams_to_ranking,
)
from bridge.config import Config
from bridge.db import connect
from bridge.feed import answer_post, deliver_typed, record_failed_delivery
from bridge.leads import ROLE_TEMPLATE
from bridge.notify import UrgentNotice, release_urgent_notice, show_notification, take_next_urgent_notice
from bridge.reviews import COMMENTS_OPEN, FIXED, WAITING_FOR_OWNER, map_db_to_reviews, store_open_prs

ROLE_MAX_CHARS = 3250
ONBOARDING = Project("onboarding", ("PROJ-39",))
EXPORT = Project("export", ("PROJ-27",))


def queued(ref: str, **changes: Any) -> QueuedPr:
    values: dict[str, Any] = {
        "ref": ref, "label": ref, "group": "waiting", "state": WAITING_FOR_OWNER, "loc": 100, "first_seen_at": 1,
        "fixed_at": None, "stack_parent": None, "issue_keys": (),
    }  # fmt: skip
    return QueuedPr(**{**values, **changes})


def question(item_id: int, at: int, urgent: bool = False) -> Question:
    return Question(item_id, at, urgent, "boris")


def test_ranking_runs_urgent_questions_fixes_then_other_prs_and_marks_only_three() -> None:
    ranking = map_streams_to_ranking(
        [question(1, 100), question(2, 50), question(3, 900, urgent=True)],
        [queued("waiting"), queued("fixed", state=FIXED, fixed_at=5)],
        [],
    )

    assert [item.key for item in ranking] == ["question:3", "question:2", "question:1", "fixed", "waiting"]
    assert [item.mark for item in ranking] == [1, 2, 3, None, None]


def test_a_pr_only_owner_answered_threads_block_comes_first_in_any_group() -> None:
    ranking = map_streams_to_ranking(
        [question(1, 100), question(3, 900, urgent=True)],
        [queued("waiting"), queued("published", group="published", state="published", resolve_threads=2)],
        [],
    )

    assert [item.key for item in ranking] == ["published", "question:3", "question:1", "waiting"]
    assert ranking[0].mark == 1
    assert ranking[0].why == "Resolve your 2 threads: answered, and all that keeps it from merging."


def test_answered_threads_rank_first_only_when_resolving_them_is_all_that_is_left() -> None:
    review: dict[str, object] = {
        "ref": "r", "label": "r", "group": "waiting", "state": COMMENTS_OPEN, "loc": 1, "first_seen_at": 1,
        "issue_keys": [], "resolve_threads": [{"url": "u", "quote": "q"}], "resolve_is_last": False,
    }  # fmt: skip
    assert map_review_to_queued_pr(review).resolve_threads == 0
    assert map_review_to_queued_pr({**review, "resolve_is_last": True}).resolve_threads == 1


def test_a_pr_can_hold_a_number_when_questions_are_few() -> None:
    ranking = map_streams_to_ranking([question(1, 1)], [queued("a", loc=1), queued("b", loc=2), queued("c", loc=3)], [])

    assert [(item.key, item.mark) for item in ranking] == [("question:1", 1), ("a", 2), ("b", 3), ("c", None)]


def test_published_drafts_in_progress_and_open_comments_get_no_place() -> None:
    ranking = map_streams_to_ranking(
        [],
        [
            queued("published", group="published"),
            queued("published-fixed", group="published", state=FIXED, fixed_at=1),
            queued("draft", group="drafts"),
            queued("on-its-owner", state=COMMENTS_OPEN),
            queued("waiting"),
        ],
        [],
    )

    assert [item.key for item in ranking] == ["waiting"]


def test_fixes_go_newest_first_since_the_reason_is_fresh_context() -> None:
    ranking = map_streams_to_ranking(
        [], [queued("older", state=FIXED, fixed_at=10), queued("newer", state=FIXED, fixed_at=20)], []
    )

    assert [item.key for item in ranking] == ["newer", "older"]
    assert "after your comments" in ranking[0].why


def test_other_prs_go_by_project_then_stack_parent_then_size_then_age() -> None:
    prs = [
        queued("no-project", loc=1),
        queued("export", issue_keys=("PROJ-27",), loc=1),
        queued("onboarding-big", issue_keys=("proj-39",), loc=500),
        queued("onboarding-parent", issue_keys=("PROJ-39",), loc=900),
        queued("child", group="drafts", stack_parent="onboarding-parent"),
        queued("onboarding-small-new", issue_keys=("PROJ-39",), loc=50, first_seen_at=9),
        queued("onboarding-small-old", issue_keys=("PROJ-39",), loc=50, first_seen_at=2),
    ]

    ranking = map_streams_to_ranking([], prs, [ONBOARDING, EXPORT])

    assert [item.key for item in ranking] == [
        "onboarding-parent", "onboarding-small-old", "onboarding-small-new", "onboarding-big", "export", "no-project",
    ]  # fmt: skip
    assert "project onboarding, 1 of 2" in ranking[0].why
    assert "blocks child" in ranking[0].why
    assert "no project" in ranking[-1].why


def test_a_pr_that_merges_after_another_follows_it_and_takes_the_next_number() -> None:
    code = queued("pr:Acme-Org/widgets#7", label="widgets#7", loc=1572, first_seen_at=1)
    docs = queued("pr:Acme-Org/widgets-docs#3", label="docs#3", loc=0, first_seen_at=2,
                  depends_on="pr:acme-org/widgets#7")  # fmt: skip
    other = queued("pr:Acme-Org/widgets#8", label="widgets#8", loc=546)

    ranking = map_streams_to_ranking([], [code, docs, other], [])

    assert [(item.key, item.mark) for item in ranking] == [
        (other.ref, 1), (code.ref, 2), (docs.ref, 3),
    ]  # fmt: skip
    assert ranking[2].why.endswith("Merges after widgets#7, so it follows it.")


def test_a_stack_follows_its_parents_in_order_and_waits_while_a_parent_is_not_ranked() -> None:
    base = queued("base", loc=900)
    middle = queued("middle", loc=10, stack_parent="base")
    top = queued("top", loc=1, stack_parent="middle")
    small = queued("small", loc=50)
    unready = queued("unready", loc=1, not_ready=True)
    held = queued("held", loc=1, depends_on="unready")

    ranking = map_streams_to_ranking([], [top, middle, small, base, unready, held], [])

    assert [item.key for item in ranking] == ["base", "middle", "top", "small"]
    assert "Stacked on middle" in ranking[2].why


def test_past_review_a_child_s_threads_stay_ranked_after_its_unranked_parent() -> None:
    parent = queued("parent", group="published", state="published")
    child = queued("child", group="published", state="published", depends_on="parent")
    threads = {"resolve_threads": [{"url": "u", "quote": "q"}], "resolve_is_last": True}

    page = map_page_to_ranked_page({"needs": []}, {"prs": [{**asdict(child), **threads}, asdict(parent)]}, [])

    assert [pr["ref"] for pr in cast(Any, page["reviews"])["prs"]] == ["parent", "child"]
    assert [item["key"] for item in cast(Any, page["attention"])["ranking"]] == ["child"]


def test_a_parent_in_another_group_or_off_the_list_leaves_the_order_alone() -> None:
    draft = queued("draft", group="drafts", state="draft")
    docs = queued("docs", loc=1, depends_on="draft")
    gone = queued("gone-child", loc=2, depends_on="merged-long-ago")
    big = queued("big", loc=500)

    ranking = map_streams_to_ranking([], [big, gone, docs, draft], [])

    assert [item.key for item in ranking] == ["docs", "gone-child", "big"]
    assert [item.mark for item in ranking] == [1, 2, 3]


def test_a_cycle_of_parents_loses_no_pr() -> None:
    prs = [queued("a", loc=1, depends_on="b"), queued("b", loc=2, depends_on="a"), queued("c", loc=3)]

    page = map_page_to_ranked_page({"needs": []}, {"prs": [asdict(pr) for pr in prs]}, [])

    assert sorted(pr["ref"] for pr in cast(Any, page["reviews"])["prs"]) == ["a", "b", "c"]


def test_the_page_puts_an_unranked_pr_right_after_its_parent_within_its_group() -> None:
    def review(ref: str, **extra: Any) -> dict[str, Any]:
        return {**asdict(queued(ref)), "issue_keys": [], **extra}

    prs = [
        review("child", reviews_gap=["Claude"], depends_on="parent"),
        review("other", loc=1),
        review("parent", loc=5),
        review("draft-child", group="drafts", state="draft", stack_parent="parent"),
    ]

    page = map_page_to_ranked_page({"needs": []}, {"prs": prs}, [])

    assert [pr["ref"] for pr in cast(Any, page["reviews"])["prs"]] == ["parent", "child", "other", "draft-child"]
    attention: Any = page["attention"]
    assert [item["key"] for item in attention["ranking"]] == ["parent", "other"]


def test_estimates_are_rough_and_size_only_and_a_fix_has_none_since_its_size_is_unknown() -> None:
    ranking = map_streams_to_ranking(
        [question(1, 1)],
        [
            queued("fixed", state=FIXED, fixed_at=1),
            queued("tiny", loc=3),
            queued("mid", loc=200),
            queued("big", loc=1888),
        ],
        [],
    )

    assert {item.key: item.estimate for item in ranking} == {
        "question:1": "~1 min", "fixed": None, "tiny": "~5 min", "mid": "~20 min", "big": "~3 h",
    }  # fmt: skip
    assert "Rough" in ESTIMATE_RULE


def test_page_keeps_to_the_ranking_and_answered_cards_sit_last() -> None:
    def post(item_id: int, **extra: object) -> dict[str, object]:
        return {"type": "post", "id": item_id, "at": item_id, "from": "boris", **extra}

    review: dict[str, Any] = {
        "ref": "r", "label": "r", "group": "waiting", "state": WAITING_FOR_OWNER, "loc": 9, "first_seen_at": 1,
        "fixed_at": None, "stack_parent": None, "issue_keys": [],
    }  # fmt: skip
    needs = [post(1, delivering=True), {"type": "lead-down"}, post(2), post(3, urgent=True)]

    page = map_page_to_ranked_page(
        {"needs": needs}, {"prs": [{**review, "ref": "later", "group": "drafts"}, review]}, []
    )

    ranked: Any = page["needs"]
    assert [need.get("id") for need in ranked] == [3, 2, None, 1]
    prs: Any = page["reviews"]
    assert [pr["ref"] for pr in prs["prs"]] == ["r", "later"]
    attention: Any = page["attention"]
    assert [item["key"] for item in attention["ranking"]] == ["question:3", "question:2", "r"]


def test_a_pr_belongs_to_a_project_by_title_or_its_owning_leads_chunk_that_names_it(tmp_path: Path) -> None:
    conn = connect(tmp_path / "b.db")
    add_session(conn, "conv-lead", "boris", branch="dev/x", surface="s-boris")
    add_lead(conn, "Boris", "s-boris")
    add_session(conn, "conv-other", "alex", surface="s-alex")
    add_lead(conn, "Alex", "s-alex")
    store_open_prs(conn, [make_pr(title="PROJ-7: thing")], 1)

    def keys() -> list[str]:
        prs: Any = map_db_to_reviews(conn, CONFIG, 2)["prs"]
        return next(pr for pr in prs if pr["ref"] == REF)["issue_keys"]

    def declare(lead: str, name: str, refs: list[str]) -> None:
        conn.execute(
            "INSERT INTO lead_work (lead, name, refs, created_at, updated_at) VALUES (?, ?, ?, 0, 0)",
            (lead, name, json.dumps(refs)),
        )

    title_only = keys()
    declare("boris", "unrelated chunk", ["issue:PROJ-1"])
    declare("alex", "another lead's chunk", ["issue:PROJ-2", "pr:Org/app#8"])
    still_title_only = keys()
    declare("boris", "the chunk", ["issue:PROJ-3", REF])

    assert title_only == ["PROJ-7"]
    assert still_title_only == ["PROJ-7"]
    assert keys() == ["PROJ-3", "PROJ-7"]


def test_project_argument_needs_a_name_and_real_issue_keys() -> None:
    assert map_arg_to_project("onboarding=PROJ-39, proj-15") == Project("onboarding", ("PROJ-39", "PROJ-15"))
    for bad in ("onboarding", "=PROJ-1", "x=", "x=not-a-key"):
        try:
            map_arg_to_project(bad)
        except PriorityError:
            continue
        raise AssertionError(bad)


def test_only_the_assistant_sets_the_priority_and_the_page_shows_it(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    team = Team(bridge_env, spawn_process)
    refused = team.guild("priority", "set", "export=PROJ-27", pid=team.a)
    assert bridge_env.cli("handle", "aide", pid=team.b).returncode == 0

    done = team.guild("priority", "set", "onboarding=PROJ-39,PROJ-15", "export=PROJ-27", pid=team.b)
    shown = team.guild("priority", "show", pid=team.a)
    page: Any = json.loads(http_request(dashboard + "/api/guild"))

    assert refused.returncode == 2
    assert "only aide" in refused.stderr
    assert done.returncode == 0, done.stderr
    assert shown.stdout.splitlines() == ["1. onboarding: PROJ-39, PROJ-15", "2. export: PROJ-27"]
    assert [p["name"] for p in page["attention"]["priority"]] == ["onboarding", "export"]


def test_urgent_is_only_for_questions_and_shows_on_the_card_and_in_the_audit(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    team = Team(bridge_env, spawn_process)
    refused = bridge_env.cli("post", "done", "Shipped", "--urgent", pid=team.a)
    bridge_env.cli("post", "decision", "Calm one", pid=team.a)
    post = bridge_env.cli("post", "blocked", "Prod is down", "--body", "What: all of it", "--urgent", pid=team.a)

    page: Any = json.loads(http_request(dashboard + "/api/guild"))
    audit = team.guild("urgent", pid=team.b)

    assert refused.returncode != 0
    assert "urgent" in post.stdout
    assert [(n["text"], n["urgent"]) for n in page["needs"]] == [("Prod is down", True), ("Calm one", False)]
    assert page["attention"]["ranking"][0]["mark"] == 1
    assert "Prod is down" in audit.stdout
    assert "What: all of it" in audit.stdout
    assert "Calm one" not in audit.stdout


def test_lead_role_says_the_urgent_criteria_and_stays_short() -> None:
    for words in ("--urgent", "prod or a customer is hurting", "money is leaking", "deadline is today"):
        assert words in ROLE_TEMPLATE
    assert len(ROLE_TEMPLATE) <= ROLE_MAX_CHARS
    for owner in ("the owner", "Sam"):
        role = ROLE_TEMPLATE.format(display="Alex", scope="s", refs="", assistant="aide", user="sam", owner=owner)
        assert len(role) <= ROLE_MAX_CHARS


def test_lead_role_says_the_claude_prefix_and_the_issue_rule() -> None:
    role = ROLE_TEMPLATE.format(display="Alex", scope="s", refs="", assistant="aide", user="sam", owner="Sam")
    assert "on GitHub and the tracker start with '🤖 Claude:' (never in chat)" in role
    assert "name nothing of ours (no lead's or aide's name" in role
    assert "search finds no duplicate; never a sub-issue" in role and "only after Sam confirms through aide" in role
    assert "approves the answers" not in role
    assert "A PR doc follows ~/.claude/skills/bridge/review-doc.md" in role
    assert "reasons OBSERVED, REPRODUCED or FROM CODE" in role
    assert "essence visible, details folded" in role
    assert "Typing in your tab is Sam: answer there; send aide one line per decision." in role
    assert "run independent pieces in parallel workers, never queued" in role
    assert "Unprompted, before Sam's queue: Codex and Claude reviews, read-only, in parallel" in role
    assert "100+ LOC or adding a service, contract, queue or chart" in role and "gets a Reviews section" in role


def seed_urgent(conn: sqlite3.Connection, now: int, conversation: str = "conv-a", item_urgent: int = 1) -> int:
    cursor = conn.execute(
        """
        INSERT INTO user_items (created_at, from_conversation, from_handle, severity, text, source, kind, urgent)
        VALUES (?, ?, 'handle', 'blocker', 'SECRET TITLE', 'agent', 'blocked', ?)
        """,
        (now, conversation, item_urgent),
    )
    return int(cursor.lastrowid or 0)


def take_all_notices(conn: sqlite3.Connection, now: int) -> list[UrgentNotice]:
    notices: list[UrgentNotice] = []
    while notice := take_next_urgent_notice(conn, Config(), now):
        notices.append(notice)
    return notices


def test_notice_fires_once_names_only_the_lead_and_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "b.db"
    conn = connect(path)
    add_session(conn, "conv-a", "boris", surface="s-boris")
    add_lead(conn, "Boris", "s-boris")
    add_session(conn, "conv-w", "customer-acme-outage", surface="s-worker", session="tab-w")
    item = seed_urgent(conn, 1000)
    worker_item = seed_urgent(conn, 1001, "conv-w")
    calm = seed_urgent(conn, 1002, item_urgent=0)

    first = take_all_notices(conn, 2000)
    conn.close()
    second = take_all_notices(connect(path), 3000)

    assert [(n.item_id, n.text) for n in first] == [
        (item, "Urgent question from Boris"),
        (worker_item, "Urgent question"),
    ]
    assert second == []
    assert calm not in [n.item_id for n in first]


def test_no_notice_for_a_locked_tab_a_closed_or_a_stale_question(tmp_path: Path) -> None:
    conn = connect(tmp_path / "b.db")
    add_session(conn, "conv-a", "boris", surface="s-boris")
    add_session(conn, "conv-l", "hidden", surface="s-locked", session="tab-l")
    conn.execute("UPDATE surfaces SET tab_name = '🔒 private' WHERE surface = 's-locked'")
    seed_urgent(conn, 10_000, "conv-l")
    closed = seed_urgent(conn, 10_000)
    conn.execute("UPDATE user_items SET resolved_at = 1 WHERE id = ?", (closed,))
    seed_urgent(conn, 1)

    assert take_all_notices(conn, 3 * 86_400_000 + 5) == []


def test_a_notice_that_could_not_show_is_tried_again(tmp_path: Path) -> None:
    conn = connect(tmp_path / "b.db")
    add_session(conn, "conv-a", "boris", surface="s-boris")
    item = seed_urgent(conn, 1000)

    taken = take_next_urgent_notice(conn, Config(), 2000)
    assert taken is not None
    skipped = take_next_urgent_notice(conn, Config(), 2000)
    release_urgent_notice(conn, taken.item_id)

    assert skipped is None
    assert take_next_urgent_notice(conn, Config(), 2500, frozenset({item})) is None
    again = take_next_urgent_notice(conn, Config(), 3000)
    assert again is not None and again.item_id == item


def test_a_restart_during_one_notice_does_not_lose_the_next(tmp_path: Path) -> None:
    path = tmp_path / "b.db"
    conn = connect(path)
    add_session(conn, "conv-a", "boris", surface="s-boris")
    seed_urgent(conn, 1000)
    second = seed_urgent(conn, 1001)

    take_next_urgent_notice(conn, Config(), 2000)
    conn.close()
    after_restart = take_next_urgent_notice(connect(path), Config(), 3000)

    assert after_restart is not None and after_restart.item_id == second


def test_show_notification_passes_text_as_an_argument_with_no_sound_and_reads_failure() -> None:
    done = mock.Mock(returncode=0, stderr="")
    with mock.patch("bridge.notify.subprocess.run", return_value=done) as run:
        assert show_notification(Config(), 'a "quote"; do shell script') is True
    command = run.call_args.args[0]
    assert command[0] == "osascript"
    assert command[-1] == 'a "quote"; do shell script'
    assert "sound" not in " ".join(command)

    done.returncode = 1
    with mock.patch("bridge.notify.subprocess.run", return_value=done):
        assert show_notification(Config(), "x") is False


def test_the_service_notifies_once_without_touching_a_tab(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    log = bridge_env.root / "notify.log"
    before = team.log()

    bridge_env.cli("post", "blocked", "SECRET TITLE", "--body", "SECRET BODY", "--urgent", pid=team.a)

    assert wait_for(log.exists, 15)
    text = log.read_text()
    assert text.count("Urgent question from Alex") == 1
    assert "SECRET" not in text
    assert team.log() == before


def test_an_answer_keeps_its_card_until_delivered(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    team = Team(bridge_env, spawn_process)
    bridge_env.cli("post", "decision", "Ship?", "--option", "I'll comment on GitHub", pid=team.a)

    def needs() -> list[Any]:
        return json.loads(http_request(dashboard + "/api/guild"))["needs"]

    with bridge_env.connect() as conn:
        answer_post(conn, 1, "I'll comment on GitHub", "sam", 1000)
    waiting = needs()
    page: Any = json.loads(http_request(dashboard + "/api/guild"))
    team.prompt(team.a, "conv-a", "next")

    assert [(n["id"], n["delivering"], n["answer"]) for n in waiting] == [(1, True, "I'll comment on GitHub")]
    assert page["attention"]["ranking"] == []
    assert needs() == []


def test_a_claim_to_type_keeps_the_card_until_typing_is_over_and_a_failure_survives_clear(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    team = Team(bridge_env, spawn_process)
    bridge_env.cli("post", "decision", "Ship?", pid=team.a)
    with bridge_env.connect() as conn:
        result = answer_post(conn, 1, "yes", "sam", 1000)
    during: list[Any] = []

    def type_after_a_clear(*_: Any, claim: Any, release: Any, **__: Any) -> str:
        assert claim()
        during.append(json.loads(http_request(dashboard + "/api/guild"))["needs"])
        with bridge_env.connect() as moved:
            moved.execute(
                "UPDATE message_recipients SET conversation_id = 'conv-a2' WHERE message_id = ?", (result.message_id,)
            )
        release()
        return "failed"

    def type_fine(*_: Any, claim: Any, **__: Any) -> str:
        assert claim()
        during.append(json.loads(http_request(dashboard + "/api/guild"))["needs"])
        return "typed"

    with bridge_env.connect() as conn:
        config = Config(agtermctl=str(bridge_env.root / "agtermctl"))
        with mock.patch("bridge.feed.type_when_idle", type_after_a_clear):
            failed = deliver_typed(conn, config, result.message_id, "conv-a")
        row = conn.execute("SELECT acked_at, offered_at, typing_started_at FROM message_recipients").fetchone()
        conn.execute(
            "UPDATE message_recipients SET conversation_id = 'conv-a' WHERE message_id = ?", (result.message_id,)
        )
        with mock.patch("bridge.feed.type_when_idle", type_fine):
            typed = deliver_typed(conn, config, result.message_id, "conv-a")

    assert failed == "failed"
    assert tuple(row) == (None, None, None)
    assert typed == "typed"
    assert [n["delivering"] for n in during[0]] == [True]
    assert [n["delivering"] for n in during[1]] == [True]
    assert json.loads(http_request(dashboard + "/api/guild"))["needs"] == []


def test_a_failing_answer_says_so_on_its_own_card_and_dismiss_clears_it_at_once(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    team = Team(bridge_env, spawn_process)
    bridge_env.cli("post", "decision", "Ship?", pid=team.a)
    with bridge_env.connect() as conn:
        result = answer_post(conn, 1, "yes", "sam", 1000)
        record_failed_delivery(conn, result.message_id, "conv-a", 1000)

    def needs() -> list[Any]:
        return json.loads(http_request(dashboard + "/api/guild"))["needs"]

    failing = needs()
    http_request(dashboard + "/api/items/1/resolve", post=True, data={})

    assert [(n["type"], n["delivery_failed"], n["answer_to"]) for n in failing] == [("post", True, failing[0]["from"])]
    assert needs() == []


def test_a_session_withdraws_only_its_own_open_post(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    team = Team(bridge_env, spawn_process)
    bridge_env.cli("post", "decision", "Ship?", pid=team.a)

    refused = bridge_env.cli("post", "resolve", "1", pid=team.b)
    open_after_refusal = json.loads(http_request(dashboard + "/api/guild"))["needs"]
    done = bridge_env.cli("post", "resolve", "1", pid=team.a)
    again = bridge_env.cli("post", "resolve", "1", pid=team.a)
    not_a_number = bridge_env.cli("post", "resolve", "one", pid=team.a)

    assert refused.returncode != 0
    assert "only its author" in refused.stderr
    assert len(open_after_refusal) == 1
    assert done.returncode == 0, done.stderr
    assert json.loads(http_request(dashboard + "/api/guild"))["needs"] == []
    assert again.returncode != 0
    assert not_a_number.returncode != 0


def test_withdraw_follows_the_tab_across_clear(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    bridge_env.cli("post", "decision", "Ship?", pid=team.a)
    bridge_env.write_session_file(team.a, "conv-a2")
    team.start(team.a, "conv-a2", "clear")

    done = bridge_env.cli("post", "resolve", "1", pid=team.a)

    assert done.returncode == 0, done.stderr


def test_send_to_a_tab_opened_moments_ago_syncs_once_and_finds_it(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    newcomer = spawn_process(env={"AGTERM_PANE_ID": "PANE-N", "AGTERM_SESSION_ID": "TAB-N"})
    bridge_env.write_session_file(newcomer, "conv-n")
    tree = json.loads(bridge_env.stub_tree.read_text())
    tree["result"]["tree"]["workspaces"][0]["sessions"].append(
        {"id": "TAB-N", "name": "New Task", "surfaces": [{"paneID": "PANE-N"}]}
    )
    bridge_env.stub_tree.write_text(json.dumps(tree))

    sent = bridge_env.cli("send", "new-task", "hello", pid=team.a)
    missing = bridge_env.cli("send", "nobody-here", "hello", pid=team.a)

    assert sent.returncode == 0, sent.stderr
    assert "new-task" in sent.stdout
    assert missing.returncode != 0
    assert "no live session" in missing.stderr
