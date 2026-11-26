import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from conftest import BridgeEnv, SpawnProcess, http_request
from test_leads import Team
from test_status_watchers import STUBS

from bridge.doc_nav import (
    DOC_STYLE_ID,
    NAV_MAX_BYTES,
    VIEW_ID,
    build_doc_style,
    convert_doc_to_viewable,
    convert_served_page_to_styled,
)
from bridge.guildview import map_body_to_sections, map_sections_to_card
from bridge.pages import add_doc_style_to_page

WEB = Path(__file__).parent.parent / "src/bridge/web"
DOC = b"<!doctype html><html><head><style>p{color:red}</style></head><body><h2>A</h2><p>x</p><h2>B</h2></body></html>"


def test_a_served_doc_gets_the_shared_style_once_after_the_viewer() -> None:
    served = convert_doc_to_viewable(DOC).decode()
    assert served.count(f'id="{DOC_STYLE_ID}"') == 1
    assert served.index(f'id="{VIEW_ID}"') < served.index(f'id="{DOC_STYLE_ID}"') < served.index("<h2")
    assert "</script" not in build_doc_style().removesuffix("</script>").split('<script data-bridge-added="style">')[1]
    assert not re.search(r"<script[^>]+src=", served), "the highlighter loads from the script, only for code blocks"


def test_a_doc_that_already_carries_the_style_is_not_given_a_second_copy() -> None:
    styled = DOC.replace(b"<body>", b"<body>" + build_doc_style().encode())
    assert convert_doc_to_viewable(styled).decode().count(f'id="{DOC_STYLE_ID}"') == 1


def test_the_style_draws_no_borders_or_rules() -> None:
    css = (WEB / "doc-style.css").read_text()
    assert not re.search(r"border(?:-(?:top|right|bottom|left))?\s*:\s*(?!0\b|none)", css)
    assert not re.search(r"box-shadow\s*:\s*inset", css)
    assert "border-style:none!important" in css
    assert "body *:not(.ds-keep,[data-ds-skip],[data-ds-skip] *)" in css, "drawings and opted-out parts keep borders"


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
    page.chmod(0o600)
    assert add_doc_style_to_page(str(tmp_path)) == f"added the shared doc style to {page}"
    assert page.stat().st_mode & 0o777 == 0o600
    styled = page.read_text()
    assert styled.startswith("<!doctype html><html><head><title>t</title></head><body class='x'><style id=")
    assert add_doc_style_to_page(str(tmp_path)) is None
    assert page.read_text() == styled and styled.count(f'id="{DOC_STYLE_ID}"') == 1


OLD_STYLE = (
    '<style id="bridge-doc-style">td{border-radius:8px}</style>'
    "<script>/* Shared doc style: highlights code blocks. */\n(function () { var a = 1 < 2; })();</script>"
)


def test_a_served_page_gets_the_current_style_in_place_of_an_older_copy_or_added() -> None:
    old = f"<!doctype html><head><style>p{{}}</style></head><body>{OLD_STYLE}<p>hi</p><script>x()</script></body>"
    served = convert_served_page_to_styled(old.encode()).decode()
    assert served == old.replace(OLD_STYLE, build_doc_style())
    assert convert_served_page_to_styled(served.encode()).decode() == served
    bare = convert_served_page_to_styled(b"<!doctype html><body><p>hi</p></body>").decode()
    assert bare == f"<!doctype html><body>{build_doc_style()}<p>hi</p></body>"
    for as_is in ("\u043f".encode("cp1251"), b"<body>" + b"x" * NAV_MAX_BYTES):
        assert convert_served_page_to_styled(as_is) == as_is


def test_registering_a_page_again_replaces_an_older_copy_of_the_style(tmp_path: Path) -> None:
    page = tmp_path / "index.html"
    page.write_text(f"<!doctype html><body>{OLD_STYLE}<p>hi</p></body>")
    assert add_doc_style_to_page(str(tmp_path)) == f"updated the shared doc style in {page}"
    assert page.read_text() == f"<!doctype html><body>{build_doc_style()}<p>hi</p></body>"


