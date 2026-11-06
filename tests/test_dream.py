import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from conftest import BridgeEnv, SpawnProcess, wait_for
from test_leads import Team

from bridge import dream
from bridge.board import BODY_MAX_CHARS
from bridge.config import map_toml_to_config
from bridge.dream import DREAM_FRAME, DREAM_MAX_CHARS, TRANSCRIPT_CUT_MARK, DreamCut, Rule, cut_dream, fit_transcript

FAKE_CLAUDE = """#!{python}
import json, os, sys, time
d = {dir!r}
open(os.path.join(d, "args.json"), "w").write(json.dumps(sys.argv[1:]))
open(os.path.join(d, "stdin.txt"), "w").write(sys.stdin.read())
open(os.path.join(d, "env.json"), "w").write(json.dumps(dict(os.environ)))
with open(os.path.join(d, "spans.log"), "a") as spans:
    spans.write(f"start {{time.time()}}\\n")
if os.path.exists(os.path.join(d, "sleep")):
    time.sleep(float(open(os.path.join(d, "sleep")).read()))
with open(os.path.join(d, "spans.log"), "a") as spans:
    spans.write(f"end {{time.time()}}\\n")
sys.stdout.write(open(os.path.join(d, "result.json")).read())
sys.exit(int(open(os.path.join(d, "exit")).read()) if os.path.exists(os.path.join(d, "exit")) else 0)
"""


