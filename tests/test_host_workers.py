import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import BridgeEnv, SpawnProcess

from bridge import gbridge, host_side, host_workers
from bridge.config import Config, Remote, load_config, map_toml_to_config
from bridge.db import connect
from bridge.hooks import HookCaller, handle_hook
from bridge.host_workers import (
    HostWorker,
    authenticate,
    build_attach_command,
    build_host_brief_note,
    build_host_cleanup_command,
    build_tab_command,
    find_remote,
    hash_token,
    make_worker_name,
    map_cwd_to_host_path,
    map_remote_to_ssh,
    map_worker_to_exact_target,
    read_host_rule_files,
    read_local_model_flags,
    read_session_state,
    sync_rules_to_remotes,
    write_record,
)
from bridge.procinfo import process_start_ms
from bridge.registry import Surface
from bridge.worker_api import convert_bridge_commands_to_gbridge, run_host_cli, start_worker_api
from bridge.workers import HostResume, TabSession, map_host_tab_to_closed_records, map_resume_command_to_host_resume


def _record(**changes: Any) -> HostWorker:
    base: dict[str, Any] = {
        "worker": "g-task-ab12", "name": "Task", "host": "devbox", "ssh": "devbox-alt", "cwd": "/Users/me/app",
        "remote_cwd": "/home/dev/app", "remote_dir": "/home/dev/.local/state/gbridge/g-task-ab12", "lead": "eli",
        "parent": "TAB-LEAD", "token_sha256": hash_token("secret"), "created_at": 1,
    }  # fmt: skip
    return HostWorker(**{**base, **changes})


def test_cwd_maps_to_the_longest_covering_checkout(tmp_path: Path) -> None:
    app = tmp_path / "app"
    (app / "svc" / "api").mkdir(parents=True)
    path_map = {str(app): "/home/dev/app", str(app / "svc"): "/srv/svc"}

    assert map_cwd_to_host_path(app, path_map) == "/home/dev/app"
    assert map_cwd_to_host_path(app / "svc" / "api", path_map) == "/srv/svc/api"
    assert map_cwd_to_host_path(tmp_path, path_map) is None
    assert map_cwd_to_host_path(tmp_path / "application", path_map) is None


def test_cwd_map_follows_symlinks_and_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert map_cwd_to_host_path(tmp_path / "link", {"~/real": "/h/real"}) == "/h/real"


def test_worker_host_watcher_must_name_a_watcher() -> None:
    watchers = [{"path": "~/claude-devbox.json", "name": "Claude · devbox"}]
    config = map_toml_to_config({"worker_host_watcher": "Claude · devbox", "watchers": watchers})
    assert config.worker_host_watcher == "Claude · devbox"
    with pytest.raises(ValueError, match="exactly one"):
        map_toml_to_config({"worker_host_watcher": "Claude · elsewhere", "watchers": watchers})
    with pytest.raises(ValueError, match="exactly one"):
        map_toml_to_config({"worker_host_watcher": "Claude · devbox", "watchers": watchers * 2})


def test_config_reads_worker_host_settings() -> None:
    config = map_toml_to_config({"worker_host": "devbox", "worker_path_map": {"~/app": "/home/dev/app"}})

    assert (config.worker_host, dict(config.worker_path_map)) == ("devbox", {"~/app": "/home/dev/app"})
    with pytest.raises(ValueError, match="absolute path"):
        map_toml_to_config({"worker_path_map": {"~/app": "app"}})


def test_worker_names_are_tmux_and_path_safe() -> None:
    name = make_worker_name("Temporal export: retry & backfill!")

    assert name.startswith("g-temporal-export-retry-backfill-")
    assert host_side.WORKER_RE.fullmatch(name)
    assert make_worker_name("Παράδειγμα").startswith("g-worker-")


def test_tmux_target_is_exact_and_quoted_for_zsh() -> None:
    assert map_worker_to_exact_target("g-task-ab12") == "'=g-task-ab12'"
    with pytest.raises(Exception, match="bad host worker"):
        map_worker_to_exact_target("g-x; rm -rf ~")


def test_attach_tunnels_to_the_worker_api_only() -> None:
    command = build_attach_command(_record(), 23456, 8766)

    assert command[:2] == ["ssh", "-t"]
    assert "ExitOnForwardFailure=yes" in command
    assert command[command.index("-R") + 1] == "127.0.0.1:23456:127.0.0.1:8766"
    assert command[-2] == "devbox-alt"
    assert "attach-session -t '=g-task-ab12'" in command[-1]
    assert "-f /dev/null" in command[-1]
    assert "printf %s 23456 > /home/dev/.local/state/gbridge/g-task-ab12/port" in command[-1]


@pytest.fixture
def short_state_dir(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A socket path must fit in 104 bytes: pytest's own temp dirs on macOS do not leave room."""
    directory = Path(tempfile.mkdtemp(prefix="b-", dir="/tmp"))
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(directory))
    yield directory
    shutil.rmtree(directory)


def _option(command: list[str], name: str) -> str | None:
    values = [command[i + 1].partition("=")[2] for i, word in enumerate(command[:-1]) if word == "-o"]
    names = [command[i + 1].partition("=")[0] for i, word in enumerate(command[:-1]) if word == "-o"]
    return values[names.index(name)] if name in names else None


def test_short_calls_share_one_master_per_host_and_tabs_by_default_keep_their_own_connection(
    short_state_dir: Path,
) -> None:
    short = map_remote_to_ssh(Remote(name="devbox", ssh="devbox-alt"), "true")
    attach = build_attach_command(_record(), 23456, 8766)
    other_host = map_remote_to_ssh(Remote(name="box-b", ssh="box-b"), "true")

    path = _option(short, "ControlPath")
    assert path is not None and path.startswith(f'"{short_state_dir}/ssh/')
    assert _option(short, "ControlMaster") == "auto"
    assert _option(short, "ControlPersist") == "300"
    assert _option(short, "ClearAllForwardings") == "yes"
    assert oct((short_state_dir / "ssh").stat().st_mode & 0o777) == "0o700"
    assert _option(attach, "ControlPath") == "none"
    assert _option(other_host, "ControlPath") != path


