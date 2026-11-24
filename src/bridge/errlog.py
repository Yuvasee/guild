import re
import threading
import time
from collections.abc import Callable
from typing import TextIO

from bridge.config import state_dir


def log_error(context: str, detail: str) -> None:
    _append("errors.log", context, detail)


def log_slow(context: str, detail: str) -> None:
    """Work that was slow but got done: kept apart, so errors.log holds only what failed."""
    _append("slow.log", context, detail)


def _append(name: str, context: str, detail: str) -> None:
    try:
        directory = state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / name).open("a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {context}\n{detail}\n")
    except OSError:
        pass


_LINE_RE = re.compile(r"[^\n]*\n|[^\n]+")


class StampedLines:
    """A stream whose every line starts with its UTC time, flushed at each line end: serve's stdout and stderr, so
    serve.log dates every line, a traceback's included. print() writes the text and its newline apart, hence the
    state."""

    def __init__(self, stream: TextIO, clock: Callable[[], float] = time.time) -> None:
        self._stream, self._clock = stream, clock
        self._lock = threading.Lock()
        self._at_line_start = True

    def write(self, text: str) -> int:
        with self._lock:
            pieces: list[str] = []
            for line in _LINE_RE.findall(text):
                if self._at_line_start:
                    pieces.append(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime(self._clock())))
                pieces.append(line)
                self._at_line_start = line.endswith("\n")
            self._stream.write("".join(pieces))
            if "\n" in text:
                self._stream.flush()
        return len(text)

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name: str) -> object:
        return getattr(self._stream, name)
