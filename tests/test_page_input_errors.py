import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).parent.parent / "src/bridge/web/guild.html"

# Just enough DOM for a card: nodes keep their children, classes, text and value; anything else is a no-op.
DOM = """
const created = [];
class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.attributes = {}; this.className = "";
    this.textContent = ""; this.value = ""; this.hidden = false; this.title = ""; this.isConnected = true;
    created.push(this); }
  get classList() {
    const node = this;
    const names = () => node.className.split(" ").filter(Boolean);
    return {
      add: (n) => { if (!names().includes(n)) node.className = [...names(), n].join(" "); },
      remove: (n) => { node.className = names().filter((m) => m !== n).join(" "); },
      contains: (n) => names().includes(n),
      toggle: (n, on) => { (on ?? !names().includes(n)) ? this.classList.add(n) : this.classList.remove(n); },
    };
  }
  append(...nodes) { for (const n of nodes) this.children.push(typeof n === "string" ? { text: n } : n); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  setAttribute(k, v) { this.attributes[k] = v; }
  addEventListener() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
}
const stub = () => new Proxy(function () {}, {
  get: (target, key) => key === Symbol.toPrimitive ? () => "" : (key in target ? target[key] : stub()),
  set: () => true,
  apply: () => stub(),
});
globalThis.document = new Proxy({
  createElement: (tag) => new Node(tag),
  querySelectorAll: (selector) => created.filter((n) => n.classList.contains(selector.replace(/^\\./, ""))),
}, { get: (target, key) => key in target ? target[key] : stub() });
globalThis.localStorage = { getItem: () => null, setItem: () => {} };
globalThis.setInterval = () => 0;
globalThis.setTimeout = () => 0;
globalThis.requestAnimationFrame = () => 0;
globalThis.CSS = { escape: (s) => s };
let answer = () => new Promise(() => {});
globalThis.fetch = (path, options) => path === "/api/guild" ? new Promise(() => {}) : answer(path, options);
const find = (node, test) => test(node) ? node : (node.children || []).map((c) => find(c, test)).find(Boolean);
"""

