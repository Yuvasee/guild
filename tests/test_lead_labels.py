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
from bridge.db import connect, transaction
from bridge.dream import convert_work_to_lines
from bridge.guildview import map_db_to_guild
from bridge.leads import LeadError, finish_lead_work, list_lead_work, set_lead_work
from bridge.registry import LEAD_LABEL_FRESH_MS, LEAD_TAB_RENAME_EVERY_MS, AgtermTab, find_lead_tab_title

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


def test_the_label_is_the_newest_fresh_open_chunk_else_the_standing_label(conn: sqlite3.Connection) -> None:
    assert find_lead_tab_title(conn, "alex", NOW) == "Alex · Data"
    set_lead_work(conn, "alex", "Nightly export", None, NOW - 2 * DAY_MS)
    set_lead_work(conn, "alex", "Release train", None, NOW - DAY_MS)
    assert find_lead_tab_title(conn, "alex", NOW) == "Alex · Release train"

    set_lead_work(conn, "alex", "Nightly export", None, NOW)
    assert find_lead_tab_title(conn, "alex", NOW) == "Alex · Nightly export", "setting a chunk again makes it newest"

    finish_lead_work(conn, "alex", "Nightly export", NOW)
    assert find_lead_tab_title(conn, "alex", NOW) == "Alex · Release train"
    finish_lead_work(conn, "alex", "Release train", NOW)
    assert find_lead_tab_title(conn, "alex", NOW) == "Alex · Data"


def test_a_chunk_older_than_three_days_gives_the_standing_label(conn: sqlite3.Connection) -> None:
    set_lead_work(conn, "alex", "Nightly export", None, NOW - LEAD_LABEL_FRESH_MS - 1)
    assert find_lead_tab_title(conn, "alex", NOW) == "Alex · Data"
    conn.execute("UPDATE leads SET label = NULL")
    assert find_lead_tab_title(conn, "alex", NOW) == "Alex"


@pytest.mark.parametrize(
    ("name", "label"),
    [
        ("ABC-12 post-upgrade follow-ups (plan)", "ABC-12 post-upgrade"),
        ("URGENT: tenant mix-up for one user", "URGENT: tenant mix-up"),
        ("Export: release and docs", "Export: release and"),
        ("Unbreakablesupercomputing all day", "Unbreakablesupercomputin"),
    ],
)
def test_a_long_name_from_before_titles_were_short_gives_its_first_whole_words(
    conn: sqlite3.Connection, name: str, label: str
) -> None:
    conn.execute(
        "INSERT INTO lead_work (lead, name, refs, created_at, updated_at) VALUES ('alex', ?, '[]', ?, ?)",
        (name, NOW, NOW),
    )
    assert find_lead_tab_title(conn, "alex", NOW) == f"Alex · {label}"


@pytest.mark.parametrize("title", ["Four words is too many", "Unbreakablesupercomputing", "Ночной экспорт", "  "])
def test_a_title_over_the_limit_is_refused_with_the_way_out(conn: sqlite3.Connection, title: str) -> None:
    with pytest.raises(LeadError) as refused:
        set_lead_work(conn, "alex", title, None, NOW)
    assert conn.execute("SELECT count(*) FROM lead_work").fetchone()[0] == 0
    assert "English" in str(refused.value) or "--about" in str(refused.value)


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


def test_the_tab_follows_the_work_at_most_once_per_ten_minutes_and_the_page_shows_the_same(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    team.write_tree(workspace_a="\U0001f441 GUILD", tab_a="Alex · Data")
    with bridge_env.connect() as conn:
        conn.execute("UPDATE leads SET tab_renamed_at = NULL")
    bridge_env.stub_log.write_text("")

    set_first = team.guild("work", "set", "Nightly export", "--about", "Move the CSV export to a worker", pid=team.a)
    bridge_env.cli("sessions")
    first = _renames(team)
    team.write_tree(workspace_a="\U0001f441 GUILD", tab_a="Alex · Nightly export")
    team.guild("work", "set", "Release train", pid=team.a)
    bridge_env.stub_log.write_text("")
    bridge_env.cli("sessions")
    within = _renames(team)
    with bridge_env.connect() as conn:
        conn.execute("UPDATE leads SET tab_renamed_at = tab_renamed_at - ?", (LEAD_TAB_RENAME_EVERY_MS,))
    bridge_env.cli("sessions")
    after = _renames(team)
    with bridge_env.connect() as conn:
        lead = cast(list[dict[str, Any]], map_db_to_guild(conn, Config(), int(time.time() * 1000), str)["leads"])[0]

    assert set_first.returncode == 0, set_first.stderr
    assert first == ["session rename Alex · Nightly export --target TAB-A"]
    assert within == [], "a second change within 10 minutes waits"
    assert after == ["session rename Alex · Release train --target TAB-A"]
    assert lead["tab_name"] == "Alex · Release train"
    assert {"name": "Nightly export", "about": "Move the CSV export to a worker"}.items() <= lead["work"][0].items()


def test_a_locked_lead_tab_is_never_renamed(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    team.write_tree(workspace_a="\U0001f441 GUILD", tab_a="Alex · Data \U0001f512")
    with bridge_env.connect() as conn, transaction(conn):
        conn.execute("UPDATE leads SET tab_renamed_at = NULL")
        set_lead_work(conn, "alex", "Nightly export", None, int(time.time() * 1000))
    bridge_env.stub_log.write_text("")

    bridge_env.cli("sessions")

    assert _renames(team) == []


def test_a_short_title_is_the_label_as_written(conn: sqlite3.Connection) -> None:
    set_lead_work(conn, "alex", "C++ SDK", None, NOW)
    assert find_lead_tab_title(conn, "alex", NOW) == "Alex · C++ SDK"


def test_a_chunk_named_before_titles_were_short_can_still_be_updated(conn: sqlite3.Connection) -> None:
    old = "A long chunk name from before the limit"
    conn.execute(
        "INSERT INTO lead_work (lead, name, refs, created_at, updated_at) VALUES ('alex', ?, '[]', 0, 0)", (old,)
    )
    set_lead_work(conn, "alex", old, ["issue:ABC-1"], NOW, "now with an about")
    assert [(c.name, c.refs, c.about) for c in list_lead_work(conn, "alex")] == [
        (old, ("issue:ABC-1",), "now with an about")
    ]


def test_a_rename_failed_or_done_takes_the_ten_minute_slot(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn.execute("UPDATE leads SET surface = 'PANE'")
    set_lead_work(conn, "alex", "Nightly export", None, NOW)
    tabs = {"PANE": AgtermTab("Alex · Data", "W", "TAB")}
    calls: list[str] = []

    def rename(agtermctl: str, session_id: str, name: str) -> bool:
        calls.append(name)
        return len(calls) > 1

    monkeypatch.setattr(registry, "rename_agterm_tab", rename)
    failed = registry.name_lead_tabs_after_work(conn, Config(), tabs, NOW)
    registry.name_lead_tabs_after_work(conn, Config(), tabs, NOW + LEAD_TAB_RENAME_EVERY_MS - 1)
    renamed = registry.name_lead_tabs_after_work(conn, Config(), tabs, NOW + LEAD_TAB_RENAME_EVERY_MS)
    registry.name_lead_tabs_after_work(conn, Config(), tabs, NOW + LEAD_TAB_RENAME_EVERY_MS + 1)

    assert calls == ["Alex · Nightly export"] * 2
    assert failed["PANE"].tab_name == "Alex · Data"
    assert renamed["PANE"].tab_name == "Alex · Nightly export"


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
