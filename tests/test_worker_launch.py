import json
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from conftest import VENV_BIN, BridgeEnv, SpawnProcess
from test_leads import Team

from bridge.cli_guild import build_worker_claude_command, list_project_mcp_servers, map_cwd_to_worker_settings
from bridge.config import load_config
from bridge.leads import ROLE_TEMPLATE
from bridge.restore import build_restore_command
from bridge.serve import DashboardState

BROWSER_RULE = (
    "No browser use (Claude in Chrome, Playwright) unless the task says so, and then only in a hidden tab group"
)
SKILL = Path(__file__).resolve().parents[1] / "agent" / "skills" / "bridge" / "SKILL.md"


def _write_mcp(directory: Path, *names: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    servers = {name: {"command": "true"} for name in names}
    (directory / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))


def test_project_servers_are_read_from_the_start_directory_and_every_one_above(tmp_path: Path) -> None:
    _write_mcp(tmp_path / "org", "linear-server", "context7")
    _write_mcp(tmp_path / "org" / "repo" / "worktree", "repo-db")
    (tmp_path / "org" / "repo" / ".mcp.json").write_text("not json")

    assert list_project_mcp_servers(tmp_path / "org" / "repo" / "worktree") == ["context7", "linear-server", "repo-db"]


def test_project_servers_behind_a_symlinked_start_directory_are_found(tmp_path: Path) -> None:
    _write_mcp(tmp_path / "real", "repo-db")
    (tmp_path / "link").symlink_to(tmp_path / "real")

    assert list_project_mcp_servers(tmp_path / "link" / "sub") == ["repo-db"]


def test_worker_settings_reject_project_servers_and_turn_playwright_off(tmp_path: Path) -> None:
    _write_mcp(tmp_path, "linear-server")

    settings = json.loads(map_cwd_to_worker_settings(tmp_path))

    assert settings == {
        "enabledPlugins": {"playwright@claude-plugins-official": False},
        "disabledMcpjsonServers": ["linear-server"],
    }


def test_worker_settings_without_project_servers_reject_nothing(tmp_path: Path) -> None:
    assert "disabledMcpjsonServers" not in json.loads(map_cwd_to_worker_settings(tmp_path))


def test_worker_claude_keeps_claude_ai_connectors_and_has_no_chrome(tmp_path: Path) -> None:
    _write_mcp(tmp_path, "it's-quoted")

    argv = shlex.split(build_worker_claude_command(tmp_path, "Your brief is in x"))

    assert argv[:3] == ["claude", "--dangerously-skip-permissions", "--no-chrome"]
    assert "--strict-mcp-config" not in argv
    assert "--mcp-config" not in argv
    assert json.loads(argv[argv.index("--settings") + 1])["disabledMcpjsonServers"] == ["it's-quoted"]
    assert argv[-1] == "Your brief is in x"


def test_worker_resume_line_keeps_its_settings_and_no_chrome(tmp_path: Path) -> None:
    _write_mcp(tmp_path, "linear-server")
    argv = ["/bin/claude", *shlex.split(build_worker_claude_command(tmp_path, "Your brief is in x"))[1:]]

    resumed = shlex.split(build_restore_command(argv, "conv"))

    assert resumed[:3] == ["/bin/claude", "--dangerously-skip-permissions", "--no-chrome"]
    assert json.loads(resumed[resumed.index("--settings") + 1])["disabledMcpjsonServers"] == ["linear-server"]
    assert resumed[-2:] == ["--resume", "conv"]
    assert "Your brief is in x" not in resumed