CHECKS = """
const eq = (a, b) => { if (a !== b) throw new Error(JSON.stringify(a) + " !== " + JSON.stringify(b)); };
const settle = () => new Promise((resolve) => setImmediate(resolve));
(async () => {
  state.now = 1_000_000;
  const need = { type: "post", id: 7, text: "Ship v2?", urgent: false, card: null, sections: [], options: ["Ship"],
                 question_ref: "Q#7", from: "alex", kind: "decision", at: 0, links: [] };
  const card = renderNeedCard(need);
  const input = find(card, (n) => n.tag === "input");
  const error = find(card, (n) => n.classList && n.classList.contains("input-error"));
  eq(error.hidden, true);

  const long = "y".repeat(8_001);
  const why = "your text is 8,001 characters; the most is 8,000";
  answer = async () => ({ status: 409, json: async () => ({ ok: false, detail: why }) });
  input.value = long;
  input.oninput();
  input.onkeydown({ key: "Enter" });
  await settle();
  eq(error.hidden, false);
  eq(error.textContent, why);
  eq(input.value, long);
  eq(drafts.get(7), long);

  const rebuilt = renderNeedCard(need);
  eq(find(rebuilt, (n) => n.tag === "input").value, long);
  const shownAgain = find(rebuilt, (n) => n.classList && n.classList.contains("input-error"));
  eq(shownAgain.textContent, why);
  eq(shownAgain.hidden, false);

  input.value = "y".repeat(7_000);
  input.oninput();
  eq(error.hidden, true);
  answer = async () => ({ status: 200, json: async () => ({ ok: true, detail: "sent" }) });
  input.onkeydown({ key: "Enter" });
  await settle();
  eq(error.hidden, true);
  eq(drafts.has(7), false);

  answer = async () => { throw new Error("connection refused"); };
  eq((await post("/api/room", {})).detail, "bridge did not answer: connection refused");
  answer = async () => ({ status: 403, json: async () => { throw new SyntaxError("not JSON"); } });
  eq((await post("/api/room", {})).detail, "bridge answered 403 with no result");
})().catch((error) => { console.error(error.stack); process.exit(1); });
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_refused_answer_shows_why_under_its_box_and_stays_there(tmp_path: Path) -> None:
    script = re.search(r"<script>(.*)</script>", PAGE.read_text(), re.S)
    assert script is not None
    path = tmp_path / "guild.js"
    path.write_text(DOM + script.group(1) + CHECKS)
    done = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=20, check=False)
    assert done.returncode == 0, done.stderr


PUBLISH_CHECKS = """
const eq = (a, b) => { if (a !== b) throw new Error(JSON.stringify(a) + " !== " + JSON.stringify(b)); };
const settle = () => new Promise((resolve) => setImmediate(resolve));
rerenderQueue = () => {};
(async () => {
  state.reviews = { mention_people: ["mira"], comment_max_chars: 300 };
  const pr = (n) => ({ ref: "pr:Org/app#" + n, repo: "Org/app", number: n, label: "#" + n, url: "https://x/" + n,
                      description: "d", can_publish: true });
  queuePublishButton(pr(1)).onclick();
  queueForm.mention = ["mira"];
  queueComment.text = "c".repeat(301);
  const send = find(renderPublishForm(pr(1)), (n) => n.tag === "button" && n.textContent === "Send");
  let refuse;
  answer = () => new Promise((resolve) => { refuse = resolve; });
  const sending = send.onclick();
  queuePublishButton(pr(2)).onclick();
  refuse({ status: 409, json: async () => ({ ok: false, detail: "the comment is 301 characters; at most 300" }) });
  await sending;
  await settle();
  eq(queueForm.ref, "pr:Org/app#2");
  queuePublishButton(pr(1)).onclick();
  eq(queueForm.ref, "pr:Org/app#1");
  eq(queueForm.mention.join(), "mira");
  eq(queueComment.text, "c".repeat(301));
})().catch((error) => { console.error(error.stack); process.exit(1); });
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_refused_publish_keeps_its_comment_while_another_form_is_open(tmp_path: Path) -> None:
    script = re.search(r"<script>(.*)</script>", PAGE.read_text(), re.S)
    assert script is not None
    path = tmp_path / "guild.js"
    path.write_text(DOM + script.group(1) + PUBLISH_CHECKS)
    done = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=20, check=False)
    assert done.returncode == 0, done.stderr


REOPEN_CHECKS = """
const eq = (a, b) => { if (a !== b) throw new Error(JSON.stringify(a) + " !== " + JSON.stringify(b)); };
const settle = () => new Promise((resolve) => setImmediate(resolve));
(async () => {
  state.now = 1_000_000;
  const ask = { id: 4, words: "Fix the export", at: 0, by: "lead", evidence: "e", how: "h" };
  const card = renderAskCard(ask, true);
  const reopen = find(card, (n) => n.tag === "button" && n.textContent === "Reopen");
  const why = "open again, but alex was not told: its tab is locked or has no live session";
  answer = async () => ({ status: 200, json: async () => ({ ok: true, detail: why }) });
  reopen.onclick();
  await settle();
  eq(statusNote, "Ask #4 " + why);
})().catch((error) => { console.error(error.stack); process.exit(1); });
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_a_reopen_nobody_was_told_of_stays_on_the_page_after_its_card_leaves(tmp_path: Path) -> None:
    script = re.search(r"<script>(.*)</script>", PAGE.read_text(), re.S)
    assert script is not None
    path = tmp_path / "guild.js"
    path.write_text(DOM + script.group(1) + REOPEN_CHECKS)
    done = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=20, check=False)
    assert done.returncode == 0, done.stderr