def test_tabs_share_only_on_a_remote_that_says_so() -> None:
    remotes = map_toml_to_config(
        {"remotes": [{"name": "devbox", "ssh": "devbox-alt"}, {"name": "box-b", "ssh": "box-b", "share_tabs": True}]}
    ).remotes

    assert [r.share_tabs for r in remotes] == [False, True]
    with pytest.raises(ValueError, match="share_tabs"):
        map_toml_to_config({"remotes": [{"name": "devbox", "ssh": "devbox-alt", "share_tabs": "yes"}]})


@pytest.mark.parametrize("state", ["/tmp/" + "d" * 70, "/tmp/" + "ж" * 35, "/tmp/100%", "/tmp/${HOME}"])
def test_a_socket_path_ssh_cannot_hold_means_no_sharing(monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", state)

    assert _option(map_remote_to_ssh(Remote(name="devbox", ssh="devbox-alt"), "true"), "ControlPath") == "none"


def test_a_socket_folder_that_cannot_be_made_means_no_sharing(short_state_dir: Path) -> None:
    (short_state_dir / "ssh").write_text("")

    command = map_remote_to_ssh(Remote(name="devbox", ssh="devbox-alt"), "true")

    assert _option(command, "ControlPath") == "none"
    assert _option(command, "ClearAllForwardings") == "yes"


@pytest.mark.parametrize("share", [False, True])
def test_tab_reattaches_until_the_session_is_gone_and_keeps_its_port_unless_ssh_failed(
    monkeypatch: pytest.MonkeyPatch, short_state_dir: Path, share: bool
) -> None:
    config = short_state_dir / "config.toml"
    config.write_text(f'[[remotes]]\nname = "devbox"\nssh = "devbox-alt"\nshare_tabs = {str(share).lower()}\n')
    monkeypatch.setenv("BRIDGE_CONFIG", str(config))
    calls: list[tuple[str, bool]] = []
    attaches: list[list[str]] = []
    attach_codes = iter([0, 255, 0])
    has_session = iter([(255, "Connection closed"), (255, ""), (1, "can't find session: =g-task-ab12")])
    ports = iter(range(30001, 30010))

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        kind = "attach" if "-t" in command[:2] else "has-session" if "has-session" in command[-1] else command[-1]
        calls.append((kind, bool(kwargs.get("start_new_session"))))
        if kind == "attach":
            attaches.append(command)
        code, stderr = (
            next(has_session) if kind == "has-session" else (next(attach_codes), "") if kind == "attach" else (0, "")
        )
        return subprocess.CompletedProcess(command, code, "", stderr)

    def nothing(*_: object) -> None:
        return None

    def read_record(worker: str) -> HostWorker:
        return _record(worker=worker)

    def pinned(*_: object) -> SimpleNamespace:
        return SimpleNamespace(ok=True)

    def removed(*_: object) -> dict[str, bool]:
        return {"ok": True}

    def next_port(*_: int) -> int:
        return next(ports)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(host_workers, "read_record", read_record)
    for name in ("write_record", "register_tab_instance", "delete_record"):
        monkeypatch.setattr(host_workers, name, nothing)
    monkeypatch.setattr(host_workers, "pin_restore_command", pinned)
    monkeypatch.setattr(host_workers, "run_on_host", removed)
    monkeypatch.setattr(host_workers.time, "sleep", nothing)
    monkeypatch.setattr(host_workers.random, "randint", next_port)

    assert host_workers.run_tab_loop("g-task-ab12", 8766, "agtermctl") == 0
    once = [("true", True)] * share + [("attach", False), ("has-session", True)]
    assert calls == once * 3
    forwards = [command[command.index("-R") + 1].split(":")[1] for command in attaches]
    assert forwards == ["30001", "30001", "30002"]
    short_path = _option(map_remote_to_ssh(Remote(name="devbox", ssh="devbox-alt"), "true"), "ControlPath")
    for command in attaches:
        assert _option(command, "ControlPath") == (short_path if share else "none")
        assert _option(command, "ControlMaster") == ("no" if share else None)


def test_cleanup_command_runs_whole_on_the_host() -> None:
    command = build_host_cleanup_command(_record())

    assert command == (
        "tmux -L gbridge -f /dev/null kill-session -t '=g-task-ab12'; rm -rf /home/dev/.local/state/gbridge/g-task-ab12"
    )


@pytest.mark.parametrize(
    ("code", "stderr", "state"),
    [
        (0, "", "present"),
        (1, "can't find session: =g-task-ab12", "gone"),
        (1, "no server running on /tmp/tmux-1001/gbridge", "gone"),
        (1, "zsh:1: g-task-ab12 not found", "unknown"),
        (255, "Connection closed", "unknown"),
    ],
)
def test_only_tmux_itself_can_say_a_session_is_gone(
    monkeypatch: pytest.MonkeyPatch, code: int, stderr: str, state: str
) -> None:
    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, code, "", stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert read_session_state(_record()) == state


def test_token_is_checked_against_its_hash_only(bridge_env: BridgeEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(bridge_env.state_dir))
    write_record(_record())
    stored = (bridge_env.state_dir / "host-workers" / "g-task-ab12.json").read_text()

    assert "secret" not in stored
    assert oct((bridge_env.state_dir / "host-workers" / "g-task-ab12.json").stat().st_mode & 0o777) == "0o600"
    assert authenticate("g-task-ab12", "secret") is not None
    assert authenticate("g-task-ab12", "wrong") is None
    assert authenticate("g-other-0000", "secret") is None
    assert authenticate("../../etc", "secret") is None


def test_host_settings_report_through_gbridge(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"pw": {}}}))
    (tmp_path / "sub").mkdir()

    settings = host_side.build_settings(str(tmp_path / "sub"), ["playwright@x"])

    assert set(settings["hooks"]) == set(host_side.HOOK_EVENTS)
    assert settings["hooks"]["Notification"][0]["matcher"] == "permission_prompt"
    command = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert command.endswith("/.local/bin/gbridge hook Stop")
    assert settings["enabledPlugins"] == {"playwright@x": False}
    assert settings["disabledMcpjsonServers"] == ["pw"]


