import json
from pathlib import Path

from conftest import BridgeEnv, SpawnProcess
from test_leads import Team

from bridge.compaction import (
    DEAD_FAILING,
    DEAD_LIMIT_SCREEN,
    DEAD_TOO_LONG,
    FULL_CONTEXT_SQL,
    PHASE_COMPACTING,
    PHASE_REQUESTED,
    is_request_delivered,
    read_context_full,
    record_death,
)
from bridge.config import Config
from bridge.context_watch import (
    ContextTab,
    list_context_tabs,
    map_screen_to_context_pct,
    map_screen_to_limit_reached,
    record_context_screens,
)
from bridge.db import transaction
from bridge.guildview import Evidence, map_evidence_to_state

RULE = "\u2500" * 40


def _screen(footer: str, above: str = "\u23fa Done.", extra_footer: str = "") -> str:
    lines = [above, "", RULE, "\u276f\u00a0", RULE, footer, "  -- INSERT -- \u23f5\u23f5 bypass permissions on"]
    return "\n".join([*lines, *([extra_footer] if extra_footer else [])]) + "\n"


LIMIT_SCREEN = _screen(
    "  Opus 5.5 | 97% ctx | ~/.local/state/gbridge/worktrees/g-proj-12-build-345",
    "\u276f [bridge] Peer message #71 from xavi\n"
    "  \u23bf  Context limit reached \u00b7 /compact or /clear to continue \u00b7 auto-compact is off",
    "  Context low (0% remaining) \u00b7 Run /compact to compact & continue",
)


def test_the_percent_comes_from_the_footer_only() -> None:
    assert map_screen_to_context_pct(_screen("  Opus 5.5 | 13% ctx | \\~/projects/bridge")) == 13
    # As a host worker showed it before its first request after a start.
    assert map_screen_to_context_pct(_screen("  Opus 5.5 | % ctx | ~/.local/state/gbridge/worktrees/g-x")) is None
    quoted = _screen("  Opus 5.5 | % ctx | ~/x", above="  Opus 5.5 | 97% ctx | ~/quoted from a report")
    assert map_screen_to_context_pct(quoted) is None


def test_context_limit_counts_only_with_the_footer_saying_so() -> None:
    assert map_screen_to_limit_reached(LIMIT_SCREEN)
    quoted = _screen(
        "  Fable 5.2 | 17% ctx | ~/projects/bridge",
        "  its screen showed Context limit reached \u00b7 /compact or /clear to continue",
    )
    assert not map_screen_to_limit_reached(quoted), "a healthy tab quoting the notice is not dead"
    # Both notices quoted right above the box of a tab whose footer is a single line.
    lines = [
        "  \u23bf  Context limit reached \u00b7 /compact or /clear to continue \u00b7 auto-compact is off",
        "  Context low (0% remaining) \u00b7 Run /compact to compact & continue", RULE, "\u276f\u00a0", RULE,
        "  Fable 5.2 | 17% ctx | ~/projects/bridge",
    ]  # fmt: skip
    assert not map_screen_to_limit_reached("\n".join(lines) + "\n")
    busy = LIMIT_SCREEN.replace("\u276f\u00a0", "\u276f half typed")
    assert not map_screen_to_limit_reached(busy)


