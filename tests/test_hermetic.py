from pathlib import Path

from conftest import LIVE_DIRS, REAL_HOME, find_live_path

from bridge.config import config_path, state_dir

LIVE_LOG = f"{REAL_HOME}/.local/state/bridge/errors.log"


def test_a_test_runs_on_a_state_dir_of_its_own_and_no_config() -> None:
    assert str(state_dir()) not in LIVE_DIRS
    assert not config_path().exists()


def test_the_guard_sees_the_live_dirs_reached_in_process(tmp_path: Path) -> None:
    assert find_live_path("open", (LIVE_LOG, "a", 0)) == LIVE_LOG
    assert find_live_path("os.rename", (str(tmp_path / "x"), Path(LIVE_LOG), None, None))
    assert find_live_path("sqlite3.connect", (f"{REAL_HOME}/.local/state/bridge/bridge.db",))
    assert find_live_path("open", (f"{REAL_HOME}/.config/bridge/config.toml", "r", 0))
    assert find_live_path("open", (str(tmp_path / "errors.log"), "a", 0)) is None
    assert find_live_path("open", (f"{REAL_HOME}/.local/state/bridge-other/errors.log", "a", 0)) is None
    assert find_live_path("open", (3, "r", 0)) is None


def test_the_guard_sees_a_child_that_would_fall_back_to_the_live_dirs(tmp_path: Path) -> None:
    own = {"BRIDGE_STATE_DIR": str(tmp_path), "BRIDGE_CONFIG": str(tmp_path / "config.toml")}

    assert find_live_path("subprocess.Popen", ("sh", ["sh"], None, {"PATH": "/bin"}))
    assert find_live_path("subprocess.Popen", ("sh", ["sh"], None, {**own, "BRIDGE_STATE_DIR": ""}))
    assert find_live_path("subprocess.Popen", ("sh", ["sh"], None, own)) is None
    assert find_live_path("subprocess.Popen", ("sh", ["sh"], None, {"HOME": str(tmp_path)})) is None
    assert find_live_path("subprocess.Popen", ("sh", ["sh"], None, None)) is None, "inherits this test's own dirs"