def test_run_script_waits_for_the_tunnel_and_sets_path() -> None:
    script = host_side.build_run_script("/h/d", "/h/it's", "--flag")

    assert "export GBRIDGE_DIR='/h/d'" in script
    assert "cd '/h/it'\"'\"'s' || exit 1" in script
    assert '[ -s "$GBRIDGE_DIR/port" ] && break' in script
    assert ".nvm/versions/node/*/bin" in script
    assert script.index('. "$HOME/.config/gbridge/env.sh"') < script.index("exec claude")
    assert script.rstrip().endswith("\"$(cat '/h/d/first-prompt')\"")


def test_host_side_refuses_bad_names_and_missing_checkouts(tmp_path: Path) -> None:
    assert host_side.run({"op": "launch", "worker": "../x"})["ok"] is False
    missing = host_side.run({"op": "launch", "worker": "g-a-0000", "cwd": str(tmp_path / "none")})
    assert missing == {"ok": False, "error": f"{tmp_path / 'none'} does not exist on this host"}


def test_host_side_never_overwrites_a_gbridge_it_did_not_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / ".local/bin/gbridge"
    target.parent.mkdir(parents=True)
    target.write_text("someone else's tool\n")

    error = host_side.install_gbridge(f"{host_side.GBRIDGE_MARK}\nnew\n")

    assert error is not None and "left alone" in error
    assert target.read_text() == "someone else's tool\n"
    target.write_text(f"{host_side.GBRIDGE_MARK}\nold\n")
    assert host_side.install_gbridge(f"{host_side.GBRIDGE_MARK}\nnew\n") is None
    assert target.read_text().endswith("new\n")


def test_host_rule_files_replace_stale_copies_and_leave_the_rest_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    skill = tmp_path / ".claude/skills/bridge"
    skill.mkdir(parents=True)
    (skill / "review-doc.md").write_text("old spec\n")
    (skill / "SKILL.md").write_text("same\n")
    (skill / "host-only.md").write_text("the host's own\n")
    (tmp_path / ".team/overlay").mkdir(parents=True)
    (tmp_path / ".team/overlay/house.md").write_text("house\n")
    (tmp_path / ".team/overlay/house.md").chmod(0o644)
    os.mkfifo(tmp_path / ".team/overlay/pipe.md")
    same_before = (skill / "SKILL.md").stat()
    elsewhere = tmp_path / "checkout"
    elsewhere.mkdir()
    (tmp_path / ".linked").symlink_to(elsewhere)
    files = [
        {"path": ".claude/skills/bridge/review-doc.md", "text": "new spec\n", "mode": 0o644},
        {"path": ".claude/skills/bridge/SKILL.md", "text": "same\n", "mode": 0o644},
        {"path": ".team/overlay/reader-profile.md", "text": "reader\n", "mode": 0o600},
        {"path": ".team/overlay/house.md", "text": "house\n", "mode": 0o600},
        {"path": ".team/overlay/pipe.md", "text": "x\n", "mode": 0o600},
        {"path": ".linked/rules.md", "text": "x\n", "mode": 0o600},
        {"path": ".team/../../escape.md", "text": "x\n", "mode": 0o600},
    ]

    updated, warnings = host_side.sync_rule_files(files)

    assert updated == ["~/.claude/skills/bridge/review-doc.md", "~/.team/overlay/reader-profile.md"]
    assert (skill / "review-doc.md").read_text() == "new spec\n"
    same_after = (skill / "SKILL.md").stat()
    assert (same_after.st_ino, same_after.st_mtime_ns) == (same_before.st_ino, same_before.st_mtime_ns)
    assert (skill / "host-only.md").read_text() == "the host's own\n"
    overlay = tmp_path / ".team/overlay/reader-profile.md"
    assert overlay.read_text() == "reader\n" and overlay.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / ".team/overlay/house.md").stat().st_mode & 0o777 == 0o600
    assert not (elsewhere / "rules.md").exists() and not (tmp_path.parent / "escape.md").exists()
    assert warnings == [
        "~/.team/overlay/pipe.md is not a regular file here; left alone",
        "rule file '.team/../../escape.md' is not a plain home-relative path; skipped",
        "~/.linked/rules.md is or sits under a symlink here and differs; left alone",
    ]
    assert [f for f in tmp_path.rglob("*.tmp")] == []
    assert host_side.sync_rule_files(files)[0] == []


