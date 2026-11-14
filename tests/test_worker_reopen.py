import ast
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from conftest import BridgeEnv, SpawnProcess
from test_host_workers import CONV, WORKTREE
from test_worker_close import LEAD_TAB, WORKER_CONV, WORKER_TAB, Worker

from bridge import host_side
from bridge.config import load_config
from bridge.host_workers import HostWorker, read_bound_conversations, read_record, write_record
from bridge.leads import ROLE_TEMPLATE
from bridge.procinfo import process_start_ms
from bridge.registry import reconcile
from bridge.serve import DashboardState
from bridge.watcher import notify_stray_workers, read_tree
from bridge.workers import (
    WORKER_CAP,
    ClosedRecord,
    forget_closed_workers,
    mark_reopened,
    record_closed_workers,
    unmark_reopened,
)

LEAD = {"id": LEAD_TAB, "name": "Lead", "surfaces": [{"paneID": "PANE-A"}]}
REOPENED = {"id": "TAB-W", "name": "Task one", "parent": LEAD_TAB, "status": "completed"}
SKILL = Path(__file__).resolve().parents[1] / "agent" / "skills" / "bridge" / "SKILL.md"


@pytest.fixture
def worker(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> Worker:
    return Worker(bridge_env, spawn_process)


def _close(worker: Worker) -> int:
    done = worker.close()
    assert done.returncode == 0, done.stderr
    conn = worker.env.connect()
    try:
        return int(conn.execute("SELECT id FROM closed_workers").fetchone()[0])
    finally:
        conn.close()


def _end_worker_run(worker: Worker) -> None:
    """Its claude has exited with the tab, as a real close ends it."""
    (worker.env.sessions_dir / f"{worker.team.b}.json").unlink()
    conn = worker.env.connect()
    try:
        conn.execute("UPDATE instances SET ended_at = 1 WHERE conversation_id = ?", (WORKER_CONV,))
    finally:
        conn.close()


def _launches(worker: Worker) -> list[str]:
    return [line for line in worker.team.log().splitlines() if line.startswith("session new")]


def _reopen(worker: Worker, wanted: str) -> subprocess.CompletedProcess[str]:
    return worker.guild("worker", "reopen", wanted)


def test_close_and_closed_offer_reopen_and_mark_the_raw_line_as_by_hand(worker: Worker) -> None:
    done = worker.close()
    closed_id = worker.env.connect().execute("SELECT id FROM closed_workers").fetchone()[0]

    assert f"reopen: guild worker reopen {closed_id}" in done.stdout
    assert f"by hand: creates a tab with no lead: cd {worker.env.root} && " in done.stdout
    listed = worker.guild("worker", "closed").stdout
    assert f"guild worker reopen {closed_id}" in listed
    assert f"by hand: creates a tab with no lead: cd {worker.env.root} && " in listed
    assert listed.index("guild worker reopen") < listed.index("by hand")


@pytest.mark.parametrize("by_name", [False, True])
def test_reopen_resumes_the_worker_under_my_tab_with_its_old_name(worker: Worker, by_name: bool) -> None:
    closed_id = _close(worker)
    _end_worker_run(worker)
    worker.write_tabs(LEAD, REOPENED)

    done = _reopen(worker, "Task one" if by_name else str(closed_id))

    assert done.returncode == 0, done.stderr
    [launch] = _launches(worker)
    assert f"--parent {LEAD_TAB}" in launch and "--name Task one" in launch
    assert f"--cwd {worker.env.root}" in launch
    assert f"--resume {WORKER_CONV}" in launch and launch.split("--command ", 1)[1].startswith("zsh -lc ")
    assert "resumed" in done.stdout
    conn = worker.env.connect()
    assert conn.execute("SELECT reopened_tab FROM closed_workers").fetchone()[0] == "TAB-W"
    assert worker.guild("worker", "closed").stdout.strip() == "no closed workers"
    assert conn.execute("SELECT parent_session FROM tab_parents WHERE agterm_session = 'TAB-W'").fetchone()[0] == (
        LEAD_TAB
    )
    again = _reopen(worker, str(closed_id))
    assert again.returncode == 2 and "already reopened in tab Task one (TAB-W)" in again.stderr


def test_only_its_lead_or_the_assistant_reopens_it(worker: Worker) -> None:
    closed_id = _close(worker)
    _end_worker_run(worker)
    conn = worker.env.connect()
    conn.execute("UPDATE closed_workers SET parent_handle = 'someone-else', parent_session = 'TAB-OTHER'")

    done = _reopen(worker, str(closed_id))

    assert done.returncode == 2
    assert "only its lead someone-else or" in done.stderr
    assert _launches(worker) == []


def test_a_conversation_running_in_a_nested_tab_is_refused(worker: Worker) -> None:
    closed_id = _close(worker)

    done = _reopen(worker, str(closed_id))

    assert done.returncode == 2
    assert f"already runs in tab Task one ({WORKER_TAB})" in done.stderr
    assert _launches(worker) == []


def test_a_tab_resumed_by_hand_outside_the_tree_is_closed_first_when_idle(worker: Worker) -> None:
    closed_id = _close(worker)
    worker.write_tabs(LEAD, {"id": WORKER_TAB, "name": "Some title", "surfaces": [{"paneID": "PANE-B"}]}, REOPENED)

    done = _reopen(worker, str(closed_id))

    assert done.returncode == 0, done.stderr
    assert f"closed Some title ({WORKER_TAB}), where it ran outside your tree" in done.stdout
    closes = [line for line in worker.team.log().splitlines() if line.startswith("session close")]
    assert closes[-1] == f"session close --target {WORKER_TAB}"
    assert len(_launches(worker)) == 1


def test_a_stray_tab_mid_turn_refuses_and_stays(worker: Worker) -> None:
    closed_id = _close(worker)
    stray = {"id": WORKER_TAB, "name": "Some title", "status": "active", "surfaces": [{"paneID": "PANE-B"}]}
    worker.write_tabs(LEAD, stray)

    done = _reopen(worker, str(closed_id))

    assert done.returncode == 2
    assert "outside your tree: Some title is mid-turn. Reopen it when it is idle" in done.stderr
    assert worker.closed_tabs() == [f"session close --target {WORKER_TAB}"]
    assert _launches(worker) == []


def test_reopen_counts_toward_the_worker_cap(worker: Worker) -> None:
    closed_id = _close(worker)
    _end_worker_run(worker)
    others = [
        {"id": f"TAB-X{n}", "name": f"Other task {n}", "parent": LEAD_TAB, "status": "completed"}
        for n in range(WORKER_CAP)
    ]
    worker.write_tabs(LEAD, *others)

    done = _reopen(worker, str(closed_id))

    assert done.returncode == 2
    assert f"{WORKER_CAP} of at most {WORKER_CAP} workers already run under this lead" in done.stderr
    assert _launches(worker) == []


def test_reopen_over_the_laptop_cap_queues_and_serve_reopens_it(
    worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed_id = _close(worker)
    _end_worker_run(worker)
    for key, value in worker.env.env.items():
        monkeypatch.setenv(key, value)
    config = Path(worker.env.env["BRIDGE_CONFIG"])
    config.write_text(config.read_text() + "laptop_agent_cap = 1\n")
    conn = worker.env.connect()
    conn.execute("UPDATE instances SET status = 'active' WHERE ended_at IS NULL")
    worker.write_tabs(LEAD)

    done = _reopen(worker, str(closed_id))
    again = _reopen(worker, str(closed_id))

    assert done.returncode == 0, done.stderr
    assert "queued reopening 'Task one' (place 1)" in done.stdout
    assert again.returncode == 2 and "already queued to reopen" in again.stderr
    [queued] = list((worker.env.state_dir / "worker-queue").glob("*.json"))
    assert json.loads(queued.read_text())["reopens"] == WORKER_CONV

    conn.execute("UPDATE instances SET status = 'idle' WHERE ended_at IS NULL")
    DashboardState(load_config()).start_queued_workers_once()

    [launch] = _launches(worker)
    assert f"--parent {LEAD_TAB}" in launch and f"--resume {WORKER_CONV}" in launch
    assert conn.execute("SELECT reopened_tab FROM closed_workers").fetchone()[0] == "TAB-W"
    assert list((worker.env.state_dir / "worker-queue").glob("*.json")) == []


def test_two_closed_workers_with_one_name_are_named_by_id(worker: Worker) -> None:
    _close(worker)
    conn = worker.env.connect()
    conn.execute(
        "INSERT INTO closed_workers (conversation_id, tab_name, parent_handle, resume_command, closed_at) "
        "SELECT 'conv-other', tab_name, parent_handle, resume_command, closed_at + 1 FROM closed_workers"
    )
    ids = [str(r[0]) for r in conn.execute("SELECT id FROM closed_workers ORDER BY id DESC")]

    done = _reopen(worker, "Task one")

    assert done.returncode == 2
    assert f"2 closed workers are named 'Task one'; name one by id: {', '.join(ids)}" in done.stderr


def test_a_stray_tab_is_linked_to_its_lead_and_the_lead_is_told_once(
    worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed_id = _close(worker)
    for key, value in worker.env.env.items():
        monkeypatch.setenv(key, value)
    worker.write_tabs(LEAD, {"id": WORKER_TAB, "name": "Some title", "surfaces": [{"paneID": "PANE-B"}]})
    config = load_config()
    conn = worker.env.connect()
    tree = read_tree(config)
    assert tree is not None

    first = notify_stray_workers(conn, config, tree, 1000)
    second = notify_stray_workers(conn, config, tree, 2000)

    assert len(first) == 1 and second == []
    body = conn.execute("SELECT body FROM messages WHERE id = ?", (first[0][0],)).fetchone()[0]
    assert body == (
        f'Tab "Some title" ({WORKER_TAB}) runs your closed worker Task one outside your tree; '
        f"reopen it with guild worker reopen {closed_id} when it is idle."
    )
    reconcile(conn, config, 3000)
    assert (
        conn.execute("SELECT parent_session FROM tab_parents WHERE agterm_session = ?", (WORKER_TAB,)).fetchone()[0]
        == LEAD_TAB
    )

    worker.write_tabs(LEAD)
    tree = read_tree(config)
    assert tree is not None
    assert notify_stray_workers(conn, config, tree, 4000) == []
    assert conn.execute("SELECT count(*) FROM stray_workers").fetchone()[0] == 0


def test_a_nested_tab_running_a_closed_conversation_is_not_stray(
    worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    _close(worker)
    for key, value in worker.env.env.items():
        monkeypatch.setenv(key, value)
    config = load_config()
    tree = read_tree(config)
    assert tree is not None

    assert notify_stray_workers(worker.env.connect(), config, tree, 1000) == []


def test_lead_role_and_skill_say_reopen_never_by_hand() -> None:
    assert "back only via `guild worker reopen <id>`" in ROLE_TEMPLATE
    assert "`guild worker reopen <id or tab name>`, never by hand" in SKILL.read_text()


def test_a_resume_whose_tab_closed_at_once_keeps_the_worker_closed(worker: Worker) -> None:
    closed_id = _close(worker)
    _end_worker_run(worker)
    worker.write_tabs(LEAD)

    done = _reopen(worker, str(closed_id))

    assert done.returncode == 1
    assert f"NOT resumed: the tab closed, so its resume command exited. guild worker reopen {closed_id}" in done.stdout
    assert f"guild worker reopen {closed_id}" in worker.guild("worker", "closed").stdout


def test_the_lead_reopens_by_its_name_once_the_tab_that_closed_it_is_gone(worker: Worker) -> None:
    closed_id = _close(worker)
    _end_worker_run(worker)
    assert worker.team.enroll().returncode == 0
    conn = worker.env.connect()
    lead = conn.execute("SELECT name FROM leads").fetchone()[0]
    conn.execute("UPDATE closed_workers SET parent_lead = ?, parent_session = 'TAB-GONE'", (lead,))
    worker.write_tabs(LEAD, REOPENED)

    done = _reopen(worker, str(closed_id))

    assert done.returncode == 0, done.stderr
    conn.execute("UPDATE closed_workers SET parent_lead = NULL, reopened_tab = NULL, reopened_at = NULL")
    refused = _reopen(worker, str(closed_id))
    assert refused.returncode == 2 and "only its lead" in refused.stderr


def test_a_stray_tab_with_nested_tabs_is_not_closed(worker: Worker) -> None:
    closed_id = _close(worker)
    stray = {"id": WORKER_TAB, "name": "Some title", "surfaces": [{"paneID": "PANE-B"}]}
    worker.write_tabs(LEAD, stray, {"id": "TAB-C", "name": "Child", "parent": WORKER_TAB})

    done = _reopen(worker, str(closed_id))

    assert done.returncode == 2 and "has nested tabs that would close with it" in done.stderr
    assert worker.closed_tabs() == [f"session close --target {WORKER_TAB}"]


def test_another_conversation_in_a_stray_tab_drops_its_old_lead_link(
    worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    _close(worker)
    for key, value in worker.env.env.items():
        monkeypatch.setenv(key, value)
    worker.write_tabs(LEAD, {"id": WORKER_TAB, "name": "Some title", "surfaces": [{"paneID": "PANE-B"}]})
    config = load_config()
    conn = worker.env.connect()
    tree = read_tree(config)
    assert tree is not None
    assert len(notify_stray_workers(conn, config, tree, 1000)) == 1

    conn.execute("UPDATE closed_workers SET conversation_id = 'conv-gone'")
    assert notify_stray_workers(conn, config, tree, 2000) == []

    assert conn.execute("SELECT count(*) FROM stray_workers").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM tab_parents WHERE agterm_session = ?", (WORKER_TAB,)).fetchone()[0] == 0


def test_a_failed_attempt_undoes_only_its_own_reopen_mark(bridge_env: BridgeEnv) -> None:
    conn = bridge_env.connect()
    record_closed_workers(conn, [ClosedRecord("c", "claude --resume c")], "Task", "lead", "TAB-A", None, 1)
    mark_reopened(conn, "c", "TAB-1", 2)
    mark_reopened(conn, "c", "TAB-2", 3)

    unmark_reopened(conn, "c", "TAB-1")

    assert conn.execute("SELECT reopened_tab FROM closed_workers").fetchone()[0] == "TAB-2"


def test_an_id_dropped_by_a_failed_close_is_never_given_again(bridge_env: BridgeEnv) -> None:
    conn = bridge_env.connect()
    [first] = record_closed_workers(conn, [ClosedRecord("a", "x")], "A", "lead", "TAB-A", None, 1)
    forget_closed_workers(conn, [first])

    [second] = record_closed_workers(conn, [ClosedRecord("b", "y")], "B", "lead", "TAB-A", None, 2)

    assert second != first


def test_a_stray_tab_nested_later_keeps_its_real_parent(worker: Worker, monkeypatch: pytest.MonkeyPatch) -> None:
    _close(worker)
    for key, value in worker.env.env.items():
        monkeypatch.setenv(key, value)
    worker.write_tabs(LEAD, {"id": WORKER_TAB, "name": "Some title", "surfaces": [{"paneID": "PANE-B"}]})
    config = load_config()
    conn = worker.env.connect()
    tree = read_tree(config)
    assert tree is not None
    assert len(notify_stray_workers(conn, config, tree, 1000)) == 1

    worker.write_tabs(
        LEAD, {"id": WORKER_TAB, "name": "Some title", "parent": "TAB-X", "surfaces": [{"paneID": "PANE-B"}]}
    )
    tree = read_tree(config)
    assert tree is not None
    assert notify_stray_workers(conn, config, tree, 2000) == []

    assert conn.execute("SELECT count(*) FROM stray_workers").fetchone()[0] == 0
    row = conn.execute("SELECT parent_session FROM tab_parents WHERE agterm_session = ?", (WORKER_TAB,)).fetchone()
    assert row[0] == "TAB-X"


HOST_LINE = (
    f"ssh -t devbox-alt '{{ [ -d {WORKTREE} ] || git -C /home/dev/app worktree add --detach {WORKTREE}; }} && "
    f"cd {WORKTREE} && ~/.local/bin/claude --model default --effort high --resume {CONV}'"
)
SSH_STUB = """#!/bin/sh
cat > "$0.stdin"
cat "$0.out"
"""


def _close_host_worker(worker: Worker, line: str = HOST_LINE, ran_on_host: bool = True) -> int:
    worker.add_remote()
    (worker.stubs / "ssh").write_text(SSH_STUB)
    launched = {"ok": True, "dir": "/home/dev/.local/state/gbridge/g-task-ab12", "cwd": WORKTREE,
                "checkout": "/home/dev/app", "worktree": WORKTREE, "restored": True}  # fmt: skip
    worker.stub_output("ssh", json.dumps(launched) + "\n")
    conn = worker.env.connect()
    [closed_id] = record_closed_workers(conn, [ClosedRecord(CONV, line)], "Task one", "lead", LEAD_TAB, None, 1)
    if ran_on_host:
        conn.execute(
            "INSERT INTO instances (instance_id, pid, proc_start_ms, conversation_id, kind, status, first_seen_at, "
            "last_observed_at, ended_at) VALUES ('1@1', 1, 1, ?, 'host:devbox', 'ended', 1, 1, 1)",
            (CONV,),
        )
    return closed_id


def test_a_host_worker_reopens_like_a_fresh_one_with_gbridge_under_its_lead(
    worker: Worker, spawn_process: SpawnProcess, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in worker.env.env.items():
        monkeypatch.setenv(key, value)
    closed_id = _close_host_worker(worker)
    worker.write_tabs(LEAD, REOPENED)
    conn = worker.env.connect()
    loop = spawn_process()

    with ThreadPoolExecutor(1) as pool:
        reopening = pool.submit(_reopen, worker, str(closed_id))
        deadline = time.monotonic() + 20
        while not _launches(worker) and time.monotonic() < deadline:
            time.sleep(0.1)
        # Its first hook through the new tab's loop process, as gbridge sends it once Claude has resumed.
        conn.execute("INSERT INTO surfaces (surface, agterm_session, updated_at) VALUES ('PANE-H', 'TAB-W', 1)")
        conn.execute(
            "INSERT INTO instances (instance_id, pid, proc_start_ms, conversation_id, surface, kind, status, "
            "first_seen_at, last_observed_at) VALUES (?, ?, ?, ?, 'PANE-H', 'host:devbox', 'idle', 1, 1)",
            (f"{loop}@1", loop, process_start_ms(loop), CONV),
        )
        done = reopening.result()

    assert done.returncode == 0, done.stderr
    assert "tab TAB-W: Task one (Claude on devbox, tmux -L gbridge session g-task-ab12)" in done.stdout
    assert done.stdout.rstrip().endswith("resumed")
    [launch] = _launches(worker)
    assert f"--parent {LEAD_TAB}" in launch and "--name Task one" in launch
    assert "_host-tab g-task-ab12" in launch and "--resume" not in launch
    program = (worker.stubs / "ssh.stdin").read_text()
    [literal] = [line.removeprefix("PAYLOAD = ") for line in program.splitlines() if line.startswith("PAYLOAD = '")]
    sent = json.loads(ast.literal_eval(literal))
    assert (sent["op"], sent["worker"], sent["resume"], sent["checkout"], sent["cwd"]) == (
        "launch",
        "g-task-ab12",
        CONV,
        "/home/dev/app",
        WORKTREE,
    )
    assert sent["claude_flags"].endswith("--model default --effort high") and len(sent["token"]) == 64
    assert "brief" not in sent and host_side.GBRIDGE_MARK in sent["gbridge"]
    record = read_record("g-task-ab12")
    assert record is not None
    assert (record.parent, record.name, record.host, record.remote_worktree) == (
        LEAD_TAB,
        "Task one",
        "devbox",
        WORKTREE,
    )
    assert record.model_flags == "--model default --effort high"
    assert read_bound_conversations("g-task-ab12") == [CONV]
    assert conn.execute("SELECT reopened_tab FROM closed_workers").fetchone()[0] == "TAB-W"


def test_a_host_worker_from_before_its_own_worktree_is_refused_with_its_by_hand_line(worker: Worker) -> None:
    line = f"ssh -t devbox-alt 'cd /home/dev/app && ~/.local/bin/claude --resume {CONV}'"
    closed_id = _close_host_worker(worker, line)

    done = _reopen(worker, str(closed_id))

    assert done.returncode == 2
    assert "before host workers had a worktree of their own" in done.stderr and line in done.stderr
    assert _launches(worker) == [] and not (worker.stubs / "ssh.stdin").exists()


def test_a_host_worker_whose_record_is_still_kept_is_refused(worker: Worker, monkeypatch: pytest.MonkeyPatch) -> None:
    closed_id = _close_host_worker(worker)
    for key, value in worker.env.env.items():
        monkeypatch.setenv(key, value)
    write_record(
        HostWorker(
            worker="g-task-ab12", name="Task one", host="devbox", ssh="devbox-alt", cwd="/", remote_cwd=WORKTREE,
            remote_dir="", lead="lead", parent=LEAD_TAB, token_sha256="", created_at=1,
        )
    )  # fmt: skip

    done = _reopen(worker, str(closed_id))

    assert done.returncode == 2
    assert "g-task-ab12 still has its host worker record: guild worker attach g-task-ab12" in done.stderr
    assert _launches(worker) == [] and not (worker.stubs / "ssh.stdin").exists()


def test_a_conversation_that_never_ran_on_the_host_is_not_given_a_host_token(worker: Worker) -> None:
    closed_id = _close_host_worker(worker, ran_on_host=False)

    done = _reopen(worker, str(closed_id))

    assert done.returncode == 2
    assert "Task one's conversation never ran on devbox, so it is not reopened there" in done.stderr
    assert _launches(worker) == [] and not (worker.stubs / "ssh.stdin").exists()
