import re
import shutil
import subprocess
from pathlib import Path

from bridge.leads import TINTS

PAGE = Path(__file__).parent.parent / "src/bridge/web/index.html"
STUBS = """
const element = () => new Proxy(function () {}, {
  get: (target, key) => key === Symbol.toPrimitive ? () => "" : (key in target ? target[key] : element()),
  set: () => true,
  apply: () => element(),
});
globalThis.document = element();
globalThis.localStorage = { getItem: () => null, setItem: () => {} };
globalThis.fetch = () => new Promise(() => {});
globalThis.setInterval = () => 0;
globalThis.setTimeout = () => 0;
globalThis.CSS = { escape: s => s };
"""


def test_page_script_evaluates_without_errors(tmp_path: Path) -> None:
    """Top-level page code must run: a load-time exception leaves the dashboard blank."""
    node = shutil.which("node")
    assert node, "node is required to check the dashboard script"
    script = re.search(r"<script>(.*)</script>", PAGE.read_text(), re.S)
    assert script is not None
    path = tmp_path / "page.js"
    path.write_text(STUBS + script.group(1))
    done = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=20, check=False)
    assert done.returncode == 0, done.stderr


def test_guild_page_script_evaluates_without_errors(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "node is required to check the page script"
    source = (PAGE.parent / "guild.html").read_text()
    script = re.search(r"<script>(.*)</script>", source, re.S)
    assert script is not None
    path = tmp_path / "guild.js"
    path.write_text(STUBS + script.group(1))
    done = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=20, check=False)
    assert done.returncode == 0, done.stderr


def test_session_names_keep_their_case(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "node is required to check the dashboard script"
    script = re.search(r"<script>(.*)</script>", PAGE.read_text(), re.S)
    assert script is not None
    checks = """
    const eq = (a, b) => { if (a !== b) throw new Error(JSON.stringify(a) + " !== " + JSON.stringify(b)); };
    eq(displayName({ handle: "hq-refactor", tab_name: "HQ Refactor" }), "HQ Refactor");
    eq(displayName({ handle: "hq-refactor-2", tab_name: "HQ Refactor" }), "hq-refactor-2");
    eq(displayName({ handle: "devbox/Work", tab_name: "Work", remote: { host: "devbox" } }), "devbox/Work");
    eq(displayName({ tab_name: "Solo", pid: 1 }), "Solo");
    """
    path = tmp_path / "page.js"
    path.write_text(STUBS + script.group(1) + checks)
    done = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=20, check=False)
    assert done.returncode == 0, done.stderr


def test_session_rows_render(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "node is required to check the dashboard script"
    script = re.search(r"<script>(.*)</script>", PAGE.read_text(), re.S)
    assert script is not None
    checks = """
    state = { now: 10_000, sessions: [], items: [], signals: [], messages: [], claims: [] };
    seen = {};
    const links = [
      { kind: "pr", label: "mono#1", url: "u", done: true },
      { kind: "issue", label: "PROJ-1", done: false },
    ];
    const local = { conversation_id: "c", handle: "hq", tab_name: "HQ", status: "idle", last_activity_at: 1_000,
                    workspace_name: "W", refs: [], links };
    renderRow(local);
    lastOk = Date.now();
    state.freshness = { github: { newest_ok_at: Date.now() - 20 * 60_000, failing: 7, watched: 40 } };
    tick();
    expanded.add("c");
    renderRow(local);
    renderRow({ handle: "devbox/x", tab_name: "x", status: "active", agent: "codex", refs: [],
                remote: { host: "devbox", tmux_id: "$1", observed_at: 9_000 }, driven_by: ["hq"] });
    """
    path = tmp_path / "page.js"
    path.write_text(STUBS + script.group(1) + checks)
    done = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=20, check=False)
    assert done.returncode == 0, done.stderr


