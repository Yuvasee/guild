import json
from pathlib import Path

import pytest

from bridge.config import Config
from bridge.db import connect
from bridge.gbridge import may_need_guard
from bridge.hooks import find_kill_by_name_refusal, handle_pre_tool_use
from bridge.install import convert_event_to_command, list_guard_globs

REFUSED = [
    "pkill -f 'pytest -q'",
    "cd /w && pkill -f suite-x",
    "sudo killall node",
    "timeout 5 pkill python",
    "ssh box 'pkill -f run'",
    'ssh -o BatchMode=yes box "cd /x && pkill -f run"',
    "bash -c 'killall node'",
    "pgrep -f worker | xargs kill",
    "pgrep -f worker | xargs -r kill -9",
    "kill $(pgrep -f worker)",
    "echo ok; killall -9 claude",
    "/usr/bin/pkill -f worker",
    "sudo -u dev pkill worker",
    "command pkill worker",
    "bash -lc 'pkill worker'",
    "if true; then pkill worker; fi",
    "kill `pgrep -f worker`",
    "pgrep -f worker | xargs /bin/kill",
    "ssh box -t pkill -f worker",
    "ssh -ti ~/.ssh/key box pkill -f worker",
]
ALLOWED = [
    "kill 4242",
    "kill -9 4242 4243",
    "pgrep -fl worker",
    "grep -n 'pkill' notes.md",
    "rg killall src/",
    "git commit -m 'no pkill here'",
    "git commit -m 'Avoid name killing; pkill affects other sessions'",
    "rg '(pkill|killall)' src/",
    "ls 'notes(pkill).md'",
    "cat <<'EOF'\npkill -f worker\nEOF",
    "pgrep -f worker | xargs echo kill",
    "pgrep -fl worker | xargs echo report; kill 4242",
    "pgrep -fl worker; xargs kill < owned-pids.txt",
]


@pytest.mark.parametrize("command", REFUSED)
def test_killing_by_name_or_pattern_is_refused(command: str) -> None:
    assert find_kill_by_name_refusal(command) is not None
    assert may_need_guard(list_guard_globs(), command), "the shell gate must start the guard for it"


@pytest.mark.parametrize("command", ALLOWED)
def test_killing_by_pid_and_mentions_pass(command: str) -> None:
    assert find_kill_by_name_refusal(command) is None


def test_the_hook_denies_it_with_the_reason(tmp_path: Path) -> None:
    conn = connect(tmp_path / "b.db")
    payload: dict[str, object] = {
        "session_id": "c",
        "cwd": "/w",
        "tool_name": "Bash",
        "tool_input": {"command": "pkill -f suite"},
    }

    decision = json.loads(handle_pre_tool_use(conn, payload, Config()))["hookSpecificOutput"]

    assert decision["permissionDecision"] == "deny"
    assert "kill <PID>" in decision["permissionDecisionReason"]


def test_a_long_harmless_command_is_read_quickly() -> None:
    import time

    text = "printf '%s' '" + "pgrep | xargs echo " * 2000 + "'"
    started = time.monotonic()
    assert find_kill_by_name_refusal(text) is None
    assert time.monotonic() - started < 1


def test_a_long_shell_flag_is_read_quickly() -> None:
    import time

    started = time.monotonic()
    find_kill_by_name_refusal("bash -c : -- -" + "c" * 64000 + "1 pkill")
    assert time.monotonic() - started < 1


@pytest.mark.parametrize("command", ["pkill -f suite", "killall node", "pgrep -f w | xargs kill"])
def test_the_macs_shell_gate_starts_the_hook_without_a_claim(tmp_path: Path, command: str) -> None:
    import subprocess

    hook = tmp_path / "hook"
    hook.write_text("#!/bin/sh\ncat > /dev/null; echo started\n")
    hook.chmod(0o755)
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    gate = convert_event_to_command(str(hook), "PreToolUse")

    done = subprocess.run(
        ["sh", "-c", gate],
        input=payload,
        capture_output=True,
        text=True,
        env={"BRIDGE_STATE_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )

    assert done.stdout.strip() == "started", "no claims-active file: the hook must start anyway"
