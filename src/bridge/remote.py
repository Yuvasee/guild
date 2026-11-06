import json
import re
import shlex
import sqlite3
import subprocess
from dataclasses import dataclass, field
from importlib.resources import files
from typing import cast

from bridge.config import Config, Remote
from bridge.db import transaction
from bridge.refs import extract_refs

SSH_TIMEOUT_S = 45
TMUX_LIST_TIMEOUT_S = 20
REMOTE_STALE_AFTER_MS = 300_000
MISSES_BEFORE_DROP = 2
_HOST_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
_TMUX_ID_RE = re.compile(r"\$\d+")
_GITHUB_RE = re.compile(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


@dataclass(frozen=True)
class Collected:
    ok: bool
    error: str | None
    sessions: list[dict[str, object]]
    remote_now: int | None
    runs: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    """tmux panes running something other than an agent: tmux, tmux_id, pane, label."""


def collect(remote: Remote) -> Collected:
    """Runs the read-only probe over ssh with a hard wall-clock limit and without touching shared ssh masters."""
    if not _HOST_RE.fullmatch(remote.name):
        return Collected(False, f"invalid remote name {remote.name!r}", [], None)
    probe = files("bridge").joinpath("remote_probe.py").read_text()
    command = _map_remote_to_ssh(remote, "python3 -I -S -")
    try:
        done = subprocess.run(command, input=probe, capture_output=True, text=True, timeout=SSH_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Collected(False, f"ssh {type(exc).__name__}", [], None)
    if done.returncode != 0:
        last = (done.stderr.strip().splitlines() or [f"exit {done.returncode}"])[-1][:200]
        return Collected(False, last, [], None)
    try:
        raw: object = json.loads(done.stdout)
    except ValueError:
        return Collected(False, "probe returned invalid JSON", [], None)
    data = cast(dict[str, object], raw) if isinstance(raw, dict) else {}
    sessions = data.get("sessions")
    now = data.get("now")
    if data.get("complete") is not True:
        return Collected(False, str(data.get("error") or "incomplete inventory"), [], None)
    if not isinstance(sessions, list):
        return Collected(False, "probe returned no session list", [], None)
    rows = [cast(dict[str, object], s) for s in cast(list[object], sessions) if isinstance(s, dict)]
    raw_runs = data.get("runs")
    runs = (
        [cast(dict[str, object], r) for r in cast(list[object], raw_runs) if isinstance(r, dict)]
        if isinstance(raw_runs, list)
        else []
    )
    return Collected(True, None, rows, now if isinstance(now, int) else None, runs)


def read_tmux_sessions(remote: Remote | None) -> set[str] | None:
    """Names and ids of tmux sessions there, on this machine without a remote; None when that cannot be known."""
    listing = ["tmux", "list-sessions", "-F", "#{session_name}\t#{session_id}"]
    command = _map_remote_to_ssh(remote, shlex.join(listing)) if remote else listing
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=TMUX_LIST_TIMEOUT_S, check=False)
    except FileNotFoundError:
        return set() if remote is None else None
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode == 0:
        return {part for line in done.stdout.splitlines() for part in line.split("\t") if part}
    # A missing or stale socket means no server; any other failure to connect says nothing about its sessions.
    no_server = any(text in done.stderr for text in ("no server", "No such file or directory", "Connection refused"))
    return set() if done.returncode == 1 and no_server else None


def _map_remote_to_ssh(remote: Remote, remote_command: str) -> list[str]:
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "ControlPath=none",
        "-o", "ForwardAgent=no",
        "-o", "ServerAliveInterval=10",
        "-o", "ServerAliveCountMax=2",
        remote.ssh,
        remote_command,
    ]  # fmt: skip


