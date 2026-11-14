import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from bridge.board import HINT_SENT, write_hint
from bridge.claude import read_session_file
from bridge.config import state_dir
from bridge.errlog import log_error
from bridge.procinfo import pid_alive

WAITER_SCRIPT = "bridge-rewake.sh"
# The waiter rewrites its pid file every poll; older than this, it is gone or the Mac slept.
BEAT_FRESH_S = 5.0
REWAKE_GRACE_S = 15.0

type WaiterState = Literal["idle", "busy", "absent"]


def rewake_dir() -> Path:
    return state_dir() / "rewake"


def busy_path(pid: int) -> Path:
    return rewake_dir() / f"{pid}.busy"


def waiter_lock(pid: int) -> Path:
    return rewake_dir() / f"{pid}.waiter"


def install_waiter_script(directory: Path | None = None) -> Path:
    """A copy outside the package, so a hook never points into a checkout that may be deleted."""
    target_dir = directory or state_dir() / "bin"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / WAITER_SCRIPT
    tmp = target.with_name(f".{WAITER_SCRIPT}.{os.getpid()}.tmp")
    tmp.write_text(Path(__file__).with_name("rewake.sh").read_text())
    tmp.replace(target)
    return target


def read_waiter(pid: int, now: float | None = None) -> WaiterState:
    """ "idle": a waiter listens between turns, and a rewake would reach it now; "busy": a turn of a session with
    waiter hooks runs, or ran with no end reported (Esc fires no Stop), and its end starts a waiter; "absent": neither.
    A waking waiter leaves before the turn it starts, so the busy mark alone counts there."""
    if busy_path(pid).exists():
        return "busy"
    lock = waiter_lock(pid)
    try:
        owner = (lock / "pid").read_text().strip()
        # A waiter from before the beat had its own file beats on its pid file.
        beat_at = max(_mtime(lock / "beat"), (lock / "pid").stat().st_mtime)
    except OSError:
        return "absent"
    age = (time.time() if now is None else now) - beat_at
    return "idle" if age <= BEAT_FRESH_S and owner.isdigit() and pid_alive(int(owner)) else "absent"


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def is_turn_running(pid: int) -> bool:
    """By Claude Code's own record: a waiting dialog counts, as an Enter typed into it would answer it."""
    session = read_session_file(pid)
    return session is not None and session.status in ("busy", "waiting")


class WaiterDeferral:
    """Holds a typer back while the tab's waiter can deliver the message without typing, and while a turn it serves
    runs, whose own hooks or the waiter after it bring the message. It types where no waiter listens, after a turn
    whose end went unreported, and when the waiter has not taken the message within the grace."""

    def __init__(
        self,
        pid: int,
        what: str,
        grace_s: float | None = None,
        waiter: Callable[[int], WaiterState] = read_waiter,
        turn_running: Callable[[int], bool] = is_turn_running,
        nudge: Callable[[int], None] = lambda pid: write_hint(pid, HINT_SENT),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._pid, self._what = pid, what
        self._grace_s = REWAKE_GRACE_S if grace_s is None else grace_s
        self._waiter, self._turn_running, self._nudge, self._clock = waiter, turn_running, nudge, clock
        self._since: float | None = None
        self.gave_up = False

    def may_type(self) -> bool:
        if self.gave_up:
            return True
        state = self._waiter(self._pid)
        if state != "idle":
            self._since = None
            return state == "absent" or not self._turn_running(self._pid)
        if self._since is None:
            self._since = self._clock()
            # A tool hook may have rewritten the send's 0 before the turn ended; the waiter acts only on a 0.
            try:
                self._nudge(self._pid)
            except OSError as exc:
                log_error("rewake", f"{self._what}: could not flag Claude pid {self._pid} for its waiter: {exc}")
            return False
        if self._clock() - self._since < self._grace_s:
            return False
        self.gave_up = True
        log_error(
            "rewake",
            f"{self._what}: the waiter of Claude pid {self._pid} did not take it in {self._grace_s:.0f} s; typing it",
        )
        return True


def prune_rewake_marks() -> None:
    """Marks of Claude processes that are gone, left by a waiter killed outright or a print-mode run's prompt."""
    try:
        entries = list(rewake_dir().iterdir())
    except OSError:
        return
    for path in entries:
        stem = path.name.split(".", 1)[0]
        if not stem.isdigit() or pid_alive(int(stem)):
            continue
        try:
            if path.is_dir():
                for inner in path.iterdir():
                    inner.unlink(missing_ok=True)
                path.rmdir()
            else:
                path.unlink(missing_ok=True)
        except OSError:
            continue
