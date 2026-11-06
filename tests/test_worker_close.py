import json
import os
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path

import pytest
from conftest import VENV_BIN, BridgeEnv, SpawnProcess
from test_leads import Team

from bridge import workers
from bridge.cli_guild import parse_args
from bridge.leads import ROLE_TEMPLATE
from bridge.procinfo import ProcessArgs
from bridge.workers import ChildTab, TabSession, map_tab_to_closed_records

LEAD_TAB = "TAB-A"
WORKER_TAB = "TAB-B"
WORKER_CONV = "conv-b"
FILE_STUB = """#!/bin/sh
[ -f "$0.stderr" ] && cat "$0.stderr" >&2
[ -f "$0.out" ] && cat "$0.out"
[ -f "$0.rc" ] && exit "$(cat "$0.rc")"
exit 0
"""


class Worker:
    """Team's first tab is the lead, its second a worker nested under it, with ps, tmux and ssh stubbed."""

    def __init__(self, bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
        self.team = Team(bridge_env, spawn_process)
        self.env = bridge_env
        self.stubs = bridge_env.root / "stubs"
        self.stubs.mkdir()
        for name in ("ps", "tmux", "ssh"):
            stub = self.stubs / name
            stub.write_text(FILE_STUB)
            stub.chmod(0o755)
        self.stub_output("ps", "")
        self.stub_output("tmux", "", rc=1, stderr="no server running on /tmp/tmux\n")
        self.write_tree()

    def stub_output(self, name: str, out: str, rc: int = 0, stderr: str = "") -> None:
        (self.stubs / f"{name}.out").write_text(out)
        (self.stubs / f"{name}.rc").write_text(str(rc))
        (self.stubs / f"{name}.stderr").write_text(stderr)

    def write_tree(self, **worker: object) -> None:
        lead = {"id": LEAD_TAB, "name": "Lead", "surfaces": [{"paneID": "PANE-A"}]}
        tab = {"id": WORKER_TAB, "name": "Task one", "parent": LEAD_TAB, "surfaces": [{"paneID": "PANE-B"}], **worker}
        self.write_tabs(lead, tab)

    def write_tabs(self, *tabs: Mapping[str, object]) -> None:
        workspaces = [{"id": "WS", "name": "Work", "sessions": list(tabs)}]
        self.env.stub_tree.write_text(json.dumps({"ok": True, "result": {"tree": {"workspaces": workspaces}}}))

    def guild(self, *args: str, session: str = LEAD_TAB) -> subprocess.CompletedProcess[str]:
        env = {
            **self.env.env,
            "CLAUDE_PID": str(self.team.a),
            "AGTERM_SESSION_ID": session,
            "PATH": f"{self.stubs}{os.pathsep}{self.env.env['PATH']}",
        }
        return subprocess.run(
            [str(VENV_BIN / "guild"), *args], env=env, capture_output=True, text=True, timeout=30, check=False
        )

    def close(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.guild("worker", "close", *(args or ("Task one",)))

    def closed_tabs(self) -> list[str]:
        return [line for line in self.team.log().splitlines() if line.startswith("session close")]

    def records(self) -> int:
        conn = self.env.connect()
        try:
            return int(conn.execute("SELECT count(*) FROM closed_workers").fetchone()[0])
        finally:
            conn.close()

    def write_transcript(self, *commands: str) -> Path:
        project = Path(self.env.env["BRIDGE_CLAUDE_PROJECTS_DIR"]) / "p"
        project.mkdir(parents=True, exist_ok=True)
        lines = [
            {
                "timestamp": "2026-11-02T10:00:00Z",
                "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": c}}]},
            }
            for c in commands
        ]
        path = project / f"{WORKER_CONV}.jsonl"
        path.write_text("".join(json.dumps(line) + "\n" for line in lines))
        return path

    def add_remote(self) -> None:
        config = Path(self.env.env["BRIDGE_CONFIG"])
        config.write_text(config.read_text() + '\n[[remotes]]\nname = "devbox"\nssh = "devbox-alt"\n')

    def assert_refused(self, done: subprocess.CompletedProcess[str], reason: str) -> None:
        assert done.returncode == 1, done.stderr
        assert reason in done.stderr
        assert "refused: " in done.stderr
        assert self.closed_tabs() == []
        assert self.records() == 0


