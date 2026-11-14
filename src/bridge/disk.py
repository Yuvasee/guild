import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

from bridge.asks import send_from_bridge
from bridge.board import Recipient
from bridge.capacity import find_assistant_unreachable
from bridge.config import Config

DISK_EVERY_S = 3600
LOW_FREE_BYTES = 40 * 10**9
HIGH_USED_PCT = 90
DU_TIMEOUT_S = 300
DOCKER_TIMEOUT_S = 10
LOW_KEY = "disk-low"
"""Its signal_posts row stands while the assistant has been told and space has not come back."""


@dataclass(frozen=True)
class DiskReading:
    at: int
    total: int
    free: int
    folders: tuple[tuple[str, int | None], ...] = ()
    """(folder, bytes); None: du did not finish in time. A folder that does not exist is left out."""
    docker: str | None = None
    """What `docker system df` says, or None when docker did not answer quickly."""
    measured: bool = False
    """False until the folders and docker have been measured after this reading."""

    @property
    def used_pct(self) -> int:
        # On APFS, / is a small system volume sharing its container: what is not free is used by some volume.
        return round(100 * (self.total - self.free) / self.total) if self.total else 0

    @property
    def low(self) -> bool:
        return self.free < LOW_FREE_BYTES or (self.total - self.free) * 100 > HIGH_USED_PCT * self.total


def read_disk(now: int) -> DiskReading:
    usage = shutil.disk_usage("/")
    return DiskReading(now, usage.total, usage.free)


def measure_disk(reading: DiskReading, folders_to_measure: tuple[str, ...]) -> DiskReading:
    folders = tuple(
        (folder, measure_folder(path)) for folder in folders_to_measure if (path := Path(folder).expanduser()).is_dir()
    )
    return replace(reading, folders=folders, docker=read_docker(), measured=True)


def measure_folder(path: Path) -> int | None:
    try:
        done = subprocess.run(
            ["du", "-sk", str(path)], capture_output=True, text=True, timeout=DU_TIMEOUT_S, stdin=subprocess.DEVNULL
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    # du exits 1 on any unreadable file inside, yet still prints the total of what it could read.
    first = done.stdout.split("\t", 1)[0]
    return int(first) * 1024 if first.isdigit() else None


def read_docker() -> str | None:
    try:
        done = subprocess.run(
            ["docker", "system", "df", "--format", "{{.Type}}\t{{.Size}}"],
            capture_output=True,
            text=True,
            timeout=DOCKER_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if done.returncode != 0:
        return None
    return convert_docker_df_to_text(done.stdout)


def convert_docker_df_to_text(output: str) -> str | None:
    parts: list[str] = []
    for line in output.splitlines():
        kind, _, size = line.partition("\t")
        if size and not size.startswith("0B"):
            parts.append(f"{kind.lower()} {size}")
    return ", ".join(parts) if parts else "nothing stored"


def map_reading_to_dict(reading: DiskReading) -> dict[str, object]:
    return {
        "at": reading.at,
        "free": reading.free,
        "total": reading.total,
        "used_pct": reading.used_pct,
        "low": reading.low,
        "folders": [{"path": path, "bytes": size} for path, size in reading.folders] if reading.measured else None,
        "docker": reading.docker,
    }


def send_disk_notice(conn: sqlite3.Connection, config: Config, reading: DiskReading) -> list[tuple[int, Recipient]]:
    """Once per crossing below the line: a notice that reached nobody is not recorded, so the next hour tries again."""
    told = conn.execute("SELECT 1 FROM signal_posts WHERE key = ?", (LOW_KEY,)).fetchone() is not None
    if not reading.low:
        if told:
            conn.execute("DELETE FROM signal_posts WHERE key = ?", (LOW_KEY,))
        return []
    if told or find_assistant_unreachable(conn, config):
        return []
    sent = send_from_bridge(conn, config, config.assistant_handle, convert_reading_to_notice(reading), reading.at)
    if sent:
        conn.execute(
            "INSERT INTO signal_posts (key, message_id, posted_at) VALUES (?, ?, ?)", (LOW_KEY, sent[0][0], reading.at)
        )
    return sent


def convert_reading_to_notice(reading: DiskReading) -> str:
    lines = [
        f"Disk space on this Mac is low: {convert_bytes_to_gb(reading.free)} free of "
        f"{convert_bytes_to_gb(reading.total)} ({reading.used_pct}% used; the line is {LOW_FREE_BYTES // 10**9} GB "
        f"free or {HIGH_USED_PCT}% used). Biggest known folders:"
    ]
    lines += [
        f"- {path}: {convert_bytes_to_gb(size) if size is not None else 'du timed out'}"
        for path, size in reading.folders
    ]
    lines.append(f"- Docker: {reading.docker}" if reading.docker else "- Docker: did not answer")
    lines.append("You hear this once until space is back above the line.")
    return "\n".join(lines)


def convert_bytes_to_gb(size: int) -> str:
    return f"{size / 10**9:.0f} GB" if size >= 10**9 else f"{size / 10**9:.1f} GB"
