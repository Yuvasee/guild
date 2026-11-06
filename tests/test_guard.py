import json
import os
import re
import subprocess
import time
from pathlib import Path
from unittest import mock

from conftest import VENV_BIN, BridgeEnv, SpawnProcess
from test_board import Pair

from bridge.config import GuardRule
from bridge.db import transaction
from bridge.guard import (
    GITHUB_STALE_S,
    find_merge_blocks,
    map_command_to_targets,
    map_merge_blocks_to_lines,
    sync_claims_flag,
)
from bridge.install import convert_event_to_command

STAGING = GuardRule(
    trigger="helm", pattern=re.compile(r"helm (upgrade|install|rollback)\b.*\bstaging\b"), resource="env:staging"
)


def repo(path: Path, branch: str = "feat/x") -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", branch], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:Acme/Web.git"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "fork", "git@github.com:me/web.git"], cwd=path, check=True)
    return path


def keys(command: str, cwd: Path) -> list[str]:
    return [t.key for t in map_command_to_targets(command, str(cwd), (STAGING,))]


def test_push_and_merge_commands_map_to_claim_keys(tmp_path: Path) -> None:
    web = repo(tmp_path / "web")
    other = repo(tmp_path / "other", branch="main")
    assert keys("git push", web) == ["branch:acme/web:feat/x"]
    assert keys("git push -u origin HEAD", web) == ["branch:acme/web:feat/x"]
    assert keys("git push --force-with-lease origin feat/x:refs/heads/release", web) == ["branch:acme/web:release"]
    assert keys("git push fork feat/x", web) == ["branch:me/web:feat/x"]
    assert keys(f"cd {other} && git push", web) == ["branch:acme/web:main"]
    assert keys(f"git -C {other} push origin +topic", web) == ["branch:acme/web:topic"]
    assert keys("git push --all", web) == ["branch:acme/web:"]
    assert keys("GIT_TRACE=1 git push origin a b", web) == ["branch:acme/web:a", "branch:acme/web:b"]
    assert keys("gh pr merge 12 --squash", web) == ["pr:acme/web#12"]
    assert keys("gh pr merge https://github.com/Acme/Api/pull/7", web) == ["pr:acme/api#7"]
    assert keys("gh pr close -R acme/api feat/y", web) == ["branch:acme/api:feat/y"]
    assert keys("gh pr merge", web) == ["branch:acme/web:feat/x"]
    assert keys("helm upgrade app ./chart -n staging", web) == ["env:staging"]


def test_read_only_commands_are_not_targets(tmp_path: Path) -> None:
    web = repo(tmp_path / "web")
    for command in ("git status", "git log --oneline", "gh pr view 12", "gh pr list", "echo git push", "helm list"):
        assert keys(command, web) == [], command
    assert keys("git fetch && git rebase origin/main", web) == []


