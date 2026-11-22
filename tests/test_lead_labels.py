import json
import sqlite3
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any, cast

import pytest
from conftest import BridgeEnv, SpawnProcess
from test_leads import Team

from bridge import registry
from bridge.config import Config
from bridge.db import connect
from bridge.dream import convert_work_to_lines
from bridge.guildview import map_db_to_guild
from bridge.leads import list_lead_work, set_lead_work
from bridge.registry import LEAD_TAB_RENAME_EVERY_MS, AgtermTab, find_lead_tab_title

DAY_MS = 24 * 60 * 60 * 1000
NOW = 100 * DAY_MS


@pytest.fixture
def conn(tmp_path: Path) -> Generator[sqlite3.Connection]:
    db = connect(tmp_path / "bridge.db")
    db.execute(
        "INSERT INTO leads (name, display_name, scope, linear_refs, tint, created_at, label, domains) "
        "VALUES ('alex', 'Alex', 'scope', '[]', 'blue', 0, 'Data', '[]')"
    )
    yield db
    db.close()


def test_the_label_is_the_standing_label_whatever_the_chunks(conn: sqlite3.Connection) -> None:
    set_lead_work(conn, "alex", "Nightly export", None, NOW - DAY_MS)
    set_lead_work(conn, "alex", "Release train", None, NOW)
    assert find_lead_tab_title(conn, "alex") == "Alex · Data"
    conn.execute("UPDATE leads SET label = NULL")
    assert find_lead_tab_title(conn, "alex") == "Alex"


def test_a_long_chunk_name_is_accepted(conn: sqlite3.Connection) -> None:
    name = "Move the nightly CSV export off the web box into its own worker"
    set_lead_work(conn, "alex", name, None, NOW)
    assert [c.name for c in list_lead_work(conn, "alex")] == [name]


def test_about_is_stored_kept_when_not_given_and_replaced_when_given(conn: sqlite3.Connection) -> None:
    set_lead_work(conn, "alex", "Nightly export", None, NOW, "Move the  CSV export to a worker")
    set_lead_work(conn, "alex", "Nightly export", ["issue:ABC-1"], NOW)
    assert [(c.name, c.about) for c in list_lead_work(conn, "alex")] == [
        ("Nightly export", "Move the CSV export to a worker")
    ]
    set_lead_work(conn, "alex", "Nightly export", None, NOW, "Now with retries")
    assert list_lead_work(conn, "alex")[0].about == "Now with retries"


def _renames(team: Team) -> list[str]:
    return [line for line in team.log().splitlines() if "session rename" in line and "TAB-A" in line]


def _page_tab_name(bridge_env: BridgeEnv) -> str:
    with bridge_env.connect() as conn:
        leads = cast(list[dict[str, Any]], map_db_to_guild(conn, Config(), int(time.time() * 1000), str)["leads"])
    return str(leads[0]["tab_name"])


