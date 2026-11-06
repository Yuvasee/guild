import json
import re
import time
from pathlib import Path
from typing import Any, cast

import pytest
from conftest import BridgeEnv, SpawnProcess
from test_leads import Team

from bridge.activity import (
    ProcessRow,
    RunningShell,
    SessionActivity,
    convert_command_to_label,
    convert_etime_to_ms,
    load_local_runs_by_conversation,
    map_panes_to_runs,
    map_processes_to_detached_work,
    map_processes_to_shells,
)
from bridge.config import Config, Remote
from bridge.drivers import map_command_to_local_links
from bridge.enrich import collect_watched_refs
from bridge.guildview import (
    Evidence,
    convert_body_to_shown_text,
    map_body_to_sections,
    map_db_to_guild,
    map_evidence_to_state,
    map_states_to_rollup,
)
from bridge.procinfo import ProcessArgs
from bridge.refs import map_text_to_ref
from bridge.remote import Collected, store
from bridge.remote_probe import list_runs  # pyright: ignore[reportUnknownVariableType]

IDLE = SessionActivity("idle", (), None)
SHELL = RunningShell("bash watch.sh", 3_600_000)
POD = Remote(name="devbox", ssh="devbox")


def evidence(**changes: object) -> Evidence:
    base: dict[str, Any] = {
        "tab_status": "completed", "tab_fresh": True, "blocked_for_ms": None, "activity": IDLE, "silent_for_ms": None,
        "remote_running": (), "remote_unsure": (), "waits": (),
    }  # fmt: skip
    merged: dict[str, Any] = {**base, **changes}
    return Evidence(**merged)


@pytest.mark.parametrize(
    ("changes", "state"),
    [
        ({}, "rest"),
        ({"activity": SessionActivity("shell", (), None)}, "working"),
        ({"activity": SessionActivity("idle", (SHELL,), None)}, "working"),
        ({"activity": SessionActivity("busy", (), None)}, "working"),
        ({"activity": SessionActivity("busy", (), None), "silent_for_ms": 3_600_000}, "unknown"),
        ({"activity": SessionActivity("busy", (SHELL,), None), "silent_for_ms": 3_600_000}, "working"),
        ({"tab_status": "active"}, "rest"),
        ({"remote_running": ("devbox/load",)}, "working"),
        ({"remote_unsure": ("devbox/load",)}, "unknown"),
        ({"waits": ("waiting for you",)}, "waiting"),
        ({"tab_status": "blocked"}, "waiting"),
        ({"tab_status": "blocked", "blocked_for_ms": 300_000}, "stuck"),
        ({"tab_status": "blocked", "blocked_for_ms": 300_000, "tab_fresh": False}, "rest"),
        ({"activity": None, "tab_status": "active"}, "unknown"),
        ({"activity": None, "tab_status": "completed"}, "unknown"),
        ({"activity": None, "tab_status": None}, "rest"),
        ({"activity": None, "tab_fresh": False}, "unknown"),
        ({"activity": SessionActivity(None, (), None)}, "unknown"),
    ],
)
def test_state_comes_from_what_is_really_running(changes: dict[str, object], state: str) -> None:
    assert map_evidence_to_state(evidence(**changes))["state"] == state


def test_rollup_lifts_a_lead_at_rest_to_what_its_workers_do() -> None:
    rest: dict[str, object] = {"state": "rest", "why": "turn ended"}
    working: dict[str, object] = {"task": "Load", "state": "working", "why": "running x"}
    stuck: dict[str, object] = {"task": "Build", "state": "stuck", "why": "a dialog"}

    assert map_states_to_rollup(rest, [])["state"] == "rest"
    assert map_states_to_rollup(rest, [working]) == {"state": "working", "why": "Load: running x"}
    assert map_states_to_rollup(rest, [working, stuck])["state"] == "stuck"
    waits = map_states_to_rollup({"state": "waiting", "why": "waiting for you"}, [working])
    assert waits["state"] == "working"


