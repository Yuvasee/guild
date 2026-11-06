import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_page import STUBS

from bridge.config import Watcher, map_toml_to_config
from bridge.status_watchers import MAX_FILE_BYTES, read_watcher

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
    eq(ok.bad, false);
    const stale = mapWatcherToLine({ ...base, state: "stale", last_run_at: now - 34 * 60_000 }, now);
    eq(stale.words, "no sweep for 34 min");
    eq(stale.bad, true);
    eq(stale.tip.includes("It said: 0 new"), true);
    const failed = mapWatcherToLine(
      { ...base, state: "failed", status: "failed", last_run_at: now - 120_000, detail: "502" }, now);
    eq(failed.words, "last sweep failed 2 min ago, 0 new");
    eq(failed.tip.includes("502"), true);
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


def test_page_rewrites_pills_at_most_once_a_minute(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "node is required to check the page script"
    script = re.search(r"<script>(.*)</script>", PAGE.read_text(), re.S)
    assert script is not None
    checks = """
    let writes = 0;
    const pills = [];
    const box = { hidden: false, children: pills,
                  replaceChildren(...nodes) { pills.splice(0, pills.length, ...nodes); } };
    globalThis.document = {
      getElementById: () => box,
      createElement: () => ({ dataset: {}, append() {}, replaceChildren() { writes++; } }),
    };
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
    if (writes !== 6) throw new Error("pills rewritten " + writes + " times");
    if ([...moments].join() !== "0,60,120") throw new Error("rewritten at seconds " + [...moments].join());
    """
    path = tmp_path / "guild.js"
    path.write_text(STUBS + script.group(1) + checks)
    done = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=20, check=False)
    assert done.returncode == 0, done.stderr
