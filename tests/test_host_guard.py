import contextlib
import io
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, cast

import pytest
from conftest import BridgeEnv, SpawnProcess
from test_board import Pair

from bridge import gbridge, host_side
from bridge.agent_prefix import LINEAR_ISSUE_TOOLS
from bridge.config import load_config, map_toml_to_config
from bridge.db import connect
from bridge.gitinfo import RepoFacts, read_repo_facts
from bridge.guard import (
    CLOSE,
    COMMAND,
    MERGE,
    PUSH,
    WRAPPED,
    HostCheckout,
    HostRepoReader,
    Unreadable,
    make_host_repo_reader,
    map_command_to_targets,
)
from bridge.hooks import handle_pre_tool_use
from bridge.host_workers import HostWorker, find_mapped_root, hash_token, list_readable_roots, write_record
from bridge.install import list_guard_globs
from bridge.procinfo import process_start_ms
from bridge.worker_api import handle_host_hook, start_worker_api
from bridge.workers import TabSession, map_host_tab_to_closed_records

CONV = "0b5c2f4e-1d2a-4c3b-9e8f-7a6b5c4d3e2f"
HOST_FACTS = RepoFacts("/home/dev/web", "feat/x", {"origin": "Acme/Web", "fork": "me/web"})


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    _git(path, "remote", "add", "origin", "https://x-token:secret@github.com/Acme/Web.git")
    _git(path, "commit", "-q", "--allow-empty", "-m", "start")
    return path


def _host_reader(facts: RepoFacts, cwd: str, roots: tuple[str, ...] = ("/home/dev",)) -> HostRepoReader:
    return make_host_repo_reader([HostCheckout(cwd, facts, facts.repo_root)], roots, "/home/dev")


def _keys(command: str, cwd: str, unreadable: list[Unreadable] | None = None) -> list[str]:
    targets = map_command_to_targets(command, cwd, unreadable, _host_reader(HOST_FACTS, cwd), strict=True)
    return [t.key for t in targets]


def test_gbridge_reports_the_git_facts_the_mac_reads_and_no_config_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".gitconfig").write_text("[push]\n\tdefault = current\n")
    web = _repo(tmp_path / "web")
    _git(web, "remote", "add", "fork", "git@gitlab.com:me/web.git")
    _git(web, "config", "branch.main.remote", "fork")
    _git(web, "worktree", "add", "-q", "--detach", str(tmp_path / "wt"), "HEAD")
    (tmp_path / "wt" / "sub").mkdir()

    reported = gbridge.read_git_info(str(tmp_path / "wt" / "sub"))
    local = read_repo_facts(str(tmp_path / "wt" / "sub"))

    assert reported is not None and local is not None
    assert {k: v for k, v in reported.items() if k != "repo"} == asdict(local)
    assert reported["repo"] == local.repo == "Acme/Web"
    assert reported["remotes"] == {"origin": "Acme/Web", "fork": None}
    assert (reported["tracking"], reported["push_default"], reported["custom_push"]) == (
        {"main": "fork"},
        "current",
        False,
    )
    assert "secret" not in json.dumps(reported)
    (tmp_path / ".gitconfig").write_text("[push]\n\tdefault = current\n[remote]\n\tpushDefault = fork\n")
    assert gbridge.read_git_info(str(web))["custom_push"] is True  # type: ignore[index]
    assert read_repo_facts(str(web)).custom_push is True  # type: ignore[union-attr]
    (tmp_path / ".gitconfig").write_text("")
    _git(web, "config", "remote.origin.pushurl", "x")
    assert gbridge.read_git_info(str(web))["custom_remote"] is True  # type: ignore[index]
    assert gbridge.read_git_info(str(web))["custom_push"] is False  # type: ignore[index]


def test_on_a_host_the_guard_reads_the_reported_checkout_and_nothing_of_the_macs(tmp_path: Path) -> None:
    mac_repo = _repo(tmp_path / "mac")

    assert _keys("git push", "/home/dev/web/sub") == ["branch:acme/web:feat/x"]
    assert _keys("git push fork HEAD", "/home/dev/web") == ["branch:me/web:feat/x"]
    assert _keys("gh pr merge 12", "/home/dev/web") == ["pr:acme/web#12"]
    assert _keys("gh pr merge 12 -R Acme/Api", "/tmp") == ["pr:acme/api#12"]
    assert _keys("git push origin --delete feat/old", "/home/dev/web") == ["branch:acme/web:feat/old"]
    unreadable: list[Unreadable] = []
    assert _keys(f"cd {mac_repo} && git push origin main", "/home/dev/web", unreadable) == []
    assert _keys("gh pr merge 99999", "/home/dev/scratch", unreadable) == []
    assert _keys("gh api -X PUT 'repos/{owner}/{repo}/pulls/5/merge'", "/home/dev/web", unreadable) == []
    assert _keys("cd vendor/lib && gh pr merge 12", "/home/dev/web", unreadable) == []
    # gbridge reports .. on its own; the Mac never takes a parent's facts from the child's.
    assert _keys("cd .. && git push", "/home/dev/web/sub", unreadable) == []
    assert [u.text for u in unreadable] == [
        "git push origin main",
        "gh pr merge 99999",
        "gh api -X PUT repos/{owner}/{repo}/pulls/5/merge",
        "gh pr merge 12",
        "git push",
    ]


