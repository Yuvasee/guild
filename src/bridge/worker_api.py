"""The HTTP API that host workers' gbridge reaches through their tab's ssh tunnel; each call acts as that worker."""

import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

from bridge.config import Config, db_path
from bridge.db import HOOK_BUSY_TIMEOUT_MS, connect
from bridge.errlog import log_error
from bridge.gitinfo import GitInfo, RepoFacts
from bridge.guard import HostCheckout, make_host_repo_reader
from bridge.hooks import HOOK_EVENTS, HookCaller, handle_hook, handle_pre_tool_use
from bridge.host_workers import (
    HOST_KIND_PREFIX,
    HostWorker,
    authenticate,
    bind_conversation,
    is_tab_loop_running,
    list_readable_roots,
)
from bridge.install import list_guard_globs
from bridge.registry import Surface

WORKER_API_PORT = 8766
"""Local port of the worker API that host workers reach through their ssh tunnel; never the dashboard's."""
MAX_BODY_BYTES = 512 * 1024
CLI_TIMEOUT_S = 50
WORKER_COMMANDS = frozenset({"send", "reply", "ack", "done", "decline", "focus", "keep", "inbox", "show"})
_BRIDGE_COMMAND_RE = re.compile(r"(?<![\w/.-])bridge (?=(?:" + "|".join(sorted(WORKER_COMMANDS)) + r")\b)")


def start_worker_api(config: Config, port: int = WORKER_API_PORT) -> ThreadingHTTPServer:
    handler = type("WorkerApiHandler", (WorkerApiHandler,), {"config": config})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def map_record_to_caller(record: HostWorker, git: object) -> HookCaller | None:
    """None while the tab's loop is not running: then nothing on this Mac stands for the worker."""
    if not record.pane or not is_tab_loop_running(record):
        return None
    return HookCaller(
        pid=record.loop_pid,
        proc_start_ms=record.loop_start_ms,
        surface=Surface(record.pane, record.agterm_session or None, record.agterm_workspace or None),
        kind=f"{HOST_KIND_PREFIX}{record.host}",
        git=map_json_to_git(git),
    )


def map_json_to_git(raw: object) -> GitInfo | None:
    if not isinstance(raw, dict):
        return None
    data = cast(dict[str, object], raw)
    root, branch, repo = data.get("repo_root"), data.get("branch"), data.get("repo")
    if not isinstance(root, str) or not root:
        return None
    return GitInfo(
        repo_root=root, branch=branch if isinstance(branch, str) else None, repo=repo if isinstance(repo, str) else None
    )


def map_json_to_repo_facts(raw: object) -> RepoFacts | None:
    git = map_json_to_git(raw)
    if git is None:
        return None
    data = cast(dict[str, object], raw)
    remotes, tracking, push_default = data.get("remotes"), data.get("tracking"), data.get("push_default")
    return RepoFacts(
        repo_root=git.repo_root,
        branch=git.branch,
        remotes={str(k): v if isinstance(v, str) else None for k, v in cast(dict[object, object], remotes).items()}
        if isinstance(remotes, dict)
        else {},
        tracking={str(k): v for k, v in cast(dict[object, object], tracking).items() if isinstance(v, str)}
        if isinstance(tracking, dict)
        else {},
        push_default=push_default if isinstance(push_default, str) else None,
        # Unknown counts as custom: on a host the guard then refuses rather than guess.
        custom_push=data.get("custom_push") is not False,
        custom_remote=data.get("custom_remote") is not False,
    )


def map_json_to_host_checkouts(raw: object, git: object, cwd: str) -> list[HostCheckout]:
    """A gbridge from before checkouts sends only cwd's facts, as git, with no real root: its repo root stands in, and
    the caller narrows the roots for it."""
    if not isinstance(raw, list):
        facts = map_json_to_repo_facts(git)
        return [HostCheckout(cwd, facts, facts.repo_root)] if facts is not None and cwd else []
    found: list[HostCheckout] = []
    for item in cast(list[object], raw):
        facts = map_json_to_repo_facts(item)
        if facts is None:
            continue
        data = cast(dict[str, object], item)
        directory, real_root = data.get("dir"), data.get("real_root")
        if isinstance(directory, str) and isinstance(real_root, str) and directory and real_root:
            found.append(HostCheckout(directory, facts, real_root))
    return found