def test_lead_label_renames_the_tab_and_the_page_at_once_and_work_does_not(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    team.write_tree(workspace_a="\U0001f441 GUILD", tab_a="Alex · Data")
    long_name = "Move the nightly CSV export off the web box into its own worker"
    bridge_env.stub_log.write_text("")

    work = team.guild("work", "set", long_name, "--about", "one line", pid=team.a)
    bridge_env.cli("sessions")
    after_work = _renames(team)
    label = team.guild("lead", "label", "alex", "Billing")

    assert work.returncode == 0, work.stderr
    assert after_work == []
    assert label.returncode == 0, label.stderr
    assert _renames(team) == ["session rename Alex · Billing --target TAB-A"]
    assert _page_tab_name(bridge_env) == "Alex · Billing"
    listed = team.guild("work", "list", pid=team.a).stdout
    assert f"  {long_name} — one line" in listed and "title" not in listed, listed


def test_a_lead_tab_renamed_by_hand_is_put_back_at_most_once_per_ten_minutes(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    team.write_tree(workspace_a="\U0001f441 GUILD", tab_a="My own name")
    with bridge_env.connect() as conn:
        conn.execute("UPDATE leads SET tab_renamed_at = NULL")
    bridge_env.stub_log.write_text("")

    bridge_env.cli("sessions")
    first = _renames(team)
    bridge_env.stub_log.write_text("")
    bridge_env.cli("sessions")
    within = _renames(team)
    with bridge_env.connect() as conn:
        conn.execute("UPDATE leads SET tab_renamed_at = tab_renamed_at - ?", (LEAD_TAB_RENAME_EVERY_MS,))
    bridge_env.cli("sessions")

    assert first == ["session rename Alex · Data --target TAB-A"]
    assert within == [], "agterm still shows the hand-given name; asked again only after 10 minutes"
    assert _renames(team) == ["session rename Alex · Data --target TAB-A"]
    assert _page_tab_name(bridge_env) == "Alex · Data"


def test_a_locked_lead_tab_is_never_renamed(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    team.write_tree(workspace_a="\U0001f441 GUILD", tab_a="My own name \U0001f512")
    with bridge_env.connect() as conn:
        conn.execute("UPDATE leads SET tab_renamed_at = NULL")
    bridge_env.stub_log.write_text("")

    bridge_env.cli("sessions")

    assert _renames(team) == []


def test_a_rename_failed_or_done_takes_the_ten_minute_slot(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn.execute("UPDATE leads SET surface = 'PANE'")
    tabs = {"PANE": AgtermTab("My own name", "W", "TAB")}
    calls: list[str] = []

    def rename(agtermctl: str, session_id: str, name: str) -> bool:
        calls.append(name)
        return len(calls) > 1

    monkeypatch.setattr(registry, "rename_agterm_tab", rename)
    failed = registry.name_lead_tabs_after_labels(conn, Config(), tabs, NOW)
    registry.name_lead_tabs_after_labels(conn, Config(), tabs, NOW + LEAD_TAB_RENAME_EVERY_MS - 1)
    renamed = registry.name_lead_tabs_after_labels(conn, Config(), tabs, NOW + LEAD_TAB_RENAME_EVERY_MS)
    registry.name_lead_tabs_after_labels(conn, Config(), tabs, NOW + LEAD_TAB_RENAME_EVERY_MS + 1)

    assert calls == ["Alex · Data"] * 2
    assert failed["PANE"].tab_name == "My own name"
    assert renamed["PANE"].tab_name == "Alex · Data"


def test_another_session_in_a_pane_of_the_lead_tab_does_not_rename_it(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    tab = {"id": "TAB-A", "name": "Alex · Data", "surfaces": [{"paneID": "PANE-A"}, {"paneID": "PANE-B"}]}
    tree = {
        "ok": True,
        "result": {"tree": {"workspaces": [{"id": "WS", "name": "\U0001f441 GUILD", "sessions": [tab]}]}},
    }
    bridge_env.stub_tree.write_text(json.dumps(tree))
    projects = Path(bridge_env.env["BRIDGE_CLAUDE_PROJECTS_DIR"]) / "p"
    projects.mkdir(parents=True)
    (projects / "conv-b3.jsonl").write_text(
        json.dumps({"type": "custom-title", "customTitle": "Something else"}) + "\n"
    )
    bridge_env.write_session_file(team.b, "conv-b3")
    team.start(team.b, "conv-b3", "clear")
    bridge_env.stub_log.write_text("")

    bridge_env.cli("sessions")

    assert "Something else" not in team.log()


def test_the_night_review_reads_the_about_beside_the_title(conn: sqlite3.Connection) -> None:
    set_lead_work(conn, "alex", "Nightly export", None, NOW, "Move the CSV export to a worker")
    assert convert_work_to_lines(conn, "alex", 0) == "- Nightly export — Move the CSV export to a worker (open)"