def test_tables_round_only_as_a_whole_and_every_colour_has_a_dark_value() -> None:
    css = (WEB / "doc-style.css").read_text()
    assert ":root:not(.ds-measure) :is(td,th):not([data-ds-skip] *){border-radius:0!important}" in css
    assert ":where(table){border-radius:12px;overflow:clip;" in css, "clip, not hidden: hidden stops the sticky header"
    rules = re.findall(r"([^{}]*)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", css, flags=re.S))
    rounded = [s for s, body in rules if re.search(r"\b(td|th)\b", s) and re.search(r"radius:(?!0)", body)]
    assert not rounded, "no cell is rounded"
    light, *darks = re.findall(r"\{(\s*--ds-fg:[^}]*)\}", css)

    def names(block: str) -> set[str]:
        return set(re.findall(r"(--ds-[\w-]+):", block))

    assert len(darks) == 2
    colours = {n for n in names(light) if n not in ("--ds-serif", "--ds-sans", "--ds-sticky-top")}
    assert all(colours <= names(dark) for dark in darks), colours - names(darks[0])


OWN = """
const toggled = {};
const h2 = (text) => ({ textContent: text, querySelector: () => null });
let heads = [];
globalThis.window = globalThis;
globalThis.addEventListener = () => {};
globalThis.location = { href: "http://127.0.0.1/", host: "127.0.0.1" };
globalThis.NodeFilter = { SHOW_TEXT: 4, FILTER_REJECT: 2, FILTER_ACCEPT: 1 };
globalThis.getComputedStyle = () => ({ backgroundColor: "rgb(255, 255, 255)", colorScheme: "normal" });
function run() {
  globalThis.document = {
    readyState: "complete",
    documentElement: { hasAttribute: () => false, setAttribute: () => {}, getAttribute: () => null,
      removeAttribute: () => {}, style: { setProperty: () => {} },
      classList: { add: () => {}, remove: () => {}, toggle: (name, on) => { toggled[name] = on; } } },
    body: { getElementsByTagName: () => [], appendChild: () => {} },
    styleSheets: [],
    createElement: () => ({ setAttribute: () => {}, addEventListener: () => {}, appendChild: () => {} }),
    querySelectorAll: (selector) => selector === "h2" ? heads : [],
    createTreeWalker: () => ({ nextNode: () => false }),
    getElementById: () => null,
  };
  eval(SCRIPT);
}
heads = [h2("In short"), h2("2026 plan")];
run();
if (toggled["ds-own-num"] !== false) throw new Error("plain headings lost their badges");
heads = [h2("1. In short"), h2("2. Plan")];
run();
if (toggled["ds-own-num"] !== true) throw new Error("self-numbered headings got badges too");
"""


def test_headings_the_doc_numbers_itself_get_no_badge(tmp_path: Path) -> None:
    run_node(tmp_path, f"const SCRIPT = {json.dumps((WEB / 'doc-style.js').read_text())};\n{OWN}")


def test_a_page_not_in_utf8_is_registered_unstyled(tmp_path: Path) -> None:
    page = tmp_path / "index.html"
    page.write_bytes('<meta charset="windows-1251"><body>\u043f\u0440\u0438\u0432\u0435\u0442</body>'.encode("cp1251"))
    before = page.read_bytes()
    assert add_doc_style_to_page(str(tmp_path)) == f"left {page} without the shared doc style: it is not UTF-8"
    assert page.read_bytes() == before


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
const started = Date.now();
renderInline("a".repeat(2000) + " " + "b-".repeat(1000));
eq(Date.now() - started < 200, true);
const card = node("div");
renderDecisionCard({ card: { intro: null, facts: [], pick_why: "see [plan](https://example.com/p)",
  options: [{ label: "Ship", cost: "see https://github.com/acme/web/pull/5", pick: true }] } }, card, () => {});
eq(find(card, (n) => n.tag === "a").length, 0);
eq(texts(card).join(" ").includes("acme/web#5"), true);
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


def test_a_pick_names_a_whole_answer() -> None:
    options = ["Ship", "Wait"]
    body = "My pick: Shipping now would be unsafe; wait."
    card = map_sections_to_card(map_body_to_sections(body, options), options)
    assert card is not None and not any(o["pick"] for o in card["options"])
    assert card["facts"] == [{"label": "My pick", "text": "Shipping now would be unsafe; wait."}]
    card = map_sections_to_card(map_body_to_sections("My pick: Ship: same risk.", options), options)
    assert card is not None and card["options"][0]["pick"] and card["pick_why"] == "same risk."


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