def handle_host_hook(
    record: HostWorker,
    config: Config,
    event: str,
    payload: dict[str, object],
    git: object,
    age_ns: int = 0,
    checkouts: object = None,
    home: object = None,
    real_roots: object = None,
) -> dict[str, object]:
    """age_ns dates a replayed hook back to when it happened, by the host's own clock, so newer ones still win.

    Every answer names the roots whose checkouts the guard reads; gbridge keeps them to pick what it reports."""
    roots = list(list_readable_roots(record, dict(config.worker_path_map)))
    if event == "PreToolUse":
        host_home = home if isinstance(home, str) and home.startswith("/") else None
        real = (
            {str(k): v for k, v in cast(dict[object, object], real_roots).items() if isinstance(v, str)}
            if isinstance(real_roots, dict)
            else {}
        )
        answer = handle_host_pre_tool_use(record, config, payload, git, checkouts, host_home, real)
        return {**answer, "roots": roots}
    caller = map_record_to_caller(record, git)
    conversation_id = payload.get("session_id")
    globs = list_guard_globs()
    if caller is None or event not in HOOK_EVENTS or not isinstance(conversation_id, str):
        return {"output": "", "globs": globs, "roots": roots}
    conn = connect(db_path(), HOOK_BUSY_TIMEOUT_MS)
    try:
        if not bind_conversation(conn, record.worker, conversation_id):
            log_error(
                "worker api", f"{record.worker} sent a hook for conversation {conversation_id[:40]!r}, not its own"
            )
            return {"output": ""}
        event_ns = time.time_ns() - max(0, age_ns)
        result = handle_hook(conn, event, payload, {}, config, event_ns, measure=True, caller=caller)
    finally:
        conn.close()
    return {
        "output": convert_bridge_commands_to_gbridge(result.output), "hint_due": result.hint_due, "globs": globs,
        "roots": roots,
    }  # fmt: skip


def handle_host_pre_tool_use(
    record: HostWorker,
    config: Config,
    payload: dict[str, object],
    git: object,
    checkouts: object = None,
    home: str | None = None,
    real_roots: dict[str, str] | None = None,
) -> dict[str, object]:
    """The Mac's own shell guard, on the host's git facts. Without checked: true gbridge refuses the command."""
    conversation_id = payload.get("session_id")
    if map_record_to_caller(record, None) is None:
        return {"checked": False, "reason": "the worker's tab on the Mac is not running"}
    if not isinstance(conversation_id, str):
        return {"checked": False, "reason": "the hook carried no session id"}
    conn = connect(db_path(), HOOK_BUSY_TIMEOUT_MS)
    try:
        if not bind_conversation(conn, record.worker, conversation_id):
            log_error("worker api", f"{record.worker} sent a guard check for {conversation_id[:40]!r}, not its own")
            return {"checked": False, "reason": "the conversation is not this worker's own"}
        cwd = payload.get("cwd")
        legacy = not isinstance(checkouts, list)
        roots = list_readable_roots(record, dict(config.worker_path_map))
        found = map_json_to_host_checkouts(checkouts, git, cwd if isinstance(cwd, str) else "")
        if legacy:
            # Its cwd's root is lexical, unchecked against symlinks: read only a command run in the worker's own
            # worktree itself, where Claude starts, not in a directory below it that may lead anywhere.
            worktree = os.path.normpath(record.remote_worktree or record.remote_cwd or "/nonexistent")
            found = [
                c for c in found
                if os.path.normpath(c.facts.repo_root) == worktree and os.path.normpath(c.directory) == worktree
            ]  # fmt: skip
            roots = (worktree,)
        reader = make_host_repo_reader(found, roots, home, real_roots)
        output = handle_pre_tool_use(conn, payload, config, reader)
    finally:
        conn.close()
    globs = list_guard_globs()
    return {"checked": True, "output": convert_bridge_commands_to_gbridge(output), "globs": globs}


