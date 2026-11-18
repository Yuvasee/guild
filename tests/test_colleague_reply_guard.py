import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_colleague_comments import LEAD, NOW, drafted, told
from test_reviews import CONFIG, REF, comment, store, wake

from bridge.colleague_comments import record_small_call
from bridge.db import transaction
from bridge.gbridge import may_need_guard
from bridge.gitinfo import RepoFacts
from bridge.hooks import handle_pre_tool_use
from bridge.install import list_guard_globs
from bridge.review_github import FetchedComment
from bridge.reviews import mark_closed
from bridge.title_guard import find_colleague_reply_refusal

FACTS = RepoFacts("/w/app", "dev/x", {"origin": "Org/app"})
MIRA = comment("p1", author="mira", body="This leaks the token")
BOT = comment("p2", author="bot", body="Possible null dereference")
OURS = FetchedComment("p4", "me", 1003, "🤖 Claude: Kept the old name on purpose", "https://x/p4")
MIRA_IN_OURS = FetchedComment("p5", "mira", 1004, "Why keep it?", "https://x/p5", parent_id="p4", parent_author="me")

ANSWERS = [
    "gh api repos/Org/app/pulls/7/comments/1/replies -f body=Fixed",
    "gh api -X POST repos/Org/app/pulls/7/comments -f body=Fixed -F in_reply_to=1",
    "gh api --method=POST /repos/org/APP/pulls/7/comments/1/replies --raw-field 'body=Fixed'",
    "gh api 'repos/{owner}/{repo}/pulls/7/comments/1/replies' -f body=Fixed",
    "gh pr comment 7 -b 'Fixed in 4be1'",
    "gh pr comment --body Fixed",
    "gh pr comment https://github.com/Org/app/pull/7 --body-file -",
    "cd /w/app && GH_REPO=Org/app gh pr comment 7 -F reply.md",
    "gh pr review 7 --comment -b 'Done'",
    "gh pr review -cb Done",
    "gh api repos/Org/app/issues/7/comments -f body=Done",
    "gh api repos/Org/app/issues/7/comments --input reply.json",
    "gh api repos/Org/app/pulls/7/reviews -f event=COMMENT -f body=Done",
    "gh pr comment -b Done >/dev/null 2>&1",
    "gh api https://api.github.com/repos/Org/app/issues/7/comments -f body=Done",
    "GH_REPO=Org/app gh api 'repos/{owner}/{repo}/issues/7/comments' -f body=Done",
]
FREE = [
    "gh api repos/Org/app/pulls/7/comments/2/replies -f body=Fixed",
    "gh api repos/Org/app/pulls/7/comments -f body=x -f commit_id=a1 -f path=a.py -F line=3",
    "gh api repos/Org/app/pulls/7/comments --input reply.json",
    "gh api repos/Org/app/pulls/7/comments -f body=Fixed -F in_reply_to=@id.txt",
    "gh api repos/Org/app/pulls/7/comments/99/replies -f body=Fixed",
    "gh api repos/Org/app/pulls/7/comments",
    "gh api repos/Org/app/pulls/7/reviews -f event=APPROVE",
    "gh api repos/Org/app/pulls/7/reviews --input review.json -f body=query",
    "GH_REPO=Other/app gh api 'repos/{owner}/{repo}/issues/7/comments' -f body=Done",
    "gh api repos/Org/app/issues/7/comments -f body=Done --help",
    "gh pr comment 7 --delete-last=true --yes",
    "gh api repos/Org/app/pulls/$N/comments/1/replies -f body=Fixed",
    "gh pr comment $PR -b Fixed",
    "gh pr comment 7 --delete-last",
    "gh pr review 7 --approve",
    "gh pr view 7 --comments",
    "gh pr comment 8 -b Fixed",
    "gh pr comment -R Other/app 7 -b Fixed",
]


def read_repo(_directory: str) -> RepoFacts | None:
    return FACTS


def refusal(conn: Any, command: str, facts: RepoFacts | None = FACTS, config: Any = CONFIG) -> str | None:
    return find_colleague_reply_refusal(conn, config, command, "/w/app", lambda _d: facts)


@pytest.fixture
def conn(tmp_path: Path) -> Any:
    return told(tmp_path, MIRA, BOT, OURS)


@pytest.mark.parametrize("command", ANSWERS)
def test_an_answer_on_a_pr_with_an_unhandled_colleague_comment_is_refused_and_names_it(conn: Any, command: str) -> None:
    assert refusal(conn, command) == (
        'bridge: mira\'s comment https://x/p1 is not handled: `guild comment small https://x/p1 "what I did"` if it '
        f"is a typo, rename or factual question; else `guild comment draft https://x/p1` ({CONFIG.owner_name} "
        "chooses; bridge posts)."
    )


@pytest.mark.parametrize("command", FREE)
def test_a_reply_in_a_bot_thread_and_what_the_guard_cannot_read_stay_free(conn: Any, command: str) -> None:
    assert refusal(conn, command) is None


@pytest.mark.parametrize("command", ANSWERS)
def test_once_marked_small_every_answer_is_free(conn: Any, command: str) -> None:
    with transaction(conn):
        record_small_call(conn, REF, "p1", "fixed it in 4be1", LEAD, NOW)
    assert refusal(conn, command) is None


@pytest.mark.parametrize("command", ANSWERS[:6])
def test_once_drafted_for_the_owner_every_answer_is_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    conn, _, _ = drafted(tmp_path, monkeypatch, MIRA)
    assert refusal(conn, command) is None


def test_our_own_thread_is_free_until_a_colleague_comments_in_it(conn: Any) -> None:
    command = "gh api repos/Org/app/pulls/7/comments/4/replies -f body=Because"
    with transaction(conn):
        record_small_call(conn, REF, "p1", "fixed it in 4be1", LEAD, NOW)
    assert refusal(conn, command) is None
    store(conn, MIRA_IN_OURS)
    wake(conn, NOW)
    assert "mira's comment https://x/p5 is not handled" in str(refusal(conn, command))


def test_a_change_request_is_drafted_never_small_and_more_are_counted(tmp_path: Path) -> None:
    review = comment("r1", at=999, author="dan", body="Split this", state="CHANGES_REQUESTED", commit="a1")
    conn = told(tmp_path, review, MIRA)
    assert refusal(conn, "gh pr comment 7 -b Done") == (
        f"bridge: dan's comment https://x/r1 is not handled: it requests changes: `guild comment draft https://x/r1` "
        f"({CONFIG.owner_name} chooses; bridge posts). (1 more like it on this PR: `guild comment list`)"
    )


def test_a_pr_outside_the_review_org_or_closed_is_free(conn: Any) -> None:
    assert refusal(conn, "gh pr comment 7 -b Done", config=replace(CONFIG, review_org="Elsewhere")) is None
    with transaction(conn):
        mark_closed(conn, [REF], NOW)
    assert refusal(conn, "gh pr comment 7 -b Done") is None


def test_the_hook_denies_with_the_refusal_and_the_shell_gate_starts_it(
    conn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("bridge.title_guard.read_repo_facts", read_repo)
    payload: dict[str, object] = {
        "session_id": "c", "cwd": "/w/app", "tool_name": "Bash",
        "tool_input": {"command": "gh pr comment 7 -b Fixed"},
    }  # fmt: skip
    decision = json.loads(handle_pre_tool_use(conn, payload, CONFIG))["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert decision["permissionDecisionReason"].startswith("bridge: mira's comment https://x/p1 is not handled")
    assert all(may_need_guard(list_guard_globs(), command) for command in ANSWERS)
