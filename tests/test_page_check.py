import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import VENV_BIN, BridgeEnv, SpawnProcess
from test_leads import Team
from test_review_doc_check import baseline

from bridge.doc_nav import convert_doc_to_viewable, convert_page_to_themed, convert_served_page_to_styled
from bridge.page_check import (
    BROWSER_ENV,
    check_rendered,
    find_browser,
    find_layout_problems,
    find_render_problems,
    find_text_problems,
    render_dom,
    render_probe,
    render_reading,
)
from bridge.review_doc_check import find_day_estimates

LONG = "The export job reads each tenant's orders and writes one file per day for the Acme team. " * 12


@pytest.fixture
def browser(monkeypatch: pytest.MonkeyPatch) -> str:
    # On a Mac a render can open a system dialog on the user's screen; run these where nobody works, or opt in.
    if sys.platform == "darwin" and os.environ.get("BRIDGE_RENDER_TESTS") != "1":
        pytest.skip("rendering tests are off on macOS; set BRIDGE_RENDER_TESTS=1 to run them")
    monkeypatch.delenv(BROWSER_ENV, raising=False)
    found = find_browser()
    if found is None:
        pytest.skip("no headless Chrome or Chromium on this host")
    return found


def page(body: str, head: str = "") -> bytes:
    return f"<!doctype html><html><head><title>t</title>{head}</head><body>{body}</body></html>".encode()


def read(raw: bytes, browser: str, theme: str = "light") -> dict[str, Any]:
    return render_probe(convert_page_to_themed(convert_served_page_to_styled(raw), theme), None, browser)


def test_time_estimates_are_found_and_sizes_in_kloc_are_not() -> None:
    for text, found in [
        ("about 3 engineer-days", "3 engineer-days"),
        ("5 person-days in all", "5 person-days"),
        ("It would take 2 weeks for one person", "2 weeks for one person"),
        ("Roughly two weeks for a single engineer", "two weeks for a single engineer"),
        ("about 3-4 days of work", "3-4 days of work"),
        ("10 engineer weeks", "10 engineer weeks"),
        ("It needs 2 weeks per person.", "2 weeks per person"),
        ("Estimate: a week for one engineer.", "a week for one engineer"),
        ("No blockers, implementation takes 3 engineer-days.", "3 engineer-days"),
        ("The feature needs 1 kLOC and 3 engineer-days.", "3 engineer-days"),
        ("The old approach was slow, the rewrite needs 3 engineer-days.", "3 engineer-days"),
        ("Estimate: 3 engineering days", "3 engineering days"),
        ("~7.5-10 eng-weeks", "7.5-10 eng-weeks"),
        ("5 weeks for one person", "5 weeks for one person"),
        ("It is 5 weeks for one person.", "5 weeks for one person"),
        ("It needs one person for 2 weeks.", "one person for 2 weeks"),
        ("one engineer for two weeks", "one engineer for two weeks"),
        ("It takes a sprint.", "takes a sprint"),
        ("Estimate: 3 days", "Estimate: 3 days"),
    ]:
        assert find_day_estimates(text) == [found], text
    for text in (
        "about 1.5 kLOC",
        "the job runs for 3 days",
        "a 2-day window",
        "in two weeks we ship",
        "a day to review",
        "Yesterday the restore lost 3 days of work.",
        "On-call coverage: 2 weeks for one engineer, then rotate.",
        "On-call coverage will be 2 weeks for one engineer, then rotate.",
        "We lost 3 days of work and will restore the files today.",
        "Use kLOC, never engineer-days.",
        "We spent 4 engineer-days on it last sprint.",
        "2 weeks per person",
        "We saw it 3 days ago.",
        "The 5-day retention applies.",
        "Fix it within 2 weeks of release.",
        "A sprint planning meeting is on Monday.",
        "It took a sprint to ship.",
        "The Estimate column lists the issues.",
    ):
        assert find_day_estimates(text) == [], text