def _worker_team(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> tuple[Team, list[ContextTab]]:
    """TAB-B (conv-b) is a worker of lead alex in TAB-A."""
    team = Team(bridge_env, spawn_process)
    team.enroll()
    raw = json.loads(bridge_env.stub_tree.read_text())
    tree = raw["result"]["tree"]
    for workspace in tree["workspaces"]:
        for tab in workspace["sessions"]:
            if tab["id"] == "TAB-B":
                tab["parent"] = "TAB-A"
    with bridge_env.connect() as conn:
        tabs = list_context_tabs(conn, Config(), tree)
    return team, tabs


def test_a_dead_worker_is_compacted_and_its_lead_and_the_assistant_told_once(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team, tabs = _worker_team(bridge_env, spawn_process)
    config = Config(compact_at_tokens=600_000, assistant_handle="nobody")
    worker = next(t for t in tabs if t.session == "TAB-B")
    with bridge_env.connect() as conn, transaction(conn):
        record_death(conn, "conv-b", DEAD_TOO_LONG, 1_000_000, 990_000)
    screen = _screen("  Fable 5.2 | 0% ctx | ~/projects/bridge")
    with bridge_env.connect() as conn, transaction(conn):
        first = record_context_screens(conn, config, tabs, {"conv-b": (screen, 1_600_000)}, 1_600_000)
    with bridge_env.connect() as conn, transaction(conn):
        second = record_context_screens(conn, config, tabs, {"conv-b": (screen, 1_700_000)}, 1_700_000)
    with bridge_env.connect() as conn:
        phase = conn.execute("SELECT compact_phase FROM conversations WHERE conversation_id = 'conv-b'").fetchone()[0]
        body = conn.execute("SELECT body FROM messages ORDER BY id DESC LIMIT 1").fetchone()[0]
        full = read_context_full(conn, "conv-b")
    drive = bridge_env.cli(
        "_compact-drive", "compact", worker.instance_id, "conv-b", "--keep", "none", "--generic-keep"
    )

    assert [(d[1], d[2]) for d in first.drives] == [("conv-b", True)]
    assert [r.handle for _, r in first.messages] == ["alex"]
    assert second.drives == [] and second.messages == [], "one alert and one drive per death"
    assert phase == PHASE_COMPACTING
    assert 'Tab "Old B" (TAB-B) cannot take a turn for 10m' in body
    assert "Prompt is too long" in body
    assert full is not None and "cannot take a turn" in full
    assert drive.returncode == 0, drive.stderr
    # Its tab comes from the database: serve's environment names none.
    assert "--target TAB-B --pane-id PANE-B /compact Keep the user's decisions" in team.log()
    dead = Evidence("completed", True, None, None, None, (), (), (), context_dead=full)
    assert map_evidence_to_state(dead) == {"state": "stuck", "why": full}


def test_a_tab_failing_on_api_errors_is_told_of_but_not_compacted(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    _, tabs = _worker_team(bridge_env, spawn_process)
    with bridge_env.connect() as conn, transaction(conn):
        record_death(conn, "conv-b", DEAD_FAILING, 1_000_000, 990_000)
        actions = record_context_screens(
            conn, Config(compact_at_tokens=600_000), tabs, {"conv-b": (_screen("  F | 40% ctx"), 1_100_000)}, 1_100_000
        )
        typable = conn.execute(
            "SELECT 1 FROM conversations c WHERE c.conversation_id = 'conv-b' AND " + FULL_CONTEXT_SQL
        ).fetchone()
        body = conn.execute("SELECT body FROM messages ORDER BY id DESC LIMIT 1").fetchone()[0]

    assert actions.drives == []
    assert "compaction does not cure an API error" in body
    assert typable is None, "mail is not held: the next message is what lets it answer again"


def test_a_host_tab_past_the_share_is_asked_by_message_then_compacted_once_it_is_in(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    _, tabs = _worker_team(bridge_env, spawn_process)
    config = Config(compact_at_tokens=600_000)
    screen = _screen("  Opus 5.5 | 80% ctx | ~/.local/state/gbridge/worktrees/g-x")
    with bridge_env.connect() as conn, transaction(conn):
        asked = record_context_screens(conn, config, tabs, {"conv-b": (screen, 1_000_000)}, 1_000_000)
    with bridge_env.connect() as conn:
        row = conn.execute(
            "SELECT compact_phase, compact_request_message, context_pct FROM conversations"
            " WHERE conversation_id = 'conv-b'"
        ).fetchone()
        body = conn.execute("SELECT body FROM messages WHERE id = ?", (row[1],)).fetchone()[0]
        waiting = is_request_delivered(conn, "conv-b")
        conn.execute("UPDATE message_recipients SET offered_at = 1 WHERE message_id = ?", (row[1],))
        delivered = is_request_delivered(conn, "conv-b")
    with bridge_env.connect() as conn, transaction(conn):
        again = record_context_screens(conn, config, tabs, {"conv-b": (screen, 1_090_000)}, 1_090_000)

    assert [r.conversation_id for _, r in asked.messages] == ["conv-b"]
    assert asked.drives == []
    assert (row[0], row[2]) == (PHASE_REQUESTED, 80)
    assert "Context reached 80% of its window" in body
    assert not waiting, "its Stop hook must not start /compact before the request is in"
    assert delivered
    assert again.messages == [] and again.drives == []


def test_a_full_idle_tab_that_ignored_the_request_is_compacted_anyway(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    _, tabs = _worker_team(bridge_env, spawn_process)
    config = Config(compact_at_tokens=600_000)
    full = _screen("  Opus 5.5 | 93% ctx | ~/x")
    with bridge_env.connect() as conn, transaction(conn):
        conn.execute(
            "UPDATE conversations SET compact_phase = ?, compact_warned_at = 0, compact_requested_at = ?"
            " WHERE conversation_id = 'conv-b'",
            (PHASE_REQUESTED, 1_000_000),
        )
        early = record_context_screens(conn, config, tabs, {"conv-b": (full, 1_100_000)}, 1_100_000)
        late = record_context_screens(conn, config, tabs, {"conv-b": (full, 1_400_000)}, 1_400_000)

    assert early.drives == []
    assert [(d[1], d[2]) for d in late.drives] == [("conv-b", True)]


def test_the_window_is_estimated_from_tokens_and_the_footer(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    _, tabs = _worker_team(bridge_env, spawn_process)
    config = Config(compact_at_tokens=600_000)

    def read(tokens: int, footer: str, busy: bool = False) -> int | None:
        screen = _screen(footer)
        if busy:
            screen = screen.replace("\u23fa Done.", "\u00b7 Thinking\u2026 (12s \u00b7 esc to interrupt)")
        with bridge_env.connect() as conn, transaction(conn):
            conn.execute("UPDATE conversations SET context_tokens = ? WHERE conversation_id = 'conv-b'", (tokens,))
            record_context_screens(conn, config, tabs, {"conv-b": (screen, 1)}, 1)
            row = conn.execute("SELECT context_window FROM conversations WHERE conversation_id = 'conv-b'").fetchone()
            return row[0]

    first = read(150_000, "  Fable 5.2 | 75% ctx | ~/x")
    # Mid-turn the hooks lag the status line: 100k stored while it shows 160k as 80%.
    lagging = read(100_000, "  Fable 5.2 | 80% ctx | ~/x", busy=True)
    switched = read(400_000, "  Opus 5.5 | 40% ctx | ~/x")

    assert first == 197_368
    assert lagging == 197_368, "a busy tab's pair is not trusted"
    assert switched == 975_609, "another model starts over"


def test_a_screen_read_before_a_compaction_or_of_another_conversation_is_dropped(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    _, tabs = _worker_team(bridge_env, spawn_process)
    config = Config(compact_at_tokens=600_000, assistant_handle="nobody")
    with bridge_env.connect() as conn, transaction(conn):
        conn.execute("UPDATE conversations SET compacted_at = 1_050_000 WHERE conversation_id = 'conv-b'")
        stale = record_context_screens(conn, config, tabs, {"conv-b": (LIMIT_SCREEN, 1_000_000)}, 1_400_000)
        why = conn.execute("SELECT context_dead_why FROM conversations WHERE conversation_id = 'conv-b'").fetchone()
        conn.execute("UPDATE conversations SET compacted_at = NULL")
        conn.execute("UPDATE instances SET conversation_id = 'conv-new' WHERE conversation_id = 'conv-b'")
        moved = record_context_screens(conn, config, tabs, {"conv-b": (LIMIT_SCREEN, 2_000_000)}, 2_000_000)

    assert stale.drives == [] and stale.messages == [] and why[0] is None
    assert moved.drives == [] and moved.messages == []


def test_a_host_tab_showing_the_limit_is_dead_until_a_real_size_shows(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    _, tabs = _worker_team(bridge_env, spawn_process)
    config = Config(assistant_handle="nobody")
    with bridge_env.connect() as conn, transaction(conn):
        dead = record_context_screens(conn, config, tabs, {"conv-b": (LIMIT_SCREEN, 1_000_000)}, 1_000_000)
        why = conn.execute("SELECT context_dead_why FROM conversations WHERE conversation_id = 'conv-b'").fetchone()
        record_context_screens(
            conn, config, tabs, {"conv-b": (_screen("  Opus 5.5 | 12% ctx | ~/x"), 1_100_000)}, 1_100_000
        )
        after = conn.execute("SELECT context_dead_why FROM conversations WHERE conversation_id = 'conv-b'").fetchone()
        body = conn.execute("SELECT body FROM messages ORDER BY id DESC LIMIT 1").fetchone()[0]

    assert why[0] == DEAD_LIMIT_SCREEN
    assert dead.drives == [], "compaction is off in this config"
    assert "compact it by hand" in body
    assert after[0] is None


def test_hidden_and_unknown_tabs_are_not_read(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    Team(bridge_env, spawn_process)
    tree = json.loads(Path(bridge_env.stub_tree).read_text())["result"]["tree"]
    tree["workspaces"][0]["sessions"][1]["name"] = "\U0001f512 Old B"
    with bridge_env.connect() as conn:
        tabs = list_context_tabs(conn, Config(), tree)

    assert [t.session for t in tabs] == ["TAB-A"]


PERMISSION_SCREEN = "\n".join(
    [
        "╭" + RULE + "╮",
        "│ Bash command                                   │",
        "│   gh pr list -R Org/app --state open           │",
        "│   List open PRs                                │",
        "│ Do you want to proceed?                        │",
        "│   1. Yes                                       │",
        "│   2. No, and tell Claude what to do differently │",
        "╰" + RULE + "╯",
    ]
)


def test_a_tab_waiting_on_a_permission_dialog_five_minutes_is_named_to_the_assistant_once(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    _, tabs = _worker_team(bridge_env, spawn_process)
    with bridge_env.connect() as conn:
        lead = conn.execute(
            "SELECT s.handle FROM instances i JOIN surfaces s ON s.surface = i.surface"
            " WHERE i.conversation_id = 'conv-a'"
        ).fetchone()[0]
    config = Config(assistant_handle=lead)

    def look(at: int, screen: str = PERMISSION_SCREEN) -> list[str]:
        with bridge_env.connect() as conn, transaction(conn):
            actions = record_context_screens(conn, config, tabs, {"conv-b": (screen, at)}, at)
            return [
                str(conn.execute("SELECT body FROM messages WHERE id = ?", (m,)).fetchone()[0])
                for m, _ in actions.messages
            ]

    assert look(1_000_000) == [], "first sight starts the wait"
    assert look(1_000_000 + 4 * 60_000) == []
    (told,) = look(1_000_000 + 5 * 60_000)
    assert "waited 5m on a permission dialog" in told and "gh pr list -R Org/app --state open" in told
    assert "│" not in told and "Do you want to proceed" not in told
    assert look(1_000_000 + 9 * 60_000) == [], "once per dialog"
    assert look(1_000_000 + 10 * 60_000, _screen("  Opus 5.5 | 20% ctx | ~/x")) == [], "answered: the marks are cleared"
    assert look(1_000_000 + 11 * 60_000) == [], "a new dialog waits its own five minutes"
