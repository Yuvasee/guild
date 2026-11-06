import os
import plistlib
import shutil
import subprocess
from pathlib import Path

from bridge.config import state_dir

LABEL = "dev.bridge.serve"


def plist_path() -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def install_service(port: int) -> Path:
    """Runs the dashboard (and its maintenance and enrichment loops) under launchd, restarting it if it dies."""
    executable = shutil.which("bridge")
    if executable is None:
        raise RuntimeError("bridge is not on PATH; install it with `uv tool install` first")
    log = state_dir() / "serve.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    # launchd starts with a bare PATH; gh and agtermctl live in the user's usual places.
    path = os.pathsep.join(
        dict.fromkeys(
            [
                str(Path(executable).parent),
                "/opt/homebrew/bin",
                "/usr/local/bin",
                "/usr/bin",
                "/bin",
                "/usr/sbin",
                "/sbin",
                *os.environ.get("PATH", "").split(os.pathsep),
            ]
        )
    )
    plist = {
        "Label": LABEL,
        "ProgramArguments": [executable, "serve", "--port", str(port)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "EnvironmentVariables": {"PATH": path, "HOME": str(Path.home())},
    }
    target = plist_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    _bootout()
    target.write_bytes(plistlib.dumps(plist))
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)], check=True, capture_output=True)
    return target


def uninstall_service() -> None:
    _bootout()
    plist_path().unlink(missing_ok=True)


def restart_service() -> None:
    subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LABEL}"], check=True, capture_output=True)


def _bootout() -> None:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, check=False)
