import json
import subprocess
from pathlib import Path

import pytest

from bridge.config import Config
from bridge.db import connect
from bridge.hooks import find_hidden_exit_code_note, handle_pre_tool_use
from bridge.install import convert_event_to_command

NOTED = [
    ("pre-commit run --all-files | tail -20", "pre-commit"),
    ("uv run pytest -q tests/x.py 2>&1 | tail -3", "pytest"),
    ("cd app && python -m pytest | grep FAILED", "pytest"),
    ("ruff check src | head", "ruff"),
    ("git commit -m 'x' 2>&1 | tail -2", "git commit"),
    ("npm test | tail", "npm test"),
    ("pnpm run lint | grep error", "pnpm lint"),
    ("go test ./... | tee out.txt", "go test"),
    ("uvx pyright | tail -1", "pyright"),
    ("set +o pipefail; pytest | tail", "pytest"),
    ("pytest tests/test_pipefail.py | tail", "pytest"),
    ("uv run --project . pytest | tail", "pytest"),
    ("uv run --python 3.12 pytest | tail", "pytest"),
    ("git -C app commit -m test | tail", "git commit"),
    ("(uv run pytest -q) 2>&1 | tail -20", "pytest"),
]
QUIET = [
    "set -o pipefail; uv run pytest -q | tail -3",
    "uv run pytest -q | tail -3; echo ${PIPESTATUS[0]}",
    "uv run pytest -q",
    "git log --oneline | head",
    "cat pytest.ini | grep addopts",
    "grep -rn pytest docs | head",
    "echo 'pytest | tail'",
    "pytest | tail; rc=${pipestatus[1]}; exit $rc",
    "setopt pipefail; ruff check | head",
]


@pytest.mark.parametrize(("command", "checker"), NOTED)
def test_a_checker_piped_into_a_filter_gets_a_note(command: str, checker: str) -> None:
    note = find_hidden_exit_code_note(command)
    assert note is not None and note.startswith(f"bridge: {checker}'s exit code is lost")


@pytest.mark.parametrize("command", QUIET)
def test_kept_exit_codes_and_other_pipes_get_none(command: str) -> None:
    assert find_hidden_exit_code_note(command) is None


def test_the_hook_adds_the_note_and_never_refuses(tmp_path: Path) -> None:
    payload: dict[str, object] = {
        "session_id": "c",
        "cwd": "/w",
        "tool_name": "Bash",
        "tool_input": {"command": "pre-commit run | tail"},
    }

    out = json.loads(handle_pre_tool_use(connect(tmp_path / "b.db"), payload, Config()))["hookSpecificOutput"]

    assert "permissionDecision" not in out
    assert "PIPESTATUS" in out["additionalContext"]


@pytest.mark.parametrize("command", [c for c, _ in NOTED])
def test_the_macs_shell_gate_starts_the_hook_for_it(tmp_path: Path, command: str) -> None:
    hook = tmp_path / "hook"
    hook.write_text("#!/bin/sh\ncat > /dev/null; echo started\n")
    hook.chmod(0o755)
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})

    done = subprocess.run(
        ["sh", "-c", convert_event_to_command(str(hook), "PreToolUse")],
        input=payload,
        capture_output=True,
        text=True,
        env={"BRIDGE_STATE_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )

    assert done.stdout.strip() == "started"
