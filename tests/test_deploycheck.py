import subprocess
import sys
from pathlib import Path

from bridge.deploycheck import end_check_claude

SLEEP = "import time; time.sleep(30)"


def test_the_checks_claude_is_ended_and_no_other_process(tmp_path: Path) -> None:
    settings = str(tmp_path / "settings.json")
    ours = subprocess.Popen([sys.executable, "-c", SLEEP, settings])
    other = subprocess.Popen([sys.executable, "-c", SLEEP, "other"])
    try:
        (tmp_path / "ours.pid").write_text(f"{ours.pid}\n")
        (tmp_path / "other.pid").write_text(f"{other.pid}\n")
        end_check_claude(tmp_path / "other.pid", settings)
        end_check_claude(tmp_path / "ours.pid", settings)
        end_check_claude(tmp_path / "missing.pid", settings)
        assert ours.wait(timeout=5) != 0
        assert other.poll() is None
    finally:
        for process in (ours, other):
            process.kill()
            process.wait()