def test_host_rule_files_are_the_skill_and_overlay_text_files_under_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    skill = tmp_path / "checkout/agent/skills/bridge"
    skill.mkdir(parents=True)
    (skill / "review-doc.md").write_text("spec\n")
    (tmp_path / ".claude/skills").mkdir(parents=True)
    (tmp_path / ".claude/skills/bridge").symlink_to(skill)
    overlay = tmp_path / ".team/overlay"
    overlay.mkdir(parents=True)
    for name in ("house.md", "house.md.bak-20260101", "house.md.proposed", "house.md.orig", ".DS_Store"):
        (overlay / name).write_text(name)
    (overlay / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
    (overlay / "drafts").mkdir()
    config = Config(overlay_dir="~/.team/overlay")

    files, notes = read_host_rule_files(config)

    checker = (Path(host_side.__file__).parent / "review_doc_check.py").read_text()
    assert files == [
        {"path": ".claude/skills/bridge/review-doc.md", "text": "spec\n", "mode": 0o644},
        {"path": ".team/overlay/house.md", "text": "house.md", "mode": 0o600},
        {"path": ".claude/skills/bridge/review_doc_check.py", "text": checker, "mode": 0o644},
    ]
    assert notes == []
    outside = tmp_path.parent / f"{tmp_path.name}-overlay"
    outside.mkdir()
    (outside / "house.md").write_text("x")
    files, notes = read_host_rule_files(Config(overlay_dir=str(outside)))
    assert [f["path"] for f in files] == [
        ".claude/skills/bridge/review-doc.md", ".claude/skills/bridge/review_doc_check.py"
    ]  # fmt: skip
    assert notes == [f"{outside} is not under {tmp_path}, so it has no path on the host"]


def test_a_rule_file_linked_to_one_written_in_the_same_sync_is_left_alone_quietly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    skill = tmp_path / ".claude/skills/bridge"
    skill.mkdir(parents=True)
    (tmp_path / ".team/overlay").mkdir(parents=True)
    (tmp_path / ".team/overlay/skill.md").write_text("old house\n")
    (skill / "house.md").symlink_to(tmp_path / ".team/overlay/skill.md")
    files = [
        {"path": ".claude/skills/bridge/house.md", "text": "new house\n", "mode": 0o644},
        {"path": ".team/overlay/skill.md", "text": "new house\n", "mode": 0o600},
    ]

    assert host_side.sync_rule_files(files) == (["~/.team/overlay/skill.md"], [])
    assert (skill / "house.md").is_symlink()


def test_a_sync_on_the_host_reports_each_rule_file_by_its_hash_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    skill = tmp_path / ".claude/skills/bridge"
    skill.mkdir(parents=True)
    (skill / "review-doc.md").write_text("old spec\n")
    os.mkfifo(skill / "pipe.md")
    files = [
        {"path": ".claude/skills/bridge/review-doc.md", "text": "new spec\n", "mode": 0o644},
        {"path": ".claude/skills/bridge/pipe.md", "text": "x\n", "mode": 0o644},
    ]

    result = host_side.run({"op": "sync", "rule_files": files})

    assert result["ok"] and result["updated"] == ["~/.claude/skills/bridge/review-doc.md"]
    assert result["hashes"] == {
        ".claude/skills/bridge/review-doc.md": hashlib.sha256(b"new spec\n").hexdigest(),
        ".claude/skills/bridge/pipe.md": None,
    }
    assert result["warning"] == "~/.claude/skills/bridge/pipe.md is not a regular file here; left alone"


def _stub_remote_home(monkeypatch: pytest.MonkeyPatch, remote_home: Path) -> list[str]:
    """run_on_host with the host-side script run here under a home of its own, as ssh would run it there."""
    reached: list[str] = []

    def run_on_host(remote: Remote, payload: dict[str, Any], timeout_s: int = 0) -> dict[str, Any]:
        reached.append(remote.name)
        if remote.name == "asleep":
            return {"ok": False, "unknown": True, "error": "ssh to asleep timed out after 60s"}
        with monkeypatch.context() as m:
            m.setenv("HOME", str(remote_home))
            return json.loads(json.dumps(host_side.run(payload)))

    monkeypatch.setattr(host_workers, "run_on_host", run_on_host)
    return reached


def test_sync_rules_brings_a_stale_remote_copy_up_to_the_macs_and_checks_it_by_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "mac"
    monkeypatch.setenv("HOME", str(home))
    (home / ".claude/skills/bridge").mkdir(parents=True)
    (home / ".claude/skills/bridge/review-doc.md").write_text("spec of today\n")
    (home / ".team/overlay").mkdir(parents=True)
    (home / ".team/overlay/reader-profile.md").write_text("reader\n")
    remote_home = tmp_path / "devbox"
    (remote_home / ".claude/skills/bridge").mkdir(parents=True)
    (remote_home / ".claude/skills/bridge/review-doc.md").write_text("spec of last week\n")
    reached = _stub_remote_home(monkeypatch, remote_home)
    config = Config(overlay_dir="~/.team/overlay", remotes=(Remote("devbox", "devbox"),))

    lines, in_step = sync_rules_to_remotes(config)

    assert in_step and reached == ["devbox"]
    assert (remote_home / ".claude/skills/bridge/review-doc.md").read_text() == "spec of today\n"
    assert (remote_home / ".team/overlay/reader-profile.md").read_text() == "reader\n"
    checker = remote_home / ".claude/skills/bridge/review_doc_check.py"
    assert checker.read_text() == (Path(host_side.__file__).parent / "review_doc_check.py").read_text()
    assert lines == [
        "devbox: updated ~/.claude/skills/bridge/review-doc.md",
        "devbox: updated ~/.team/overlay/reader-profile.md",
        "devbox: updated ~/.claude/skills/bridge/review_doc_check.py",
        "devbox: 3 rule files match the Mac by sha256",
    ]
    assert sync_rules_to_remotes(config) == (["devbox: 3 rule files match the Mac by sha256"], True)


def test_sync_rules_warns_of_a_remote_it_cannot_reach_or_whose_copy_still_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "mac"
    monkeypatch.setenv("HOME", str(home))
    (home / ".claude/skills/bridge").mkdir(parents=True)
    (home / ".claude/skills/bridge/review-doc.md").write_text("spec of today\n")
    remote_home = tmp_path / "devbox"
    (remote_home / ".claude").mkdir(parents=True)
    elsewhere = tmp_path / "devbox-checkout"
    elsewhere.mkdir()
    (elsewhere / "review-doc.md").write_text("spec of last week\n")
    (remote_home / ".claude/skills").mkdir()
    (remote_home / ".claude/skills/bridge").symlink_to(elsewhere)
    reached = _stub_remote_home(monkeypatch, remote_home)
    config = Config(remotes=(Remote("asleep", "asleep"), Remote("devbox", "devbox")))

    lines, in_step = sync_rules_to_remotes(config)

    assert not in_step and reached == ["asleep", "devbox"]
    assert (elsewhere / "review-doc.md").read_text() == "spec of last week\n"
    assert lines[0] == "asleep: not synced: ssh to asleep timed out after 60s"
    assert lines[-1] == (
        "devbox: differs from the Mac after the sync: ~/.claude/skills/bridge/review-doc.md, "
        "~/.claude/skills/bridge/review_doc_check.py"
    )


def test_sync_rules_still_sends_the_skill_when_the_overlay_is_outside_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "mac"
    monkeypatch.setenv("HOME", str(home))
    (home / ".claude/skills/bridge").mkdir(parents=True)
    (home / ".claude/skills/bridge/review-doc.md").write_text("spec of today\n")
    outside = tmp_path / "team-overlay"
    outside.mkdir()
    (outside / "house.md").write_text("x")
    remote_home = tmp_path / "devbox"
    remote_home.mkdir()
    reached = _stub_remote_home(monkeypatch, remote_home)
    config = Config(overlay_dir=str(outside), remotes=(Remote("devbox", "devbox"),))

    lines, in_step = sync_rules_to_remotes(config)

    assert in_step and reached == ["devbox"]
    assert lines[0] == f"rule files: {outside} is not under {home}, so it has no path on the host"
    assert lines[-1] == "devbox: 2 rule files match the Mac by sha256"
    assert (remote_home / ".claude/skills/bridge/review-doc.md").read_text() == "spec of today\n"


def test_sync_rules_over_the_cap_sends_nothing_and_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".team/overlay").mkdir(parents=True)
    (tmp_path / ".team/overlay/a.md").write_text("a" * 1_000_000)
    reached = _stub_remote_home(monkeypatch, tmp_path / "devbox")

    lines, in_step = sync_rules_to_remotes(Config(overlay_dir="~/.team/overlay", remotes=(Remote("devbox", "devbox"),)))

    assert not in_step and reached == []
    assert lines == [
        "rule files: rule files come to over 1000000 bytes, the cap; none was sent",
        "nothing was sent",
    ]