def test_a_page_that_sizes_in_days_is_flagged_but_not_for_words_in_its_scripts() -> None:
    assert find_text_problems(page("<p>It is 4 engineer-days.</p>")) == [
        "it sizes work in time ('4 engineer-days'); the team sizes work in kLOC, never in days or weeks of someone's "
        "time: say how many kLOC it adds or changes"
    ]
    assert find_text_problems(page("<p>It adds 1.2 kLOC.</p><script>var a = '4 engineer-days';</script>")) == []
    # Words of one block never excuse an estimate in the next.
    assert find_text_problems(page("<ul><li>Not blocked</li><li>3 engineer-days</li></ul>")) != []


def reading(**parts: Any) -> dict[str, Any]:
    base = {"theme": "light", "page": "#ffffff", "column": {"left": 250, "right": 1350, "window": 1600, "page": 1600}}
    return base | parts


def test_a_reading_with_a_full_window_column_a_mismatched_diagram_and_faint_text_is_flagged() -> None:
    area = {
        "label": "Order flow",
        "heading": "How it works",
        "samples": 9,
        "off": 9,
        "bg": "#f4f4f4",
        "texts": [{"text": f"step {n}", "ratio": 1.4, "fg": "#d8d8d8", "bg": "#f4f4f4"} for n in range(5)],
    }
    wide = reading(column={"left": 8, "right": 1590, "window": 1600, "page": 1700}, areas=[area])
    problems = find_layout_problems(wide) + find_render_problems(wide, "light")
    assert [p.split(";")[0] for p in problems] == [
        "its main column spans 1582 of a 1600 px window",
        "the page scrolls sideways (1700 px wide in a 1600 px window)",
        "rendered light: the diagram 'Order flow' under 'How it works' has its own background (#f4f4f4) on a #ffffff "
        "page",
        "rendered light: text in the diagram 'Order flow' under 'How it works' is hard to read, under 2.5:1 contrast: "
        "'step 0' 1.4:1 (#d8d8d8 on #f4f4f4)",
    ]
    assert "and 2 more" in problems[3]


def test_a_reading_of_a_centred_column_and_a_themed_diagram_passes() -> None:
    area: dict[str, Any] = {"label": "Order flow", "heading": "", "samples": 9, "off": 4, "bg": "#f4f4f4", "texts": []}
    assert find_layout_problems(reading(areas=[area])) == find_render_problems(reading(areas=[area]), "light") == []
    assert find_render_problems(reading(areas=[area]), "dark") == []
    assert find_render_problems({"error": "Chrome did not render it"}, "dark") == [
        "rendered dark: the check could not read the page (Chrome did not render it)"
    ]


def test_a_page_with_no_width_of_its_own_reads_in_a_centred_column(browser: str) -> None:
    table = "<table><tr><th>Field</th><th>Meaning</th></tr><tr><td>order_id</td><td>The order.</td></tr></table>"
    got = read(page(f"<h1>Orders</h1><p>{LONG}</p>{table}"), browser)
    column = got["column"]
    assert column["right"] - column["left"] <= 1100 and column["page"] == column["window"] == 1600, got
    assert abs((column["left"] + column["right"]) / 2 - 800) <= 2, "centred"
    assert find_layout_problems(got) == find_render_problems(got, "light") == []


def test_a_page_that_takes_the_whole_window_is_flagged(browser: str) -> None:
    raw = page(f"<h1>Orders</h1><p>{LONG}</p>", "<style>body{max-width:none} p{max-width:none}</style>")
    problems = find_layout_problems(read(raw, browser))
    assert len(problems) == 1 and problems[0].startswith("its main column spans"), problems


