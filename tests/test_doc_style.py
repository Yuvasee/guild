import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from conftest import BridgeEnv, SpawnProcess, http_request
from test_leads import Team
from test_status_watchers import STUBS

from bridge.doc_nav import DOC_STYLE_ID, VIEW_ID, build_doc_style, convert_doc_to_viewable
from bridge.pages import add_doc_style_to_page

WEB = Path(__file__).parent.parent / "src/bridge/web"
DOC = b"<!doctype html><html><head><style>p{color:red}</style></head><body><h2>A</h2><p>x</p><h2>B</h2></body></html>"


def test_a_served_doc_gets_the_shared_style_once_after_the_viewer() -> None:
    served = convert_doc_to_viewable(DOC).decode()
    assert served.count(f'id="{DOC_STYLE_ID}"') == 1
    assert served.index(f'id="{VIEW_ID}"') < served.index(f'id="{DOC_STYLE_ID}"') < served.index("<h2")
    assert "</script" not in build_doc_style().removesuffix("</script>").split("<script>", 1)[1]
    assert not re.search(r"<script[^>]+src=", served), "the highlighter loads from the script, only for code blocks"


def test_a_doc_that_already_carries_the_style_is_not_given_a_second_copy() -> None:
    styled = DOC.replace(b"<body>", b"<body>" + build_doc_style().encode())
    assert convert_doc_to_viewable(styled).decode().count(f'id="{DOC_STYLE_ID}"') == 1


def test_the_style_draws_no_borders_or_rules() -> None:
    css = (WEB / "doc-style.css").read_text()
    assert not re.search(r"border(?:-(?:top|right|bottom|left))?\s*:\s*(?!0\b|none)", css)
    assert not re.search(r"box-shadow\s*:\s*inset", css)
    assert "border-style:none!important" in css


DOM = """
const element = () => new Proxy(function () {}, {
  get: (target, key) => key === Symbol.toPrimitive ? () => "" : (key in target ? target[key] : element()),
  set: () => true,
  apply: () => element(),
});
const like = (props) => new Proxy(props, { get: (target, key) => key in target ? target[key] : element() });
const created = [];
let pres = [];
globalThis.window = globalThis;
globalThis.addEventListener = () => {};
globalThis.location = { href: "http://127.0.0.1/", host: "127.0.0.1" };
globalThis.NodeFilter = { SHOW_TEXT: 4, FILTER_REJECT: 2, FILTER_ACCEPT: 1 };
globalThis.getComputedStyle = () => like({ backgroundColor: "rgb(255, 255, 255)" });
function load() {
  globalThis.document = like({
    readyState: "complete",
    documentElement: like({ hasAttribute: () => false }),
    body: like({ getElementsByTagName: () => [] }),
    head: like({ appendChild: () => {} }),
    querySelectorAll: (selector) => selector === "pre" ? pres : [],
    createElement: (tag) => { created.push(tag); return element(); },
    createTreeWalker: () => ({ nextNode: () => false }),
    getElementById: () => null,
  });
  eval(SCRIPT);
}
const pre = like({ closest: () => null, hasAttribute: () => false, classList: like({ contains: () => false }) });
load();
if (created.includes("script")) throw new Error("a doc without code blocks loaded the highlighter");
pres = [pre];
load();
if (!created.includes("script")) throw new Error("a doc with a code block did not load the highlighter");
"""


def run_node(tmp_path: Path, source: str) -> None:
    node = shutil.which("node")
    assert node, "node is required to check the page scripts"
    path = tmp_path / "check.js"
    path.write_text(source)
    done = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=20, check=False)
    assert done.returncode == 0, done.stderr


def test_the_highlighter_loads_only_for_a_doc_with_a_code_block(tmp_path: Path) -> None:
    run_node(tmp_path, f"const SCRIPT = {json.dumps((WEB / 'doc-style.js').read_text())};\n{DOM}")


def test_registering_a_page_inlines_the_style_once(tmp_path: Path) -> None:
    page = tmp_path / "index.html"
    page.write_text("<!doctype html><html><head><title>t</title></head><body class='x'><p>hi</p></body></html>")
    assert add_doc_style_to_page(str(tmp_path)) == page
    styled = page.read_text()
    assert styled.startswith("<!doctype html><html><head><title>t</title></head><body class='x'><style id=")
    assert add_doc_style_to_page(str(tmp_path)) is None
    assert page.read_text() == styled and styled.count(f'id="{DOC_STYLE_ID}"') == 1


def test_the_cli_says_when_it_styled_a_page(bridge_env: BridgeEnv, spawn_process: SpawnProcess, tmp_path: Path) -> None:
    pid = spawn_process()
    bridge_env.write_session_file(pid, "conv-style")
    (tmp_path / "pages/a").mkdir(parents=True)
    (tmp_path / "pages/a/index.html").write_text("<!doctype html><body><p>a</p></body>")
    added = bridge_env.cli("page", "add", "a", "--title", "A", "--source", str(tmp_path / "pages/a"), pid=pid)
    assert added.returncode == 0, added.stderr
    assert f"added the shared doc style to {(tmp_path / 'pages/a/index.html').resolve()}" in added.stdout


