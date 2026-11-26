import json
import sqlite3
import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest

from bridge import gh_quota
from bridge.config import Config
from bridge.db import connect
from bridge.gh_quota import CoreQuota, is_quota_spending
from bridge.guard import make_host_repo_reader
from bridge.hooks import handle_pre_tool_use
from bridge.install import list_guard_globs

RESET = 1_800_000_000
CONFIG = Config(assistant_handle="aide")


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[sqlite3.Connection]:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    c = connect(tmp_path / "bridge.db")
    c.execute(
        "INSERT INTO surfaces (surface, agterm_session, handle, updated_at) VALUES ('lead', 'TAB-lead', 'boris', 0)"
    )
    c.execute("INSERT INTO surfaces (surface, agterm_session, handle, updated_at) VALUES ('wrk', 'TAB-wrk', 'w1', 0)")
    c.execute(
        "INSERT INTO leads (name, display_name, scope, linear_refs, tint, surface, created_at, domains) "
        "VALUES ('boris', 'Boris', 'scope', '[]', 'blue', 'lead', 0, '[]')"
    )
    c.execute("INSERT INTO tab_parents (agterm_session, parent_session) VALUES ('TAB-wrk', 'TAB-lead')")
    for conversation, surface in (("conv-lead", "lead"), ("conv-wrk", "wrk")):
        c.execute(
            "INSERT INTO instances (instance_id, pid, proc_start_ms, conversation_id, surface, status, first_seen_at, "
            "last_observed_at, last_activity_at) VALUES (?, 1, 0, ?, ?, 'idle', 0, 0, 0)",
            (f"i-{conversation}", conversation, surface),
        )
    yield c
    c.close()


class Reads:
    def __init__(self, remaining: int | None) -> None:
        self.remaining = remaining
        self.calls = 0

    def __call__(self, args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        self.calls += 1
        if self.remaining is None:
            raise subprocess.TimeoutExpired(args, 3)
        body = {"resources": {"core": {"remaining": self.remaining, "limit": 5000, "reset": RESET}}}
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")


def run(conn: sqlite3.Connection, conversation: str, command: str = "gh pr list", reader: object = None) -> str:
    payload: dict[str, object] = {
        "session_id": conversation, "cwd": "/w", "tool_name": "Bash", "tool_input": {"command": command},
    }  # fmt: skip
    return handle_pre_tool_use(conn, payload, CONFIG, reader)  # type: ignore[arg-type]


def test_a_worker_at_1499_is_refused_with_the_reset_time(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(subprocess, "run", Reads(1499))

    out = json.loads(run(conn, "conv-wrk"))["hookSpecificOutput"]

    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"] == (
        "gh guard: core quota 1499/5000 < 1500 reserve, resets 08:00 UTC; retry after reset"
    )


def test_a_worker_at_1500_is_allowed(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", Reads(1500))

    assert run(conn, "conv-wrk") == ""


def test_a_lead_keeps_the_reserve_and_is_allowed_at_zero(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = Reads(0)
    monkeypatch.setattr(subprocess, "run", reads)

    assert run(conn, "conv-lead") == ""
    assert reads.calls == 0


def test_a_host_worker_is_refused_through_the_same_path(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(subprocess, "run", Reads(10))

    out = run(conn, "conv-host", "gh run list", make_host_repo_reader([], ("/home/dev",)))

    assert "core quota 10/5000" in json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]


def test_a_second_command_within_30_seconds_reads_the_cache(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = Reads(1600)
    monkeypatch.setattr(subprocess, "run", reads)

    run(conn, "conv-wrk")
    run(conn, "conv-wrk", "gh api repos/o/r/pulls")

    assert reads.calls == 1


def test_the_cache_expires_after_30_seconds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    levels = iter([CoreQuota(2000, 5000, RESET), CoreQuota(100, 5000, RESET)])

    assert gh_quota.find_quota_refusal(1500, lambda: next(levels), now=1000) is None
    assert gh_quota.find_quota_refusal(1500, lambda: next(levels), now=1029) is None
    assert gh_quota.find_quota_refusal(1500, lambda: next(levels), now=1030) is not None


def test_a_failed_read_allows_the_command_and_logs_once(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reads = Reads(None)
    monkeypatch.setattr(subprocess, "run", reads)

    assert run(conn, "conv-wrk") == ""
    assert run(conn, "conv-wrk") == ""
    assert gh_quota.find_quota_refusal(1500, lambda: None, now=10**10) is None

    assert reads.calls == 1
    assert (tmp_path / "errors.log").read_text().count("gh guard") == 1


def test_a_command_that_spends_no_core_quota_never_reads_it(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = Reads(0)
    monkeypatch.setattr(subprocess, "run", reads)

    for command in ("gh auth status", "gh api rate_limit", "git status", "echo gh pr list"):
        assert run(conn, "conv-wrk", command) == ""
    assert reads.calls == 0


@pytest.mark.parametrize(
    "command",
    ["gh api repos/o/r", "gh run watch 12 --interval 60", "gh pr view 3 -R o/r", "gh issue list", "gh repo view", "gh release list",
     "gh search prs x", "cd /w && gh pr checks 4"],
)  # fmt: skip
def test_every_spending_command_reaches_the_guard_and_is_refused(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    monkeypatch.setattr(subprocess, "run", Reads(0))

    assert "gh guard" in run(conn, "conv-wrk", command)


def test_the_shell_gate_starts_the_guard_for_gh_commands() -> None:
    from bridge.gbridge import may_need_guard

    assert all(may_need_guard(list_guard_globs(), c) for c in ("gh api x", "gh run list", "gh search prs q"))


def test_is_quota_spending_ignores_the_free_read() -> None:
    assert is_quota_spending(["gh", "pr", "list"]) and not is_quota_spending(["gh", "api", "rate_limit"])
