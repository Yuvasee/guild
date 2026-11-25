import json
import shutil
import sqlite3
import subprocess
import time
from collections.abc import Generator
from pathlib import Path
from typing import cast

import pytest
from conftest import BridgeEnv, SpawnProcess, http_request, wait_for
from test_asks import EVIDENCE, HOW, LEAD, WORKER, write_assistant
from test_feed import prompt, start
from test_leads import Team
from test_reviews import add_session

from bridge.asks import (
    AskError,
    add_ask,
    claim_ask_done,
    mark_ask_told,
    post_verified_batch,
    reopen_ask,
    start_ask,
    verify_ask,
    wait_ask,
)
from bridge.board import BODY_MAX_CHARS, Party
from bridge.config import Config
from bridge.db import connect
from bridge.guildview import ANSWERS_MAX, map_db_to_guild

VERIFIER = Party("conv-l", "alex")
DAY_MS = 86_400_000


@pytest.fixture
def conn(tmp_path: Path) -> Generator[sqlite3.Connection]:
    c = connect(tmp_path / "bridge.db")
    yield c
    c.close()


def answers(base: str) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], json.loads(http_request(base + "/api/guild"))["answers"])


def page_post(base: str, path: str, data: dict[str, object] | None = None) -> dict[str, object]:
    return json.loads(http_request(base + path, post=True, data=data))


def verified(conn: sqlite3.Connection, words: str, question: bool = False, wait_on_owner: bool = False) -> int:
    ask = add_ask(conn, "alex", "aide", words, "tab", ["https://x.example/pr/1"], 1000, question)
    start_ask(conn, ask.id, WORKER, 2000)
    if wait_on_owner:
        wait_ask(conn, ask.id, WORKER, "owner", "which copy to show", 2500)
    claim_ask_done(conn, ask.id, WORKER, "Deployed PR 1 to the status page", 3000)
    verify_ask(conn, ask.id, LEAD, EVIDENCE, HOW, 4000)
    return ask.id


def test_a_batch_summary_lists_the_untold_verified_asks_once_and_skips_posted_ones(conn: sqlite3.Connection) -> None:
    first = verified(conn, "Make the status page show the new copy")
    second = verified(conn, "Rename the export button\nto Download")
    posted = verified(conn, "Why does the export stall?", question=True)
    mark_ask_told(conn, posted, 5000)
    add_session(conn, "conv-aide", "aide")

    message_id, listed, left = post_verified_batch(conn, Config(), VERIFIER, 6000)

    body = conn.execute("SELECT body FROM messages WHERE id = ?", (message_id,)).fetchone()["body"]
    assert (listed, left) == (2, 0)
    assert body == (
        "2 asks verified\n"
        f"- ask #{first}: Make the status page show the new copy https://x.example/pr/1\n"
        f"- ask #{second}: Rename the export button to Download https://x.example/pr/1"
    )
    assert conn.execute("SELECT count(*) FROM user_items WHERE text = '2 asks verified'").fetchone()[0] == 0
    with pytest.raises(AskError, match="no ask you verified"):
        post_verified_batch(conn, Config(), VERIFIER, 7000)


def test_a_batch_too_long_for_one_post_leaves_the_rest_for_the_next(conn: sqlite3.Connection) -> None:
    for n in range(30):
        verified(conn, f"Ask number {n} " + "with many words " * 10)
    add_session(conn, "conv-aide", "aide")

    _, listed, left = post_verified_batch(conn, Config(), VERIFIER, 6000)
    _, listed_next, left_next = post_verified_batch(conn, Config(), VERIFIER, 7000)

    assert listed + left == 30 and left > 0
    assert (listed_next, left_next) == (left, 0)


