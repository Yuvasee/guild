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
