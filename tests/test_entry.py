import sys
import threading
import time
from collections.abc import Generator
from pathlib import Path

import pytest

from bridge.entry import load_main


@pytest.fixture
def package(tmp_path: Path) -> Generator[Path]:
    """A stand-in package whose runner imports a sibling that is not on disk yet, as mid-reinstall."""
    root = tmp_path / "pkgroot"
    pkg = root / "halfpkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "runner.py").write_text("from halfpkg.part import VALUE\n\ndef main() -> int:\n    return VALUE\n")
    sys.path.insert(0, str(root))
    yield pkg
    sys.path.remove(str(root))
    for name in [n for n in sys.modules if n == "halfpkg" or n.startswith("halfpkg.")]:
        del sys.modules[name]


def test_a_module_missing_for_a_moment_is_waited_for(package: Path, tmp_path: Path) -> None:
    def write_later() -> None:
        time.sleep(0.3)
        (package / "part.py").write_text("VALUE = 7\n")

    writer = threading.Thread(target=write_later)
    writer.start()
    started = time.monotonic()
    main = load_main("halfpkg.runner", wait_s=3.0)
    writer.join()

    assert main is not None and main() == 7
    assert time.monotonic() - started >= 0.3


def test_a_module_still_missing_after_the_wait_is_logged_once(
    package: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path / "state"))

    main = load_main("halfpkg.runner", wait_s=0.3)

    log = (tmp_path / "state" / "errors.log").read_text()
    assert main is None
    assert log.count("import halfpkg.runner") == 1 and "No module named 'halfpkg.part'" in log
