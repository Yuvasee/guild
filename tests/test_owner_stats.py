import json
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from bridge import owner_stats
from bridge.board import USER_CONVERSATION
from bridge.config import Config
from bridge.db import connect, transaction
from bridge.gh_quota import CoreQuota
from bridge.owner_stats import (
    OTHER,
    OwnerPr,
    PromptReader,
    count_answers,
    count_answers_opened,
    count_asks,
    count_dashboard_messages,
    count_owner_comments,
    count_prs,
    count_published_prs,
    count_tab_prompts,
    count_withdrawn_questions,
    list_owner_prompts,
    map_answers_to_median_hours,
    map_conversations_to_agents,
    map_counts_to_values,
    map_db_to_stats,
    map_measures_to_values,
    read_pr_cache,
)
from bridge.review_github import GitHubError

CONFIG = Config(review_author="me", assistant_handle="aide", work_timezone="UTC")
DAY = 24 * 3_600_000
T0 = 1_795_651_200_000
"""2026-11-26 00:00 UTC."""
HOUR = 3_600_000


def entry(text: object, at: str = "2026-11-26T10:00:00Z", uuid: str = "u1", **extra: Any) -> bytes:
    data = {"type": "user", "uuid": uuid, "timestamp": at, "message": {"role": "user", "content": text}, **extra}
    return json.dumps(data, separators=(",", ":")).encode()


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return connect(tmp_path / "t.db")


def add_tab(c: sqlite3.Connection, name: str, tab: str, parent: str | None = None, tab_name: str | None = None) -> None:
    c.execute(
        "INSERT INTO surfaces (surface, agterm_session, handle, tab_name, updated_at) VALUES (?, ?, ?, ?, 0)",
        (f"s-{name}", tab, name, tab_name),
    )
    c.execute(
        "INSERT INTO instances (instance_id, pid, proc_start_ms, surface, status, first_seen_at, last_observed_at) "
        "VALUES (?, 1, 1, ?, 'idle', 0, 0)",
        (f"i-{name}", f"s-{name}"),
    )
    c.execute(
        "INSERT INTO conversations (conversation_id, instance_id, first_seen_at, last_seen_at) VALUES (?, ?, 0, ?)",
        (f"c-{name}", f"i-{name}", T0),
    )
    if parent:
        c.execute("INSERT INTO tab_parents (agterm_session, parent_session) VALUES (?, ?)", (tab, parent))


def add_lead(c: sqlite3.Connection, name: str) -> None:
    c.execute(
        "INSERT INTO leads (name, display_name, scope, linear_refs, tint, surface, created_at) "
        "VALUES (?, ?, 's', '[]', 'blue', ?, 0)",
        (name, name.capitalize(), f"s-{name}"),
    )


def test_only_what_the_owner_typed_counts_and_a_paste_is_his_unless_bridge_pasted_mail() -> None:
    lines = [
        entry("ship it", uuid="typed", promptSource="typed"),
        entry("<div>Fix this component</div>", uuid="markup", promptSource="typed"),
        entry('<pasted_content id="1">\nhis long note', uuid="paste"),
        entry("from an older Claude Code with no promptSource", uuid="old"),
        entry("Reply ok", uuid="sdk", promptSource="sdk", entrypoint="sdk-cli"),
        entry("<task-notification> done", uuid="task", promptSource="system"),
        entry("[bridge] Peer messages from other agent sessions", uuid="mail"),
        entry('<pasted_content id="2">\n[bridge] sam on the dashboard (#7): hi', uuid="mail-paste"),
        entry("Your brief is in /tmp/b.md. Read it and do it.", uuid="brief"),
        entry("Guild: you are Bea, a team lead now.", uuid="wake"),
        entry("Another Claude session sent a message: <teammate-message>", uuid="peer"),
        entry("You have bridge messages from bea: run `bridge inbox`.", uuid="old-mail"),
        entry("/compact", uuid="slash"),
        entry("<command-name>/model</command-name>", uuid="command"),
        entry("<local-command-stdout>done</local-command-stdout>", uuid="stdout"),
        entry("<bash-input>ls</bash-input>", uuid="bang"),
        entry("<system-reminder>x</system-reminder>", uuid="reminder"),
        entry("[Request interrupted by user for tool use]", uuid="esc"),
        entry("summary", uuid="compact", isCompactSummary=True),
        entry("meta", uuid="meta", isMeta=True),
        entry("side", uuid="side", isSidechain=True),
        entry([{"type": "tool_result", "content": "out"}], uuid="tool", toolUseResult={"stdout": "out"}),
        json.dumps({"type": "assistant", "uuid": "reply", "message": {"content": "ok"}}).encode(),
        b"not json",
    ]
    assert [uuid for uuid, _ in list_owner_prompts(lines)] == ["typed", "markup", "paste", "old"]


