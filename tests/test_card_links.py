import json
import re
import sqlite3
from collections.abc import Generator
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest
from conftest import BridgeEnv, SpawnProcess
from test_doc_style import run_node
from test_leads import Team
from test_pages import make_source
from test_status_watchers import STUBS

from bridge.config import Config
from bridge.db import connect
from bridge.guildview import map_db_to_guild
from bridge.pages import add_page, archive_page, find_unlinked_page_mention
from bridge.serve import map_ref_to_url

PAGE = Path(__file__).parent.parent / "src/bridge/web/guild.html"
CONFIG = Config(issue_prefixes=("ABC",), issue_url="https://linear.app/acme/issue/{key}")


@pytest.fixture
def conn(tmp_path: Path) -> Generator[sqlite3.Connection]:
    c = connect(tmp_path / "bridge.db")
    yield c
    c.close()


def add_item(conn: sqlite3.Connection, kind: str, text: str, body: str, links: list[dict[str, str]]) -> None:
    conn.execute(
        "INSERT INTO user_items (created_at, from_conversation, from_handle, severity, text, body, links, source, "
        "kind) VALUES (1000, 'conv-l', 'alex', 'info', ?, ?, ?, 'agent', ?)",
        (text, body, json.dumps(links), kind),
    )


def guild(conn: sqlite3.Connection, config: Config) -> dict[str, list[dict[str, object]]]:
    page = map_db_to_guild(conn, config, 2000, lambda ref: map_ref_to_url(ref, config))
    return cast(dict[str, list[dict[str, object]]], page)