def run_host_cli(record: HostWorker, argv: list[str], stdin: str) -> dict[str, object]:
    """The bridge CLI itself, as the worker: it finds the worker's session by the tab's loop process."""
    if not argv or argv[0] not in WORKER_COMMANDS:
        return {"code": 2, "stdout": "", "stderr": f"gbridge: only {', '.join(sorted(WORKER_COMMANDS))}\n"}
    if map_record_to_caller(record, None) is None:
        return {"code": 1, "stdout": "", "stderr": "gbridge: the worker's tab on the Mac is not running\n"}
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "AGTERM_"))}
    env |= {
        "CLAUDE_PID": str(record.loop_pid),
        "AGTERM_SESSION_ID": record.agterm_session,
        "AGTERM_PANE_ID": record.pane,
        "AGTERM_WORKSPACE_ID": record.agterm_workspace,
    }
    cwd = record.cwd if Path(record.cwd).is_dir() else None
    try:
        done = subprocess.run(
            [sys.executable, "-m", "bridge", *argv],
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            cwd=cwd,
            timeout=CLI_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"code": 1, "stdout": "", "stderr": f"gbridge: bridge did not answer in {CLI_TIMEOUT_S}s\n"}
    stderr = re.sub(r"^bridge: ", "gbridge: ", done.stderr, flags=re.MULTILINE)
    return {
        "code": done.returncode,
        "stdout": convert_bridge_commands_to_gbridge(done.stdout),
        "stderr": convert_bridge_commands_to_gbridge(stderr),
    }


def convert_bridge_commands_to_gbridge(text: str) -> str:
    """bridge's wording names its own commands; on the host the worker has to type gbridge."""
    return _BRIDGE_COMMAND_RE.sub("gbridge ", text)


class WorkerApiHandler(BaseHTTPRequestHandler):
    config: Config

    def do_POST(self) -> None:
        worker = self.headers.get("X-Bridge-Worker") or ""
        auth = self.headers.get("Authorization") or ""
        record = authenticate(worker, auth.removeprefix("Bearer ")) if auth.startswith("Bearer ") else None
        if record is None:
            self._json(403, {"stderr": "unknown worker or token"})
            return
        data = self._read_json()
        if data is None:
            self._json(400, {"stderr": "expected a JSON object"})
            return
        try:
            if self.path == "/hook":
                event, payload = data.get("event"), data.get("payload")
                if not isinstance(event, str) or not isinstance(payload, dict):
                    self._json(400, {"stderr": "expected {event, payload}"})
                    return
                age = data.get("age_ns")
                answer = handle_host_hook(
                    record, self.config, event, cast(dict[str, object], payload), data.get("git"),
                    age if isinstance(age, int) else 0, data.get("checkouts"), data.get("home"), data.get("real_roots"),
                )  # fmt: skip
                self._json(200, answer)
            elif self.path == "/cli":
                argv, stdin = data.get("argv"), data.get("stdin", "")
                if not isinstance(argv, list) or not isinstance(stdin, str):
                    self._json(400, {"stderr": "expected {argv: [string], stdin: string}"})
                    return
                self._json(200, run_host_cli(record, [str(a) for a in cast(list[Any], argv)], stdin))
            else:
                self._json(404, {"stderr": "not found"})
        except Exception:
            log_error(f"worker api {self.path}", traceback.format_exc())
            self._json(500, {"stderr": "bridge failed; see its error log on the Mac"})

    def _read_json(self) -> dict[str, object] | None:
        length_text = self.headers.get("Content-Length") or ""
        if not re.fullmatch(r"[0-9]{1,7}", length_text) or not 0 < int(length_text) <= MAX_BODY_BYTES:
            return None
        try:
            raw: object = json.loads(self.rfile.read(int(length_text)))
        except ValueError:
            return None
        return cast(dict[str, object], raw) if isinstance(raw, dict) else None

    def log_message(self, format: str, *args: object) -> None:
        if os.environ.get("BRIDGE_HTTP_LOG"):
            super().log_message(format, *args)

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