def test_header_says_hooks_silent(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "node is required to check the dashboard script"
    script = re.search(r"<script>(.*)</script>", PAGE.read_text(), re.S)
    assert script is not None
    checks = """
    const header = { className: "", title: "", kids: [], replaceChildren(...kids) { this.kids = kids; } };
    globalThis.document = {
      getElementById: () => header, createTextNode: text => ({ text }), createElement: () => element(),
    };
    state = { now: Date.now(), sessions: [], items: [], signals: [], messages: [], claims: [],
              hooks: { silent: true, last_hook_at: Date.now() - 3_600_000, last_turn_at: Date.now() - 10_000 } };
    lastOk = Date.now();
    tick();
    if (header.kids[1].text !== "Hooks silent" || !header.className.includes("error")) {
      throw new Error(JSON.stringify(header));
    }
    """
    path = tmp_path / "page.js"
    path.write_text(STUBS + script.group(1) + checks)
    done = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=20, check=False)
    assert done.returncode == 0, done.stderr


def test_feed_and_board_render(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "node is required to check the dashboard script"
    script = re.search(r"<script>(.*)</script>", PAGE.read_text(), re.S)
    assert script is not None
    checks = """
    const now = Date.now();
    state = { now, user: "sam", sessions: [{ conversation_id: "c", handle: "hq", tab_name: "HQ", surface: "P",
              status: "idle", refs: [], links: [] }], signals: [], claims: [], remotes: [],
      items: [
        { id: 1, kind: "decision", severity: "action", source: "agent", from_conversation: "c", from_handle: "hq",
          text: "Ship v2?", body: "Flag costs a day", options: ["Ship", "Wait"], created_at: now - 1000, surface: "P",
          links: [{ label: "PR", url: "https://github.com/a/b/pull/1" },
                  { label: "bad", url: "javascript:alert(1)" }] },
        { id: 2, kind: "done", severity: "info", source: "agent", from_conversation: "c", from_handle: "hq",
          text: "Merged #7", options: [], links: [], created_at: now - 5000 },
        { id: 3, kind: "blocked", severity: "blocker", source: "agent", from_conversation: "c", from_handle: "hq",
          text: "Need a token", options: [], links: [], created_at: now - 9000, resolved_at: now - 100,
          answered_at: now - 100, answer: "Use the staging one" },
      ],
      messages: [
        { id: 1, thread_id: 1, kind: "info", board: 1, from_user: 1, from_handle: "sam", body: "Freeze merges",
          recipients: "hq:delivered", topic: "release", created_at: now - 3000 },
        { id: 2, thread_id: 1, kind: "reply", board: 0, from_user: 0, from_handle: "hq", body: "ok",
          recipients: "sam:queued", unread_for_user: 1, created_at: now - 2000 },
      ] };
    seen = {};
    const eq = (a, b) => { if (a !== b) throw new Error(JSON.stringify(a) + " !== " + JSON.stringify(b)); };
    eq(isWaiting(state.items[0]), true);
    eq(isWaiting(state.items[1]), false);
    eq(isWaiting(state.items[2]), false);
    render();
    """
    path = tmp_path / "page.js"
    path.write_text(STUBS + script.group(1) + checks)
    done = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=20, check=False)
    assert done.returncode == 0, done.stderr


def run_queue_checks(tmp_path: Path, checks: str) -> None:
    node = shutil.which("node")
    assert node, "node is required to check the page script"
    source = (PAGE.parent / "guild.html").read_text()
    script = re.search(r"<script>(.*)</script>", source, re.S)
    assert script is not None
    path = tmp_path / "guild.js"
    path.write_text(STUBS + script.group(1) + QUEUE_FIXTURE + checks)
    done = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=20, check=False)
    assert done.returncode == 0, done.stderr


QUEUE_FIXTURE = """
    const eq = (a, b) => { if (a !== b) throw new Error(JSON.stringify(a) + " !== " + JSON.stringify(b)); };
    const NONE = String.fromCharCode(0x2013);
    const pr = {
      ref: "pr:O/app#7", label: "app#7", repo: "O/app", number: 7, title: "T",
      url: "https://github.com/O/app/pull/7", draft: false, loc: 120, large: true, checks: "FAILURE",
      state: "waiting for you", group: "waiting", group_why: "open and not published", rounds: 0,
      owner: "alex", owner_surface: "s",
      lead: "Alex", can_publish: true, can_retry: false, can_repost: false, commits_since: null, reviewed_at: null,
      reviewed_by: null, post_state: null, post_detail: null, post_link: null, doc_url: null, doc_stale: false,
      worktree_url: null, worktree_state: null, worktree_detail: null, worktree_stale: false, approved_by: [],
      approvers: [], approval_left: null, bot_approved: false, owner_open_threads: 0, open_threads: 0,
      merge_state: "BLOCKED", resolve_threads: [], resolve_is_last: false, reviews_ran: [], reviews_gap: null,
    };
    const attention = { ranking: [], priority: [], estimate_rule: "rough" };
    const github = { ok_at: 980, every_s: 90, stale_s: 300 };
    const slack = { enabled: true, mode: "token", every_s: 60, ok_at: 960, error: null, error_at: null };
"""