MARKDOWN = """
const node = (tag) => ({ tag, children: [], className: "", textContent: "", href: undefined,
  append(...kids) { this.children.push(...kids); }, set title(v) {}, setAttribute() {} });
globalThis.document = { createElement: (tag) => node(tag) };
const tags = (n) => typeof n === "string" ? [] : [n.tag, ...n.children.flatMap(tags)];
const texts = (n) => typeof n === "string" ? [n] : [n.textContent, ...n.children.flatMap(texts)];
const find = (n, test) =>
  typeof n === "string" ? [] : [...(test(n) ? [n] : []), ...n.children.flatMap((k) => find(k, test))];
const eq = (a, b) => {
  if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(JSON.stringify(a) + " !== " + JSON.stringify(b));
};
const box = renderMarkdown("<img src=x onerror=alert(1)> **bold** `a<b>` see Q#12 and acme/web#3 or #405 " +
  "[bad](javascript:alert(1)) https://github.com/acme/web/pull/5\\n\\n- one\\n- <script>two</script>\\n1. first");
eq([...new Set(tags(box))].sort(), ["a", "code", "div", "li", "ol", "p", "span", "strong", "ul"]);
eq(texts(box).join("").includes("<img src=x onerror=alert(1)>"), true);
eq(texts(box).join("").includes("<script>two</script>"), true);
eq(find(box, (n) => n.tag === "strong").map((n) => texts(n).join("")), ["bold"]);
eq(find(box, (n) => n.tag === "code").map((n) => n.textContent), ["a<b>"]);
eq(find(box, (n) => n.className === "chip").map((n) => n.textContent), ["Q#12", "acme/web#3", "#405", "acme/web#5"]);
eq(find(box, (n) => n.tag === "a").map((n) => n.href), ["https://github.com/acme/web/pull/5"]);
eq(find(box, (n) => n.tag === "li").length, 3);
"""


def test_card_markdown_shows_markup_as_text(tmp_path: Path) -> None:
    page = (WEB / "guild.html").read_text()
    script = re.search(r"<script>(.*)</script>", page, re.S)
    assert script is not None
    run_node(tmp_path, STUBS + script.group(1) + MARKDOWN)


def needs(dashboard: str) -> list[Any]:
    return json.loads(http_request(dashboard + "/api/guild"))["needs"]


def test_decision_fields_reach_the_card_and_old_posts_stay_text(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, dashboard: str
) -> None:
    team = Team(bridge_env, spawn_process)
    old = bridge_env.cli("post", "decision", "Old?", "--body", "Plain words.", "--option", "Yes", pid=team.a)
    assert old.returncode == 0, old.stderr
    new = bridge_env.cli(
        "post", "decision", "Ship the export worker?",
        "--what", "the worker behind a flag", "--why", "the window closes Friday",
        "--option", "Ship", "--cost", "one deploy", "--option", "Wait", "--option", "Drop", "--cost", "`two` days",
        "--pick", "1", "--pick-why", "keeps the window",
        "--link", "PR=https://github.com/acme/web/pull/7",
        pid=team.a,
    )  # fmt: skip
    assert new.returncode == 0, new.stderr

    plain, card = needs(dashboard)
    assert plain["card"] is None and plain["sections"] == [{"label": None, "text": "Plain words."}]
    assert card["card"] == {
        "intro": None,
        "facts": [
            {"label": "What", "text": "the worker behind a flag"},
            {"label": "Why now", "text": "the window closes Friday"},
        ],
        "options": [
            {"label": "Ship", "cost": "one deploy", "pick": True},
            {"label": "Wait", "cost": None, "pick": False},
            {"label": "Drop", "cost": "`two` days", "pick": False},
        ],
        "pick_why": "keeps the window",
    }
    assert card["options"] == ["Ship", "Wait", "Drop"]
    assert card["links"] == [{"label": "PR", "url": "https://github.com/acme/web/pull/7"}]


def test_decision_fields_are_checked(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    cases = [
        (["--cost", "x"], "--cost follows the --option"),
        (["--option", "A", "--cost", "x", "--cost", "y"], "one --cost per --option"),
        (["--option", "A", "--pick", "2", "--what", "w"], "--pick is an answer's number, 1 to 1"),
        (["--option", "A", "--pick-why", "w", "--what", "w"], "--pick-why needs --pick"),
    ]
    for extra, error in cases:
        done = bridge_env.cli("post", "decision", "Q?", *extra, pid=team.a)
        assert done.returncode == 2 and error in done.stderr, (extra, done.stderr)
    done = bridge_env.cli("post", "insight", "Note", "--what", "w", pid=team.a)
    assert done.returncode == 2 and "are for blocked and decision posts" in done.stderr