BAD_SVG = (
    '<svg viewBox="0 0 600 200" width="600" height="200" aria-label="Order flow">'
    '<rect width="600" height="200" fill="#f4f4f4"/><text x="20" y="100" fill="#222222">Checkout</text></svg>'
)
THEMED_SVG = (
    '<svg viewBox="0 0 600 200" width="600" height="200" aria-label="Order flow">'
    '<rect width="600" height="200" style="fill:var(--ds-tint)"/>'
    '<text x="20" y="100" style="fill:currentColor">Checkout</text></svg>'
)


def test_a_diagram_with_a_fixed_light_background_is_flagged_dark_only(browser: str) -> None:
    raw = page(f"<h2>How it works</h2>{BAD_SVG}")
    assert find_render_problems(read(raw, browser, "light"), "light") == []
    dark = read(raw, browser, "dark")
    assert dark["theme"] == "dark"
    problems = find_render_problems(dark, "dark")
    assert len(problems) == 1 and problems[0].startswith(
        "rendered dark: the diagram 'Order flow' under 'How it works' has its own background (#f4f4f4)"
    ), problems


def test_a_diagram_coloured_by_the_theme_passes_light_and_dark(browser: str) -> None:
    raw = page(f"<h2>How it works</h2>{THEMED_SVG}")
    for theme in ("light", "dark"):
        assert find_render_problems(read(raw, browser, theme), theme) == [], theme


def test_faint_diagram_text_is_flagged(browser: str) -> None:
    raw = page(THEMED_SVG.replace("fill:currentColor", "fill:#e4e4e4"))
    problems = find_render_problems(read(raw, browser, "light"), "light")
    assert len(problems) == 1 and "'Checkout'" in problems[0] and "hard to read" in problems[0], problems


def haloed_label(stroke_width: float) -> bytes:
    """A chart's last-value label as a weight log draws it: over a bar, ringed in the card's colour."""
    return page(
        '<h2>Trend</h2><svg viewBox="0 0 600 200" width="600" height="200" aria-label="Weight">'
        '<rect x="0" y="0" width="600" height="200" fill="#ffffff"/>'
        '<rect x="200" y="40" width="160" height="160" fill="#0b8a6e"/>'
        '<text x="280" y="80" text-anchor="middle" font-size="14" font-weight="600" fill="#c34f33" stroke="#ffffff" '
        f'stroke-width="{stroke_width}" paint-order="stroke">94,0</text></svg>'
    )


def test_text_ringed_by_a_wide_stroke_is_read_against_the_stroke(browser: str) -> None:
    assert find_render_problems(read(haloed_label(3.5), browser, "light"), "light") == []


def test_text_with_a_thin_stroke_is_still_read_against_what_lies_under_it(browser: str) -> None:
    problems = find_render_problems(read(haloed_label(1), browser, "light"), "light")
    assert len(problems) == 1 and "'94,0'" in problems[0] and "hard to read" in problems[0], problems


MEASURE_TABLE = """
var box = document.querySelector(".wrap"), t = box.querySelector("table"), cs = getComputedStyle(box);
window.addEventListener("load", function () { setTimeout(function () {
  var s = document.createElement("script"); s.type = "application/json"; s.id = "bridge-probe-out";
  s.textContent = JSON.stringify({ scrolls: box.scrollWidth - box.clientWidth, table: t.offsetWidth,
    room: box.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight),
    right: box.getBoundingClientRect().right, window: document.documentElement.clientWidth });
  document.body.appendChild(s); }, 300); });
"""


def test_a_table_wider_than_its_column_by_less_than_the_padding_does_not_scroll(browser: str) -> None:
    raw = page(
        '<main><div class="wrap"><table><tr><td style="padding:0"><div style="width:626px;height:10px"></div></td>'
        "</tr></table></div></main>",
        "<style>main{max-width:640px;margin:0 auto}.wrap{overflow-x:auto;padding:10px}</style>",
    )
    got = render_reading(convert_served_page_to_styled(raw), None, browser, MEASURE_TABLE)
    assert got["scrolls"] <= 0 and got["table"] == got["room"] and got["room"] >= 626, got
    assert got["right"] <= got["window"] - 24, json.dumps(got)