def test_an_approved_row_names_its_approvers_and_what_is_left_for_the_owner(tmp_path: Path) -> None:
    run_queue_checks(
        tmp_path,
        """
    state = { now: 1000, attention, reviews: { enabled: true, rules: [], dry_run: false, prs: [pr] } };
    eq(queueApprovalWhy(pr), null);
    const ivo = { ...pr, approved_by: ["ivo"], approvers: ["ivo"], approval_left: "CI pending" };
    eq(queueApprovalWhy(ivo), "approved by ivo · CI pending: it merges once CI is green");
    eq(queueApprovalWord(ivo), "approved");
    eq(queueApprovalWord(pr), NONE);
    const bot = { ...pr, bot_approved: true, approvers: ["ReviewBot"], approval_left: "your comments open",
                  merge_state: "CLEAN" };
    eq(queueApprovalWhy(bot),
       "approved by ReviewBot · your comments open: only the review bot approved, no person yet; its lead settles " +
       "your comments first, then it may merge");
    eq(queueApprovalWord(bot), "your comments");
    const done = { ...ivo, group: "ready", merge_path: "ready", approval_left: "ready to merge (lead)" };
    eq(queueApprovalWhy(done).startsWith("approved by ivo · ready to merge (lead): CI green, no conflicts"), true);
    eq(queueApprovalWord(done), "ready (lead)");
    eq(queueApprovalWhy({ ...ivo, approval_left: null, draft: true }),
       "approved by ivo: a draft, its owner marks it ready first");
    eq(queueMergeWhy({ ...done, merge_state: "CLEAN" }), "GitHub can merge it now");
    const gated = { ...bot, approval_left: "blocked on GitHub", merge_block: null, merge_state: "BLOCKED" };
    eq(queueMergeWord(gated), "blocked on GitHub");
    eq(queueMergeWhy(gated).startsWith("branch protection wants a person's review; the review bot's may"), true);
    eq(queueApprovalWhy(gated).includes("branch protection may not count the review bot"), true);
    eq(queueMergeWord({ ...pr, merge_state: "BLOCKED" }), "blocked");
    for (const row of [ivo, bot, done]) renderQueueTableRow(row);
        """,
    )


def test_the_table_says_how_fresh_github_and_slack_are(tmp_path: Path) -> None:
    run_queue_checks(
        tmp_path,
        """
    const words = (queue, now) => mapQueueToFreshness(queue, now).map((p) => p.text + (p.stale ? "!" : "")).join(" | ");
    const minute = 60_000;
    eq(words({ github, slack }, 41_000), "GitHub read 40 s ago | Slack read 40 s ago");
    eq(words({ github, slack }, 980 + 6 * minute), "GitHub read 6 min ago, stale! | Slack read 6 min ago");
    const connector = { ...slack, mode: "connector", every_s: 300, ok_at: null };
    eq(words({ github, slack: connector }, 41_000), "GitHub read 40 s ago | Slack not read yet");
    eq(words({ github, slack: { ...connector, ok_at: 960 } }, 960 + 2 * minute),
       "GitHub read 2 min ago | Slack read 2 min ago");
    const failedOnce = { ...connector, ok_at: 960, error: "never called", error_at: 970, failures: 1 };
    eq(words({ github, slack: failedOnce }, 41_000), "GitHub read 40 s ago | Slack read 40 s ago");
    eq(words({ github, slack: { ...failedOnce, failures: 3 } }, 41_000),
       "GitHub read 40 s ago | Slack reads failing, last good 40 s ago!");
    eq(words({ github: { ...github, ok_at: 960 + 21 * minute }, slack: failedOnce }, 960 + 21 * minute),
       "GitHub read 0 s ago | Slack reads failing, last good 21 min ago!");
    eq(words({ github: { ...github, ok_at: null }, slack: { ...slack, enabled: false } }, 1000), "GitHub not read yet");
    eq(words({ github: { ...github, ok_at: null }, error: "auth failed", slack: { ...slack, enabled: false } }, 1000),
       "GitHub not read yet!");
    """,
    )


