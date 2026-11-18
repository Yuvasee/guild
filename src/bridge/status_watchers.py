import json
import os
import sqlite3
import stat
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from bridge.asks import send_from_bridge
from bridge.board import Recipient
from bridge.config import Config, Watcher

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


def find_owner_needs(watchers: tuple[Watcher, ...], now: int) -> list[dict[str, object]]:
    """A failed watcher whose job says only the owner's hands can fix it is a Needs-you card while it reads so."""
    return [
        {"type": "watcher", "path": shown["path"], "text": f"{shown['name']}: {shown['summary'] or 'failed'}"}
        for shown in read_watchers(watchers, now)
        if shown["state"] == "failed" and shown.get("needs_owner")
    ]


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
        "needs_owner": fields.get("needs_owner") is True,
    }


STALE_GRACE_MS = 10 * 60_000
SLEPT_GAP_MS = 2 * 60_000
"""A longer pause between two alert rounds means the Mac slept (rounds run every 30 s)."""
_awake: dict[str, int] = {}
"""When this process last ran a round, and since when it has run without a sleep-sized gap."""


def note_awake(now: int) -> int:
    """Since when the Mac has been awake without a gap: a watcher's job gets its turn only once the Mac is up."""
    last = _awake.get("last")
    if last is None or now - last > SLEPT_GAP_MS:
        _awake["since"] = now
    _awake["last"] = now
    return _awake["since"]


def alert_watchers(conn: sqlite3.Connection, config: Config, now: int) -> list[tuple[int, Recipient]]:
    """Once per spell away from ok: watcher_alert_to hears it, the assistant hears if it outlasts
    watcher_escalate_after_min, and everyone told hears when it is back. signal_posts keeps the spell, as serve
    restarts with most deploys. A message that reached nobody is not recorded, so the next round tries again."""
    # Off, every spell is forgotten: one left open would silence the next spell once alerts are back on.
    paths = {w.path for w in config.watchers} if config.watcher_alert_to else set[str]()
    for (key,) in conn.execute("SELECT key FROM signal_posts WHERE substr(key, 1, 8) = 'watcher:'").fetchall():
        if str(key).split("\n", 1)[0].removeprefix("watcher:") not in paths:
            conn.execute("DELETE FROM signal_posts WHERE key = ?", (key,))
    if not config.watcher_alert_to:
        return []
    sent: list[tuple[int, Recipient]] = []
    awake_since = note_awake(now)
    for watcher in config.watchers:
        shown = read_watcher(watcher, now)
        name, state = str(shown["name"]), str(shown["state"])
        spell = f"watcher:{watcher.path}"
        row = conn.execute("SELECT posted_at FROM signal_posts WHERE key = ?", (spell,)).fetchone()
        told = {
            str(k).rsplit("\n", 1)[1]: int(at)
            for k, at in conn.execute(
                "SELECT key, posted_at FROM signal_posts WHERE substr(key, 1, ?) = ?", (len(spell) + 1, spell + "\n")
            )
        }
        if state == "ok":
            if row is None:
                continue
            for handle in sorted({k.split(" ", 1)[1] for k in told}):
                text = f"{name} watch is back to ok (not ok since {_clock(int(row[0]))})."
                sent += send_from_bridge(conn, config, handle, text, now)
            conn.execute("DELETE FROM signal_posts WHERE key = ? OR substr(key, 1, ?) = ?",
                         (spell, len(spell) + 1, spell + "\n"))  # fmt: skip
            continue
        if row is None:
            # message_id 0: the spell's start is a mark, not a message.
            conn.execute("INSERT INTO signal_posts (key, message_id, posted_at) VALUES (?, 0, ?)", (spell, now))
            since = now
        else:
            since = int(row[0])
        summary = _summarize(shown)
        alert_key = f"alert {config.watcher_alert_to}"
        # After the Mac sleeps, every watcher reads stale until its job's next run: wait one grace of the Mac awake.
        if state == "stale" and alert_key not in told and now - max(since, awake_since) < STALE_GRACE_MS:
            continue
        if alert_key not in told:
            detail = f" {shown['detail']}" if state == "failed" and shown.get("detail") else ""
            text = f"{name} watch: {state}: {summary} (since {_clock(since)}).{detail}"
            alerted = _send_once(conn, config, f"{spell}\n{alert_key}", config.watcher_alert_to, text, now)
            if alerted:
                told[alert_key] = now
            sent += alerted
        escalation_key = f"escalation {config.assistant_handle}"
        if escalation_key not in told and now - since >= config.watcher_escalate_after_min * 60_000:
            who = (
                f"{config.watcher_alert_to} was told at {_clock(told[alert_key])}."
                if alert_key in told
                else f"{config.watcher_alert_to} could not be reached."
            )
            text = (
                f"{name} watch is still not ok after {(now - since) // 60_000} min: {state}: {summary} "
                f"(since {_clock(since)}). {who}"
            )
            sent += _send_once(conn, config, f"{spell}\n{escalation_key}", config.assistant_handle, text, now)
    return sent


def _send_once(
    conn: sqlite3.Connection, config: Config, key: str, to: str, text: str, now: int
) -> list[tuple[int, Recipient]]:
    sent = send_from_bridge(conn, config, to, text, now)
    if sent:
        conn.execute("INSERT INTO signal_posts (key, message_id, posted_at) VALUES (?, ?, ?)", (key, sent[0][0], now))
    return sent


def _summarize(shown: dict[str, object]) -> str:
    if shown["state"] == "stale":
        last_run = _clock(cast(int, shown["last_run_at"]))
        return f"last run {last_run}, over its {shown['stale_after_min']}-min limit"
    return str(shown.get("why") or shown.get("summary") or shown["state"])


def _clock(at_ms: int) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(at_ms / 1000))