def test_a_host_push_or_merge_naming_no_target_is_refused_and_on_the_mac_it_still_passes(
    bridge_env: BridgeEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in bridge_env.env.items():
        monkeypatch.setenv(key, value)
    config = load_config()
    conn = connect(bridge_env.db)
    payload: dict[str, object] = {
        "session_id": CONV, "cwd": "/home/dev/scratch", "tool_name": "Bash",
        "tool_input": {"command": "gh pr merge 99999"},
    }  # fmt: skip

    on_host = handle_pre_tool_use(conn, payload, config, _host_reader(HOST_FACTS, "/home/dev/scratch"))
    on_mac = handle_pre_tool_use(conn, {**payload, "cwd": str(bridge_env.root)}, config)
    named = handle_pre_tool_use(
        conn, {**payload, "tool_input": {"command": "gh pr merge 99999 -R Acme/Web"}}, config,
        _host_reader(HOST_FACTS, "/home/dev/scratch"),
    )  # fmt: skip

    push = {**payload, "tool_input": {"command": "git -C /home/dev/other push origin main"}}
    push_on_host = handle_pre_tool_use(conn, push, config, _host_reader(HOST_FACTS, "/home/dev/web"))
    push_on_mac = handle_pre_tool_use(conn, {**push, "cwd": str(bridge_env.root)}, config)

    decision = json.loads(on_host)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "names no PR the guard can identify (gh pr merge 99999)" in decision["permissionDecisionReason"]
    assert on_mac == "" and named == ""
    reason = json.loads(push_on_host)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "this push names no branch the guard can read (git -C /home/dev/other push origin main)" in reason
    assert push_on_mac == ""


def _host_worker(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, monkeypatch: pytest.MonkeyPatch, **fields: str
) -> HostWorker:
    for key, value in bridge_env.env.items():
        monkeypatch.setenv(key, value)
    loop = spawn_process()
    start = process_start_ms(loop)
    assert start is not None
    record = HostWorker(
        worker="g-task-ab12", name="Task", host="devbox", ssh="devbox-alt", cwd="/Users/me/web",
        remote_cwd="/home/dev/web", remote_dir="/home/dev/.local/state/gbridge/g-task-ab12", lead="eli",
        parent="TAB-LEAD",
        token_sha256=hash_token("secret"), created_at=1, loop_pid=loop, loop_start_ms=start, pane="PANE-H",
        agterm_session="TAB-H", tab="TAB-H",
    )  # fmt: skip
    record = replace(record, **fields)
    write_record(record)
    return record


def _gbridge_home(tmp_path: Path, port: int) -> Path:
    home = tmp_path / "gbridge"
    home.mkdir()
    (home / "port").write_text(str(port))
    (home / "token").write_text("secret")
    (home / "worker").write_text("g-task-ab12")
    return home


def _run_gbridge_guard(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: str, cwd: str
) -> str:
    payload = {"session_id": CONV, "cwd": cwd, "tool_name": "Bash", "tool_input": {"command": command}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    gbridge.run_guard(home)
    return capsys.readouterr().out


def test_a_host_workers_push_over_a_claim_is_denied_through_the_worker_api(
    bridge_env: BridgeEnv,
    spawn_process: SpawnProcess,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    pair = Pair(bridge_env, spawn_process)
    claimed = bridge_env.cli("claim", "branch:acme/web:feat/x", "--offline", "--reason", "rebasing", pid=pair.a)
    assert claimed.returncode == 0, claimed.stderr
    checkout = _repo(tmp_path / "host" / "web")
    _host_worker(bridge_env, spawn_process, monkeypatch, remote_worktree=str(checkout))
    _git(checkout, "checkout", "-q", "-b", "feat/x")
    server = start_worker_api(map_toml_to_config({}), port=0)
    home = _gbridge_home(tmp_path, server.server_address[1])
    try:
        denied = _run_gbridge_guard(home, monkeypatch, capsys, "git push origin feat/x", str(checkout))
        other = _run_gbridge_guard(home, monkeypatch, capsys, "git push origin feat/y", str(checkout))
    finally:
        server.shutdown()

    decision = json.loads(denied)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "branch:acme/web:feat/x is reserved by session a (rebasing)" in decision["permissionDecisionReason"]
    assert 'gbridge send a "need branch:acme/web:feat/x' in decision["permissionDecisionReason"]
    assert other == ""
    assert json.loads((home / gbridge.GLOBS_FILE).read_text()) == list_guard_globs()


def test_a_host_workers_push_from_a_sibling_checkout_under_its_mapped_root_is_read_and_one_elsewhere_refused(
    bridge_env: BridgeEnv,
    spawn_process: SpawnProcess,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    pair = Pair(bridge_env, spawn_process)
    claimed = bridge_env.cli("claim", "branch:acme/shop:feat/x", "--offline", "--reason", "rebasing", pid=pair.a)
    assert claimed.returncode == 0, claimed.stderr
    host = tmp_path / "host"
    root = _repo(host / "acme-co")
    shop = _repo(root / "monorepo")
    _git(shop, "remote", "set-url", "origin", "git@github.com:Acme/Shop.git")
    sibling = root / "_wt" / "shop"
    _git(shop, "worktree", "add", "-q", "-b", "feat/x", str(sibling))
    own = host / ".local" / "state" / "gbridge" / "worktrees" / "g-task-ab12"
    _git(root, "worktree", "add", "-q", "--detach", str(own))
    outside = _repo(host / "other")
    _git(outside, "remote", "set-url", "origin", "git@github.com:Acme/Other.git")
    (root / "_wt" / "escape").symlink_to(outside)
    (outside / "docs").mkdir()
    # git would find the outside repo from there; its parents by name lead to the root's.
    (root / "_wt" / "hop").symlink_to(outside / "docs")
    _host_worker(
        bridge_env, spawn_process, monkeypatch, remote_checkout=str(root), remote_worktree=str(own), remote_cwd=str(own)
    )
    monkeypatch.setenv("HOME", str(host))
    config = map_toml_to_config({"worker_path_map": {"~/acme-co": str(root)}})
    server = start_worker_api(config, port=0)
    home = _gbridge_home(tmp_path, server.server_address[1])

    def guard(command: str) -> str:
        out = _run_gbridge_guard(home, monkeypatch, capsys, command, str(own))
        return json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"] if out else ""

    try:
        before_roots = guard(f"cd {sibling} && git push origin feat/x")
        roots = json.loads((home / gbridge.ROOTS_FILE).read_text())
        claimed_push = guard(f"cd {sibling} && git push origin feat/x")
        by_home = guard("cd ~/acme-co/_wt/shop && git push origin feat/x")
        by_option = guard(f"git -C {sibling} push origin feat/x")
        other_branch = guard(f"cd {sibling} && git push origin feat/y")
        own_push = guard("git push origin HEAD:feat/own")
        outside_push = guard(f"cd {outside} && git push origin main")
        escape = guard(f"cd {root}/_wt/escape && git push origin main")
        hop = guard(f"cd {root}/_wt/hop && git push origin main")
        other_user = guard("cd ~dev/acme-co/_wt/shop && git push origin feat/x")
    finally:
        server.shutdown()

    assert roots == [str(root), str(own)]
    assert "this push names no branch the guard can read" in before_roots
    for reason in (claimed_push, by_home, by_option):
        assert "branch:acme/shop:feat/x is reserved by session a (rebasing)" in reason, reason
    assert other_branch == "" and own_push == ""
    for reason in (outside_push, escape, hop, other_user):
        assert "this push names no branch the guard can read" in reason, reason
        assert f"only of a checkout under {root} or {own}: run it there" in reason, reason


def test_gbridge_reports_only_checkouts_whose_real_path_is_under_a_readable_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    root = _repo(tmp_path / "root")
    inner = _repo(root / "inner")
    outside = _repo(tmp_path / "outside")
    (root / "link").symlink_to(outside)
    home = tmp_path / "gb"
    home.mkdir()

    def dirs(command: str) -> list[str]:
        return [c["dir"] for c in gbridge.read_guard_checkouts(home, command, str(root / "inner"))]

    assert dirs(f"cd {outside} && git push") == [str(inner)]
    gbridge.save_roots(home, [str(root)])
    assert dirs(f"cd {outside} && git push; cd {root}/link && git push; cd ~/root && git -C inner push") == [
        str(inner), str(root),
    ]  # fmt: skip
    assert dirs("cd ..;  cd $X && git push; cd 'with space' && git push") == [str(inner), str(root)]
    command = "cd a && cd ../b; git -C c -C d push; git -Ce push; cd -; cd -P d; tar -C f"
    assert gbridge.list_command_dirs(command, "/w") == [
        ("/w/a", "/w/a"), ("/w/b", "/w/a/../b"), ("/w/b/c", "/w/b/c"), ("/w/b/c/d", "/w/b/c/d"), ("/w/b/e", "/w/b/e"),
    ]  # fmt: skip
    assert gbridge.list_command_dirs("cd 'unclosed", "/w") == []
    assert gbridge.list_command_dirs("cd ~/a; cd ~root/b; cd ../../z; cd /x/../y; git -C /q push", "/w") == [
        (str(tmp_path / "a"), str(tmp_path / "a")), ("/q", "/q"),
    ]  # fmt: skip
    assert gbridge.list_command_dirs("cd '~/a' && cd ~/b", "/w") == []
    assert gbridge.list_command_dirs("cd ../../z && cd \\\n y", "/w/v") == [("/z", "/w/v/../../z"), ("/z/y", "/z/y")]


def test_gbridge_reports_a_directory_only_if_every_way_the_command_reaches_it_is_that_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path / "r")
    sib = _repo(root / "sib")
    outside = _repo(tmp_path / "o")
    (outside / "sub").mkdir()
    (root / "link").symlink_to(outside / "sub")
    (outside / "sub" / "back").symlink_to(sib)
    home = tmp_path / "gb"
    home.mkdir()
    gbridge.save_roots(home, [str(root)])

    def dirs(command: str, cwd: Path = sib) -> list[str]:
        return [c["dir"] for c in gbridge.read_guard_checkouts(home, command, str(cwd))]

    assert dirs(f"git -C {root}/link/.. push origin main") == [str(sib)]
    assert dirs(f"cd {sib}/missing/..; git push origin main", root) == [str(root)]
    # Read through the symlinks, link/back is the sibling; link itself is outside and stays unreported.
    linked = f"git -C {root}/link/back status; git -C {root}/link push origin main"
    assert dirs(linked) == [
        str(sib),
        f"{root}/link/back",
    ]
    # cd follows the name, git -C .. the physical path: root is reached two ways, so it is not reported at all.
    assert dirs(f"cd {root}/link && git -C .. push origin main", root) == []
    assert dirs(f"git -C{sib} push; git -C {root} -C sib push", root) == [str(root), str(sib)]
    # Each -C goes on from where the last led: through link, .. is the outside repo, and so is its sib.
    _repo(outside / "sib")
    assert dirs(f"git -C {root}/link -C .. -C sib push origin main", root) == []
    monkeypatch.setenv("HOME", str(tmp_path))
    assert dirs("cd ~/r/sib && git push", root) == [str(root), str(sib)]
    for literal in ("cd '~/r/sib' && git push", 'cd ~""/r/sib && git push', "git -C~/r/sib push"):
        assert dirs(literal, root) == [str(root)], literal
    # An echoed cd moves nothing: .. from link is still the outside repo, so root is left out.
    assert dirs(f"cd {root}/link && echo cd {sib} && git -C .. push origin main", root) == []
    assert dirs(f"cd {sib} && git commit -F - <<EOF\ncd {root}\nEOF\ngit push", root) == [str(root)]
    assert gbridge.list_command_dirs("x=1; cd a\ncd b || cd /c; env cd /d; echo > cd", "/w") == [
        ("/w/a", "/w/a"), ("/w/a/b", "/w/a/b"), ("/c", "/c"),
    ]  # fmt: skip
    gbridge.save_roots(home, [str(tmp_path / "elsewhere")])
    assert dirs("git push") == []


def test_the_host_reader_reads_a_checkout_only_under_a_readable_root_and_a_tilde_as_the_hosts_home() -> None:
    sibling = RepoFacts("/home/dev/acme-co/_wt/shop", "feat/x", {"origin": "Acme/Shop"})
    checkouts = [
        HostCheckout("/home/dev/acme-co/_wt/shop/sub", sibling, sibling.repo_root),
        HostCheckout("/home/dev/acme-co/_wt/esc", replace(sibling, repo_root="/home/dev/acme-co/_wt/esc"), "/srv/x"),
        HostCheckout("/home/dev/other", replace(sibling, repo_root="/home/dev/other"), "/home/dev/other"),
    ]
    reader = make_host_repo_reader(checkouts, ("/home/dev/acme-co", "relative/ignored"), "/home/dev")

    def keys(command: str, cwd: str = "/home/dev/wt") -> list[str]:
        """What the guard reads: nothing once any part is unreadable, since the host then refuses the command."""
        unreadable: list[Unreadable] = []
        targets = map_command_to_targets(command, cwd, unreadable, reader, strict=True, home=reader.home)
        return [] if unreadable else [t.key for t in targets]

    assert reader.roots == ("/home/dev/acme-co",)
    assert keys("cd ~/acme-co/_wt/shop/sub && git push") == ["branch:acme/shop:feat/x"]
    assert keys("cd /home/dev/acme-co/_wt/shop/sub && cd ../sub && git push origin feat/y") == [
        "branch:acme/shop:feat/y"
    ]
    # Only the directory gbridge read: its parent may be reached through a symlink to anywhere.
    assert keys("cd /home/dev/acme-co/_wt/shop && git push") == []
    assert keys("cd /home/dev/acme-co/_wt/shop/sub/x/.. && git push origin feat/y") == []
    assert keys("git -C /home/dev/acme-co/_wt/shop/sub/x/.. push origin feat/y") == []
    assert keys("git -C ~alice/../dev/acme-co/_wt/shop/sub push origin feat/y") == []
    assert keys("cd '~/acme-co/_wt/shop/sub' && git push") == []
    assert keys("false && cd ~/acme-co/_wt/shop/sub || git push origin main") == []
    assert keys("cd ~/acme-co/_wt/shop/sub || git push origin main") == []
    assert keys("cd ~/acme-co/_wt/shop/sub </nonexistent; git push origin main") == []
    assert keys("git -C~/acme-co/_wt/shop/sub push origin feat/y") == []
    assert keys('cd ~""/acme-co/_wt/shop/sub && git push') == []
    for elsewhere in (
        "cd ~/acme-co/_wt/shop/sub & git push",
        "cd ~/acme-co/_wt/shop/sub | cat; git push",
        "env cd ~/acme-co/_wt/shop/sub; git push",
        "echo | cd ~/acme-co/_wt/shop/sub; git push",
    ):
        assert keys(elsewhere, "/home/dev/acme-co/_wt/shop/sub") == [], elsewhere
    assert keys("git push", "/home/dev/acme-co/_wt/shop/sub") == ["branch:acme/shop:feat/x"]
    assert keys("cd ~/acme-co/_wt/shop/sub && git push origin feat/y || echo failed") == ["branch:acme/shop:feat/y"]
    assert keys("cd /home/dev/acme-co/_wt && git push origin main") == []
    assert keys("cd /home/dev/acme-co/_wt/esc && git push origin main") == []
    assert keys("cd /home/dev/other && git push origin main") == []
    assert keys("cd ~dev/acme-co/_wt/shop/sub && git push") == []
    no_home = make_host_repo_reader(checkouts, ("/home/dev/acme-co",))
    assert map_command_to_targets("cd ~/acme-co/_wt/shop/sub && git push", "/", None, no_home, strict=True) == []
    linked = make_host_repo_reader(
        [HostCheckout("/r/sib", replace(sibling, repo_root="/r/sib"), "/mnt/r/sib")], ("/r",), None,
        {"/r": "/mnt/r", "/elsewhere": "/srv"},
    )  # fmt: skip
    assert linked("/r/sib") is not None
    assert make_host_repo_reader(list(linked.checkouts), ("/r",))("/r/sib") is None
    assert make_host_repo_reader(list(linked.checkouts), ("/r",), None, {"/r": "/srv"})("/r/sib") is None
    physical = HostCheckout("/mnt/r/own", replace(sibling, repo_root="/mnt/r/own"), "/mnt/r/own")
    assert make_host_repo_reader([physical], ("/r",), None, {"/r": "/mnt/r"})("/mnt/r/own") is not None


def test_a_host_workers_readable_roots_are_its_mapped_root_and_its_own_worktree() -> None:
    path_map = {"~/acme-co": "/home/dev/acme-co", "~/acme-co/deep": "/home/dev/acme-co/deep", "~/x": "/home/dev/x"}
    record = HostWorker(
        worker="g-a-1", name="a", host="devbox", ssh="devbox", cwd="/Users/me/acme-co/monorepo", remote_cwd="/wt/sub",
        remote_dir="/d", lead="eli", parent="P", token_sha256="t", created_at=1,
        remote_checkout="/home/dev/acme-co/monorepo", remote_worktree="/wt",
    )  # fmt: skip

    assert find_mapped_root("/home/dev/acme-co/deep/a", path_map) == "/home/dev/acme-co/deep"
    assert find_mapped_root("/home/dev/acme-cox", path_map) is None
    assert list_readable_roots(record, path_map) == ("/home/dev/acme-co", "/wt")
    assert list_readable_roots(record, {}) == ("/home/dev/acme-co/monorepo", "/wt")
    assert list_readable_roots(replace(record, remote_checkout="", remote_worktree=""), path_map) == ("/wt/sub",)


def test_the_api_says_unchecked_when_the_guard_cannot_stand_for_the_worker(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _host_worker(bridge_env, spawn_process, monkeypatch)
    payload: dict[str, object] = {"session_id": CONV, "cwd": "/h", "tool_name": "Bash", "tool_input": {"command": "x"}}
    conn = connect(bridge_env.db)
    conn.execute(
        "INSERT INTO instances (instance_id, pid, proc_start_ms, first_seen_at, last_observed_at) "
        "VALUES ('9@1', 9, 1, 1, 1)"
    )
    other = "11111111-2222-4333-8444-5555555555ab"
    conn.execute(
        "INSERT INTO conversations (conversation_id, instance_id, first_seen_at, last_seen_at) VALUES (?, '9@1', 1, 1)",
        (other,),
    )
    conn.commit()

    gone = handle_host_hook(replace(record, loop_pid=0), load_config(), "PreToolUse", payload, None)
    forged = handle_host_hook(record, load_config(), "PreToolUse", {**payload, "session_id": other}, None)
    own = handle_host_hook(record, load_config(), "PreToolUse", payload, None)

    assert gone["checked"] is False and forged["checked"] is False
    assert own == {"checked": True, "output": "", "globs": list_guard_globs(), "roots": ["/home/dev/web"]}

    # A gbridge from before checkouts: its cwd is read only when that is the worker's own worktree itself.
    def legacy(cwd: str) -> object:
        push = {**payload, "cwd": cwd, "tool_input": {"command": "git push origin feat/x"}}
        facts = {**asdict(replace(HOST_FACTS, repo_root=cwd)), "repo": "Acme/Web"}
        return handle_host_hook(record, load_config(), "PreToolUse", push, facts)["output"]

    assert legacy("/home/dev/web") == ""
    assert "this push names no branch the guard can read" in str(handle_host_hook(
        record, load_config(), "PreToolUse",
        {**payload, "cwd": "/home/dev/web/link", "tool_input": {"command": "git push origin feat/x"}},
        {**asdict(HOST_FACTS), "repo_root": "/home/dev/web"},
    )["output"])  # fmt: skip
    assert "this push names no branch the guard can read" in str(legacy("/home/dev/web/escape"))


def test_gbridge_refuses_a_guarded_command_it_cannot_get_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = _gbridge_home(tmp_path, 1)
    answers: list[Any] = []

    def fake_post(home: Path, path: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        if answer == "slow":
            time.sleep(5)
            return {}
        return cast(dict[str, Any], answer)

    def reason(out: str) -> str:
        decision = json.loads(out)["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
        return decision["permissionDecisionReason"]

    monkeypatch.setattr(gbridge, "post", fake_post)
    answers.append(OSError("connection refused"))
    assert "could not be reached (connection refused), so this command was not run: until" in reason(
        _run_gbridge_guard(home, monkeypatch, capsys, "ls", "/h")
    )
    (home / gbridge.GLOBS_FILE).write_text(json.dumps(list_guard_globs()))
    answers.append(OSError("connection refused"))
    assert "this command, which may push or merge, was not run" in reason(
        _run_gbridge_guard(home, monkeypatch, capsys, "git push", "/h")
    )
    answers.append({"checked": False, "reason": "the worker's tab on the Mac is not running"})
    assert "did not check this command (the worker's tab" in reason(
        _run_gbridge_guard(home, monkeypatch, capsys, "git push", "/h")
    )
    answers.append({"stderr": "gbridge: bridge refused (500): ...", "code": 2})
    assert "bridge refused (500)" in reason(_run_gbridge_guard(home, monkeypatch, capsys, "gh pr merge 1", "/h"))
    answers.append("slow")
    monkeypatch.setattr(gbridge, "GUARD_DEADLINE_S", 1)
    assert "no answer in 1s" in reason(_run_gbridge_guard(home, monkeypatch, capsys, "git push", "/h"))
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    gbridge.run_guard(home)
    assert "could not check this command" in reason(capsys.readouterr().out)
    assert answers == []


@pytest.mark.parametrize("swallowed", [OSError, Exception])
def test_the_deadline_holds_even_when_a_read_swallows_its_alarm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    swallowed: type[Exception],
) -> None:
    home = _gbridge_home(tmp_path, 1)

    def slow_read(cwd: str) -> None:
        with contextlib.suppress(swallowed):
            time.sleep(3)

    def passes(home: Path, path: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        return {"checked": True, "output": ""}

    monkeypatch.setattr(gbridge, "GUARD_DEADLINE_S", 1)
    monkeypatch.setattr(gbridge, "read_git_info", slow_read)
    monkeypatch.setattr(gbridge, "post", passes)
    started = time.monotonic()

    out = _run_gbridge_guard(home, monkeypatch, capsys, "git push", str(tmp_path))

    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "no answer in 1s" in out
    assert time.monotonic() - started < 2.5


def test_gbridge_asks_the_mac_only_for_what_its_gate_would_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = _gbridge_home(tmp_path, 1)
    sent: list[dict[str, Any]] = []

    def fake_post(home: Path, path: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        sent.append(body)
        return {"checked": True, "output": "", "globs": list_guard_globs()}

    monkeypatch.setattr(gbridge, "post", fake_post)
    assert _run_gbridge_guard(home, monkeypatch, capsys, "ls -la", "/h") == ""
    assert len(sent) == 1, "without the Mac's globs every command is asked about"
    assert json.loads((home / gbridge.GLOBS_FILE).read_text()) == list_guard_globs()

    for command in ("ls -la", "pytest -q", "echo hi > f"):
        assert _run_gbridge_guard(home, monkeypatch, capsys, command, "/h") == ""
    for command in ("git  push", "git -C x push", "gh pr merge 3", "gh api -X PUT x/merge"):
        _run_gbridge_guard(home, monkeypatch, capsys, command, "/h")
    for tool in ("Edit", "Write", "Read"):
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool_name": tool, "tool_input": {}})))
        gbridge.run_guard(home)
    assert capsys.readouterr().out == ""
    assert [s["payload"]["tool_input"]["command"] for s in sent[1:]] == [
        "git  push", "git -C x push", "gh pr merge 3", "gh api -X PUT x/merge",
    ]  # fmt: skip
    assert set(sent[1]["payload"]) == {"session_id", "cwd", "tool_name", "tool_input"}
    for command in ('git pu""sh origin main', "gh pr mer'g'e 12", "git p\\ush"):
        assert gbridge.may_need_guard(list_guard_globs(), command), command
    assert gbridge.may_need_guard(list_guard_globs(), 'gh pr create --title "🤖 Claude: x"')


def test_host_settings_send_bash_and_linear_issue_saves_to_the_guard() -> None:
    settings = host_side.build_settings("/nonexistent", [])

    group = settings["hooks"]["PreToolUse"][0]
    assert group["matcher"] == "|".join(("Bash", *LINEAR_ISSUE_TOOLS))
    assert gbridge.LINEAR_ISSUE_TOOLS == LINEAR_ISSUE_TOOLS
    assert group["hooks"][0]["command"].endswith("/.local/bin/gbridge hook PreToolUse")
    assert group["hooks"][0]["timeout"] > gbridge.GUARD_DEADLINE_S


def test_a_host_worker_runs_in_its_own_worktree_removed_only_when_nothing_is_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    def gone(worker: str) -> str:
        return "gone"

    monkeypatch.setattr(host_side, "session_state", gone)
    shared = _repo(tmp_path / "acme")
    (shared / "svc").mkdir()
    (shared / "svc" / "app.py").write_text("")
    _git(shared, "add", "svc")
    _git(shared, "commit", "-q", "-m", "svc")

    checkout, cwd = host_side.add_worktree("g-a-0000", str(shared / "svc"))  # type: ignore[misc]
    worktree = Path(host_side.worktree_dir("g-a-0000"))
    assert (checkout, cwd) == (str(shared.resolve()), str(worktree / "svc"))
    assert _git(worktree, "rev-parse", "HEAD") == _git(shared, "rev-parse", "HEAD")
    (worktree / "new.txt").write_text("work")
    kept = host_side.remove("g-a-0000")
    assert kept == {"ok": True, "error": "", "kept": f"{worktree} is kept: it has uncommitted changes"}
    assert worktree.is_dir()

    _git(worktree, "add", "new.txt")
    _git(worktree, "commit", "-q", "-m", "work")
    assert host_side.remove("g-a-0000")["kept"].endswith("its HEAD commit is on no branch")
    _git(shared, "config", "status.showUntrackedFiles", "no")
    (worktree / "untracked.txt").write_text("work")
    _git(worktree, "branch", "keep-me")
    assert host_side.remove("g-a-0000")["kept"].endswith("it has uncommitted changes")
    (worktree / "untracked.txt").unlink()
    _git(worktree, "branch", "feat/x")
    assert host_side.remove("g-a-0000") == {"ok": True, "error": ""}
    assert not worktree.exists() and not Path(host_side.base_dir()).exists()
    assert "g-a-0000" not in _git(shared, "worktree", "list")
    assert _git(shared, "status", "--porcelain") == ""


def test_a_host_worktree_is_trusted_in_claudes_config_and_only_that_entry_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    worktree = host_side.worktree_dir("g-a-0000")
    config = tmp_path / ".claude.json"
    assert host_side.set_worktree_trust(worktree, True) == f"{config} does not exist yet"
    assert host_side.set_worktree_trust(worktree, False) is None
    original: dict[str, Any] = {
        "numStartups": 3, "userID": "u", "projects": {"/home/dev/web": {"hasTrustDialogAccepted": False, "x": 1}},
    }  # fmt: skip
    config.write_text(json.dumps(original))
    config.chmod(0o640)

    assert host_side.set_worktree_trust(worktree, True) is None

    data = json.loads(config.read_text())
    assert data["projects"] == {**original["projects"], worktree: {"hasTrustDialogAccepted": True}}
    assert {k: v for k, v in data.items() if k != "projects"} == {"numStartups": 3, "userID": "u"}
    assert config.stat().st_mode & 0o777 == 0o640
    data["projects"][worktree]["lastCost"] = 1.5
    config.write_text(json.dumps(data))
    assert host_side.set_worktree_trust(worktree, True) is None
    assert json.loads(config.read_text())["projects"][worktree] == {"hasTrustDialogAccepted": True, "lastCost": 1.5}

    assert host_side.set_worktree_trust(worktree, False) is None
    assert json.loads(config.read_text()) == original
    assert not Path(f"{config}.lock").exists()
    config.write_text("{not json")
    assert "not valid JSON" in str(host_side.set_worktree_trust(worktree, True))
    assert config.read_text() == "{not json"


def test_the_trust_write_waits_for_claudes_lock_and_passes_a_stale_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(host_side, "CLAUDE_JSON_LOCK_WAIT_S", 0.2)
    config = tmp_path / ".claude.json"
    config.write_text("{}")
    lock = Path(f"{config}.lock")
    lock.mkdir()

    started = time.monotonic()
    assert "was not free" in str(host_side.set_worktree_trust("/w/g-a-0000", True))
    assert time.monotonic() - started >= 0.2
    stale = time.time() - 60
    os.utime(lock, (stale, stale))
    assert "was not free" in str(host_side.set_worktree_trust("/w/g-a-0000", True))
    assert lock.is_dir() and config.read_text() == "{}"
    lock.rmdir()
    assert host_side.set_worktree_trust("/w/g-a-0000", True) is None
    assert json.loads(config.read_text()) == {"projects": {"/w/g-a-0000": {"hasTrustDialogAccepted": True}}}
    assert not lock.exists()


def test_the_trust_write_never_writes_or_frees_a_lock_it_no_longer_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config = tmp_path / ".claude.json"
    config.write_text("{}")
    lock = Path(f"{config}.lock")
    real_loads = json.loads

    def taken_over(text: str) -> Any:
        # Claude Code takes the lock over while bridge reads: its lock directory replaces ours.
        lock.rmdir()
        lock.mkdir()
        return real_loads(text)

    monkeypatch.setattr(host_side.json, "loads", taken_over)

    assert "too long to write safely" in str(host_side.set_worktree_trust("/w/g-a-0000", True))
    assert config.read_text() == "{}" and lock.is_dir()


def test_a_host_worker_outside_a_git_checkout_is_not_launched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "plain").mkdir()

    error = host_side.add_worktree("g-a-0000", str(tmp_path / "plain"))

    assert isinstance(error, str) and "not in a git checkout" in error


def test_a_closed_host_worker_resumes_in_its_worktree_brought_back_if_removed() -> None:
    record = HostWorker(
        worker="g-a-0000", name="A", host="devbox", ssh="devbox-alt", cwd="/Users/me/web",
        remote_cwd="/h/.local/state/gbridge/worktrees/g-a-0000/svc", remote_dir="/h/d", lead="eli", parent="P",
        token_sha256="", created_at=1, remote_checkout="/h/acme",
        remote_worktree="/h/.local/state/gbridge/worktrees/g-a-0000",
    )  # fmt: skip

    [closed] = map_host_tab_to_closed_records(record, [TabSession("PANE-H", "conv-1", 1, False, "idle", None, None)])

    inner = (
        "{ [ -d /h/.local/state/gbridge/worktrees/g-a-0000 ] || git -C /h/acme worktree add --detach "
        "/h/.local/state/gbridge/worktrees/g-a-0000; } && cd /h/.local/state/gbridge/worktrees/g-a-0000/svc && "
        "~/.local/bin/claude --resume conv-1"
    )
    assert closed.resume_command == f"ssh -t devbox-alt '{inner}'"


@pytest.mark.parametrize("globs", [None, "not json", "[]", '{"a": 1}'])
def test_without_a_readable_set_of_globs_every_bash_command_is_asked_about(tmp_path: Path, globs: str | None) -> None:
    home = _gbridge_home(tmp_path, 1)
    if globs is not None:
        (home / gbridge.GLOBS_FILE).write_text(globs)

    assert gbridge.read_globs(home) is None
    assert gbridge.may_need_guard(gbridge.read_globs(home), "ls -la")


MERGE_MUTATION = (
    "gh api graphql -f query='mutation { mergePullRequest(input: {pullRequestId: \"PR_x\"}) { clientMutationId } }'"
)
CLOSE_MUTATION = (
    "gh api graphql -f query='mutation { closePullRequest(input: {pullRequestId: \"PR_x\"}) { clientMutationId } }'"
)


@pytest.mark.parametrize(
    ("command", "kind"),
    [
        ("if true; then git push origin feat/x; fi", COMMAND),
        ('bash -c "git push origin feat/x"', COMMAND),
        ("sh -c 'gh pr merge 12 -R Acme/Web'", COMMAND),
        ("eval git push origin feat/x", COMMAND),
        ("(git push origin feat/x)", COMMAND),
        ("{ git push origin feat/x; }", COMMAND),
        ("echo feat/x | xargs git push origin", COMMAND),
        ("echo 'git push origin main' | sh", COMMAND),
        ("sudo git push origin feat/x", COMMAND),
        ("bash -s <<'EOF'\ngit push origin feat/x\nEOF", COMMAND),
        ("find . -maxdepth 0 -exec git push origin feat/x \\;", COMMAND),
        ("cat > p.sh <<EOF\ngit push origin main\nEOF\nbash p.sh", COMMAND),
        ("python3 - <<EOF\nimport os; os.system('git push origin main')\nEOF", COMMAND),
        ('echo "$(git push origin main)"', COMMAND),
        ("timeout 60 bash -c 'git push origin main'", COMMAND),
        ("timeout --frobnicate 60 git push origin feat/x", COMMAND),
        ("timeout 60 git push origin feat/x | sh", COMMAND),
        ('git push origin feat/x > "$(git push origin main)"', COMMAND),
        ("X=$(git push origin main)", COMMAND),
        ("git push origin feat/x 2>&1 > `git push origin main`", COMMAND),
        ("echo '>'&bash -c 'git push origin main'", COMMAND),
        ("echo \\>&bash -c 'git push origin main'", COMMAND),
        ("git grep -O'sh -c \"git push origin main\"' x", COMMAND),
        ("git fetch --upload-pack='sh -c \"git push origin main\"' /tmp/r", COMMAND),
        ("git rebase -x 'git push origin main' origin/main", COMMAND),
        ("echo 'git push origin main' > p; rg --pre ./p x .", COMMAND),
        ("GH_REPO=Acme/Other gh pr merge 12", COMMAND),
        ("GIT_DIR=/home/dev/other/.git git push origin main", PUSH),
        ("export GIT_DIR=/home/dev/other/.git; git push origin main", COMMAND),
        ("GIT_CONFIG_GLOBAL=/tmp/c git push origin feat/x", PUSH),
        ("git -C/home/dev/other push origin main", PUSH),
        ("git --git-dir=/home/dev/other/.git push origin main", PUSH),
        ("git --work-tree /home/dev/other push origin main", PUSH),
        ("git push --repo fork", PUSH),
        ("git push origin feat/x --frobnicate", PUSH),
        ("git config alias.p push && git p origin main", COMMAND),
        ("git checkout feat/y && git push origin HEAD", PUSH),
        ("gh pr checkout 12 && gh pr merge --squash", COMMAND),
        ("gh alias set m 'pr merge' && gh m 12", COMMAND),
        (MERGE_MUTATION, MERGE),
        (CLOSE_MUTATION, CLOSE),
        ("gh api graphql -F query=@merge.graphql", COMMAND),
        ("cd /home/dev/other && gh pr close 12", CLOSE),
        ('gh pr close "$PR" -R Acme/Web', CLOSE),
        ("gh pr close 12 -R $REPO", CLOSE),
        ("gh api -X PATCH 'repos/{owner}/{repo}/pulls/12' -f state=closed", CLOSE),
        ("gh api -X PATCH repos/Acme/Web/issues/$N -f state=closed", CLOSE),
        ("gh api -X PUT repos/$R/pulls/12/merge", MERGE),
        ("gh api -X PUT repos/Acme/Web/pulls/12/update-branch", COMMAND),
        ("gh api -X PATCH repos/Acme/Web/git/refs/heads/main -f sha=abc # pulls", COMMAND),
        ("printf '%s' 'unclosed; git push origin feat/x", COMMAND),
        ("cat <<EOF\n$(git push origin main)\nEOF", COMMAND),
        ('cat <<-E""OF\n\tok\n\tEOF\ncat <<EOF\n`git push origin main`\nEOF', COMMAND),
        ('GIT_AUTHOR_NAME="$(git push origin main)" git status', COMMAND),
        ("git grep -nO\"sh -c 'git push origin main'\" x", COMMAND),
        ("git grep --open-files-in-page=\"sh -c 'git push origin main'\" x", COMMAND),
        ("git rebase -ix 'git push origin main' origin/main", COMMAND),
        ("git clone -c core.sshCommand=./p https://github.com/Acme/Web /tmp/w && git push origin main", COMMAND),
        ("git push origin HEAD:{main,release}", PUSH),
        ("git -C . switch main && git push origin HEAD", PUSH),
        (
            "gh api graphql -f 'query=mutation($s:PullRequestUpdateState!){updatePullRequest(input:{pullRequestId:"
            '"PR_x",state:$s}){clientMutationId}}\' -f s=CLOSED',
            CLOSE,
        ),
        ("gh api graphql -f 'query=mutation($i:UpdatePullRequestInput!){updatePullRequest(input:$i){id}}'", CLOSE),
        ("gh api graphql -f 'query=mutation{deleteRef(input:{refId:\"R\"}){clientMutationId}}'", COMMAND),
        ("true && cd .; gh api graphql --input /tmp/merge.json", COMMAND),
        ("bash -c 'gh pr create --title \"\U0001f916 Claude: x\" --body test'", COMMAND),
        ("git -c core.sshCommand=./p push origin feat/x", PUSH),
        ("git push https://github.com/Acme/Web.git feat/x", PUSH),
    ],
)
def test_on_a_host_a_trigger_the_guard_cannot_read_is_refused(command: str, kind: str) -> None:
    unreadable: list[Unreadable] = []

    _keys(command, "/home/dev/web", unreadable)

    assert unreadable, command
    assert unreadable[0].kind == kind, unreadable


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("uv run pytest -q && git push origin feat/x", ["branch:acme/web:feat/x"]),
        ("git push origin feat/x 2>&1 | tail -5", ["branch:acme/web:feat/x"]),
        ("git push origin feat/x > /tmp/push.log", ["branch:acme/web:feat/x"]),
        ("git add -A && git commit -m 'fix git push docs' && git push origin feat/x", ["branch:acme/web:feat/x"]),
        ("grep -rn 'git push' docs | head", []),
        ("git switch -c feat/y && git push -u origin HEAD", ["branch:acme/web:feat/y"]),
        ("git rebase origin/main && git push --force-with-lease origin feat/x", ["branch:acme/web:feat/x"]),
        ("GIT_EDITOR=true git rebase --continue && HUSKY=0 git push origin feat/x", ["branch:acme/web:feat/x"]),
        ("git push -fu origin feat/x", ["branch:acme/web:feat/x"]),
        ("git push --dry-run origin main", []),
        ("git push origin --tags", []),
        ("git push origin refs/tags/v1.2", []),
        ("gh api repos/Acme/Web/pulls/12", []),
        ("gh api repos/Acme/Web/pulls/12/comments -f body='after git push'", []),
        ("gh api -X PATCH repos/Acme/Web/pulls/12 -f title=better", []),
        ("gh api -X PATCH repos/Acme/Web/pulls/12 -f state=closed", ["pr:acme/web#12"]),
        ("gh api -XPATCH repos/Acme/Web/issues/12 -fstate=closed", ["pr:acme/web#12"]),
        ("gh api graphql -f query='query { search(query: \"is:merged\", type: ISSUE) { issueCount } }'", []),
        ("gh pr view 12 --json state && gh pr merge 12 --squash", ["pr:acme/web#12"]),
        ("gh pr close 12 -c 'superseded'", ["pr:acme/web#12"]),
        ("gh pr merge -RAcme/Api 12", ["pr:acme/api#12"]),
        ("gh pr merge 12 -R=Acme/Api", ["pr:acme/api#12"]),
        ("gh pr merge -dRAcme/Api 12", ["pr:acme/api#12"]),
        ("gh pr merge --body '-RAcme/Other' 12", ["pr:acme/web#12"]),
        ("gh pr merge -b '-RAcme/Other' 12", ["pr:acme/web#12"]),
        ("gh api -iXPUT repos/Acme/Web/pulls/12/merge", ["pr:acme/web#12"]),
        ("gh api -X PATCH repos/Acme/Web/pulls/12 -f=state=closed", ["pr:acme/web#12"]),
        ("printf '%s\\n' $'can\\'t'; git push origin feat/x", ["branch:acme/web:feat/x"]),
        ('gh api graphql -f \'query=mutation{addComment(input:{subjectId:"I",body:"ok"}){clientMutationId}}\'', []),
        ("git grep -n 'git push' -- docs", []),
        ("git commit -F - <<'EOF' && git push origin feat/x\nFix `$(x)`\nEOF", ["branch:acme/web:feat/x"]),
        ("bridge send eli '\U0001f916 note' && git push origin feat/x", ["branch:acme/web:feat/x"]),
        ('timeout 60 git push origin feat/x </dev/null 2>&1; echo "exit=$?"', ["branch:acme/web:feat/x"]),
        ("timeout -s KILL -k 5 60 gh pr merge 1 --squash 2>&1 && echo done", ["pr:acme/web#1"]),
        ("timeout --signal=TERM --kill-after=5s 2m git push origin feat/x &>/tmp/log", ["branch:acme/web:feat/x"]),
        ("gh pr merge < /dev/null 12 >&2", ["pr:acme/web#12"]),
        ("git push origin feat/x > /tmp/push.log 2>&1 || echo failed", ["branch:acme/web:feat/x"]),
        ("ls -la", []),
        ("git -c user.email=bot@example.com push origin HEAD:feat/x", ["branch:acme/web:feat/x"]),
    ],
)
def test_on_a_host_a_plain_push_or_merge_still_passes(command: str, expected: list[str]) -> None:
    unreadable: list[Unreadable] = []

    assert _keys(command, "/home/dev/web", unreadable) == expected
    assert unreadable == [], unreadable


def test_global_push_config_keeps_an_explicit_push_target_on_the_mac(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    (tmp_path / ".gitconfig").write_text("[remote]\n\tpushDefault = origin\n")
    web = _repo(tmp_path / "web")

    def mac(command: str) -> tuple[list[str], list[str]]:
        unreadable: list[Unreadable] = []
        targets = map_command_to_targets(command, str(web), unreadable)
        return [t.key for t in targets], [u.text for u in unreadable]

    assert mac("git push origin main") == (["branch:acme/web:main"], [])
    assert mac("git push") == (["branch:acme/web:main"], ["git push"])
    (tmp_path / ".gitconfig").write_text('[url "git@github.com:"]\n\tinsteadOf = https://github.com/\n')
    assert mac("git push origin main") == (["branch:acme/web:main"], ["git push origin main"])
    assert mac("git push --repo origin") == (["branch:acme/web:main"], ["git push --repo origin"])


def _on_the_mac(command: str, cwd: Path) -> tuple[list[str], list[tuple[str, bool]]]:
    unreadable: list[Unreadable] = []
    targets = map_command_to_targets(command, str(cwd), unreadable)
    return [t.key for t in targets], [(u.kind, u.refused_on_mac) for u in unreadable]


@pytest.mark.parametrize(
    "command",
    [
        "bash -c 'git push origin feat/x'",
        'sh -c "cd /tmp && git push"',
        "zsh -lc 'gh pr merge 12'",
        "timeout 60 bash -c 'git push origin feat/x'",
        "sh -c \"bash -c 'git push origin feat/x'\"",
        "eval git push origin feat/x",
        "eval 'gh api -X PUT repos/Acme/Web/pulls/12/merge'",
        "git branch | xargs git push origin",
        "gh pr list | xargs -n 1 -I{} gh pr merge {}",
        "env -S 'git push origin feat/x'",
        "env -iS 'git push origin feat/x'",
        "env --split-string='git push origin feat/x'",
        "bash -o pipefail -c 'git push origin feat/x'",
        "bash -c 'if true; then /usr/bin/git push origin feat/x; fi'",
        "eval 'git -C .' 'push origin feat/x'",
        "env -i -S 'git push origin feat/x'",
        "echo main | xargs -tn 1 git push origin",
        "git push github.com:Acme/Web.git main",
        "git -c core.sshCommand=./p push origin feat/x",
        "git -c remote.origin.url=git@github.com:Acme/Other.git push origin feat/x",
        "git -c url.git@github.com:Acme/Other.insteadOf=git@github.com:Acme/Web push origin feat/x",
        "git -c user.name=bot -c credential.helper=./h push origin feat/x",
        "git --config-env user.name=NAME push origin feat/x",
        "git push https://github.com/Acme/Web.git HEAD:feat/x",
        "git push git@github.com:Acme/Web.git feat/x",
        "sudo git push origin feat/x",
        "sudo -u dev -E git push origin feat/x",
        "sudo --user dev gh pr merge 12",
        "sudo -s <<'EOF'\ngit push origin feat/x\nEOF",
        "sudo bash -c 'git push origin feat/x'",
        "bash -s <<'EOF'\ngit push origin feat/x\nEOF",
        "sh <<EOF\ncd /tmp\ngit push origin feat/x\nEOF",
        "bash -s -- arg <<-EOF\n\tgh pr merge 12\n\tEOF",
        "bash <<< 'git push origin feat/x'",
        "zsh -s <<<'gh pr merge 12'",
        "sudo -i <<< 'git push origin feat/x'",
        "echo 'git push origin feat/x' | bash",
        "printf '%s\\n' 'git push origin feat/x' | sh -s",
        "cat <<'EOF' | sh\ngit push origin feat/x\nEOF",
        "find . -maxdepth 0 -exec git push origin feat/x \\;",
        "find . -maxdepth 0 -execdir gh pr merge 12 +",
        "find . -name x -exec grep -q x {} \\; -ok git push origin feat/x \\;",
        "find . -maxdepth 0 -exec sh -c 'git push origin feat/x' \\;",
    ],
)
def test_the_mac_refuses_a_push_or_merge_it_would_judge_written_so_it_cannot_read_it(
    tmp_path: Path, command: str
) -> None:
    assert _on_the_mac(command, _repo(tmp_path / "web")) == ([], [(WRAPPED, True)])


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git -c user.email=bot@example.com push origin HEAD:feat/y", ["branch:acme/web:feat/y"]),
        ("git -c user.name=bot -c color.ui=never push origin feat/y", ["branch:acme/web:feat/y"]),
        ("echo git push origin main", []),
        ("git commit -m 'then git push origin main'", []),
        ("git config push.default current", []),
        ("bash -c 'echo git push origin main'", []),
        ("bash -c 'git status && git log -1'", []),
        ("command -v git", []),
        ("echo main | xargs echo git push", []),
        ("bash scripts/push.sh", []),
        ("bash scripts/release.sh 'git push origin main'", []),
        ("find . -name '*.md' | xargs grep -l 'gh pr merge'", []),
        ("git log | xargs grep 'git push origin'", []),
        ("echo main | xargs echo 'git push origin main'", []),
        ("git commit -S -m 'git push origin main'", []),
        ("git -c core.pager=cat log", []),
        ("git push --dry-run https://github.com/Acme/Web.git main", []),
        ("sudo grep -rn 'git push' /etc", []),
        ("sudo -u dev git status", []),
        ("find . -name '*.sh' -exec grep -l 'git push' {} +", []),
        ("find . -exec echo git push origin main \\;", []),
        ("find . -name git -o -name push", []),
        ("bash -s <<'EOF'\necho git push origin main\nEOF", []),
        ("cat <<'EOF'\ngit push origin main\nEOF", []),
        ("git log --oneline | grep 'git push'", []),
        ("echo 'git push origin main' | grep push", []),
        ("git push origin main | sh", ["branch:acme/web:main"]),
        ("bash -s < notes.txt", []),
        ("echo 'git push origin main' | xargs sh -c 'echo \"$@\"' sh", []),
    ],
)
def test_the_mac_reads_a_harmless_config_push_and_never_refuses_what_is_no_push(
    tmp_path: Path, command: str, expected: list[str]
) -> None:
    assert _on_the_mac(command, _repo(tmp_path / "web")) == (expected, [])


def test_the_mac_says_to_write_a_wrapped_push_plainly(
    bridge_env: BridgeEnv, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for key, value in bridge_env.env.items():
        monkeypatch.setenv(key, value)
    config = load_config()
    conn = connect(bridge_env.db)
    web = _repo(tmp_path / "web")

    def on_mac(command: str) -> str:
        payload: dict[str, object] = {
            "session_id": CONV, "cwd": str(web), "tool_name": "Bash", "tool_input": {"command": command},
        }  # fmt: skip
        out = handle_pre_tool_use(conn, payload, config)
        return json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"] if out else ""

    assert "write it as a plain `git push <remote> <branch>`" in on_mac("bash -c 'git push origin main'")
    assert "so the guard can read it" in on_mac("git push git@github.com:Acme/Web.git main")
    assert "sudo, find -exec" in on_mac("sudo git push origin main")
    assert on_mac("git -c user.email=bot@example.com push origin HEAD:main") == ""


def test_mac_refuses_a_merge_or_api_close_whose_text_hides_its_pr(
    bridge_env: BridgeEnv, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for key, value in bridge_env.env.items():
        monkeypatch.setenv(key, value)
    config = load_config()
    conn = connect(bridge_env.db)
    web = _repo(tmp_path / "web")

    def on_mac(command: str) -> str:
        payload: dict[str, object] = {
            "session_id": CONV, "cwd": str(web), "tool_name": "Bash", "tool_input": {"command": command},
        }  # fmt: skip
        out = handle_pre_tool_use(conn, payload, config)
        return json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"] if out else ""

    def keys(command: str) -> list[str]:
        return [t.key for t in map_command_to_targets(command, str(web))]

    for command in (
        "GH_HOST=ghe.example.com gh pr merge 12",
        "GIT_DIR=/elsewhere/.git gh pr merge 12",
        'gh pr merge "$PR" --squash',
        "cd $OTHER && gh pr merge 12",
        MERGE_MUTATION,
        CLOSE_MUTATION,
        "gh api -X PUT 'repos/{owner}/{repo}/pulls/5/merge'",
        "gh api -X PATCH 'repos/{owner}/{repo}/pulls/12' -f state=closed",
    ):
        assert "names no PR the guard can identify" in on_mac(command), command
    assert keys("GH_REPO=Acme/Other gh pr merge 12") == ["pr:acme/other#12"]
    assert keys("gh pr merge -RAcme/Other 12") == ["pr:acme/other#12"]
    assert keys("gh api -X PATCH repos/Acme/Web/pulls/12 -f state=closed") == ["pr:acme/web#12"]
    for command in ("GH_REPO=Acme/Other gh pr merge 12", 'gh pr merge 12 --body "$NOTES"', "gh pr close $PR"):
        assert on_mac(command) == "", command
    assert on_mac("if true; then git push origin main; fi") == ""


def test_the_mac_reads_a_push_or_merge_under_timeout_and_redirections() -> None:
    def mac(command: str) -> list[str]:
        reader = _host_reader(HOST_FACTS, "/home/dev/web")
        return [t.key for t in map_command_to_targets(command, "/home/dev/web", None, reader)]

    assert mac("echo '>'&git push origin main") == ["branch:acme/web:main"]
    assert mac('echo ""&gh pr merge 12') == ["pr:acme/web#12"]
    assert mac("echo '>'&gh pr close 12") == ["pr:acme/web#12"]
    assert mac("git push origin '>' >/dev/null main") == ["branch:acme/web:>", "branch:acme/web:main"]

    assert mac('timeout 60 git push origin main </dev/null 2>&1; echo "exit=$?"') == ["branch:acme/web:main"]
    assert mac("timeout -k 5 60 gh pr merge 3 2>&1") == ["pr:acme/web#3"]
    assert mac("git push origin main 2>&1 | tail -3") == ["branch:acme/web:main"]


def test_a_host_gh_pr_from_a_checkout_whose_remote_urls_are_rewritten_is_refused() -> None:
    rewritten = replace(HOST_FACTS, custom_remote=True)

    def read(command: str, strict: bool) -> tuple[list[str], list[Unreadable]]:
        unreadable: list[Unreadable] = []
        reader = _host_reader(rewritten, "/home/dev/web")
        targets = map_command_to_targets(command, "/home/dev/web", unreadable, reader, strict=strict)
        return [t.key for t in targets], unreadable

    keys, unreadable = read("gh pr merge 12", strict=True)
    assert keys == ["pr:acme/web#12"] and unreadable and unreadable[0].kind == MERGE
    assert not unreadable[0].refused_on_mac
    assert read("gh pr merge 12", strict=False) == (["pr:acme/web#12"], [])
    assert read("gh pr merge 12 -R Acme/Web", strict=True) == (["pr:acme/web#12"], [])


def test_a_branch_remote_set_outside_the_repo_makes_a_bare_push_unsure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    web = _repo(tmp_path / "web")
    (tmp_path / ".gitconfig").write_text('[branch "main"]\n\tremote = fork\n')

    reported = gbridge.read_git_info(str(web))
    local = read_repo_facts(str(web))

    assert local is not None and local.custom_push and local.tracking == {}
    assert reported is not None and reported["custom_push"] is True


def test_the_gates_send_api_closes_and_continued_lines_to_the_guard() -> None:
    globs = list_guard_globs()

    for command in (
        "gh api -X PATCH repos/Acme/Web/issues/12 -f state=closed",
        "git pu\\\nsh origin main",
        "gh pr mer\\\nge 12",
        "gh api -X PATCH repos/Acme/Web/pulls/12 -f state=closed",
        CLOSE_MUTATION,
    ):
        assert gbridge.may_need_guard(globs, command), command
    assert not gbridge.may_need_guard(globs, "gh api user")


def test_gbridge_reads_includes_rewrites_and_extra_config_files_as_the_mac_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    web = _repo(tmp_path / "web")
    config = web / ".git" / "config"
    plain = config.read_text()

    def facts() -> tuple[bool, bool]:
        reported = gbridge.read_git_info(str(web))
        local = read_repo_facts(str(web))
        assert reported is not None and local is not None
        assert {k: v for k, v in reported.items() if k != "repo"} == asdict(local)
        return local.custom_push, local.custom_remote

    assert facts() == (False, False)
    for extra in (
        "[include]\n\tpath = more.conf\n",
        '[includeIf "gitdir:~/w/"]\n\tpath = more.conf\n',
        '[url "git@github.com:Acme/Protected"]\n\tinsteadOf = git@github.com:Acme/Web\n',
        '[remote "origin"]\n\turl = https://github.com/Acme/Protected.git\n',
        '[remote "origin"] pushurl = https://github.com/Acme/Protected.git\n',
    ):
        config.write_text(plain + extra)
        assert facts() == (False, True), extra
    config.write_text(plain + '[branch "main"]\n\tpushRemote = fork\n')
    assert facts() == (True, False)
    config.write_text(plain)
    (tmp_path / "xdg" / "git").mkdir(parents=True)
    (tmp_path / "xdg" / "git" / "config").write_text("[remote]\n\tpushDefault = fork\n[push]\n\tdefault = current\n")
    assert facts() == (True, False)
    assert read_repo_facts(str(web)).push_default == "current"  # type: ignore[union-attr]
    (tmp_path / "xdg" / "git" / "config").write_text("")
    (web / ".git" / "config.worktree").write_text('[remote "origin"]\n\tpushurl = x\n')
    assert facts() == (False, True)


def test_gbridge_reports_no_checkout_when_the_environment_picks_another_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    web = _repo(tmp_path / "web")
    assert gbridge.read_guard_git_info(str(web)) is not None

    monkeypatch.setenv("GH_REPO", "Acme/Other")

    assert gbridge.read_guard_git_info(str(web)) is None


def test_a_host_workers_linear_title_goes_to_the_macs_title_check_and_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = _gbridge_home(tmp_path, 1)
    (home / gbridge.GLOBS_FILE).write_text(json.dumps(list_guard_globs()))
    sent: list[dict[str, Any]] = []
    answers: list[Any] = []

    def fake_post(home: Path, path: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        sent.append(body)
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return cast(dict[str, Any], answer)

    def save(title: str) -> str:
        payload = {
            "session_id": CONV, "cwd": "/h", "tool_name": LINEAR_ISSUE_TOOLS[0],
            "tool_input": {"title": title, "description": "long text", "team": "PROJ"},
        }  # fmt: skip
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        gbridge.run_guard(home)
        return capsys.readouterr().out

    monkeypatch.setattr(gbridge, "post", fake_post)
    assert save("Fix the login bug") == ""
    assert sent == []
    answers.append({"checked": True, "output": '{"denied": 1}'})
    assert save("🤖 Claude: Fix the login bug") == '{"denied": 1}'
    assert sent[0]["payload"]["tool_name"] == LINEAR_ISSUE_TOOLS[0]
    assert sent[0]["payload"]["tool_input"] == {"title": "🤖 Claude: Fix the login bug"}
    assert sent[0]["checkouts"] == []
    answers.append(OSError("connection refused"))
    out = save("🤖 Claude: Fix the login bug")
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "this Linear issue save was not run" in out


def test_the_worker_api_applies_the_title_check_to_a_linear_save(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _host_worker(bridge_env, spawn_process, monkeypatch)
    payload: dict[str, object] = {
        "session_id": CONV, "cwd": "/h", "tool_name": LINEAR_ISSUE_TOOLS[0],
        "tool_input": {"title": "🤖 Claude: Fix the login bug"},
    }  # fmt: skip

    answer = handle_host_hook(record, load_config(), "PreToolUse", payload, None)

    assert answer["checked"] is True
    decision = json.loads(str(answer["output"]))["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "Fix the login bug" in decision["permissionDecisionReason"]


def test_a_host_worker_from_a_directory_not_committed_is_refused_and_leaves_no_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    shared = _repo(tmp_path / "acme")
    (shared / "_sessions" / "today").mkdir(parents=True)
    (shared / "_sessions" / "today" / "notes.md").write_text("untracked")

    refused = host_side.add_worktree("g-b-0000", str(shared / "_sessions" / "today"))
    assert isinstance(refused, str) and "_sessions/today is not in" in refused and "--cwd" in refused
    assert not Path(host_side.worktree_dir("g-b-0000")).exists()
    assert "g-b-0000" not in _git(shared, "worktree", "list")


def test_a_push_the_guard_passes_on_a_host_records_the_branch_and_the_repo_it_went_to(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.db")
    conn.execute(
        "INSERT INTO instances (instance_id, pid, proc_start_ms, conversation_id, repo, first_seen_at,"
        " last_observed_at) VALUES ('i', 1, 1, ?, 'Acme/Web', 0, 0)",
        (CONV,),
    )
    payload: dict[str, object] = {
        "session_id": CONV, "cwd": "/home/dev/web", "tool_name": "Bash",
        "tool_input": {"command": "git push origin HEAD:feat/y"},
    }  # fmt: skip
    config = map_toml_to_config({})
    reader = _host_reader(HOST_FACTS, "/home/dev/web")

    on_host = handle_pre_tool_use(conn, payload, config, reader)
    to_fork = handle_pre_tool_use(
        conn, {**payload, "tool_input": {"command": "git push fork HEAD:feat/z"}}, config, reader
    )
    on_mac = handle_pre_tool_use(conn, {**payload, "session_id": "mac"}, config)

    assert on_host == "" and to_fork == "" and on_mac == ""
    assert [tuple(r) for r in conn.execute("SELECT conversation_id, branch, repo FROM review_branch_writes")] == [
        (CONV, "feat/y", "acme/web"),
        (CONV, "feat/z", "me/web"),
    ]
