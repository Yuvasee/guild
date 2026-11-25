import json
import os
import signal
import sqlite3
import subprocess
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from conftest import BridgeEnv, SpawnProcess
from test_board import Pair
from test_capacity import add_lead, add_session
from test_reviews import CONFIG, make_pr

from bridge.board import Party
from bridge.claims import HOTSPOT_WINDOW_MS, Holder, load_hotspots, record_claim_event
from bridge.db import connect, transaction
from bridge.gitinfo import RepoFacts
from bridge.guard import HostCheckout, Target, make_host_repo_reader
from bridge.guildview import map_db_to_guild
from bridge.hooks import handle_pre_tool_use
from bridge.reviews import store_open_prs
from bridge.title_guard import find_unclaimed_prs

NOW = 1_800_000_000_000
FACTS = RepoFacts("/w/app", "dev/x", {"origin": "Org/app"})


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[sqlite3.Connection]:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    checkout = tmp_path / "app"
    checkout.mkdir()
    for args in (["init", "-q", "-b", "dev/x"], ["remote", "add", "origin", "https://github.com/Org/app.git"]):
        subprocess.run(["git", *args], cwd=checkout, check=True)
    monkeypatch.setenv("CHECKOUT", str(checkout))
    c = connect(tmp_path / "bridge.db")
    with transaction(c):
        add_lead(c, "anna", [])
        add_lead(c, "bert", [])
        add_worker(c, "w1", "anna")
        add_worker(c, "w2", "bert")
        add_session(c, "solo", "TAB-solo", "idle", 0, pid=990)
        store_open_prs(c, [make_pr()], 500)
    yield c
    c.close()


def add_worker(conn: sqlite3.Connection, handle: str, lead: str) -> None:
    add_session(conn, handle, f"TAB-{handle}", "active", 0, pid=900 + len(handle))
    conn.execute(
        "INSERT INTO tab_parents (agterm_session, parent_session) VALUES (?, ?)", (f"TAB-{handle}", f"TAB-{lead}")
    )


def holder(handle: str) -> Holder:
    return Holder("env:staging", None, handle, f"conv-{handle}", None, 0, 0, True)


def collide(conn: sqlite3.Connection, asker: str, held_by: str, resource: str = "env:staging", at: int = NOW) -> None:
    with transaction(conn):
        record_claim_event(conn, "refused", resource, Party(f"conv-{asker}", asker), holder(held_by), at)


def claim(conn: sqlite3.Connection, handle: str, resource: str = "pr:org/app#7", alias: str | None = None) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO claims (resource, alias, token, holder_conversation, holder_handle, acquired_at, renewed_at, "
            "expires_at) VALUES (?, ?, 't', ?, ?, 0, 0, ?)",
            (resource, alias, f"conv-{handle}", handle, NOW * 2),
        )


def guard(conn: sqlite3.Connection, handle: str, command: str, host: bool = False) -> str:
    cwd = "/w/app" if host else os.environ["CHECKOUT"]
    payload: dict[str, object] = {
        "session_id": f"conv-{handle}", "cwd": cwd, "tool_name": "Bash", "tool_input": {"command": command},
    }  # fmt: skip
    reader = make_host_repo_reader([HostCheckout("/w/app", FACTS, "/w/app")], ("/w",), "/home/dev") if host else None
    output = handle_pre_tool_use(conn, payload, CONFIG, reader)
    if not output:
        return ""
    decision: dict[str, Any] = json.loads(output)["hookSpecificOutput"]
    assert "permissionDecision" not in decision, decision
    return str(decision["additionalContext"])