def test_sync_rules_with_no_remotes_reaches_none(monkeypatch: pytest.MonkeyPatch) -> None:
    reached = _stub_remote_home(monkeypatch, Path("/nonexistent"))

    assert sync_rules_to_remotes(Config()) == (["no remotes configured"], True)
    assert reached == []


def test_host_rule_files_over_the_cap_send_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    overlay = tmp_path / ".team/overlay"
    overlay.mkdir(parents=True)
    checker = len((Path(host_side.__file__).parent / "review_doc_check.py").read_bytes())
    (overlay / "near.md").write_text("n" * (1_000_000 - checker + 1))

    files, notes = read_host_rule_files(Config(overlay_dir="~/.team/overlay"))

    assert (files, notes) == ([], ["rule files come to over 1000000 bytes, the cap; none was sent"]), "checker counts"
    (overlay / "near.md").unlink()
    (overlay / "a.md").write_text("a" * 600_000)
    (overlay / "b.html").write_text("b" * 600_000)

    files, notes = read_host_rule_files(Config(overlay_dir="~/.team/overlay"))

    assert files == []
    assert notes == ["rule files come to over 1000000 bytes, the cap; none was sent"]
    (overlay / "b.html").unlink()
    (overlay / "locked.md").write_text("x")
    (overlay / "locked.md").chmod(0o000)

    files, notes = read_host_rule_files(Config(overlay_dir="~/.team/overlay"))

    assert [f["path"] for f in files] == [".team/overlay/a.md", ".claude/skills/bridge/review_doc_check.py"]
    assert len(notes) == 1 and notes[0].startswith(f"{overlay / 'locked.md'} could not be read")


def test_gbridge_source_is_marked_for_the_installer() -> None:
    assert host_side.GBRIDGE_MARK in Path(gbridge.__file__).read_text()


def test_gbridge_hook_trims_the_payload_and_honours_the_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sent: list[dict[str, Any]] = []

    def fake_post(home: Path, path: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        sent.append(body)
        return {"output": "ctx", "hint_due": int(time.time()) + 30}

    monkeypatch.setattr(gbridge, "post", fake_post)
    payload = {"session_id": "s", "cwd": str(tmp_path), "tool_response": "x" * 10_000, "prompt": "p" * 100_000}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    gbridge.run_hook(tmp_path, "UserPromptSubmit")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    gbridge.run_hook(tmp_path, "PostToolUse")

    assert len(sent) == 1
    assert sent[0]["event"] == "UserPromptSubmit"
    assert "tool_response" not in sent[0]["payload"]
    assert len(sent[0]["payload"]["prompt"]) == gbridge.TEXT_MAX_CHARS
    assert capsys.readouterr().out == "ctx"


def test_gbridge_hook_fails_open_when_the_tunnel_is_down(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "port").write_text("1")
    (tmp_path / "token").write_text("t")
    (tmp_path / "worker").write_text("g-a-0000")
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))

    gbridge.run_hook(tmp_path, "Stop")

    assert "hook Stop" in (tmp_path / "gbridge.log").read_text()


