import json
import os
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Generator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_capacity import add_session

from bridge import status_watchers
from bridge.config import Config, PageLink, Watcher, map_toml_to_config
from bridge.db import connect
from bridge.guildview import map_db_to_guild
from bridge.status_watchers import MAX_FILE_BYTES, alert_watchers, find_owner_needs, read_watcher

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

NOW = int(datetime(2026, 11, 4, 12, 0, tzinfo=UTC).timestamp() * 1000)
PAGE = Path(__file__).parent.parent / "src/bridge/web/guild.html"


def _write(tmp_path: Path, **fields: object) -> Watcher:
    path = tmp_path / "status.json"
    path.write_text(json.dumps({"name": "prod alerts", "status": "ok", "summary": "0 new", **fields}))
    return Watcher(path=str(path))


def test_fresh_ok_sweep_is_ok(tmp_path: Path) -> None:
    watcher = _write(tmp_path, last_run_utc="2026-11-04T11:53:00Z", stale_after_min=20, extra={"x": 1})
    shown = read_watcher(watcher, NOW)
    assert shown == {
        "name": "prod alerts",
        "path": watcher.path,
        "status": "ok",
        "state": "ok",
        "last_run_at": NOW - 7 * 60_000,
        "stale_after_min": 20,
        "summary": "0 new",
        "detail": "",
        "needs_owner": False,
    }


def test_sweep_older_than_its_limit_is_stale(tmp_path: Path) -> None:
    watcher = _write(tmp_path, last_run_utc="2026-11-04T11:26:00+00:00", stale_after_min=30)
    assert read_watcher(watcher, NOW)["state"] == "stale"


def test_limit_defaults_to_twenty_minutes(tmp_path: Path) -> None:
    assert read_watcher(_write(tmp_path, last_run_utc="2026-11-04T11:41:00Z"), NOW)["state"] == "ok"
    assert read_watcher(_write(tmp_path, last_run_utc="2026-11-04T11:39:00Z"), NOW)["state"] == "stale"


def test_failed_sweep_is_failed_and_keeps_its_detail(tmp_path: Path) -> None:
    watcher = _write(tmp_path, last_run_utc="2026-11-04T11:58:00Z", status="failed", detail="Sentry 502")
    shown = read_watcher(watcher, NOW)
    assert (shown["state"], shown["detail"]) == ("failed", "Sentry 502")


@pytest.mark.parametrize(
    ("fields", "cards"),
    [
        ({"status": "failed", "needs_owner": True}, [{"type": "watcher", "text": "Claude · devbox: login needed"}]),
        ({"status": "failed"}, []),
        ({"status": "failed", "needs_owner": "yes"}, []),
        ({"status": "ok", "needs_owner": True}, []),
        ({"status": "failed", "needs_owner": True, "last_run_utc": "2026-11-04T10:00:00Z"}, []),
    ],
)
def test_only_a_fresh_failed_watcher_that_asks_for_the_owner_is_a_needs_you_card(
    tmp_path: Path, fields: dict[str, object], cards: list[dict[str, object]]
) -> None:
    watcher = _write(
        tmp_path,
        **{"name": "Claude · devbox", "summary": "login needed", "last_run_utc": "2026-11-04T11:58:00Z", **fields},
    )
    assert find_owner_needs((watcher,), NOW) == [{**c, "path": watcher.path} for c in cards]


