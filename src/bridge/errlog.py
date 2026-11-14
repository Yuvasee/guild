import time

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
