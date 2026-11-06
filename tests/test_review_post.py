import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from conftest import VENV_BIN, BridgeEnv, free_port, http_request, wait_up
from test_reviews import CONFIG, REF, add_lead, add_session, make_pr, setup_pr

from bridge.config import Config
from bridge.db import transaction
from bridge.review_post import (
    COMMENT_MAX_CHARS,
    DRY_RUN,
    EXTERNAL,
    FAILED,
    INTERRUPTED,
    POSTED,
    POSTED_NO_LINK,
    SEND_TOOL,
    SENDING,
    WAITING_DRAFT,
    WAITING_SENTENCE,
    WAITING_STACK,
    ReviewError,
    advance_posts,
    build_send_command,
    build_send_env,
    map_arg_to_ref,
    map_events_to_outcome,
    map_pr_to_slack_text,
    map_typed_comment_to_slack,
    record_publish,
    record_published_elsewhere,
    record_sentence,
    reopen_unsent_post,
    retry_failed_post,
    run_review_post,
    send_to_slack,
)
from bridge.reviews import map_db_to_reviews, store_open_prs

TEXT = "https://github.com/Org/app/pull/7 - Part of the export project."
LIVE = Config(review_post_dry_run=False, review_channel_id="C1")


def tool_use(name: str = SEND_TOOL, channel: str = "C1", text: str = TEXT) -> str:
    block = {"type": "tool_use", "id": "t1", "name": name, "input": {"channel_id": channel, "message": text}}
    return json.dumps({"type": "assistant", "message": {"content": [block]}})


def tool_result(content: object, is_error: bool | None = None) -> str:
    block = {"type": "tool_result", "tool_use_id": "t1", "content": content, "is_error": is_error}
    return json.dumps({"type": "user", "message": {"content": [block]}})


def final(text: str) -> str:
    return json.dumps({"type": "result", "result": text})


def test_post_text_is_the_link_the_description_direct_and_the_people_the_owner_picked() -> None:
    assert map_pr_to_slack_text("http://u", " Exports  run\nnightly. ", False, []) == "http://u - Exports run nightly."
    assert (
        map_pr_to_slack_text("http://u", "Exports run", True, ["U1", "U2"])
        == "http://u - Exports run - direct <@U1> <@U2>"
    )


def test_owner_comment_follows_the_mentions_and_needs_someone_mentioned() -> None:
    assert (
        map_pr_to_slack_text("http://u", "Exports run", True, ["U1", "U2"], "please look at\nthe retry")
        == "http://u - Exports run - direct <@U1> <@U2> please look at\nthe retry"
    )
    assert map_pr_to_slack_text("http://u", "Exports run", False, ["U1"], "") == "http://u - Exports run <@U1>"
    assert map_pr_to_slack_text("http://u", "Exports run", True, [], "ignored") == "http://u - Exports run - direct"


def test_owner_comment_goes_out_as_typed_with_picker_names_made_mentions() -> None:
    people = {"Kai": "U01AAAA0001", "Nina": "U01BBBB0002"}
    typed = "  @kai & @Nina: see <https://x.test|the log>, <@U77> and a@x.test; @Kaira @Bob\r\nthanks \n"
    assert map_typed_comment_to_slack(typed, people) == (
        "<@U01AAAA0001> & <@U01BBBB0002>: see <https://x.test|the log>, <@U77> and a@x.test; @Kaira @Bob\nthanks"
    )
    assert map_typed_comment_to_slack(" @Kai ", {}) == "@Kai"
    assert map_typed_comment_to_slack("<https://x.test/@Kai|profile> (@Kai) @Ka\u0131", people) == (
        "<https://x.test/@Kai|profile> (<@U01AAAA0001>) @Ka\u0131"
    )


def test_owner_comment_is_capped_after_trimming_the_ends() -> None:
    assert len(map_typed_comment_to_slack(" " + "x" * COMMENT_MAX_CHARS + "\n", {})) == COMMENT_MAX_CHARS
    with pytest.raises(ReviewError, match=f"301 characters; at most {COMMENT_MAX_CHARS}"):
        map_typed_comment_to_slack("x" * (COMMENT_MAX_CHARS + 1), {})