def test_the_owner_card_is_among_the_pages_needs_while_failed_and_gone_once_ok(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    watcher = _write(
        tmp_path, name="Claude · devbox", status="failed", summary="login needed", needs_owner=True,
        last_run_utc="2026-11-04T11:58:00Z",
    )  # fmt: skip
    config = Config(watchers=(watcher,))
    needs = map_db_to_guild(conn, config, NOW, lambda ref: "")["needs"]
    card = {"type": "watcher", "path": watcher.path, "text": "Claude · devbox: login needed"}
    assert card in needs  # type: ignore[operator]
    _write(tmp_path, name="Claude · devbox", status="ok", summary="ok", last_run_utc="2026-11-04T11:59:00Z")
    assert map_db_to_guild(conn, config, NOW, lambda ref: "")["needs"] == []


def test_missing_file_keeps_the_configured_name(tmp_path: Path) -> None:
    shown = read_watcher(Watcher(path=str(tmp_path / "nope.json"), name="prod alerts"), NOW)
    assert (shown["name"], shown["state"], shown["why"]) == ("prod alerts", "missing", "no status file")


@pytest.mark.parametrize(
    "body",
    [
        "{not json",
        "[]",
        '{"status": "ok"}',
        '{"status": "fine", "last_run_utc": "2026-11-04T11:58:00Z"}',
        '{"status": "ok", "last_run_utc": "yesterday"}',
        "[" * 100_000,
        b"\xff\xfe",
    ],
)
def test_malformed_file_is_unreadable(tmp_path: Path, body: str | bytes) -> None:
    path = tmp_path / "status.json"
    if isinstance(body, bytes):
        path.write_bytes(body)
    else:
        path.write_text(body[:MAX_FILE_BYTES])
    shown = read_watcher(Watcher(path=str(path)), NOW)
    assert shown["state"] == "unreadable"
    assert set(shown) == {"name", "path", "state", "why"}


def test_oversized_file_is_unreadable_without_parsing(tmp_path: Path) -> None:
    watcher = _write(tmp_path, last_run_utc="2026-11-04T11:58:00Z", detail="x" * MAX_FILE_BYTES)
    shown = read_watcher(watcher, NOW)
    assert (shown["state"], shown["why"]) == ("unreadable", "status file is over 64 KB")


def test_directory_is_unreadable_and_leaks_no_descriptor(tmp_path: Path) -> None:
    before = len(os.listdir("/dev/fd"))
    for _ in range(5):
        assert read_watcher(Watcher(path=str(tmp_path)), NOW)["state"] == "unreadable"
    assert len(os.listdir("/dev/fd")) == before


def test_config_reads_watchers_and_expands_home_when_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "s.json").write_text('{"status": "ok", "last_run_utc": "2026-11-04T11:58:00Z"}')
    config = map_toml_to_config({"watchers": [{"path": "~/s.json", "name": "alerts"}]})
    assert config.watchers == (Watcher(path="~/s.json", name="alerts"),)
    assert read_watcher(config.watchers[0], NOW)["state"] == "ok"
    with pytest.raises(ValueError, match="needs a path"):
        map_toml_to_config({"watchers": [{"name": "x"}]})


def test_page_words_change_by_the_minute_and_say_why_when_red(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "node is required to check the page script"
    script = re.search(r"<script>(.*)</script>", PAGE.read_text(), re.S)
    assert script is not None
    checks = """
    const eq = (a, b) => { if (a !== b) throw new Error(JSON.stringify(a) + " !== " + JSON.stringify(b)); };
    const now = 1_000_000_000;
    const base = { name: "prod alerts", path: "~/s.json", status: "ok", summary: "0 new", detail: "",
                   stale_after_min: 20 };
    eq(mapWatcherToLine({ ...base, state: "ok", last_run_at: now - 30_000 }, now).words, "last sweep just now, 0 new");
    const ok = mapWatcherToLine({ ...base, state: "ok", last_run_at: now - 7 * 60_000 - 59_000 }, now);
    eq(ok.words, "last sweep 7 min ago, 0 new");
    eq(ok.short, "7 min ago");
    eq(ok.level, "working");
    const stale = mapWatcherToLine({ ...base, state: "stale", last_run_at: now - 34 * 60_000 }, now);
    eq(stale.words, "no sweep for 34 min");
    eq(stale.tip.includes("It said: 0 new"), true);
    eq(stale.level, "stuck");
    const failed = mapWatcherToLine(
      { ...base, state: "failed", status: "failed", last_run_at: now - 120_000, detail: "502" }, now);
    eq(failed.words, "checked 2 min ago: 0 new");
    eq(failed.tip.includes("502"), true);
    eq(failed.short, "0 new");
    const long = mapWatcherToLine({ ...base, state: "failed", status: "failed", last_run_at: now - 60_000,
      summary: "nightly sync: 6 jobs fell back to the slow path in the last hour, clears 10:41Z" }, now);
    eq(long.short.length <= 48 && long.short.endsWith("…"), true);
    eq(long.words.startsWith("checked 1 min ago: nightly sync: 6 jobs"), true);
    eq(failed.level, "waiting");
    const missing = mapWatcherToLine({ name: "x", path: "p", state: "missing", why: "no status file" }, now);
    eq(missing.words, "no status file");
    """
    path = tmp_path / "guild.js"
    path.write_text(STUBS + script.group(1) + checks)
    done = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=20, check=False)
    assert done.returncode == 0, done.stderr


