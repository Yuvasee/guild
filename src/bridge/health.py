import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

HOOKS_SILENT_MS = 5 * 60_000
"""Tool hooks run at least every 30 s of activity and Stop ends every turn, so a longer gap means they are gone."""
RECENT_TURN_MS = 3_600_000
"""Older activity says nothing about whether hooks work now."""
_TAIL_STEPS = (256 * 1024, 4 * 1024 * 1024)
_LOCAL_COMMAND_PREFIXES = ("<local-command", "<command-name>")
_turn_cache: dict[str, tuple[int, int | None]] = {}


@dataclass(frozen=True)
class HookHealth:
    last_hook_at: int | None
    last_turn_at: int | None
    """The newest user or assistant entry in any live local session's transcript."""
    silent: bool


def check_hooks(conn: sqlite3.Connection, at_ms: int) -> HookHealth:
    """Sessions keep talking but no hook reaches bridge: the hooks were removed or disabled in Claude Code."""
    row = conn.execute("SELECT at FROM hook_runs ORDER BY id DESC LIMIT 1").fetchone()
    last_hook_at = int(row["at"]) if row and row["at"] is not None else None
    paths = [
        str(r["transcript_path"])
        for r in conn.execute(
            """
            SELECT c.transcript_path FROM instances i
            JOIN conversations c ON c.conversation_id = i.conversation_id
            WHERE i.ended_at IS NULL AND c.transcript_path IS NOT NULL
            """
        )
    ]
    for stale in set(_turn_cache) - set(paths):
        del _turn_cache[stale]
    turns = [t for t in (read_last_turn_at(Path(p)) for p in paths) if t is not None]
    last_turn_at = max(turns, default=None)
    silent = (
        last_turn_at is not None
        and at_ms - last_turn_at < RECENT_TURN_MS
        and last_turn_at - (last_hook_at or 0) > HOOKS_SILENT_MS
    )
    return HookHealth(last_hook_at, last_turn_at, silent)


def read_last_turn_at(path: Path) -> int | None:
    """Time of the newest user or assistant turn; other entries (titles, summaries, local commands) skip hooks."""
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return None
    cached = _turn_cache.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    result = None
    for tail in _TAIL_STEPS:
        result = _scan_tail_for_turn(path, tail)
        if result is not None:
            break
    _turn_cache[str(path)] = (mtime, result)
    return result


def _scan_tail_for_turn(path: Path, tail: int) -> int | None:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            start = max(0, f.tell() - tail)
            f.seek(start)
            lines = f.read().split(b"\n")
    except OSError:
        return None
    for raw in reversed(lines[1:] if start > 0 else lines):
        if b'"type":"assistant"' not in raw and b'"type":"user"' not in raw:
            continue
        try:
            entry: object = json.loads(raw)
        except ValueError:
            continue
        data = cast(dict[str, object], entry) if isinstance(entry, dict) else {}
        stamp = data.get("timestamp")
        if data.get("type") not in ("user", "assistant") or data.get("isSidechain") or data.get("isMeta"):
            continue
        message = data.get("message")
        content = cast(dict[str, object], message).get("content") if isinstance(message, dict) else None
        if isinstance(content, str) and content.lstrip().startswith(_LOCAL_COMMAND_PREFIXES):
            continue
        if not isinstance(stamp, str):
            continue
        try:
            return int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            continue
    return None
