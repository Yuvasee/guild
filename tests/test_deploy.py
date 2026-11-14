import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import BridgeEnv

REPO = Path(__file__).resolve().parent.parent
GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.com"]


@dataclass
class Deploy:
    home: Path
    checkout: Path
    env: dict[str, str]
    launchctl_log: Path

    @property
    def root(self) -> Path:
        return self.home / ".local/share/bridge"

    def run(self, *args: str, check: str = "--help") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.checkout / "scripts/deploy.sh"), *args],
            env={**self.env, "BRIDGE_DEPLOY_CHECK": check},
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )

    def commit(self, message: str) -> None:
        subprocess.run(["git", *GIT_ID, "commit", "-q", "--allow-empty", "-m", message], cwd=self.checkout, check=True)

    def link(self, name: str) -> Path:
        return Path(os.readlink(self.root / name))


@pytest.fixture
def deploy(bridge_env: BridgeEnv, tmp_path: Path) -> Deploy:
    """This repo's HEAD plus the working copy of deploy.sh, over an install laid out as `uv tool install` leaves it,
    with launchctl stubbed: nothing reaches the live install, service or settings."""
    uv = shutil.which("uv")
    assert uv is not None
    # uv under the temp HOME still builds offline from the real cache, with the real managed Pythons.
    uv_dirs = {
        "UV_CACHE_DIR": subprocess.run([uv, "cache", "dir"], capture_output=True, text=True, check=True).stdout.strip(),
        "UV_PYTHON_INSTALL_DIR": subprocess.run(
            [uv, "python", "dir"], capture_output=True, text=True, check=True
        ).stdout.strip(),
    }
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", "-q", str(REPO), str(checkout)], check=True)
    (checkout / "scripts").mkdir(exist_ok=True)
    shutil.copy2(REPO / "scripts/deploy.sh", checkout / "scripts/deploy.sh")
    subprocess.run(["git", "add", "-A"], cwd=checkout, check=True)
    subprocess.run(["git", *GIT_ID, "commit", "-q", "--allow-empty", "-m", "deploy.sh"], cwd=checkout, check=True)

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    stub_bin = Path(bridge_env.env["PATH"].split(os.pathsep)[0])
    log = tmp_path / "launchctl.log"
    (stub_bin / "launchctl").write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n')
    (stub_bin / "launchctl").chmod(0o755)
    # Homebrew's GNU mv sits before /bin on the owner's PATH and has no -h.
    gnu_mv = 'case "$1" in -*h*) echo "mv: invalid option -- h" >&2; exit 1;; esac\nexec /bin/mv "$@"'
    (stub_bin / "mv").write_text(f"#!/bin/sh\n{gnu_mv}\n")
    (stub_bin / "mv").chmod(0o755)
    env = {**bridge_env.env, **uv_dirs, "HOME": str(home), "CLAUDE_PID": str(os.getpid())}
    env["PATH"] = f"{stub_bin}{os.pathsep}{os.path.dirname(uv)}{os.pathsep}/usr/bin:/bin"
    subprocess.run(
        [uv, "tool", "install", "--compile-bytecode", str(checkout)],
        env={**env, "UV_TOOL_DIR": str(home / ".local/share/uv/tools"), "UV_TOOL_BIN_DIR": str(home / ".local/bin")},
        capture_output=True,
        check=True,
    )
    bridge_env.write_session_file(os.getpid(), "deploy-session")
    return Deploy(home=home, checkout=checkout, env=env, launchctl_log=log)


