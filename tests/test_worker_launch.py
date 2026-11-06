import json
import re
import shlex
import subprocess
from pathlib import Path

from conftest import VENV_BIN, BridgeEnv, SpawnProcess
from test_leads import Team

from bridge.cli_guild import build_worker_claude_command, list_project_mcp_servers, map_cwd_to_worker_settings
from bridge.leads import ROLE_TEMPLATE
from bridge.restore import build_restore_command

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
    assert brief.startswith("Fix the thing\n")
    assert BROWSER_RULE in brief
    assert "gets a Codex review and a Claude review" in brief and "section headed Reviews" in brief


def test_lead_role_and_skill_say_no_browser_unless_the_task_says_so() -> None:
    assert BROWSER_RULE in ROLE_TEMPLATE
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
        [str(VENV_BIN / "guild"), "worker", "Task 6", "--prompt", "Fix the thing"],
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
        [str(VENV_BIN / "guild"), "worker", "Task 6", "--prompt", "Fix the thing"],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )  # fmt: skip
    assert "workers already run" not in done.stderr
    assert [line for line in bridge_env.stub_log.read_text().splitlines() if line.startswith("session new")]


def test_the_refusal_says_each_workers_age_and_last_state_and_the_cap_comes_from_config() -> None:
    from bridge.config import Config, map_toml_to_config
    from bridge.workers import LiveWorker, map_workers_to_cap_refusal

    hour = 3_600_000
    workers = [
        LiveWorker("b", "Newer", "active", 10 * hour, 9 * hour),
        LiveWorker("a", "Older", "completed", 9 * hour, 2 * hour),
    ]
    lines = map_workers_to_cap_refusal(workers, 2, 11 * hour).splitlines()
    assert lines[1:] == ["- Older: started 9h ago, done, idle for 2h", "- Newer: started 2h ago, working for 1h"]
    assert Config().worker_cap == 5
    assert map_toml_to_config({"worker_cap": 3}).worker_cap == 3
