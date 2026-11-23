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

    def fail(self, now: int) -> int | None:
        """Minutes it has failed, when that is to be told now."""
        if self.since is None:
            self.since = now
        if self.told or now - self.since < LOOP_FAILING_TELL_AFTER_MS:
            return None
        return (now - self.since) // 60_000

    def recovered(self, now: int) -> int | None:
        """Minutes it failed, when a told spell is to be told over; reset() once that is said."""
        return (now - self.since) // 60_000 if self.told and self.since is not None else None

    def reset(self) -> None:
        self.since, self.told = None, False