def test_a_changing_number_keeps_the_card_being_typed_in(tmp_path: Path) -> None:
    source = (PAGE.parent / "guild.html").read_text()
    assert "const key = JSON.stringify(need);" in source
    run_queue_checks(
        tmp_path,
        """
    const slot = { dataset: {}, replaceChildren: (...nodes) => { slot.count = nodes.length; } };
    const card = { querySelector: () => slot };
    const need = { id: 4 };
    const ranked = (mark) => ({ ...attention, ranking: [{ key: "question:4", mark, why: "w", estimate: null }] });
    state = { now: 1000, attention: ranked(1) };
    updateCardMark(card, need);
    eq(slot.count, 1);
    slot.count = -1;
    updateCardMark(card, need);
    eq(slot.count, -1);
    state.attention = ranked(2);
    updateCardMark(card, need);
    eq(slot.count, 1);
    state.attention = attention;
    updateCardMark(card, need);
    eq(slot.count, 0);
    """,
    )


def test_queue_table_renders_and_its_words_say_each_stage(tmp_path: Path) -> None:
    run_queue_checks(
        tmp_path,
        """
    const drafting = { ...pr, draft: true, group: "drafts", can_publish: false, open_threads: 3 };
    const reviews = { enabled: true, error: null, dry_run: true, rules: ["r"], prs: [pr, drafting], github, slack };
    state = { now: 1000, user: "Sam", reviews, attention };
    renderQueue();
    state.attention = { ...attention, ranking: [{ key: pr.ref, mark: 2, why: "w", estimate: "~5 min" }],
                        priority: [{ name: "onboarding", issue_keys: ["PROJ-1"] }] };
    renderQueue();
    eq(rankOf(pr.ref).mark, 2);
    eq(rankOf("pr:O/app#8"), null);
    eq(isWaitingOnYou({ delivering: true }), false);
    eq(isWaitingOnYou({ delivering: true, delivery_failed: true }), true);
    eq(queueReviewWord(pr), NONE);
    eq(queueReviewWord({ ...pr, owner_open_threads: 2, open_threads: 3 }), "2 open");
    const unanswered = { ...pr, merge_block: "the last comment has no answer and no commit after it" };
    eq(queueReviewWord(unanswered), "comment open");
    const at = new Date(2026, 10, 3, 11, 2).getTime();
    const theirs = { ...pr, owner_last_at: at, commits_after_owner: 1, owner_open_threads: 1, open_threads: 3,
                  merge_block: "1 review thread is unresolved" };
    const ownerWhy = queueReviewWhy(theirs);
    eq(ownerWhy.startsWith("Your comments: 1 review thread is unresolved. your last comment or review: "), true);
    eq(ownerWhy.endsWith(", 1 commit since; 3 unresolved review threads in all"), true);
    eq(queueReviewWord({ ...pr, reviewed_at: 5, commits_since: 1 }), NONE);
    const why = queueReviewWhy({ ...pr, reviewed_at: 5, commits_since: 1, reviewed_oid: "c0ffee123aaaa" });
    eq(why.startsWith("pushed since your review at ") && why.endsWith(", c0ffee123"), true);
    eq(queueSlackWord(pr), NONE);
    eq(queueSlackWord({ ...pr, post_state: "dry-run", state: "published" }), "dry run");
    eq(queueSlackWord({ ...pr, post_link: "https://x", post_state: "external", state: "published" }), "published");
    eq(queueSlackWord({ ...pr, post_state: "posted-link-unknown", state: "published" }), "sent?");
    const posted = { ...pr, post_link: "https://x", post_state: "external", state: "published" };
    eq(queueSlackWord({ ...posted, posted_at: 1000 - 3 * 3_600_000 }), "published 3h ago");
    eq(queueSlackWord({ ...posted, posted_at: 1000 - 5 * 60_000 }), "published 5m ago");
    eq(queueSlackWord({ ...posted, post_state: "posted", posted_at: 1000 - 2 * 86_400_000 }), "published 2d ago");
    eq(queueSlackWord({ ...pr, requested: 1 }), NONE);
    renderQueueSlackCell({ ...pr, group: "ready" });
    const unreviewed = queueMergeWhy({ ...pr, merge_state: "CLEAN", merge_path: null });
    eq(unreviewed.endsWith("until it is approved, CI is green and nothing of yours is open"), true);
    eq(queueMergeWhy({ ...pr, merge_state: "CLEAN", merge_path: "ready" }), "GitHub can merge it now");
    const form = { ref: pr.ref, direct: true, mention: ["Kai", "Nina"] };
    eq(queuePostPreview({ ...pr, description: "Exports run nightly" }, form),
       "https://github.com/O/app/pull/7 - Exports run nightly - direct @Kai @Nina");
    queueForm = form;
    state.reviews = { ...reviews, mention_people: ["Kai", "Nina"] };
    renderQueue();
    renderPublishForm(pr);
    queueForm = null;
    eq(queueMergeWord({ ...pr, merge_state: "BEHIND", base: "main" }), "behind main");
    eq(queueMergeWord({ ...pr, merge_state: "DIRTY" }), "conflicts");
    const blocked = { ...pr, merge_state: "CLEAN", merge_block: "1 review thread is unresolved" };
    eq(queueMergeWord(blocked), "your comments");
    eq(queueMergeWhy(blocked).startsWith("Your comments: 1 review thread is unresolved"), true);
    const answered = { ...blocked, resolve_is_last: true, resolve_threads: [{ url: "u", quote: "Renamed." }] };
    eq(queueReviewWord(answered), "resolve 1");
    eq(queueReviewWhy(answered).endsWith("all that keeps it from merging"), true);
    eq(queueReviewWhy({ ...answered, resolve_is_last: false }).endsWith("more than that to merge"), true);
    renderResolveRow({ ...answered, resolve_threads: [answered.resolve_threads[0], { url: "v", quote: "Done." }] });
    const asked = { ...pr, merge_state: "DIRTY", base: "main", conflict_asked_at: 5 };
    eq(queueMergeWhy(asked).includes("asked to merge main"), true);
    eq(queueDocWord(pr), "none yet");
    const gapped = { ...pr, reviews_gap: "Claude review missing", reviews_ran: ["Codex"] };
    eq(queueReviewMarksWhy(gapped), "reviews: Codex; Claude review missing");
    eq(queueReviewMarksWhy({ ...pr, reviews_ran: [] }), "");
    eq(queueDocWord({ ...pr, large: false }), NONE);
    eq(queueDocWord({ ...pr, doc_url: "/d", doc_stale: true }), "older");
    eq(queueDocWord({ ...pr, doc_url: "/d", doc_stale: true, doc_updating: true }), "updating");
    eq(queueFolderWord({ ...pr, worktree_url: "vscode://x", worktree_state: "dirty" }), "has changes");
    state.reviews = { enabled: false, prs: [pr], rules: [] };
    renderQueue();
    state.reviews = { enabled: true, prs: [], rules: [], error: "gh failed", dry_run: true, github, slack };
    renderQueue();
    """,
    )