def test_guild_worker_starts_claude_without_chrome_and_briefs_it_on_the_browser(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    raw = json.loads(bridge_env.stub_tree.read_text())
    raw["result"]["tree"]["workspaces"][0]["sessions"].append({"id": "TAB-W", "name": "Task", "status": "active"})
    bridge_env.stub_tree.write_text(json.dumps(raw))
    cwd = bridge_env.root / "repo"
    _write_mcp(cwd, "linear-server")
    env = dict(bridge_env.env, CLAUDE_PID=str(team.a), AGTERM_SESSION_ID="TAB-A")

    done = subprocess.run(
        [str(VENV_BIN / "guild"), "worker", "Task", "--cwd", str(cwd), "--prompt", "Fix the thing"],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )  # fmt: skip

    assert done.returncode == 0, done.stderr
    [launch] = [line for line in bridge_env.stub_log.read_text().splitlines() if line.startswith("session new")]
    assert "--no-chrome" in launch
    assert "--strict-mcp-config" not in launch
    assert "disabledMcpjsonServers" in launch
    brief_path = re.search(r"Your brief is in (\S+\.md)", launch)
    assert brief_path is not None
    brief = Path(brief_path.group(1)).read_text()
    assert brief.startswith("# The Guild constitution") and "# Your task\n\nFix the thing\n" in brief
    assert "codex exec ... </dev/null" in brief
    assert BROWSER_RULE in brief
    assert "a part headed Reviews" in brief


@pytest.mark.parametrize(
    ("status", "where"),
    [("failed", "local"), ("ok", "host"), ("missing", "host")],
)
def test_a_worker_starts_on_this_mac_while_the_hosts_claude_watcher_reads_failed(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, status: str, where: str
) -> None:
    team = Team(bridge_env, spawn_process)
    raw = json.loads(bridge_env.stub_tree.read_text())
    raw["result"]["tree"]["workspaces"][0]["sessions"].append({"id": "TAB-W", "name": "Task", "status": "active"})
    bridge_env.stub_tree.write_text(json.dumps(raw))
    status_file = bridge_env.root / "claude-devbox.json"
    if status != "missing":
        run = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        fields = {"status": status, "last_run_utc": run, "summary": "weekly limit until 07:00"}
        status_file.write_text(json.dumps(fields))
    config_path = Path(bridge_env.env["BRIDGE_CONFIG"])
    config_path.write_text(
        'worker_host = "devbox"\nworker_host_watcher = "Claude · devbox"\n'
        + config_path.read_text()
        + f'[[watchers]]\nname = "Claude · devbox"\npath = "{status_file}"\n'
    )
    env = dict(bridge_env.env, CLAUDE_PID=str(team.a), AGTERM_SESSION_ID="TAB-A")

    done = subprocess.run(
        [str(VENV_BIN / "guild"), "worker", "Task", "--cwd", str(bridge_env.root), "--prompt", "Fix the thing"],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )  # fmt: skip

    if where == "local":
        assert done.returncode == 0, done.stderr
        assert "starting 'Task' on this Mac: Claude · devbox reads failed: weekly limit until 07:00" in done.stdout
        assert "session new" in bridge_env.stub_log.read_text()
    else:
        assert "'devbox' is not a [[remotes]] name" in done.stderr, "it went for the host"


def test_lead_role_and_skill_say_no_browser_unless_the_task_says_so() -> None:
    assert "No browser (Chrome, Playwright) unless the task says so, then a hidden tab group" in ROLE_TEMPLATE
    assert "one `guild ask summary` tells {assistant}, who picks what reaches {owner}." in ROLE_TEMPLATE
    assert " ".join(BROWSER_RULE.split()) in " ".join(SKILL.read_text().split())


def test_a_project_config_with_a_byte_order_mark_is_read(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text("﻿" + json.dumps({"mcpServers": {"repo-db": {}}}), encoding="utf-8")

    assert list_project_mcp_servers(tmp_path) == ["repo-db"]


def test_a_sixth_worker_under_one_lead_is_refused_and_the_five_are_named_oldest_first(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    raw = json.loads(bridge_env.stub_tree.read_text())
    sessions = raw["result"]["tree"]["workspaces"][0]["sessions"]
    sessions += [
        {"id": f"TAB-W{n}", "name": f"Task {n}", "status": "completed" if n == 2 else "active", "parent": "TAB-A",
         "statusChangedAt": 1000}
        for n in range(1, 6)
    ]  # fmt: skip
    sessions.append({"id": "TAB-X", "name": "Elsewhere", "status": "active", "parent": "TAB-B"})
    bridge_env.stub_tree.write_text(json.dumps(raw))
    env = dict(bridge_env.env, CLAUDE_PID=str(team.a), AGTERM_SESSION_ID="TAB-A")

    done = subprocess.run(
        [str(VENV_BIN / "guild"), "worker", "Task 6", "--cwd", str(bridge_env.root), "--prompt", "Fix the thing"],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )  # fmt: skip

    assert done.returncode != 0
    assert "5 of at most 5 workers already run under this lead" in done.stderr
    assert "- Task 2: age unknown, done, idle for " in done.stderr
    assert "Elsewhere" not in done.stderr
    log = bridge_env.stub_log.read_text() if bridge_env.stub_log.exists() else ""
    assert not [line for line in log.splitlines() if line.startswith("session new")]

    sessions[:] = [s for s in sessions if s["id"] != "TAB-W1"]
    sessions.append({"id": "TAB-W", "name": "Task 6", "status": "active"})
    bridge_env.stub_tree.write_text(json.dumps(raw))
    done = subprocess.run(
        [str(VENV_BIN / "guild"), "worker", "Task 6", "--cwd", str(bridge_env.root), "--prompt", "Fix the thing"],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )  # fmt: skip
    assert "workers already run" not in done.stderr
    assert [line for line in bridge_env.stub_log.read_text().splitlines() if line.startswith("session new")]


def test_the_refusal_says_each_workers_age_and_last_state_and_the_cap_comes_from_config() -> None:
    from bridge.workers import LiveWorker, map_workers_to_cap_refusal

    hour = 3_600_000
    workers = [
        LiveWorker("b", "Newer", "active", 10 * hour, 9 * hour),
        LiveWorker("a", "Older", "completed", 9 * hour, 2 * hour),
    ]
    lines = map_workers_to_cap_refusal(workers, 2, 11 * hour).splitlines()
    assert lines[1:] == ["- Older: started 9h ago, done, idle for 2h", "- Newer: started 2h ago, working for 1h"]


def test_laptop_cap_queues_a_worker_and_serve_starts_it_when_a_slot_frees(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, monkeypatch: pytest.MonkeyPatch
) -> None:
    team = Team(bridge_env, spawn_process)
    for key, value in bridge_env.env.items():
        monkeypatch.setenv(key, value)
    config_path = Path(bridge_env.env["BRIDGE_CONFIG"])
    config_path.write_text(config_path.read_text() + "laptop_agent_cap = 1\n")
    with bridge_env.connect() as conn:
        conn.execute("UPDATE instances SET status = 'active' WHERE ended_at IS NULL")
    env = dict(bridge_env.env, CLAUDE_PID=str(team.a), AGTERM_SESSION_ID="TAB-A")

    queued = subprocess.run(
        [str(VENV_BIN / "guild"), "worker", "Task", "--cwd", str(bridge_env.root), "--prompt", "Fix the thing"],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )  # fmt: skip

    assert queued.returncode == 0, queued.stderr
    assert "queued 'Task' (place 1)" in queued.stdout and "laptop cap is 1" in queued.stdout
    log = bridge_env.stub_log
    assert not log.exists() or "session new" not in log.read_text()
    assert len(list((bridge_env.state_dir / "worker-queue").glob("*.json"))) == 1

    state = DashboardState(load_config())
    state.start_queued_workers_once()
    assert not log.exists() or "session new" not in log.read_text(), "still at the cap"

    with bridge_env.connect() as conn:
        conn.execute("UPDATE instances SET status = 'idle' WHERE ended_at IS NULL")
    state.start_queued_workers_once()

    launches = [line for line in bridge_env.stub_log.read_text().splitlines() if line.startswith("session new")]
    assert len(launches) == 1 and "--parent TAB-A" in launches[0]
    assert list((bridge_env.state_dir / "worker-queue").glob("*.json")) == []
    with bridge_env.connect() as conn:
        text = conn.execute("SELECT body FROM messages ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert "Your queued worker 'Task' has started" in text


def test_a_local_worker_starts_on_the_pinned_model(tmp_path: Path) -> None:
    argv = shlex.split(build_worker_claude_command(tmp_path, "Your brief is in x", "claude-opus-5-5"))
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5"
    assert "--model" not in shlex.split(build_worker_claude_command(tmp_path, "Your brief is in x"))


def test_a_model_switch_dialog_is_confirmed_once_and_the_switch_is_read_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from bridge import cli_guild
    from bridge.config import Config

    screens = ["Switch model?\n\u276f 1. Yes, switch to Fable 5.2\n  2. No, go back", "⎿  Set model to Fable 5.2"]
    typed: list[list[str]] = []

    def run_agterm(config: Config, args: list[str]) -> str:
        typed.append(args)
        return ""

    monkeypatch.setattr(
        cli_guild, "read_screen_tail", mock.create_autospec(cli_guild.read_screen_tail, side_effect=screens)
    )
    monkeypatch.setattr(cli_guild, "run_agterm", run_agterm)
    monkeypatch.setattr(cli_guild.time, "sleep", mock.create_autospec(time.sleep))
    assert cli_guild.confirm_model_switch(Config(), "S1") == "switched"
    assert typed == [["session", "type", "--target", "S1", "\r"]]


def test_a_tab_is_known_on_a_model_by_its_footer() -> None:
    from bridge.cli_guild import convert_model_to_label, is_on_model

    assert convert_model_to_label("claude-opus-5-5") == "opus 5.5"
    assert convert_model_to_label("claude-fable-5-2") == "fable 5.2"
    assert convert_model_to_label("opus") == "opus"
    screen = "\u2500" * 40 + "\n\u276f \n" + "\u2500" * 40 + "\n  Opus 5.5 | 41% ctx | ~/acme-co\n  -- INSERT --"
    assert is_on_model(screen, "claude-opus-5-5") and not is_on_model(screen, "claude-fable-5-2")
    assert is_on_model(screen, "opus")


def test_a_busy_tab_reads_queued_not_stuck(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Typed while a turn runs, /model waits as the next prompt: five tabs reported 'stuck' and all switched later."""
    from bridge import cli_guild
    from bridge.config import Config

    monkeypatch.setattr(
        cli_guild, "read_screen_tail", mock.create_autospec(cli_guild.read_screen_tail, return_value="busy")
    )
    monkeypatch.setattr(
        cli_guild, "type_when_idle", mock.create_autospec(cli_guild.type_when_idle, return_value="stuck")
    )
    monkeypatch.setattr(
        cli_guild, "confirm_model_switch", mock.create_autospec(cli_guild.confirm_model_switch, return_value=None)
    )
    monkeypatch.setattr(
        cli_guild, "is_surface_locked", mock.create_autospec(cli_guild.is_surface_locked, return_value=False)
    )
    conn: Any = mock.Mock()
    conn.execute.return_value.fetchone.return_value = {"surface": "P1", "agterm_session": "S1"}
    args: Any = mock.Mock(model="claude-opus-5-5", names=["alex"], all=False)
    printed: list[str] = []

    def capture_print(*a: object, **k: object) -> None:
        printed.append(" ".join(map(str, a)))

    monkeypatch.setattr("builtins.print", capture_print)
    assert cli_guild.cmd_lead_model(args, conn, Config()) == 0
    assert printed[0].startswith("alex: queued")