def test_fifo_at_the_path_is_unreadable_without_blocking(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    os.mkfifo(path)
    assert read_watcher(Watcher(path=str(path)), NOW)["state"] == "unreadable"


def test_page_rewrites_watcher_chips_at_most_once_a_minute(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "node is required to check the page script"
    script = re.search(r"<script>(.*)</script>", PAGE.read_text(), re.S)
    assert script is not None
    checks = """
    let writes = 0;
    const node = () => ({ dataset: {}, children: [], classList: { toggle() {} },
                          append(...kids) { this.children.push(...kids); }, replaceChildren() { writes++; } });
    const box = { ...node(), replaceChildren(...kids) { this.children = kids; } };
    const quiet = () => ({ dataset: {}, classList: { toggle() {} }, replaceChildren() {} });
    const pop = () => ({ ...quiet(), children: [quiet(), quiet()] });
    const others = { links: pop(), laptop: pop() };
    globalThis.document = { getElementById: (id) => others[id] || box, createElement: node };
    const w = (at) => ({ name: "a", path: "p", status: "ok", state: "ok", summary: "", detail: "",
                         stale_after_min: 20, last_run_at: at });
    state.watchers = [w(1_000_000), w(1_030_000)];
    const moments = new Set();
    for (let second = 0; second <= 120; second += 5) {
      state.now = 1_200_000 + second * 1000;
      const before = writes;
      renderWatchers();
      if (writes !== before) moments.add(second);
    }
    if (writes !== 6) throw new Error("chips rewritten " + writes + " times");
    if ([...moments].join() !== "0,60,120") throw new Error("rewritten at seconds " + [...moments].join());
    """
    path = tmp_path / "guild.js"
    path.write_text(STUBS + script.group(1) + checks)
    done = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=20, check=False)
    assert done.returncode == 0, done.stderr


def test_laptop_block_counts_agents_and_disk_and_is_absent_without_either(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "node is required to check the page script"
    script = re.search(r"<script>(.*)</script>", PAGE.read_text(), re.S)
    assert script is not None
    checks = """
    const eq = (a, b) => { if (a !== b) throw new Error(JSON.stringify(a) + " !== " + JSON.stringify(b)); };
    eq(laptopLoadCount({ cap: 0, busy: 3 }), null);
    const count = laptopLoadCount({ cap: 8, busy: 3, queued: 1, hosts: { devbox: 2 } });
    eq(count.name, "Load: 3/8 agents working · 2 on devbox · 1 queued");
    const summary = { title: "" }, panel = {};
    let bad = false;
    const box = { dataset: {}, children: [summary, panel], classList: { toggle: (c, on) => { bad = on; } } };
    globalThis.document = { getElementById: () => box };
    state.agents = { cap: 8, busy: 3, queued: 1, hosts: { devbox: 2 } };
    state.disk = { free: 50e9, total: 500e9, used_pct: 90, low: true, at: 0 };
    renderLaptop();
    eq(box.hidden, false);
    eq(summary.textContent, "Laptop · 3/8 agents · 1 queued · 50 GB free");
    eq(bad, true);
    state.agents = null;
    state.disk = null;
    renderLaptop();
    eq(box.hidden, true);
    """
    path = tmp_path / "guild.js"
    path.write_text(STUBS + script.group(1) + checks)
    done = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=20, check=False)
    assert done.returncode == 0, done.stderr


def test_config_reads_page_links_and_refuses_a_non_http_one() -> None:
    config = map_toml_to_config({"links": [{"name": "Versions", "url": "https://versions.example"}]})
    assert config.links == (PageLink(name="Versions", url="https://versions.example"),)
    for bad in ({"name": "x", "url": "javascript:alert(1)"}, {"url": "https://a.example"}):
        with pytest.raises(ValueError):
            map_toml_to_config({"links": [bad]})


def test_an_open_page_reloads_when_the_service_serves_another_version(tmp_path: Path) -> None:
    """A tab left open across a deploy kept its old script and showed wording the new page no longer has."""
    node = shutil.which("node")
    assert node, "node is required to check the page script"
    script = re.search(r"<script>(.*)</script>", PAGE.read_text(), re.S)
    assert script is not None
    checks = """
    const eq = (a, b) => { if (a !== b) throw new Error(JSON.stringify(a) + " !== " + JSON.stringify(b)); };
    let reloads = 0;
    let fields = [];
    globalThis.location = { reload: () => { reloads += 1; } };
    const page = globalThis.document;
    globalThis.document = new Proxy(page, { get: (t, k) => (k === "querySelectorAll" ? () => fields : t[k]) });
    state = { page_version: "aaa" };
    eq(isPageOutdated(), false);
    eq(isPageOutdated(), false);
    state = { page_version: "bbb" };
    fields = [{ value: "half a sentence" }];
    eq(isPageOutdated(), true);
    eq(reloads, 0);
    fields = [{ value: "  " }];
    eq(isPageOutdated(), true);
    eq(reloads, 1);
    """
    path = tmp_path / "guild.js"
    path.write_text(STUBS + script.group(1) + checks)
    done = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=20, check=False)
    assert done.returncode == 0, done.stderr


