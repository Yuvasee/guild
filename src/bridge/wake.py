import threading
import time
from collections.abc import Callable

SLEEP_GAP_S = 60.0


class WakeWatch:
    """Since when a delay counts: the last wake of the Mac; and since when agterm has not answered.

    macOS stops the monotonic clock while the Mac sleeps, so the wall clock running well ahead of it between two looks
    means the Mac slept in between. agterm not answering never pauses the count: a hung agterm is itself an outage.
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
            return self._back_at_ms

    def silent_since(self) -> int | None:
        """Hours the Mac slept are not silence: counted from the wake at the latest."""
        back = self.counted_from()
        with self._lock:
            if self._silent_since_ms is None:
                return None
            return min(max(self._silent_since_ms, back), int(self._wall() * 1000))

    def note_agterm(self, answered: bool) -> None:
        with self._lock:
            now = int(self._wall() * 1000)
            if answered:
                self._silent_since_ms = None
            elif not answered and self._silent_since_ms is None:
                self._silent_since_ms = now
