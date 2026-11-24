from dataclasses import dataclass, field

AGTERM_SILENT_NOTICE_AFTER_MS = 300_000


@dataclass
class OutageNotices:
    """The owner's macOS notices for an outage the assistant alone cannot handle: more than one tab not receiving
    messages, the assistant's own tab among them, or agterm silent. One notice per episode; it ends when the cause
    clears, so the next one is shown again."""

    open: set[str] = field(default_factory=lambda: set[str]())

    def take(self, undelivered: list[str], assistant: str, silent_since: int | None, now: int) -> list[tuple[str, str]]:
        """(cause, text) of each notice to show now; undelivered: the handles of the tabs past the undelivered limit."""
        causes: dict[str, str] = {}
        if len(undelivered) > 1 or (undelivered and assistant in undelivered):
            names = ", ".join(sorted(set(undelivered)))
            causes["tabs"] = f"bridge: messages are not reaching {names}"
        if silent_since is not None and now - silent_since >= AGTERM_SILENT_NOTICE_AFTER_MS:
            causes["agterm"] = f"bridge: agterm has not answered for {(now - silent_since) // 60_000} min"
        self.open &= causes.keys()
        new = [(cause, text) for cause, text in causes.items() if cause not in self.open]
        self.open |= causes.keys()
        return new

    def release(self, cause: str) -> None:
        """A notice that surely did not show is tried again on the next pass."""
        self.open.discard(cause)


LOOP_FAILING_TELL_AFTER_MS = 10 * 60_000
"""A background loop failing this long is no blip: polls run every minute or two, and a lock or a GitHub hiccup clears
in a few."""


@dataclass
class FailureSpell:
    """A background loop that keeps failing: told once when it has failed for LOOP_FAILING_TELL_AFTER_MS, and once more
    when it works again. Without the first notice, every round of it fails quietly in the log."""

    since: int | None = None
    told: bool = False

    def fail(self, now: int, awake_since: int = 0) -> int | None:
        """Minutes it has failed, when that is to be told now. awake_since: the Mac's last wake; an untold spell starts
        over there, since a sleeping Mac reaches nothing and a waking one has no network for a while."""
        if self.since is None or not self.told:
            self.since = min(max(now if self.since is None else self.since, awake_since), now)
        if self.told or now - self.since < LOOP_FAILING_TELL_AFTER_MS:
            return None
        return (now - self.since) // 60_000

    def recovered(self, now: int) -> int | None:
        """Minutes it failed, when a told spell is to be told over; reset() once that is said."""
        return (now - self.since) // 60_000 if self.told and self.since is not None else None

    def reset(self) -> None:
        self.since, self.told = None, False


@dataclass
class LoopStalls:
    """serve's background loops by when each last finished a round: one past its limit is told once, and once more
    when it finishes a round again. Every loop looks at all of them as it rests, so a hung one is seen by the others.
    limits: each watched loop's limit in ms; only loops that run belong in it."""

    started_at: int
    limits: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    finished: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    told: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    """Each loop told as stalled, with the round it had last finished then (started_at if none)."""
    back_at: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    """Each told loop's first finished round after its stall, kept until its recovery is told."""

    def take(self, now: int, awake_since: int = 0) -> tuple[list[tuple[str, int | None]], list[tuple[str, int]]]:
        """(loop, its last finished round or None) newly past its limit, and (loop, minutes it went without one) for
        each told loop that has finished a round since. awake_since: the Mac's last wake, as a sleeping Mac runs no
        loop; mark_told and mark_back once each is said."""
        stalled: list[tuple[str, int | None]] = []
        back: list[tuple[str, int]] = []
        # A copy: serve may still be starting loops while the first ones rest.
        for name, limit in list(self.limits.items()):
            last = self.finished.get(name)
            stuck = now - max(self.started_at if last is None else last, awake_since) > limit
            if name in self.told and last is not None and last > self.told[name] and stuck:
                # Back and stuck again before its recovery was told: the new stall is what matters now.
                self.mark_back(name)
            if name in self.told:
                if last is not None and last > self.told[name]:
                    first = self.back_at.setdefault(name, last)
                    back.append((name, (first - self.told[name]) // 60_000))
            elif stuck:
                stalled.append((name, last))
        return stalled, back

    def mark_told(self, name: str, last: int | None) -> None:
        """last: as take gave it; the loop may have finished a round since, which is then its recovery."""
        self.told[name] = self.started_at if last is None else last

    def mark_back(self, name: str) -> None:
        self.told.pop(name, None)
        self.back_at.pop(name, None)
