import time

from bridge.config import state_dir


def log_error(context: str, detail: str) -> None:
    try:
        directory = state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "errors.log").open("a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {context}\n{detail}\n")
    except OSError:
        pass