class Night:
    def __init__(self, bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
        self.env = bridge_env
        self.team = Team(bridge_env, spawn_process)
        assert self.team.enroll("Alex", "old-a").returncode == 0
        assert bridge_env.cli("handle", "aide", pid=self.team.b).returncode == 0
        self.fake = bridge_env.root / "fake"
        self.fake.mkdir()
        claude = self.fake / "claude"
        claude.write_text(FAKE_CLAUDE.format(python=sys.executable, dir=str(self.fake)))
        claude.chmod(0o755)
        bridge_env.env["PATH"] = f"{self.fake}:{bridge_env.env.get('PATH', '')}"
        self.project = bridge_env.root / "projects" / "-repo"
        self.project.mkdir(parents=True)
        self.lead_dir = bridge_env.state_dir / "leads" / "alex"

    def answer(self, dream_text: str, rules: list[dict[str, str]] | None = None, **extra: object) -> None:
        result = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "",
            "structured_output": {"dream": dream_text, "rules": rules or []},
            "usage": {
                "input_tokens": 3,
                "cache_creation_input_tokens": 1200,
                "cache_read_input_tokens": 40,
                "output_tokens": 500,
            },
            "total_cost_usd": 0.42,
            **extra,
        }
        (self.fake / "result.json").write_text(json.dumps(result))

    def write_transcript(self, entries: list[dict[str, object]], conversation: str = "conv-a") -> None:
        with (self.project / f"{conversation}.jsonl").open("w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

    def dream(self, *leads: str) -> subprocess.CompletedProcess[str]:
        return self.team.guild("dream", *(leads or ("alex",)), "--wait")

    def runs(self, directory: Path | None = None) -> list[dict[str, object]]:
        log = (directory or self.lead_dir) / "dream-runs.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()]

    def configure(self, text: str) -> None:
        config = Path(self.env.env["BRIDGE_CONFIG"])
        config.write_text(config.read_text() + text)

    def stdin(self) -> str:
        return (self.fake / "stdin.txt").read_text()


def _ago(minutes: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _entry(kind: str, content: object, minutes_ago: int, uuid: str) -> dict[str, object]:
    return {"type": kind, "timestamp": _ago(minutes_ago), "uuid": uuid, "message": {"role": kind, "content": content}}


def test_a_night_dreams_from_the_day_and_the_dream_reaches_only_the_lead(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    night = Night(bridge_env, spawn_process)
    night.write_transcript(
        [
            _entry("user", "the owner says: never merge with an open thread", 120, "aaaaaaaa11"),
            _entry("assistant", [{"type": "text", "text": "Understood, holding the merge."}], 119, "bbbbbbbb22"),
            _entry("assistant", [{"type": "tool_use", "name": "Bash", "input": {"command": "gh pr view 7"}}], 118, "c"),
            _entry("user", "from two days ago, outside the window", 60 * 48, "dddddddd44"),
        ]
    )
    (night.project / "memory").mkdir()
    (night.project / "memory" / "MEMORY.md").write_text("- Explainers are HTML\n")
    assert night.team.guild("notes", "--write", pid=night.team.a, stdin="Owns PROJ-33.\n").returncode == 0
    assert bridge_env.cli("send", "alex", "the deploy waits for the owner", pid=night.team.b).returncode == 0
    night.answer(
        "a merge button glowing behind a closed door; the owner's voice: not yet",
        [{"rule": "Never merge while a thread of the owner's is open.", "source": "2026-11-03 08:22 aaaaaaaa"}],
    )

    done = night.dream()
    started = night.team.start(night.team.a, "conv-a", "compact")
    assistant_start = night.team.start(night.team.b, "conv-b", "compact")

    assert done.returncode == 0, done.stderr
    assert "Alex: ok:" in done.stdout
    args = json.loads((night.fake / "args.json").read_text())
    assert args[:2] == ["-p", "--model"] and args[2] == "opus"
    for flag in ("--safe-mode", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in args
    assert args[args.index("--tools") + 1] == ""
    assert json.loads(args[args.index("--settings") + 1]) == {"disableAllHooks": True}
    assert "Dream it. Not a report" in args[args.index("--system-prompt") + 1]
    env = json.loads((night.fake / "env.json").read_text())
    assert not [k for k in env if k.startswith(("CLAUDE", "AGTERM_"))]
    material = night.stdin()
    assert "never merge with an open thread" in material
    assert "Understood, holding the merge." in material
    assert 'tool Bash: {"command": "gh pr view 7"}' in material
    assert "outside the window" not in material
    assert "the deploy waits for the owner" in material
    assert "Owns PROJ-33." in material
    assert "Explainers are HTML" in material
    assert "You are Alex, a team lead" in material
    assert "Your notes are empty" not in material

    assert (night.lead_dir / "dream.md").read_text().startswith("a merge button glowing")
    assert len(list((night.lead_dir / "dreams").glob("*.md"))) == 1
    [run] = night.runs()
    assert run["outcome"] == "ok" and run["rules"] == 1 and run["dream_cut_from"] is None
    assert run["tokens"] == {"input": 3, "cache_creation_input": 1200, "cache_read_input": 40, "output": 500}
    assert run["cost_usd"] == 0.42 and isinstance(run["seconds"], float)

    with bridge_env.connect() as conn:
        rows = conn.execute(
            "SELECT m.body, r.handle FROM messages m JOIN message_recipients r ON r.message_id = m.id "
            "WHERE m.from_handle = 'bridge' AND m.body LIKE 'Proposed durable rules%'"
        ).fetchall()
    assert [r["handle"] for r in rows] == ["aide"]
    rule = "1. Never merge while a thread of the owner's is open. (source: 2026-11-03 08:22 aaaaaaaa)"
    assert rule in rows[0]["body"]
    assert "glowing" not in rows[0]["body"]

    assert DREAM_FRAME in started
    assert "a merge button glowing" in started
    assert started.index("You are Alex") < started.index(DREAM_FRAME)
    assert "glowing" not in assistant_start


def test_the_next_night_starts_where_the_last_good_dream_ended(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    night = Night(bridge_env, spawn_process)
    night.answer("first dream")
    assert night.dream().returncode == 0
    night.answer("second dream")

    assert night.dream().returncode == 0

    first, second = night.runs()
    assert second["since_ms"] == first["until_ms"]
    assert len(list((night.lead_dir / "dreams").glob("*.md"))) == 2
    assert "## Your previous dream\nfirst dream" in night.stdin()


def test_a_failed_night_keeps_the_previous_dream_and_says_why(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    night = Night(bridge_env, spawn_process)
    night.answer("the good dream")
    assert night.dream().returncode == 0
    night.answer("", subtype="error_max_budget_usd", is_error=True)

    failed = night.dream()
    (night.fake / "result.json").write_text("not json")
    (night.fake / "exit").write_text("1")
    crashed = night.dream()

    assert failed.returncode == 1 and crashed.returncode == 1
    assert (night.lead_dir / "dream.md").read_text() == "the good dream\n"
    _, over_budget, broken = night.runs()
    assert over_budget["outcome"] == "failed" and "error_max_budget_usd" in str(over_budget["why"])
    assert broken["outcome"] == "failed" and "without a JSON result" in str(broken["why"])


def test_a_long_dream_is_cut_under_the_cap_and_the_log_says_so(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    night = Night(bridge_env, spawn_process)
    night.answer(("The corridor folds again. " * 60 + "\n\n") * 4)

    assert night.dream().returncode == 0

    stored = (night.lead_dir / "dream.md").read_text().strip()
    [run] = night.runs()
    assert len(stored) <= DREAM_MAX_CHARS
    assert stored.endswith("…")
    assert run["dream_cut_from"] == len((("The corridor folds again. " * 60 + "\n\n") * 4).strip())


def test_no_rules_sends_nothing_and_no_lead_named_is_not_an_error(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    night = Night(bridge_env, spawn_process)
    night.answer("quiet night")

    assert night.dream().returncode == 0
    nobody = night.team.guild("dream")

    with bridge_env.connect() as conn:
        assert not conn.execute("SELECT 1 FROM messages WHERE body LIKE 'Proposed durable rules%'").fetchall()
    assert nobody.returncode == 0 and "no lead to dream" in nobody.stdout


def test_a_dream_that_does_not_fit_the_start_comes_with_the_next_prompt(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    night = Night(bridge_env, spawn_process)
    night.answer("d" * 2900)
    assert night.dream().returncode == 0
    keep = subprocess.run(
        [str(Path(sys.executable).parent / "bridge"), "keep"],
        input="k" * 6000,
        env={**bridge_env.env, "CLAUDE_PID": str(night.team.a)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert keep.returncode == 0, keep.stderr

    started = night.team.start(night.team.a, "conv-a", "compact")
    following = night.team.prompt(night.team.a, "conv-a", "go on")

    assert "k" * 6000 in started and len(started) <= 10_000
    assert DREAM_FRAME not in started
    assert DREAM_FRAME in following and "d" * 2900 in following and len(following) <= 10_000


def test_an_old_dream_names_its_night(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    path = tmp_path / "leads" / "alex" / "dream.md"
    path.parent.mkdir(parents=True)
    path.write_text("old fog\n")
    written = int(path.stat().st_mtime * 1000)

    fresh = dream.convert_dream_to_context(path.parent, written + 3600_000)
    stale = dream.convert_dream_to_context(path.parent, written + 3 * 24 * 3600_000)

    assert fresh == f"[guild] {DREAM_FRAME}\nold fog"
    assert stale is not None and "from last night" not in stale and "Your dream from the night of " in stale
    assert dream.convert_dream_to_context(tmp_path / "leads" / "boris") is None


def test_a_claude_that_runs_too_long_is_stopped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    claude = tmp_path / "claude"
    claude.write_text(f"#!{sys.executable}\nimport time; time.sleep(5)\n")
    claude.chmod(0o755)

    with pytest.raises(DreamCut, match="ran over 1 s"):
        dream.call_claude(str(claude), "opus", "dream", "day", 1)


def test_the_transcript_drops_tools_first_and_then_the_oldest_talk() -> None:
    lines = [("t1", "old talk " * 10, False), ("t2", "tool " * 50, True), ("t3", "late talk " * 10, False)]

    whole, none = fit_transcript(lines, 10_000)
    talk, tools_cut = fit_transcript(lines, 250)
    tail, both_cut = fit_transcript(lines, 160)

    assert none is None and len(whole) == 3
    assert tools_cut == "tool calls and results dropped" and talk == ["old talk " * 10, "late talk " * 10]
    assert both_cut is not None and "oldest" in both_cut and tail == [TRANSCRIPT_CUT_MARK, "late talk " * 10]


def test_cut_dream_ends_on_a_sentence() -> None:
    text = "Short one. " * 400

    cut, original = cut_dream(text)

    assert original == len(text) and len(cut) <= DREAM_MAX_CHARS and cut.endswith("one.…")
    assert cut_dream("small") == ("small", None)


def test_a_cleared_conversation_still_belongs_to_the_day(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    night = Night(bridge_env, spawn_process)
    (night.project / "conv-a.jsonl").write_text(json.dumps(_entry("user", "morning words", 90, "m1")) + "\n")
    night.team.start(night.team.a, "conv-a2", "clear")
    (night.project / "conv-a2.jsonl").write_text(json.dumps(_entry("user", "evening words", 10, "e1")) + "\n")
    night.answer("both")

    assert night.dream().returncode == 0

    material = night.stdin()
    assert "morning words" in material and "evening words" in material
    assert night.runs()[0]["sources_missing"] == []


def test_a_flood_of_board_posts_leaves_room_for_the_conversation(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    night = Night(bridge_env, spawn_process)
    night.write_transcript([_entry("user", "the one thing the owner said", 30, "u1")])
    with bridge_env.connect() as conn:
        for n in range(300):
            conn.execute(
                "INSERT INTO messages (created_at, from_conversation, from_handle, kind, board, body) "
                "VALUES (?, 'x', 'boris', 'info', 1, ?)",
                (int(time.time() * 1000) - 60_000, f"post {n} " + "x" * 1900),
            )
    night.answer("noise")

    assert night.dream().returncode == 0

    material = night.stdin()
    assert len(material) <= dream.INPUT_MAX_CHARS
    assert "the one thing the owner said" in material
    assert "post 299 " in material and "post 0 " not in material


def test_seven_rules_at_full_length_all_reach_the_assistant() -> None:
    longest = Rule(dream._clip("r" * 999, dream.RULE_MAX_CHARS), dream._clip("s" * 999, dream.RULE_SOURCE_MAX_CHARS))  # pyright: ignore[reportPrivateUsage]
    rules = [longest] * dream.RULES_MAX

    body = dream.convert_rules_to_message("Alex", rules, 0)

    assert body.count("\n") == dream.RULES_MAX and len(body) <= BODY_MAX_CHARS


def test_all_is_every_lead_not_retired_and_the_assistant(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, monkeypatch: pytest.MonkeyPatch
) -> None:
    night = Night(bridge_env, spawn_process)
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(bridge_env.state_dir))
    with bridge_env.connect() as conn:
        for name, retired in (("boris", None), ("charlie", 1)):
            conn.execute(
                "INSERT INTO leads (name, display_name, scope, linear_refs, tint, created_at, retired_at) "
                "SELECT ?, ?, scope, linear_refs, tint, created_at + 1, ? FROM leads WHERE name = 'alex'",
                (name, name.capitalize(), retired),
            )
    night.configure('dream_leads = ["all"]\n')

    with bridge_env.connect() as conn:
        config = map_toml_to_config(tomllib.loads(Path(bridge_env.env["BRIDGE_CONFIG"]).read_text()))
        dreamers = dream.resolve_dreamers(conn, config, [])

    assert [d.handle for d in dreamers] == ["alex", "boris", "aide"]
    assert dreamers[-1].lead is None and dreamers[-1].directory == bridge_env.state_dir / "assistant"


def test_the_assistant_dreams_its_own_tab_and_wakes_with_it_below_its_role(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    night = Night(bridge_env, spawn_process)
    night.write_transcript([_entry("user", "the owner to the assistant: route it to Alex", 30, "x1")], "conv-b")
    night.write_transcript([_entry("user", "only in the lead's tab", 30, "y1")])
    noted = night.team.guild("notes", "--write", pid=night.team.b, stdin="Sam wants one question at a time.\n")
    assert noted.returncode == 0
    night.answer("a switchboard humming with Alex's light on")

    done = night.dream("aide")
    started = night.team.start(night.team.b, "conv-b", "compact")
    lead_start = night.team.start(night.team.a, "conv-a", "compact")

    assert done.returncode == 0, done.stderr
    material = night.stdin()
    assert "route it to Alex" in material and "only in the lead's tab" not in material
    assert "Right Hand" in material and "Sam wants one question at a time." in material
    prompt = json.loads((night.fake / "args.json").read_text())
    system = prompt[prompt.index("--system-prompt") + 1]
    assert "You are Aide, the owner's Right Hand" in system and "relayed by" not in system
    assistant_dir = bridge_env.state_dir / "assistant"
    assert (assistant_dir / "dream.md").read_text().startswith("a switchboard humming")
    [run] = night.runs(assistant_dir)
    assert run["lead"] == "aide" and run["outcome"] == "ok"
    assert DREAM_FRAME in started and started.index("Right Hand") < started.index("a switchboard humming")
    assert "switchboard" not in lead_start


def test_wait_all_dreams_in_parallel_and_prints_time_and_tokens(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    night = Night(bridge_env, spawn_process)
    night.answer("shared fog")
    (night.fake / "sleep").write_text("1.5")

    done = night.team.guild("dream", "alex", "aide", "--wait-all")

    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert lines[0].startswith("Alex: ok, ") and lines[0].endswith(" s, 1,743 tokens")
    assert lines[1].startswith("Aide: ok, ")
    assert lines[2].startswith("total: ") and lines[2].endswith(" s, 3,486 tokens") and "$" not in done.stdout
    spans = (night.fake / "spans.log").read_text().split()
    starts = [float(v) for k, v in zip(spans[::2], spans[1::2], strict=True) if k == "start"]
    ends = [float(v) for k, v in zip(spans[::2], spans[1::2], strict=True) if k == "end"]
    assert len(starts) == 2 and max(starts) < min(ends)
    with bridge_env.connect() as conn:
        assert not conn.execute("SELECT 1 FROM messages WHERE body LIKE 'No dream tonight%'").fetchall()


def test_a_dream_over_the_limit_is_cut_keeps_the_last_one_and_the_assistant_hears_of_it(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    night = Night(bridge_env, spawn_process)
    night.answer("the good dream")
    assert night.dream().returncode == 0
    night.configure("dream_timeout_s = 1\n")
    (night.fake / "sleep").write_text("4")

    done = night.team.guild("dream", "alex", "--wait-all")

    assert done.returncode == 1
    assert done.stdout.startswith("Alex: cut, ")
    assert "tokens unknown (claude ran over 1 s and was stopped)" in done.stdout
    assert (night.lead_dir / "dream.md").read_text() == "the good dream\n"
    assert night.runs()[-1]["outcome"] == "cut"
    with bridge_env.connect() as conn:
        [row] = conn.execute(
            "SELECT m.body, r.handle FROM messages m JOIN message_recipients r ON r.message_id = m.id "
            "WHERE m.body LIKE 'No dream tonight%'"
        ).fetchall()
    assert row["handle"] == "aide"
    assert row["body"] == (
        "No dream tonight for Alex (cut: claude ran over 1 s and was stopped). Each keeps its previous dream."
    )


def test_the_night_never_runs_more_than_its_cap_at_once(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[str] = []
    finished: list[str] = []
    live: set[str] = set()

    class Process:
        def __init__(self, night: str, handle: str) -> None:
            assert night == "n1"
            self.handle, self.polls = handle, 0
            self.returncode: int | None = None
            started.append(handle)
            live.add(handle)
            assert len(live) <= 2

        def poll(self) -> int | None:
            self.polls += 1
            if self.polls < 2:
                return None
            live.discard(self.handle)
            finished.append(self.handle)
            self.returncode = 0
            return 0

    monkeypatch.setattr(dream, "start_dream", Process)
    monkeypatch.setattr(dream.time, "sleep", lambda _s: None)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(dream, "_stop", lambda _p: None)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(dream, "send_night_summary", lambda *_a: None)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    dreamers = [dream.Dreamer(h, h, bridge_env.root / h, None, None) for h in ("a", "b", "c", "d", "e")]

    runs = dream.run_night(cast(sqlite3.Connection, None), map_toml_to_config({}), "n1", dreamers, parallel=2)

    assert started == ["a", "b", "c", "d", "e"] and sorted(finished) == started
    assert [r.dreamer.handle for r in runs] == started
    assert {r.why for r in runs} == {"the dream process ended without a log line"}


def test_a_timeout_stops_what_claude_started_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    marker = tmp_path / "child.pid"
    claude = tmp_path / "claude"
    claude.write_text(
        f"#!{sys.executable}\nimport subprocess, time\n"
        f"p = subprocess.Popen(['sleep', '30'])\nopen({str(marker)!r}, 'w').write(str(p.pid))\ntime.sleep(30)\n"
    )
    claude.chmod(0o755)

    with pytest.raises(DreamCut):
        dream.call_claude(str(claude), "opus", "dream", "day", 1)

    child = int(marker.read_text())
    assert subprocess.run(["kill", "-0", str(child)], capture_output=True, check=False).returncode != 0


def test_the_night_stopping_a_dream_stops_its_claude(tmp_path: Path) -> None:
    marker = tmp_path / "claude.pid"
    claude = tmp_path / "claude"
    claude.write_text(
        f"#!{sys.executable}\nimport os, time\nopen({str(marker)!r}, 'w').write(str(os.getpid()))\ntime.sleep(30)\n"
    )
    claude.chmod(0o755)
    script = (
        "from bridge import dream; dream.stop_claude_on_term(); "
        f"dream.call_claude({str(claude)!r}, 'opus', 'dream', 'day', 60)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script], env={**os.environ, "BRIDGE_STATE_DIR": str(tmp_path)}, start_new_session=True
    )
    assert wait_for(lambda: marker.exists() and marker.read_text() != "")

    dream._stop(process)  # pyright: ignore[reportPrivateUsage]

    assert process.returncode == 128 + signal.SIGTERM
    assert wait_for(lambda: subprocess.run(["kill", "-0", marker.read_text()], check=False).returncode != 0)


def test_a_term_while_the_dream_is_stored_waits_for_the_night_mark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dream, "_claude_group", None)
    dream.stop_claude_on_term()
    try:
        with pytest.raises(SystemExit) as stopped, dream._term_held():  # pyright: ignore[reportPrivateUsage]
            os.kill(os.getpid(), signal.SIGTERM)
            (tmp_path / "done").write_text("both writes happen")
        assert stopped.value.code == 128 + signal.SIGTERM
        assert (tmp_path / "done").exists()
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        monkeypatch.setattr(dream, "_term_pending", None)