def test_section_nav_drops_hidden_sections_and_marks_the_one_under_the_bar(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "node is required to check the page script"
    script = re.search(r"<script>(.*)</script>", PAGE.read_text(), re.S)
    assert script is not None
    checks = """
    const eq = (a, b) => { if (a !== b) throw new Error(JSON.stringify(a) + " !== " + JSON.stringify(b)); };
    const tops = { needs: -900, queue: -400, leads: 60, chat: 900 };
    const sections = Object.fromEntries(["needs", "queue", "leads", "chat"].map((id) =>
      [id, { hidden: id === "needs", getBoundingClientRect: () => ({ top: tops[id], bottom: 0 }) }]));
    sections.sections = { getBoundingClientRect: () => ({ bottom: 42 }) };
    const tab = (id) => {
      const t = { hash: "#" + id, on: false };
      t.classList = { toggle: (c, on) => { t.on = on; } };
      return t;
    };
    const tabs = ["needs", "queue", "leads", "chat"].map(tab);
    globalThis.window = { innerHeight: 800, scrollY: 1000 };
    globalThis.document = { getElementById: (id) => sections[id], querySelectorAll: () => tabs,
                            documentElement: { scrollHeight: 5000 } };
    renderSectionNav();
    eq(tabs[0].hidden, true);
    eq(tabs.filter((t) => t.on).map((t) => t.hash).join(), "#leads");
    window.scrollY = 4200;
    renderSectionNav();
    eq(tabs.filter((t) => t.on).map((t) => t.hash).join(), "#chat");
    window.scrollY = 0;
    document.documentElement.scrollHeight = 800;
    tops.leads = 300;
    renderSectionNav();
    eq(tabs.filter((t) => t.on).map((t) => t.hash).join(), "#queue");
    """
    path = tmp_path / "guild.js"
    path.write_text(STUBS + script.group(1) + checks)
    done = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=20, check=False)
    assert done.returncode == 0, done.stderr


ALERT_CONFIG = Config(assistant_handle="aide", watcher_alert_to="tooling", watcher_escalate_after_min=60)
MIN = 60_000


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[sqlite3.Connection]:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    status_watchers._awake.clear()
    c = connect(tmp_path / "bridge.db")
    add_session(c, "tooling", "TAB-T", "idle", 0, pid=901)
    add_session(c, "aide", "TAB-A", "idle", 0, pid=902)
    yield c
    c.close()


