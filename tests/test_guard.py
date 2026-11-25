import json
import os
import subprocess
import time
from pathlib import Path
from unittest import mock

from conftest import VENV_BIN, BridgeEnv, SpawnProcess
from test_board import Pair

from bridge.db import transaction
from bridge.guard import (
    GITHUB_STALE_S,
    Unreadable,
    find_merge_blocks,
    map_command_to_targets,
    map_merge_blocks_to_lines,
    sync_claims_flag,
)
from bridge.install import convert_event_to_command


def repo(path: Path, branch: str = "feat/x") -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", branch], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:Acme/Web.git"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "fork", "git@github.com:me/web.git"], cwd=path, check=True)
    return path


def keys(command: str, cwd: Path) -> list[str]:
    return [t.key for t in map_command_to_targets(command, str(cwd))]


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
    assert keys("git push origin feat/x --tags", web) == ["branch:acme/web:feat/x"]
    assert keys("git push --prune origin feat/x", web) == ["branch:acme/web:feat/x"]
    assert keys("git push origin --tags", web) == []
    assert keys("git push --prune", web) == ["branch:acme/web:feat/x"]
    assert keys("git push --all --prune origin", web) == ["branch:acme/web:"]
    assert keys("GIT_TRACE=1 git push origin a b", web) == ["branch:acme/web:a", "branch:acme/web:b"]
    assert keys("gh pr merge 12 --squash", web) == ["pr:acme/web#12"]
    assert keys("gh pr merge https://github.com/Acme/Api/pull/7", web) == ["pr:acme/api#7"]
    assert keys("gh pr close -R acme/api feat/y", web) == ["branch:acme/api:feat/y"]
    assert keys("gh pr merge", web) == ["branch:acme/web:feat/x"]


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
    assert keys("git push origin --delete feat/x", web) == ["branch:acme/web:feat/x"]
    assert keys("git push -d origin feat/x refs/heads/feat/y", web) == [
        "branch:acme/web:feat/x",
        "branch:acme/web:feat/y",
    ]
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
    (web / ".git" / "config").write_text((web / ".git" / "config").read_text() + '[remote "origin"]\n\tpushurl = x\n')
    unreadable: list[Unreadable] = []
    # The Mac keeps its best reading, so a claim still holds, and fails closed where it must (hosts).
    assert [t.key for t in map_command_to_targets("git push", str(web), unreadable)] == ["branch:acme/web:feat/x"]
    assert [u.text for u in unreadable] == ["git push"]


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