@pytest.fixture
def worker(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> Worker:
    return Worker(bridge_env, spawn_process)


def test_close_records_the_conversation_and_prints_the_resume_line(worker: Worker) -> None:
    done = worker.close()

    assert done.returncode == 0, done.stderr
    assert worker.closed_tabs() == [f"session close --target {WORKER_TAB}"]
    assert "closed Task one" in done.stdout
    assert f"--resume {WORKER_CONV}" in done.stdout
    assert f"cd {worker.env.root} && " in done.stdout

    listed = worker.guild("worker", "closed")
    assert "Task one" in listed.stdout
    assert "(lead old-a)" in listed.stdout
    assert f"--resume {WORKER_CONV}" in listed.stdout


def test_close_finds_the_tab_by_agterm_id(worker: Worker) -> None:
    assert worker.close(WORKER_TAB).returncode == 0
    assert worker.closed_tabs() == [f"session close --target {WORKER_TAB}"]


def test_a_tab_that_is_not_my_child_is_refused(worker: Worker) -> None:
    done = worker.guild("worker", "close", "Task one", session="TAB-ELSE")

    assert done.returncode == 2
    assert "not a tab nested under yours" in done.stderr
    assert worker.closed_tabs() == []


def test_two_children_with_one_name_are_refused_and_named_by_id(worker: Worker) -> None:
    lead = {"id": LEAD_TAB, "name": "Lead", "surfaces": [{"paneID": "PANE-A"}]}
    tabs = [
        {"id": tab, "name": "Task one", "parent": LEAD_TAB, "surfaces": [{"paneID": "PANE-B"}]}
        for tab in (WORKER_TAB, "TAB-C")
    ]
    worker.write_tabs(lead, *tabs)

    done = worker.close()

    assert done.returncode == 2
    assert "TAB-C" in done.stderr
    assert worker.closed_tabs() == []


def test_live_background_shell_refuses(worker: Worker) -> None:
    shell = "/bin/zsh -c source /home/u/.claude/shell-snapshots/snapshot-zsh-1.sh && long-job"
    worker.stub_output("ps", f"900 {worker.team.b} {shell}\n901 900 long-job --now\n")

    worker.assert_refused(worker.close(), "background shell 900: long-job --now")


def test_helpers_alone_do_not_refuse(worker: Worker) -> None:
    pid = worker.team.b
    worker.stub_output("ps", f"801 {pid} npm exec @playwright/mcp@latest\n802 {pid} caffeinate -i -t 300\n")

    done = worker.close()

    assert done.returncode == 0, done.stderr
    assert "refused" not in done.stderr


def test_a_shell_of_another_session_does_not_refuse(worker: Worker) -> None:
    shell = "/bin/zsh -c source /home/u/.claude/shell-snapshots/snapshot-zsh-1.sh"
    worker.stub_output("ps", f"900 {worker.team.a} {shell}\n")

    assert worker.close().returncode == 0


def test_a_detached_process_of_this_tab_refuses(worker: Worker, spawn_process: SpawnProcess) -> None:
    mine = spawn_process(env={"AGTERM_SESSION_ID": WORKER_TAB})
    worker.stub_output("ps", f"{mine} 1 nohup-job\n")

    worker.assert_refused(worker.close(), f"detached process {mine}: nohup-job")


def test_detached_processes_that_are_not_this_tabs_work_do_not_refuse(
    worker: Worker, spawn_process: SpawnProcess
) -> None:
    other = spawn_process(env={"AGTERM_SESSION_ID": "TAB-ELSE"})
    mux = spawn_process(env={"AGTERM_SESSION_ID": WORKER_TAB})
    worker.stub_output("ps", f"{other} 1 nohup-job\n{mux} 1 ssh: /tmp/ctl [mux]\n")

    assert worker.close().returncode == 0


def test_failed_ps_is_said_and_does_not_refuse(worker: Worker) -> None:
    worker.stub_output("ps", "", rc=1)

    done = worker.close()

    assert done.returncode == 0
    assert "not known: ps failed" in done.stderr


def test_live_local_tmux_session_refuses(worker: Worker) -> None:
    worker.write_transcript("tmux new -d -s nightly ./job.sh")
    worker.stub_output("tmux", "nightly\t$1\n")

    worker.assert_refused(worker.close(), "tmux session nightly on this machine")


def test_finished_local_tmux_session_does_not_refuse(worker: Worker) -> None:
    worker.write_transcript("tmux new -d -s nightly ./job.sh")
    worker.stub_output("tmux", "another\t$1\n")

    assert worker.close().returncode == 0


def test_live_remote_tmux_session_refuses(worker: Worker) -> None:
    worker.add_remote()
    worker.write_transcript("ssh devbox-alt 'tmux new -d -s w-run ./run.sh'")
    worker.stub_output("ssh", "w-run\t$4\n")

    done = worker.close()

    worker.assert_refused(done, "tmux session w-run on devbox")
    assert "outside tmux on devbox" in done.stderr


def test_remote_tmux_link_from_the_remote_drivers_refuses(worker: Worker) -> None:
    worker.add_remote()
    worker.write_transcript()
    conn = worker.env.connect()
    conn.execute(
        "INSERT INTO remote_drivers (conversation_id, host, session, last_seen_at) VALUES (?, 'devbox', 'old-run', 1)",
        (WORKER_CONV,),
    )
    conn.close()
    worker.stub_output("ssh", "old-run\t$4\n")

    worker.assert_refused(worker.close(), "tmux session old-run on devbox")


def test_unreachable_remote_is_said_and_does_not_refuse(worker: Worker) -> None:
    worker.add_remote()
    worker.write_transcript("ssh devbox-alt 'tmux new -d -s w-run ./run.sh'")
    worker.stub_output("ssh", "", rc=255, stderr="ssh: connect timed out\n")

    done = worker.close()

    assert done.returncode == 0
    assert "not known: could not list tmux on devbox" in done.stderr
    assert worker.closed_tabs() != []


def test_ssh_without_tmux_is_said_to_be_unseen(worker: Worker) -> None:
    worker.add_remote()
    worker.write_transcript("ssh devbox-alt 'nohup ./run.sh &'")

    done = worker.close()

    assert done.returncode == 0
    assert "not known: a run started outside tmux on devbox" in done.stderr


def test_missing_transcript_is_said(worker: Worker) -> None:
    done = worker.close()

    assert done.returncode == 0
    assert f"not known: no transcript for {WORKER_CONV}" in done.stderr


def test_a_subagent_still_writing_refuses_and_a_quiet_one_does_not(worker: Worker) -> None:
    path = worker.write_transcript()
    log = path.with_suffix("") / "subagents" / "agent-1.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text("{}\n")

    worker.assert_refused(worker.close(), "native subagent wrote")

    old = time.time() - 3600
    os.utime(log, (old, old))
    assert worker.close().returncode == 0


def test_mid_turn_session_refuses(worker: Worker) -> None:
    worker.env.write_session_file(worker.team.b, WORKER_CONV, status="busy")
    conn = worker.env.connect()
    conn.execute("UPDATE instances SET status = 'active' WHERE conversation_id = ?", (WORKER_CONV,))
    conn.close()

    worker.assert_refused(worker.close(), "Task one is mid-turn")


def test_agterm_active_status_refuses(worker: Worker) -> None:
    worker.write_tree(status="active")

    worker.assert_refused(worker.close(), "Task one is mid-turn")


def test_blocked_tab_refuses(worker: Worker) -> None:
    worker.write_tree(status="blocked")

    worker.assert_refused(worker.close(), "Task one is blocked on a prompt")


def test_undelivered_report_only_warns(worker: Worker) -> None:
    sent = worker.env.cli("send", "old-a", "the report", pid=worker.team.b)
    assert sent.returncode == 0, sent.stderr

    done = worker.close()

    assert done.returncode == 0, done.stderr
    assert "not delivered to you yet" in done.stderr
    assert "the report" in done.stderr
    assert worker.closed_tabs() != []


def test_delivered_report_does_not_warn(worker: Worker) -> None:
    worker.env.cli("send", "old-a", "the report", pid=worker.team.b)
    conn = worker.env.connect()
    conn.execute("UPDATE message_recipients SET offered_at = 1")
    conn.close()

    assert "not delivered" not in worker.close().stderr


def test_force_closes_and_says_what_it_passed_over(worker: Worker) -> None:
    worker.write_tree(status="active")
    worker.write_transcript("tmux new -d -s nightly ./job.sh")
    worker.stub_output("tmux", "nightly\t$1\n")

    done = worker.close("Task one", "--force")

    assert done.returncode == 0
    assert "forced past: Task one is mid-turn" in done.stdout
    assert "forced past: tmux session nightly" in done.stdout
    assert worker.closed_tabs() != []
    assert worker.records() == 1


@pytest.mark.parametrize("flags", [(), ("--force",)])
def test_nested_tabs_refuse_even_with_force(worker: Worker, flags: tuple[str, ...]) -> None:
    lead = {"id": LEAD_TAB, "name": "Lead", "surfaces": [{"paneID": "PANE-A"}]}
    tab = {"id": WORKER_TAB, "name": "Task one", "parent": LEAD_TAB, "surfaces": [{"paneID": "PANE-B"}]}
    worker.write_tabs(lead, tab, {"id": "TAB-C", "name": "Sub task", "parent": WORKER_TAB, "surfaces": []})

    done = worker.close("Task one", *flags)

    assert done.returncode == 2
    assert "Sub task" in done.stderr
    assert worker.closed_tabs() == []


def test_a_lead_nested_under_me_is_refused_even_with_force(worker: Worker) -> None:
    assert worker.team.guild("lead", "add", "Bea", "--tab", "old-b", "--scope", "s", "--label", "L").returncode == 0
    worker.write_tree()
    worker.env.stub_log.write_text("")

    done = worker.close("Task one", "--force")

    assert done.returncode == 2
    assert "team lead" in done.stderr
    assert worker.closed_tabs() == []


def test_a_locked_child_is_refused_even_with_force(worker: Worker) -> None:
    worker.write_tree(name="🔒 Task one")

    done = worker.close("🔒 Task one", "--force")

    assert done.returncode == 2
    assert "locked" in done.stderr
    assert worker.closed_tabs() == []


def test_failed_close_keeps_no_record(worker: Worker) -> None:
    stub = worker.env.root / "agtermctl"
    stub.write_text(stub.read_text().replace("#!/bin/sh\n", '#!/bin/sh\n[ "$1 $2" = "session close" ] && exit 1\n', 1))

    done = worker.close()

    assert done.returncode == 1
    assert "stays open" in done.stderr
    assert worker.records() == 0


def test_failed_close_of_a_tab_that_did_go_keeps_the_record(worker: Worker) -> None:
    gone = {"id": LEAD_TAB, "name": "Lead", "surfaces": [{"paneID": "PANE-A"}]}
    after = {"ok": True, "result": {"tree": {"workspaces": [{"id": "WS", "name": "Work", "sessions": [gone]}]}}}
    (worker.env.root / "tree-after.json").write_text(json.dumps(after))
    stub = worker.env.root / "agtermctl"
    stub.write_text(
        stub.read_text().replace(
            "#!/bin/sh\n",
            '#!/bin/sh\n[ "$1 $2" = "session close" ] && { '
            f'cp {worker.env.root / "tree-after.json"} "$STUB_TREE"; exit 1; }}\n',
            1,
        )
    )

    done = worker.close()

    assert done.returncode == 0, done.stderr
    assert worker.records() == 1
    assert f"--resume {WORKER_CONV}" in done.stdout


def test_a_turn_that_starts_during_the_checks_refuses(worker: Worker) -> None:
    worker.write_tree(status="active")
    busy = worker.env.stub_tree.read_text()
    (worker.env.root / "tree-busy.json").write_text(busy)
    worker.write_tree()
    worker.write_transcript("tmux new -d -s gone ./job.sh")
    tmux = worker.stubs / "tmux"
    tmux.write_text(f'#!/bin/sh\ncp {worker.env.root / "tree-busy.json"} "$STUB_TREE"\necho other\n')

    worker.assert_refused(worker.close(), "Task one is mid-turn")


def test_old_tmux_launch_past_the_drivers_scan_window_is_still_seen(worker: Worker) -> None:
    path = worker.write_transcript("tmux new -d -s nightly ./job.sh")
    with path.open("a") as transcript:
        transcript.write(json.dumps({"type": "padding", "text": "x" * 6_000_000}) + "\n")
    worker.stub_output("tmux", "nightly\t$1\n")

    worker.assert_refused(worker.close(), "tmux session nightly on this machine")


def test_a_run_of_the_conversation_before_clear_refuses(worker: Worker) -> None:
    worker.add_remote()
    conn = worker.env.connect()
    conn.execute(
        "INSERT INTO remote_drivers (conversation_id, host, session, last_seen_at) VALUES (?, 'devbox', 'old-run', 1)",
        (WORKER_CONV,),
    )
    conn.close()
    worker.write_transcript()
    worker.env.write_session_file(worker.team.b, "conv-b2")
    worker.team.start(worker.team.b, "conv-b2", "clear")
    worker.stub_output("ssh", "old-run\t$4\n")

    worker.assert_refused(worker.close(), "tmux session old-run on devbox")


def test_an_exited_pane_keeps_its_resume_record_beside_a_live_one(worker: Worker) -> None:
    conn = worker.env.connect()
    conn.execute("INSERT INTO surfaces (surface, updated_at) VALUES ('PANE-C', 1)")
    conn.execute(
        "INSERT INTO instances (instance_id, pid, proc_start_ms, conversation_id, surface, status, first_seen_at, "
        "last_observed_at, ended_at) VALUES ('1@1', 1, 1, 'conv-c', 'PANE-C', 'ended', 1, 1, 1)"
    )
    conn.execute(
        "INSERT INTO conversations (conversation_id, instance_id, first_seen_at, last_seen_at) "
        "VALUES ('conv-c', '1@1', 1, 1)"
    )
    conn.close()
    lead = {"id": LEAD_TAB, "name": "Lead", "surfaces": [{"paneID": "PANE-A"}]}
    panes = [{"paneID": "PANE-B"}, {"paneID": "PANE-C"}]
    worker.write_tabs(lead, {"id": WORKER_TAB, "name": "Task one", "parent": LEAD_TAB, "surfaces": panes})

    done = worker.close()

    assert done.returncode == 0, done.stderr
    assert "--resume conv-b" in done.stdout
    assert "--resume conv-c" in done.stdout
    assert worker.records() == 2


def test_tmux_on_its_own_socket_is_said_to_be_unchecked(worker: Worker) -> None:
    worker.write_transcript("tmux -L task new -d -s nightly ./job.sh")

    assert "socket of its own" in worker.close().stderr


def test_tmux_that_cannot_be_connected_to_is_said_and_not_read_as_empty(worker: Worker) -> None:
    worker.write_transcript("tmux new -d -s nightly ./job.sh")
    worker.stub_output("tmux", "", rc=1, stderr="error connecting to /tmp/tmux (Permission denied)\n")

    assert "not known: could not list tmux on this machine" in worker.close().stderr


def test_a_command_that_replaced_its_shell_is_said(worker: Worker) -> None:
    worker.stub_output("ps", f"900 {worker.team.b} sleep 3600\n")

    assert "not known: process 900 under the worker's claude: sleep 3600" in worker.close().stderr


def test_a_quiet_subagent_is_said_to_be_unknown(worker: Worker) -> None:
    path = worker.write_transcript()
    log = path.with_suffix("") / "subagents" / "agent-1.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text("{}\n")
    old = time.time() - 3600
    os.utime(log, (old, old))

    assert "not known: a native subagent silent" in worker.close().stderr


def test_resume_line_keeps_claudes_flags_and_drops_the_brief(monkeypatch: pytest.MonkeyPatch) -> None:
    argv = ["/bin/claude", "--dangerously-skip-permissions", "--strict-mcp-config", "Your brief is in x"]

    def read_args(_pid: int) -> ProcessArgs:
        return ProcessArgs(argv=argv, env={})

    def read_no_file(_pid: int) -> None:
        return None

    monkeypatch.setattr(workers, "process_args", read_args)
    monkeypatch.setattr(workers, "read_session_file", read_no_file)
    tab = ChildTab("T", "Task", "W", None, ("P",), (), None, "/tab cwd")
    session = TabSession("P", "conv", 42, True, "idle", None, None)

    [record] = map_tab_to_closed_records(tab, [session])

    assert record.resume_command == (
        "cd '/tab cwd' && /bin/claude --dangerously-skip-permissions --strict-mcp-config --resume conv"
    )


def test_a_tab_with_no_known_session_closes_with_nothing_to_resume(worker: Worker) -> None:
    lead = {"id": LEAD_TAB, "name": "Lead", "surfaces": [{"paneID": "PANE-A"}]}
    worker.write_tabs(lead, {"id": "TAB-S", "name": "Shell", "parent": LEAD_TAB, "surfaces": [{"paneID": "PANE-S"}]})

    done = worker.guild("worker", "close", "Shell")

    assert done.returncode == 0
    assert "nothing to resume" in done.stdout
    assert worker.records() == 0


def test_a_task_name_with_close_in_it_still_starts_a_worker() -> None:
    args = parse_args(["worker", "close the old issue", "--prompt", "x"])

    assert args.name == "close the old issue"
    assert parse_args(["worker", "close", "Task"]).tab == "Task"
    assert parse_args(["worker", "closed"]).limit == 30


def test_role_suggests_not_keeping_idle_workers() -> None:
    assert "Don't keep idle workers who have no plans for a new job or whose job is clearly done" in ROLE_TEMPLATE
