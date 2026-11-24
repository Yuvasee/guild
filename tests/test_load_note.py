import json
import os
import subprocess
from pathlib import Path

import pytest

from bridge.config import Config, Remote
from bridge.db import connect
from bridge.hooks import find_heavy_job, handle_pre_tool_use
from bridge.install import convert_event_to_command, list_guard_globs

HEAVY = [
    ("uv run pytest -q", "pytest"),
    ("pytest tests/", "pytest"),
    ("FOO=1 timeout 600 uv run pytest -x > out.txt 2>&1", "pytest"),
    ("cd app && python -m pytest -n 8", "pytest"),
    ("uv run pyright", "pyright"),
    ("mypy src", "mypy"),
    ("npx tsc --noEmit -p .", "tsc"),
    ("npm test", "npm test"),
    ("pnpm run build", "pnpm build"),
    ("yarn test:unit", "yarn test:unit"),
    ("uv sync --all-extras", "uv sync"),
    ("docker build -t app .", "docker build"),
    ("docker buildx build .", "docker build"),
    ("make", "make"),
    ("make -j 8 test", "make test"),
    ("cargo test", "cargo test"),
    ("go test ./...", "go test"),
    ("nohup pytest > ~/s.log 2>&1 &", "pytest"),
    ("pytest --ignore=tests/test_slow.py", "pytest"),
    ("pytest --ignore tests/test_slow.py -p no:cacheprovider", "pytest"),
    ("npm test -- --coverage", "npm test"),
    ("cargo test -p core", "cargo test"),
]
LIGHT = [
    "uv run pytest tests/test_hooks.py",
    "pytest tests/test_hooks.py::test_one -q",
    "pytest -k load",
    "pytest -kload tests",
    "uv run pyright src/bridge/hooks.py",
    "tsc src/a.ts",
    "npm run lint",
    "npm install",
    "uv run ruff check",
    "make lint",
    "go test ./pkg -run TestLoad",
    "ssh worker-1 'cd ~/x && uv run pytest'",
    "ssh worker-1 pytest",
    "echo 'pytest'",
    "grep -rn pytest docs",
    "pytest --version",
    "docker run app",
    "npm test -- src/a.test.ts",
    "cargo test test_load",
    "go test foo_test.go",
]
HOST = Config(worker_host="w1", remotes=(Remote(name="w1", ssh="worker-1.example"),))


@pytest.mark.parametrize(("command", "job"), HEAVY)
def test_a_whole_project_job_is_heavy(command: str, job: str) -> None:
    assert find_heavy_job(command) == job


@pytest.mark.parametrize("command", LIGHT)
def test_a_targeted_job_or_one_run_through_ssh_is_not(command: str) -> None:
    assert find_heavy_job(command) is None


def _run(tmp_path: Path, command: str, config: Config = HOST) -> dict[str, str] | None:
    payload: dict[str, object] = {
        "session_id": "conv-1",
        "cwd": "/w",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }
    out = handle_pre_tool_use(connect(tmp_path / "b.db"), payload, config)
    return json.loads(out)["hookSpecificOutput"] if out else None


def test_over_the_threshold_the_note_names_the_host_and_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    monkeypatch.setattr(os, "getloadavg", lambda: (140.0, 90.0, 60.0))

    out = _run(tmp_path, "uv run pytest -q")

    assert out is not None and "permissionDecision" not in out
    note = out["additionalContext"]
    assert "load is 140 on 8 cores" in note and "pytest" in note
    assert "archive ${t:-HEAD}" in note and "ssh worker-1.example" in note and "nohup bash -lc" in note
    assert "d=job-conv-1-$(date +%s)" in note
    assert "load note: conv-1 pytest at load 140" in (tmp_path / "slow.log").read_text()


def test_the_note_joins_the_pipe_note(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    monkeypatch.setattr(os, "getloadavg", lambda: (40.0, 0.0, 0.0))

    out = _run(tmp_path, "uv run pytest | tail")

    assert out is not None
    assert "PIPESTATUS" in out["additionalContext"] and "load is 40" in out["additionalContext"]


def test_without_a_worker_host_the_note_says_to_narrow_or_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    monkeypatch.setattr(os, "getloadavg", lambda: (40.0, 0.0, 0.0))

    out = _run(tmp_path, "npm test", Config())

    assert out is not None and "no worker_host is set" in out["additionalContext"]


@pytest.mark.parametrize(("load", "config"), [(24.0, HOST), (200.0, Config(heavy_job_load_per_core=0))])
def test_at_or_under_the_threshold_or_turned_off_there_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, load: float, config: Config
) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    monkeypatch.setattr(os, "getloadavg", lambda: (load, 0.0, 0.0))

    assert _run(tmp_path, "uv run pytest -q", config) is None
    assert not (tmp_path / "slow.log").exists()


@pytest.mark.parametrize("command", ["uv run pytest tests/test_hooks.py", "ssh worker-1 'uv run pytest'"])
def test_a_targeted_or_ssh_run_gets_none_under_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(os, "getloadavg", lambda: (500.0, 0.0, 0.0))

    assert _run(tmp_path, command) is None


@pytest.mark.parametrize("command", [c for c, _ in HEAVY])
def test_the_macs_shell_gate_starts_the_hook_for_it(tmp_path: Path, command: str) -> None:
    hook = tmp_path / "hook"
    hook.write_text("#!/bin/sh\ncat > /dev/null; echo started\n")
    hook.chmod(0o755)

    done = subprocess.run(
        ["sh", "-c", convert_event_to_command(str(hook), "PreToolUse")],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
        capture_output=True,
        text=True,
        env={"BRIDGE_STATE_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )

    assert done.stdout.strip() == "started"


def test_a_hosts_gate_does_not_send_its_heavy_jobs_to_the_mac() -> None:
    assert not {"*uv*sync*", "*docker*build*"} & set(list_guard_globs())
