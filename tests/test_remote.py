import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import VENV_BIN, BridgeEnv, free_port, http_request, wait_up

from bridge.config import Config, Remote
from bridge.remote import Collected, attach_command, collect, list_remote_sessions, store

POD = Remote(name="devbox", ssh="devbox")
CONFIG = Config(issue_prefixes=("ABC",))


def session(tmux: str, state: str, prompt: str) -> dict[str, object]:
    return {
        "tmux": tmux, "tmux_id": "$3", "pane": "0.0", "pid": 42, "started_at": 1000, "agent": "codex",
        "state": state, "prompt": prompt, "reply": "done", "activity_at": 5_000,
        "branch": "feat/abc-1", "origin": "git@github.com:acme/web.git", "cwd": "/home/dev/web",
    }  # fmt: skip


def test_store_replaces_on_success_and_keeps_rows_on_failure(bridge_env: BridgeEnv) -> None:
    with bridge_env.connect() as conn:
        store(conn, POD, Collected(True, None, [session("s10", "working", "fix ABC-7, PR #12")], 4_000), CONFIG, 10_000)
        first = list_remote_sessions(conn, 10_000)
        store(conn, POD, Collected(False, "ssh TimeoutExpired", [], None), CONFIG, 20_000)
        kept = list_remote_sessions(conn, 20_000)
        later = list_remote_sessions(conn, 10_000 + 400_000)
        error = conn.execute("SELECT error, error_since FROM remote_hosts").fetchone()

    [row] = first
    assert (row["handle"], row["status"], row["agent"], row["repo"]) == ("devbox/s10", "active", "codex", "acme/web")
    assert row["last_activity_at"] == 5_000 + 6_000, "remote clock skew is corrected"
    assert row["refs"] == ["issue:ABC-7", "pr:acme/web#12"]
    assert [r["instance_id"] for r in kept] == [row["instance_id"]]
    assert later[0]["status"] == "stale"
    assert tuple(error) == ("ssh TimeoutExpired", 20_000)


def test_attach_command_quotes_both_shell_layers() -> None:
    assert attach_command(POD, "$3") == "ssh -t devbox 'tmux attach -t '\"'\"'$3'\"'\"''"
    with pytest.raises(ValueError):
        attach_command(POD, "$3; rm -rf ~")


def test_probe_runs_isolated_and_prints_json(tmp_path: Path) -> None:
    probe = Path(__file__).parent.parent / "src/bridge/remote_probe.py"
    done = subprocess.run(
        [sys.executable, "-I", "-S", "-"], input=probe.read_text(), capture_output=True, text=True, cwd=tmp_path
    )
    assert done.returncode == 0, done.stderr
    assert isinstance(json.loads(done.stdout)["sessions"], list)


def test_collect_from_a_real_remote() -> None:
    host = os.environ.get("BRIDGE_TEST_REMOTE")
    if not host:
        pytest.skip("set BRIDGE_TEST_REMOTE=<ssh host> to probe a real remote")
    result = collect(Remote(name="test", ssh=host))
    assert result.ok, result.error
    assert all("tmux" in s and "agent" in s for s in result.sessions)


def test_unreadable_transcript_keeps_previous_text(bridge_env: BridgeEnv) -> None:
    with bridge_env.connect() as conn:
        store(conn, POD, Collected(True, None, [session("s10", "working", "first prompt")], 4_000), CONFIG, 10_000)
        blank = {**session("s10", "working", ""), "prompt": None, "reply": None}
        store(conn, POD, Collected(True, None, [blank], 4_000), CONFIG, 20_000)
        [row] = list_remote_sessions(conn, 20_000)
    assert (row["last_prompt"], row["last_reply"]) == ("first prompt", "done")


def test_dashboard_attach_opens_an_agterm_tab(bridge_env: BridgeEnv) -> None:
    config = Path(bridge_env.env["BRIDGE_CONFIG"])
    config.write_text(config.read_text() + '\n[[remotes]]\nname = "devbox"\nssh = "devbox"\n')
    with bridge_env.connect() as conn:
        store(conn, POD, Collected(True, None, [session("s10", "idle", "x")], 4_000), CONFIG, 10_000)
    port = free_port()
    server = subprocess.Popen([str(VENV_BIN / "bridge"), "serve", "--port", str(port)], env=bridge_env.env)
    try:
        wait_up(f"http://127.0.0.1:{port}")
        body = http_request(f"http://127.0.0.1:{port}/api/remote/devbox/%243/attach", post=True)
    finally:
        server.terminate()
        server.wait()
    assert json.loads(body)["ok"] is True
    assert bridge_env.stub_log.read_text() == f"session new --name devbox $3 --command {attach_command(POD, '$3')}\n"


def test_conversation_switch_does_not_inherit_text_and_misses_are_tolerated(bridge_env: BridgeEnv) -> None:
    first = {**session("s10", "idle", "old prompt"), "conversation_id": "c1"}
    switched = {**session("s10", "idle", ""), "conversation_id": "c2", "prompt": None, "reply": None}
    with bridge_env.connect() as conn:
        store(conn, POD, Collected(True, None, [first], 4_000), CONFIG, 10_000)
        store(conn, POD, Collected(True, None, [switched], 4_000), CONFIG, 20_000)
        [row] = list_remote_sessions(conn, 20_000)
        assert (row["last_prompt"], row["last_reply"]) == (None, None)
        refs_session = {**session("s11", "idle", "see PR #12"), "tmux_id": "$4", "conversation_id": "c9"}
        store(conn, POD, Collected(True, None, [refs_session], 4_000), CONFIG, 25_000)
        store(conn, POD, Collected(True, None, [], 4_000), CONFIG, 30_000)
        kept = list_remote_sessions(conn, 30_000)
        assert len(kept) == 1, "one miss keeps the session"
        assert kept[0]["refs"] == ["pr:acme/web#12"], "and its refs"
        store(conn, POD, Collected(True, None, [], 4_000), CONFIG, 40_000)
        assert list_remote_sessions(conn, 40_000) == []