def test_a_stray_click_only_arms_publish(tmp_path: Path) -> None:
    run_queue_checks(
        tmp_path,
        """
    const posted = [];
    el = (tag, cls, text) => ({ textContent: text });
    post = async (path) => { posted.push(path); return { ok: true }; };
    setStatus = () => {};
    refresh = () => {};
    const first = queueAction(pr, "Publish to Slack", "publish");
    first.onclick();
    eq(posted.length, 0);
    eq(first.textContent, "Really post?");
    eq(queueAction(pr, "Publish to Slack", "publish").textContent, "Really post?");
    first.onclick();
    eq(posted[0], "/api/reviews/O/app/7/publish");
    eq(queueArmed, null);
    queueAction(pr, "Retry", "retry").onclick();
    eq(posted[1], "/api/reviews/O/app/7/retry");
    """,
    )


def test_the_queue_never_offers_approval_as_owner_action() -> None:
    """GitHub approval belongs to the owner's colleagues, and shows only as their state; here the owner publishes."""
    src = PAGE.parent.parent
    queue_js = (PAGE.parent / "guild.html").read_text().split("const QUEUE_CHECKS")[1].split("const RENDERERS")[0]
    texts = [queue_js, *(p.read_text() for p in src.glob("review*.py"))]
    assert not any(re.search(r"\bapprov(e|al)\b", text, re.I) for text in texts)
    assert not re.search(r"\bidle\b", queue_js)


