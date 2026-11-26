import fcntl
import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from bridge.codex_reads import find_codex_read_refusal, list_codex_reads
from bridge.config import Config
from bridge.db import connect
from bridge.gbridge import may_need_guard
from bridge.guard import make_host_repo_reader
from bridge.hooks import handle_pre_tool_use
from bridge.install import convert_event_to_command, list_guard_globs


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("read", "other"):
        subprocess.run(["git", "init", "-q", str(tmp_path / name)], check=True)
    (tmp_path / "read" / "src").mkdir()
    return tmp_path


@pytest.fixture
def codex(home: Path) -> Iterator[int]:
    """A live run reading home/read, its slot file locked as codex-slot.py locks it."""
    slots = home / ".local/state/guild/codex-slot"
    slots.mkdir(parents=True)
    (slots / "1.lock").write_text(f"pid 4242 since 10:52:28 in {home / 'other'}")  # its run has ended: no lock
    fd = os.open(slots / "2.lock", os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.write(fd, f"pid 5151 since 11:02:00 in {home / 'read' / 'src'}".encode())
    yield fd
    os.close(fd)


@pytest.mark.parametrize(
    ("command", "where"),
    [
        ("git rebase origin/main", "read"),
        ("git reset --hard HEAD~1", "read/src"),
        ("git checkout -- notes.md", "read"),
        ("git switch -c spike", "read"),
        ("git pull --ff-only", "read"),
        ("cd read && git rebase origin/main", "."),
        ("cd ~/read/src; git fetch && git rebase origin/main", "."),
        ("git -C read checkout main", "."),
        ("git -C ../read -c user.name=x pull", "other"),
        ("git fetch && if true; then git reset --hard origin/main; fi", "read"),
    ],
)
def test_moving_the_files_codex_reads_is_refused_naming_it(home: Path, codex: int, command: str, where: str) -> None:
    refusal = find_codex_read_refusal(command, str(home / where))

    assert refusal is not None
    assert refusal.startswith(f"bridge: Codex (pid 5151, since 11:02:00) is reading {home / 'read'}; git ")


@pytest.mark.parametrize(
    ("command", "where"),
    [
        ("git status && git log --oneline -3", "read"),
        ("git diff origin/main", "read"),
        ("git rebase origin/main", "other"),
        ("cd ../other && git reset --hard", "read"),
        ("git -C ../other checkout main", "read"),
        ("git rebase --help", "read"),
        ('cd "$WT" && git rebase origin/main', "."),
        ("git commit -m 'rebase later'", "read"),
    ],
)
def test_reads_other_worktrees_and_unread_directories_pass(home: Path, codex: int, command: str, where: str) -> None:
    assert find_codex_read_refusal(command, str(home / where)) is None


def test_a_run_that_has_ended_holds_nothing(home: Path, codex: int) -> None:
    assert [r.pid for r in list_codex_reads()] == ["5151"]

    fcntl.flock(codex, fcntl.LOCK_UN)

    assert list_codex_reads() == []
    assert find_codex_read_refusal("git rebase origin/main", str(home / "read")) is None


def test_with_no_slot_records_nothing_is_refused(home: Path) -> None:
    assert find_codex_read_refusal("git reset --hard", str(home / "read")) is None


def test_the_hook_denies_on_the_mac_but_not_for_a_host_worker(home: Path, codex: int) -> None:
    conn = connect(home / "b.db")
    payload: dict[str, object] = {
        "session_id": "c", "cwd": str(home / "read"), "tool_name": "Bash",
        "tool_input": {"command": "git rebase origin/main"},
    }  # fmt: skip

    decision = json.loads(handle_pre_tool_use(conn, payload, Config()))["hookSpecificOutput"]
    on_host = handle_pre_tool_use(conn, payload, Config(), make_host_repo_reader([], (str(home),), str(home)))

    assert decision["permissionDecision"] == "deny"
    assert "Codex (pid 5151" in decision["permissionDecisionReason"]
    assert "Codex" not in on_host


@pytest.mark.parametrize(
    "command", ["git rebase origin/main", "git reset --hard", "git checkout main", "git switch x", "git pull"]
)
def test_only_the_macs_gate_starts_the_hook_for_it(tmp_path: Path, command: str) -> None:
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
    assert not may_need_guard(list_guard_globs(), command), "a host's worktrees are not read by the Mac's Codex"
