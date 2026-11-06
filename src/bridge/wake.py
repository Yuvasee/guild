import threading
import time
from collections.abc import Callable

SLEEP_GAP_S = 60.0


class WakeWatch:
    """Since when a delay counts: the last wake of the Mac, or agterm's last return after it stopped answering.

    macOS stops the monotonic clock while the Mac sleeps, so the wall clock running well ahead of it between two looks
    means the Mac slept in between. While agterm does not answer, a delay counts from when it stopped.
    """

    def __init__(self, wall: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._wall, self._monotonic = wall, monotonic
        self._lock = threading.Lock()
        self._looked = (wall(), monotonic())
        self._back_at_ms = 0
        self._silent_since_ms: int | None = None

    def counted_from(self) -> int:
        with self._lock:
            wall, monotonic = self._wall(), self._monotonic()
            if (wall - self._looked[0]) - (monotonic - self._looked[1]) > SLEEP_GAP_S:
                self._back_at_ms = int(wall * 1000)
            self._looked = (wall, monotonic)
            # A wall clock set back leaves these ahead of it; from now is the latest a count may start.
            now = int(wall * 1000)
            self._back_at_ms = min(self._back_at_ms, now)
            if self._silent_since_ms is not None:
                self._silent_since_ms = min(self._silent_since_ms, now)
            return max(self._back_at_ms, self._silent_since_ms or 0)

    def note_agterm(self, answered: bool) -> None:
        with self._lock:
            now = int(self._wall() * 1000)
            if answered and self._silent_since_ms is not None:
                self._back_at_ms = max(self._back_at_ms, now)
                self._silent_since_ms = None
            elif not answered and self._silent_since_ms is None:
                self._silent_since_ms = now