def test_guard_denies_push_over_another_sessions_claim(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pair = Pair(bridge_env, spawn_process)
    web = repo(bridge_env.root / "web")
    claimed = bridge_env.cli("claim", "branch:acme/web:feat/x", "--offline", "--reason", "rebasing", pid=pair.a)
    assert claimed.returncode == 0, claimed.stderr

    from_b = _run_guard(bridge_env, pair.b, "conv-b", "git push origin feat/x", web)
    from_a = _run_guard(bridge_env, pair.a, "conv-a", "git push origin feat/x", web)
    unrelated = _run_guard(bridge_env, pair.b, "conv-b", "git push origin feat/other", web)

    decision = json.loads(from_b)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "branch:acme/web:feat/x is reserved by session a (rebasing)" in decision["permissionDecisionReason"]
    assert 'bridge send a "need branch:acme/web:feat/x' in decision["permissionDecisionReason"]
    assert from_a == "" and unrelated == ""


def test_guard_is_inert_without_claims(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pair = Pair(bridge_env, spawn_process)
    web = repo(bridge_env.root / "web")
    assert _run_guard(bridge_env, pair.b, "conv-b", "git push", web) == ""
    assert not (bridge_env.state_dir / "claims-active").exists()


def _run_guard(bridge_env: BridgeEnv, pid: int, conversation: str, command: str, cwd: Path) -> str:
    guard = convert_event_to_command(str(VENV_BIN / "bridge-hook"), "PreToolUse")
    payload = json.dumps(
        {"session_id": conversation, "cwd": str(cwd), "tool_name": "Bash", "tool_input": {"command": command}}
    )
    env = {**bridge_env.env, "CLAUDE_PID": str(pid)}
    done = subprocess.run(["/bin/sh", "-c", guard], input=payload, env=env, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_parsing_cases_from_review(tmp_path: Path) -> None:
    web = repo(tmp_path / "web")
    other = repo(tmp_path / "other", branch="main")
    assert keys("printf 'hello;git push origin main;world'", web) == []
    assert keys(f"false || cd {other}; git push", web) == []
    assert keys("git push --dry-run", web) == []
    assert keys("git push origin --delete feat/x", web) == []
    assert keys("git checkout release && git push", web) == ["branch:acme/web:release"]
    assert keys("git push origin 'refs/heads/*:refs/heads/*'", web) == []
    assert keys("cat <<EOF\ngit push\nEOF", web) == []
    assert keys("echo done # git push", web) == []
    assert keys("printf '%s\\n' 'helm upgrade app -n staging'", web) == []
    assert keys("printf '%s\\n' ';' git push origin main", web) == []
    assert keys("git push -nq origin main", web) == []
    assert keys("git push git@github.com:me/fork.git HEAD", web) == []
    assert keys(f"false && cd {other}; git push origin main", web) == []
    assert keys(f"make && cd {other} && git push origin main", web) == ["branch:acme/web:main"]
    assert keys("git -c push.default=upstream push", web) == []
    assert keys("git -cpush.default=upstream push", web) == []
    assert keys("printf %s foo\\; git push origin main", web) == []
    assert keys("git push origin 'ma'in", web) == ["branch:acme/web:main"]
    assert keys("cat <<'X' > notes.md\ngit push origin main\nX\ngit status", web) == []
    assert keys("echo '<<EOF'\ngit push origin main", web) == ["branch:acme/web:main"]
    assert keys("cat <<EOF\n EOF \ngit push origin main\nEOF\ngit status", web) == []
    helm_glob = GuardRule(trigger="helm*upgrade", pattern=re.compile(r"helm upgrade .*staging"), resource="env:staging")
    assert [t.key for t in map_command_to_targets("helm upgrade app -n staging", str(web), (helm_glob,))] == [
        "env:staging"
    ]
    (web / ".git" / "config").write_text((web / ".git" / "config").read_text() + '[remote "origin"]\n\tpushurl = x\n')
    assert keys("git push", web) == []


def test_shell_gate_reaches_python_for_spaced_and_dash_c_forms(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    pair = Pair(bridge_env, spawn_process)
    web = repo(bridge_env.root / "web")
    bridge_env.cli("claim", "branch:acme/web:feat/x", "--offline", pid=pair.a)
    assert "deny" in _run_guard(bridge_env, pair.b, "conv-b", "git  push", web)
    assert "deny" in _run_guard(bridge_env, pair.b, "conv-b", f"git -C {web} push", bridge_env.root)


def test_forced_takeover_raises_a_blocker_item(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pair = Pair(bridge_env, spawn_process)
    bridge_env.cli("claim", "env:staging", pid=pair.a)
    forced = bridge_env.cli("claim", "env:staging", "--force", pid=pair.b)
    with bridge_env.connect() as conn:
        items = [tuple(r) for r in conn.execute("SELECT severity, text FROM user_items")]
    assert forced.returncode == 0
    assert items == [("blocker", "b forced a takeover of env:staging from a")]


def test_a_merge_through_the_api_maps_to_its_pr_and_a_read_does_not(tmp_path: Path) -> None:
    web = repo(tmp_path / "web")
    assert keys("gh api -X PUT repos/Acme/Web/pulls/12/merge -f merge_method=squash", web) == ["pr:acme/web#12"]
    assert keys("gh api --method PUT /repos/Acme/Web/pulls/12/merge", web) == ["pr:acme/web#12"]
    assert keys("gh api repos/Acme/Web/pulls/12/merge", web) == []


def test_a_merge_waits_for_approval_owner_comments_and_green_ci_by_number_and_by_branch(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    from test_reviews import make_pr

    from bridge.reviews import store_merge_blocks, store_open_prs

    pair = Pair(bridge_env, spawn_process)
    web = repo(bridge_env.root / "web")
    now = time.time_ns() // 1_000_000

    def update(sql: str) -> None:
        with bridge_env.connect() as conn, transaction(conn):
            conn.execute(sql)
            store_merge_blocks(conn)
            with mock.patch.dict(os.environ, {"BRIDGE_STATE_DIR": str(bridge_env.state_dir)}):
                sync_claims_flag(conn)

    def refusal(command: str) -> str:
        decision = json.loads(_run_guard(bridge_env, pair.b, "conv-b", command, web))["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny", command
        return str(decision["permissionDecisionReason"])

    with bridge_env.connect() as conn:
        store_open_prs(conn, [make_pr(ref="pr:Acme/Web#12", repo="Acme/Web", number=12, head="feat/x")], 500)
        conn.execute("INSERT OR REPLACE INTO review_poll (id, ok_at) VALUES (1, ?)", (now,))
    update("UPDATE review_prs SET merge_block = NULL")
    assert "Acme/Web#12 is not mergeable yet: not approved." in refusal("gh pr merge 12")
    update("""UPDATE review_prs SET approved_by = '["mira"]', open_threads = 1, owner_open_threads = 1""")
    for command in ("gh pr merge 12 --squash", "gh pr merge", "gh api -X PUT repos/Acme/Web/pulls/12/merge"):
        assert "the owner's comments are open (1 review thread is unresolved)" in refusal(command)
    assert _run_guard(bridge_env, pair.b, "conv-b", "gh pr merge 13", web) == ""
    update("UPDATE review_prs SET open_threads = 0, owner_open_threads = 0, checks = 'PENDING'")
    assert "not mergeable yet: CI pending." in refusal("gh pr merge 12")
    update("UPDATE review_prs SET checks = NULL, merge_state = 'BLOCKED'")
    assert "not mergeable yet: blocked on GitHub." in refusal("gh pr merge 12")
    update("UPDATE review_prs SET merge_state = 'CLEAN'")
    assert (bridge_env.state_dir / "claims-active").exists()
    assert _run_guard(bridge_env, pair.b, "conv-b", "gh pr merge 12", web) == ""
    update("UPDATE review_prs SET closed_at = 1")
    assert not (bridge_env.state_dir / "claims-active").exists()


def test_a_stale_queue_reads_github_once_and_an_unreadable_pr_is_refused(tmp_path: Path) -> None:
    from test_reviews import CONFIG, make_pr

    from bridge.db import connect
    from bridge.review_github import GitHubError
    from bridge.reviews import store_open_prs

    web = repo(tmp_path / "web")
    conn = connect(tmp_path / "t.db")
    with transaction(conn):
        store_open_prs(conn, [make_pr(ref="pr:Acme/Web#12", repo="Acme/Web", number=12, head="feat/x")], 500)
        conn.execute("""UPDATE review_prs SET approved_by = '["mira"]'""")
        conn.execute("INSERT OR REPLACE INTO review_poll (id, ok_at) VALUES (1, 1000)")
    targets = map_command_to_targets("gh pr merge 12", str(web), ())
    now = 1000 + GITHUB_STALE_S * 1000 + 1
    with mock.patch("bridge.guard.fetch_merge_facts", return_value=(False, True, "FAILURE", "CLEAN", 0)) as fetch:
        [refused] = find_merge_blocks(conn, CONFIG, targets, now)
    fetch.assert_called_once_with(CONFIG, "Acme/Web", 12)
    assert refused.waits == ("CI red",)
    with mock.patch("bridge.guard.fetch_merge_facts", return_value=(False, True, None, "CLEAN", 0)):
        assert find_merge_blocks(conn, CONFIG, targets, now) == []
    with transaction(conn):
        conn.execute("UPDATE review_prs SET merge_block = '1 review thread is unresolved'")
    with mock.patch("bridge.guard.fetch_merge_facts", return_value=(False, True, None, "CLEAN", 0)):
        assert find_merge_blocks(conn, CONFIG, targets, now) == []
    with mock.patch("bridge.guard.fetch_merge_facts", return_value=(False, True, None, "CLEAN", 2)):
        [threads] = find_merge_blocks(conn, CONFIG, targets, now)
    assert threads.waits == ("comments open",) and threads.merge_block == "2 review threads are unresolved"
    with mock.patch("bridge.guard.fetch_merge_facts", side_effect=GitHubError("gh failed: timeout")):
        [unread] = find_merge_blocks(conn, CONFIG, targets, now)
    [line] = map_merge_blocks_to_lines([unread], "the owner", "The owner")
    assert "cannot be checked for merging: GitHub could not be read (gh failed: timeout)" in line
    fresh = 1000 + GITHUB_STALE_S * 1000
    # A fresh cache's refusal is confirmed live: it stands when GitHub agrees or can't be read, and lifts when GitHub
    # has moved on (a CI rerun or an approval one poll behind the cache).
    with mock.patch("bridge.guard.fetch_merge_facts", return_value=(False, True, None, "CLEAN", 1)) as fetch:
        [cached] = find_merge_blocks(conn, CONFIG, targets, fresh)
    fetch.assert_called_once()
    assert cached.waits == ("comments open",)
    with mock.patch("bridge.guard.fetch_merge_facts", side_effect=GitHubError("gh failed: timeout")):
        [kept] = find_merge_blocks(conn, CONFIG, targets, fresh)
    assert kept.waits == ("comments open",) and kept.error is None
    with transaction(conn):
        conn.execute("UPDATE review_prs SET checks = 'PENDING', merge_block = NULL")
    with mock.patch("bridge.guard.fetch_merge_facts", return_value=(False, True, "SUCCESS", "CLEAN", 0)):
        assert find_merge_blocks(conn, CONFIG, targets, fresh) == []
    with transaction(conn):
        conn.execute("UPDATE review_prs SET checks = NULL")
    with mock.patch("bridge.guard.fetch_merge_facts") as fetch:
        assert find_merge_blocks(conn, CONFIG, targets, fresh) == []
    fetch.assert_not_called()


def test_the_live_read_is_short_and_counts_only_owner_unresolved_threads() -> None:
    from test_reviews import CONFIG

    from bridge.review_github import fetch_merge_facts

    def thread(resolved: bool, login: str) -> dict[str, object]:
        return {"isResolved": resolved, "comments": {"nodes": [{"author": {"login": login}}]}}

    review = {"state": "APPROVED", "author": {"__typename": "User", "login": "mira"}}
    answer = {"data": {"repository": {"pullRequest": {
        "isDraft": False, "mergeStateStatus": "CLEAN",
        "commits": {"nodes": [{"commit": {"statusCheckRollup": None}}]},
        "reviews": {"nodes": [review]},
        "reviewThreads": {"nodes": [thread(False, CONFIG.review_author.upper()), thread(True, CONFIG.review_author),
                                    thread(False, "mira")]},
    }}}}  # fmt: skip
    with mock.patch("bridge.review_github.run_gh", return_value=json.dumps(answer)) as gh:
        assert fetch_merge_facts(CONFIG, "Acme/Web", 12) == (False, True, None, "CLEAN", 1)
    assert gh.call_args.args[1] <= 3
