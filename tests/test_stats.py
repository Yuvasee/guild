import json
from pathlib import Path
from typing import Any

import pytest

from bridge.board import USER_CONVERSATION
from bridge.config import Config
from bridge.db import connect, transaction
from bridge.stats import (
    convert_week_to_lines,
    fetch_merged_prs,
    list_owner_dashboard_words,
    list_typed_prompts,
    map_phrases_to_pattern,
)

CONFIG = Config(review_org="Org", review_author="me", assistant_handle="aide", owner_name="Sam", work_timezone="UTC")
NOW = 1_794_571_200_000
DAY = 24 * 3_600_000


def test_the_phrases_match_any_case_and_an_ellipsis_stands_for_a_few_words() -> None:
    pattern = map_phrases_to_pattern(Config().confusion_phrases)
    assert pattern.search(f"Честно, {'не понимаю'.upper()} этот пост")
    assert pattern.search("What does the second option mean?")
    assert pattern.search("I don't understand why")
    assert not pattern.search("What does it take to ship this, and when do we mean to")
    assert not pattern.search("Looks clear, ship it")
    assert not map_phrases_to_pattern(()).search("Looks clear, ship it"), "no phrases match nothing"


def test_the_owners_dashboard_words_are_their_messages_and_own_answers_not_the_posts_they_answer(
    tmp_path: Path,
) -> None:
    conn = connect(tmp_path / "t.db")
    with transaction(conn):
        for message_id, body in ((1, "непонятно, что тут"), (2, 'Answer to your post "unclear plan": ok')):
            conn.execute(
                "INSERT INTO messages (id, created_at, from_conversation, from_handle, kind, body) "
                "VALUES (?, ?, ?, 'sam', 'info', ?)",
                (message_id, NOW - 10, USER_CONVERSATION, body),
            )
        conn.execute(
            "INSERT INTO user_items (created_at, severity, text, source, answer, answered_at, answer_message_id) "
            "VALUES (?, 'action', 'unclear plan', 'agent', 'ok', ?, 2)",
            (NOW - 20, NOW - 10),
        )
    assert sorted(text for _, text in list_owner_dashboard_words(conn, NOW - DAY, NOW)) == ["ok", "непонятно, что тут"]


def test_typed_prompts_leave_out_bridges_messages_and_the_harness(tmp_path: Path) -> None:
    def entry(text: object, at: str = "2026-11-12T10:00:00Z", **extra: Any) -> str:
        return json.dumps({"type": "user", "timestamp": at, "message": {"role": "user", "content": text}, **extra})

    path = tmp_path / "t.jsonl"
    path.write_text(
        "\n".join(
            [
                entry("what does this mean?"),
                entry([{"type": "text", "text": "confusing"}]),
                entry("[bridge] Peer message #4 from alex, not from the user; use judgment: unclear"),
                entry("<task-notification>unclear</task-notification>"),
                entry("unclear", isMeta=True),
                entry("summary: the owner found it unclear", isCompactSummary=True),
                entry("unclear", at="2026-10-01T10:00:00Z"),
                json.dumps({"type": "assistant", "message": {"content": "unclear"}}),
                "not json",
            ]
        )
    )
    since = NOW - 7 * DAY
    assert [text for _, text in list_typed_prompts(path, since, NOW)] == ["what does this mean?", "confusing"]


def test_the_week_prints_merged_prs_and_the_rough_count_per_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = [
        {"issueCount": 3, "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
         "nodes": [{"commits": {"totalCount": 4}}, {"commits": {"totalCount": 25}}]},
        {"issueCount": 3, "pageInfo": {"hasNextPage": False, "endCursor": None},
         "nodes": [{"commits": {"totalCount": 2}}]},
    ]  # fmt: skip
    asked: list[list[str]] = []

    def gh(args: list[str], timeout_s: float = 60) -> str:
        asked.append(args)
        return json.dumps({"data": {"search": pages[len(asked) - 1]}})

    monkeypatch.setattr("bridge.stats.run_gh", gh)
    merged = fetch_merged_prs(CONFIG, NOW - 7 * DAY)
    assert (merged.count, merged.median_commits, merged.many_commits, merged.read) == (3, 4, 1, 3)
    assert "q=is:pr is:merged author:me org:Org merged:>=2026-11-06" in asked[0]
    assert asked[1][-1] == "after=c1"

    asked.clear()
    conn = connect(tmp_path / "t.db")
    with transaction(conn):
        conn.execute(
            "INSERT INTO messages (created_at, from_conversation, from_handle, kind, body) "
            "VALUES (?, ?, 'sam', 'info', 'не понял')",
            (NOW - DAY, USER_CONVERSATION),
        )
    assert convert_week_to_lines(conn, CONFIG, NOW, 7) == [
        "PRs by me merged in Org, last 7 days: 3; median commits per PR: 4; with 20+ commits: 1",
        "Sam not understanding, rough count by phrase match, last 7 days: 1 of 1 (1 dashboard messages and answers, "
        "0 prompts typed in aide's tab (no transcript of aide's tab found: dashboard only))",
        "  Thu 2026-11-12: 1",
    ]