def test_a_transcript_is_read_from_where_the_last_read_stopped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(owner_stats, "READ_CHUNK", 7)
    path = tmp_path / "t.jsonl"
    path.write_bytes(entry("one", uuid="a") + b"\n" + entry("two", uuid="b")[:20])
    reader = PromptReader()
    assert [u for u, _ in reader.read(path)] == ["a"], "a half-written last line waits for the next read"
    path.write_bytes(entry("one", uuid="a") + b"\n" + entry("two", uuid="b") + b"\n")
    assert [u for u, _ in reader.read(path)] == ["a", "b"]
    path.write_bytes(entry("new", uuid="c") + b"\n")
    assert [u for u, _ in reader.read(path)] == ["c"], "a shorter file was replaced: read anew"
    missing = reader.read(tmp_path / "gone.jsonl")
    assert missing == []


def test_a_runaway_transcript_line_is_skipped_to_its_end_and_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(owner_stats, "READ_CHUNK", 7)
    monkeypatch.setattr(owner_stats, "MAX_RECORD", 200)
    path = tmp_path / "t.jsonl"
    head = entry("one", uuid="a") + b"\n" + b"x" * 300
    path.write_bytes(head)
    reader = PromptReader()
    assert ([u for u, _ in reader.read(path)], reader.skipped) == (["a"], 1)
    path.write_bytes(head + b"y" * 300 + b"\n" + entry("two", uuid="b") + b"\n")
    assert ([u for u, _ in reader.read(path)], reader.skipped) == (["a", "b"], 1), "one line, skipped once"
    whole, parts = tmp_path / "whole.jsonl", tmp_path / "parts.jsonl"
    big = entry("x" * 300, uuid="big")
    whole.write_bytes(big + b"\n")
    parts.write_bytes(big)
    reader = PromptReader()
    reader.read(parts)
    parts.write_bytes(big + b"\n")
    assert (reader.read(whole), reader.read(parts), reader.skipped) == ([], [], 2), "the same either way it was read"


def test_a_conversation_counts_for_its_lead_its_workers_lead_or_the_assistant_and_a_locked_tab_for_nobody(
    conn: sqlite3.Connection,
) -> None:
    with transaction(conn):
        add_tab(conn, "bea", "t-bea")
        add_lead(conn, "bea")
        add_tab(conn, "w1", "t-w1", parent="t-bea")
        add_tab(conn, "aide", "t-aide")
        add_tab(conn, "w2", "t-w2", parent="t-aide")
        add_tab(conn, "probe", "t-probe")
        add_tab(conn, "game", "t-game", tab_name="🔒 game")
        conn.execute(
            "INSERT INTO closed_workers (conversation_id, tab_name, parent_handle, resume_command, closed_at) "
            "VALUES ('c-gone', 'gone', 'bea', 'claude --resume', 0)"
        )
    agents = map_conversations_to_agents(
        conn, CONFIG, ["c-bea", "c-w1", "c-aide", "c-w2", "c-probe", "c-game", "c-gone"]
    )
    assert agents == {
        "c-bea": "bea",
        "c-w1": "bea",
        "c-aide": "aide",
        "c-w2": "aide",
        "c-probe": OTHER,
        "c-gone": "bea",
    }


