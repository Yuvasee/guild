import json
import sqlite3
from pathlib import Path
from typing import Any, cast
from unittest import mock

import pytest
from conftest import BridgeEnv, SpawnProcess
from test_leads import Team
from test_reviews import CONFIG, REF, add_session, comment, make_pr, store, wake

from bridge.board import MessageStateError, Party
from bridge.colleague_comments import (
    FEED_FOR_MS,
    UNFINISHED_AFTER_MS,
    claim_choice,
    latch_held_breaches,
    list_open_calls,
    map_arg_to_comment_key,
    map_db_to_colleague_feed,
    record_draft,
    record_small_call,
    settle_unfinished_choices,
)
from bridge.db import connect, now_ms, transaction
from bridge.errors import UsageError
from bridge.feed import answer_post
from bridge.guildview import map_db_to_guild
from bridge.review_github import FetchedComment, GitHubError
from bridge.reviews import mark_closed, store_open_prs
from bridge.serve import DashboardState

NOW = 200_000
LEAD = Party("c1", "worker")


def setup_pr(tmp_path: Path, **pr: Any) -> Any:
    conn = connect(tmp_path / "bridge.db")
    with transaction(conn):
        store_open_prs(conn, [make_pr(**pr)], 500)
    return conn


def told(tmp_path: Path, *comments: Any, **pr: Any) -> Any:
    conn = setup_pr(tmp_path, **pr)
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
    store(conn, *comments)
    wake(conn, NOW)
    return conn


def feed(conn: Any, now: int = NOW) -> dict[str, dict[str, Any]]:
    return {str(e["comment_id"]): e for e in map_db_to_colleague_feed(conn, CONFIG, now)}


def test_a_colleagues_comment_wakes_with_the_owners_rule_and_records_who_was_told(tmp_path: Path) -> None:
    conn = told(tmp_path, comment("p1", author="mira", body="This leaks the token"))
    body = conn.execute("SELECT body FROM messages").fetchone()[0]
    assert "guild comment small <link>" in body and "unsure = substantive" in body
    assert "Fix what this PR introduced" not in body and "held" not in body
    row = conn.execute("SELECT owner_handle, call FROM colleague_calls").fetchone()
    assert (row["owner_handle"], row["call"]) == ("worker", None)


def test_a_colleagues_change_request_is_held_in_the_wake_and_on_the_page(tmp_path: Path) -> None:
    conn = told(tmp_path, comment("r1", author="mira", body="Split this", state="CHANGES_REQUESTED", commit="a1"))
    body = conn.execute("SELECT body FROM messages").fetchone()[0]
    assert "mira requested changes: substantive, so reply and push for it are held" in body
    entry = feed(conn)["r1"]
    assert (entry["status"], entry["stands_out"], entry["review"]) == ("substantive", True, "CHANGES_REQUESTED")
    with transaction(conn), pytest.raises(UsageError, match="substantive"):
        record_small_call(conn, REF, "r1", "split it", LEAD, NOW)


def test_the_feed_is_newest_first_with_each_call_and_a_bare_approval_needs_none(tmp_path: Path) -> None:
    conn = told(
        tmp_path,
        comment("i1", at=1000, author="mira", body="typo: recieve"),
        comment("r2", at=1001, author="mira", body="", state="APPROVED", commit="a1"),
        comment("p3", at=1002, author="dan", body="x" * 400),
        approved_by=("mira",),
    )
    with transaction(conn):
        record_small_call(conn, REF, "i1", "fixed the typo in 4be1", LEAD, NOW)
    entries = map_db_to_colleague_feed(conn, CONFIG, NOW)
    assert [e["comment_id"] for e in entries] == ["p3", "r2", "i1"]
    assert [(e["status"], e["stands_out"]) for e in entries] == [
        ("undecided", True),
        ("approved", False),
        ("small", False),
    ]
    assert entries[2]["note"] == "fixed the typo in 4be1" and entries[2]["by"] == "worker"
    assert len(str(entries[0]["quote"])) == 300 and str(entries[0]["quote"]).endswith("…")
    assert entries[0]["label"] == "app#7" and entries[0]["lead"] == "worker"


def test_an_undecided_comment_on_a_closed_pr_no_longer_stands_out(tmp_path: Path) -> None:
    conn = told(tmp_path, comment("i1", author="mira"))
    with transaction(conn):
        mark_closed(conn, [REF], NOW)
    assert feed(conn)["i1"]["stands_out"] is False
    assert list_open_calls(conn, CONFIG, LEAD, None, True, NOW) == []


