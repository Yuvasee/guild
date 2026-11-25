import json
import time
from datetime import UTC, datetime
from pathlib import Path

from conftest import BridgeEnv, SpawnProcess

from bridge.board import SYSTEM_CONVERSATION, USER_CONVERSATION, Party, send_message
from bridge.compaction import (
    DEAD_FAILING,
    DEAD_REFUSED,
    DEAD_TOO_LONG,
    estimate_window,
    is_near_threshold,
    map_failed_to_death,
    map_window_to_compact_at,
    read_transcript_tail,
)
from bridge.db import now_ms, transaction
from bridge.feed import resolve_tab
from bridge.inject import Part, save_later_parts


def _stamp(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _assistant(tokens: int, at_ms: int) -> str:
    usage = {"input_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": tokens - 20}
    usage["output_tokens"] = 10
    entry = {"type": "assistant", "message": {"role": "assistant", "usage": usage}, "timestamp": _stamp(at_ms)}
    return json.dumps(entry) + "\n"


def _failed(text: str, at_ms: int, error: str = "invalid_request") -> str:
    """As Claude Code 2.1.319 writes a request the API refused."""
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    message = {"model": "<synthetic>", "role": "assistant", "usage": usage, "content": [{"type": "text", "text": text}]}
    entry = {
        "type": "assistant", "isSidechain": False, "message": message, "error": error, "isApiErrorMessage": True,
        "timestamp": _stamp(at_ms),
    }  # fmt: skip
    return json.dumps(entry) + "\n"


def _prompt(text: str) -> str:
    return json.dumps({"type": "user", "message": {"content": text}}) + "\n"


def _context(stdout: str) -> str:
    return str(json.loads(stdout)["hookSpecificOutput"]["additionalContext"]) if stdout.strip() else ""


def test_prompt_too_long_turns_mark_the_tab_dead_from_the_last_real_answer(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        _assistant(230_000, 1_000_000) + _prompt("x") + _failed("Prompt is too long", 2_000_000) + _prompt("y")
        + _failed("Prompt is too long", 3_000_000)
    )  # fmt: skip

    tail = read_transcript_tail(transcript)

    assert tail is not None and tail.reading is not None and tail.failed is not None
    assert (tail.reading.tokens, tail.reading.at_ms) == (230_000, 1_000_000)
    assert (tail.failed.count, tail.failed.too_long, tail.failed.first_at_ms) == (2, True, 2_000_000)
    assert map_failed_to_death(tail.failed) == DEAD_TOO_LONG


def test_other_api_errors_kill_the_tab_only_from_the_third_in_a_row(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    slept = "API Error: Your computer went to sleep mid-response."
    transcript.write_text(_assistant(50_000, 1_000) + _failed(slept, 2_000, "server_error") * 2)
    two = read_transcript_tail(transcript)
    with transcript.open("a") as f:
        f.write(_failed(slept, 3_000, "server_error"))
    three = read_transcript_tail(transcript)
    with transcript.open("a") as f:
        f.write(_assistant(51_000, 4_000))
    answered = read_transcript_tail(transcript)

    refused = tmp_path / "r.jsonl"
    refused.write_text(_assistant(50_000, 1_000) + _failed("input length exceeds the context limit", 2_000) * 3)
    refused_tail = read_transcript_tail(refused)

    assert refused_tail is not None and map_failed_to_death(refused_tail.failed) == DEAD_REFUSED
    assert two is not None and map_failed_to_death(two.failed) is None
    assert three is not None and map_failed_to_death(three.failed) == DEAD_FAILING
    assert answered is not None and answered.failed is None


def test_the_threshold_follows_the_tabs_own_window() -> None:
    assert map_window_to_compact_at(600_000, 0.75, None) == 600_000
    assert map_window_to_compact_at(600_000, 0.75, 1_000_000) == 600_000
    assert map_window_to_compact_at(600_000, 0.75, 200_000) == 150_000
    assert map_window_to_compact_at(0, 0.75, 200_000) == 0, "compaction off stays off"
    # 150k shown as 75%: the window is above 150k * 100 / 76, never taken as larger than it may be.
    assert estimate_window(150_000, 75) == 197_368


def test_a_200k_tab_is_asked_to_compact_at_150k_not_600k(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    config = Path(bridge_env.env["BRIDGE_CONFIG"])
    config.write_text(config.read_text() + "compact_at_tokens = 600000\n")
    pid = spawn_process()
    bridge_env.write_session_file(pid, "conv-w")
    transcript = bridge_env.root / "w.jsonl"
    transcript.write_text(_assistant(100_000, now_ms()))
    base = {"session_id": "conv-w", "transcript_path": str(transcript)}
    bridge_env.hook("UserPromptSubmit", {**base, "prompt": "go", "prompt_id": "p1"}, pid)
    with bridge_env.connect() as conn:
        conn.execute("UPDATE conversations SET context_window = 200000")
        near = is_near_threshold(conn, "conv-w", 600_000, 0.75)
        conn.execute("UPDATE conversations SET context_tokens = 120000")
        nearer = is_near_threshold(conn, "conv-w", 600_000, 0.75)

    with transcript.open("a") as f:
        f.write(_assistant(160_000, now_ms()))
    warned = bridge_env.hook("UserPromptSubmit", {**base, "prompt": "more", "prompt_id": "p2"}, pid)

    assert not near, "the margin shrinks with the threshold: 100k is not near 150k"
    assert nearer
    assert "Context reached 160k tokens" in _context(warned.stdout)


def test_mail_offered_to_a_dead_tab_is_held_and_given_again_after_compaction(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    config = Path(bridge_env.env["BRIDGE_CONFIG"])
    config.write_text(config.read_text() + "compact_at_tokens = 600000\n")
    pid = spawn_process()
    bridge_env.write_session_file(pid, "conv-x")
    transcript = bridge_env.root / "x.jsonl"
    transcript.write_text(_assistant(230_000, now_ms() - 60_000))
    base = {"session_id": "conv-x", "transcript_path": str(transcript)}
    bridge_env.hook("SessionStart", {**base, "source": "startup"}, pid)

    def send(text: str) -> None:
        with bridge_env.connect() as conn, transaction(conn):
            recipient = resolve_tab(conn, "conv-x", "x")
            send_message(conn, Party(SYSTEM_CONVERSATION, "bridge"), [recipient], "info", text, now_ms())

    send("before the death was seen")
    # The prompt carrying it fails: it is offered, never read.
    offered = bridge_env.hook("UserPromptSubmit", {**base, "prompt": "go", "prompt_id": "p1"}, pid)
    with transcript.open("a") as f:
        f.write(_failed("Prompt is too long", now_ms()))
    bridge_env.hook("Stop", {**base, "last_assistant_message": "Prompt is too long"}, pid)
    send("after the death was seen")
    held = bridge_env.hook("UserPromptSubmit", {**base, "prompt": "again", "prompt_id": "p2"}, pid)
    with bridge_env.connect() as conn:
        why = conn.execute("SELECT context_dead_why FROM conversations").fetchone()[0]

    with transcript.open("a") as f:
        f.write(json.dumps({"type": "system", "subtype": "compact_boundary"}) + "\n")
    restored = bridge_env.hook("SessionStart", {**base, "source": "compact"}, pid)
    with bridge_env.connect() as conn:
        after = conn.execute("SELECT context_dead_why, context_unread_from FROM conversations").fetchone()

    assert "before the death was seen" in _context(offered.stdout)
    assert why == DEAD_TOO_LONG
    assert "after the death was seen" not in _context(held.stdout)
    assert "before the death was seen" in _context(restored.stdout)
    assert "after the death was seen" in _context(restored.stdout)
    assert tuple(after) == (None, None)


def test_clear_on_a_dead_tab_carries_its_unread_mail_as_unread(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    config = Path(bridge_env.env["BRIDGE_CONFIG"])
    config.write_text(config.read_text() + "compact_at_tokens = 600000\n")
    pid = spawn_process()
    bridge_env.write_session_file(pid, "conv-y")
    transcript = bridge_env.root / "y.jsonl"
    transcript.write_text(_assistant(230_000, now_ms() - 60_000))
    base = {"session_id": "conv-y", "transcript_path": str(transcript)}
    bridge_env.hook("SessionStart", {**base, "source": "startup"}, pid)
    with bridge_env.connect() as conn, transaction(conn):
        recipient = resolve_tab(conn, "conv-y", "y")
        send_message(conn, Party(SYSTEM_CONVERSATION, "bridge"), [recipient], "info", "read me later", now_ms())
    bridge_env.hook("UserPromptSubmit", {**base, "prompt": "go", "prompt_id": "p1"}, pid)
    with transcript.open("a") as f:
        f.write(_failed("Prompt is too long", now_ms()))
    bridge_env.hook("Stop", {**base, "last_assistant_message": "Prompt is too long"}, pid)

    bridge_env.write_session_file(pid, "conv-z")
    cleared = bridge_env.hook("SessionStart", {"session_id": "conv-z", "source": "clear"}, pid)

    assert "read me later" in _context(cleared.stdout)


def _words(size: int) -> str:
    return " ".join(f"w{i}x" for i in range(size))[:size].rsplit(" ", 1)[0]


def _start_with_a_long_message(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, words: bool = True
) -> tuple[int, Path, str]:
    """A tab whose next hook gives 3,000 characters of notes and part 1 of an 8,000-character message; part 2 waits."""
    config = Path(bridge_env.env["BRIDGE_CONFIG"])
    config.write_text(config.read_text() + "compact_at_tokens = 600000\n")
    pid = spawn_process()
    bridge_env.write_session_file(pid, "conv-x")
    transcript = bridge_env.root / "x.jsonl"
    transcript.write_text(_assistant(230_000, now_ms() - 60_000))
    bridge_env.hook(
        "SessionStart", {"session_id": "conv-x", "transcript_path": str(transcript), "source": "startup"}, pid
    )
    text = _words(8_000) if words else ""
    with bridge_env.connect() as conn, transaction(conn):
        save_later_parts(conn, "conv-x", [Part("NOTES " + "n" * 3_000, "your notes")])
        if text:
            recipient = resolve_tab(conn, "conv-x", "x")
            send_message(conn, Party(USER_CONVERSATION, "user"), [recipient], "info", text, now_ms())
    return pid, transcript, text


def _compact(bridge_env: BridgeEnv, pid: int, transcript: Path) -> str:
    base = {"session_id": "conv-x", "transcript_path": str(transcript)}
    with transcript.open("a") as f:
        f.write(_failed("Prompt is too long", now_ms()))
    bridge_env.hook("Stop", {**base, "last_assistant_message": "Prompt is too long"}, pid)
    with transcript.open("a") as f:
        f.write(json.dumps({"type": "system", "subtype": "compact_boundary"}) + "\n")
    restored = bridge_env.hook("SessionStart", {**base, "source": "compact"}, pid)
    after = bridge_env.hook("UserPromptSubmit", {**base, "prompt": "continue", "prompt_id": "p9"}, pid)
    return _context(restored.stdout) + _context(after.stdout)


def test_the_tail_of_the_users_long_message_comes_again_when_the_request_carrying_it_failed(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    pid, transcript, words = _start_with_a_long_message(bridge_env, spawn_process)
    base = {"session_id": "conv-x", "transcript_path": str(transcript)}
    first = _context(bridge_env.hook("UserPromptSubmit", {**base, "prompt": "go", "prompt_id": "p1"}, pid).stdout)
    assert "user, part 1 of 2:" in first and "user, part 2 of 2:" not in first
    time.sleep(0.05)
    with transcript.open("a") as f:
        f.write(_assistant(230_000, now_ms()))
    time.sleep(0.05)
    second = _context(bridge_env.hook("PostToolUse", {**base, "tool_name": "Bash"}, pid).stdout)
    assert "user, part 2 of 2:" in second

    given = _compact(bridge_env, pid, transcript)

    tail = words.split()[-50:]
    assert "user, part 2 of 2:" in given and all(word in given for word in tail), "its request failed: it comes again"


def test_a_message_given_again_after_compaction_comes_once_in_order(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    pid, transcript, words = _start_with_a_long_message(bridge_env, spawn_process)
    base = {"session_id": "conv-x", "transcript_path": str(transcript)}
    first = _context(bridge_env.hook("UserPromptSubmit", {**base, "prompt": "go", "prompt_id": "p1"}, pid).stdout)
    assert "user, part 1 of 2:" in first

    given = _compact(bridge_env, pid, transcript)

    numbered = words.split()
    assert all(given.count(word) == 1 for word in numbered), "part 2 that waited does not come again besides"
    at = [given.find(word) for word in numbered]
    assert at == sorted(at)


def test_a_message_given_again_keeps_its_place_before_a_newer_one_still_waiting(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    """A (three parts) is older than B (two): part 1 of A arrives and is answered, the request carrying parts 2 and 3
    fails while B still waits, and only A is given again; it must still come before B."""
    pid, transcript, _ = _start_with_a_long_message(bridge_env, spawn_process, words=False)
    base = {"session_id": "conv-x", "transcript_path": str(transcript)}
    older = " ".join(f"a{i}x" for i in range(2_200))[:9_000]
    newer = " ".join(f"b{i}x" for i in range(1_200))[:5_000]
    with bridge_env.connect() as conn, transaction(conn):
        recipient = resolve_tab(conn, "conv-x", "x")
        for words in (older, newer):
            send_message(conn, Party(USER_CONVERSATION, "user"), [recipient], "info", words, now_ms())
    first = _context(bridge_env.hook("UserPromptSubmit", {**base, "prompt": "go", "prompt_id": "p1"}, pid).stdout)
    assert "user, part 1 of 3:" in first and "b0x" not in first
    time.sleep(0.05)
    with transcript.open("a") as f:
        f.write(_assistant(230_000, now_ms()))
    time.sleep(0.05)
    second = _context(bridge_env.hook("PostToolUse", {**base, "tool_name": "Bash"}, pid).stdout)
    assert "user, part 3 of 3:" in second and "b0x" not in second, "B still waits"

    given = _compact(bridge_env, pid, transcript)
    for n in range(3):
        after = {**base, "prompt": f"more {n}", "prompt_id": f"q{n}"}
        given += _context(bridge_env.hook("UserPromptSubmit", after, pid).stdout)

    assert -1 < given.find("a0x") < given.find("b0x"), "the older message first"
