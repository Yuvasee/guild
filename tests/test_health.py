import json
from datetime import UTC, datetime

from conftest import BridgeEnv, SpawnProcess

from bridge.health import HOOKS_SILENT_MS, check_hooks


def _entry(kind: str, at_ms: int, text: str = "x", meta: bool = False) -> str:
    stamp = datetime.fromtimestamp(at_ms / 1000, UTC).isoformat().replace("+00:00", "Z")
    entry = {"type": kind, "timestamp": stamp, "isMeta": meta, "message": {"content": text}}
    return json.dumps(entry, separators=(",", ":")) + "\n"


def test_hooks_are_silent_when_sessions_talk_and_no_hook_follows(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    pid = spawn_process()
    bridge_env.write_session_file(pid, "conv-h")
    transcript = bridge_env.root / "h.jsonl"
    now = int(datetime.now(UTC).timestamp() * 1000)
    transcript.write_text(_entry("user", now - 600_000))
    bridge_env.hook(
        "UserPromptSubmit", {"session_id": "conv-h", "transcript_path": str(transcript), "prompt": "go"}, pid
    )

    with bridge_env.connect() as conn:
        fresh = check_hooks(conn, now)
        assert fresh.last_turn_at == now - 600_000 and not fresh.silent
        conn.execute("UPDATE hook_runs SET at = ?", (now - HOOKS_SILENT_MS - 120_000,))
        # Titles, meta entries and local commands are written without hooks, so they say nothing about them.
        with transcript.open("a") as f:
            f.write(_entry("ai-title", now))
            f.write(_entry("user", now, meta=True))
            f.write(_entry("user", now, text="<local-command-stdout>ok</local-command-stdout>"))
        assert not check_hooks(conn, now).silent
        with transcript.open("a") as f:
            f.write(_entry("assistant", now))
        health = check_hooks(conn, now)
        assert health.silent and health.last_turn_at == now
        assert not check_hooks(conn, now + 2 * 3_600_000).silent, "yesterday's gap is not today's alarm"
        # A turn larger than the first tail read is still found.
        with transcript.open("a") as f:
            f.write(_entry("assistant", now + 1, text="y" * 300_000))
        assert check_hooks(conn, now).last_turn_at == now + 1


def test_no_sessions_is_not_silence(bridge_env: BridgeEnv) -> None:
    with bridge_env.connect() as conn:
        assert not check_hooks(conn, int(datetime.now(UTC).timestamp() * 1000)).silent
