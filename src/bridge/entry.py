"""Entry points that outlive a reinstall: `uv tool install --reinstall` replaces the package's files in place, and a
process starting in that fraction of a second finds a module missing. Only the standard library is imported here."""

import importlib
import os
import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path

IMPORT_WAIT_S = 3.0
"""Within a hook's 5 s: the files are back in well under a second."""
IMPORT_RETRY_S = 0.1


def load_main(module: str, wait_s: float = IMPORT_WAIT_S) -> Callable[[], int] | None:
    deadline = time.monotonic() + wait_s
    while True:
        try:
            return importlib.import_module(module).main
        except ImportError:
            if time.monotonic() >= deadline:
                _log_error(module, traceback.format_exc())
                return None
        # What half-imported from the old files or the new must not be reused, nor the stale directory listings.
        _forget_package(module.partition(".")[0])
        importlib.invalidate_caches()
        time.sleep(IMPORT_RETRY_S)


def hook_main() -> int:
    """A hook fails open, as it always has: exit 0 with nothing written."""
    main = load_main("bridge.hooks")
    return main() if main is not None else 0


def cli_main() -> int:
    main = load_main("bridge.cli")
    return main() if main is not None else 1


def guild_main() -> int:
    main = load_main("bridge.cli_guild")
    return main() if main is not None else 1


def _forget_package(package: str) -> None:
    for name in [n for n in sys.modules if n.startswith(f"{package}.") and n != __name__]:
        del sys.modules[name]


def _log_error(module: str, detail: str) -> None:
    # bridge.errlog may be the very module that is missing; this is its path and format.
    try:
        directory = Path(os.environ.get("BRIDGE_STATE_DIR") or Path.home() / ".local/state/bridge")
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "errors.log").open("a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} import {module}\n{detail}\n")
    except OSError:
        pass