def _job(tmp_path: Path, status: str, at: int, summary: str = "3 checks", detail: str = "") -> Config:
    path = tmp_path / "job.json"
    run = datetime.fromtimestamp(at / 1000, UTC).isoformat()
    fields = {"name": "nightly sync", "status": status, "last_run_utc": run, "summary": summary, "detail": detail}
    path.write_text(json.dumps(fields))
    return replace(ALERT_CONFIG, watchers=(Watcher(path=str(path)),))


def _told(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT r.handle, m.body FROM messages m JOIN message_recipients r ON r.message_id = m.id ORDER BY m.id"
    )
    return [(str(handle), str(body)) for handle, body in rows]


def test_an_ok_watcher_tells_nobody(conn: sqlite3.Connection, tmp_path: Path) -> None:
    assert alert_watchers(conn, _job(tmp_path, "ok", NOW), NOW) == []
    assert _told(conn) == []


def test_leaving_ok_tells_once_then_the_assistant_once_then_both_hear_it_is_back(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    alert_watchers(conn, _job(tmp_path, "ok", NOW), NOW)
    config = _job(tmp_path, "failed", NOW + MIN, summary="2 of 3 checks failed", detail="db unreachable")
    assert len(alert_watchers(conn, config, NOW + MIN)) == 1
    [(handle, body)] = _told(conn)
    assert handle == "tooling"
    assert re.fullmatch(
        r"nightly sync watch: failed: 2 of 3 checks failed \(since [\d-]+ [\d:]+\)\. db unreachable", body
    )

    for minutes in (30, 60):
        config = _job(tmp_path, "failed", NOW + minutes * MIN, summary="3 of 3 checks failed")
        assert alert_watchers(conn, config, NOW + minutes * MIN) == [], "a new summary is not a new alert"
    config = _job(tmp_path, "failed", NOW + 61 * MIN, summary="3 of 3 checks failed")
    assert len(alert_watchers(conn, config, NOW + 61 * MIN)) == 1, "escalation counts from when it left ok"
    handle, body = _told(conn)[-1]
    assert handle == "aide"
    assert body.startswith("nightly sync watch is still not ok after 60 min: failed: 3 of 3 checks failed (since ")
    assert re.search(r"tooling was told at [\d-]+ [\d:]+\.$", body)
    assert alert_watchers(conn, _job(tmp_path, "failed", NOW + 200 * MIN), NOW + 200 * MIN) == []

    assert len(alert_watchers(conn, _job(tmp_path, "ok", NOW + 201 * MIN), NOW + 201 * MIN)) == 2
    back = _told(conn)[-2:]
    assert sorted(h for h, _ in back) == ["aide", "tooling"]
    assert all(re.fullmatch(r"nightly sync watch is back to ok \(not ok since [\d-]+ [\d:]+\)\.", b) for _, b in back)
    assert alert_watchers(conn, _job(tmp_path, "ok", NOW + 202 * MIN), NOW + 202 * MIN) == []
    assert conn.execute("SELECT count(*) FROM signal_posts").fetchone()[0] == 0

    assert len(alert_watchers(conn, _job(tmp_path, "failed", NOW + 203 * MIN), NOW + 203 * MIN)) == 1
    assert _told(conn)[-1][0] == "tooling", "a new spell is told again"


def test_back_to_ok_before_the_escalation_tells_only_the_first_one(conn: sqlite3.Connection, tmp_path: Path) -> None:
    alert_watchers(conn, _job(tmp_path, "failed", NOW), NOW)
    alert_watchers(conn, _job(tmp_path, "ok", NOW + 5 * MIN), NOW + 5 * MIN)
    assert [h for h, _ in _told(conn)] == ["tooling", "tooling"]


def test_stale_and_missing_count_as_not_ok(conn: sqlite3.Connection, tmp_path: Path) -> None:
    config = _job(tmp_path, "ok", NOW - 30 * MIN)
    assert alert_watchers(conn, config, NOW) == [], "stale waits one grace: a Mac waking from sleep reads stale"
    stay_awake(NOW, NOW + 10 * MIN)
    assert len(alert_watchers(conn, config, NOW + 10 * MIN)) == 1
    assert re.match(
        r"nightly sync watch: stale: last run [\d-]+ [\d:]+, over its 20-min limit \(since ", _told(conn)[0][1]
    )
    (tmp_path / "job.json").unlink()
    assert alert_watchers(conn, config, NOW + 11 * MIN) == [], "stale to missing is the same spell"
    missing = replace(config, watchers=(Watcher(path=str(tmp_path / "gone.json"), name="backup"),))
    alert_watchers(conn, missing, NOW + 12 * MIN)
    assert _told(conn)[-1][1].startswith("backup watch: missing: no status file (since ")


def stay_awake(start: int, end: int) -> None:
    for at in range(start, end, 30_000):
        status_watchers.note_awake(at)


def test_the_stale_grace_counts_only_time_the_mac_is_awake(conn: sqlite3.Connection, tmp_path: Path) -> None:
    config = _job(tmp_path, "ok", NOW - 30 * MIN)
    stay_awake(NOW - MIN, NOW)
    assert alert_watchers(conn, config, NOW) == [], "a brief wake starts the spell"
    assert alert_watchers(conn, config, NOW + 16 * MIN) == [], "after a sleep the grace starts again"
    stay_awake(NOW + 16 * MIN, NOW + 26 * MIN)
    assert len(alert_watchers(conn, config, NOW + 26 * MIN)) == 1


def test_a_stale_watcher_that_runs_again_within_the_grace_tells_nobody(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    config = _job(tmp_path, "ok", NOW - 30 * MIN)
    assert alert_watchers(conn, config, NOW) == []
    _job(tmp_path, "ok", NOW + 5 * MIN)
    assert alert_watchers(conn, config, NOW + 5 * MIN) == []
    assert _told(conn) == []


def test_off_when_nobody_is_named(conn: sqlite3.Connection, tmp_path: Path) -> None:
    config = replace(_job(tmp_path, "failed", NOW), watcher_alert_to="")
    assert alert_watchers(conn, config, NOW) == []
    assert alert_watchers(conn, config, NOW + 120 * MIN) == []
    assert _told(conn) == []
    assert conn.execute("SELECT count(*) FROM signal_posts").fetchone()[0] == 0


def test_an_alert_that_reached_nobody_is_tried_again_and_the_assistant_hears_so(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    config = replace(_job(tmp_path, "failed", NOW), watcher_alert_to="nobody-here")
    assert alert_watchers(conn, config, NOW) == []
    stay_awake(NOW, NOW + 60 * MIN)
    assert alert_watchers(conn, config, NOW + 60 * MIN) != []
    assert _told(conn)[-1][1].endswith("nobody-here could not be reached.")
    assert alert_watchers(conn, replace(config, watcher_alert_to="tooling"), NOW + 61 * MIN) != []
    assert _told(conn)[-1][0] == "tooling"


def test_switching_alerts_off_forgets_the_spell(conn: sqlite3.Connection, tmp_path: Path) -> None:
    alert_watchers(conn, _job(tmp_path, "failed", NOW), NOW)
    alert_watchers(conn, replace(_job(tmp_path, "ok", NOW + MIN), watcher_alert_to=""), NOW + MIN)
    assert len(alert_watchers(conn, _job(tmp_path, "failed", NOW + 2 * MIN), NOW + 2 * MIN)) == 1
    assert [h for h, _ in _told(conn)] == ["tooling", "tooling"]


def test_an_alert_that_gets_through_as_the_escalation_falls_due_is_named_so(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    alert_watchers(conn, replace(_job(tmp_path, "failed", NOW), watcher_alert_to="nobody-here"), NOW)
    renamed = replace(_job(tmp_path, "failed", NOW + 60 * MIN), watcher_alert_to="tooling")
    assert len(alert_watchers(conn, renamed, NOW + 60 * MIN)) == 2
    assert re.search(r"tooling was told at [\d-]+ [\d:]+\.$", _told(conn)[-1][1])