def test_tab_prompts_count_once_per_entry_though_a_resumed_transcript_repeats_them(tmp_path: Path) -> None:
    first, second = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    first.write_bytes(entry("hi", uuid="x") + b"\n")
    second.write_bytes(entry("hi", uuid="x") + b"\n" + entry("more", "2026-11-27T09:00:00Z", uuid="y") + b"\n")
    reader = PromptReader()
    found = count_tab_prompts(
        {"c1": first, "c2": second, "c3": first}, {"c1": "bea", "c2": "bea"}, reader.read, T0, None
    )
    assert found == {"bea": {"2026-11-26": 1, "2026-11-27": 1}}


def test_dashboard_messages_count_once_per_agent_and_once_in_all_and_never_for_a_locked_tab(
    conn: sqlite3.Connection,
) -> None:
    with transaction(conn):
        add_lead(conn, "bea")
        add_tab(conn, "game", "t-game", tab_name="🔒 game")
        for message_id in (1, 2, 3):
            conn.execute(
                "INSERT INTO messages (id, created_at, from_conversation, from_handle, kind, body) "
                "VALUES (?, ?, ?, 'sam', 'info', 'x')",
                (message_id, T0 + HOUR, USER_CONVERSATION),
            )
        recipients = [
            (1, "c-bea", "bea"),
            (1, "c-w", "w"),
            (1, "c-x", "x"),
            (2, "c-aide", "aide"),
            (2, "c-game", "game"),
            (3, "c-bea", "bea"),
        ]
        conn.executemany(
            "INSERT INTO message_recipients (message_id, conversation_id, handle) VALUES (?, ?, ?)", recipients
        )
        conn.execute(
            "INSERT INTO user_items (created_at, severity, text, source, answer_message_id) "
            "VALUES (0, 'action', 'q', 'agent', 3)"
        )
    per_agent, total = count_dashboard_messages(conn, CONFIG, {"c-w": "bea"}, T0, None)
    assert per_agent == {"bea": {"2026-11-26": 1}, OTHER: {"2026-11-26": 1}, "aide": {"2026-11-26": 1}}
    assert total == {"2026-11-26": 2}, "a message to three agents is one message; an answer to a post is none"


def add_item(c: sqlite3.Connection, kind: str = "decision", created: int = T0, **fields: object) -> None:
    columns = ["kind", "created_at", "severity", "text", "source", *fields]
    c.execute(
        f"INSERT INTO user_items ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
        (kind, created, "action", "q", "agent", *fields.values()),
    )


def test_answers_split_by_button_recommended_typed_and_chat(conn: sqlite3.Connection) -> None:
    options = json.dumps(["Ship", "Wait"])
    body = "What: release.\nMy pick: Ship. It is ready."
    with transaction(conn):
        add_item(conn, answer="Ship", answered_at=T0 + HOUR, options=options, body=body)
        add_item(conn, answer="Wait", answered_at=T0 + HOUR, options=options, body=body)
        add_item(conn, kind="blocked", answer="do it Monday", answered_at=T0 + HOUR, options=options, body=body)
        add_item(conn, answer="Ship", answered_at=T0 + HOUR, answered_by="aide", options=options, body=body)
        add_item(conn, kind="done", answer="Ship", answered_at=T0 + HOUR, options=options)
    found = count_answers(conn, CONFIG, T0, None)
    assert found == {
        "button": {"2026-11-26": 2},
        "pick": {"2026-11-26": 1},
        "typed": {"2026-11-26": 1},
        "chat": {"2026-11-26": 1},
    }