def test_every_agterm_tint_has_its_value_in_both_themes() -> None:
    css = (PAGE.parent / "guild.html").read_text().split("</style>")[0]
    light, dark = css.split("@media (prefers-color-scheme: dark)")
    for tint in TINTS:
        assert re.search(rf"--tint-{tint}: #[0-9a-f]{{6}}", light), tint
        assert re.search(rf"--tint-{tint}: #[0-9a-f]{{6}}", dark.split("* {")[0]), tint
        assert f".tint-{tint} {{" in css


def test_each_group_gets_a_header_and_publish_only_in_the_first(tmp_path: Path) -> None:
    run_queue_checks(
        tmp_path,
        """
    const made = [];
    const realEl = el;
    el = (tag, cls, text) => {
      if (cls === "group" || cls === "act") made.push(text);
      return realEl(tag, cls, text);
    };
    const published = { ...pr, group: "published", can_publish: false };
    const drafting = { ...pr, group: "drafts", can_publish: false, draft: true };
    const prs = [pr, published, drafting];
    const reviews = { enabled: true, rules: [], dry_run: true, chat: "slack", prs, github, slack };
    state = { now: 1000, attention, reviews };
    renderQueue();
    eq(made.join(), "Open,Publish to Slack,Published,Drafts in progress");
    made.length = 0;
    lastRendered.clear();
    state.reviews = { ...state.reviews, chat: "none" };
    renderQueue();
    eq(made.join(), "Open,Publish,Published,Drafts in progress");
    state.reviews = { ...state.reviews, chat: "slack" };
    made.length = 0;
    lastRendered.clear();
    state.reviews.prs = [published];
    renderQueue();
    eq(made.join(), "Published");
    """,
    )


def test_pr_sort_keeps_groups_ranking_ties_and_empties_last(tmp_path: Path) -> None:
    run_queue_checks(
        tmp_path,
        """
    const a = { ...pr, ref: "a", label: "app#9", loc: 900, lead: "bea", checks: "SUCCESS", merge_state: "CLEAN" };
    const b = { ...pr, ref: "b", label: "app#10", loc: 80, lead: null, owner: null, checks: null,
                merge_state: "DIRTY" };
    const c = { ...pr, ref: "c", label: "App#2", loc: 80, lead: "Al", checks: "PENDING", merge_state: "UNKNOWN" };
    const refs = (prs) => prs.map((p) => p.ref).join();
    const ranked = [a, b, c];
    eq(refs(sortQueueGroup(ranked, null)), "a,b,c");
    eq(refs(sortQueueGroup(ranked, { key: "lines", dir: 1 })), "b,c,a");
    eq(refs(sortQueueGroup(ranked, { key: "lines", dir: -1 })), "a,b,c");
    eq(refs(sortQueueGroup(ranked, { key: "pr", dir: 1 })), "c,a,b");
    eq(refs(sortQueueGroup(ranked, { key: "who", dir: 1 })), "c,a,b");
    eq(refs(sortQueueGroup(ranked, { key: "who", dir: -1 })), "a,c,b");
    eq(refs(sortQueueGroup(ranked, { key: "ci", dir: 1 })), "c,a,b");
    eq(refs(sortQueueGroup(ranked, { key: "ci", dir: -1 })), "a,c,b");
    eq(refs(sortQueueGroup(ranked, { key: "merge", dir: 1 })), "b,c,a");
    const approved = [a, { ...b, approvers: ["x", "y"] }, { ...c, approvers: ["x"] }];
    eq(refs(sortQueueGroup(approved, { key: "approved", dir: 1 })), "c,b,a");
    eq(refs(sortQueueGroup(approved, { key: "approved", dir: -1 })), "b,c,a");

    const stored = new Map();
    localStorage = { getItem: (k) => stored.get(k) ?? null, setItem: (k, v) => stored.set(k, v),
                     removeItem: (k) => stored.delete(k) };
    state = { now: 1000, attention, reviews: { enabled: true, rules: [], dry_run: false, prs: ranked } };
    clickQueueSort("lines");
    eq(JSON.stringify(loadQueueSort()), '{"key":"lines","dir":1}');
    clickQueueSort("lines");
    eq(queueSort.dir, -1);
    setQueueSort(null, "pr");
    eq(loadQueueSort(), null);
    stored.set(QUEUE_SORT_STORE, '{"key":"place","dir":1}');
    eq(loadQueueSort(), null);

    localStorage = { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); },
                     removeItem: () => { throw new Error("blocked"); } };
    eq(loadQueueSort(), null);
    clickQueueSort("who");
    eq(queueSort.key, "who");
    """,
    )


