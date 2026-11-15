import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts/backup.sh"
AGE = shutil.which("age") or ""
AGE_KEYGEN = shutil.which("age-keygen") or ""

pytestmark = pytest.mark.skipif(not (AGE and AGE_KEYGEN), reason="needs age and age-keygen")


@dataclass
class Backup:
    home: Path
    tmp: Path
    identity: str
    env: dict[str, str]
    launchctl_log: Path

    @property
    def backups(self) -> Path:
        return self.home / ".guild/backups"

    def run(self, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/bash", str(SCRIPT), *args],
            env={**self.env, **extra},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def decrypt(self, archive: Path, into: Path) -> list[str]:
        key = self.tmp.parent / "key.txt"
        key.write_text(self.identity + "\n")
        into.mkdir()
        plain = subprocess.run([AGE, "-d", "-i", str(key), str(archive)], capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", str(into)], input=plain, check=True)
        names = subprocess.run(["tar", "-t"], input=plain, capture_output=True, check=True).stdout.decode()
        return names.split()


def today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


@pytest.fixture
def backup(tmp_path: Path) -> Backup:
    home = tmp_path / "home"
    guild = home / ".guild"
    (guild / "people").mkdir(parents=True)
    (guild / "people/someone.md").write_text("notes\n")
    (guild / "backups").mkdir()
    (guild / "backups/backup.log").write_text("old log\n")
    key = subprocess.run([AGE_KEYGEN], capture_output=True, text=True, check=True).stdout
    identity = next(line for line in key.splitlines() if line.startswith("AGE-SECRET-KEY-"))
    public = subprocess.run([AGE_KEYGEN, "-y"], input=identity, capture_output=True, text=True, check=True).stdout
    (guild / "backup-recipient.txt").write_text(public)
    state = tmp_path / "state"
    state.mkdir()
    db = sqlite3.connect(state / "bridge.db")
    db.execute("create table t (v text)")
    db.execute("insert into t values ('kept')")
    db.commit()
    db.close()
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    log = tmp_path / "launchctl.log"
    (stub_bin / "launchctl").write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n')
    (stub_bin / "launchctl").chmod(0o755)
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    env = {
        "HOME": str(home),
        "USER": "tester",
        "TMPDIR": str(tmp),
        "BRIDGE_STATE_DIR": str(state),
        "PATH": f"{stub_bin}{os.pathsep}{os.path.dirname(AGE)}{os.pathsep}/usr/bin:/bin:/usr/sbin:/sbin",
    }
    return Backup(home=home, tmp=tmp, identity=identity, env=env, launchctl_log=log)


def test_backup_holds_guild_and_db_and_says_once_there_is_no_copy_folder(backup: Backup, tmp_path: Path) -> None:
    result = backup.run()

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("no copy folder configured") == 1
    archives = sorted(p.name for p in backup.backups.glob("*.tar.age"))
    assert archives == [f"{today()}.tar.age"]
    assert (backup.backups / archives[0]).stat().st_mode & 0o077 == 0
    out = tmp_path / "out"
    names = backup.decrypt(backup.backups / archives[0], out)
    assert ".guild/people/someone.md" in names
    assert ".guild/backup-recipient.txt" in names
    assert "bridge.db" in names
    assert not [n for n in names if n.startswith(".guild/backups")]
    assert sqlite3.connect(out / "bridge.db").execute("select v from t").fetchall() == [("kept",)]
    assert list(backup.tmp.iterdir()) == []


def test_backup_keeps_the_newest_14_here_and_in_the_copy_folder(backup: Backup, tmp_path: Path) -> None:
    copy = tmp_path / "Cloud Docs/guild-backup"
    copy.mkdir(parents=True)
    old = [f"2020-01-{day:02d}.tar.age" for day in range(1, 21)]
    for name in old:
        (backup.backups / name).write_text("x")
        (copy / name).write_text("x")
    (copy / "unrelated.txt").write_text("keep me")

    result = backup.run(GUILD_BACKUP_COPY_DIR=str(copy))

    assert result.returncode == 0, result.stderr
    expected = [*old[-13:], f"{today()}.tar.age"]
    assert sorted(p.name for p in backup.backups.glob("*.tar.age")) == expected
    assert sorted(p.name for p in copy.glob("*.tar.age")) == expected
    assert (copy / "unrelated.txt").exists()
    assert (backup.backups / "backup.log").exists()
    assert (copy / f"{today()}.tar.age").read_bytes() == (backup.backups / f"{today()}.tar.age").read_bytes()
    assert "no copy folder" not in result.stdout


def test_copy_folder_from_the_guild_file_expands_the_home_tilde(backup: Backup) -> None:
    (backup.home / ".guild/backup-copy-dir.txt").write_text("# synced by iCloud\n~/Cloud Docs/guild-backup\n")

    result = backup.run()

    assert result.returncode == 0, result.stderr
    assert (backup.home / "Cloud Docs/guild-backup" / f"{today()}.tar.age").exists()


def test_backup_without_a_recipient_fails_and_leaves_no_archive(backup: Backup) -> None:
    (backup.home / ".guild/backup-recipient.txt").unlink()

    result = backup.run()

    assert result.returncode == 1
    assert "backup-recipient.txt" in result.stderr
    assert list(backup.backups.glob("*.tar.age*")) == []


def test_restore_test_decrypts_checks_and_removes_the_plaintext(backup: Backup) -> None:
    assert backup.run().returncode == 0

    result = backup.run("restore-test", GUILD_BACKUP_TEST_IDENTITY=backup.identity)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith(f"restore-test: ok ({today()}.tar.age)")
    assert ".guild/people/someone.md" in result.stdout
    assert list(backup.tmp.iterdir()) == []


def test_restore_test_fails_with_the_wrong_identity_and_still_removes_its_temp_files(backup: Backup) -> None:
    assert backup.run().returncode == 0
    other = subprocess.run([AGE_KEYGEN], capture_output=True, text=True, check=True).stdout
    wrong = next(line for line in other.splitlines() if line.startswith("AGE-SECRET-KEY-"))

    result = backup.run("restore-test", GUILD_BACKUP_TEST_IDENTITY=wrong)

    assert result.returncode == 1
    assert "restore-test: fail at decrypt" in result.stderr
    assert list(backup.tmp.iterdir()) == []


def test_restore_test_without_a_backup_fails(backup: Backup) -> None:
    result = backup.run("restore-test", GUILD_BACKUP_TEST_IDENTITY=backup.identity)

    assert result.returncode == 1
    assert "restore-test: fail: no backup" in result.stderr


def test_install_writes_a_daily_agent_and_uninstall_removes_it(backup: Backup) -> None:
    plist = backup.home / "Library/LaunchAgents/dev.guild.backup.plist"

    result = backup.run("install", GUILD_BACKUP_COPY_DIR="/x/Mobile Documents/guild-backup")

    assert result.returncode == 0, result.stderr
    subprocess.run(["plutil", "-lint", str(plist)], check=True, capture_output=True)
    text = plist.read_text()
    for part in (
        "<string>dev.guild.backup</string>",
        f"<string>{SCRIPT}</string>",
        "<key>Hour</key><integer>3</integer><key>Minute</key><integer>30</integer>",
        f"<string>{backup.env['BRIDGE_STATE_DIR']}</string>",
        "<string>/x/Mobile Documents/guild-backup</string>",
        os.path.dirname(AGE),
    ):
        assert part in text
    uid = os.getuid()
    assert f"bootstrap gui/{uid} {plist}" in backup.launchctl_log.read_text()

    result = backup.run("uninstall")

    assert result.returncode == 0, result.stderr
    assert not plist.exists()
    assert backup.launchctl_log.read_text().splitlines()[-1] == f"bootout gui/{uid}/dev.guild.backup"