def test_the_median_wait_is_over_the_questions_answered_that_day(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        for hours in (1, 2, 10):
            add_item(conn, created=T0, answer="x", answered_at=T0 + hours * HOUR)
        add_item(conn, created=T0 + 2 * DAY, answer="x", answered_at=T0 + 2 * DAY + 60_000)
    medians = map_answers_to_median_hours(conn, CONFIG, T0, None)
    assert medians == {"2026-11-26": 2.0, "2026-11-28": 0.0}
    days = ["2026-11-26", "2026-11-27", "2026-11-28"]
    assert map_measures_to_values(medians, days) == [2.0, None, 0.0], "a day with no answer is blank, a 0 stays 0"


def test_withdrawn_questions_are_closed_with_no_answer(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        add_item(conn, resolved_at=T0 + HOUR)
        add_item(conn, resolved_at=T0 + HOUR, answer="x", answered_at=T0)
        add_item(conn, kind="done", resolved_at=T0 + HOUR)
    assert count_withdrawn_questions(conn, CONFIG, T0, None) == {"2026-11-26": 1}


def test_answers_opened_count_done_and_insight_posts_by_when_he_opened_them(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        add_item(conn, kind="done", opened_at=T0 + HOUR)
        add_item(conn, kind="insight", opened_at=T0 + DAY)
        add_item(conn, kind="decision", opened_at=T0 + HOUR)
        add_item(conn, kind="done")
    assert count_answers_opened(conn, CONFIG, T0, None) == {"2026-11-26": 1, "2026-11-27": 1}


def test_the_feed_lines_leave_out_posts_from_a_locked_tab(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        add_tab(conn, "aide", "t-aide")
        add_tab(conn, "game", "t-game", tab_name="🔒 game")
        for conversation_id in ("c-aide", "c-game", None):
            add_item(conn, answer="mine", answered_at=T0 + HOUR, from_conversation=conversation_id)
            add_item(conn, resolved_at=T0 + HOUR, from_conversation=conversation_id)
            add_item(conn, kind="done", opened_at=T0 + HOUR, from_conversation=conversation_id)
    assert count_answers(conn, CONFIG, T0, None)["typed"] == {"2026-11-26": 2}
    assert map_answers_to_median_hours(conn, CONFIG, T0, None) == {"2026-11-26": 1.0}
    assert count_withdrawn_questions(conn, CONFIG, T0, None) == {"2026-11-26": 2}
    assert count_answers_opened(conn, CONFIG, T0, None) == {"2026-11-26": 2}


def test_published_prs_count_by_their_post_time_and_leave_out_the_ignored_posts(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        posts = [
            (1, f"{T0 // 1000 - 86400}.000100"),
            (2, f"{T0 // 1000 + 60}.000200"),
            (3, f"{T0 // 1000 + 90}.3"),
            (4, f"{T0 // 1000 + 99}.4"),
        ]
        for number, ts in posts:
            conn.execute(
                "INSERT INTO review_slack_posts (repo, number, ts, seen_at) VALUES ('o/r', ?, ?, ?)", (number, ts, T0)
            )
    config = Config(review_ignored_posts=(f"{T0 // 1000 + 90}.3",), work_timezone="UTC")
    found = count_published_prs(conn, config, ["O/r#1", "O/r#2", "O/r#3"], T0 - DAY, None)
    assert found == {"2026-11-25": 1, "2026-11-26": 1}, "his PRs by when posted; #4 is a colleague's"


def test_his_comments_are_the_ones_the_review_queue_judged_his(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        for comment_id, verdict in (("a", "his"), ("b", "agent"), ("c", "unclear")):
            conn.execute(
                "INSERT INTO review_comments (ref, comment_id, created_at, body, url, verdict) "
                "VALUES ('pr:o/r#1', ?, ?, 'x', 'u', ?)",
                (comment_id, T0, verdict),
            )
    assert count_owner_comments(conn, T0, None) == {"2026-11-26": 1}


def test_asks_count_recorded_verified_and_reopened(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO asks (id, owner, recorded_by, words, source, state, created_at, updated_at) "
            "VALUES (1, 'bea', 'aide', 'w', 's', 'open', ?, ?)",
            (T0, T0),
        )
        events = [("verified", "ok"), ("open", "reopened: still broken"), ("open", None), ("done_claimed", "x")]
        for state, note in events:
            conn.execute(
                "INSERT INTO ask_events (ask_id, at, by, state, note) VALUES (1, ?, 'v', ?, ?)", (T0 + DAY, state, note)
            )
    found = count_asks(conn, T0, None)
    assert found == {"recorded": {"2026-11-26": 1}, "verified": {"2026-11-27": 1}, "reopened": {"2026-11-27": 1}}


def test_prs_count_by_the_day_each_thing_happened() -> None:
    prs = [
        OwnerPr(T0 + HOUR, T0 + DAY, T0 + DAY, 100),
        OwnerPr(T0 + HOUR, None, T0 + DAY, 30),
        OwnerPr(T0 - DAY, T0 + HOUR, T0 + HOUR, 7),
        OwnerPr(T0 + DAY, None, None, 5),
    ]
    found = count_prs(prs, T0, None)
    assert found == {
        "opened": {"2026-11-26": 2, "2026-11-27": 1},
        "merged": {"2026-11-27": 1, "2026-11-26": 1},
        "closed": {"2026-11-27": 1},
        "loc_written": {"2026-11-26": 130, "2026-11-27": 5},
        "loc_merged": {"2026-11-27": 100, "2026-11-26": 7},
    }


def pr_node(number: int, merged: str | None = None, files: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "number": number,
        "repository": {"nameWithOwner": "o/r"},
        "createdAt": "2026-11-26T10:00:00Z",
        "mergedAt": merged,
        "closedAt": merged,
        "additions": 120,
        "deletions": 30,
        "files": {"pageInfo": {"hasNextPage": False}, "nodes": files or []},
    }


class FakeGitHub:
    """GraphQL searches answer from `answer(query, page)`; REST file lists from `files`."""

    def __init__(self, answer: Callable[[str, int], dict[str, object]], files: list[Any] | None = None) -> None:
        self.answer = answer
        self.files = files or []
        self.queries: list[str] = []
        self.rest: list[str] = []

    def __call__(self, args: list[str], timeout_s: float = 0) -> str:
        if args[:2] == ["api", "graphql"]:
            query = next(a for a in args if a.startswith("q="))[2:]
            page = sum(1 for q in self.queries if q == query)
            self.queries.append(query)
            return json.dumps({"data": {"search": self.answer(query, page)}})
        self.rest.append(args[-1])
        return json.dumps([self.files])


def one_page(*nodes: dict[str, object], count: int | None = None) -> dict[str, object]:
    return {
        "issueCount": len(nodes) if count is None else count,
        "pageInfo": {"hasNextPage": False},
        "nodes": list(nodes),
    }


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> Any:
    def install(fake: FakeGitHub) -> FakeGitHub:
        monkeypatch.setattr(owner_stats.review_github, "run_gh", fake)
        return fake

    return install


def test_owner_prs_are_paged_and_lose_lockfiles_past_their_first_hundred_files(github: Any) -> None:
    big = pr_node(2, "2026-11-27T10:00:00Z", [{"path": "a.py", "additions": 1, "deletions": 0}])
    big["files"] = {"pageInfo": {"hasNextPage": True}, "nodes": [{"path": "a.py", "additions": 1, "deletions": 0}]}

    def answer(_: str, page: int) -> dict[str, object]:
        if page == 0:
            return {"issueCount": 2, "pageInfo": {"hasNextPage": True, "endCursor": "c1"}, "nodes": [pr_node(1)]}
        return one_page(big, count=2)

    fake = github(FakeGitHub(answer, files=[{"filename": "a/uv.lock", "additions": 100, "deletions": 0}]))
    prs, capped = owner_stats.search_owner_prs(CONFIG, "created:x", lambda: None)
    assert {k: v.loc for k, v in prs.items()} == {"o/r#1": 150, "o/r#2": 50}
    assert (capped, fake.rest) == (False, ["repos/o/r/pulls/2/files?per_page=100"])


def test_the_first_read_goes_a_window_at_a_time_from_the_utc_start_and_takes_older_prs_updated_since(
    github: Any,
) -> None:
    fake = github(FakeGitHub(lambda q, _: one_page(pr_node(len(q)))))
    since = 1_792_270_800_000
    """2026-10-17 21:00 UTC: midnight in a zone 3 hours east."""
    owner_stats.fetch_owner_prs(CONFIG, since, since + 40 * DAY, lambda: None)
    assert fake.queries == [
        "is:pr author:me created:2026-10-17T21:00:00Z..2026-11-16T20:59:59Z",
        "is:pr author:me created:2026-11-16T21:00:00Z..2026-11-26T21:59:59Z",
        "is:pr author:me created:<2026-10-17T21:00:00Z updated:>=2026-10-17T21:00:00Z",
    ]


def test_a_read_from_now_has_no_created_window_and_does_not_fail(github: Any) -> None:
    fake = github(FakeGitHub(lambda q, _: one_page()))
    assert owner_stats.fetch_owner_prs(CONFIG, T0, T0, lambda: None) == ({}, [])
    assert fake.queries == ["is:pr author:me created:<2026-11-26T00:00:00Z updated:>=2026-11-26T00:00:00Z"]


def store(tmp_path: Path, refusal: str | None = None, core: str | None = None) -> owner_stats.PrStore:
    return owner_stats.PrStore(tmp_path / "prs.json", lambda _: refusal, lambda _: core)


def test_the_store_reads_github_at_most_hourly_and_then_only_what_changed(tmp_path: Path, github: Any) -> None:
    fake = github(FakeGitHub(lambda q, _: one_page(pr_node(len(fake.queries)))))
    prs = store(tmp_path)
    prs.refresh(CONFIG, T0 - DAY, T0)
    first = prs.view(CONFIG, T0 - DAY, T0 + HOUR - 1)
    reads = len(fake.queries)
    prs.refresh(CONFIG, T0 - DAY, T0 + HOUR)
    assert first.fetched_at == T0 and not first.loading and reads == 2, "one window and the older-updated search"
    assert fake.queries[-1] == "is:pr author:me updated:>=2026-11-25T00:00:00Z"
    again = store(tmp_path).view(CONFIG, T0 - DAY, T0 + HOUR + 1)
    assert (again.fetched_at, len(again.prs), len(fake.queries)) == (T0 + HOUR, 3, reads + 1), "from the file"


def test_a_capped_search_says_the_history_is_incomplete_and_the_next_read_is_full_again(
    tmp_path: Path, github: Any
) -> None:
    fake = github(FakeGitHub(lambda q, _: one_page(pr_node(1), count=1001 if "created:2026" in q else 1)))
    prs = store(tmp_path)
    prs.refresh(CONFIG, T0 - DAY, T0)
    view = prs.view(CONFIG, T0 - DAY, T0)
    assert view.incomplete and "created:2026-11-25" in view.incomplete
    prs.refresh(CONFIG, T0 - DAY, T0 + HOUR)
    assert fake.queries[-1].startswith("is:pr author:me created:<"), "not marked full: it reads everything again"


def test_a_capped_recent_read_keeps_the_warning_until_a_full_read_is_not_capped(tmp_path: Path, github: Any) -> None:
    cap = {"recent": False}

    def answer(q: str, _: int) -> dict[str, object]:
        recent = q.startswith("is:pr author:me updated:")
        return one_page(pr_node(1), count=1001 if recent and cap["recent"] else 1)

    fake = github(FakeGitHub(answer))
    prs = store(tmp_path)
    prs.refresh(CONFIG, T0 - DAY, T0)
    cap["recent"] = True
    prs.refresh(CONFIG, T0 - DAY, T0 + HOUR)
    view = prs.view(CONFIG, T0 - DAY, T0 + HOUR)
    assert (view.incomplete, view.since_ms) == ("GitHub's 1000-result cap cut recent updates", None)
    reads = len(fake.queries)
    prs.refresh(CONFIG, T0 - DAY, T0 + 2 * HOUR)
    view = prs.view(CONFIG, T0 - DAY, T0 + 2 * HOUR)
    assert any("created:" in q for q in fake.queries[reads:]), "the read after a cut one is full"
    assert (view.incomplete, view.since_ms) == (None, T0 - DAY)


def test_a_low_rest_quota_stops_the_file_pages_and_says_why(tmp_path: Path, github: Any) -> None:
    big = pr_node(2)
    big["files"] = {"pageInfo": {"hasNextPage": True}, "nodes": []}
    fake = github(FakeGitHub(lambda q, _: one_page(big)))
    prs = store(tmp_path, core="REST quota low")
    prs.refresh(CONFIG, T0 - DAY, T0)
    view = prs.view(CONFIG, T0 - DAY, T0)
    assert (view.error, view.fetched_at, fake.rest) == ("REST quota low", None, [])


def test_a_bug_in_the_read_shows_in_the_header_is_logged_and_does_not_hold_the_hour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    logged: list[str] = []

    def fetch(config: Config, since: int, now: int, core_refusal: Callable[[], str | None]) -> Any:
        calls.append(now)
        raise ValueError("bad window")

    monkeypatch.setattr(owner_stats, "fetch_owner_prs", fetch)

    def log(context: str, detail: str) -> None:
        logged.append(detail)

    monkeypatch.setattr(owner_stats, "log_error", log)
    prs = store(tmp_path)
    prs.refresh_or_log(CONFIG, T0 - DAY, T0)
    view = prs.view(CONFIG, T0 - DAY, T0 + 1)
    assert view.error == "read failed: ValueError: bad window"
    assert calls == [T0, T0 + 1], "the next page load read again"
    assert logged and "ValueError" in logged[0]


def test_a_cache_that_cannot_be_written_still_holds_the_hour_and_says_so(tmp_path: Path, github: Any) -> None:
    fake = github(FakeGitHub(lambda q, _: one_page(pr_node(1))))
    (tmp_path / "file").write_text("")
    prs = owner_stats.PrStore(tmp_path / "file" / "prs.json", lambda _: None, lambda _: None)
    prs.refresh(CONFIG, T0 - DAY, T0)
    reads = len(fake.queries)
    view = prs.view(CONFIG, T0 - DAY, T0 + 1)
    assert view.write_error and view.write_error.startswith("cache not saved")
    assert (len(fake.queries), view.fetched_at, view.loading) == (reads, T0, False)


def test_another_account_or_loc_rules_read_everything_anew(tmp_path: Path, github: Any) -> None:
    github(FakeGitHub(lambda q, _: one_page(pr_node(2 if "author:you" in q else 1))))
    prs = store(tmp_path)
    prs.refresh(CONFIG, T0 - DAY, T0)
    other = Config(review_author="you", work_timezone="UTC")
    assert set(prs.view(other, T0 - DAY, T0 + 1).prs) == {"o/r#2"}, "a full read for the other account, not a merge"
    rules = Config(review_author="me", review_generated_files=("gen.ts",), work_timezone="UTC")
    assert read_pr_cache(tmp_path / "prs.json").identity != owner_stats.map_config_to_identity(rules)


@pytest.mark.parametrize(
    "text",
    [
        '{"prs": []}',
        '{"prs": {}, "tried_at": "soon"}',
        "not json",
        '{"prs": {"a": [1]}}',
        '{"prs": {"a": [1, null, null, 1e400]}}',
        '{"prs": {"a": [1e400, null, null, 1]}}',
        '{"prs": {"a": [null, null, null, 1]}}',
        '{"prs": {}, "fetched_at": 1e400}',
    ],
)
def test_a_cache_file_of_the_wrong_shape_reads_as_none(tmp_path: Path, text: str) -> None:
    path = tmp_path / "prs.json"
    path.write_text(text)
    assert read_pr_cache(path) == owner_stats.PrCache()


def test_a_low_graphql_quota_or_a_failed_read_keeps_the_cached_prs_and_says_why(tmp_path: Path, github: Any) -> None:
    github(FakeGitHub(lambda q, _: one_page(pr_node(1))))
    store(tmp_path).refresh(CONFIG, T0 - DAY, T0)
    low = store(tmp_path, "GraphQL quota low")
    low.refresh(CONFIG, T0 - DAY, T0 + HOUR)
    assert (set(low.view(CONFIG, T0 - DAY, T0 + HOUR).prs), low.view(CONFIG, T0 - DAY, T0 + HOUR).error) == (
        {"o/r#1"},
        "GraphQL quota low",
    )

    def broken(q: str, _: int) -> dict[str, object]:
        raise GitHubError("gh failed: boom")

    github(FakeGitHub(broken))
    failing = store(tmp_path)
    failing.refresh(CONFIG, T0 - DAY, T0 + 2 * HOUR)
    view = failing.view(CONFIG, T0 - DAY, T0 + 2 * HOUR)
    assert (set(view.prs), view.error, view.fetched_at) == ({"o/r#1"}, "gh failed: boom", T0)
    assert store(tmp_path).view(Config(), T0, T0).error == "review_author is not set"


def test_the_quota_check_reads_the_graphql_bucket() -> None:
    low = CoreQuota(remaining=100, limit=5000, reset_at=1_795_651_200)
    assert (
        owner_stats.find_graphql_refusal(1500, lambda: low) == "GraphQL quota 100/5000 < 1500 reserve, resets 00:00 UTC"
    )
    assert owner_stats.find_graphql_refusal(50, lambda: low) is None
    assert owner_stats.find_graphql_refusal(1500, lambda: None) is None


def test_a_page_with_no_cached_prs_waits_briefly_then_shows_github_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()
    monkeypatch.setattr(owner_stats, "FIRST_WAIT_S", 0.05)

    def refresh(self: owner_stats.PrStore, config: Config, since: int, now: int) -> None:
        release.wait(5)

    monkeypatch.setattr(owner_stats.PrStore, "refresh", refresh)
    prs = store(tmp_path)
    view = prs.view(CONFIG, T0 - DAY, T0)
    release.set()
    assert view.loading and view.fetched_at is None


def test_a_line_is_blank_before_its_source_has_a_record() -> None:
    days = ["2026-11-25", "2026-11-26", "2026-11-27"]
    assert map_counts_to_values({"2026-11-26": 1500}, days, 1000) == [None, 1.5, 0]
    assert map_counts_to_values({}, days) == [None, None, None]


def test_the_page_data_has_every_line_over_every_day(conn: sqlite3.Connection, tmp_path: Path) -> None:
    with transaction(conn):
        add_tab(conn, "aide", "t-aide")
        conn.execute(
            "INSERT INTO messages (id, created_at, from_conversation, from_handle, kind, body) "
            "VALUES (1, ?, ?, 'sam', 'info', 'x')",
            (T0 + HOUR, USER_CONVERSATION),
        )
        conn.execute(
            "INSERT INTO message_recipients (message_id, conversation_id, handle) VALUES (1, 'c-aide', 'aide')"
        )
        transcript = tmp_path / "c.jsonl"
        transcript.write_bytes(entry("hi", "2026-11-27T08:00:00Z") + b"\n")
        conn.execute(
            "UPDATE conversations SET transcript_path = ? WHERE conversation_id = 'c-aide'", (str(transcript),)
        )
    stats = map_db_to_stats(
        conn, CONFIG, T0 + DAY + 9 * HOUR, PromptReader().read, lambda _: read_pr_cache(tmp_path / "none")
    )
    assert stats["github"] == {
        "fetched_at": None,
        "error": None,
        "incomplete": None,
        "write_error": None,
        "loading": False,
        "prs": 0,
    }
    lines: Any = stats["lines"]
    by_key = {line["key"]: line for line in lines}
    assert stats["days"] == ["2026-11-26", "2026-11-27"]
    assert by_key["msg_aide"]["values"] == [1, 1]
    assert by_key["msg_all"]["values"] == [1, 1]
    assert by_key["loc_merged"]["unit"] == "kloc"
    assert by_key["q_hours"]["unit"] == "hours"
    assert {line["group"] for line in lines} == {"Messages", "Questions", "Asks", "PRs", "Lines of code", "Comments"}


def test_messages_are_blank_on_days_whose_transcripts_claude_code_has_deleted(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(owner_stats, "TRANSCRIPT_KEEP_DAYS", 1)
    with transaction(conn):
        conn.execute(
            "INSERT INTO messages (id, created_at, from_conversation, from_handle, kind, body) "
            "VALUES (1, ?, ?, 'sam', 'info', 'x')",
            (T0 + HOUR, USER_CONVERSATION),
        )
        add_tab(conn, "aide", "t-aide")
        conn.execute(
            "INSERT INTO message_recipients (message_id, conversation_id, handle) VALUES (1, 'c-aide', 'aide')"
        )
        add_item(conn, answer="mine", answered_at=T0 + HOUR)
    stats = map_db_to_stats(
        conn, CONFIG, T0 + 2 * DAY + HOUR, PromptReader().read, lambda _: read_pr_cache(tmp_path / "none")
    )
    lines: Any = stats["lines"]
    by_key = {line["key"]: line for line in lines}
    assert by_key["msg_all"]["values"] == [None, 0, 0]
    assert by_key["q_typed"]["values"] == [1, 0, 0], "other sources keep their history"
