from pathlib import Path

from bridge.service import map_port_to_plist


def test_the_service_runs_unthrottled_so_hooks_never_wait_on_a_starved_lock_holder(tmp_path: Path) -> None:
    plist = map_port_to_plist("/usr/local/bin/bridge", 8765, tmp_path / "serve.log", "/usr/bin")

    assert plist["ProcessType"] == "Interactive"
    assert plist["ProgramArguments"] == ["/usr/local/bin/bridge", "serve", "--port", "8765"]