def test_a_post_that_sizes_in_days_is_sent_with_a_warning_and_a_review_doc_is_refused(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    days = bridge_env.cli("post", "insight", "Export plan", "--body", "About 3 engineer-days.", pid=team.a)
    assert days.returncode == 0 and "warning: it sizes work in time ('3 engineer-days')" in days.stderr, days.stderr
    assert "kLOC" in days.stderr
    told = bridge_env.cli("tell-user", "Export plan: about 2 weeks for one person", pid=team.a)
    assert told.returncode == 0 and "warning: it sizes work in time ('2 weeks for one person')" in told.stderr
    kloc = bridge_env.cli("post", "insight", "Export plan", "--body", "About 1.5 kLOC.", pid=team.a)
    assert kloc.returncode == 0 and "warning" not in kloc.stderr, kloc.stderr
    assert [p for p in baseline("<p>It is 5 person-days.</p>") if "kLOC" in p] == [
        "it sizes work in time ('5 person-days'); the team sizes work in kLOC, never in days or weeks of someone's "
        "time: say how many kLOC it adds or changes"
    ]
    assert [p for p in baseline("<p>It adds 1.5 kLOC.</p>") if "kLOC" in p] == []


def test_page_check_lists_problems_per_page_and_says_when_it_could_not_render(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, tmp_path: Path
) -> None:
    pid = spawn_process()
    bridge_env.write_session_file(pid, "conv-check")
    for slug, words in (("plan", "It is 4 engineer-days."), ("notes", "It adds 1.2 kLOC.")):
        (tmp_path / slug).mkdir()
        (tmp_path / slug / "index.html").write_bytes(page(f"<p>{words}</p>"))
        added = bridge_env.cli("page", "add", slug, "--title", slug, "--source", str(tmp_path / slug), pid=pid)
        assert added.returncode == 0, added.stderr
        assert ("warning: it sizes work in time" in added.stderr) == (slug == "plan"), added.stderr
    done = bridge_env.cli("page", "check", "--all", pid=pid)
    assert done.returncode == 1
    assert "notes: nothing found in its words" in done.stdout and "plan: it sizes work in time" in done.stderr
    assert "notes: no Chrome or Chromium found (set BRIDGE_BROWSER), so it was not rendered" in done.stderr
    assert bridge_env.cli("page", "check", "notes", pid=pid).returncode == 0
    assert bridge_env.cli("page", "check", pid=pid).returncode == 2


MEASURE_BREAKS = """
window.addEventListener("load", function () { setTimeout(function () {
  var after = [];
  [].forEach.call(document.querySelectorAll("code"), function (c) {
    var walker = document.createTreeWalker(c, NodeFilter.SHOW_TEXT), range = document.createRange();
    var node, top = null, last = "";
    while ((node = walker.nextNode())) {
      for (var i = 0; i < node.length; i++) {
        range.setStart(node, i); range.setEnd(node, i + 1);
        var r = range.getClientRects()[0];
        if (!r) continue;
        if (top !== null && r.top > top + 4) after.push(last);
        top = r.top; last = node.data[i];
      }
    }
  });
  var s = document.createElement("script"); s.type = "application/json"; s.id = "bridge-probe-out";
  s.textContent = JSON.stringify({ after: after }); document.body.appendChild(s); }, 300); });
"""


def test_long_code_in_a_narrow_column_breaks_only_after_a_slash_dot_or_space(browser: str) -> None:
    cells = "".join(
        f'<tr><td style="width:220px"><code>{code}</code></td><td>{LONG}</td></tr>'
        for code in (
            "targetgroup/eks-prod-staging-tg/3f9a1c7e2b4d6a80",
            "services/order_service/potential_issues.py",
            "{agent_id, run_id, potential_issues_count}",
        )
    )
    got = render_reading(convert_served_page_to_styled(page(f"<table>{cells}</table>")), None, browser, MEASURE_BREAKS)
    assert got["after"] and set(got["after"]) <= {"/", ".", " "}, got


MEASURE_PAGE = """
window.addEventListener("load", function () { setTimeout(function () {
  var s = document.createElement("script"); s.type = "application/json"; s.id = "bridge-probe-out";
  s.textContent = JSON.stringify({ page: document.documentElement.scrollWidth,
    window: document.documentElement.clientWidth });
  document.body.appendChild(s); }, 300); });
"""

NARROW_NAMES = (
    "<p>The file <code>tests/test_issue_discovery_potential_issue_store_transactions.py</code> and the name "
    "<code>target_status_band_attempts_score_conversation</code> and <code>user_management_service.py:274-296</code>"
    " are read here.</p>"
)


def test_long_code_never_stretches_a_narrow_page(browser: str) -> None:
    got = render_reading(convert_served_page_to_styled(page(NARROW_NAMES)), None, browser, MEASURE_PAGE, (390, 800))
    assert got["page"] == got["window"], got


def test_long_code_does_not_break_inside_a_name_on_a_wide_page(browser: str) -> None:
    raw = page(NARROW_NAMES)
    got = render_reading(convert_served_page_to_styled(raw), None, browser, MEASURE_BREAKS, (1440, 900))
    assert set(got["after"]) <= {"/", ".", " "}, got


MEASURE_WIDE = """
window.addEventListener("load", function () { setTimeout(function () {
  var t = document.querySelector("table"), box = t.parentElement, r = box.getBoundingClientRect();
  var s = document.createElement("script"); s.type = "application/json"; s.id = "bridge-probe-out";
  s.textContent = JSON.stringify({ scrolls: box.scrollWidth - box.clientWidth, left: r.left, right: r.right,
    page: document.documentElement.scrollWidth, window: document.documentElement.clientWidth });
  document.body.appendChild(s); }, 300); });
"""


MEASURE_BAR = """
window.addEventListener("load", function () { setTimeout(function () {
  var out = document.getElementById("bridge-probe-out"), got = JSON.parse(out.textContent);
  got.bar = document.getElementById("bridge-doc-nav").getBoundingClientRect().right;
  out.textContent = JSON.stringify(got); }, 400); });
"""


@pytest.mark.parametrize("wide", [1300, 2000])
def test_a_table_wider_than_the_container_grows_to_the_window_and_scrolls_only_past_it(browser: str, wide: int) -> None:
    table = f'<table><tr><td style="padding:0"><div style="width:{wide}px;height:10px"></div></td></tr></table>'
    raw = page(f"<h2>Fields</h2><p>{LONG}</p><h2>Rows</h2>{table}")
    got = render_reading(convert_served_page_to_styled(raw), None, browser, MEASURE_WIDE, (1440, 900))
    assert got["page"] == got["window"] == 1440, got
    assert got["left"] >= 24 and got["right"] <= 1440 - 24, got
    if wide < 1440:
        assert got["scrolls"] <= 0, f"a {wide}px table shows whole: {got}"
    else:
        assert got["scrolls"] > 0, f"a {wide}px table scrolls in its own box: {got}"
    doc = render_reading(convert_doc_to_viewable(raw), None, browser, MEASURE_WIDE + MEASURE_BAR, (1440, 900))
    assert doc["page"] == 1440 and doc["left"] >= doc["bar"] + 24 and doc["right"] <= 1440 - 24, doc


@pytest.mark.parametrize("window", [600, 900])
def test_a_widened_table_reaches_the_text_columns_edges_when_the_body_fills_the_window(
    browser: str, window: int
) -> None:
    table = '<table><tr><td style="padding:0"><div style="width:1500px;height:10px"></div></td></tr></table>'
    raw = page(f"<h2>Fields</h2><p>{LONG}</p><h2>Rows</h2>{table}")
    got = render_reading(convert_served_page_to_styled(raw), None, browser, MEASURE_WIDE, (window, 900))
    assert got["left"] == 16 and got["right"] == window - 16, got


def test_a_widened_table_beside_the_contents_bar_reaches_the_columns_right_edge_at_900(browser: str) -> None:
    table = '<table><tr><td style="padding:0"><div style="width:1500px;height:10px"></div></td></tr></table>'
    raw = page(f"<h2>Fields</h2><p>{LONG}</p><h2>Rows</h2>{table}")
    doc = render_reading(convert_doc_to_viewable(raw), None, browser, MEASURE_WIDE + MEASURE_BAR, (900, 900))
    assert doc["right"] == 900 - 16 and doc["left"] >= doc["bar"] + 24, doc


def small_text_svg(css: str, text: str) -> bytes:
    return page(
        f'<h2>Flow</h2><svg viewBox="0 0 600 200" width="600" height="200" aria-label="Flow">{css}'
        f'<rect width="600" height="200" style="fill:var(--ds-tint)"/>{text}</svg>'
    )


@pytest.mark.parametrize(
    ("body", "small"),
    [
        ('<style>.t{font-size:10px}</style><text class="t" x="20" y="100">a</text>', True),
        ('<style>svg{font-size:9px}</style><text x="20" y="100">a</text>', True),
        ('<g font-size="8"><text x="20" y="100">a</text></g>', True),
        ('<text x="20" y="100" style="font-size:9pt">a</text>', True),
        ('<text x="20" y="100" style="font-size:var(--s,10px)">a</text>', True),
        ('<text x="20" y="100" font-size="14">a</text>', False),
        ('<text x="20" y="100" style="font-size:12pt">a</text>', False),
    ],
)
def test_diagram_text_drawn_under_14px_on_screen_is_flagged_whatever_sets_its_size(
    browser: str, body: str, small: bool
) -> None:
    problems = find_render_problems(read(small_text_svg("", body), browser), "light")
    assert [("drawn under 14px" in p) for p in problems] == ([True] if small else []), problems


def test_diagram_text_scaled_down_by_its_viewbox_is_flagged(browser: str) -> None:
    raw = page(
        '<h2>Flow</h2><svg viewBox="0 0 2400 800" width="600" height="200" aria-label="Flow">'
        '<rect width="2400" height="800" style="fill:var(--ds-tint)"/>'
        '<text x="20" y="100" font-size="20">a</text></svg>'
    )
    problems = find_render_problems(read(raw, browser), "light")
    assert len(problems) == 1 and "'a' 5px" in problems[0], problems


def test_a_layout_fault_only_the_dark_page_shows_is_told_as_dark(monkeypatch: pytest.MonkeyPatch) -> None:
    import bridge.page_check as page_check

    wide = reading(column={"left": 250, "right": 1350, "window": 1600, "page": 1800})

    def only_dark(page: bytes, assets: Path | None, browser: str) -> dict[str, Any]:
        return wide if page == b"dark" else reading()

    def both(page: bytes, assets: Path | None, browser: str) -> dict[str, Any]:
        return wide

    monkeypatch.setattr(page_check, "render_probe", only_dark)
    problems, skipped = check_rendered(lambda theme: theme.encode(), None, "chrome")
    assert skipped is None and [p.split(";")[0] for p in problems] == [
        "rendered dark: the page scrolls sideways (1800 px wide in a 1600 px window)"
    ]
    monkeypatch.setattr(page_check, "render_probe", both)
    assert len(check_rendered(lambda theme: theme.encode(), None, "chrome")[0]) == 1, "told once when both show it"


MODULE_READ = """
window.addEventListener("load", function () { setTimeout(function () {
  var s = document.createElement("script"); s.type = "application/json"; s.id = "bridge-probe-out";
  s.textContent = JSON.stringify({ built: document.getElementById("t").textContent });
  document.body.appendChild(s); }, 300); });
"""


def test_a_module_script_beside_the_page_runs_during_the_check(browser: str, tmp_path: Path) -> None:
    (tmp_path / "diagram.js").write_text('document.getElementById("t").textContent = "built";')
    raw = page('<div id="t"></div><script type="module" src="./diagram.js"></script>')
    assert render_reading(raw, tmp_path, browser, MODULE_READ) == {"built": "built"}


def test_the_check_reaches_only_its_own_server_never_another_local_port(browser: str, tmp_path: Path) -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    hits: list[str] = []

    class Other(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    other = ThreadingHTTPServer(("127.0.0.1", 0), Other)
    threading.Thread(target=other.serve_forever, daemon=True).start()
    port = other.server_address[1]
    (tmp_path / "own.txt").write_text("own")
    raw = page(
        f'<img src="http://127.0.0.1:{port}/img.png"><img src="http://localhost:{port}/img2.png">'
        f'<script>fetch("http://127.0.0.1:{port}/fetch").catch(function () {{}});</script>'
        '<script>fetch("./own.txt").then(function (r) { return r.text(); }).then(function (t) {'
        'document.body.setAttribute("data-own", t); });</script>'
    )
    try:
        dom, error = render_dom(raw, tmp_path, browser)
    finally:
        other.shutdown()
        other.server_close()
    assert error is None and 'data-own="own"' in dom, dom[-400:]
    assert hits == [], hits


def test_a_copy_the_browser_saved_is_served_again_as_the_original_was(browser: str) -> None:
    from bridge.doc_nav import remove_injected_parts

    table = '<table><tr><td style="padding:0"><div style="width:1300px;height:10px"></div></td></tr></table>'
    raw = page(
        f"<h2>Fields</h2><p>{LONG} <code>services/order_service/potential_issues.py</code></p>"
        f'<p><span class="term" data-def="One pass of the export.">run</span></p><h2>Rows</h2>{table}'
    ).replace(b"<html>", b'<html lang="en" style="color: #222">')
    served = convert_page_to_themed(convert_doc_to_viewable(raw), "light")
    saved, error = render_dom(served, None, browser, (1440, 900))
    assert error is None
    for mark in ("data-ds-done", 'id="ds-theme"', "data-ds-widened", "data-bridge-wrap", "ds-long", "<wbr>", "bdv-in"):
        assert mark in saved, f"the fixture holds what the live page carries: {mark}"
    bare, _ = remove_injected_parts(saved)
    for mark in ("data-ds-done", 'id="ds-theme"', "data-ds-widened", "data-bridge-wrap", "ds-long", "<wbr>", "bdv-in",
        "data-bridge-added", "data-bridge-root", "padding-left", "ds-center"):  # fmt: skip
        assert mark not in bare, mark
    assert '<html lang="en" style="color: #222">' in bare
    again = render_reading(
        convert_page_to_themed(convert_doc_to_viewable(bare.encode()), "dark"), None, browser, MEASURE_WIDE, (1440, 900)
    )
    first = render_reading(convert_page_to_themed(served, "dark"), None, browser, MEASURE_WIDE, (1440, 900))
    assert again == first, (again, first)


MEASURE_BODY = """
window.addEventListener("load", function () { setTimeout(function () {
  var r = document.body.getBoundingClientRect(), s = document.createElement("script");
  s.type = "application/json"; s.id = "bridge-probe-out";
  s.textContent = JSON.stringify({ left: Math.round(r.left), right: Math.round(r.right),
    page: document.documentElement.scrollWidth, window: document.documentElement.clientWidth });
  document.body.appendChild(s); }, 300); });
"""


@pytest.mark.parametrize(
    ("own", "left"),
    [("", 250), ("body{width:100%;margin:0}", 250), ("body{margin:0}", 250), ("body{margin:0 0 0 300px}", 300)],
)
def test_the_container_keeps_a_pages_own_body_width_and_margins(browser: str, own: str, left: int) -> None:
    raw = page(f"<p>{LONG}</p>", f"<style>{own}</style>")
    got = render_reading(convert_served_page_to_styled(raw), None, browser, MEASURE_BODY)
    assert got["page"] == got["window"] == 1600 and got["left"] == left, (own, got)


def fake_browser(tmp_path: Path, body: str) -> str:
    script = tmp_path / "browser"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    return str(script)


def test_the_browser_never_asks_macos_for_a_keychain(tmp_path: Path) -> None:
    args = tmp_path / "args"
    render_dom(b"<p>hi</p>", None, fake_browser(tmp_path, f"printf '%s\\n' \"$@\" > {args}; echo '<html></html>'"))

    command = args.read_text().splitlines()
    assert "--use-mock-keychain" in command and "--password-store=basic" in command


def test_the_browser_goes_direct_only_to_its_own_server_over_http(tmp_path: Path) -> None:
    args = tmp_path / "args"
    render_dom(b"<p>hi</p>", None, fake_browser(tmp_path, f"printf '%s\\n' \"$@\" > {args}; echo '<html></html>'"))

    [bypass] = [a for a in args.read_text().splitlines() if a.startswith("--proxy-bypass-list=")]
    [served] = [a for a in args.read_text().splitlines() if a.startswith("http://127.0.0.1:")]
    assert bypass == f"--proxy-bypass-list=<-loopback>;{served.rstrip('/')}"


def start_browser_with_a_helper(tmp_path: Path) -> tuple[str, Path]:
    """A browser that starts a helper process and then hangs, as Chrome's renderers outlive a stuck Chrome."""
    helper = tmp_path / "helper.pid"
    return fake_browser(tmp_path, f"sleep 300 & echo $! > {helper}.tmp; mv {helper}.tmp {helper}; sleep 300"), helper


def is_gone(pid: int) -> bool:
    for _ in range(50):
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        if not state or state.startswith("Z"):
            return True
        time.sleep(0.1)
    return False


def test_a_browser_that_hangs_is_stopped_with_its_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import bridge.page_check as page_check

    monkeypatch.setattr(page_check, "RENDER_TIMEOUT_S", 2)
    browser, helper = start_browser_with_a_helper(tmp_path)

    dom, why = render_dom(b"<p>hi</p>", None, browser)

    assert dom == "" and why == "browser did not render it (TimeoutExpired)"
    assert is_gone(int(helper.read_text()))


def test_an_interrupted_check_stops_the_browser_and_its_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bridge.page_check as page_check

    browser, helper = start_browser_with_a_helper(tmp_path)

    class Interrupted(subprocess.Popen[bytes]):
        def communicate(self, *_: object, **__: object) -> tuple[bytes, bytes]:
            while not helper.exists():
                time.sleep(0.05)
            raise KeyboardInterrupt

    with monkeypatch.context() as patch, pytest.raises(KeyboardInterrupt):
        patch.setattr(page_check.subprocess, "Popen", Interrupted)
        render_dom(b"<p>hi</p>", None, browser)

    assert is_gone(int(helper.read_text()))


def test_an_interrupted_page_check_stops_its_browser_and_helpers(bridge_env: BridgeEnv, tmp_path: Path) -> None:
    browser, helper = start_browser_with_a_helper(tmp_path)
    (tmp_path / "page.html").write_bytes(page("<p>hi</p>"))
    check = subprocess.Popen(
        [str(VENV_BIN / "bridge"), "page", "check", "--file", str(tmp_path / "page.html")],
        env={**bridge_env.env, BROWSER_ENV: browser},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(200):
            if helper.exists() or check.poll() is not None:
                break
            time.sleep(0.05)
        assert helper.exists(), "the check started its browser"
        check.send_signal(signal.SIGINT)
        check.wait(timeout=10)
    finally:
        if check.poll() is None:
            check.kill()
    assert is_gone(int(helper.read_text()))