def test_asks_say_whose_they_are_and_how_old(tmp_path: Path) -> None:
    run_queue_checks(
        tmp_path,
        """
    const hour = 3_600_000;
    state = { now: 30 * hour, leads: [{ name: "alex", display_name: "Alex" }], asks: { waiting: [], verified: [] } };
    eq(leadAsksText(1, 25 * hour), "1 ask open · oldest 5h");
    eq(leadAsksText(3, 29 * hour), "3 asks open · oldest 1h");
    eq(askMeta({ id: 4, owner: "alex", by: "work-1", at: 28 * hour }, false),
       "#4 · Alex · claimed done by work-1 2h ago, not verified yet");
    eq(askMeta({ id: 5, owner: "aide", by: "boris", at: 26 * hour, closes_at: 50 * hour }, true),
       "#5 · aide · verified 4h ago · closes by itself in 20h");
    const wait = { on: "owner", why: "Org admin rights", by: "alex", at: 29 * hour };
    eq(askMeta({ id: 6, owner: "alex", by: "work-1", at: 28 * hour, wait }, false),
       "#6 · Alex · claimed done by work-1 2h ago, not verified yet · waiting on you");
    const released = { ...wait, on: "release" };
    eq(leadAskLine({ id: 7, state: "in_progress", words: "Add the bot", overdue: false, wait: released }),
       "#7 in progress (waiting on a release: Org admin rights): Add the bot");
    eq(askMeta({ id: 8, owner: "alex", state: "in_progress", at: 25 * hour, wait }, false),
       "#8 · Alex · in progress · 5h old · waiting on you");
    const paused = { on: null, until: new Date(2099, 0, 4).getTime(), why: "Next sweep", by: "xavi", at: 0 };
    eq(leadAskLine({ id: 9, state: "open", words: "Sweep", overdue: false, wait: paused }),
       "#9 open (paused until Sun 4 Jan: Next sweep): Sweep");
    eq(askWaitText({ ...paused, until: new Date(2099, 0, 4, 9, 30).getTime() }), "paused until Sun 4 Jan 09:30");
    state.asks.waiting = [{ id: 6, owner: "alex", by: "work-1", at: 28 * hour, words: "w", claim: "c", wait }];
    state.asks.on_you = [{ id: 8, owner: "alex", state: "open", at: 25 * hour, words: "w", claim: null, wait }];
    renderAsks();
    """,
    )


def test_a_lead_row_counts_its_workers_against_the_cap_and_goes_red_at_it(tmp_path: Path) -> None:
    run_queue_checks(
        tmp_path,
        """
    const realEl = el;
    const made = [];
    el = (tag, cls, text) => { made.push((cls || "") + "=" + (text || "")); return realEl(tag, cls, text); };
    const lead = { tab_name: "Eli", surface: "s", state: "working", why: "", workers: [], work: [], worker_cap: 5 };
    renderLeadRow({ ...lead, worker_count: 3 });
    eq(made.includes("cap=3/5 workers"), true);
    made.length = 0;
    renderLeadRow({ ...lead, worker_count: 5 });
    eq(made.includes("cap full=5/5 workers"), true);
    made.length = 0;
    renderLeadRow({ ...lead, worker_count: 0 });
    eq(made.some((m) => m.startsWith("cap")), false);
    el = realEl;
        """,
    )