def test_a_first_deploy_moves_the_entry_points_onto_a_release_and_keeps_uvs_venv_to_roll_back_to(
    deploy: Deploy, bridge_env: BridgeEnv
) -> None:
    result = deploy.run()

    assert result.returncode == 0, result.stderr
    release = deploy.link("current")
    assert release.parent.parent == deploy.root / "releases"
    assert deploy.link("previous") == deploy.home / ".local/share/uv/tools/bridge"
    for name in ("bridge", "bridge-hook", "guild"):
        assert os.readlink(deploy.home / ".local/bin" / name) == str(deploy.root / "current/bin" / name)
    # A process started through `current` names its release's own interpreter, so a later swap cannot reach it.
    hook_script = (release / "bin/bridge-hook").read_text()
    assert str(release / "bin/python") in hook_script and "/current/" not in hook_script
    settings = json.loads((deploy.home / ".claude/settings.json").read_text())
    assert str(deploy.home / ".local/bin/bridge-hook") in json.dumps(settings["hooks"])
    assert "kickstart -k" in deploy.launchctl_log.read_text()
    sha = subprocess.run(
        ["git", "rev-parse", "--short=12", "HEAD"], cwd=deploy.checkout, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert (bridge_env.state_dir / f"backups/bridge-pre-{sha}.db").exists()
    assert (bridge_env.state_dir / f"deploys/{sha}-check-before.txt").exists()
    assert (bridge_env.state_dir / f"deploys/{sha}-check-after.txt").exists()
    with bridge_env.connect() as conn:
        assert (
            conn.execute("SELECT body FROM messages WHERE board = 1")
            .fetchone()["body"]
            .startswith(f"Deploying bridge {sha}")
        )


def test_a_second_deploy_installs_the_changed_code_not_uvs_cached_build(deploy: Deploy) -> None:
    assert deploy.run().returncode == 0
    marker = "# changed after the first deploy\n"
    with (deploy.checkout / "src/bridge/db.py").open("a") as source:
        source.write(marker)
    subprocess.run(["git", "add", "-A"], cwd=deploy.checkout, check=True)
    deploy.commit("change db.py")

    assert deploy.run().returncode == 0
    installed = next(deploy.link("current").glob("lib/python*/site-packages/bridge/db.py"))
    assert installed.read_text().endswith(marker)


def test_rollback_swaps_back_to_the_previous_release_and_again_forward(deploy: Deploy) -> None:
    assert deploy.run().returncode == 0
    first = deploy.link("current")
    deploy.commit("second")
    assert deploy.run().returncode == 0
    second = deploy.link("current")
    assert second != first and deploy.link("previous") == first

    back = deploy.run("rollback")

    assert back.returncode == 0, back.stderr
    assert (deploy.link("current"), deploy.link("previous")) == (first, second)
    assert deploy.launchctl_log.read_text().count("kickstart -k") == 3
    assert deploy.run("rollback").returncode == 0
    assert (deploy.link("current"), deploy.link("previous")) == (second, first)


def test_old_releases_are_pruned_to_the_newest_five_but_never_the_current_one(deploy: Deploy) -> None:
    releases = deploy.root / "releases"
    # Named to sort after any real release, so only its being current keeps the one this deploy makes.
    stale = [releases / f"2099010{i}T000000Z-old" for i in range(1, 7)]
    for path in stale:
        (path / "bridge").mkdir(parents=True)

    assert deploy.run().returncode == 0

    kept = {p.name for p in releases.iterdir()}
    assert kept == {p.name for p in stale[1:]} | {deploy.link("current").parent.name}


def test_a_release_a_running_process_was_started_from_is_not_pruned(deploy: Deploy) -> None:
    releases = deploy.root / "releases"
    stale = [releases / f"2099010{i}T000000Z-old" for i in range(1, 7)]
    for path in stale:
        (path / "bridge/bin").mkdir(parents=True)
    # What ps shows for a host tab's loop: the release's own python in its argv.
    loop = subprocess.Popen(["/bin/sh", "-c", "sleep 60; :", str(stale[0] / "bridge/bin/python"), "_host-tab"])
    try:
        assert deploy.run().returncode == 0
    finally:
        loop.kill()
        loop.wait()

    assert stale[0].exists()


def test_a_second_deploy_waits_for_nobody_and_stops_while_one_runs(deploy: Deploy) -> None:
    deploy.root.mkdir(parents=True)
    holder = subprocess.Popen(["lockf", str(deploy.root / "deploy.lock"), "sleep", "60"])
    try:
        time.sleep(0.5)
        result = deploy.run()
    finally:
        holder.kill()
        holder.wait()

    assert result.returncode != 0
    assert "already locked" in result.stderr
    assert not (deploy.root / "current").exists()


def test_an_entry_point_that_is_no_venvs_is_not_taken_as_the_release_to_roll_back_to(deploy: Deploy) -> None:
    entry = deploy.home / ".local/bin/bridge"
    entry.unlink()
    entry.write_text("#!/bin/sh\nexit 0\n")
    entry.chmod(0o755)

    result = deploy.run()

    assert result.returncode == 1
    assert "stopped at 'find the release to roll back to'" in result.stderr
    assert not list((deploy.root / "releases").iterdir())


def test_a_failing_step_stops_the_deploy_and_names_it_before_anything_is_installed(deploy: Deploy) -> None:
    result = deploy.run(check="no-such-command")

    assert result.returncode == 1
    assert "stopped at 'deploy-check before'" in result.stderr
    assert not (deploy.root / "current").exists()
    assert not list((deploy.root / "releases").iterdir())


def test_a_dirty_checkout_is_not_deployed(deploy: Deploy) -> None:
    (deploy.checkout / "stray.txt").write_text("x")

    result = deploy.run()

    assert result.returncode == 1
    assert "stopped at 'check the checkout'" in result.stderr
    assert not (deploy.root / "current").exists()