def test_detached_work_is_found_by_the_owner_it_inherited_and_helpers_are_not() -> None:
    claude, other = 100, 200
    rows = [
        ProcessRow(claude, 1, 90_000, "claude"),
        ProcessRow(101, claude, 5000, "/bin/zsh -c source x/shell-snapshots/snapshot-1"),
        ProcessRow(102, 1, 60_000, "/opt/k6 run plan.js"),
        ProcessRow(103, 102, 60_000, "/opt/k6-worker"),
        ProcessRow(104, 1, 60_000, "aws ssm start-session --target i-1"),
        ProcessRow(105, 1, 60_000, "python -m bridge _deliver 1 conv"),
        ProcessRow(106, 1, 60_000, "/opt/gone-owner"),
        ProcessRow(107, 1, 60_000, "/opt/no-env"),
    ]
    env = {101: claude, 102: claude, 103: claude, 104: claude, 105: claude, 106: 999}

    def read_args(pid: int) -> ProcessArgs | None:
        return ProcessArgs([], {"CLAUDE_PID": str(env[pid])}) if pid in env else None

    work = map_processes_to_detached_work(rows, {claude, other}, read_args)

    assert list(work) == [claude]
    assert [shell.label for shell in work[claude]] == ["k6 run"], "one per detached tree; its own children stay out"


def test_local_tmux_session_named_by_a_command_is_linked_and_only_a_real_run_counts() -> None:
    links = map_command_to_local_links("cd /x && tmux new-session -d -s load 'k6 run'", "conv", 5)
    remote = map_command_to_local_links("ssh devbox tmux new-session -d -s far", "conv", 5)
    rows = [
        ProcessRow(11, 10, 60_000, "/opt/k6 run plan.js"),
        ProcessRow(21, 20, 60_000, "nvim ."),
    ]

    rows.append(ProcessRow(41, 40, 60_000, "bash -c 'while true; do x; done'"))
    rows.append(ProcessRow(50, 1, 60_000, "/opt/k6 run other.js"))
    rows.append(ProcessRow(10, 1, 60_000, "-zsh"))
    panes = [("load", 10), ("editor", 20), ("empty", 30), ("inline", 40), ("direct", 50)]
    runs = map_panes_to_runs(panes, rows)

    assert [link.session for link in links] == ["load"]
    assert remote == []
    assert list(runs) == ["load", "inline", "direct"]
    assert runs["load"].label == "tmux load: k6 run"


def test_local_run_reaches_the_conversation_that_named_it_but_not_a_later_one(bridge_env: BridgeEnv) -> None:
    now = int(time.time() * 1000)
    run = RunningShell("tmux load: k6", 60_000)
    with bridge_env.connect() as conn:
        conn.execute(
            "INSERT INTO remote_drivers (conversation_id, host, session, last_seen_at) VALUES "
            "('recent', 'local', 'load', ?), ('before-it-started', 'local', 'load', ?), ('old', 'local', 'load', ?)",
            (now - 60_000, now - 3_600_000, now - 30 * 86_400_000),
        )
        linked = load_local_runs_by_conversation(conn, {"load": run}, now)

    assert list(linked) == ["recent"]


def test_only_bash_shells_count_and_labels_leave_arguments_out() -> None:
    claude = 100
    rows = [
        ProcessRow(101, claude, 5000, "npm exec @playwright/mcp@latest"),
        ProcessRow(102, claude, 5000, "caffeinate -i -t 300"),
        ProcessRow(
            103, claude, 60000, "/bin/zsh -c source /Users/me/.claude/shell-snapshots/snapshot-zsh-1.sh && eval x"
        ),
        ProcessRow(104, 103, 60000, "/bin/bash /tmp/scratchpad/watch.sh --token=hunter2 secretvalue extra"),
        ProcessRow(105, claude, 1000, "/bin/zsh -c source /Users/me/.claude/shell-snapshots/snapshot-zsh-2.sh"),
        ProcessRow(106, 105, 1000, "sleep 60"),
    ]

    shells = map_processes_to_shells(rows)

    assert [s.label for s in shells[claude]] == ["bash watch.sh", "a watch loop"]
    assert "hunter2" not in convert_command_to_label(rows[3].command)
    assert convert_command_to_label("bash -c 'T=abc; curl -H token'") == "a script"
    assert convert_command_to_label("TOKEN=abc ./run.sh") == "run.sh"
    assert list(shells) == [claude]


def test_ps_elapsed_time_forms() -> None:
    assert convert_etime_to_ms("00:26") == 26_000
    assert convert_etime_to_ms("01:57:32") == 7_052_000
    assert convert_etime_to_ms("90-04:10:41") == 90 * 86_400_000 + 15_041_000
    assert convert_etime_to_ms("junk") == 0