THEME = """
const attrs = {};
const media = (text) => ({ mediaText: text });
const dark = media("screen and (prefers-color-scheme: dark)"), print = media("print");
const nested = media("(prefers-color-scheme:light)");
const sheet = { media: media(""), cssRules: [{ media: dark, cssRules: [{ media: nested }] }, { media: print }] };
let stored = null;
globalThis.window = globalThis;
const onWindow = {};
globalThis.addEventListener = (name, fn) => { onWindow[name] = fn; };
globalThis.localStorage = { getItem: () => stored, setItem: (k, v) => { stored = v; } };
globalThis.location = { href: "http://127.0.0.1/", host: "127.0.0.1" };
globalThis.NodeFilter = { SHOW_TEXT: 4, FILTER_REJECT: 2, FILTER_ACCEPT: 1 };
globalThis.getComputedStyle = () => ({ backgroundColor: "rgb(255, 255, 255)", colorScheme: "normal" });
const listeners = {};
globalThis.document = {
  readyState: "loading",
  documentElement: { hasAttribute: () => false, setAttribute: (k, v) => { attrs[k] = v; }, getAttribute: () => null,
    removeAttribute: (k) => { delete attrs[k]; }, classList: { add: () => {}, remove: () => {}, toggle: () => {} } },
  body: null,
  styleSheets: [sheet],
  addEventListener: (name, fn) => { listeners[name] = fn; },
  querySelectorAll: () => [],
  dispatchEvent: () => true,
};
const check = (cond, what) => { if (!cond) throw new Error(`${what}: ${JSON.stringify([attrs, dark, nested])}`); };
stored = "light";
eval(SCRIPT);
check(attrs["data-theme"] === "light", "the stored choice sets data-theme before the body is parsed");
check(dark.mediaText === "screen and (max-width:0.001px)", "a dark-only rule is off under a forced light theme");
check(nested.mediaText === "(min-width:0px)", "a nested light-only rule is on under a forced light theme");
check(print.mediaText === "print", "other media rules are left alone");
stored = "auto";
onWindow.storage({ key: "bridge-theme", newValue: "auto" });
check(!("data-theme" in attrs), "auto, chosen in another tab, drops data-theme");
check(dark.mediaText === "screen and (prefers-color-scheme: dark)", "auto gives the page its own rule back");
check(nested.mediaText === "(prefers-color-scheme:light)", "auto gives the nested rule back");
onWindow.storage({ key: "bridge-theme", newValue: null });
check(attrs["data-theme"] === "light", "no choice at all is light, not the system's");
"""

BREAKS = """
const made = (tag) => ({ tag, children: [], className: "", textContent: "" });
const code = (text, cls) => ({
  textContent: text, children: [], parts: [], closest: () => null,
  classList: { contains: (c) => c === cls, add: function (c) { this.added = c; } },
  appendChild: function (n) { this.parts.push(n.tag === "wbr" ? "|" : n.tag ? n.textContent : n.text); },
});
const codes = [code("a_really_long_configuration_filename.py", "ds-path"),
  code("services/order_service/handlers/refund.py:120", "ds-path"), code("billing.invoices.render_summary_lines"),
  code("short_name"), code("x".repeat(40)), code("{agent_id, run_id, potential_issues_count}")];
globalThis.window = globalThis;
globalThis.addEventListener = () => {};
globalThis.location = { href: "http://127.0.0.1/", host: "127.0.0.1" };
globalThis.NodeFilter = { SHOW_TEXT: 4, FILTER_REJECT: 2, FILTER_ACCEPT: 1 };
globalThis.getComputedStyle = () => ({ backgroundColor: "rgb(255, 255, 255)", colorScheme: "normal" });
globalThis.document = {
  readyState: "complete",
  documentElement: { hasAttribute: () => false, setAttribute: () => {}, getAttribute: () => null,
    removeAttribute: () => {}, style: { setProperty: () => {} }, classList: { add: () => {}, remove: () => {},
      toggle: () => {}, contains: () => false } },
  body: { getElementsByTagName: () => [], appendChild: () => {} },
  styleSheets: [],
  querySelectorAll: (selector) => selector === "code" ? codes : [],
  createElement: (tag) => Object.assign(made(tag), { setAttribute: () => {}, addEventListener: () => {},
    appendChild: () => {} }),
  createTextNode: (text) => ({ text }),
  createTreeWalker: () => ({ nextNode: () => false }),
  getElementById: () => null,
};
eval(SCRIPT);
const got = codes.map((c) => c.parts.join(""));
const want = ["", "services/|order_service/|handlers/|refund.py:120", "billing.|invoices.|render_summary_lines", "",
  "", "{agent_id, |run_id, |potential_issues_count}"];
if (JSON.stringify(got) !== JSON.stringify(want)) throw new Error(JSON.stringify(got));
"""