def test_a_merge_waits_for_approval_owners_comments_and_green_ci_by_number_and_by_branch(
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
    assert "Acme/Web#12 is not mergeable yet: the owner has not reviewed it" in refusal("gh pr merge 12")
    update("""UPDATE review_prs SET approved_by = '["mira"]'""")
    assert "a colleague's or the bot's approval does not stand in" in refusal("gh pr merge 12")
    update("UPDATE review_prs SET approved_by = '[]', reviewed_at = 600")
    assert "Acme/Web#12 is not mergeable yet: not approved." in refusal("gh pr merge 12")
    update("""UPDATE review_prs SET approved_by = '["mira"]', open_threads = 1, his_open_threads = 1""")
    for command in ("gh pr merge 12 --squash", "gh pr merge", "gh api -X PUT repos/Acme/Web/pulls/12/merge"):
        assert "the owner's comments are open (1 review thread is unresolved)" in refusal(command)
    assert _run_guard(bridge_env, pair.b, "conv-b", "gh pr merge 13", web) == ""
    update("UPDATE review_prs SET open_threads = 0, his_open_threads = 0, checks = 'PENDING'")
    assert "not mergeable yet: CI pending." in refusal("gh pr merge 12")
    update("UPDATE review_prs SET checks = NULL, merge_state = 'BLOCKED'")
    assert "not mergeable yet: blocked on GitHub." in refusal("gh pr merge 12")
    update("""UPDATE review_prs SET merge_state = 'CLEAN', dep_blockers = '["waits for Acme/Lib#3 to merge"]'""")
    assert "waits for Acme/Lib#3 to merge (the PRs its body names merge first" in refusal("gh pr merge 12")
    update("UPDATE review_prs SET dep_blockers = NULL")
    assert (bridge_env.state_dir / "claims-active").exists()
    passed = json.loads(_run_guard(bridge_env, pair.b, "conv-b", "gh pr merge 12", web))["hookSpecificOutput"]
    assert "permissionDecision" not in passed
    assert passed["additionalContext"].startswith("bridge: no claim held on pr:Acme/Web#12:")
    update("UPDATE review_prs SET closed_at = 1")
    assert not (bridge_env.state_dir / "claims-active").exists()


def test_a_merge_that_deletes_a_kept_sandbox_is_refused_once_per_session_and_head_and_passes_when_run_again(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    from test_reviews import make_pr

    from bridge.reviews import store_open_prs

    pair = Pair(bridge_env, spawn_process)
    web = repo(bridge_env.root / "web")
    now = time.time_ns() // 1_000_000
    ready = {"repo": "Acme/Web", "head_oid": "c1", "approved_by": ("mira",)}
    with bridge_env.connect() as conn, transaction(conn):
        store_open_prs(
            conn,
            [
                make_pr(ref="pr:Acme/Web#12", number=12, head="feat/x", persist_sandbox=True, **ready),
                make_pr(ref="pr:Acme/Web#13", number=13, head="feat/y", **ready),
            ],
            500,
        )
        conn.execute("UPDATE review_prs SET reviewed_at = 600, merge_state = 'CLEAN', merge_block = NULL")
        conn.execute("INSERT OR REPLACE INTO review_poll (id, ok_at) VALUES (1, ?)", (now,))
        with mock.patch.dict(os.environ, {"BRIDGE_STATE_DIR": str(bridge_env.state_dir)}):
            sync_claims_flag(conn)

    def denied(command: str, pid: int = pair.b, conversation: str = "conv-b") -> bool:
        out = _run_guard(bridge_env, pid, conversation, command, web)
        return bool(out) and json.loads(out)["hookSpecificOutput"].get("permissionDecision") == "deny"

    def update(sql: str, *args: object) -> None:
        with bridge_env.connect() as conn, transaction(conn):
            conn.execute(sql, args)

    assert not denied("gh pr merge 12 --disable-auto")
    assert not denied("gh pr merge 12 --help")
    first = json.loads(_run_guard(bridge_env, pair.b, "conv-b", "gh api -X PUT repos/Acme/Web/pulls/12/merge", web))
    reason = first["hookSpecificOutput"]["permissionDecisionReason"]
    assert "Acme/Web#12 asks in its body to keep its persistent sandbox (PERSIST_SANDBOX)" in reason
    assert "deletes sandbox pr-12" in reason and "run the same merge again within 10 minutes" in reason
    assert denied("gh pr merge 12", pair.a, "conv-a")
    assert not denied("gh pr merge 12", pair.a, "conv-a")
    assert not denied("gh pr merge 13")
    update("UPDATE review_prs SET head_oid = 'c2'")
    assert denied("gh pr merge 12", pair.a, "conv-a")
    assert not denied("gh pr merge 12", pair.a, "conv-a")
    update("UPDATE review_prs SET sandbox_warned_at = ?", now - 11 * 60_000)
    assert denied("gh pr merge 12", pair.a, "conv-a")
    update("UPDATE review_prs SET sandbox_warned_for = NULL, merge_ok_oid = 'c2'")
    assert not denied("gh pr merge 12")


def test_an_owner_merge_ok_skips_the_sandbox_warning_only_for_the_head_github_says_is_merged(tmp_path: Path) -> None:
    from test_reviews import CONFIG, make_pr

    from bridge.db import connect
    from bridge.guard import find_sandbox_merges
    from bridge.reviews import store_open_prs

    web = repo(tmp_path / "web")
    conn = connect(tmp_path / "t.db")
    with transaction(conn):
        store_open_prs(
            conn,
            [make_pr(ref="pr:Acme/Web#12", repo="Acme/Web", number=12, head="feat/x", persist_sandbox=True)],
            500,
        )
        conn.execute("""UPDATE review_prs SET approved_by = '["mira"]', reviewed_at = 600, merge_ok_oid = 'a1'""")
        conn.execute("INSERT OR REPLACE INTO review_poll (id, ok_at) VALUES (1, 1000)")
    targets = map_command_to_targets("gh pr merge 12", str(web))
    now = 1000 + GITHUB_STALE_S * 1000 + 1
    heads: dict[str, str] = {}
    with mock.patch(
        "bridge.review_github.fetch_merge_facts", return_value=(False, True, None, "CLEAN", 0, "b2", (), ())
    ):
        assert find_merge_blocks(conn, CONFIG, targets, now, heads=heads) == []
    assert heads == {"pr:Acme/Web#12": "b2"}
    [warned] = find_sandbox_merges(conn, targets, now, "conv-b", heads)
    assert warned.warned_for == "conv-b b2"
    assert find_sandbox_merges(conn, targets, now, "conv-b", {"pr:Acme/Web#12": "a1"}) == []


def test_recording_a_sandbox_warning_waits_on_a_busy_database_only_until_the_merge_checks_deadline(
    tmp_path: Path,
) -> None:
    from test_reviews import make_pr

    from bridge.db import connect
    from bridge.guard import SandboxMerge
    from bridge.hooks import _record_sandbox_warnings  # pyright: ignore[reportPrivateUsage]
    from bridge.reviews import store_open_prs

    conn = connect(tmp_path / "t.db")
    with transaction(conn):
        store_open_prs(conn, [make_pr(ref="pr:Acme/Web#12", repo="Acme/Web", number=12, persist_sandbox=True)], 500)
    row = conn.execute("SELECT * FROM review_prs").fetchone()
    other = connect(tmp_path / "t.db")
    other.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    _record_sandbox_warnings(conn, [SandboxMerge(row, "conv-b a1")], 700, started + 0.1)
    assert time.monotonic() - started < 1
    other.rollback()
    _record_sandbox_warnings(conn, [SandboxMerge(row, "conv-b a1")], 700, time.monotonic() + 1)
    assert tuple(conn.execute("SELECT sandbox_warned_at, sandbox_warned_for FROM review_prs").fetchone()) == (
        700,
        "conv-b a1",
    )


def test_a_stale_queue_reads_github_once_and_an_unreadable_pr_is_refused(tmp_path: Path) -> None:
    from test_reviews import CONFIG, make_pr

    from bridge.db import connect
    from bridge.review_github import GitHubError
    from bridge.reviews import store_open_prs

    web = repo(tmp_path / "web")
    conn = connect(tmp_path / "t.db")
    with transaction(conn):
        store_open_prs(conn, [make_pr(ref="pr:Acme/Web#12", repo="Acme/Web", number=12, head="feat/x")], 500)
        conn.execute("""UPDATE review_prs SET approved_by = '["mira"]', reviewed_at = 600""")
        conn.execute("INSERT OR REPLACE INTO review_poll (id, ok_at) VALUES (1, 1000)")
    targets = map_command_to_targets("gh pr merge 12", str(web))
    now = 1000 + GITHUB_STALE_S * 1000 + 1
    with mock.patch(
        "bridge.review_github.fetch_merge_facts", return_value=(False, True, "FAILURE", "CLEAN", 0, "a1", (), ())
    ) as fetch:
        [refused] = find_merge_blocks(conn, CONFIG, targets, now)
    assert fetch.call_args.args[:3] == (CONFIG, "Acme/Web", 12)
    assert refused.waits == ("CI red",)
    with mock.patch(
        "bridge.review_github.fetch_merge_facts", return_value=(False, True, None, "CLEAN", 0, "a1", (), ())
    ):
        assert find_merge_blocks(conn, CONFIG, targets, now) == []
    with transaction(conn):
        conn.execute("UPDATE review_prs SET merge_block = '1 review thread is unresolved'")
    with mock.patch(
        "bridge.review_github.fetch_merge_facts", return_value=(False, True, None, "CLEAN", 0, "a1", (), ())
    ):
        assert find_merge_blocks(conn, CONFIG, targets, now) == []
    with mock.patch(
        "bridge.review_github.fetch_merge_facts", return_value=(False, True, None, "CLEAN", 2, "a1", (), ())
    ):
        [threads] = find_merge_blocks(conn, CONFIG, targets, now)
    assert threads.waits == ("comments open",) and threads.merge_block == "2 review threads are unresolved"
    with mock.patch("bridge.review_github.fetch_merge_facts", side_effect=GitHubError("gh failed: timeout")):
        [unread] = find_merge_blocks(conn, CONFIG, targets, now)
    [line] = map_merge_blocks_to_lines([unread], "the owner", "The owner")
    assert "cannot be checked for merging: GitHub could not be read (gh failed: timeout)" in line
    fresh = 1000 + GITHUB_STALE_S * 1000
    # A fresh cache's refusal is confirmed live: it stands when GitHub agrees or can't be read, and lifts when GitHub
    # has moved on (a CI rerun or an approval one poll behind the cache).
    with mock.patch(
        "bridge.review_github.fetch_merge_facts", return_value=(False, True, None, "CLEAN", 1, "a1", (), ())
    ) as fetch:
        [cached] = find_merge_blocks(conn, CONFIG, targets, fresh)
    fetch.assert_called_once()
    assert cached.waits == ("comments open",)
    with mock.patch("bridge.review_github.fetch_merge_facts", side_effect=GitHubError("gh failed: timeout")):
        [kept] = find_merge_blocks(conn, CONFIG, targets, fresh)
    assert kept.waits == ("comments open",) and kept.error is None
    with transaction(conn):
        conn.execute("UPDATE review_prs SET checks = 'PENDING', merge_block = NULL")
    with mock.patch(
        "bridge.review_github.fetch_merge_facts", return_value=(False, True, "SUCCESS", "CLEAN", 0, "a1", (), ())
    ):
        assert find_merge_blocks(conn, CONFIG, targets, fresh) == []
    with transaction(conn):
        conn.execute("UPDATE review_prs SET checks = NULL")
    with mock.patch("bridge.review_github.fetch_merge_facts") as fetch:
        assert find_merge_blocks(conn, CONFIG, targets, fresh) == []
    fetch.assert_not_called()


def test_a_wait_on_a_body_sentence_holds_only_while_the_body_read_live_still_says_it(tmp_path: Path) -> None:
    from test_reviews import CONFIG, make_pr

    from bridge.db import connect
    from bridge.review_deps import map_markdown_to_preconditions
    from bridge.review_github import GitHubError
    from bridge.reviews import store_open_prs

    held_body = "Adds the warp drive. Do not merge until the reactor is cooled."
    [said] = map_markdown_to_preconditions(held_body, "Acme/Web", 13)
    web = repo(tmp_path / "web")
    conn = connect(tmp_path / "t.db")
    with transaction(conn):
        store_open_prs(
            conn,
            [make_pr(ref=f"pr:Acme/Web#{n}", repo="Acme/Web", number=n, head=f"feat/{n}") for n in (12, 13)],
            500,
        )
        conn.execute("""UPDATE review_prs SET approved_by = '["mira"]', reviewed_at = 600""")
        conn.execute("INSERT OR REPLACE INTO review_poll (id, ok_at) VALUES (1, 1000)")

    def blocks(number: int, body: str | Exception) -> list[str]:
        targets = map_command_to_targets(f"gh pr merge {number}", str(web))
        with (
            mock.patch("bridge.review_github.fetch_pr_body", side_effect=[body]) as read,
            mock.patch("bridge.review_github.fetch_merge_facts", side_effect=GitHubError("gh failed: timeout")),
        ):
            refusals = find_merge_blocks(conn, CONFIG, targets, 1000)
        assert read.call_args.args[:2] == ("Acme/Web", 13)
        return [refusal.waits_on or "" for refusal in refusals]

    with transaction(conn):
        conn.execute("UPDATE review_prs SET dep_blockers = ? WHERE number = 13", (json.dumps([said]),))
    assert blocks(13, held_body) == [said]
    assert blocks(13, "Adds the warp drive.") == []
    [stale] = blocks(13, GitHubError("gh failed: timeout"))
    assert stale.startswith(said) and "as of the queue's last poll: reading Acme/Web#13 (gh failed: timeout)" in stale

    with transaction(conn):
        conn.execute(
            "UPDATE review_prs SET siblings = ?, sibling_quotes = ? WHERE number = 12",
            (json.dumps(["pr:Acme/Web#13"]), json.dumps({"pr:Acme/Web#13": ' (its body says: "Goes with #13.")'})),
        )
    assert blocks(12, held_body) == [
        f'goes with Acme/Web#13 (its body says: "Goes with #13."), which is not ready: {said}'
    ]
    assert blocks(12, "Adds the warp drive.") == []


def test_a_wait_on_a_pr_quoting_a_body_sentence_holds_while_the_body_read_live_still_names_that_pr(
    tmp_path: Path,
) -> None:
    from test_reviews import CONFIG, make_pr

    from bridge.db import connect
    from bridge.review_deps import map_dependencies_to_quotes, map_markdown_to_dependencies, map_states_to_blockers
    from bridge.review_github import GitHubError
    from bridge.reviews import store_open_prs

    def map_body_to_waits(body: str, number: int = 13) -> list[str]:
        said = map_markdown_to_dependencies(body, "Acme/Web", number)
        refs = tuple(ref for ref, _ in said)
        return list(map_states_to_blockers(refs, set(), dict.fromkeys(refs, "OPEN"), map_dependencies_to_quotes(said)))

    [stored] = map_body_to_waits("Adds the warp drive. Merge after Acme/Web#12, not before.")
    web = repo(tmp_path / "web")
    conn = connect(tmp_path / "t.db")
    with transaction(conn):
        store_open_prs(
            conn,
            [make_pr(ref=f"pr:Acme/Web#{n}", repo="Acme/Web", number=n, head=f"feat/{n}") for n in (12, 13, 14)],
            500,
        )
        conn.execute("""UPDATE review_prs SET approved_by = '["mira"]', reviewed_at = 600""")
        conn.execute("INSERT OR REPLACE INTO review_poll (id, ok_at) VALUES (1, 1000)")
        conn.execute(
            """UPDATE review_prs SET dep_blockers = ?, must_merge = '["Acme/Web#12"]' WHERE number = 13""",
            (json.dumps([stored]),),
        )

    def blocks(*bodies: str | Exception) -> tuple[list[str], list[int]]:
        targets = map_command_to_targets("gh pr merge 13", str(web))
        with (
            mock.patch("bridge.review_github.fetch_pr_body", side_effect=bodies) as read,
            mock.patch("bridge.review_github.fetch_merge_facts", side_effect=GitHubError("gh failed: timeout")),
        ):
            refusals = find_merge_blocks(conn, CONFIG, targets, 1000)
        return [refusal.waits_on or "" for refusal in refusals], [call.args[1] for call in read.call_args_list]

    def quoting(sentence: str) -> str:
        return stored.partition(" (")[0] + f' (its body says: "{sentence}")'

    assert blocks("Adds the warp drive.") == ([], [13]), "the body no longer names the PR"
    edited = "Adds the warp drive. Merge only after Acme/Web#12 is in."
    [now] = map_body_to_waits(edited)
    assert blocks(edited)[0] == [now] and "is in." in now, "the live sentence is quoted"
    for sentence in ("Merge once #12 is merged.", "Do not merge before #12 is merged."):
        assert blocks(f"Adds the warp drive. {sentence}")[0] == [quoting(sentence)], "other words that name it"
    url = "Merge after #12. History: https://github.com/Other/Web/pull/12."
    assert blocks(url)[0] == [quoting("Merge after #12.")], "a link to another repo's #12 does not move the bare #12"
    [[new], _] = blocks("Adds the warp drive. Merge after #14.")
    assert new == (
        "its body now says it merges after Acme/Web#14, which the queue has not read yet: wait for its next poll"
        ' (its body says: "Merge after #14.")'
    )
    [[unread], _] = blocks(GitHubError("gh failed: timeout"))
    assert unread.startswith(stored) and "as of the queue's last poll" in unread

    [sibling_wait] = map_body_to_waits("Merge after Acme/Web#12.", 14)
    with transaction(conn):
        conn.execute(
            """UPDATE review_prs SET dep_blockers = ?, must_merge = '["Acme/Web#12"]' WHERE number = 14""",
            (json.dumps([sibling_wait]),),
        )
        conn.execute(
            """UPDATE review_prs SET siblings = '["pr:Acme/Web#14"]', sibling_quotes = '{}' WHERE number = 13"""
        )
    assert blocks(edited)[1] == [13], "its own live wait decides: the sibling's body is not read"
    with transaction(conn):
        conn.execute("UPDATE review_prs SET dep_blockers = '[]', must_merge = '[]' WHERE number = 13")
    assert blocks("Adds the warp drive.") == ([], [14]), "the sibling's body no longer names the PR"
    [[waits], _] = blocks("Merge once Acme/Web#12 is in.")
    assert "which is not ready" in waits and waits.endswith('(its body says: "Merge once Acme/Web#12 is in.")')


def test_a_merge_refused_for_ci_github_would_not_run_names_github_only_at_the_head_it_was_read_for(
    tmp_path: Path,
) -> None:
    from test_reviews import CONFIG, make_pr

    from bridge.db import connect
    from bridge.review_github import CI_BLOCKED_BY_BILLING, GitHubError
    from bridge.reviews import store_ci_blocked_by, store_open_prs

    web = repo(tmp_path / "web")
    conn = connect(tmp_path / "t.db")
    with transaction(conn):
        store_open_prs(conn, [make_pr(ref="pr:Acme/Web#12", repo="Acme/Web", number=12, checks="FAILURE")], 500)
        conn.execute("""UPDATE review_prs SET approved_by = '["mira"]', reviewed_at = 600""")
    targets = map_command_to_targets("gh pr merge 12", str(web))

    def line(live_head: str | None = "a1", live: bool = True) -> str:
        if live:
            facts = mock.patch(
                "bridge.review_github.fetch_merge_facts",
                return_value=(False, True, "FAILURE", "CLEAN", 0, live_head, (), ()),
            )
        else:
            facts = mock.patch("bridge.review_github.fetch_merge_facts", side_effect=GitHubError("gh failed: timeout"))
        with facts:
            [refused] = find_merge_blocks(conn, CONFIG, targets, 1000)
        assert refused.waits == ("CI red",)
        [text] = map_merge_blocks_to_lines([refused], "the owner", "The owner")
        return text

    assert "is not mergeable yet: CI red. The owner's rule" in line()
    with transaction(conn):
        store_ci_blocked_by(conn, "pr:Acme/Web#12", "a1", CI_BLOCKED_BY_BILLING)
    assert (
        "is not mergeable yet: CI didn't run: GitHub billing (not a code failure; rerun the failed jobs once GitHub "
        "runs them again). The owner's rule" in line()
    )
    assert "is not mergeable yet: CI red." in line("b2"), "the reason was read for an older head"
    with transaction(conn):
        conn.execute("INSERT OR REPLACE INTO review_poll (id, ok_at) VALUES (1, 1000)")
    assert "CI didn't run: GitHub billing" in line(live=False), "a fresh queue's own reason stands unconfirmed"


def test_the_live_read_is_short_and_counts_only_owners_unresolved_threads() -> None:
    from test_reviews import CONFIG

    from bridge.review_github import fetch_merge_facts

    def thread(resolved: bool, login: str) -> dict[str, object]:
        return {"isResolved": resolved, "comments": {"nodes": [{"author": {"login": login}}]}}

    review = {"state": "APPROVED", "author": {"__typename": "User", "login": "mira"}}
    answer: dict[str, object] = {"data": {"repository": {"pullRequest": {
        "isDraft": False, "mergeStateStatus": "CLEAN",
        "commits": {"nodes": [{"commit": {"oid": "a1", "statusCheckRollup": None}}]},
        "reviews": {"nodes": [review]},
        "comments": {"nodes": []},
        "reviewThreads": {"nodes": [thread(False, CONFIG.review_author.upper()), thread(True, CONFIG.review_author),
                                    thread(False, "mira")]},
    }}}}  # fmt: skip
    with mock.patch("bridge.review_github.run_gh", return_value=json.dumps(answer)) as gh:
        assert fetch_merge_facts(CONFIG, "Acme/Web", 12) == (False, True, None, "CLEAN", 1, "a1", ("mira",), ())
    assert gh.call_args.args[1] <= 3