@pytest.mark.parametrize(
    ("text", "ref"),
    [
        ("PROJ-27", "issue:PROJ-27"),
        ("proj-12", "issue:PROJ-12"),
        ("#45", "pr:a/b#45"),
        ("Acme-Org/monorepo#45", "pr:Acme-Org/monorepo#45"),
        ("https://github.com/a/b/pull/3/files", "pr:a/b#3"),
        ("https://github.com/a/b/issues/4", "gh-issue:a/b#4"),
        ("https://linear.app/acme/issue/PROJ-9/some-title", "issue:PROJ-9"),
        ("pr:a/b#1", "pr:a/b#1"),
        ("P-PROJ-41", None),
        ("OTHER-1", None),
        ("see PROJ-1", None),
        ("junk", None),
    ],
)
def test_one_argument_is_one_ref_or_refused(text: str, ref: str | None) -> None:
    assert map_text_to_ref(text, issue_prefixes=("PROJ",), default_repo="a/b") == ref


def test_bare_number_without_a_repo_is_refused() -> None:
    assert map_text_to_ref("#12", issue_prefixes=("PROJ",), default_repo=None) is None


def test_work_chunks_are_declared_updated_finished_and_watched(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()

    first = team.guild("work", "set", "Export  worker", "ABC-7", pid=team.a)
    renamed = team.guild("work", "set", "export worker", "acme/web#9", pid=team.a)
    keep_refs = team.guild("work", "set", "Export worker", pid=team.a)
    bad = team.guild("work", "set", "Other", "not-a-ref", pid=team.a)
    outsider = team.guild("work", "set", "Mine", pid=team.b)
    by_name = team.guild("work", "set", "Seeded", "ABC-8", "--lead", "alex", pid=team.b)
    with bridge_env.connect() as conn:
        watched = collect_watched_refs(conn)
    listed = team.guild("work", "list", pid=team.a)
    done = team.guild("work", "done", "EXPORT WORKER", pid=team.a)
    again = team.guild("work", "done", "Export worker", pid=team.a)
    with bridge_env.connect() as conn:
        after = collect_watched_refs(conn)
    back = team.guild("work", "set", "Export worker", pid=team.a)

    assert first.returncode == renamed.returncode == keep_refs.returncode == by_name.returncode == 0
    assert bad.returncode == 2 and "not-a-ref" in bad.stderr
    assert outsider.returncode != 0
    assert "pr:acme/web#9" in listed.stdout and "ABC-7" not in listed.stdout, "given refs replace, none keep them"
    assert "pr:acme/web#9" in watched and "issue:ABC-8" in watched
    assert done.returncode == 0 and again.returncode == 2
    assert "pr:acme/web#9" not in after, "a finished chunk stops being polled"
    assert "acme/web#9" in back.stdout, "setting a finished chunk brings it back"


def test_lead_without_work_says_so_and_a_retired_lead_takes_none(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    bridge_env.cli("focus", "Something technical", pid=team.a)

    with bridge_env.connect() as conn:
        state = map_db_to_guild(conn, Config(), int(time.time() * 1000), lambda ref: "")
    lead = cast(list[dict[str, Any]], state["leads"])[0]
    team.guild("lead", "retire", "alex", "--force")
    retired = team.guild("work", "set", "Late", "--lead", "alex", pid=team.b)

    assert lead["work"] == []
    assert "Something technical" not in json.dumps(state["leads"])
    assert retired.returncode == 2


def test_a_run_on_the_host_makes_its_driver_working_and_a_silent_host_unknown(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    now = int(time.time() * 1000)
    tab: dict[str, object] = {"id": "TAB-A", "name": "Alex", "status": "completed"}
    tree: dict[str, object] = {"workspaces": [{"name": "Alex · Data", "sessions": [tab]}]}
    activity = {team.a: IDLE}
    run: dict[str, object] = {"tmux": "load", "tmux_id": "$5", "pane": "0.0", "label": "k6 run"}

    def lead_at(at: int) -> dict[str, Any]:
        with bridge_env.connect() as conn:
            state = map_db_to_guild(conn, Config(), at, lambda ref: "", tree, at, activity, at)
        return cast(list[dict[str, Any]], state["leads"])[0]

    with bridge_env.connect() as conn:
        conn.execute(
            "INSERT INTO remote_drivers (conversation_id, host, session, last_seen_at) "
            "VALUES ('conv-a', 'devbox', 'load', ?)",
            (now,),
        )
        store(conn, POD, Collected(True, None, [], now, [run]), Config(), now)
    working = lead_at(now)
    silent = lead_at(now + 600_000)
    with bridge_env.connect() as conn:
        store(conn, POD, Collected(True, None, [], now, []), Config(), now + 1000)
    finished = lead_at(now + 2000)

    assert (working["state"], working["why"]) == ("working", "running on devbox/load")
    assert silent["state"] == "unknown"
    assert finished["state"] == "rest"


def test_remote_link_outlives_a_day_but_not_a_later_run_and_hides_locked_and_keeps_unknown(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    now = int(time.time() * 1000)
    tab: dict[str, object] = {"id": "TAB-A", "name": "Alex", "status": "completed"}
    tree: dict[str, object] = {"workspaces": [{"name": "Alex · Data", "sessions": [tab]}]}
    activity = {team.a: IDLE}
    linked = now - 3 * 86_400_000

    def state_with(runs: list[dict[str, object]], sessions: list[dict[str, object]]) -> str:
        with bridge_env.connect() as conn:
            store(conn, POD, Collected(True, None, sessions, now, runs), Config(), now)
            guild = map_db_to_guild(conn, Config(), now, lambda ref: "", tree, now, activity, now)
        return str(cast(list[dict[str, Any]], guild["leads"])[0]["state"])

    with bridge_env.connect() as conn:
        conn.execute(
            "INSERT INTO remote_drivers (conversation_id, host, session, last_seen_at) "
            "VALUES ('conv-a', 'devbox', '$5', ?)",
            (linked,),
        )
    run: dict[str, object] = {"tmux": "load", "tmux_id": "$5", "pane": "0.0", "label": "k6", "started_at": linked + 5}
    agent: dict[str, object] = {"tmux": "load", "tmux_id": "$5", "pane": "0.0", "pid": 7, "started_at": linked}

    assert state_with([run], []) == "working", "three days after the command that started it"
    assert state_with([{**run, "started_at": now - 1000}], []) == "rest", "a later run under the same tmux id"
    assert state_with([{**run, "tmux": "Secret 🔒"}], []) == "rest"
    assert state_with([], [{**agent, "state": "unknown"}]) == "unknown"
    assert state_with([], [{**agent, "state": "finished"}]) == "rest"


def test_a_live_worker_whose_state_cannot_be_read_stays_however_old_its_tab_status(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    now = int(time.time() * 1000)
    old = (now - 5 * 3_600_000) / 1000
    worker: dict[str, object] = {
        "id": "TAB-B",
        "name": "Build",
        "parent": "TAB-A",
        "status": "completed",
        "statusChangedAt": old,
    }
    tree: dict[str, object] = {"workspaces": [{"name": "Alex · Data", "sessions": [worker]}]}
    with bridge_env.connect() as conn:
        guild = map_db_to_guild(
            conn, Config(), now, lambda ref: "", tree, now, {team.b: SessionActivity(None, (), None)}, now
        )

    assert [w["state"] for w in cast(list[dict[str, Any]], guild["leads"])[0]["workers"]] == ["unknown"]


def test_a_locked_tab_cannot_declare_work_for_a_lead(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    team.write_tree(workspace_a="Work", workspace_b="🔒 Misc")
    bridge_env.cli("sessions")

    done = team.guild("work", "set", "Private", "--lead", "alex", pid=team.b)

    assert done.returncode == 2 and "locked" in done.stderr


def test_probe_lists_panes_running_something_besides_an_agent() -> None:
    panes = [
        {"session": "shell", "pane": "0.0", "pid": "1", "session_id": "$1"},
        {"session": "load", "pane": "0.0", "pid": "2", "session_id": "$2"},
        {"session": "direct", "pane": "0.0", "pid": "4", "session_id": "$3"},
        {"session": "agent", "pane": "0.0", "pid": "5", "session_id": "$4"},
        {"session": "editor", "pane": "1.0", "pid": "6", "session_id": "$5"},
        {"session": "unreadable", "pane": "0.0", "pid": "8", "session_id": "$6"},
        {"session": "beside", "pane": "0.0", "pid": "10", "session_id": "$7"},
    ]
    argv = {
        1: ["-zsh"], 2: ["bash"], 3: ["/usr/bin/k6", "run", "--vus=9", "plan.js"], 4: ["./run.sh"], 5: ["claude"],
        6: ["zsh"], 7: ["nvim", "."], 8: ["bash"], 10: ["zsh"], 11: ["loadgen"],
    }  # fmt: skip

    runs = cast(
        list[dict[str, str]], list_runs(panes, {2: [3], 6: [7], 8: [9], 10: [5, 11]}, argv, {"0.0$4": 5, "0.0$7": 5})
    )

    assert [(r["tmux"], r["label"]) for r in runs] == [("load", "k6 run"), ("direct", "run.sh"), ("beside", "loadgen")]


def test_room_levels_separate_messages_from_status_lines_and_keep_locked_out(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    bridge_env.cli("board", "Export shipped", "--type", "report", pid=team.a)
    bridge_env.cli("board", "Restarting the service", "--type", "heads-up", pid=team.a)
    bridge_env.cli("board", "From a tab outside the team", pid=team.b)
    with bridge_env.connect() as conn:
        state = map_db_to_guild(conn, Config(), int(time.time() * 1000), lambda ref: "")
    room = {str(item["body"]): item for item in cast(list[dict[str, Any]], state["room"]) if item["kind"] == "post"}
    team.write_tree(workspace_a="Work", workspace_b="🔒 Misc")
    bridge_env.cli("sessions")
    with bridge_env.connect() as conn:
        locked = map_db_to_guild(conn, Config(), int(time.time() * 1000), lambda ref: "")

    assert room["Export shipped"]["level"] == "message"
    assert room["Export shipped"]["who"] == "Alex"
    assert room["Export shipped"]["leads"] == ["alex"]
    assert room["Restarting the service"]["level"] == "line"
    assert room["From a tab outside the team"]["level"] == "line"
    assert "From a tab outside the team" not in json.dumps(locked["room"])


@pytest.mark.parametrize(
    ("body", "shown"),
    [
        ("Ada: deploying now", "deploying now"),
        ("ada: deploying now", "deploying now"),
        ("ADA:deploying", "deploying"),
        ("Ada: Ada: twice", "twice"),
        ("Tooling (Ada): signed with a label", "signed with a label"),
        ("Quinn: not the author", "Quinn: not the author"),
        ("Tooling (Quinn): someone else", "Tooling (Quinn): someone else"),
        ("Ada: PROJ-1: both colons", "PROJ-1: both colons"),
        ("note from Ada: later", "note from Ada: later"),
        ("Ada:", "Ada:"),
        ("no prefix at all", "no prefix at all"),
    ],
)
def test_authors_own_name_is_not_repeated_in_the_text(body: str, shown: str) -> None:
    assert convert_body_to_shown_text(body, ("ada-tab", "ada")) == shown


def test_room_drops_the_authors_prefix_but_keeps_the_stored_text(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    bridge_env.cli("board", "Alex: export shipped", "--type", "report", pid=team.a)
    with bridge_env.connect() as conn:
        state = map_db_to_guild(conn, Config(), int(time.time() * 1000), lambda ref: "")
        stored = conn.execute("SELECT body FROM messages WHERE board = 1").fetchone()[0]

    assert [i["body"] for i in cast(list[dict[str, Any]], state["room"]) if i["kind"] == "post"] == ["export shipped"]
    assert stored == "Alex: export shipped"


def test_author_shows_as_the_lead_or_a_named_task_with_the_handle_kept_for_hover(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    bridge_env.cli("board", "From the worker", "--type", "report", pid=team.b)
    now = int(time.time() * 1000)

    def who_with_task(task: str) -> tuple[str, str]:
        tab: dict[str, object] = {"id": "TAB-B", "name": task, "parent": "TAB-A", "status": "active"}
        tree: dict[str, object] = {"workspaces": [{"name": "Alex · Data", "sessions": [tab]}]}
        with bridge_env.connect() as conn:
            state = map_db_to_guild(conn, Config(), now, lambda ref: "", tree, now, {}, now)
        post = cast(list[dict[str, Any]], state["room"])[-1]
        return post["who"], post["from"]

    assert who_with_task("Load test") == ("Load test · Alex", "old-b")
    assert who_with_task("Worker brief 0badcafe") == ("Alex", "old-b")
    with bridge_env.connect() as conn:
        outside = cast(list[dict[str, Any]], map_db_to_guild(conn, Config(), now, lambda ref: "")["room"])[-1]
    assert outside["who"] == "Old B", "outside the team: the tab's name when known"


def test_resources_list_environments_before_branches_and_prs(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    for resource in ("branch:acme/web:feat-x", "pr:acme/web#9", "env:staging"):
        assert bridge_env.cli("claim", resource, "--reason", "why", "--offline", pid=team.a).returncode == 0
    with bridge_env.connect() as conn:
        claims = cast(
            list[dict[str, Any]], map_db_to_guild(conn, Config(), int(time.time() * 1000), lambda ref: "")["claims"]
        )

    assert [(c["kind"], c["label"]) for c in claims] == [("env", "staging"), ("branch", "feat-x"), ("pr", "web#9")]


def test_page_has_a_favicon_no_external_loads_and_never_says_idle() -> None:
    page = (Path(__file__).parent.parent / "src/bridge/web/guild.html").read_text()

    assert 'rel="icon"' in page
    assert not re.search(r"""(?:src|href)=["']https?:""", page)
    assert not re.search(r"\bidle\b", page, re.I)


def sections(body: str, options: tuple[str, ...] = ()) -> list[tuple[object, object]]:
    return [(part["label"], part["text"]) for part in map_body_to_sections(body, list(options))]


def test_a_body_splits_on_its_labels_after_a_sentence_or_at_a_line_start() -> None:
    body = (
        "What: stops a leak. Why now: it blocks rollout.\nMerge cost: old grants stop.\nThey return.\nWait cost: none."
    )
    assert sections(body) == [
        ("What", "stops a leak."),
        ("Why now", "it blocks rollout."),
        ("Merge cost", "old grants stop.\nThey return."),
        ("Wait cost", "none."),
    ]


def test_an_answer_name_is_a_label_only_as_given() -> None:
    body = "It replaces six docs. Yes: they change. No: nothing changes. Maybe: not an answer here."
    assert sections(body, ("Yes", "No")) == [
        (None, "It replaces six docs."),
        ("Yes", "they change."),
        ("No", "nothing changes. Maybe: not an answer here."),
    ]


def test_an_answer_cost_counts_whatever_the_answer_is_called() -> None:
    body = "What: blocked. Ship behind a flag cost: a day. ship later cost: two days."
    assert sections(body, ("Ship behind a flag", "ship later")) == [
        ("What", "blocked."),
        ("Ship behind a flag cost", "a day."),
        ("ship later cost", "two days."),
    ]


@pytest.mark.parametrize(
    "body",
    [
        "Plain text\n\n  with its own breaks  \nand no labels.",
        "The real question is What: nobody knows, and Cost: unknown, mid sentence.",
        "see https://x.test/a?What: yes",
    ],
)
def test_a_body_without_labels_renders_as_it_is(body: str) -> None:
    assert sections(body) == [(None, body)]


def test_a_lead_shows_agterm_tab_name_and_tint(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    conn = bridge_env.connect()
    lead_row = conn.execute("SELECT surface, tint FROM leads WHERE name = 'alex'").fetchone()
    tab = conn.execute("SELECT agterm_session FROM surfaces WHERE surface = ?", (lead_row["surface"],)).fetchone()[0]
    now = int(time.time() * 1000)

    def shown(tree: dict[str, object] | None) -> tuple[object, object]:
        lead = cast(list[dict[str, Any]], map_db_to_guild(conn, Config(), now, lambda ref: "", tree, now)["leads"])[0]
        return lead["tab_name"], lead["tint"]

    def team_with(**fields: object) -> dict[str, object]:
        return {"workspaces": [{"name": "Other"}, {"name": "TEAM", "tint": "red", "sessions": [{**fields, "id": tab}]}]}

    assert shown(team_with(name="Alex · Moved", tint="purple")) == ("Alex · Moved", "purple")
    assert shown(team_with(name="Alex · Moved")) == ("Alex · Moved", None)
    assert shown(team_with(name="Alex · Moved", tint="javascript:x")) == ("Alex · Moved", None)
    conn.execute("UPDATE surfaces SET tab_name = 'Cached name' WHERE agterm_session = ?", (tab,))
    assert shown(None) == ("Cached name", lead_row["tint"])
    conn.execute("UPDATE surfaces SET tab_name = NULL WHERE agterm_session = ?", (tab,))
    conn.execute("UPDATE leads SET label = 'Label' WHERE name = 'alex'")
    assert shown(None) == ("Alex · Label", lead_row["tint"])
