import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from bridge.config import Watcher

MAX_FILE_BYTES = 64 * 1024
DEFAULT_STALE_AFTER_MIN = 20
SUMMARY_CHARS = 200
DETAIL_CHARS = 2000


def read_watchers(watchers: tuple[Watcher, ...], now: int) -> list[dict[str, object]]:
    return [read_watcher(w, now) for w in watchers]


def read_watcher(watcher: Watcher, now: int) -> dict[str, object]:
    """One watcher's line for the page; only the fields below ever leave the status file."""
    shown = {"name": watcher.name or watcher.path, "path": watcher.path}
    path = Path(os.path.expanduser(watcher.path))
    try:
        # Nonblocking, so a FIFO put at the path cannot hang the snapshot before fstat turns it away.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return {**shown, "state": "unreadable", "why": "status path is not a regular file"}
            raw = _read_up_to(fd, MAX_FILE_BYTES + 1)
        finally:
            os.close(fd)
    except FileNotFoundError:
        return {**shown, "state": "missing", "why": "no status file"}
    except OSError as exc:
        return {**shown, "state": "unreadable", "why": f"status file unreadable: {exc.strerror or exc}"}
    if len(raw) > MAX_FILE_BYTES:
        return {**shown, "state": "unreadable", "why": f"status file is over {MAX_FILE_BYTES // 1024} KB"}
    try:
        status = map_json_to_status(json.loads(raw))
    except (ValueError, RecursionError) as exc:
        return {**shown, "state": "unreadable", "why": f"status file unreadable: {exc}"[:SUMMARY_CHARS]}
    name = status["name"] or shown["name"]
    last_run_at = cast(int, status["last_run_at"])
    stale_after_min = cast(int, status["stale_after_min"])
    if now - last_run_at > stale_after_min * 60_000:
        state = "stale"
    elif status["status"] == "failed":
        state = "failed"
    else:
        state = "ok"
    return {**shown, **status, "name": name, "state": state}


def _read_up_to(fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while size < limit and (chunk := os.read(fd, limit - size)):
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def map_json_to_status(data: object) -> dict[str, object]:
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    fields = cast(dict[str, object], data)
    status = fields.get("status")
    if status not in ("ok", "failed"):
        raise ValueError('status is neither "ok" nor "failed"')
    last_run = fields.get("last_run_utc")
    if not isinstance(last_run, str):
        raise ValueError("no last_run_utc")
    try:
        when = datetime.fromisoformat(last_run)
    except ValueError:
        raise ValueError("last_run_utc is not ISO 8601") from None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    stale_after = fields.get("stale_after_min", DEFAULT_STALE_AFTER_MIN)
    if not isinstance(stale_after, int) or isinstance(stale_after, bool) or stale_after <= 0:
        stale_after = DEFAULT_STALE_AFTER_MIN
    name, summary, detail = fields.get("name"), fields.get("summary"), fields.get("detail")
    return {
        "name": name.strip()[:SUMMARY_CHARS] if isinstance(name, str) else "",
        "status": status,
        "last_run_at": int(when.timestamp() * 1000),
        "stale_after_min": stale_after,
        "summary": summary.strip()[:SUMMARY_CHARS] if isinstance(summary, str) else "",
        "detail": detail.strip()[:DETAIL_CHARS] if isinstance(detail, str) else "",
    }
