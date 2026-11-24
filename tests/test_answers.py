import json
import shutil
import sqlite3
import subprocess
import time
from collections.abc import Generator
from pathlib import Path
from typing import cast

import pytest
from conftest import BridgeEnv, SpawnProcess, http_request
from test_asks import EVIDENCE, HOW, LEAD, WORKER, write_assistant
from test_feed import prompt, start
from test_leads import Team

from bridge.asks import (
    AskError,
    add_ask,
    claim_ask_done,
    get_ask,
    is_answer_due,
    mark_ask_told,
    post_verified_ask,
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


def test_only_the_owners_question_or_an_ask_that_waited_on_the_owner_is_posted_on_verify(
    conn: sqlite3.Connection,
) -> None:
    plain = verified(conn, "Make the status page show the new copy")
    question = verified(conn, "Why does the export stall at night?", question=True)
    waited = verified(conn, "Pick the banner copy with me", wait_on_owner=True)

    assert get_ask(conn, waited).wait_on is None, "the claim cleared the wait; only its history keeps it"
    assert [is_answer_due(conn, get_ask(conn, i)) for i in (plain, question, waited)] == [False, True, True]
    item_id = post_verified_ask(conn, get_ask(conn, question), VERIFIER, 5000)
    row = conn.execute("SELECT kind, text, body, links, ask_id FROM user_items WHERE id = ?", (item_id,)).fetchone()
    assert (row["kind"], row["text"], row["ask_id"]) == ("done", "Verified: Why does the export stall at night?", 2)
    assert row["body"] == f"{EVIDENCE}\n\nHow: {HOW}"
    assert json.loads(row["links"]) == [{"label": "x.example", "url": "https://x.example/pr/1"}]
    assert get_ask(conn, question).told_at == 5000


def test_a_batch_summary_lists_the_untold_verified_asks_once_and_skips_posted_ones(conn: sqlite3.Connection) -> None:
    first = verified(conn, "Make the status page show the new copy")
    second = verified(conn, "Rename the export button\nto Download")
    posted = verified(conn, "Why does the export stall?", question=True)
    post_verified_ask(conn, get_ask(conn, posted), VERIFIER, 5000)

    item_id, listed, left = post_verified_batch(conn, VERIFIER, 6000)

    row = conn.execute("SELECT kind, text, body FROM user_items WHERE id = ?", (item_id,)).fetchone()
    assert (row["kind"], row["text"], listed, left) == ("done", "2 asks verified", 2, 0)
    assert row["body"] == (
        f"- ask #{first}: Make the status page show the new copy https://x.example/pr/1\n"
        f"- ask #{second}: Rename the export button to Download https://x.example/pr/1"
    )
    with pytest.raises(AskError, match="no ask you verified"):
        post_verified_batch(conn, VERIFIER, 7000)


def test_a_batch_too_long_for_one_post_leaves_the_rest_for_the_next(conn: sqlite3.Connection) -> None:
    for n in range(30):
        verified(conn, f"Ask number {n} " + "with many words " * 10)

    _, listed, left = post_verified_batch(conn, VERIFIER, 6000)
    _, listed_next, left_next = post_verified_batch(conn, VERIFIER, 7000)

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
        post_verified_batch(c, Party("conv-l", "alex"), 9)
    c.close()


def test_a_result_post_shows_in_answers_until_ok_and_a_reply_reaches_its_author(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    pids = start(bridge_env, spawn_process, agterm_tabs=False)
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
    context = prompt(bridge_env, pids["a"], "conv-a", "next")
    assert 'Reply to your done post A#2 "Export fixed": Thanks, also check the weekly one' in context
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


def test_a_reply_goes_only_to_a_result_from_a_session(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    pids = start(bridge_env, spawn_process, agterm_tabs=False)
    bridge_env.cli("post", "decision", "Ship?", "--option", "Ship", pid=pids["a"])
    with bridge_env.connect() as db:
        db.execute(
            "INSERT INTO user_items (created_at, from_conversation, from_handle, severity, text, source, kind) "
            "VALUES (strftime('%s','now') * 1000, 'system', 'bridge', 'info', 'Merged web#7', 'agent', 'done')"
        )
    [merged] = answers(dashboard)
    assert merged["can_reply"] is False

    for item_id, words in ((1, "no done or insight post A#1"), (2, "is bridge's own")):
        refused = json.loads(
            http_request(dashboard + f"/api/items/{item_id}/reply", post=True, data={"text": "hi"}, expect=409)
        )
        assert words in str(refused["detail"])


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


def test_cli_verify_posts_the_owners_question_and_summary_posts_the_rest(
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

    assert "posted to Answers as A#1" in question.stdout, question.stderr
    assert "goes into your next `guild ask summary`" in plain.stdout
    assert summary.stdout.strip() == "A#2 lists 1 verified ask", summary.stderr
    assert nothing.returncode == 2 and "no ask you verified" in nothing.stderr
    with bridge_env.connect() as db:
        rows = db.execute("SELECT text, ask_id, from_handle FROM user_items ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [
        ("Verified: Why does the export stall?", 1, "old-b"), ("1 ask verified", None, "old-b"),
    ]  # fmt: skip


def test_a_reopened_ask_is_told_again_once_verified_again(conn: sqlite3.Connection) -> None:
    ask_id = verified(conn, "Make the status page show the new copy")
    post_verified_batch(conn, VERIFIER, 5000)
    reopen_ask(conn, ask_id, "aide", "still the old copy", 6000)
    start_ask(conn, ask_id, WORKER, 7000)
    claim_ask_done(conn, ask_id, WORKER, "Deployed PR 2 to the status page", 8000)
    verify_ask(conn, ask_id, LEAD, EVIDENCE, HOW, 9000)

    assert post_verified_batch(conn, VERIFIER, 10_000)[1:] == (1, 0)


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


def test_an_ask_already_answered_by_a_post_is_not_posted_again_on_verify(conn: sqlite3.Connection) -> None:
    ask = add_ask(conn, "alex", "aide", "Why does the export stall?", "tab", [], 1000, question=True)
    mark_ask_told(conn, ask.id, 1500)
    start_ask(conn, ask.id, WORKER, 2000)
    claim_ask_done(conn, ask.id, WORKER, "Deployed PR 1 to the status page", 3000)

    assert is_answer_due(conn, verify_ask(conn, ask.id, LEAD, EVIDENCE, HOW, 4000)) is False


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

    item_id, listed, left = post_verified_batch(conn, VERIFIER, 5000)

    row = conn.execute("SELECT text, body FROM user_items WHERE id = ?", (item_id,)).fetchone()
    assert (row["text"], row["body"], listed, left) == ("1 ask verified", f"- ask #{ask.id}: Check the report", 1, 0)


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