def test_long_code_wraps_only_at_a_space_slash_or_dot_never_inside_a_name(tmp_path: Path) -> None:
    run_node(tmp_path, f"const SCRIPT = {json.dumps((WEB / 'doc-style.js').read_text())};\n{BREAKS}")


def test_a_stored_theme_applies_before_the_page_paints(tmp_path: Path) -> None:
    run_node(tmp_path, f"const SCRIPT = {json.dumps((WEB / 'doc-style.js').read_text())};\n{THEME}")
    script = (WEB / "doc-style.js").read_text()
    assert script.index("applyTheme(chosen);") < script.index("function start()"), "not deferred to load"
    uses = [line for line in script.splitlines() if "localStorage" in line]
    assert uses and all("try {" in line for line in uses), "storage can throw in a private window"


def test_names_in_code_never_break_inside_and_text_reads_large() -> None:
    css = re.sub(r"/\*.*?\*/", "", (WEB / "doc-style.css").read_text(), flags=re.S)
    nav = (Path(__file__).parent.parent / "src/bridge/doc_nav.py").read_text()
    assert not re.search(r"word-break:\s*break-all", css)
    anywhere = [m.strip() for m in re.findall(r"([^{}]*)\{[^}]*overflow-wrap:\s*anywhere", css)]
    assert anywhere == ["code.ds-long .ds-seg"]
    assert ":not(pre)>code{overflow-wrap:anywhere}" not in nav and "code{overflow-wrap:anywhere}" not in nav
    code = ":not(pre)>code{font-size:.88em!important;white-space:nowrap!important;overflow-wrap:normal!important;"
    assert code in css
    assert "body{font-size:18px!important}" in css


def test_a_served_page_is_told_the_kept_theme_before_the_shared_style() -> None:
    from bridge.doc_nav import DOC_STYLE_ID, convert_page_to_themed

    page = f'<body><p>x</p><style id="{DOC_STYLE_ID}">a{{}}</style>'.encode()

    themed = convert_page_to_themed(page, "dark")

    assert themed.index(b'window.bridgeTheme="dark"') < themed.index(DOC_STYLE_ID.encode())
    assert convert_page_to_themed(page, None) == page
    assert convert_page_to_themed(page, '"><script>') == page
    assert convert_page_to_themed(b"<body>no shared style</body>", "dark") == b"<body>no shared style</body>"


SAVED = b"<!doctype html><html lang='en'><head><title>t</title></head><body><h2>A</h2><p>x</p><h2>B</h2></body></html>"


def test_a_copy_saved_from_a_served_page_or_doc_is_served_like_the_original() -> None:
    from bridge.doc_nav import convert_page_to_themed

    def page(raw: bytes, theme: str) -> bytes:
        return convert_page_to_themed(convert_served_page_to_styled(raw), theme)

    def doc(raw: bytes, theme: str) -> bytes:
        return convert_page_to_themed(convert_doc_to_viewable(raw), theme)

    assert page(page(SAVED, "light"), "dark") == page(SAVED, "dark")
    with_ids = SAVED.replace(b"<h2>A", b'<h2 id="a">A').replace(b"<h2>B", b'<h2 id="b">B')
    assert page(doc(SAVED, "light"), "dark") == page(with_ids, "dark"), "a review doc saved and registered as a page"
    assert doc(doc(SAVED, "light"), "dark") == doc(SAVED, "dark")
    served = page(SAVED, "dark")
    for part in (f'id="{DOC_STYLE_ID}"', "window.bridgeTheme=", f'id="{VIEW_ID}"', 'id="bridge-doc-nav"'):
        assert served.count(part.encode()) == (1 if part in (f'id="{DOC_STYLE_ID}"', "window.bridgeTheme=") else 0)


