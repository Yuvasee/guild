import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

import pytest

from bridge.db import connect
from bridge.procinfo import process_start_ms

VENV_BIN = Path(sys.executable).parent

REAL_HOME = str(Path.home())
LIVE_DIRS = tuple(
    {
        os.path.abspath(p)
        for d in (f"{REAL_HOME}/.local/state/bridge", f"{REAL_HOME}/.config/bridge")
        for p in (d, os.path.realpath(d))
    }
)
_LIVE_PREFIXES = tuple(f"{d}/" for d in LIVE_DIRS)
_PATH_ARGS = {
    "open": 1,
    "os.mkdir": 1,
    "os.remove": 1,
    "os.rmdir": 1,
    "os.rename": 2,
    "os.symlink": 2,
    "shutil.copyfile": 2,
    "sqlite3.connect": 1,
}
LIVE_ACCESSES: list[str] = []


def _is_live(path: str) -> bool:
    path = os.path.abspath(path)
    return path in LIVE_DIRS or path.startswith(_LIVE_PREFIXES)


def find_live_path(event: str, args: tuple[object, ...]) -> str | None:
    """The live service's state or config that this process opens, or the live state a child it starts would write."""
    if event == "subprocess.Popen":
        # Only a bridge child reads its config, and a child's argv does not say reliably whether it is one.
        env = cast("Mapping[str, str]", args[3]) if isinstance(args[3], Mapping) else os.environ
        path = env.get("BRIDGE_STATE_DIR") or f"{env.get('HOME') or REAL_HOME}/.local/state/bridge"
        return f"{path} as the state dir of {args[1]!r}" if _is_live(path) else None
    for arg in args[: _PATH_ARGS.get(event, 0)]:
        if isinstance(arg, (str, bytes, os.PathLike)) and _is_live(
            path := os.fsdecode(cast("str | os.PathLike[str]", arg))
        ):
            return path
    return None


def _record_live_access(event: str, args: tuple[object, ...]) -> None:
    # A snapshot of the live dirs cannot tell the tests' writes from the running service's; this sees only our own.
    if (event == "subprocess.Popen" or event in _PATH_ARGS) and (path := find_live_path(event, args)):
        LIVE_ACCESSES.append(f"{event} {path}")


sys.addaudithook(_record_live_access)


def pytest_configure(config: pytest.Config) -> None:
    """Collection and session fixtures run outside any test: they get a state dir and config of their own too."""
    session = Path(tempfile.mkdtemp(prefix="bridge-tests-"))
    os.environ["BRIDGE_STATE_DIR"] = str(session / "state")
    os.environ["BRIDGE_CONFIG"] = str(session / "no-config.toml")
    # Commands that render a page in a headless browser skip it: a test that renders asks for the browser itself.
    os.environ["BRIDGE_BROWSER"] = ""


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if LIVE_ACCESSES:
        sys.stderr.write("\nTests reached the live bridge state or config:\n" + "\n".join(LIVE_ACCESSES) + "\n")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(autouse=True)
def _hermetic_bridge_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> Generator[None]:
    """A test that needs a particular state dir or config still sets its own: it runs after this."""
    own = tmp_path_factory.mktemp("bridge-state")
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(own))
    monkeypatch.setenv("BRIDGE_CONFIG", str(own / "no-config.toml"))
    before = len(LIVE_ACCESSES)
    yield
    assert LIVE_ACCESSES[before:] == [], "the test reached the live bridge state or config"


@pytest.fixture(autouse=True)
def _no_claude_config_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Code that edits Claude Code's own config finds it under HOME, which tests point at tmp_path."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


@pytest.fixture(autouse=True)
def _first_reader_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registering a doc reads the first reader's recorded rounds; tests of other things have one clean round."""

    def one_clean_round(doc: str) -> list[tuple[int, list[str], str]]:
        return [(1, [], "right")]

    monkeypatch.setattr("bridge.review_worktree.read_reader_rounds", one_clean_round)


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
    # bridge shows notifications with osascript and raises agterm with open: stubs keep both off the real screen.
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    for name, log in (("osascript", "notify.log"), ("open", "open.log")):
        (stub_bin / name).write_text(f'#!/bin/sh\necho "$@" >> "{tmp_path}/{log}"\n')
        (stub_bin / name).chmod(0o755)
    # serve's hourly disk check would otherwise du the real home folders for tens of seconds during a test.
    for name in ("du", "docker"):
        (stub_bin / name).write_text("#!/bin/sh\nexit 1\n")
        (stub_bin / name).chmod(0o755)
    config = tmp_path / "config.toml"
    # Each message wakes its tab at once, as before batching; the batch tests set their own.
    config.write_text(
        f'issue_prefixes = ["ABC"]\nagtermctl = "{stub}"\nissue_url = "https://tracker.example/issue/{{key}}"\n'
        "batch_age_min = 0\n"
    )
    # A Mac worker starts only in a folder Claude Code trusts: the tests' folders are, in a config of their own.
    claude_home = tmp_path / "claude-home"
    claude_home.mkdir()
    trusted = {"hasTrustDialogAccepted": True}
    (claude_home / ".claude.json").write_text(json.dumps({"projects": {os.path.realpath(tmp_path): trusted}}))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AGTERM_", "CLAUDE"))}
    env.update(
        PATH=f"{stub_bin}{os.pathsep}{env.get('PATH', '')}",
        CLAUDE_CONFIG_DIR=str(claude_home),
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
    # A server slow to come up fails the setup, and without this it outlives the run.
    try:
        wait_up(base)
        yield base
    finally:
        server.terminate()
        server.wait()


def http_request(
    url: str,
    post: bool = False,
    headers: dict[str, str] | None = None,
    expect: int = 200,
    data: dict[str, object] | None = None,
) -> str:
    origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(url))
    all_headers = {"X-Bridge": "1", "Origin": origin, "Sec-Fetch-Site": "same-origin"} if post else {}
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


def _is_gh_signed_in() -> bool:
    # Read from gh's own files, not `gh auth status`: no subprocess may start while tests are collected.
    gh_config = Path(os.environ.get("GH_CONFIG_DIR") or Path.home() / ".config" / "gh")
    return shutil.which("gh") is not None and bool(os.environ.get("GH_TOKEN") or (gh_config / "hosts.yml").exists())


requires_github = pytest.mark.skipif(not _is_gh_signed_in(), reason="calls live GitHub: needs a signed-in gh")