def test_gbridge_says_plainly_when_the_mac_is_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "port").write_text("1")
    (tmp_path / "token").write_text("t")
    (tmp_path / "worker").write_text("g-a-0000")
    monkeypatch.setenv("GBRIDGE_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    code = gbridge.main(["inbox"])

    assert code == gbridge.UNREACHABLE_EXIT
    assert "not reachable" in capsys.readouterr().err
    assert gbridge.main(["claim", "x"]) == 2


def test_gbridge_reads_stdin_only_for_keep_without_text() -> None:
    assert gbridge.reads_stdin(["keep"])
    assert not gbridge.reads_stdin(["keep", "notes"])
    assert not gbridge.reads_stdin(["keep", "--show"])
    assert not gbridge.reads_stdin(["focus", "x"])
    assert not gbridge.reads_stdin(["inbox"])


def test_bridge_wording_becomes_gbridge() -> None:
    text = "run `bridge ack 12` or bridge reply 12; bridge post stays; ~/bin/bridge send is a path"

    assert convert_bridge_commands_to_gbridge(text) == (
        "run `gbridge ack 12` or gbridge reply 12; bridge post stays; ~/bin/bridge send is a path"
    )


def test_cli_allows_only_the_worker_commands() -> None:
    assert run_host_cli(_record(), ["post", "done", "x"], "")["code"] == 2
    assert run_host_cli(_record(), ["send", "eli", "x"], "")["stderr"] == (
        "gbridge: the worker's tab on the Mac is not running\n"
    )


def test_hook_from_a_host_worker_stands_as_its_tab_process(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in bridge_env.env.items():
        monkeypatch.setenv(key, value)
    loop = spawn_process()
    start = process_start_ms(loop)
    assert start is not None
    caller = HookCaller(loop, start, Surface("PANE-H", "TAB-H", "WS"), "host:devbox", None)
    payload: dict[str, object] = {"session_id": "conv-h", "cwd": "/home/dev/app", "transcript_path": "/h/t.jsonl"}
    conn = connect(bridge_env.db)

    started = handle_hook(
        conn, "SessionStart", {**payload, "source": "startup"}, {}, load_config(), time.time_ns(), caller=caller
    )
    prompt = handle_hook(
        conn, "UserPromptSubmit", {**payload, "prompt": "go"}, {}, load_config(), time.time_ns(), caller=caller
    )

    row = conn.execute("SELECT * FROM instances").fetchone()
    conversation = conn.execute("SELECT transcript_path FROM conversations").fetchone()
    assert (row["instance_id"], row["kind"], row["surface"], row["status"]) == (
        f"{loop}@{start}",
        "host:devbox",
        "PANE-H",
        "active",
    )
    assert conversation["transcript_path"] is None
    assert started.pin_conversation is None
    assert prompt.hint_due is not None
    assert not (bridge_env.state_dir / "hints" / str(loop)).exists()


def test_worker_api_refuses_a_wrong_token(bridge_env: BridgeEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(bridge_env.state_dir))
    write_record(_record())
    server = start_worker_api(map_toml_to_config({}), port=0)
    port = server.server_address[1]

    def call(token: str) -> int:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/cli",
            data=json.dumps({"argv": ["inbox"], "stdin": ""}).encode(),
            headers={"Authorization": f"Bearer {token}", "X-Bridge-Worker": "g-task-ab12"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return int(response.status)
        except urllib.error.HTTPError as exc:
            return exc.code

    try:
        assert call("wrong") == 403
        assert call("secret") == 200
    finally:
        server.shutdown()


def test_closed_host_worker_resumes_on_the_host() -> None:
    sessions = [TabSession("PANE-H", "conv-1", 1, False, "idle", None, None)]

    records = map_host_tab_to_closed_records(_record(), sessions)

    assert [r.resume_command for r in records] == [
        "ssh -t devbox-alt 'cd /home/dev/app && ~/.local/bin/claude --resume conv-1'"
    ]


def test_a_host_worker_gets_the_model_and_effort_a_worker_on_this_mac_gets(tmp_path: Path) -> None:
    assert read_local_model_flags(tmp_path) == "--model default"
    (tmp_path / "settings.json").write_text(json.dumps({"model": "claude-opus-5-5", "effortLevel": "high"}))
    assert read_local_model_flags(tmp_path) == "--model claude-opus-5-5 --effort high"
    (tmp_path / "settings.json").write_text(json.dumps({"model": "opus[1m]", "effortLevel": "turbo"}))
    assert read_local_model_flags(tmp_path) == "--model 'opus[1m]'"
    (tmp_path / "settings.json").write_text("[]")
    assert read_local_model_flags(tmp_path) == "--model default"


def test_a_closed_host_worker_resumes_with_the_model_it_started_with() -> None:
    sessions = [TabSession("PANE-H", "conv-1", 1, False, "idle", None, None)]

    records = map_host_tab_to_closed_records(replace(_record(), model_flags="--model default --effort high"), sessions)

    assert [r.resume_command for r in records] == [
        "ssh -t devbox-alt 'cd /home/dev/app && ~/.local/bin/claude --model default --effort high --resume conv-1'"
    ]


def test_remote_name_lookup() -> None:
    config = map_toml_to_config({"remotes": [{"name": "devbox", "ssh": "devbox-alt"}]})

    assert find_remote(config, "devbox") == Remote(name="devbox", ssh="devbox-alt")
    with pytest.raises(Exception, match="not a \\[\\[remotes\\]\\] name"):
        find_remote(config, "box9")


def test_laptop_cap_counts_only_agents_working_on_this_mac(bridge_env: BridgeEnv) -> None:
    from bridge.workers import count_busy_agents, count_busy_host_agents

    conn = connect(bridge_env.db)
    for n, kind, status in ((1, "interactive", "active"), (2, "host:devbox", "active"), (3, "host:devbox", "idle")):
        conn.execute(
            "INSERT INTO instances (instance_id, pid, proc_start_ms, kind, status, first_seen_at, last_observed_at) "
            "VALUES (?, ?, 1, ?, ?, 1, 1)",
            (f"{n}@1", n, kind, status),
        )

    assert count_busy_agents(conn) == 1
    assert count_busy_host_agents(conn) == {"devbox": 1}


@pytest.mark.parametrize(
    ("status", "since_s", "state"),
    [("active", 0, "working"), ("completed", 0, "rest"), ("blocked", 10, "waiting"), ("blocked", 3600, "stuck")],
)
def test_a_host_workers_state_comes_from_its_hooks(status: str, since_s: int, state: str) -> None:
    from bridge.guildview import Evidence, map_host_worker_to_state

    now = 10_000_000
    evidence = Evidence(None, True, None, None, None, (), (), ())
    tab: dict[str, object] = {"status": status, "statusChangedAt": (now - since_s * 1000) / 1000}

    result = map_host_worker_to_state(tab, "devbox", evidence, now)

    assert result["state"] == state
    assert "devbox" in str(result["why"])


CONV = "0b5c2f4e-1d2a-4c3b-9e8f-7a6b5c4d3e2f"


def test_a_worker_binds_only_conversations_nobody_else_has(
    bridge_env: BridgeEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bridge.host_workers import bind_conversation, read_bound_conversations

    monkeypatch.setenv("BRIDGE_STATE_DIR", str(bridge_env.state_dir))
    write_record(_record())
    conn = connect(bridge_env.db)
    other = "11111111-2222-4333-8444-5555555555ab"
    conn.execute(
        "INSERT INTO instances (instance_id, pid, proc_start_ms, first_seen_at, last_observed_at) "
        "VALUES ('9@1', 9, 1, 1, 1)"
    )
    conn.execute(
        "INSERT INTO conversations (conversation_id, instance_id, first_seen_at, last_seen_at) VALUES (?, '9@1', 1, 1)",
        (other,),
    )

    assert bind_conversation(conn, "g-task-ab12", CONV)
    assert bind_conversation(conn, "g-task-ab12", CONV)
    assert not bind_conversation(conn, "g-task-ab12", other)
    assert not bind_conversation(conn, "g-task-ab12", "user")
    assert not bind_conversation(conn, "g-task-ab12", "system")
    assert read_bound_conversations("g-task-ab12") == [CONV]


def _host_worker_with_loop(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, monkeypatch: pytest.MonkeyPatch
) -> HostWorker:
    for key, value in bridge_env.env.items():
        monkeypatch.setenv(key, value)
    loop = spawn_process()
    start = process_start_ms(loop)
    assert start is not None
    record = _record(loop_pid=loop, loop_start_ms=start, pane="PANE-H", agterm_session="TAB-H", tab="TAB-H")
    write_record(record)
    return record


def test_a_hook_for_someone_elses_conversation_does_nothing(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bridge.worker_api import handle_host_hook

    record = _host_worker_with_loop(bridge_env, spawn_process, monkeypatch)

    forged = handle_host_hook(record, load_config(), "SessionStart", {"session_id": "user", "source": "compact"}, None)
    real = handle_host_hook(record, load_config(), "SessionStart", {"session_id": CONV, "source": "startup"}, None)

    conn = connect(bridge_env.db)
    assert forged["output"] == "" and "hint_due" not in forged
    assert "output" in real
    assert [r["conversation_id"] for r in conn.execute("SELECT conversation_id FROM instances")] == [CONV]


def test_a_replayed_hook_is_dated_back_so_a_newer_one_wins(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bridge.worker_api import handle_host_hook

    record = _host_worker_with_loop(bridge_env, spawn_process, monkeypatch)
    payload: dict[str, object] = {"session_id": CONV}
    handle_host_hook(record, load_config(), "SessionStart", {**payload, "source": "startup"}, None)
    handle_host_hook(record, load_config(), "UserPromptSubmit", {**payload, "prompt": "go"}, None)

    handle_host_hook(record, load_config(), "Stop", payload, None, age_ns=60 * 10**9)

    conn = connect(bridge_env.db)
    assert conn.execute("SELECT status FROM instances").fetchone()["status"] == "active"


def test_a_reopened_tab_takes_over_the_workers_conversation_at_once(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from bridge.host_workers import register_tab_instance
    from bridge.worker_api import handle_host_hook

    record = _host_worker_with_loop(bridge_env, spawn_process, monkeypatch)
    handle_host_hook(record, load_config(), "SessionStart", {"session_id": CONV, "source": "startup"}, None)
    new_loop = spawn_process()
    new_start = process_start_ms(new_loop)
    assert new_start is not None
    reopened = replace(record, loop_pid=new_loop, loop_start_ms=new_start, pane="PANE-2", agterm_session="TAB-2")

    register_tab_instance(reopened)

    conn = connect(bridge_env.db)
    row = conn.execute("SELECT * FROM instances WHERE pid = ?", (new_loop,)).fetchone()
    assert (row["conversation_id"], row["surface"], row["kind"], row["status"]) == (
        CONV,
        "PANE-2",
        "host:devbox",
        "idle",
    )


def test_cleanup_keeps_files_while_the_session_state_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    directory = Path(host_side.worker_dir("g-a-0000"))
    directory.mkdir(parents=True)
    state = "unknown"

    def read_state(worker: str) -> str:
        return state

    monkeypatch.setattr(host_side, "session_state", read_state)

    result = host_side.remove("g-a-0000")

    assert result["ok"] is False
    assert directory.is_dir()
    state = "gone"
    assert host_side.remove("g-a-0000") == {"ok": True, "error": ""}
    assert not directory.exists()
    assert not directory.parent.exists()


def test_a_lost_stop_waits_for_the_replayer_and_a_newer_hook_supersedes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned: list[list[str]] = []
    sent: list[dict[str, Any]] = []

    def spawn(argv: list[str], **_: object) -> None:
        spawned.append(argv)

    def down(home: Path, path: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        raise OSError("tunnel down")

    def up(home: Path, path: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        sent.append(body)
        return {}

    monkeypatch.setattr(gbridge.subprocess, "Popen", spawn)

    monkeypatch.setattr(gbridge, "post", down)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": CONV})))
    gbridge.run_hook(tmp_path, "Stop")

    pending = json.loads((tmp_path / gbridge.PENDING_FILE).read_text())
    assert pending["event"] == "Stop"
    assert spawned and spawned[0][-1] == "_replay"

    monkeypatch.setattr(gbridge, "post", up)
    gbridge.replay_pending(tmp_path)
    assert sent[0]["event"] == "Stop" and sent[0]["age_ns"] >= 0 and "at_ns" not in sent[0]
    assert not (tmp_path / gbridge.PENDING_FILE).exists()

    monkeypatch.setattr(gbridge, "post", down)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": CONV})))
    gbridge.run_hook(tmp_path, "Stop")
    monkeypatch.setattr(gbridge, "post", up)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": CONV})))
    gbridge.run_hook(tmp_path, "UserPromptSubmit")
    assert not (tmp_path / gbridge.PENDING_FILE).exists()


def test_a_host_worker_gets_the_installs_codex_note_with_its_lead_named(tmp_path: Path) -> None:
    assert "Codex" not in build_host_brief_note(Config(), "devbox", "g-x-1", "alex", "/home/dev/acme-co")
    (tmp_path / "host-worker-codex.md").write_text("Codex does not run here: ask {lead} to run it on the Mac.\n")
    note = build_host_brief_note(Config(overlay_dir=str(tmp_path)), "devbox", "g-x-1", "alex", "/home/dev/acme-co")
    assert "Codex does not run here: ask alex to run it on the Mac.\n\nFiles someone" in note


def test_a_worker_model_overrides_the_settings_model_and_keeps_its_effort(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text('{"model": "fable", "effortLevel": "high"}')
    assert read_local_model_flags(tmp_path, model_override="claude-opus-5-5") == "--model claude-opus-5-5 --effort high"


def test_a_host_tab_restarts_through_the_bridge_on_path_not_the_release_that_opened_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = tmp_path / "bin" / "bridge"
    bridge.parent.mkdir()
    bridge.write_text("#!/bin/sh\n")
    bridge.chmod(0o755)
    monkeypatch.setenv("PATH", str(bridge.parent))

    command = build_tab_command("devbox-w1")

    assert f"{bridge} _host-tab devbox-w1'" in command
    assert sys.executable not in command


WORKTREE = "/home/dev/.local/state/gbridge/worktrees/g-task-ab12"


def test_a_closed_host_workers_line_gives_reopen_its_name_worktree_and_model() -> None:
    host = replace(
        _record(), remote_cwd=f"{WORKTREE}/svc", remote_checkout="/home/dev/app", remote_worktree=WORKTREE,
        model_flags="--model claude-opus-5-5 --effort high",
    )  # fmt: skip
    [record] = map_host_tab_to_closed_records(host, [TabSession("PANE-H", CONV, 1, False, "idle", None, None)])

    assert map_resume_command_to_host_resume(record.resume_command, CONV) == HostResume(
        "devbox-alt", "g-task-ab12", "/home/dev/app", f"{WORKTREE}/svc", "--model claude-opus-5-5 --effort high"
    )
    assert map_resume_command_to_host_resume(record.resume_command, "another-conversation") is None


def test_a_line_from_before_host_worktrees_or_outside_them_gives_no_reopen() -> None:
    shared = f"ssh -t devbox-alt 'cd /home/dev/app && ~/.local/bin/claude --resume {CONV}'"
    elsewhere = replace(_record(), remote_cwd="/srv/w", remote_checkout="/home/dev/app", remote_worktree="/srv/w")
    [record] = map_host_tab_to_closed_records(elsewhere, [TabSession("PANE-H", CONV, 1, False, "idle", None, None)])

    assert map_resume_command_to_host_resume(shared, CONV) is None
    assert map_resume_command_to_host_resume(record.resume_command, CONV) is None
    assert map_resume_command_to_host_resume("cd /x && claude --resume " + CONV, CONV) is None


def test_a_resumed_run_script_resumes_instead_of_reading_a_brief() -> None:
    script = host_side.build_run_script("/h/d", "/h/w", "--flag", CONV)

    assert script.rstrip().endswith(f"claude --flag --settings '/h/d/settings.json' --resume '{CONV}'")
    assert "first-prompt" not in script


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _relaunch_payload(checkout: Path, cwd: str) -> dict[str, Any]:
    return {
        "op": "launch", "worker": "g-a-0000", "resume": CONV, "checkout": str(checkout), "cwd": cwd, "token": "tok",
        "gbridge": f"{host_side.GBRIDGE_MARK}\n", "claude_flags": "--model default", "globs": [],
        "disabled_plugins": ["playwright@x"], "mapped_root": str(checkout),
        "rule_files": [{"path": ".claude/skills/bridge/review-doc.md", "text": "new spec\n", "mode": 0o644}],
    }  # fmt: skip


def test_a_reopened_host_worker_gets_gbridge_in_its_old_worktree_restored_at_the_same_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude.json").write_text("{}")
    sessions: list[list[str]] = []

    def fake_tmux(*args: str) -> subprocess.CompletedProcess[str]:
        if args[0] == "new-session":
            sessions.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    def gone(worker: str) -> str:
        return "gone"

    def which(name: str) -> str:
        return f"/usr/bin/{name}"

    monkeypatch.setattr(host_side, "session_state", gone)
    monkeypatch.setattr(host_side, "tmux", fake_tmux)
    monkeypatch.setattr(host_side.shutil, "which", which)
    shared = tmp_path / "app"
    (shared / "svc").mkdir(parents=True)
    (shared / "svc" / "a.py").write_text("")
    for args in (["init", "-q"], ["add", "svc"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "svc"]):
        _git(shared, *args)
    _, cwd = host_side.add_worktree("g-a-0000", str(shared))  # type: ignore[misc]
    worktree = Path(host_side.worktree_dir("g-a-0000"))
    _git(shared, "worktree", "remove", str(worktree))
    shared = shared.resolve()
    payload = _relaunch_payload(shared, f"{worktree}/svc")

    cwds = {7: str(worktree), 8: "/elsewhere"}

    def processes() -> dict[int, tuple[int, str]]:
        return {7: (1, "claude --continue"), 8: (1, f"claude --resume {CONV}"), 9: (1, "bash")}

    def runs_in(pid: int, directory: str) -> bool:
        return cwds.get(pid, "/").startswith(directory)

    monkeypatch.setattr(host_side, "read_processes", processes)
    monkeypatch.setattr(host_side, "runs_in", runs_in)
    running = host_side.run(payload)
    assert running == {"ok": False, "error": f"a Claude already runs in {worktree}: pid 7"}
    assert not worktree.exists() and sessions == []

    cwds[7] = "/elsewhere"
    started = host_side.run(payload)

    directory = Path(host_side.worker_dir("g-a-0000"))
    assert started == {
        "ok": True, "dir": str(directory), "home": str(tmp_path), "cwd": f"{worktree}/svc", "checkout": str(shared),
        "worktree": str(worktree), "restored": True, "updated": ["~/.claude/skills/bridge/review-doc.md"],
    }  # fmt: skip
    assert (tmp_path / ".claude/skills/bridge/review-doc.md").read_text() == "new spec\n"
    assert (worktree / "svc" / "a.py").exists() and cwd == str(worktree)
    assert sessions == [
        ["new-session", "-d", "-s", "g-a-0000", "-x", "200", "-y", "50", "-c", f"{worktree}/svc",
         f"bash -l {directory}/run.sh"]
    ]  # fmt: skip
    assert (directory / "token").read_text() == "tok"
    assert not (directory / "brief.md").exists() and not (directory / "first-prompt").exists()
    assert f"--resume '{CONV}'" in (directory / "run.sh").read_text()
    settings = json.loads((directory / "settings.json").read_text())
    assert set(settings["hooks"]) == set(host_side.HOOK_EVENTS)
    trusted = json.loads((tmp_path / ".claude.json").read_text())["projects"]
    assert trusted[str(worktree)] == {"hasTrustDialogAccepted": True}
    assert (tmp_path / ".local" / "bin" / "gbridge").exists()

    assert host_side.run(payload) == {"ok": False, "error": f"{directory} already exists here"}


def test_a_configured_host_sync_dir_goes_with_its_subfolders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    skill = tmp_path / ".claude/skills/slides"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("slides\n")
    (skill / "references/fonts.md").write_text("fonts\n")
    (skill / ".git").mkdir()
    (skill / ".git/HEAD").write_text("ref\n")
    (skill / "icon.jpeg").write_bytes(b"\xff\xd8\xff\xe0\x00")

    files, notes = read_host_rule_files(Config(host_sync_dirs=("~/.claude/skills/slides",)))

    paths = [f["path"] for f in files]
    assert ".claude/skills/slides/SKILL.md" in paths and ".claude/skills/slides/references/fonts.md" in paths
    assert not any(".git" in p or "icon.jpeg" in p for p in paths), "hidden folders and binaries stay"
    assert notes == []
    assert ".claude/skills/slides/SKILL.md" not in [f["path"] for f in read_host_rule_files(Config())[0]]