def test_asks_verified_before_the_change_count_as_told(tmp_path: Path) -> None:
    from bridge.db import MIGRATIONS

    raw = sqlite3.connect(tmp_path / "old.db")
    raw.row_factory = sqlite3.Row
    for migration in MIGRATIONS[:-1]:
        raw.executescript(migration)
    raw.execute(f"PRAGMA user_version = {len(MIGRATIONS) - 1}")
    raw.execute(
        "INSERT INTO asks (owner, recorded_by, words, source, state, created_at, updated_at, verified_at) "
        "VALUES ('alex', 'aide', 'old words', 'tab', 'verified', 1, 2, 3)"
    )
    raw.commit()
    raw.close()

    c = connect(tmp_path / "old.db")
    assert c.execute("SELECT told_at FROM asks").fetchone()[0] == 3
    with pytest.raises(AskError, match="no ask you verified"):
        post_verified_batch(c, Config(), Party("conv-l", "alex"), 9)
    c.close()


def test_a_result_post_shows_in_answers_until_ok_and_a_reply_reaches_the_assistant(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    pids = start(bridge_env, spawn_process, agterm_tabs=False)
    assert bridge_env.cli("handle", "aide", pid=pids["b"]).returncode == 0, "the page's config names aide the assistant"
    with bridge_env.connect() as db:
        db.execute(
            "INSERT INTO asks (owner, recorded_by, words, source, state, created_at, updated_at) "
            "VALUES ('a', 'a', 'Is the nightly export fixed?', 'tab', 'open', 1, 1)"
        )
    first = bridge_env.cli("post", "insight", "Older news", pid=pids["a"])
    posted = bridge_env.cli(
        "post", "done", "Export fixed", "--body", "line\n" * 9, "--link", "Report=https://x.example/r/1",
        "--ask", "1", "--pin", pid=pids["a"],
    )  # fmt: skip
    assert first.returncode == 0 and posted.returncode == 0, posted.stderr
    assert "pinned as #1" in posted.stdout

    newest, older = answers(dashboard)
    assert (newest["text"], older["text"]) == ("Export fixed", "Older news")
    assert newest["ask"] == {"id": 1, "words": "Is the nightly export fixed?"}
    assert (newest["ref"], newest["unread"], newest["can_reply"]) == ("A#2", True, True)
    assert newest["links"] == [{"label": "Report", "url": "https://x.example/r/1"}]
    with bridge_env.connect() as db:
        assert db.execute("SELECT told_at IS NOT NULL FROM asks").fetchone()[0] == 1

    assert page_post(dashboard, "/api/items/2/open") == {"ok": True}
    assert page_post(dashboard, "/api/items/2/reply", {"text": "Thanks, also check the weekly one"})["ok"]
    context = prompt(bridge_env, pids["b"], "conv-b", "next")
    assert 'Reply on a\'s done post A#2 "Export fixed": Thanks, also check the weekly one' in context
    assert [a["text"] for a in answers(dashboard)] == ["Export fixed", "Older news"], "a reply leaves it there"
    assert answers(dashboard)[0]["unread"] is False

    assert page_post(dashboard, "/api/items/2/resolve") == {"ok": True}
    assert [a["text"] for a in answers(dashboard)] == ["Older news"]
    pins = json.loads(bridge_env.cli("pins", "--json").stdout)
    assert [(p["title"], p["body"], p["source"]) for p in pins] == [("Export fixed", "https://x.example/r/1", "ask #1")]


def test_pin_and_ask_are_for_results_and_a_link_is_pinned_once(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    pids = start(bridge_env, spawn_process, agterm_tabs=False)
    on_question = bridge_env.cli("post", "decision", "Ship?", "--ask", "1", pid=pids["a"])
    no_link = bridge_env.cli("post", "done", "Shipped", "--pin", pid=pids["a"])
    no_ask = bridge_env.cli("post", "done", "Shipped", "--ask", "9", pid=pids["a"])
    assert on_question.returncode == 2 and "for done and insight posts" in on_question.stderr
    assert no_link.returncode == 2 and "give one" in no_link.stderr
    assert no_ask.returncode == 2 and "no ask #9" in no_ask.stderr

    link = "Page=https://claude.example/artifact/abc"
    assert "pinned as #1" in bridge_env.cli("post", "done", "Page", "--link", link, "--pin", pid=pids["a"]).stdout
    again = bridge_env.cli("post", "done", "Page v2", "--link", link, "--pin", pid=pids["a"])
    assert again.returncode == 0 and "already pinned" in again.stdout
    assert len(json.loads(bridge_env.cli("pins", "--json").stdout)) == 1


def test_a_reply_goes_to_the_assistant_even_on_bridges_own_result_and_never_on_a_question(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    pids = start(bridge_env, spawn_process, agterm_tabs=False)
    assert bridge_env.cli("handle", "aide", pid=pids["b"]).returncode == 0, "the page's config names aide the assistant"
    bridge_env.cli("post", "decision", "Ship?", "--option", "Ship", pid=pids["a"])
    with bridge_env.connect() as db:
        db.execute(
            "INSERT INTO user_items (created_at, from_conversation, from_handle, severity, text, source, kind) "
            "VALUES (strftime('%s','now') * 1000, 'system', 'bridge', 'info', 'Merged web#7', 'agent', 'done')"
        )
    [merged] = answers(dashboard)
    assert merged["can_reply"] is True

    refused = json.loads(http_request(dashboard + "/api/items/1/reply", post=True, data={"text": "hi"}, expect=409))
    assert "no done or insight post A#1" in str(refused["detail"])
    assert page_post(dashboard, "/api/items/2/reply", {"text": "which PR was it?"})["ok"]
    context = prompt(bridge_env, pids["b"], "conv-b", "next")
    assert 'Reply on bridge\'s done post A#2 "Merged web#7": which PR was it?' in context


def test_the_assistant_is_reminded_once_of_a_result_it_gave_the_owner_without_a_post(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    write_assistant(bridge_env, "old-b")

    def stop(pid: int, conv: str, reply: str) -> None:
        pane = "PANE-A" if pid == team.a else "PANE-B"
        payload = {"session_id": conv, "cwd": str(bridge_env.root), "last_assistant_message": reply}
        assert bridge_env.hook("Stop", payload, pid, {"AGTERM_PANE_ID": pane}).returncode == 0

    team.prompt(team.b, "conv-b", "is the export fixed?")
    stop(team.b, "conv-b", "Yes, see https://x.example/r/1")
    reminded = team.prompt(team.b, "conv-b", "thanks")
    assert "carried a link and this turn posted nothing to the feed" in reminded
    stop(team.b, "conv-b", "You're welcome.")
    assert "posted nothing" not in team.prompt(team.b, "conv-b", "and the weekly one?")

    bridge_env.cli("post", "done", "Weekly export fixed", "--link", "https://x.example/r/2", pid=team.b)
    stop(team.b, "conv-b", "Fixed too: https://x.example/r/2")
    assert "posted nothing" not in team.prompt(team.b, "conv-b", "ok")

    team.prompt(team.a, "conv-a", "where is the report?")
    stop(team.a, "conv-a", "Here: https://x.example/r/3")
    assert "posted nothing" not in team.prompt(team.a, "conv-a", "ok"), "only the assistant gets it"


def test_a_bridge_typed_prompt_is_not_the_owners(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    write_assistant(bridge_env, "old-b")
    team.prompt(team.b, "conv-b", "[bridge] Peer REQUEST #4 from alex: the link?")
    payload = {"session_id": "conv-b", "cwd": str(bridge_env.root), "last_assistant_message": "https://x.example/r/1"}
    bridge_env.hook("Stop", payload, team.b, {"AGTERM_PANE_ID": "PANE-B"})
    assert "posted nothing" not in team.prompt(team.b, "conv-b", "next")


def test_the_page_puts_answers_beside_questions_and_calls_them_questions() -> None:
    page = (Path(__file__).parent.parent / "src/bridge/web/guild.html").read_text()
    assert '<div id="top-pair">' in page and "<h2>Questions</h2>" in page and "<h2>Answers</h2>" in page
    assert page.index('id="needs"') < page.index('id="answers"') < page.index('id="queue"')
    assert "Needs you" not in page
    assert 'querySelectorAll("main section.section")' in page, "both columns keep their section tabs"


def test_cli_verify_never_posts_to_answers_and_the_summary_goes_to_the_assistant(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    assert team.enroll().returncode == 0
    write_assistant(bridge_env, "old-b")
    for words, extra in (("Why does the export stall?", ("--question",)), ("Rename the export button", ())):
        assert team.guild("ask", "add", "alex", "--words", words, *extra, pid=team.b).returncode == 0
    for ask_id in ("1", "2"):
        team.guild("ask", "start", ask_id, pid=team.a)
        team.guild("ask", "done", ask_id, "--claim", "Deployed PR 1 to the status page", pid=team.a)

    question = team.guild("ask", "verify", "1", "--evidence", EVIDENCE, "--how", HOW, pid=team.b)
    plain = team.guild("ask", "verify", "2", "--evidence", EVIDENCE, "--how", HOW, pid=team.b)
    summary = team.guild("ask", "summary", pid=team.b)
    nothing = team.guild("ask", "summary", pid=team.b)

    assert "goes into your next `guild ask summary`" in question.stdout, question.stderr
    assert "goes into your next `guild ask summary`" in plain.stdout
    assert summary.stdout.startswith("message #") and "lists 2 verified asks" in summary.stdout, summary.stderr
    assert nothing.returncode == 2 and "no ask you verified" in nothing.stderr
    with bridge_env.connect() as db:
        assert db.execute("SELECT count(*) FROM user_items").fetchone()[0] == 0, "not even the owner's question"


def test_a_reopened_ask_is_told_again_once_verified_again(conn: sqlite3.Connection) -> None:
    ask_id = verified(conn, "Make the status page show the new copy")
    add_session(conn, "conv-aide", "aide")
    post_verified_batch(conn, Config(), VERIFIER, 5000)
    reopen_ask(conn, ask_id, "aide", "still the old copy", 6000)
    start_ask(conn, ask_id, WORKER, 7000)
    claim_ask_done(conn, ask_id, WORKER, "Deployed PR 2 to the status page", 8000)
    verify_ask(conn, ask_id, LEAD, EVIDENCE, HOW, 9000)

    assert post_verified_batch(conn, Config(), VERIFIER, 10_000)[1:] == (1, 0)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's regex")
def test_an_ask_number_stays_text_and_answer_refs_become_chips() -> None:
    page = (Path(__file__).parent.parent / "src/bridge/web/guild.html").read_text()
    [line] = [line for line in page.splitlines() if line.startswith("const MD_INLINE")]
    script = line.replace("const MD_INLINE", "const R") + (
        ";console.log(JSON.stringify(process.argv.slice(1).map((t) => [...t.matchAll(R)].map((m) => m[0]))))"
    )
    texts = ["ask #11: Mute the alert", "Asks #12 and #13", "see #412", "A#3 and Q#2"]
    out = subprocess.run(["node", "-e", script, *texts], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == [[], ["#13"], ["#412"], ["A#3", "Q#2"]]


def test_an_unopened_answer_stays_until_ok_however_old(conn: sqlite3.Connection) -> None:
    now = 100 * DAY_MS
    for days, resolved in ((10, None), (1, None), (2, 2 * DAY_MS)):
        conn.execute(
            "INSERT INTO user_items (created_at, from_conversation, from_handle, severity, text, source, kind, "
            "resolved_at) VALUES (?, 'conv-l', 'alex', 'info', ?, 'agent', 'done', ?)",
            (now - days * DAY_MS, f"{days} days old", resolved),
        )
    page = map_db_to_guild(conn, Config(), now, lambda ref: "")
    assert [a["text"] for a in cast(list[dict[str, object]], page["answers"])] == ["1 days old", "10 days old"]


def test_results_posted_before_answers_existed_do_not_flood_the_column(tmp_path: Path) -> None:
    from bridge.db import MIGRATIONS

    raw = sqlite3.connect(tmp_path / "old.db")
    for migration in MIGRATIONS[:-1]:
        raw.executescript(migration)
    raw.execute(f"PRAGMA user_version = {len(MIGRATIONS) - 1}")
    recent = int(time.time() * 1000) - DAY_MS
    for at, text in ((1000, "years old"), (recent, "yesterday")):
        raw.execute(
            "INSERT INTO user_items (created_at, severity, text, source, kind) VALUES (?, 'info', ?, 'agent', 'done')",
            (at, text),
        )
    raw.commit()
    raw.close()

    c = connect(tmp_path / "old.db")
    rows = c.execute("SELECT text, resolved_at IS NOT NULL FROM user_items ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("years old", 1), ("yesterday", 0)]
    c.close()


def test_a_summary_line_with_a_huge_link_still_lists_the_ask(conn: sqlite3.Connection) -> None:
    huge = "https://x.example/" + "a" * (BODY_MAX_CHARS + 100)
    ask = add_ask(conn, "alex", "aide", "Check the report", "tab", [huge], 1000)
    start_ask(conn, ask.id, WORKER, 2000)
    claim_ask_done(conn, ask.id, WORKER, "Deployed PR 1 to the status page", 3000)
    verify_ask(conn, ask.id, LEAD, EVIDENCE, HOW, 4000)

    add_session(conn, "conv-aide", "aide")
    message_id, listed, left = post_verified_batch(conn, Config(), VERIFIER, 5000)

    body = conn.execute("SELECT body FROM messages WHERE id = ?", (message_id,)).fetchone()["body"]
    assert (body, listed, left) == (f"1 ask verified\n- ask #{ask.id}: Check the report", 1, 0)


def test_past_the_cap_the_page_says_how_many_more_answers_wait(conn: sqlite3.Connection) -> None:
    for n in range(ANSWERS_MAX + 2):
        conn.execute(
            "INSERT INTO user_items (created_at, from_conversation, from_handle, severity, text, source, kind) "
            "VALUES (?, 'conv-l', 'alex', 'info', ?, 'agent', 'insight')",
            (n, f"news {n}"),
        )
    page = map_db_to_guild(conn, Config(), 10**6, lambda ref: "")
    shown = cast(list[dict[str, object]], page["answers"])
    assert (len(shown), shown[0]["text"], page["answers_more"]) == (ANSWERS_MAX, f"news {ANSWERS_MAX + 1}", 2)


def test_a_workers_posts_go_to_its_lead_not_the_owner(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    assert team.enroll().returncode == 0
    with bridge_env.connect() as db:
        db.execute("INSERT INTO tab_parents (agterm_session, parent_session) VALUES ('TAB-B', 'TAB-A')")

    done = bridge_env.cli("post", "done", "Commit 1a2b on my branch", pid=team.b)
    question = bridge_env.cli("post", "decision", "Ship it?", "--option", "Ship", pid=team.b)

    assert "sent to alex as draft message" in done.stdout, done.stderr
    assert "sent to alex as draft message" in question.stdout, question.stderr
    with bridge_env.connect() as db:
        assert db.execute("SELECT count(*) FROM user_items").fetchone()[0] == 0
        bodies = [str(r[0]) for r in db.execute("SELECT body FROM messages ORDER BY id")]
    assert bodies[-1].startswith("Draft decision post, not shown to the owner: Ship it?")
    assert "Answer 1: Ship" in bodies[-1]


def test_a_leads_question_and_unasked_result_go_to_the_assistant(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    assert team.enroll().returncode == 0
    assert bridge_env.cli("handle", "aide", pid=team.b).returncode == 0
    with bridge_env.connect() as db:
        db.execute(
            "INSERT INTO asks (owner, recorded_by, words, source, state, created_at, updated_at) "
            "VALUES ('alex', 'aide', 'Is the export fixed?', 'tab', 'open', 1, 1)"
        )

    result = bridge_env.cli("post", "done", "Retry cap added", "--body", "details", pid=team.a)
    question = bridge_env.cli(
        "post", "decision", "Ship the retry?", "--what", "a cap", "--why", "release", "--option", "Ship",
        "--cost", "one deploy", pid=team.a,
    )  # fmt: skip
    answer = bridge_env.cli("post", "done", "Export fixed", "--ask", "1", pid=team.a)
    by_assistant = bridge_env.cli("post", "insight", "Weekly numbers", pid=team.b)

    assert "sent to aide as draft message" in result.stdout, result.stderr
    assert "sent to aide as draft message" in question.stdout, question.stderr
    assert "is in the user's feed" in answer.stdout and "is in the user's feed" in by_assistant.stdout
    with bridge_env.connect() as db:
        items = [r[0] for r in db.execute("SELECT text FROM user_items ORDER BY id")]
        bodies = [str(r[0]) for r in db.execute("SELECT body FROM messages ORDER BY id")]
    assert items == ["Export fixed", "Weekly numbers"]
    assert bodies[0] == "Draft done post, not shown to the owner: Retry cap added\ndetails"
    assert "What: a cap\nWhy now: release\nShip cost: one deploy\nAnswer 1: Ship" in bodies[1]


def test_the_assistant_or_its_author_takes_an_answer_off(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pids = start(bridge_env, spawn_process, agterm_tabs=False)
    bridge_env.cli("post", "insight", "Not for the owner", pid=pids["a"])
    bridge_env.cli("post", "insight", "Old news", pid=pids["a"])
    refused = bridge_env.cli("post", "resolve", "1", pid=pids["b"])
    assert refused.returncode == 2 and "only its author withdraws it" in refused.stderr
    assert bridge_env.cli("handle", "aide", pid=pids["b"]).returncode == 0
    assert bridge_env.cli("post", "resolve", "1", pid=pids["b"]).returncode == 0
    assert bridge_env.cli("post", "resolve", "2", pid=pids["a"]).returncode == 0
    with bridge_env.connect() as db:
        assert db.execute("SELECT count(*) FROM user_items WHERE resolved_at IS NULL").fetchone()[0] == 0


def test_a_draft_keeps_every_field_and_wakes_its_recipient(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    assert team.enroll().returncode == 0
    assert bridge_env.cli("handle", "aide", pid=team.b).returncode == 0
    with bridge_env.connect() as db:
        db.execute(
            "INSERT INTO asks (owner, recorded_by, words, source, state, created_at, updated_at) "
            "VALUES ('alex', 'aide', 'Ship the retry?', 'tab', 'open', 1, 1)"
        )

    question = bridge_env.cli(
        "post", "blocked", "Staging is down", "--what", "the deploy failed", "--why", "release today",
        "--option", "Roll back", "--cost", "an hour", "--option", "Wait", "--urgent", pid=team.a,
    )  # fmt: skip
    long_result = bridge_env.cli(
        "post", "insight", "Long notes", "--body", "x" * (BODY_MAX_CHARS + 50), "--link", "Notes=https://x.example/n",
        pid=team.a,
    )  # fmt: skip
    told = bridge_env.cli("tell-user", "Heads up", pid=team.a)

    assert question.returncode == 0 and long_result.returncode == 0 and told.returncode == 0, told.stderr
    with bridge_env.connect() as db:
        rows = db.execute("SELECT body, post_type FROM messages ORDER BY id").fetchall()
        assert db.execute("SELECT count(*) FROM user_items").fetchone()[0] == 0
    bodies = [str(r["body"]) for r in rows]
    assert {r["post_type"] for r in rows} == {"to-assistant"}, "marked so an idle assistant is woken"
    assert bodies[0].startswith("Draft blocked post (urgent), not shown to the owner: Staging is down")
    assert "Roll back cost: an hour" in bodies[0] and "Answer 1: Roll back\nAnswer 2: Wait" in bodies[0]
    parts = [b for b in bodies if b.startswith("(part ")]
    count = len(parts)
    assert count >= 2 and parts[0].startswith(f"(part 1 of {count}) Draft insight post")
    joined = "".join(p.removeprefix(f"(part {n} of {count}) ") for n, p in enumerate(parts, 1))
    assert "x" * (BODY_MAX_CHARS + 50) in joined and joined.endswith("Link: Notes=https://x.example/n"), "never cut"
    assert bodies[-1].startswith("Draft insight post, not shown to the owner: Heads up")


def test_a_leads_draft_is_typed_into_the_idle_assistants_tab(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    assert team.enroll().returncode == 0
    assert bridge_env.cli("handle", "aide", pid=team.b).returncode == 0
    huge = bridge_env.cli("post", "insight", "Big notes", "--body", "y " * 1_500, pid=team.a)
    assert huge.returncode == 0 and "sent to aide as draft message" in huge.stdout, huge.stderr
    assert wait_for(
        lambda: bridge_env.stub_log.exists() and "Draft insight post" in bridge_env.stub_log.read_text(), 15
    ), "an idle assistant's tab gets the draft typed in"
    assert "--target TAB-B" in bridge_env.stub_log.read_text()


def test_a_split_never_cuts_a_word_or_a_link() -> None:
    from bridge.cli_board import split_at_spaces

    text = ("word " * 30) + "/p/release-notes " + ("more " * 30)
    parts = split_at_spaces(text, 60)
    assert all(len(p) <= 60 for p in parts)
    assert any("/p/release-notes" in p for p in parts) and " ".join(parts).split() == text.split()
    assert split_at_spaces("x" * 130, 60) == ["x" * 60, "x" * 60, "x" * 10], "no space: a hard cut is the only way"


def test_a_draft_split_at_spaces_fits_every_part_whatever_the_count() -> None:
    from bridge.cli_board import split_draft

    for links in range(1, 14):
        for size in (900, 1_300, 1_700, 1_960):
            text = (
                "head\n" + "w " * 1_000 + "".join(f"\nLink: https://x.example/{n}/" + "u" * size for n in range(links))
            )
            parts = split_draft(text)
            assert all(len(p) <= BODY_MAX_CHARS for p in parts), (links, size)
            assert all(p.startswith(f"(part {n} of {len(parts)}) ") for n, p in enumerate(parts, 1))


def test_a_locked_tab_forwards_no_draft(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    assert team.enroll().returncode == 0
    with bridge_env.connect() as db:
        db.execute("INSERT INTO tab_parents (agterm_session, parent_session) VALUES ('TAB-B', 'TAB-A')")
    with bridge_env.connect() as db:
        db.execute("UPDATE surfaces SET tab_name = '🔒 private' WHERE handle = 'old-b'")

    done = bridge_env.cli("post", "done", "Private notes", pid=team.b)

    assert done.returncode == 2 and "locked" in done.stderr, done.stdout
    with bridge_env.connect() as db:
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_a_summary_waits_while_the_assistants_tab_is_locked(conn: sqlite3.Connection) -> None:
    verified(conn, "Make the status page show the new copy")
    add_session(conn, "conv-aide", "aide")
    conn.execute("UPDATE surfaces SET tab_name = '🔒 private' WHERE handle = 'aide'")
    with pytest.raises(AskError, match="locked"):
        post_verified_batch(conn, Config(), VERIFIER, 5000)
    conn.execute("UPDATE surfaces SET tab_name = 'aide' WHERE handle = 'aide'")
    assert post_verified_batch(conn, Config(), VERIFIER, 6000)[1:] == (1, 0), "still waiting, not marked told"


def test_a_reply_is_refused_while_the_assistants_tab_is_locked(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    pids = start(bridge_env, spawn_process, agterm_tabs=False)
    assert bridge_env.cli("handle", "aide", pid=pids["b"]).returncode == 0
    bridge_env.cli("post", "insight", "Weekly numbers", pid=pids["a"])
    with bridge_env.connect() as db:
        db.execute("UPDATE surfaces SET tab_name = '🔒 private' WHERE handle = 'aide'")

    refused = json.loads(http_request(dashboard + "/api/items/1/reply", post=True, data={"text": "hi"}, expect=409))

    assert "locked" in str(refused["detail"])
    assert answers(dashboard)[0]["unread"] is True, "a refused reply does not mark the answer read"