def test_a_refused_and_a_forced_claim_are_recorded_with_both_sides_and_a_dead_holders_takeover_is_not(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    pair = Pair(bridge_env, spawn_process)
    bridge_env.cli("claim", "env:staging", pid=pair.a)
    assert bridge_env.cli("claim", "env:STAGING", pid=pair.b).returncode == 3
    assert bridge_env.cli("claim", "env:staging", "--force", pid=pair.b).returncode == 0
    os.kill(pair.b, signal.SIGKILL)
    os.waitpid(pair.b, 0)
    bridge_env.cli("sessions")
    assert bridge_env.cli("claim", "env:staging", pid=pair.a).stdout.startswith("taken over")

    with bridge_env.connect() as db:
        rows = [
            tuple(r)
            for r in db.execute(
                "SELECT kind, resource, conversation_id, handle, holder_conversation, holder_handle FROM claim_events "
                "ORDER BY id"
            )
        ]
    assert rows == [
        ("refused", "env:staging", "conv-b", "b", "conv-a", "a"),
        ("forced", "env:staging", "conv-b", "b", "conv-a", "a"),
    ]


def test_hotspots_count_last_weeks_collisions_by_resource_and_by_lead_pair_a_worker_as_its_lead(
    conn: sqlite3.Connection,
) -> None:
    collide(conn, "w1", "bert")
    collide(conn, "anna", "w2")
    collide(conn, "bert", "anna", resource="pr:org/app#7")
    collide(conn, "solo", "w1", resource="pr:org/app#7")
    collide(conn, "w1", "anna", resource="pr:org/app#7")
    collide(conn, "w1", "bert", at=NOW - HOTSPOT_WINDOW_MS - 1)
    with transaction(conn):
        record_claim_event(conn, "nudged", "env:staging", Party("conv-w1", "w1"), None, NOW)

    hotspots = load_hotspots(conn, NOW, lambda _conversation: True)

    assert hotspots.resources == [("pr:org/app#7", 3), ("env:staging", 2)]
    assert hotspots.pairs == [("anna", "bert", 3), ("anna", "anna", 1), ("anna", "solo", 1)]


def test_the_dashboard_shows_hotspots_with_lead_names_and_nothing_when_there_were_no_collisions(
    conn: sqlite3.Connection,
) -> None:
    def hotspots_shown() -> Any:
        return map_db_to_guild(conn, CONFIG, NOW, lambda ref: f"https://x/{ref}")["hotspots"]

    assert hotspots_shown() == {"resources": [], "pairs": []}
    collide(conn, "w1", "bert", resource="pr:org/app#7")

    hotspots = hotspots_shown()

    assert hotspots["pairs"] == [{"a": "Anna", "b": "Bert", "count": 1}]
    assert [(r["resource"], r["label"], r["count"]) for r in hotspots["resources"]] == [("pr:org/app#7", "app#7", 1)]


def test_a_change_to_a_queued_pr_with_no_claim_is_nudged_once_per_pr_and_session_and_recorded(
    conn: sqlite3.Connection,
) -> None:
    first = guard(conn, "anna", "gh pr comment 7 -b hi")

    assert first == "bridge: no claim held on pr:Org/app#7: bridge claim it first (`bridge claim pr:Org/app#7`); " \
        "this note comes once per PR."  # fmt: skip
    assert guard(conn, "anna", "git push origin dev/x") == ""
    assert "pr:Org/app#7" in guard(conn, "bert", "gh pr edit 7 --title New")
    rows = [tuple(r) for r in conn.execute("SELECT kind, resource, handle, lead FROM claim_events ORDER BY id")]
    assert rows == [("nudged", "pr:Org/app#7", "anna", "anna"), ("nudged", "pr:Org/app#7", "bert", "bert")]


@pytest.mark.parametrize(
    ("command", "ref"),
    [
        ("git push", "pr:Org/app#7"),
        ("git push origin dev/x", "pr:Org/app#7"),
        ("gh pr edit --add-label x", "pr:Org/app#7"),
        ("gh pr comment 7 -b hi", "pr:Org/app#7"),
    ],
)
def test_each_pr_change_is_nudged(conn: sqlite3.Connection, command: str, ref: str) -> None:
    assert f"no claim held on {ref}:" in guard(conn, "anna", command)


def test_a_merge_the_owners_rule_lets_pass_is_nudged(conn: sqlite3.Connection) -> None:
    """The hook would read GitHub for the owner's merge rule first; the nudge reads the same merge target."""
    for key in ("pr:org/app#7", "branch:org/app:dev/x"):
        merge = [Target(key, "gh pr merge")]
        assert find_unclaimed_prs(conn, "", None, merge, "conv-anna", lambda _d: None) == ["pr:Org/app#7"]


@pytest.mark.parametrize(
    "command", ["git push origin other", "gh pr comment 8 -b hi", "gh pr view 7", "gh pr comment 7 --help"]
)
def test_a_command_that_changes_no_queued_pr_is_not_nudged(conn: sqlite3.Connection, command: str) -> None:
    assert guard(conn, "anna", command) == ""


def test_a_claim_held_by_the_session_or_its_team_by_pr_or_by_head_branch_needs_no_nudge(
    conn: sqlite3.Connection,
) -> None:
    claim(conn, "anna")
    assert guard(conn, "anna", "git push origin dev/x") == ""
    assert guard(conn, "w1", "git push origin dev/x") == ""
    # Another team's claim whose holder is not running is no conflict, and no claim of this team's.
    assert "no claim held" in guard(conn, "w2", "git push origin dev/x")
    with transaction(conn):
        conn.execute("DELETE FROM claims")
    claim(conn, "w1", resource="branch:org/app:dev/x")
    assert guard(conn, "anna", "gh pr comment 7 -b hi") == ""


def test_a_host_worker_is_told_to_ask_its_lead(conn: sqlite3.Connection) -> None:
    note = guard(conn, "w1", "git push origin dev/x", host=True)
    assert note.startswith("bridge: no claim held on pr:Org/app#7: ask your lead to claim it first;")


def test_hotspots_leave_out_a_collision_with_a_locked_session(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        conn.execute("UPDATE surfaces SET tab_name = '🔒 private' WHERE handle = 'solo'")
        conn.execute(
            "INSERT INTO conversations (conversation_id, instance_id, first_seen_at, last_seen_at) "
            "VALUES ('conv-solo', 'i-solo', 0, 0)"
        )
    collide(conn, "solo", "anna", resource="path:/private/plan")
    collide(conn, "w1", "bert")
    hotspots: Any = map_db_to_guild(conn, CONFIG, NOW, lambda ref: "")["hotspots"]

    assert [r["resource"] for r in hotspots["resources"]] == ["env:staging"]
    assert hotspots["pairs"] == [{"a": "Anna", "b": "Bert", "count": 1}]


@pytest.mark.parametrize("command", ["gh -R Org/app pr comment 7 -b hi", "gh --repo Org/app pr edit 7 --title New"])
def test_a_repo_option_before_the_subcommand_is_still_nudged(conn: sqlite3.Connection, command: str) -> None:
    assert "no claim held on pr:Org/app#7:" in guard(conn, "anna", command)


@pytest.mark.parametrize("command", ["git push --all origin", "git push --mirror origin"])
def test_a_push_of_every_branch_is_nudged_for_its_repos_queued_prs(conn: sqlite3.Connection, command: str) -> None:
    assert "no claim held on pr:Org/app#7:" in guard(conn, "anna", command)


def test_a_claim_on_a_branch_of_another_case_is_not_the_prs(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        conn.execute("UPDATE review_prs SET head = 'Dev/X'")
    claim(conn, "anna", resource="branch:org/app:dev/x")
    assert "no claim held on pr:Org/app#7:" in guard(conn, "anna", "gh pr comment 7 -b hi")
    with transaction(conn):
        conn.execute("DELETE FROM claims")
        conn.execute("DELETE FROM claim_events")
    claim(conn, "anna", resource="branch:org/app:Dev/X")
    assert guard(conn, "anna", "gh pr comment 7 -b hi") == ""


def test_an_ended_teammates_claim_does_not_count(conn: sqlite3.Connection) -> None:
    claim(conn, "w1")
    with transaction(conn):
        conn.execute("UPDATE instances SET ended_at = 1, status = 'ended' WHERE conversation_id = 'conv-w1'")
    assert "no claim held on pr:Org/app#7:" in guard(conn, "anna", "gh pr comment 7 -b hi")


def test_two_hooks_that_both_read_no_note_yet_give_it_once(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Both read before either wrote: the record decides which one tells.
    def unclaimed(*_: object) -> list[str]:
        return ["pr:Org/app#7"]

    monkeypatch.setattr("bridge.title_guard.find_unclaimed_prs", unclaimed)
    first = guard(conn, "anna", "gh pr comment 7 -b hi")
    second = guard(conn, "anna", "gh pr comment 7 -b hi")

    assert "no claim held on pr:Org/app#7:" in first and second == ""
    assert conn.execute("SELECT count(*) FROM claim_events WHERE kind = 'nudged'").fetchone()[0] == 1


@pytest.mark.parametrize(
    "command",
    [
        "git push origin dev/x --tags",
        "git push --prune origin dev/x",
        "git push --prune",
        "git push --all --prune origin",
    ],
)
def test_a_branch_push_with_tags_or_any_push_with_prune_is_nudged(conn: sqlite3.Connection, command: str) -> None:
    assert "no claim held on pr:Org/app#7:" in guard(conn, "anna", command)
    assert conn.execute("SELECT count(*) FROM claim_events WHERE kind = 'nudged'").fetchone()[0] == 1


@pytest.mark.parametrize("command", ["git push origin --tags", "git push --tags"])
def test_a_tags_only_push_is_not_nudged(conn: sqlite3.Connection, command: str) -> None:
    assert guard(conn, "anna", command) == ""
