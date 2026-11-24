import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from conftest import VENV_BIN, BridgeEnv, SpawnProcess
from test_leads import Team

from bridge.errors import UsageError
from bridge.folder_trust import ensure_worker_folder_trusted, find_trusting_path

TRUSTED = {"hasTrustDialogAccepted": True}


def _write_claude_json(home: Path, projects: dict[str, Any]) -> Path:
    path = home / ".claude.json"
    path.write_text(json.dumps({"numStartups": 7, "projects": projects}))
    return path


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = (tmp_path / "home").resolve()
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def test_outside_git_a_trusted_folder_above_counts(home: Path) -> None:
    folder = home / "notes" / "week"
    folder.mkdir(parents=True)
    _write_claude_json(home, {str(home / "notes"): TRUSTED})

    assert find_trusting_path(folder) == str(home / "notes")


def test_in_a_checkout_only_folders_up_to_its_root_and_its_main_checkout_count(home: Path) -> None:
    repo = home / "code" / "shop"
    (repo / "api").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "x")
    linked = home / "code" / "shop-wt" / "fix"
    _git(repo, "worktree", "add", "-q", "--detach", str(linked))

    _write_claude_json(home, {str(home / "code"): TRUSTED, str(repo): {"hasTrustDialogAccepted": False}})
    assert find_trusting_path(repo / "api") is None
    assert find_trusting_path(linked) is None

    _write_claude_json(home, {str(repo): TRUSTED})
    assert find_trusting_path(repo / "api") == str(repo)
    assert find_trusting_path(linked) == str(repo)


def test_a_folder_outside_the_roots_is_refused_and_nothing_is_written(home: Path) -> None:
    folder = home / "downloads" / "tool"
    folder.mkdir(parents=True)
    config = _write_claude_json(home, {})
    before = config.read_text()

    with pytest.raises(UsageError, match=r"Claude Code has not trusted .* run claude in it once.*worker_trust_roots"):
        ensure_worker_folder_trusted(folder, ("~/code",))

    assert config.read_text() == before


def test_a_folder_under_a_root_is_marked_trusted_and_the_rest_of_the_file_is_kept(home: Path) -> None:
    folder = home / "code" / "fresh-clone"
    folder.mkdir(parents=True)
    config = _write_claude_json(home, {"/elsewhere": {"hasTrustDialogAccepted": False, "lastCost": 2}})

    note = ensure_worker_folder_trusted(folder, ("~/code",))

    assert note == f"marked {folder} trusted for Claude Code: it is under ~/code in worker_trust_roots"
    data = json.loads(config.read_text())
    assert data == {
        "numStartups": 7,
        "projects": {"/elsewhere": {"hasTrustDialogAccepted": False, "lastCost": 2}, str(folder): TRUSTED},
    }
    assert ensure_worker_folder_trusted(folder, ()) == ""


def test_only_the_real_path_counts_and_is_marked_as_claude_reads_its_start_folder(home: Path) -> None:
    real = home / "code" / "caf\u00e9"
    real.mkdir(parents=True)
    (home / "trusted").mkdir()
    link = home / "trusted" / "link"
    link.symlink_to(real)
    config = _write_claude_json(home, {str(home / "trusted"): TRUSTED})

    assert find_trusting_path(link) is None
    note = ensure_worker_folder_trusted(home / "trusted" / "link", ("~/code",))

    assert note.startswith(f"marked {real} trusted")
    assert json.loads(config.read_text())["projects"] == {str(home / "trusted"): TRUSTED, str(real): TRUSTED}
    assert find_trusting_path(Path(str(home / "code" / "cafe\u0301"))) == str(real)


def test_the_home_folder_is_never_marked_and_an_unreadable_config_refuses(home: Path) -> None:
    config = _write_claude_json(home, {})

    with pytest.raises(UsageError, match="at every start"):
        ensure_worker_folder_trusted(home, ("~",))
    assert json.loads(config.read_text())["projects"] == {}

    config.write_text("{half")
    with pytest.raises(UsageError, match="could not read"):
        ensure_worker_folder_trusted(home / "code", ("~",))


def test_guild_worker_refuses_an_untrusted_folder_and_starts_in_one_under_a_root(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    raw = json.loads(bridge_env.stub_tree.read_text())
    raw["result"]["tree"]["workspaces"][0]["sessions"].append({"id": "TAB-W", "name": "Task", "status": "active"})
    bridge_env.stub_tree.write_text(json.dumps(raw))
    claude_json = Path(bridge_env.env["CLAUDE_CONFIG_DIR"]) / ".claude.json"
    claude_json.write_text("{}")
    clone = bridge_env.root / "projects-root" / "new-clone"
    clone.mkdir(parents=True)
    env = dict(bridge_env.env, CLAUDE_PID=str(team.a), AGTERM_SESSION_ID="TAB-A")
    argv = [str(VENV_BIN / "guild"), "worker", "Task", "--cwd", str(clone), "--prompt", "Fix the thing"]

    refused = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30, check=False)

    assert refused.returncode != 0
    assert f"Claude Code has not trusted {clone} yet" in refused.stderr
    assert not bridge_env.stub_log.exists() or "session new" not in bridge_env.stub_log.read_text()

    config_path = Path(bridge_env.env["BRIDGE_CONFIG"])
    config_path.write_text(f'worker_trust_roots = ["{bridge_env.root / "projects-root"}"]\n' + config_path.read_text())
    started = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30, check=False)

    assert started.returncode == 0, started.stderr
    assert f"marked {clone} trusted for Claude Code" in started.stdout
    assert json.loads(claude_json.read_text())["projects"][str(clone)] == TRUSTED
    assert "session new" in bridge_env.stub_log.read_text()
