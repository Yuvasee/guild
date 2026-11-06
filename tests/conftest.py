import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pytest

from bridge.db import connect
from bridge.procinfo import process_start_ms

VENV_BIN = Path(sys.executable).parent
STUB_AGTERMCTL = """#!/bin/sh
pane=none
prev=
for arg; do [ "$prev" = --pane-id ] && pane=$arg; prev=$arg; done
# What bridge typed sits in its pane's box until a lone Enter submits it; $STUB_DRAFT is someone else's draft.
box="$STUB_DRAFT-$pane"
if [ "$1" = tree ]; then
  if [ -f "$STUB_TREE" ]; then cat "$STUB_TREE"; else echo '{"ok":true,"result":{"tree":{"workspaces":[]}}}'; fi
elif [ "$1 $2" = "session text" ]; then
  if [ -n "$STUB_DRAFT" ] && [ -f "$box" ]; then cat "$box"; exit 0; fi
  if [ -n "$STUB_DRAFT" ] && [ -f "$STUB_DRAFT" ]; then cat "$STUB_DRAFT"; exit 0; fi
  printf '%s\\n' '\u2500\u2500' '\u276f\u00a0' '\u2500\u2500' '  -- INSERT --'
else
  [ "$1 $2" = "session type" ] && [ -f "$STUB_TYPE_FAILS" ] && exit 1
  [ "$1 $2" = "session type" ] && [ -f "$STUB_TYPE_HANGS" ] && sleep 30
  echo "$@" >> "$STUB_LOG"
  [ "$1 $2" = "workspace new" ] && echo '{"ok":true,"result":{"id":"WS-NEW"}}'
  [ "$1 $2" = "session new" ] && echo '{"ok":true,"result":{"id":"TAB-W"}}'
  for last; do :; done
  # STUB_DROP_ENTER: Claude Code drops the first Enter on the typed "continue".
  if [ "$1 $2" = "session type" ] && [ -n "$STUB_DRAFT" ]; then
    case "$last" in
      "$(printf '\\r')")
        if [ -n "$STUB_DROP_ENTER" ] && [ ! -f "$STUB_DRAFT.dropped" ] && grep -q Continue "$box" 2>/dev/null; then
          touch "$STUB_DRAFT.dropped"
        else rm -f "$box"; fi ;;
      .|"$(printf '\\177')") ;;
      *) [ -f "$box" ] || printf '%s\\n' '\u2500\u2500' "\u276f $last" '\u2500\u2500' > "$box" ;;
    esac
  fi
fi
"""


