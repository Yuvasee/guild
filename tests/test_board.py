import json
import subprocess
import time
from pathlib import Path

from conftest import VENV_BIN, BridgeEnv, SpawnProcess

from bridge.install import convert_event_to_command


class Pair:
    def __init__(self, bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
        self.env = bridge_env
        self.a = spawn_process(env={"AGTERM_PANE_ID": "PANE-A"})
        self.b = spawn_process(env={"AGTERM_PANE_ID": "PANE-B"})
        bridge_env.write_session_file(self.a, "conv-a")
        bridge_env.write_session_file(self.b, "conv-b")
        tree = [("A", ["PANE-A"]), ("B", ["PANE-B"])]
        sessions = [{"name": n, "surfaces": [{"paneID": p} for p in panes]} for n, panes in tree]
        bridge_env.stub_tree.write_text(
            json.dumps({"ok": True, "result": {"tree": {"workspaces": [{"name": "W", "sessions": sessions}]}}})
        )
        for pid, conv in ((self.a, "conv-a"), (self.b, "conv-b")):
            self.prompt(pid, conv, "start")
        bridge_env.cli("sessions")

    def prompt(self, pid: int, conv: str, text: str) -> str:
        payload = {
            "session_id": conv,
            "cwd": str(self.env.root),
            "prompt": text,
            "prompt_id": f"{text}-{time.time_ns()}",
        }
        return _context(self.env.hook("UserPromptSubmit", payload, pid).stdout)


def test_request_reply_round_trip(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pair = Pair(bridge_env, spawn_process)

    sent = bridge_env.cli("send", "b", "can I take staging for 1h?", "--request", pid=pair.a)
    delivered = pair.prompt(pair.b, "conv-b", "next")
    redelivered = pair.prompt(pair.b, "conv-b", "again")
    acked = bridge_env.cli("ack", "1", pid=pair.b)
    replied = bridge_env.cli("reply", "1", "yes, release it by 18:00", "--done", pid=pair.b)
    to_sender = pair.prompt(pair.a, "conv-a", "next")
    inbox_b = bridge_env.cli("inbox", pid=pair.b)

    assert sent.returncode == 0, sent.stderr
    assert sent.stdout.startswith("#1 request → b")
    assert "#1 REQUEST from a, respond within 14m" in delivered or "#1 REQUEST from a, respond within 15m" in delivered
    assert "not from the user" in delivered
    assert redelivered == "", "a just-offered request is not re-offered before its backoff"
    assert acked.returncode == 0 and replied.returncode == 0, replied.stderr
    assert '#2 reply to #1 from b: "yes, release it by 18:00"' in to_sender
    assert inbox_b.stdout.strip() == "inbox empty"


def test_mid_turn_delivery_through_shell_guard(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pair = Pair(bridge_env, spawn_process)
    guard = convert_event_to_command(str(VENV_BIN / "bridge-hook"), "PostToolUse")

    def tool_call() -> str:
        payload = json.dumps({"session_id": "conv-b", "cwd": str(bridge_env.root), "tool_name": "Bash"})
        env = {**bridge_env.env, "CLAUDE_PID": str(pair.b)}
        done = subprocess.run(["/bin/sh", "-c", guard], input=payload, env=env, capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
        return _context(done.stdout)

    quiet = tool_call()
    bridge_env.cli("send", "b", "heads up: main is red", pid=pair.a)
    mid_turn = tool_call()
    after = tool_call()

    assert quiet == ""
    assert '#1 info from a: "heads up: main is red"' in mid_turn
    assert after == ""
    hint = Path(bridge_env.state_dir / "hints" / str(pair.b)).read_text()
    assert int(hint) > time.time()


def test_a_subagent_neither_sees_nor_settles_the_main_conversations_mail(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    pair = Pair(bridge_env, spawn_process)
    bridge_env.cli("send", "b", "heads up: main is red", pid=pair.a)
    base = {"session_id": "conv-b", "cwd": str(bridge_env.root), "tool_name": "Bash"}

    in_subagent = bridge_env.hook("PostToolUse", {**base, "agent_id": "a0000001"}, pair.b)
    with bridge_env.connect() as conn:
        untouched = conn.execute("SELECT offered_at, acked_at FROM message_recipients").fetchone()
    in_main = bridge_env.hook("PostToolUse", base, pair.b)

    assert _context(in_subagent.stdout) == ""
    assert (untouched["offered_at"], untouched["acked_at"]) == (None, None)
    assert '#1 info from a: "heads up: main is red"' in _context(in_main.stdout)


def test_overdue_request_escalates_and_resolves(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pair = Pair(bridge_env, spawn_process)
    bridge_env.cli("send", "b", "need review of #12", "--request", "--respond-within", "1s", pid=pair.a)
    time.sleep(1.2)
    bridge_env.cli("inbox", pid=pair.a)
    with bridge_env.connect() as conn:
        opened = conn.execute("SELECT text, source, resolved_at FROM user_items").fetchall()
    bridge_env.cli("decline", "1", pid=pair.b)
    with bridge_env.connect() as conn:
        resolved = conn.execute("SELECT resolved_at FROM user_items").fetchone()[0]
    assert [(r["source"], r["resolved_at"]) for r in opened] == [("overdue", None)]
    assert opened[0]["text"].startswith("a → b: no response to #1")
    assert resolved is not None


def test_injection_respects_byte_budget(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pair = Pair(bridge_env, spawn_process)
    for i in range(12):
        bridge_env.cli("send", "b", f"message {i} " + "é" * 300, pid=pair.a)
    first = pair.prompt(pair.b, "conv-b", "one")
    second = pair.prompt(pair.b, "conv-b", "two")
    shown = [line for line in (first + "\n" + second).splitlines() if line.startswith("#")]
    assert len(first.encode()) <= 1500
    assert "More waiting: bridge inbox" in first
    assert len({line.split()[0] for line in shown}) == len(shown), "nothing is shown twice"


def test_clear_keeps_open_mail_for_the_tab(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pair = Pair(bridge_env, spawn_process)
    bridge_env.cli("send", "b", "please rebase", "--request", pid=pair.a)
    cleared = bridge_env.hook("SessionStart", {"session_id": "conv-b2", "source": "clear"}, pair.b)
    assert "#1 REQUEST from a" in _context(cleared.stdout)


def test_tell_user_and_addressing_errors(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pair = Pair(bridge_env, spawn_process)
    told = bridge_env.cli("tell-user", "PR #12 is ready to merge", "--action", pid=pair.a)
    too_long = bridge_env.cli("tell-user", "x" * 200, pid=pair.a)
    unknown = bridge_env.cli("send", "nobody", "hi", pid=pair.a)
    to_self = bridge_env.cli("send", "a", "hi", pid=pair.a)
    with bridge_env.connect() as conn:
        items = [tuple(r) for r in conn.execute("SELECT from_handle, severity, text FROM user_items")]
    assert told.returncode == 0
    assert items == [("a", "action", "PR #12 is ready to merge")]
    assert too_long.returncode == 2
    assert unknown.returncode == 2 and "live handles: a, b" in unknown.stderr
    assert to_self.returncode == 2


def test_inbox_acknowledges_only_what_it_shows(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pair = Pair(bridge_env, spawn_process)
    with bridge_env.connect() as conn:
        conn.executemany(
            "INSERT INTO messages (id, created_at, from_conversation, from_handle, kind, thread_id, body) "
            "VALUES (?, ?, 'conv-a', 'a', 'info', ?, 'note')",
            [(i, i, i) for i in range(1, 52)],
        )
        conn.executemany(
            "INSERT INTO message_recipients (message_id, conversation_id, handle) VALUES (?, 'conv-b', 'b')",
            [(i,) for i in range(1, 52)],
        )
    bridge_env.cli("inbox", pid=pair.b)
    with bridge_env.connect() as conn:
        unacked = conn.execute("SELECT count(*) FROM message_recipients WHERE acked_at IS NULL").fetchone()[0]
    assert unacked == 1


def test_obligations_stay_within_budget_and_reply_acknowledges(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    pair = Pair(bridge_env, spawn_process)
    for i in range(8):
        bridge_env.cli("send", "b", f"obligation {i} " + "x" * 150, "--request", pid=pair.a)
    bridge_env.cli("ack", *[str(i) for i in range(1, 9)], pid=pair.b)
    for i in range(4):
        bridge_env.cli("send", "b", f"new request {i} " + "y" * 190, "--request", pid=pair.a)
    started = bridge_env.hook("SessionStart", {"session_id": "conv-b", "source": "resume"}, pair.b)
    replied = bridge_env.cli("reply", "9", "on it", pid=pair.b)
    with bridge_env.connect() as conn:
        acked = conn.execute("SELECT acked_at FROM message_recipients WHERE message_id = 9").fetchone()[0]
    context = _context(started.stdout)
    assert "Open requests you acknowledged" in context
    assert len(context.encode()) <= 1500
    assert replied.returncode == 0 and acked is not None


def test_missing_hint_file_still_checks_the_inbox(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pair = Pair(bridge_env, spawn_process)
    bridge_env.cli("send", "b", "fyi", pid=pair.a)
    (bridge_env.state_dir / "hints" / str(pair.b)).unlink()
    guard = convert_event_to_command(str(VENV_BIN / "bridge-hook"), "PostToolUse")
    payload = json.dumps({"session_id": "conv-b", "tool_name": "Bash"})
    env = {**bridge_env.env, "CLAUDE_PID": str(pair.b)}
    done = subprocess.run(["/bin/sh", "-c", guard], input=payload, env=env, capture_output=True, text=True)
    assert '#1 info from a: "fyi"' in _context(done.stdout)


def _context(stdout: str) -> str:
    if not stdout:
        return ""
    return str(json.loads(stdout)["hookSpecificOutput"]["additionalContext"])