def test_a_comment_is_found_by_its_link_or_its_id(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    store(conn, comment("p11", author="mira"), comment("r12", author="mira", body="ok", state="COMMENTED"))
    assert map_arg_to_comment_key(conn, "https://x/p11") == (REF, "p11")
    assert map_arg_to_comment_key(conn, "https://github.com/Org/app/pull/7#discussion_r11") == (REF, "p11")
    assert map_arg_to_comment_key(conn, "https://github.com/Org/app/pull/7#pullrequestreview-12") == (REF, "r12")
    assert map_arg_to_comment_key(conn, "12") == (REF, "r12")
    assert map_arg_to_comment_key(conn, "p11") == (REF, "p11")
    with pytest.raises(UsageError, match="has not read"):
        map_arg_to_comment_key(conn, "i11")
    with pytest.raises(UsageError, match="not a link"):
        map_arg_to_comment_key(conn, "https://github.com/Org/app/pull/7")


def test_only_a_colleagues_classified_comment_takes_a_call_and_it_says_what_was_done(tmp_path: Path) -> None:
    conn = told(tmp_path, comment("i1", author="mira"), comment("i2", at=1001, body="mine"))
    with transaction(conn), pytest.raises(UsageError, match="not a colleague's"):
        record_small_call(conn, REF, "i2", "done", LEAD, NOW)
    with transaction(conn), pytest.raises(UsageError, match="say what you did"):
        record_small_call(conn, REF, "i1", "  ", LEAD, NOW)


def test_the_open_list_is_the_ones_told_to_this_session_or_its_lead(tmp_path: Path) -> None:
    conn = told(tmp_path, comment("i1", author="mira"))
    assert [e["comment_id"] for e in list_open_calls(conn, CONFIG, LEAD, None, False, NOW)] == ["i1"]
    other = Party("c9", "someone")
    assert list_open_calls(conn, CONFIG, other, None, False, NOW) == []
    assert len(list_open_calls(conn, CONFIG, other, None, True, NOW)) == 1
    with transaction(conn):
        record_small_call(conn, REF, "i1", "answered: yes, it is cached", LEAD, NOW)
    assert list_open_calls(conn, CONFIG, other, None, True, NOW) == []


def test_a_push_or_a_reply_to_it_while_a_change_request_is_held_shows_and_other_work_on_the_pr_does_not(
    tmp_path: Path,
) -> None:
    review = comment("r1", author="mira", body="Split this into two PRs", state="CHANGES_REQUESTED", commit="a1")
    conn = told(tmp_path, review)

    def latch(at: int) -> list[str]:
        with transaction(conn):
            latch_held_breaches(conn, at)
        return cast(list[str], feed(conn, at)["r1"]["breaches"])

    with transaction(conn):
        conn.execute("UPDATE review_branch_writes SET at = ? WHERE branch = 'dev/x'", (NOW + 5,))
        conn.execute("INSERT INTO review_heads VALUES (?, 'b0', ?)", (REF, NOW - 5))
    store(conn, comment("i2", at=NOW + 6, body="🤖 Claude: @mira renamed, thanks"))
    with transaction(conn):
        conn.execute("UPDATE review_comments SET verdict = 'agent' WHERE comment_id = 'i2'")
    assert latch(NOW + 10) == []
    with transaction(conn):
        conn.execute("INSERT INTO review_heads VALUES (?, 'b1', ?)", (REF, NOW + 20))
    store(conn, comment("i3", at=NOW + 30, body="🤖 Claude:\n> Split this into two PRs\n\nDone in 9f2"))
    with transaction(conn):
        conn.execute("UPDATE review_comments SET verdict = 'agent' WHERE comment_id = 'i3'")
    assert latch(NOW + 5000) == ["pushed while held", "replied while held"]
    assert feed(conn, NOW + 5000)["r1"]["stands_out"] is True


def test_the_guild_page_carries_the_feed(tmp_path: Path) -> None:
    conn = told(tmp_path, comment("i1", author="mira"))
    page = map_db_to_guild(conn, CONFIG, NOW, lambda ref: "")
    assert [e["comment_id"] for e in cast(list[dict[str, Any]], page["colleague_comments"])] == ["i1"]


def test_a_lead_records_a_small_call_by_link_and_lists_its_open_ones(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    pr = make_pr(ref="pr:acme/web#9", repo="acme/web", number=9, url="https://github.com/acme/web/pull/9")
    with bridge_env.connect() as conn, transaction(conn):
        store_open_prs(conn, [pr], 1)
        conn.execute(
            """
            INSERT INTO review_comments (ref, comment_id, created_at, body, url, author, verdict, told_at)
            VALUES ('pr:acme/web#9', 'p5', ?, 'nit: rename x', 'https://github.com/acme/web/pull/9#discussion_r5',
                    'mira', 'colleague', ?)
            """,
            (now_ms() - 2000, now_ms() - 1000),
        )
    listed = team.guild("comment", "list", "--all", pid=team.a)
    assert listed.returncode == 0 and "p5 web#9 mira: undecided" in listed.stdout
    done = team.guild("comment", "small", "https://github.com/acme/web/pull/9#discussion_r5", "renamed", pid=team.a)
    assert done.returncode == 0, done.stderr
    assert "no colleague comment waits" in team.guild("comment", "list", "--all", pid=team.a).stdout
    with bridge_env.connect() as conn:
        row = conn.execute("SELECT call, note, called_by FROM colleague_calls").fetchone()
    assert (row["call"], row["note"]) == ("small", "renamed") and row["called_by"]


class FakeGitHub:
    """Answers gh api: a POST adds the comment, a listing returns what is there."""

    def __init__(self, fails: str | None = None, has: list[dict[str, object]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fails = fails
        self.comments: list[dict[str, object]] = list(has or [])

    @property
    def posts(self) -> list[list[str]]:
        return [c for c in self.calls if "POST" in c]

    def __call__(self, args: list[str]) -> str:
        self.calls.append(args)
        if self.fails:
            raise GitHubError(self.fails)
        if "POST" not in args:
            return json.dumps([self.comments])
        posted: dict[str, object] = {
            "body": args[-1].removeprefix("body="),
            "html_url": f"https://github.com/Org/app/pull/7#c{900 + len(self.comments)}",
        }
        self.comments.append(posted)
        return json.dumps(posted)


def drafted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *comments: Any) -> tuple[Any, DashboardState, int]:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    conn = told(tmp_path, *comments)
    first = str(comments[0].comment_id)
    with transaction(conn):
        item = record_draft(
            conn, CONFIG, REF, first, "🤖 Claude: Fair point, I will split it.", "split in two", LEAD, NOW
        )
    state = DashboardState(CONFIG)
    return conn, state, item


def choose(state: DashboardState, item: int, choice: str, text: str = "", github: Any = None) -> tuple[bool, str]:
    state._github = github or FakeGitHub()  # pyright: ignore[reportPrivateUsage]
    with mock.patch.object(state, "_deliver"):
        return state.choose_colleague_reply(item, choice, text)


def test_a_draft_waits_for_the_owner_as_a_needs_you_card_with_the_comment_draft_and_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, _, item = drafted(tmp_path, monkeypatch, comment("p1", author="mira", body="Split this into two PRs"))
    body = conn.execute("SELECT body FROM messages").fetchone()[0]
    assert "guild comment draft <link>" in body and "bridge post decision" not in body
    row = conn.execute("SELECT kind, text FROM user_items WHERE id = ?", (item,)).fetchone()
    assert (row["kind"], row["text"]) == ("decision", "Reply to mira on app#7?")
    entry = feed(conn)["p1"]
    assert (entry["status"], entry["stands_out"], entry["draft"], entry["note"]) == (
        "draft",
        True,
        "Fair point, I will split it.",
        "split in two",
    )
    needs = cast(list[dict[str, Any]], map_db_to_guild(conn, CONFIG, NOW, lambda ref: "")["needs"])
    card = next(n for n in needs if n.get("id") == item)
    assert card["colleague"]["quote"] == "Split this into two PRs" and card["colleague"]["draft"]
    with transaction(conn), pytest.raises(MessageStateError, match="draft reply to a colleague"):
        answer_post(conn, item, "Send as is", "user", NOW)
    with transaction(conn), pytest.raises(UsageError, match="already has a draft"):
        record_small_call(conn, REF, "p1", "renamed", LEAD, NOW)


def test_a_new_draft_replaces_the_one_still_waiting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn, _, item = drafted(tmp_path, monkeypatch, comment("p1", author="mira"))
    with transaction(conn):
        again = record_draft(conn, CONFIG, REF, "p1", "Better words", "same plan", LEAD, NOW + 1)
    resolved = dict(conn.execute("SELECT id, resolved_at IS NOT NULL FROM user_items").fetchall())
    assert resolved == {item: 1, again: 0}
    assert feed(conn)["p1"]["draft"] == "Better words"


def test_send_as_is_replies_in_the_thread_and_tells_the_lead_the_final_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, state, item = drafted(tmp_path, monkeypatch, comment("p1", author="mira"))
    github = FakeGitHub()
    assert choose(state, item, "as-is", github=github) == (True, "sent")
    assert github.posts == [
        [
            "api", "-X", "POST", "repos/Org/app/pulls/7/comments/1/replies",
            "-f", "body=🤖 Claude: Fair point, I will split it.",
        ]
    ]  # fmt: skip
    entry = feed(conn)["p1"]
    assert (entry["status"], entry["reply_url"], entry["stands_out"]) == (
        "as-is",
        "https://github.com/Org/app/pull/7#c900",
        False,
    )
    item_row = conn.execute("SELECT answer, resolved_at, answer_message_id FROM user_items").fetchone()
    assert item_row["answer"].startswith("Send as is") and item_row["resolved_at"] is not None
    told_lead = conn.execute("SELECT body FROM messages WHERE id = ?", (item_row["answer_message_id"],)).fetchone()[0]
    assert "send your draft as is" in told_lead and "Fair point, I will split it." in told_lead
    assert "#c900" in told_lead and "split in two" in told_lead
    assert choose(state, item, "as-is", github=github)[0] is False and len(github.posts) == 1


def test_an_edited_reply_to_a_review_is_a_pr_comment_quoting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = comment(
        "r5", author="mira", body="Counting is spread out.\nMove it into one place.", state="CHANGES_REQUESTED"
    )
    conn, state, item = drafted(tmp_path, monkeypatch, review)
    assert choose(state, item, "edited", "  ")[0] is False
    github = FakeGitHub()
    assert choose(state, item, "edited", "Agreed; moved into the counter in 3a1.", github)[0] is True
    path, body = github.posts[0][3], github.posts[0][5]
    assert path == "repos/Org/app/issues/7/comments"
    assert body == (
        "body=🤖 Claude: @mira, on [your review](https://x/r5):\n\n"
        "> Counting is spread out.\n> Move it into one place.\n\nAgreed; moved into the counter in 3a1."
    )
    assert (
        feed(conn)["r5"]["status"] == "edited" and feed(conn)["r5"]["reply"] == "Agreed; moved into the counter in 3a1."
    )


def test_answering_themselves_posts_nothing_and_tells_the_lead_not_to_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, state, item = drafted(tmp_path, monkeypatch, comment("p1", author="mira"))
    github = FakeGitHub()
    assert choose(state, item, "himself", github=github) == (True, "sent")
    assert github.calls == []
    assert feed(conn)["p1"]["status"] == "himself"
    assert "Do not reply to it" in conn.execute("SELECT body FROM messages ORDER BY id DESC").fetchone()[0]


def test_a_failed_post_brings_the_card_back_with_the_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn, state, item = drafted(tmp_path, monkeypatch, comment("p1", author="mira"))
    ok, detail = choose(state, item, "as-is", github=FakeGitHub(fails="gh failed: HTTP 422"))
    assert not ok and "HTTP 422" in detail
    entry = feed(conn)["p1"]
    assert (entry["status"], entry["reply_error"], entry["stands_out"]) == ("failed", "gh failed: HTTP 422", True)
    assert conn.execute("SELECT resolved_at FROM user_items").fetchone()[0] is None
    assert choose(state, item, "as-is")[0] is True
    assert feed(conn)["p1"]["reply_error"] is None


def test_an_agents_reply_in_the_thread_while_a_draft_waits_shows_as_replied_while_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, _, _ = drafted(tmp_path, monkeypatch, comment("p1", author="mira"))
    store(conn, FetchedComment("p2", "me", NOW + 20, "🤖 Claude: done", "https://x/p2", parent_id="p1"))
    with transaction(conn):
        conn.execute("UPDATE review_comments SET verdict = 'agent' WHERE comment_id = 'p2'")
        latch_held_breaches(conn, NOW + 5000)
    assert feed(conn, NOW + 5000)["p1"]["breaches"] == ["replied while held"]


def test_a_lead_drafts_from_a_file(bridge_env: BridgeEnv, spawn_process: SpawnProcess, tmp_path: Path) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    pr = make_pr(ref="pr:acme/web#9", repo="acme/web", number=9, url="https://github.com/acme/web/pull/9")
    with bridge_env.connect() as conn, transaction(conn):
        store_open_prs(conn, [pr], 1)
        conn.execute(
            """
            INSERT INTO review_comments (ref, comment_id, created_at, body, url, author, verdict, told_at)
            VALUES ('pr:acme/web#9', 'p5', ?, 'make this async', 'https://github.com/acme/web/pull/9#discussion_r5',
                    'mira', 'colleague', ?)
            """,
            (now_ms() - 2000, now_ms() - 1000),
        )
    reply = tmp_path / "reply.md"
    reply.write_text("Not in this PR: it would change the API.\n")
    done = team.guild("comment", "draft", "p5", "--reply-file", str(reply), "--plan", "follow-up issue", pid=team.a)
    assert done.returncode == 0, done.stderr
    assert "waits for" in done.stdout and "held" in done.stdout
    assert "draft waits for the owner" in team.guild("comment", "list", "--all", pid=team.a).stdout


class TimesOutAfterPosting(FakeGitHub):
    def __call__(self, args: list[str]) -> str:
        answer = super().__call__(args)
        if "POST" in args and len(self.posts) == 1:
            raise GitHubError("gh failed: TimeoutExpired")
        return answer


def test_a_post_that_timed_out_after_github_took_it_is_found_and_never_posted_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, state, item = drafted(tmp_path, monkeypatch, comment("p1", author="mira"))
    github = TimesOutAfterPosting()
    assert choose(state, item, "as-is", github=github)[0] is False
    assert feed(conn)["p1"]["status"] == "failed"
    assert choose(state, item, "as-is", github=github) == (True, "sent")
    assert len(github.posts) == 1 and feed(conn)["p1"]["reply_url"] == "https://github.com/Org/app/pull/7#c900"


def test_a_post_whose_record_failed_waits_unoffered_and_is_settled_from_github(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, state, item = drafted(tmp_path, monkeypatch, comment("p1", author="mira"))
    github = FakeGitHub()
    with mock.patch("bridge.serve.finish_choice", side_effect=sqlite3.OperationalError("database is locked")):
        ok, detail = choose(state, item, "as-is", github=github)
    assert not ok and "posted as https://github.com/Org/app/pull/7#c900" in detail
    assert feed(conn)["p1"]["status"] == "posting"
    assert choose(state, item, "as-is", github=github)[0] is False
    assert settle_unfinished_choices(conn, CONFIG, github, "user", NOW + 1) == []
    settled = settle_unfinished_choices(conn, CONFIG, github, "user", now_ms() + UNFINISHED_AFTER_MS + 1)
    assert len(settled) == 1 and len(github.posts) == 1
    assert (feed(conn)["p1"]["status"], feed(conn)["p1"]["reply_url"]) == (
        "as-is",
        "https://github.com/Org/app/pull/7#c900",
    )


def test_a_choice_cut_off_before_posting_goes_back_to_the_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, _, item = drafted(tmp_path, monkeypatch, comment("p1", author="mira"))
    with transaction(conn):
        claim_choice(conn, item, "as-is", "", now_ms())
    github = FakeGitHub()
    assert settle_unfinished_choices(conn, CONFIG, github, "user", now_ms() + UNFINISHED_AFTER_MS + 1) == []
    entry = feed(conn)["p1"]
    assert (entry["status"], github.posts) == ("failed", [])
    assert "nothing was posted" in str(entry["reply_error"])


def test_a_reply_to_a_colleagues_reply_goes_through_the_threads_first_comment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reply = FetchedComment("p2", "mira", 1001, "Still leaks", "https://x/p2", parent_id="p1", parent_author="me")
    _, state, item = drafted(tmp_path, monkeypatch, reply)
    github = FakeGitHub()
    assert choose(state, item, "as-is", github=github)[0] is True
    assert github.posts[0][3] == "repos/Org/app/pulls/7/comments/1/replies"


def test_an_edit_after_a_try_that_github_took_anyway_posts_nothing_and_reports_what_is_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, state, item = drafted(tmp_path, monkeypatch, comment("p1", author="mira"))
    github = TimesOutAfterPosting()
    assert choose(state, item, "as-is", github=github)[0] is False
    assert choose(state, item, "edited", "Other words", github)[0] is True
    assert len(github.posts) == 1
    entry = feed(conn)["p1"]
    assert (entry["reply"], entry["reply_url"]) == (
        "Fair point, I will split it.",
        "https://github.com/Org/app/pull/7#c900",
    )


def test_the_open_list_and_the_page_keep_an_old_open_comment_past_the_feeds_window(tmp_path: Path) -> None:
    conn = told(tmp_path, comment("i1", author="mira"))
    later = NOW + FEED_FOR_MS + 1
    assert [e["comment_id"] for e in list_open_calls(conn, CONFIG, LEAD, None, True, later)] == ["i1"]
    assert [e["comment_id"] for e in map_db_to_colleague_feed(conn, CONFIG, later)] == ["i1"]
    with transaction(conn):
        record_small_call(conn, REF, "i1", "answered", LEAD, NOW)
    assert map_db_to_colleague_feed(conn, CONFIG, later) == []


def test_the_owners_own_reply_dated_to_the_second_before_the_choice_is_no_breach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, _, item = drafted(tmp_path, monkeypatch, comment("p1", author="mira"))
    with transaction(conn):
        conn.execute("UPDATE colleague_calls SET choice = 'as-is', chosen_at = ? WHERE item_id = ?", (NOW + 2700, item))
    store(conn, FetchedComment("p3", "me", NOW + 2000, "🤖 Claude: Fair point", "https://x/p3", parent_id="p1"))
    with transaction(conn):
        conn.execute("UPDATE review_comments SET verdict = 'agent' WHERE comment_id = 'p3'")
        latch_held_breaches(conn, NOW + 3000)
    assert feed(conn, NOW + 3000)["p1"]["breaches"] == []


def test_a_comment_read_before_owners_were_recorded_shows_the_prs_current_owner(tmp_path: Path) -> None:
    conn = told(tmp_path, comment("i1", author="mira"))
    with transaction(conn):
        conn.execute("DELETE FROM colleague_calls")
    assert feed(conn)["i1"]["lead"] == "worker"
    with transaction(conn):
        conn.execute("UPDATE instances SET ended_at = 1")
    assert feed(conn)["i1"]["lead"] is None


def test_the_quote_drops_bold_and_code_marks_and_keeps_the_words(tmp_path: Path) -> None:
    conn = told(tmp_path, comment("p1", author="mira", body="**[Should-fix]** `last_access = None` skips __init__"))
    assert feed(conn)["p1"]["quote"] == "[Should-fix] last_access = None skips __init__"


def test_the_page_shows_only_what_needs_attention(tmp_path: Path) -> None:
    """The owner: 'Colleague comments in the dashboard should show only things which need attention'."""
    conn = told(
        tmp_path,
        comment("r9", author="mira", body="Rework this", state="CHANGES_REQUESTED", commit="a1"),
        comment("p1", author="mira", body="Split the module", review="r9"),
        comment("p2", author="mira", body="typo", review="r9"),
    )
    entries = feed(conn)
    assert {k: e["needs_attention"] for k, e in entries.items()} == {"r9": True, "p1": True, "p2": True}
    with transaction(conn):
        record_small_call(conn, REF, "p1", "renamed", LEAD, NOW)
        record_small_call(conn, REF, "p2", "fixed", LEAD, NOW)
    entries = feed(conn)
    # A change request whose own comments are all settled is settled with them.
    assert {k: e["needs_attention"] for k, e in entries.items()} == {"r9": False, "p1": False, "p2": False}
    with transaction(conn):
        mark_closed(conn, [REF], NOW)
    assert not any(e["needs_attention"] for e in feed(conn).values())


def test_a_draft_or_an_edited_reply_with_an_unfilled_slot_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, _, item = drafted(tmp_path, monkeypatch, comment("p1", author="mira"), comment("p2", author="mira"))
    for text in ("Fixed in <sha>.", "See <link>", "Tracked in ACME-xxxx", "TODO: answer"):
        with transaction(conn), pytest.raises(UsageError, match="fill it in"):
            record_draft(conn, CONFIG, REF, "p2", text, "plan", LEAD, NOW)
    with transaction(conn):
        record_draft(conn, CONFIG, REF, "p2", "Fixed in a1b2c3d, tracked in ACME-12.", "plan", LEAD, NOW)
    with transaction(conn), pytest.raises(UsageError, match="<sha>"):
        claim_choice(conn, item, "edited", "Fixed in <sha>.", now_ms())
    assert feed(conn)["p1"]["status"] != "edited"