@dataclass
class BridgeEnv:
    root: Path
    state_dir: Path
    sessions_dir: Path
    stub_log: Path
    stub_tree: Path
    env: dict[str, str]

    @property
    def db(self) -> Path:
        return self.state_dir / "bridge.db"

    def connect(self) -> sqlite3.Connection:
        return connect(self.db)

    def hook(
        self, event: str, payload: Mapping[str, object], pid: int | None, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        if pid is not None:
            env["CLAUDE_PID"] = str(pid)
        env.update(extra_env or {})
        return subprocess.run(
            [str(VENV_BIN / "bridge-hook"), event],
            input=json.dumps(payload),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def cli(self, *args: str, pid: int | None = None) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        if pid is not None:
            env["CLAUDE_PID"] = str(pid)
        return subprocess.run(
            [str(VENV_BIN / "bridge"), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def write_session_file(self, pid: int, session_id: str, kind: str = "interactive", status: str = "idle") -> None:
        start = process_start_ms(pid)
        assert start is not None
        data = {
            "pid": pid,
            "sessionId": session_id,
            "cwd": str(self.root),
            "startedAt": start + 1500,
            "kind": kind,
            "name": f"peer-{pid}",
            "status": status,
            "statusUpdatedAt": start,
        }
        (self.sessions_dir / f"{pid}.json").write_text(json.dumps(data))


class SpawnProcess(Protocol):
    def __call__(self, *argv: str, env: dict[str, str] | None = None) -> int: ...


@pytest.fixture
def bridge_env(tmp_path: Path) -> BridgeEnv:
    state_dir = tmp_path / "state"
    sessions_dir = tmp_path / "claude-sessions"
    sessions_dir.mkdir()
    stub = tmp_path / "agtermctl"
    stub.write_text(STUB_AGTERMCTL)
    stub.chmod(0o755)
    notify = tmp_path / "notify"
    notify.write_text(f'#!/bin/sh\necho "$@" >> "{tmp_path}/notify.log"\n')
    notify.chmod(0o755)
    config = tmp_path / "config.toml"
    config.write_text(
        f'issue_prefixes = ["ABC"]\nagtermctl = "{stub}"\nterminal_app = ""\nnotify_command = "{notify}"\n'
        'issue_url = "https://tracker.example/issue/{key}"\n'
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AGTERM_", "CLAUDE"))}
    env.update(
        BRIDGE_STATE_DIR=str(state_dir),
        BRIDGE_CONFIG=str(config),
        BRIDGE_CLAUDE_SESSIONS_DIR=str(sessions_dir),
        BRIDGE_CLAUDE_PROJECTS_DIR=str(tmp_path / "projects"),
        STUB_LOG=str(tmp_path / "agtermctl.log"),
        STUB_TREE=str(tmp_path / "tree.json"),
        STUB_DRAFT=str(tmp_path / "draft"),
        STUB_TYPE_FAILS=str(tmp_path / "type-fails"),
        STUB_TYPE_HANGS=str(tmp_path / "type-hangs"),
    )
    return BridgeEnv(
        root=tmp_path,
        state_dir=state_dir,
        sessions_dir=sessions_dir,
        stub_log=tmp_path / "agtermctl.log",
        stub_tree=tmp_path / "tree.json",
        env=env,
    )


@pytest.fixture
def spawn_process() -> Generator[SpawnProcess]:
    spawned: list[subprocess.Popen[bytes]] = []

    def spawn(*argv: str, env: dict[str, str] | None = None) -> int:
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("AGTERM_", "CLAUDE"))}
        # macOS hides the environment of /bin/sleep and other platform binaries, and tests rely on reading it.
        default = [sys.executable, "-c", "import time; time.sleep(300)"]
        proc = subprocess.Popen(list(argv) if argv else default, env={**clean, **(env or {})})
        spawned.append(proc)
        deadline = time.monotonic() + 5
        while process_start_ms(proc.pid) is None and time.monotonic() < deadline:
            time.sleep(0.01)
        return proc.pid

    yield spawn
    for proc in spawned:
        proc.kill()
        proc.wait()


def _github_reachable() -> bool:
    try:
        return subprocess.run(["gh", "auth", "status"], capture_output=True, timeout=15, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


needs_github = pytest.mark.skipif(not _github_reachable(), reason="needs network and a signed-in gh")


def wait_for(predicate: Callable[[], bool], timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def dashboard(bridge_env: BridgeEnv) -> Generator[str]:
    port = free_port()
    server = subprocess.Popen([str(VENV_BIN / "bridge"), "serve", "--port", str(port)], env=bridge_env.env)
    base = f"http://127.0.0.1:{port}"
    wait_up(base)
    yield base
    server.terminate()
    server.wait()


def http_request(
    url: str,
    post: bool = False,
    headers: dict[str, str] | None = None,
    expect: int = 200,
    data: dict[str, object] | None = None,
) -> str:
    all_headers = {"X-Bridge": "1"} if post else {}
    all_headers.update(headers or {})
    body = json.dumps(data).encode() if data is not None else None
    if body is not None:
        all_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, method="POST" if post else "GET", headers=all_headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            status, body = response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read().decode()
    assert status == expect, f"{url}: {status} {body}"
    return body


def wait_up(base: str) -> None:
    deadline = time.monotonic() + 5
    while True:
        try:
            urllib.request.urlopen(base + "/api/state", timeout=1).close()
            return
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