def store(conn: sqlite3.Connection, remote: Remote, result: Collected, config: Config, at_ms: int) -> None:
    """Only a successful, complete inventory replaces the host's rows; a failure keeps the last data."""
    with transaction(conn):
        if not result.ok:
            conn.execute(
                """
                INSERT INTO remote_hosts (host, ok_at, error, error_since) VALUES (?, NULL, ?, ?)
                ON CONFLICT (host) DO UPDATE SET
                    error = excluded.error, error_since = coalesce(remote_hosts.error_since, excluded.error_since)
                """,
                (remote.name, result.error, at_ms),
            )
            return
        skew = at_ms - result.remote_now if result.remote_now else 0
        previous_rows = {
            str(r["key"]): r for r in conn.execute("SELECT * FROM remote_sessions WHERE host = ?", (remote.name,))
        }
        previous = {key: cast(dict[str, object], json.loads(r["data"])) for key, r in previous_rows.items()}
        previous_refs: dict[str, list[str]] = {}
        for r in conn.execute("SELECT key, ref FROM remote_refs WHERE host = ?", (remote.name,)):
            previous_refs.setdefault(str(r["key"]), []).append(str(r["ref"]))
        conn.execute("DELETE FROM remote_sessions WHERE host = ?", (remote.name,))
        conn.execute("DELETE FROM remote_refs WHERE host = ?", (remote.name,))
        conn.execute("DELETE FROM remote_runs WHERE host = ?", (remote.name,))
        conn.executemany(
            "INSERT OR REPLACE INTO remote_runs (host, tmux, tmux_id, pane, label, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    remote.name,
                    str(r.get("tmux")),
                    str(r.get("tmux_id")),
                    str(r.get("pane")),
                    str(r.get("label") or ""),
                    r.get("started_at"),
                )
                for r in result.runs
            ],
        )
        for session in result.sessions:
            key = f"{session.get('tmux_id')}:{session.get('pane')}:{session.get('pid')}:{session.get('started_at')}"
            before = previous.pop(key, {})
            same_conversation = session.get("conversation_id") in (None, before.get("conversation_id"))
            for field in ("prompt", "reply", "conversation_id"):
                # An unreadable transcript this round must not erase what we knew about the same conversation.
                if same_conversation and not session.get(field) and before.get(field):
                    session[field] = before[field]
            activity = session.get("activity_at")
            local_activity = int(activity) + skew if isinstance(activity, int) else None
            conn.execute(
                "INSERT INTO remote_sessions (host, key, data, observed_at, activity_at) VALUES (?, ?, ?, ?, ?)",
                (remote.name, key, json.dumps(session), at_ms, local_activity),
            )
            repo = map_url_to_repo(session.get("origin"))
            for field in ("prompt", "reply"):
                text = session.get(field)
                if isinstance(text, str):
                    refs = extract_refs(
                        text, issue_prefixes=config.issue_prefixes, default_repo=config.pr_repo_for(repo)
                    )
                    conn.executemany(
                        "INSERT OR IGNORE INTO remote_refs (host, key, ref) VALUES (?, ?, ?)",
                        [(remote.name, key, ref) for ref in refs],
                    )
        for key, before in previous.items():
            # A process can vanish from one probe by a race; drop it only after it is missing twice.
            misses = int(str(before.get("missing", 0))) + 1
            if misses < MISSES_BEFORE_DROP:
                old = previous_rows[key]
                conn.execute(
                    "INSERT INTO remote_sessions (host, key, data, observed_at, activity_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        remote.name,
                        key,
                        json.dumps({**before, "missing": misses, "state": "unknown"}),
                        old["observed_at"],
                        old["activity_at"],
                    ),
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO remote_refs (host, key, ref) VALUES (?, ?, ?)",
                    [(remote.name, key, ref) for ref in previous_refs.get(key, [])],
                )
        conn.execute(
            """
            INSERT INTO remote_hosts (host, ok_at, error, error_since) VALUES (?, ?, NULL, NULL)
            ON CONFLICT (host) DO UPDATE SET ok_at = excluded.ok_at, error = NULL, error_since = NULL
            """,
            (remote.name, at_ms),
        )


def list_remote_sessions(conn: sqlite3.Connection, at_ms: int) -> list[dict[str, object]]:
    """Remote sessions in the same shape the dashboard uses for local ones."""
    hosts = {str(r["host"]): r for r in conn.execute("SELECT * FROM remote_hosts")}
    refs: dict[tuple[str, str], list[str]] = {}
    for r in conn.execute("SELECT host, key, ref FROM remote_refs ORDER BY ref"):
        refs.setdefault((str(r["host"]), str(r["key"])), []).append(str(r["ref"]))
    result: list[dict[str, object]] = []
    for row in conn.execute("SELECT * FROM remote_sessions ORDER BY host, key"):
        host, key = str(row["host"]), str(row["key"])
        data = cast(dict[str, object], json.loads(row["data"]))
        host_row = hosts.get(host)
        host_ok = host_row["ok_at"] if host_row else None
        stale = host_ok is None or at_ms - int(host_ok) > REMOTE_STALE_AFTER_MS
        state = str(data.get("state") or "unknown")
        status = "stale" if stale or state == "unknown" else ("active" if state == "working" else "idle")
        tmux = str(data.get("tmux") or "")
        result.append(
            {
                "instance_id": f"{host}:{key}",
                "conversation_id": data.get("conversation_id") or f"{host}:{key}",
                "handle": f"{host}/{tmux}",
                "tab_name": tmux,
                "workspace_name": host,
                "status": status,
                "attention": None,
                "pid": data.get("pid"),
                "cwd": data.get("cwd"),
                "repo": map_url_to_repo(data.get("origin")),
                "branch": data.get("branch"),
                "focus": None,
                "last_prompt": data.get("prompt"),
                "last_reply": data.get("reply"),
                "last_activity_at": row["activity_at"],
                "refs": refs.get((host, key), []),
                "surface": None,
                "agent": data.get("agent"),
                "remote": {
                    "host": host,
                    "tmux_id": data.get("tmux_id"),
                    "error": host_row["error"] if host_row else None,
                    "observed_at": row["observed_at"],
                },
            }
        )
    return result


def attach_command(remote: Remote, tmux_id: str) -> str:
    """A local shell line that attaches to a remote tmux session; both shell layers are quoted."""
    if not _TMUX_ID_RE.fullmatch(tmux_id):
        raise ValueError("invalid tmux session id")
    remote_line = shlex.join(["tmux", "attach", "-t", tmux_id])
    return shlex.join(["ssh", "-t", remote.ssh, remote_line])


def map_url_to_repo(url: object) -> str | None:
    if not isinstance(url, str):
        return None
    match = _GITHUB_RE.search(url)
    return f"{match.group(1)}/{match.group(2)}" if match else None