def test_a_card_names_our_page_by_its_title_and_never_by_the_bare_host(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    add_page(conn, "lambda-jobs", "Lambda jobs run log", str(make_source(tmp_path, "a", "index.html")), None, False,
             "alex", 1000)  # fmt: skip
    add_page(conn, "old", "Old notes", str(make_source(tmp_path, "b", "index.html")), None, False, "alex", 1000)
    archive_page(conn, "old", 1500)
    links = [
        {"label": "127.0.0.1:8765", "url": "http://127.0.0.1:8765/p/lambda-jobs"},
        {"label": "x.example", "url": "https://x.example/r/1"},
        {"label": "Report", "url": "https://x.example/r/2"},
        {"label": "127.0.0.1:8765", "url": "http://127.0.0.1:8765/p/old"},
        {"label": "elsewhere.example", "url": "https://elsewhere.example/p/lambda-jobs"},
        {"label": "localhost:3000", "url": "http://localhost:3000/p/lambda-jobs"},
    ]
    add_item(conn, "done", "Lambda jobs just happened", "", links)
    add_item(conn, "decision", "Retry the jobs?", "", links)

    page = guild(conn, Config())

    shown = [
        {"label": "Lambda jobs run log", "url": "http://127.0.0.1:8765/p/lambda-jobs"},
        {"label": "", "url": "https://x.example/r/1"},
        {"label": "Report", "url": "https://x.example/r/2"},
        {"label": "", "url": "http://127.0.0.1:8765/p/old"},
        {"label": "", "url": "https://elsewhere.example/p/lambda-jobs"},
        {"label": "", "url": "http://localhost:3000/p/lambda-jobs"},
    ]
    assert page["answers"][0]["links"] == shown
    assert [n["links"] for n in page["needs"] if n["type"] == "post"] == [shown]


def test_refs_in_card_text_get_their_urls_and_unknown_ones_none(conn: sqlite3.Connection, tmp_path: Path) -> None:
    add_page(conn, "lambda-jobs", "Lambda jobs", str(make_source(tmp_path, "a", "index.html")), None, False, "alex",
             1000)  # fmt: skip
    add_item(
        conn,
        "done",
        "ABC-12 fixed in acme/web#3",
        "see /p/lambda-jobs and /p/lambda-jobs#runs, not /p/nope, https://x.example/p/lambda-jobs; "
        "XYZ-9 and abc-4 stay",
        [],
    )

    refs = guild(conn, CONFIG)["answers"][0]["refs"]
    no_tracker = guild(conn, Config(issue_prefixes=("ABC",)))["answers"][0]["refs"]

    assert refs == [
        {"text": "acme/web#3", "url": "https://github.com/acme/web/pull/3"},
        {"text": "ABC-12", "url": "https://linear.app/acme/issue/ABC-12"},
        {"text": "/p/lambda-jobs", "url": "/p/lambda-jobs"},
        {"text": "/p/lambda-jobs#runs", "url": "/p/lambda-jobs#runs"},
    ]
    assert [r["text"] for r in cast(list[dict[str, str]], no_tracker)] == [
        "acme/web#3",
        "/p/lambda-jobs",
        "/p/lambda-jobs#runs",
    ]


CARDS = """
const node = (tag) => ({ tag, children: [], className: "", textContent: "", href: undefined, classList: { add() {} },
  dataset: {}, append(...kids) { this.children.push(...kids); }, addEventListener() {}, set title(v) {},
  setAttribute() {} });
globalThis.document = { createElement: (tag) => node(tag) };
globalThis.requestAnimationFrame = () => 0;
const texts = (n) => typeof n === "string" ? [n] : [n.textContent, ...n.children.flatMap(texts)];
const find = (n, test) =>
  typeof n === "string" ? [] : [...(test(n) ? [n] : []), ...n.children.flatMap((k) => find(k, test))];
const eq = (a, b) => {
  if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(JSON.stringify(a) + " !== " + JSON.stringify(b));
};
const anchors = (n) => find(n, (k) => k.tag === "a").map((a) => [texts(a).join(""), a.href]);
const refs = [
  { text: "ABC-12", url: "https://linear.app/acme/issue/ABC-12" },
  { text: "ABC-1", url: "https://linear.app/acme/issue/ABC-1" },
  { text: "acme/web#3", url: "https://github.com/acme/web/pull/3" },
  { text: "acme/2fa-tools#42", url: "https://github.com/acme/2fa-tools/pull/42" },
  { text: "/p/lambda-jobs", url: "/p/lambda-jobs" },
  { text: "/p/lambda-jobs#runs", url: "/p/lambda-jobs#runs" },
  { text: "<b>x</b>", url: "javascript:alert(1)" },
];
const links = [
  { label: "Lambda jobs run log", url: "http://127.0.0.1:8765/p/lambda-jobs" },
  { label: "", url: "https://x.example/r/1" },
  { label: "<img src=x onerror=alert(1)>", url: "https://x.example/r/2" },
];
state.now = 5000;
const answer = { id: 1, ref: "A#1", at: 1000, kind: "done", from: "alex", unread: false, ask: null, can_reply: false,
  text: "ABC-12 shipped, ABC-19 not, notes at https://docs.example/report?issue=ABC-12&view=full",
  body: "acme/web#3, acme/2fa-tools#42 and /p/lambda-jobs (/p/lambda-jobs#runs); /p/nope, XYZ-9 and <b>x</b> stay text",
  links, refs };
const card = renderAnswerCard(answer);
const body = card.children.find((k) => k.className === "links");
eq(anchors(body), [["Lambda jobs run log", "http://127.0.0.1:8765/p/lambda-jobs"],
  ["x.example/r/1", "https://x.example/r/1"], ["<img src=x onerror=alert(1)>", "https://x.example/r/2"]]);
const foot = card.children.find((k) => k.className === "foot");
eq(anchors(foot), []);
eq(anchors(card.children[0]), [["ABC-12", "https://linear.app/acme/issue/ABC-12"],
  ["docs.example/report?issue=ABC-12&view=full", "https://docs.example/report?issue=ABC-12&view=full"]]);
eq(texts(card.children[0]).join("").includes("ABC-19 not"), true);
const md = card.children.find((k) => k.className === "md");
eq(anchors(md), [["acme/web#3", "https://github.com/acme/web/pull/3"],
  ["acme/2fa-tools#42", "https://github.com/acme/2fa-tools/pull/42"], ["/p/lambda-jobs", "/p/lambda-jobs"],
  ["/p/lambda-jobs#runs", "/p/lambda-jobs#runs"]]);
eq(texts(md).join("").includes("/p/nope, XYZ-9 and <b>x</b> stay text"), true);
eq(find(md, (k) => k.tag === "b").length, 0);
const need = { type: "post", id: 2, question_ref: "Q#2", at: 1000, kind: "decision", from: "alex", urgent: false,
  text: "Ship ABC-12?", sections: [{ text: "see /p/lambda-jobs" }], card: null, options: [], links, refs };
const question = renderNeedCard(need);
eq(anchors(question.children.find((k) => k.className === "links"))[0], ["Lambda jobs run log",
  "http://127.0.0.1:8765/p/lambda-jobs"]);
eq(anchors(question.children.find((k) => k.className === "foot")), []);
eq(anchors(question.children[0]), [["ABC-12", "https://linear.app/acme/issue/ABC-12"]]);
const bare = renderAnswerCard({ ...answer, links: [], refs: [] });
eq(bare.children.some((k) => k.className === "links"), false);
eq(anchors(bare), [["docs.example/report?issue=ABC-12&view=full", "https://docs.example/report?issue=ABC-12&view=full"]]);
"""


def test_cards_show_links_in_the_body_and_refs_as_links_and_text_stays_text(tmp_path: Path) -> None:
    script = re.search(r"<script>(.*)</script>", PAGE.read_text(), re.S)
    assert script is not None
    run_node(tmp_path, STUBS + script.group(1) + CARDS)


def test_a_post_naming_a_page_needs_its_link(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, tmp_path: Path
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    with closing(bridge_env.connect()) as db:
        add_page(db, "lambda-jobs", "Lambda jobs", str(make_source(tmp_path, "a", "index.html")), None, False, "alex",
                 1000)  # fmt: skip
    url = "http://127.0.0.1:8765/p/lambda-jobs"

    def post(*args: str) -> tuple[int, str]:
        done = bridge_env.cli("post", *args, pid=team.b)
        return done.returncode, done.stderr

    cited = post("done", "Report at /p/lambda-jobs", "--no-link")
    titled = post("done", "The lambda jobs just happened")
    by_slug = post("decision", "Retry?", "--body", "lambda-jobs shows two failures")
    worded = post("done", "The analysis is ready")
    told = bridge_env.cli("tell-user", "The explainer is up", pid=team.b)
    other = post("done", "Lambda jobs: see the run", "--link", "Run=https://x.example/r/1")
    option = post("decision", "Retry?", "--body", "Choose one", "--option", "Read /p/lambda-jobs", "--option", "Skip")

    assert cited[0] != 0 and f"--link 'Title={url}'" in cited[1] and "--no-link" not in cited[1]
    assert titled[0] != 0 and f"--link 'Lambda jobs={url}'" in titled[1] and "add --no-link" in titled[1]
    assert by_slug[0] != 0 and "the page 'Lambda jobs'" in by_slug[1]
    assert worded[0] != 0 and "says 'analysis' but carries no link" in worded[1] and "add --no-link" in worded[1]
    assert told.returncode != 0 and "tell-user carries no link: use `bridge post insight" in told.stderr
    assert other[0] != 0 and "the page 'Lambda jobs'" in other[1], "a link to something else is not the page's"
    assert option[0] != 0 and "names /p/lambda-jobs" in option[1], "an answer button is read too"
    for flags in (("--cost", "See https://x.example"), ("--pick", "1", "--pick-why", "See https://x.example")):
        priced = post("decision", "Read the analysis?", "--body", "Choose", "--option", "Yes", *flags)
        assert priced[0] != 0 and "says 'analysis'" in priced[1], f"{flags[0]}: a button shows no link"
    with closing(bridge_env.connect()) as db:
        assert db.execute("SELECT count(*) FROM user_items").fetchone()[0] == 0

    assert post("done", "Lambda jobs just happened: /p/lambda-jobs", "--link", url)[0] == 0
    assert post("done", "The analysis is ready", "--link", "Analysis=https://x.example/a")[0] == 0
    assert post("insight", "The status page is back", "--no-link")[0] == 0
    assert post("done", "Deployed the fix to staging")[0] == 0
    assert post("decision", "Read the analysis?", "--what", "See https://x.example", "--option", "Yes")[0] == 0
    external = "https://x.example/p/external-only"
    assert post("done", f"Their notes: {external}", "--link", external)[0] == 0, "another site's /p/ is not ours"
    assert bridge_env.cli("send", "alex", f"see {external}", pid=team.b).returncode == 0
    assert bridge_env.cli("tell-user", "Docs are not ours here", "--no-link", pid=team.b).returncode == 0
    with closing(bridge_env.connect()) as db:
        rows = db.execute("SELECT text, links FROM user_items ORDER BY id").fetchall()
    assert json.loads(rows[0]["links"]) == [{"label": "127.0.0.1:8765", "url": url}]
    assert len(rows) == 7


def test_a_draft_to_the_assistant_is_checked_and_keeps_the_pages_link(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, tmp_path: Path
) -> None:
    team = Team(bridge_env, spawn_process)
    assert team.enroll().returncode == 0
    assert bridge_env.cli("handle", "aide", pid=team.b).returncode == 0
    with closing(bridge_env.connect()) as db:
        add_page(db, "lambda-jobs", "Lambda jobs", str(make_source(tmp_path, "a", "index.html")), None, False, "alex",
                 1000)  # fmt: skip
    url = "http://127.0.0.1:8765/p/lambda-jobs"

    refused = bridge_env.cli("post", "done", "Lambda jobs just happened", pid=team.a)
    option = bridge_env.cli(
        "post", "decision", "Retry?", "--body", "Choose", "--option", "Read /p/lambda-jobs", pid=team.a
    )
    sent = bridge_env.cli("post", "done", "Lambda jobs just happened", "--link", f"Lambda jobs={url}", pid=team.a)

    assert refused.returncode != 0 and "the page 'Lambda jobs'" in refused.stderr
    assert option.returncode != 0 and "names /p/lambda-jobs" in option.stderr, "a draft's answer button is read too"
    assert sent.returncode == 0 and "draft message" in sent.stdout, sent.stderr
    with closing(bridge_env.connect()) as db:
        [body] = [str(r["body"]) for r in db.execute("SELECT body FROM messages WHERE post_type = 'to-assistant'")]
        assert db.execute("SELECT count(*) FROM user_items").fetchone()[0] == 0
    assert body.endswith(f"Link: Lambda jobs={url}")


def test_the_page_declares_each_function_once() -> None:
    names = re.findall(r"^(?:async )?function (\w+)", PAGE.read_text(), re.M)
    assert sorted({n for n in names if names.count(n) > 1}) == [], "a second declaration silently replaces the first"


@pytest.mark.parametrize(
    ("text", "refused"),
    [
        ("The write-up is ready", True),
        ("see the doc.", True),
        ("The analysis is ready", True),
        ("Docs, pages and analyses updated", False),
        ("The page is up at https://x.example/a", False),
        ("Fixed pages.py and the doc-style CSS", False),
        ("page-load time is down; src/pages/ cleaned", False),
        ("Docker image rebuilt", False),
    ],
)
def test_the_page_words_count_only_as_words(conn: sqlite3.Connection, text: str, refused: bool) -> None:
    assert (find_unlinked_page_mention(conn, text, [], False) is not None) is refused
    assert find_unlinked_page_mention(conn, text, ["https://x.example/a"], False) is None
    assert find_unlinked_page_mention(conn, text, [], True) is None


def test_only_a_url_the_card_shows_as_a_link_lets_the_page_words_pass(conn: sqlite3.Connection) -> None:
    assert find_unlinked_page_mention(conn, "The analysis is at https://x.example/a", [], False) is None
    assert find_unlinked_page_mention(conn, "The analysis is at `https://x.example/a`", [], False) is not None
    assert find_unlinked_page_mention(conn, "The analysis is ready", [], False, ["at https://x.example/a"]) is not None


def test_a_page_path_inside_another_sites_url_is_theirs(conn: sqlite3.Connection, tmp_path: Path) -> None:
    add_page(conn, "lambda-jobs", "Lambda jobs", str(make_source(tmp_path, "a", "index.html")), None, False, "alex",
             1000)  # fmt: skip
    for url in (
        "https://x.example/?next=/p/lambda-jobs",
        "https://x.example/app#/p/lambda-jobs",
        "https://x.example/p/lambda-jobs",
        "https://x.example/?next=/p/nope",
    ):
        assert find_unlinked_page_mention(conn, f"Their notes: {url}", [url], False) is None, url
    assert find_unlinked_page_mention(conn, "see http://127.0.0.1:8765/p/lambda-jobs", [], False) is not None
