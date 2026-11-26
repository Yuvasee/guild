import os
import subprocess
import sys
from pathlib import Path

GATE = Path(__file__).parent.parent / "scripts" / "leak-gate.py"


def check_message(tmp_path: Path, message: str) -> subprocess.CompletedProcess[str]:
    lists = tmp_path / "lists"
    lists.mkdir(exist_ok=True)
    (lists / "denylist.txt").write_text("acmecorp\n")
    file = tmp_path / "COMMIT_EDITMSG"
    file.write_text(message)
    return subprocess.run(
        [sys.executable, "-I", str(GATE), "--commit-msg", str(file)],
        cwd=tmp_path,
        env={**os.environ, "GUILD_LEAK_DIR": str(lists)},
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_commit_message_naming_a_refused_term_is_refused(tmp_path: Path) -> None:
    done = check_message(tmp_path, "Export retries\n\nWhy: AcmeCorp's uploads failed.\n")
    assert done.returncode == 1 and "PRIVATE  commit message:3: 'AcmeCorp'" in done.stdout, done.stdout


def test_a_clean_commit_message_passes_and_gits_comment_lines_are_not_read(tmp_path: Path) -> None:
    done = check_message(tmp_path, "Export retries\n\nWhy: uploads failed.\n# On branch acmecorp-export\n")
    assert done.returncode == 0 and "leak-gate: 0 problem(s)" in done.stdout, done.stdout + done.stderr
