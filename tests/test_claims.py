import os
import signal
import subprocess
from concurrent.futures import ThreadPoolExecutor

from conftest import VENV_BIN, BridgeEnv, SpawnProcess, needs_github
from test_board import Pair


def test_simultaneous_claims_have_one_winner(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pids = [spawn_process() for _ in range(10)]
    for i, pid in enumerate(pids):
        bridge_env.write_session_file(pid, f"conv-{i}")
    bridge_env.cli("sessions")

    def claim(pid: int) -> int:
        env = {**bridge_env.env, "CLAUDE_PID": str(pid)}
        return subprocess.run(
            [str(VENV_BIN / "bridge"), "claim", "env:Staging", "--reason", "load test"],
            env=env, capture_output=True, text=True, check=False,
        ).returncode  # fmt: skip

    with ThreadPoolExecutor(max_workers=10) as pool:
        codes = sorted(pool.map(claim, pids))
    assert codes == [0] + [3] * 9


def test_held_claim_handoff_with_token(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pair = Pair(bridge_env, spawn_process)
    first = bridge_env.cli("claim", "env:staging", "--reason", "regression run", pid=pair.a)
    token = first.stdout.split("token ")[1].split()[0]
    refused = bridge_env.cli("claim", "env:STAGING", pid=pair.b)
    wrong_token = bridge_env.cli("release", "env:staging", "--token", "nope", pid=pair.b)
    released = bridge_env.cli("release", "env:staging", "--token", token, pid=pair.a)
    second = bridge_env.cli("claim", "env:staging", pid=pair.b)

    assert first.returncode == 0 and first.stdout.startswith("claimed env:staging")
    assert refused.returncode == 3
    assert "HELD: env:staging held by a" in refused.stdout and "regression run" in refused.stdout
    assert 'bridge send a "need env:staging' in refused.stdout
    assert wrong_token.returncode == 2
    assert released.returncode == 0
    assert second.returncode == 0


def test_claim_of_ended_holder_is_taken_over_and_holder_told(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    pair = Pair(bridge_env, spawn_process)
    bridge_env.cli("claim", "issue:abc-7", pid=pair.a)
    os.kill(pair.a, signal.SIGKILL)
    os.waitpid(pair.a, 0)
    bridge_env.cli("sessions")

    taken = bridge_env.cli("claim", "issue:ABC-7", pid=pair.b)

    assert taken.returncode == 0, taken.stdout + taken.stderr
    assert taken.stdout.startswith("taken over: issue:ABC-7")
    with bridge_env.connect() as conn:
        told = conn.execute("SELECT body FROM messages m JOIN message_recipients r ON r.message_id = m.id "
                            "WHERE r.conversation_id = 'conv-a'").fetchone()[0]  # fmt: skip
    assert told == "I took over your claim on issue:ABC-7."


def test_expired_claim_of_live_holder_is_still_held(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pair = Pair(bridge_env, spawn_process)
    bridge_env.cli("claim", "env:demo", "--ttl", "1s", pid=pair.a)
    with bridge_env.connect() as conn:
        conn.execute("UPDATE claims SET expires_at = expires_at - 5000")
    refused = bridge_env.cli("claim", "env:demo", pid=pair.b)
    assert refused.returncode == 3
    assert "expired but the holder is still running" in refused.stdout


def test_invalid_resources_are_rejected(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pair = Pair(bridge_env, spawn_process)
    for resource in ("staging", "db:prod", "pr:abc"):
        result = bridge_env.cli("claim", resource, "--offline", pid=pair.a)
        assert result.returncode == 2, resource


@needs_github
def test_pr_and_head_branch_are_one_resource(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    """Uses the real GitHub API: a merged public PR whose head branch never changes."""
    pair = Pair(bridge_env, spawn_process)
    by_pr = bridge_env.cli("claim", "pr:cli/cli#1", pid=pair.a)
    by_branch = bridge_env.cli("claim", "branch:cli/cli:gh-pr", pid=pair.b)
    who = bridge_env.cli("who", "branch:cli/cli:gh-pr", pid=pair.b)

    assert by_pr.returncode == 0, by_pr.stderr
    assert "claimed pr:cli/cli#1 (= branch:cli/cli:gh-pr)" in by_pr.stdout
    assert by_branch.returncode == 3, by_branch.stdout + by_branch.stderr
    assert who.stdout.startswith("pr:cli/cli#1 held by a")