def test_a_description_is_at_most_seven_words_and_carries_no_agent_prefix(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        record_sentence(conn, CONFIG, REF, "One two three four five six seven", "alex", 900)
        with pytest.raises(ReviewError, match=r"at most 7 words.*has 8"):
            record_sentence(conn, CONFIG, REF, "One two three four five six seven eight", "alex", 900)
        with pytest.raises(ReviewError, match="no '🤖 Claude:' on Slack"):
            record_sentence(conn, CONFIG, REF, "🤖 Claude: Exports run nightly", "alex", 900)


def test_a_stored_description_too_long_for_the_new_format_is_asked_for_again(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
        conn.execute("UPDATE review_prs SET sentence = 'Part of the export project for every team we have'")
    assert publish(conn)[0] == []
    assert post_state(conn) == "waiting-sentence"
    assert "at most 7 words" in conn.execute("SELECT body FROM messages").fetchone()[0]


def test_what_the_owner_picked_in_the_form_shapes_the_post(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    add_sentence(conn)
    with transaction(conn):
        record_publish(conn, REF, "sam", 1000, True, ("U01AAAA0001",))
        assert advance_posts(conn, CONFIG, 1000)[0] == [REF]
    with mock.patch.dict("os.environ", {"BRIDGE_STATE_DIR": str(tmp_path)}):
        run_review_post(conn, CONFIG, REF)
    logged = json.loads((tmp_path / "review-posts.log").read_text())
    assert logged["text"] == f"{TEXT} - direct <@U01AAAA0001>"


def test_owner_comment_is_stored_and_logged_with_the_post(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    add_sentence(conn)
    with transaction(conn):
        record_publish(conn, REF, "sam", 1000, False, ("U01AAAA0001",), "the retry path <@U77>")
        assert advance_posts(conn, CONFIG, 1000)[0] == [REF]
    assert conn.execute("SELECT post_comment FROM review_prs").fetchone()[0] == "the retry path <@U77>"
    with mock.patch.dict("os.environ", {"BRIDGE_STATE_DIR": str(tmp_path)}):
        run_review_post(conn, CONFIG, REF)
    logged = json.loads((tmp_path / "review-posts.log").read_text())
    assert logged["text"] == f"{TEXT} <@U01AAAA0001> the retry path <@U77>"


def test_a_comment_with_nobody_mentioned_is_dropped(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    add_sentence(conn)
    with transaction(conn):
        record_publish(conn, REF, "sam", 1000, True, (), "nobody reads this")
        assert advance_posts(conn, CONFIG, 1000)[0] == [REF]
    assert conn.execute("SELECT post_comment FROM review_prs").fetchone()[0] is None
    with mock.patch.dict("os.environ", {"BRIDGE_STATE_DIR": str(tmp_path)}):
        run_review_post(conn, CONFIG, REF)
    assert json.loads((tmp_path / "review-posts.log").read_text())["text"] == f"{TEXT} - direct"


def test_a_permalink_is_read_from_the_tool_result() -> None:
    content = [{"type": "text", "text": '{"link":"https:\\/\\/acme.slack.com\\/archives\\/C1\\/p123"}'}]
    outcome = map_events_to_outcome("\n".join([tool_use(), tool_result(content)]), "C1", TEXT)
    assert (outcome.state, outcome.link, outcome.detail) == (POSTED, "https://acme.slack.com/archives/C1/p123", None)


def test_a_called_tool_with_no_readable_link_is_sent_and_never_retried() -> None:
    outcome = map_events_to_outcome("\n".join([tool_use(), tool_result("ok")]), "C1", TEXT)
    assert (outcome.state, outcome.link) == (POSTED_NO_LINK, None)
    assert map_events_to_outcome(tool_use(), "C1", TEXT).state == POSTED_NO_LINK


def test_invented_results_in_the_models_text_count_for_nothing() -> None:
    invented = final('<invoke name="x"/> {"link":"https://acme.slack.com/archives/C1/p9"}')
    outcome = map_events_to_outcome(invented, "C1", TEXT)
    assert outcome.state == FAILED and outcome.link is None


def test_a_link_only_in_the_models_text_is_not_trusted() -> None:
    lines = [tool_use(), tool_result("ok"), final("https://acme.slack.com/archives/C1/p9")]
    assert map_events_to_outcome("\n".join(lines), "C1", TEXT).link is None


def test_another_tool_is_not_a_send() -> None:
    assert map_events_to_outcome(tool_use(name="mcp__other"), "C1", TEXT).state == FAILED


def test_a_tool_error_allows_retry() -> None:
    outcome = map_events_to_outcome("\n".join([tool_use(), tool_result("denied", True)]), "C1", TEXT)
    assert outcome.state == FAILED and "error" in str(outcome.detail)


def test_a_changed_text_or_channel_is_said_on_the_row() -> None:
    lines = [tool_use(channel="C2"), tool_result("https://acme.slack.com/archives/C2/p1")]
    assert "differs" in str(map_events_to_outcome("\n".join(lines), "C1", TEXT).detail)


def test_the_send_allows_only_the_send_tool_with_hooks_off_and_user_settings() -> None:
    command = build_send_command(LIVE, "C1", TEXT)
    assert command[command.index("--allowedTools") + 1] == SEND_TOOL
    assert command[command.index("--tools") + 1] == ""
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    assert command[command.index("--setting-sources") + 1] == "user"
    assert "disableAllHooks" in command[command.index("--settings") + 1]
    assert json.dumps({"channel_id": "C1", "message": TEXT}, ensure_ascii=False) in command[2]


def test_the_send_env_has_a_user_and_no_claude_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("LOGNAME", raising=False)
    monkeypatch.setenv("CLAUDECODE", "1")
    env = build_send_env()
    assert env["USER"] and env["LOGNAME"] and "CLAUDECODE" not in env


def run_result(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout, "")


def test_a_connector_not_ready_is_tried_again_since_nothing_was_posted() -> None:
    results = [run_result(final("not ready")), run_result("\n".join([tool_use(), tool_result("ok")]))]
    with mock.patch("subprocess.run", side_effect=results) as run, mock.patch("time.sleep"):
        outcome = send_to_slack(LIVE, TEXT)
    assert outcome.state == POSTED_NO_LINK and run.call_count == 2


def test_a_tool_error_is_not_retried_on_its_own() -> None:
    results = [run_result("\n".join([tool_use(), tool_result("denied", True)]))]
    with mock.patch("subprocess.run", side_effect=results) as run:
        assert send_to_slack(LIVE, TEXT).state == FAILED
    assert run.call_count == 1


def test_a_timeout_after_the_call_counts_as_sent() -> None:
    timeout = subprocess.TimeoutExpired([], 1, output=tool_use().encode())
    with mock.patch("subprocess.run", side_effect=timeout):
        assert send_to_slack(LIVE, TEXT).state == POSTED_NO_LINK


def test_no_channel_sends_nothing() -> None:
    with mock.patch("subprocess.run") as run:
        assert send_to_slack(Config(review_post_dry_run=False), TEXT).state == FAILED
    run.assert_not_called()


def publish(conn: sqlite3.Connection, now: int = 1000) -> tuple[list[str], list[Any]]:
    with transaction(conn):
        record_publish(conn, REF, "sam", now)
        return advance_posts(conn, CONFIG, now)


def post_state(conn: sqlite3.Connection) -> str | None:
    return conn.execute("SELECT post_state FROM review_prs").fetchone()[0]


def add_sentence(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        record_sentence(conn, CONFIG, REF, "Part of the export project.", "alex", 900)


def claimed(tmp_path: Path) -> sqlite3.Connection:
    conn = setup_pr(tmp_path)
    add_sentence(conn)
    publish(conn)
    return conn


def test_publish_records_who_and_when(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    add_sentence(conn)
    publish(conn)
    row = conn.execute("SELECT reviewed_at, reviewed_by, reviewed_oid FROM review_prs").fetchone()
    assert tuple(row) == (1000, "sam", "a1")
    view: Any = map_db_to_reviews(conn, CONFIG, 0)
    assert (view["prs"][0]["state"], view["prs"][0]["can_publish"]) == ("published", False)


def group_of(conn: sqlite3.Connection) -> tuple[str, bool]:
    view: Any = map_db_to_reviews(conn, CONFIG, 0)
    return str(view["prs"][0]["group"]), bool(view["prs"][0]["can_publish"])


def test_a_draft_never_waits_for_the_owner_even_with_an_old_ready_mark_and_joins_once_ready_on_github(
    tmp_path: Path,
) -> None:
    conn = setup_pr(tmp_path, draft=True)
    assert group_of(conn) == ("drafts", False)
    with transaction(conn):
        conn.execute("UPDATE review_prs SET ready_at = 1000, ready_by = 'alex'")
    assert group_of(conn) == ("drafts", False)
    with transaction(conn):
        store_open_prs(conn, [make_pr(draft=False)], 1100)
    assert group_of(conn) == ("waiting", True)


def test_each_draft_spell_asks_its_owner_once_to_mark_it_ready_on_github(tmp_path: Path) -> None:
    from bridge.review_post import ask_to_mark_drafts_ready

    conn = setup_pr(tmp_path, draft=True)
    with transaction(conn):
        assert ask_to_mark_drafts_ready(conn, CONFIG, 1000) == []
        add_session(conn, "c1", "worker", branch="dev/x")
        assert len(ask_to_mark_drafts_ready(conn, CONFIG, 1000)) == 1
        assert ask_to_mark_drafts_ready(conn, CONFIG, 2000) == []
    body = conn.execute("SELECT body FROM messages").fetchone()[0]
    assert "gh pr ready 7 --repo Org/app" in body and "say why" in body
    with transaction(conn):
        store_open_prs(conn, [make_pr(draft=False)], 3000)
        assert ask_to_mark_drafts_ready(conn, CONFIG, 3000) == []
        store_open_prs(conn, [make_pr(draft=True)], 4000)
        assert len(ask_to_mark_drafts_ready(conn, CONFIG, 4000)) == 1


def test_publish_on_a_draft_marks_it_ready_and_posts(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path, draft=True)
    add_sentence(conn)
    assert publish(conn)[0] == [REF] and post_state(conn) == SENDING
    assert tuple(conn.execute("SELECT ready_at, ready_by FROM review_prs").fetchone()) == (1000, "sam")
    with mock.patch.dict("os.environ", {"BRIDGE_STATE_DIR": str(tmp_path)}):
        outcome = run_review_post(conn, CONFIG, REF)
    assert outcome is not None and outcome.state == DRY_RUN


def told_text(tmp_path: Path, name: str, published: str, direct: bool = False, **changes: Any) -> str:
    """The owner's message for a PR in one of the states a post ends in."""
    (tmp_path / name).mkdir()
    conn = setup_pr(tmp_path / name, **changes)
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
        if published == "by hand":
            record_published_elsewhere(conn, REF, "https://acme.slack.com/archives/C/p1", 1000)
        else:
            record_publish(conn, REF, "sam", 1000, direct=direct)
            link = "https://acme.slack.com/archives/C/p1" if published == POSTED else None
            conn.execute("UPDATE review_prs SET post_state = ?, post_link = ?", (published, link))
        assert len(advance_posts(conn, CONFIG, 2000)[1]) == 1
    return str(conn.execute("SELECT body FROM messages").fetchone()[0])


def test_the_lead_hears_that_the_owners_click_is_permission_to_act_right_away(tmp_path: Path) -> None:
    text = told_text(tmp_path, "live", POSTED, draft=True)
    assert "The owner pressed Publish" in text and "https://acme.slack.com/archives/C/p1" in text
    assert "The owner's click is the permission to mark it ready on GitHub and request its reviewers" in text
    assert "do not stop to ask again" in text and "right away, because the post already links there" in text


def test_dry_run_wording_claims_no_post(tmp_path: Path) -> None:
    text = told_text(tmp_path, "dry", DRY_RUN, draft=True)
    assert "Nothing was posted" in text and "mark it ready on GitHub and request its reviewers" in text
    assert "The owner's click is the permission" in text and "right away" not in text
    assert "the Slack post is out" not in text and "already links" not in text and "slack.com" not in text


def test_steps_follow_what_the_pr_still_needs(tmp_path: Path) -> None:
    undrafted = told_text(tmp_path, "open", POSTED)
    assert "permission to request its reviewers" in undrafted and "mark it ready" not in undrafted
    requested = told_text(tmp_path, "asked", POSTED, draft=True, requested=1)
    assert "permission to mark it ready on GitHub," in requested and "request its reviewers" not in requested


def test_a_direct_post_asks_for_no_reviewers_and_says_none_are_needed(tmp_path: Path) -> None:
    for name, published, changes in (("open", POSTED, {}), ("draft", POSTED, {"draft": True}), ("dry", DRY_RUN, {})):
        text = told_text(tmp_path, name, published, direct=True, **changes)
        assert "request its reviewers" not in text, name
        assert "The owner sent it direct: it needs no requested reviewers, so request none and do not ask." in text, (
            name
        )
        assert "The owner's click is the permission to" not in text or "mark it ready on GitHub," in text, name
    assert "permission to mark it ready on GitHub, so do not stop" in told_text(
        tmp_path, "draft2", POSTED, direct=True, draft=True
    )


def test_a_post_by_hand_claims_no_click_of_owner(tmp_path: Path) -> None:
    text = told_text(tmp_path, "hand", "by hand")
    assert "request its reviewers" in text and "slack.com" in text and "already links there" in text
    assert "permission" not in text and "pressed Publish" not in text


def test_the_owner_can_publish_a_pr_that_already_has_reviewers(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path, requested=1, approved_by=("ann",))
    add_sentence(conn)
    assert publish(conn)[0] == [REF]


def test_a_post_recorded_by_hand_is_never_posted(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        record_published_elsewhere(conn, REF, "https://acme.slack.com/archives/C/p1", 1000)
    with pytest.raises(ReviewError), transaction(conn):
        record_publish(conn, REF, "sam", 1000)
    view: Any = map_db_to_reviews(conn, CONFIG, 0)
    assert view["prs"][0]["state"] == "published" and view["prs"][0]["post_link"].endswith("p1")


def test_with_no_sentence_it_stands_and_the_owner_is_asked_once(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
    to_send, told = publish(conn)
    assert to_send == [] and post_state(conn) == WAITING_SENTENCE
    assert [r.handle for _, r in told] == ["worker"]
    assert "guild review sentence Org/app#7" in conn.execute("SELECT body FROM messages").fetchone()[0]
    with transaction(conn):
        assert advance_posts(conn, CONFIG, 2000) == ([], [])
    add_sentence(conn)
    with transaction(conn):
        assert advance_posts(conn, CONFIG, 3000)[0] == [REF]


def test_the_ask_waits_for_an_owner(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    assert publish(conn)[1] == []
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
        assert len(advance_posts(conn, CONFIG, 2000)[1]) == 1


def test_a_stacked_pr_waits_until_it_is_first_unmerged(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path, stack_parent="pr:Org/app#6")
    add_sentence(conn)
    assert publish(conn)[0] == [] and post_state(conn) == WAITING_STACK
    with transaction(conn):
        store_open_prs(conn, [make_pr(stack_parent=None)], 1500)
        assert advance_posts(conn, CONFIG, 2000)[0] == [REF]


def test_a_pr_turned_draft_after_publish_holds_its_post(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    add_sentence(conn)
    with transaction(conn):
        record_publish(conn, REF, "sam", 1000)
        store_open_prs(conn, [make_pr(draft=True)], 1500)
        assert advance_posts(conn, CONFIG, 2000)[0] == []
    assert post_state(conn) == WAITING_DRAFT


def test_claiming_happens_once(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    add_sentence(conn)
    assert publish(conn)[0] == [REF] and post_state(conn) == SENDING
    with transaction(conn):
        assert advance_posts(conn, CONFIG, 1001)[0] == []


def test_a_later_call_that_went_through_counts_as_sent() -> None:
    second = {"type": "tool_use", "id": "t2", "name": SEND_TOOL, "input": {"channel_id": "C1", "message": TEXT}}
    lines = [
        tool_use(),
        tool_result("boom", True),
        json.dumps({"type": "assistant", "message": {"content": [second]}}),
        json.dumps(
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "content": "ok"}]}}
        ),
    ]
    assert map_events_to_outcome("\n".join(lines), "C1", TEXT).state == POSTED_NO_LINK


def test_a_sent_post_with_no_link_cannot_be_reopened(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        conn.execute("UPDATE review_prs SET post_state = ?", (POSTED_NO_LINK,))
    with pytest.raises(ReviewError), transaction(conn):
        reopen_unsent_post(conn, REF)
    with pytest.raises(ReviewError), transaction(conn):
        retry_failed_post(conn, REF)


def test_the_sender_rechecks_what_changed_since_the_claim(tmp_path: Path) -> None:
    conn = claimed(tmp_path)
    with transaction(conn):
        store_open_prs(conn, [make_pr(stack_parent="pr:Org/app#6")], 1500)
    with mock.patch("bridge.review_post.send_to_slack") as send:
        assert run_review_post(conn, LIVE, REF) is None
    send.assert_not_called()
    assert post_state(conn) == WAITING_STACK


def test_the_senders_recheck_does_not_erase_a_post_recorded_by_hand(tmp_path: Path) -> None:
    conn = claimed(tmp_path)
    with transaction(conn):
        store_open_prs(conn, [make_pr(stack_parent="pr:Org/app#6")], 1500)
    real = conn.execute

    class Interleaved:
        def __getattr__(self, name: str) -> Any:
            return getattr(conn, name)

        def execute(self, sql: str, *args: Any) -> Any:
            result = real(sql, *args)
            if sql.startswith("SELECT * FROM review_prs WHERE ref"):
                row = result.fetchone()
                record_published_elsewhere(conn, REF, "https://acme.slack.com/archives/C/p1", 1600)
                return mock.Mock(fetchone=lambda: row)
            return result

    with mock.patch("bridge.review_post.transaction", side_effect=transaction):
        run_review_post(Interleaved(), LIVE, REF)  # pyright: ignore[reportArgumentType]
    assert post_state(conn) == EXTERNAL


def test_the_page_can_send_for_real_what_a_dry_run_logged_only_once_sending_is_on(tmp_path: Path) -> None:
    conn = claimed(tmp_path)
    with transaction(conn):
        conn.execute("UPDATE review_prs SET post_state = ?", (DRY_RUN,))
    on: Any = map_db_to_reviews(conn, Config(review_post_dry_run=False), 0)["prs"]
    off: Any = map_db_to_reviews(conn, Config(review_post_dry_run=True), 0)["prs"]
    assert (on[0]["can_repost"], off[0]["can_repost"]) == (True, False)


def test_a_sender_back_from_a_stop_finds_a_newer_claim_and_does_nothing(tmp_path: Path) -> None:
    conn = claimed(tmp_path)
    with mock.patch("bridge.review_post.send_to_slack") as send:
        assert run_review_post(conn, LIVE, REF, claim=1) is None
    send.assert_not_called()
    assert post_state(conn) == SENDING


def test_a_stale_sender_stops_before_its_next_attempt() -> None:
    first = subprocess.CompletedProcess([], 0, final("not ready"), "")
    with mock.patch("subprocess.run", side_effect=[first]) as run, mock.patch("time.sleep"):
        send_to_slack(LIVE, TEXT, lambda: False)
    assert run.call_count == 1


def test_a_dry_run_post_can_go_for_real_once_sending_is_on(tmp_path: Path) -> None:
    conn = claimed(tmp_path)
    with transaction(conn):
        conn.execute("UPDATE review_prs SET post_state = ?", (DRY_RUN,))
        reopen_unsent_post(conn, REF)
        assert advance_posts(conn, CONFIG, 5000)[0] == [REF]


def test_a_post_recorded_by_hand_also_tells_the_owner_to_request_reviewers(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
        record_published_elsewhere(conn, REF, "https://acme.slack.com/archives/C/p1", 1000)
        told = advance_posts(conn, CONFIG, 2000)[1]
        assert len(told) == 1
        assert advance_posts(conn, CONFIG, 3000)[1] == []
    assert "request its reviewers" in conn.execute("SELECT body FROM messages").fetchone()[0]


def test_no_reviewers_are_asked_for_once_someone_reviewed(tmp_path: Path) -> None:
    """GitHub drops a review request once that reviewer comments, and they may have asked to hold."""
    conn = setup_pr(tmp_path, requested=0, others_reviewed=True)
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
        record_published_elsewhere(conn, REF, "https://acme.slack.com/archives/C/p1", 1000)
        assert advance_posts(conn, CONFIG, 2000)[1] == []
        store_open_prs(conn, [make_pr(requested=0, others_reviewed=False)], 3000)
        assert advance_posts(conn, CONFIG, 4000)[1] == []
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_a_reviewed_draft_is_told_to_mark_ready_only(tmp_path: Path) -> None:
    text = told_text(tmp_path, "draft", POSTED, draft=True, others_reviewed=True)
    assert "permission to mark it ready on GitHub," in text and "request its reviewers" not in text


def test_a_post_on_a_row_not_yet_polled_since_the_upgrade_waits_for_the_poll(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        conn.execute("UPDATE review_prs SET others_reviewed = NULL")
        add_session(conn, "c1", "worker", branch="dev/x")
        record_published_elsewhere(conn, REF, "https://acme.slack.com/archives/C/p1", 1000)
        assert advance_posts(conn, CONFIG, 2000)[1] == []
        store_open_prs(conn, [make_pr()], 3000)
        assert len(advance_posts(conn, CONFIG, 4000)[1]) == 1


def test_a_sender_that_stopped_offers_no_retry(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    add_sentence(conn)
    publish(conn)
    with transaction(conn):
        advance_posts(conn, CONFIG, 1000 + 700_000)
    assert post_state(conn) == INTERRUPTED
    assert map_db_to_reviews(conn, CONFIG, 0)["prs"][0]["can_retry"] is False  # type: ignore[index]
    with pytest.raises(ReviewError), transaction(conn):
        retry_failed_post(conn, REF)
    with transaction(conn):
        reopen_unsent_post(conn, REF)
    assert post_state(conn) is None


def test_dry_run_writes_the_log_sends_nothing_and_tells_the_owner(tmp_path: Path, bridge_env: BridgeEnv) -> None:
    conn = claimed(tmp_path)
    with mock.patch.dict("os.environ", {"BRIDGE_STATE_DIR": str(tmp_path)}), mock.patch("subprocess.run") as run:
        outcome = run_review_post(conn, CONFIG, REF)
    run.assert_not_called()
    assert outcome is not None and outcome.state == DRY_RUN and post_state(conn) == DRY_RUN
    logged = json.loads((tmp_path / "review-posts.log").read_text())
    assert logged["text"] == TEXT
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
        told = advance_posts(conn, CONFIG, 2000)[1]
    assert len(told) == 1
    body = conn.execute("SELECT body FROM messages").fetchone()[0]
    assert "request its reviewers" in body and "dry run" in body
    with transaction(conn):
        assert advance_posts(conn, CONFIG, 3000)[1] == []


def test_dry_run_is_the_default() -> None:
    assert Config().review_post_dry_run is True


def test_the_sender_ignores_a_ref_not_claimed_for_it(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    assert run_review_post(conn, LIVE, REF) is None


def test_a_real_send_records_the_link_and_a_failure_can_retry(tmp_path: Path) -> None:
    conn = claimed(tmp_path)
    with (
        mock.patch.dict("os.environ", {"BRIDGE_STATE_DIR": str(tmp_path)}),
        mock.patch(
            "bridge.review_post.send_to_slack",
            return_value=map_events_to_outcome(tool_use(), "C1", TEXT).__class__(FAILED, None, "the tool failed"),
        ),
    ):
        run_review_post(conn, LIVE, REF)
    view: Any = map_db_to_reviews(conn, CONFIG, 0)
    assert (view["prs"][0]["can_retry"], view["prs"][0]["post_detail"]) == (True, "the tool failed")
    with transaction(conn):
        retry_failed_post(conn, REF)
        assert advance_posts(conn, CONFIG, 5000)[0] == [REF]


def test_a_sentence_naming_anything_of_ours_is_refused_with_the_list(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_lead(conn, "Boris", "s-boris")
        conn.execute("UPDATE leads SET retired_at = 5")
        for sentence, named in (
            ("Boris's export project.", "Boris"),
            ("Part of the Guild page.", "Guild"),
            ("Two workers asked for it.", "worker"),
            ("What aide asked for.", "Aide"),
        ):
            with pytest.raises(ReviewError, match=f"not {named} ") as refused:
                record_sentence(conn, CONFIG, REF, sentence, "alex", 900)
            assert "Boris, Aide, worker, lead, Guild, bridge" in str(refused.value)
        record_sentence(conn, CONFIG, REF, "Part of the bridgehead migration.", "alex", 900)


def test_a_title_is_no_sentence(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path, title="Fix export")
    with pytest.raises(ReviewError), transaction(conn):
        record_sentence(conn, CONFIG, REF, "Fix export.", "a", 1)
    with pytest.raises(ReviewError), transaction(conn):
        record_sentence(conn, CONFIG, REF, "  ", "a", 1)


def test_a_pr_can_be_named_several_ways(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    for text in ("7", "#7", "app#7", "Org/app#7", "https://github.com/Org/app/pull/7"):
        assert map_arg_to_ref(conn, text) == REF
    with pytest.raises(ReviewError):
        map_arg_to_ref(conn, "8")
    with pytest.raises(ReviewError):
        map_arg_to_ref(conn, "other#7")


def test_guild_commands_and_the_publish_route(bridge_env: BridgeEnv, dashboard: str) -> None:
    conn = bridge_env.connect()
    with transaction(conn):
        store_open_prs(conn, [make_pr()], 500)
    http_request(f"{dashboard}/api/reviews/Org/app/7/retry", post=True, expect=409)
    http_request(f"{dashboard}/api/reviews/Org/app/7/repost", post=True, expect=409)
    http_request(f"{dashboard}/api/reviews/Org/app/8/publish", post=True, expect=409)
    http_request(f"{dashboard}/api/reviews/Org/app/7/publish", expect=404)
    http_request(f"{dashboard}/api/reviews/Org/app/7/publish", post=True)
    guild = json.loads(http_request(dashboard + "/api/guild"))
    assert guild["reviews"]["prs"][0]["state"] == "published"
    assert guild["reviews"]["prs"][0]["post_state"] == WAITING_SENTENCE
    assert guild["reviews"]["dry_run"] is True


def test_mention_people_come_from_config_in_order_and_need_slack_ids() -> None:
    from bridge.config import map_toml_to_config

    config = map_toml_to_config({"review_mention_people": {"Kai": "U01AAAA0001", "Nina": "U01BBBB0002"}})
    assert config.review_mention_people == (("Kai", "U01AAAA0001"), ("Nina", "U01BBBB0002"))
    with pytest.raises(ValueError, match="Slack user id"):
        map_toml_to_config({"review_channel_id": "C1", "review_mention_people": {"Kai": "@kai"}})


def test_the_publish_form_sends_direct_and_names() -> None:
    from bridge.serve import map_json_to_publish_choice

    assert map_json_to_publish_choice({}) == (False, (), "")
    assert map_json_to_publish_choice({"direct": True, "mention": ["Kai", "Kai", "Nina"], "comment": " hi "}) == (
        True,
        ("Kai", "Nina"),
        " hi ",
    )
    assert map_json_to_publish_choice({"direct": "yes"}) is None
    assert map_json_to_publish_choice({"mention": "Kai"}) is None
    assert map_json_to_publish_choice({"comment": 5}) is None


def test_the_publish_route_keeps_a_comment_only_with_someone_mentioned_and_caps_it(bridge_env: BridgeEnv) -> None:
    config = bridge_env.root / "config.toml"
    config.write_text(config.read_text() + '[review_mention_people]\nKai = "U01AAAA0001"\n')
    conn = bridge_env.connect()
    with transaction(conn):
        store_open_prs(conn, [make_pr(), make_pr(ref="pr:Org/app#8", number=8)], 500)
    port = free_port()
    server = subprocess.Popen([str(VENV_BIN / "bridge"), "serve", "--port", str(port)], env=bridge_env.env)
    try:
        base = f"http://127.0.0.1:{port}/api/reviews/Org/app"
        wait_up(f"http://127.0.0.1:{port}")
        too_long: dict[str, object] = {"mention": ["Kai"], "comment": "x" * (COMMENT_MAX_CHARS + 1)}
        assert "at most 300" in http_request(f"{base}/7/publish", post=True, data=too_long, expect=409)
        http_request(f"{base}/7/publish", post=True, data={"mention": ["Kai"], "comment": " look, @Kai \n"})
        http_request(f"{base}/8/publish", post=True, data={"direct": True, "comment": "x" * 999})
    finally:
        server.terminate()
        server.wait()
    stored = dict(conn.execute("SELECT number, post_comment FROM review_prs").fetchall())
    assert stored == {7: "look, <@U01AAAA0001>", 8: None}


def test_a_closed_pr_asks_its_owner_once_to_clean_up_and_says_why_owner_worktree_was_kept(tmp_path: Path) -> None:
    from bridge.review_post import ask_to_clean_up

    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
        conn.execute("UPDATE review_prs SET closed_at = 1000, wt_path = '/wt/review-app-7', wt_state = 'preparing'")
        assert ask_to_clean_up(conn, CONFIG, 2000) == []
        conn.execute("UPDATE review_prs SET wt_state = 'failed', wt_at = 900, wt_detail = 'Python env: uv broke'")
        assert ask_to_clean_up(conn, CONFIG, 2000) == []
        conn.execute(
            "UPDATE review_prs SET wt_state = 'dirty', wt_at = 1500, wt_detail = 'PR closed; kept because it has x.txt'"
        )
        assert len(ask_to_clean_up(conn, CONFIG, 2000)) == 1
        assert ask_to_clean_up(conn, CONFIG, 3000) == []
    body = conn.execute("SELECT body FROM messages").fetchone()[0]
    assert "git worktree remove" in body and "never rm -rf" in body and "leave alone any with uncommitted work" in body
    assert "/wt/review-app-7 was kept: PR closed; kept because it has x.txt" in body


def test_a_pr_closed_long_ago_asks_nobody_to_clean_up(tmp_path: Path) -> None:
    from bridge.review_post import ask_to_clean_up

    conn = setup_pr(tmp_path)
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
        conn.execute("UPDATE review_prs SET closed_at = 1000")
        assert ask_to_clean_up(conn, CONFIG, 1000 + 8 * 24 * 3_600_000) == []


def test_post_for_real_after_a_dry_run_takes_what_the_owner_picks_then(tmp_path: Path) -> None:
    from bridge.review_post import record_post_choice

    conn = claimed(tmp_path)
    with mock.patch.dict("os.environ", {"BRIDGE_STATE_DIR": str(tmp_path)}):
        run_review_post(conn, CONFIG, REF)
        with transaction(conn):
            reopen_unsent_post(conn, REF)
            record_post_choice(conn, REF, True, ("U01CCCC0003",))
            assert advance_posts(conn, CONFIG, 5000)[0] == [REF]
        run_review_post(conn, CONFIG, REF)
    logged = [json.loads(line) for line in (tmp_path / "review-posts.log").read_text().splitlines()]
    assert logged[-1]["text"] == f"{TEXT} - direct <@U01CCCC0003>"


TUESDAY_10 = 1_793_700_000_000
"""2026-11-03 10:00 UTC, the zone the tests' CONFIG works in."""


def test_working_time_is_the_configured_days_nine_to_seven_in_the_configured_zone() -> None:
    from bridge.review_post import is_working_time

    hour = 3_600_000
    assert is_working_time(TUESDAY_10, CONFIG)
    assert not is_working_time(TUESDAY_10 - 2 * hour, CONFIG)
    assert is_working_time(TUESDAY_10 - hour, CONFIG)
    assert not is_working_time(TUESDAY_10 + 9 * hour, CONFIG)
    assert is_working_time(TUESDAY_10 + 9 * hour - 1, CONFIG)
    assert is_working_time(TUESDAY_10 + 3 * 24 * hour, CONFIG)
    assert not is_working_time(TUESDAY_10 + 4 * 24 * hour, CONFIG)
    assert is_working_time(TUESDAY_10 + 6 * 24 * hour, CONFIG)
    week = Config(work_timezone="Etc/GMT-9", work_days=(1, 2))
    assert not is_working_time(TUESDAY_10, week)
    assert is_working_time(TUESDAY_10 - 8 * hour, week)
    assert not is_working_time(TUESDAY_10 + 3 * 24 * hour - 8 * hour, week)


def test_a_conflicting_pr_asks_its_lead_once_per_head_every_three_hours_of_work(tmp_path: Path) -> None:
    from bridge.review_post import ask_to_resolve_conflicts

    conn = setup_pr(tmp_path, merge_state="DIRTY")
    hour = 3_600_000
    with transaction(conn):
        add_lead(conn, "Alex", "s-alex")
        add_session(conn, "c-lead", "alex", surface="s-alex", session="tab-alex")
        add_session(conn, "c1", "worker", branch="dev/x", session="tab-worker")
        conn.execute("INSERT INTO tab_parents (agterm_session, parent_session) VALUES ('tab-worker', 'tab-alex')")
        sent = ask_to_resolve_conflicts(conn, CONFIG, TUESDAY_10)
    assert [r.handle for _, r in sent] == ["alex"]
    body = conn.execute("SELECT body FROM messages").fetchone()[0]
    assert "has merge conflicts with main" in body and "Merge main into dev/x" in body
    assert "never a rebase or force push" in body and "change no meaning without saying so" in body
    assert "let CI run again" in body and "--type report" in body
    views: Any = map_db_to_reviews(conn, CONFIG, TUESDAY_10)["prs"]
    assert views[0]["conflict_asked_at"] == TUESDAY_10
    with transaction(conn):
        assert ask_to_resolve_conflicts(conn, CONFIG, TUESDAY_10 + 3 * hour) == []
        store_open_prs(conn, [make_pr(head_oid="b2", merge_state="DIRTY")], TUESDAY_10)
        assert ask_to_resolve_conflicts(conn, CONFIG, TUESDAY_10 + 4 * hour) == []
        assert len(ask_to_resolve_conflicts(conn, CONFIG, TUESDAY_10 + 6 * hour)) == 1
        store_open_prs(conn, [make_pr(head_oid="c3", merge_state="DIRTY")], TUESDAY_10)
        assert ask_to_resolve_conflicts(conn, CONFIG, TUESDAY_10 + 4 * 24 * hour) == []


def test_behind_asks_too_and_drafts_and_clean_prs_do_not(tmp_path: Path) -> None:
    from bridge.review_post import ask_to_resolve_conflicts

    conn = setup_pr(tmp_path, merge_state="BEHIND")
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
        assert len(ask_to_resolve_conflicts(conn, CONFIG, TUESDAY_10)) == 1
    assert "is behind main" in conn.execute("SELECT body FROM messages").fetchone()[0]
    for changes in (
        {"merge_state": "CLEAN", "head_oid": "z9"},
        {"merge_state": "DIRTY", "draft": True, "head_oid": "z9"},
    ):
        with transaction(conn):
            store_open_prs(conn, [make_pr(**changes)], TUESDAY_10)
            conn.execute("UPDATE review_poll SET conflicts_checked_at = NULL")
            assert ask_to_resolve_conflicts(conn, CONFIG, TUESDAY_10) == []


def test_publishing_neither_frees_nor_blocks_the_merge(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    with transaction(conn):
        record_publish(conn, REF, "theo", 1000)
    assert conn.execute("SELECT merge_path FROM review_prs").fetchone()[0] is None
    other = setup_pr(tmp_path / "other") if (tmp_path / "other").mkdir() is None else None
    assert other is not None
    with transaction(other):
        other.execute("""UPDATE review_prs SET approved_by = '["mira"]'""")
        record_published_elsewhere(other, REF, "https://x/post", 1000)
    assert other.execute("SELECT merge_path FROM review_prs").fetchone()[0] == "ready"
