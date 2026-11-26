import json
import subprocess
from pathlib import Path

import pytest

from bridge.config import Config
from bridge.gbridge import may_need_guard
from bridge.hooks import find_rule_refusal
from bridge.install import convert_event_to_command, list_guard_globs

FAST = [
    "gh run watch",
    "gh run watch 123456",
    "gh run watch 123456 --exit-status",
    "gh run watch 123456 -i 10",
    "gh run watch 123456 --interval=30",
    "gh run watch -R acme/web 123456 -i59",
    'gh run watch 123456 --interval "$EVERY"',
    "gh run watch 123456 -i",
    "sleep 60; gh run watch 123456",
    "bash -c 'gh run watch 123456 --compact'",
]
SLOW_OR_OTHER = [
    "gh run watch 123456 --interval 60",
    "gh run watch 123456 -i 300 --exit-status",
    "gh run watch 123456 -i=120",
    "gh run watch 123456 --interval=90 --compact",
    "gh run watch --help",
    "gh run view 123456 --log-failed",
    "gh pr checks 42",
    "git commit -m 'stop using gh run watch'",
]


@pytest.mark.parametrize("command", FAST)
def test_a_run_watch_faster_than_once_a_minute_is_refused(command: str) -> None:
    refusal = find_rule_refusal(command, Config())

    assert refusal is not None and "Pass --interval 60 or more" in refusal and "gh pr checks" in refusal
    assert may_need_guard(list_guard_globs(), command), "the host's gate must start the guard for it"


@pytest.mark.parametrize("command", SLOW_OR_OTHER)
def test_a_minute_or_slower_and_other_gh_commands_pass(command: str) -> None:
    assert find_rule_refusal(command, Config()) is None


def test_the_macs_shell_gate_starts_the_hook_with_no_claim(tmp_path: Path) -> None:
    hook = tmp_path / "hook"
    hook.write_text("#!/bin/sh\ncat > /dev/null; echo started\n")
    hook.chmod(0o755)
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "gh run watch 123456"}})

    done = subprocess.run(
        ["sh", "-c", convert_event_to_command(str(hook), "PreToolUse")],
        input=payload,
        capture_output=True,
        text=True,
        env={"BRIDGE_STATE_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )

    assert done.stdout.strip() == "started"