def test_a_copy_saved_from_the_browser_loses_the_marks_that_keep_the_style_from_running() -> None:
    from bridge.doc_nav import BEHIND_BANNER_STYLE, remove_injected_parts

    live = (
        '<!doctype html><html data-viewer-terms="" lang="en" class="acme ds-light bdv-light ds-cs" data-theme="light" '
        f'data-ds-done=""><head></head><body><div style="{BEHIND_BANNER_STYLE}">doc older than code</div><p>x</p>'
        '<div id="ds-theme" role="group" data-ds-skip=""><button>Light</button></div></body></html>'
    )
    bare, found = remove_injected_parts(live)
    assert bare == '<!doctype html><html lang="en" class="acme"><head></head><body><p>x</p></body></html>'
    assert found == ["the marks on <html>", "the behind-the-PR line", "the theme switch"]
    padded = (
        '<html data-ds-done="" style="padding-left: 268px; box-sizing: border-box; color: red"><body></body></html>'
    )
    assert remove_injected_parts(padded)[0] == '<html style="color: red"><body></body></html>', "the bar's gutter goes"
    own = '<!doctype html><html data-theme="dark"><body><pre>&lt;nav id="bridge-doc-nav"&gt;</pre></body></html>'
    assert remove_injected_parts(own) == (own, []), "a page's own data-theme, and markup shown as text, stay"


def test_only_what_bridge_added_is_removed_never_the_pages_own_markup_or_scripts() -> None:
    from bridge.doc_nav import remove_injected_parts

    for own in (
        '<textarea><nav id="bridge-doc-nav">Authored example</nav></textarea>',
        '<template><div id="ds-theme">An example</div></template>',
        '<script>var shown = "<nav id=\\"bridge-doc-nav\\">x</nav><div id=\\"ds-theme\\"></div>";</script>',
        '<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>'
        "<script>hljs.highlightAll();</script>",
        "<p>Set <code>window.bridgeTheme</code> to pick one.</p><script>window.bridgeTheme = pick();</script>",
    ):
        page = f"<!doctype html><html lang='en'><body>{own}</body></html>"
        assert remove_injected_parts(page) == (page, []), own
    added = (
        '<head><script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.11.1/highlight.min.js" '
        'data-bridge-added="highlighter"></script></head><body><div class="ds-scroll" data-bridge-wrap="">'
        '<table data-ds-widened="" style="width: 100%;"><tr><td><code class="ds-path ds-long"><span class="ds-seg '
        'ds-head">potential_</span><wbr><span class="ds-seg">issues.py</span></code></td></tr></table></div>'
        '<p class="lead bdv-in2">x</p></body>'
    )
    bare, found = remove_injected_parts(added)
    assert bare == (
        '<head></head><body><table><tr><td><code class="ds-path">potential_issues.py</code></td></tr></table>'
        '<p class="lead">x</p></body>'
    )
    assert found == ["the code highlighter", "the scroll boxes", "the widened tables", "the code line breaks",
        "the layout classes"]  # fmt: skip


def test_registering_a_saved_served_copy_keeps_one_current_style_and_says_what_it_removed(tmp_path: Path) -> None:
    from bridge.doc_nav import convert_page_to_themed

    page = tmp_path / "index.html"
    page.write_bytes(convert_page_to_themed(convert_doc_to_viewable(SAVED), "dark"))
    assert add_doc_style_to_page(str(tmp_path)) == (
        f"updated the shared doc style in {page}; removed what bridge adds when serving it: the marks on <html>, "
        "the contents bar, the viewer, the theme choice"
    )
    assert page.read_bytes() == convert_served_page_to_styled(SAVED).replace(b"<h2>A", b'<h2 id="a">A').replace(
        b"<h2>B", b'<h2 id="b">B'
    )


def test_old_unmarked_scripts_are_known_by_what_they_hold_never_by_where_they_sit() -> None:
    from bridge.doc_nav import remove_injected_parts

    style = '<style id="bridge-doc-style">p{}</style>'
    style_script = "<script>/* Shared doc style: highlights code blocks. */\n(function () {})();</script>"
    view = f'<style id="{VIEW_ID}">.x{{}}</style>'
    view_script = "<script>document.documentElement.setAttribute('data-viewer-terms','');(function(){})();</script>"
    saved = (
        f"<!doctype html><html data-viewer-terms=\"\" lang='en'><body>{style}\n{style_script}<p>x</p>{view}"
        f"<script>author()</script>\n{view_script}</body></html>"
    )
    bare, found = remove_injected_parts(saved)
    assert bare == "<!doctype html><html lang='en'><body>\n<p>x</p><script>author()</script>\n</body></html>"
    assert found == ["the marks on <html>", "the shared doc style", "the viewer"]
