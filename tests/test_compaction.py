import json
import time
from datetime import UTC, datetime
from pathlib import Path

from conftest import BridgeEnv, SpawnProcess, wait_for

from bridge.compaction import (
    CONTINUE_PROMPT,
    ContextReading,
    estimate_pending_tokens,
    map_screen_to_draft,
    map_screen_to_idle,
    read_context_tokens,
)


def _assistant(tokens: int, sidechain: bool = False, at: str = "") -> str:
    usage = {"input_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": tokens - 20}
    usage["output_tokens"] = 10
    entry = {"type": "assistant", "isSidechain": sidechain, "message": {"role": "assistant", "usage": usage}}
    entry["timestamp"] = at or datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return json.dumps(entry) + "\n"


def _context(result_stdout: str) -> str:
    if not result_stdout.strip():
        return ""
    return str(json.loads(result_stdout)["hookSpecificOutput"]["additionalContext"])


def test_context_size_comes_from_the_newest_main_thread_usage(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    filler = json.dumps({"type": "user", "message": {"content": "x" * 400_000}}) + "\n"
    at = "2026-11-02T14:16:10.000Z"
    transcript.write_text(_assistant(1_000) + filler + _assistant(5_000, at=at) + _assistant(9_000, sidechain=True))
    assert read_context_tokens(transcript) == ContextReading(5_000, 1793628970000)
    assert read_context_tokens(tmp_path / "missing.jsonl") is None


def test_warning_then_notes_come_back_after_compaction(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    config = Path(bridge_env.env["BRIDGE_CONFIG"])
    config.write_text(config.read_text() + "compact_at_tokens = 500000\n")
    pid = spawn_process()
    bridge_env.write_session_file(pid, "conv-k")
    transcript = bridge_env.root / "t.jsonl"
    transcript.write_text(_assistant(450_000))
    base = {"session_id": "conv-k", "transcript_path": str(transcript)}

    below = bridge_env.hook("UserPromptSubmit", {**base, "prompt": "go", "prompt_id": "p1"}, pid)
    assert "Context reached" not in _context(below.stdout)

    with transcript.open("a") as f:
        f.write(_assistant(510_000))
    # Stop cannot carry context to the model, so the warning waits for the next event that can.
    bridge_env.hook("Stop", {**base, "last_assistant_message": "done"}, pid)
    warned = bridge_env.hook("UserPromptSubmit", {**base, "prompt": "more", "prompt_id": "p2"}, pid)
    assert "Context reached 510k tokens" in _context(warned.stdout)
    again = bridge_env.hook("PostToolUse", {**base, "tool_name": "Bash"}, pid)
    assert "Context reached" not in _context(again.stdout), "one warning per compaction cycle"

    kept = bridge_env.cli("keep", "decided: ship v2 behind a flag; PR acme/web#7", pid=pid)
    assert kept.returncode == 0, kept.stderr

    with transcript.open("a") as f:
        f.write(json.dumps({"type": "system", "subtype": "compact_boundary"}) + "\n")
    restored = bridge_env.hook("SessionStart", {**base, "source": "compact"}, pid)
    assert "decided: ship v2 behind a flag; PR acme/web#7" in _context(restored.stdout)
    quiet = bridge_env.hook("UserPromptSubmit", {**base, "prompt": "after", "prompt_id": "p4"}, pid)
    assert "Context reached" not in _context(quiet.stdout), "usage from before the boundary is not the new size"

    with transcript.open("a") as f:
        f.write(_assistant(520_000))
    rewarned = bridge_env.hook("UserPromptSubmit", {**base, "prompt": "next", "prompt_id": "p5"}, pid)
    assert "Context reached 520k tokens" in _context(rewarned.stdout), "a new cycle warns again"


def test_a_new_list_shows_the_lines_of_the_old_one_it_drops(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pid = spawn_process()
    bridge_env.write_session_file(pid, "conv-r")
    bridge_env.hook("SessionStart", {"session_id": "conv-r", "source": "startup"}, pid)

    first = bridge_env.cli("keep", "PROJ-34 is mine since 11-03\nPR acme/web#7 waits for review", pid=pid)
    second = bridge_env.cli("keep", "PR acme/web#7 waits for review\nnext: deploy asked", pid=pid)
    third = bridge_env.cli("keep", "PR acme/web#7 waits for review\nnext: deploy asked\nPROJ-34 is mine", pid=pid)

    assert "This replaced" not in first.stdout
    assert "This replaced the list you saved" in second.stdout
    assert second.stdout.rstrip().endswith("PROJ-34 is mine since 11-03")
    assert "PR acme/web#7" not in second.stdout.split("This replaced", 1)[1]
    assert "This replaced" not in third.stdout, "a list that keeps every line of the old one replaces it quietly"


def test_a_large_tool_result_counts_before_the_api_does(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    config = Path(bridge_env.env["BRIDGE_CONFIG"])
    config.write_text(config.read_text() + "compact_at_tokens = 500000\n")
    pid = spawn_process()
    bridge_env.write_session_file(pid, "conv-big")
    transcript = bridge_env.root / "big.jsonl"
    transcript.write_text(_assistant(450_000))
    base = {"session_id": "conv-big", "transcript_path": str(transcript)}
    bridge_env.hook("UserPromptSubmit", {**base, "prompt": "go", "prompt_id": "p1"}, pid)
    result = bridge_env.hook("PostToolUse", {**base, "tool_name": "Read", "tool_response": "x" * 300_000}, pid)
    assert "Context reached 525k tokens" in _context(result.stdout)


def test_the_first_hooks_after_compaction_do_not_warn_from_the_old_size(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    config = Path(bridge_env.env["BRIDGE_CONFIG"])
    config.write_text(config.read_text() + "compact_at_tokens = 600000\n")
    pid = spawn_process()
    bridge_env.write_session_file(pid, "conv-race")
    transcript = bridge_env.root / "race.jsonl"
    boundary = json.dumps({"type": "system", "subtype": "compact_boundary"}) + "\n"
    transcript.write_text(boundary + _assistant(601_000))
    base = {"session_id": "conv-race", "transcript_path": str(transcript)}

    # Compacted by hand before bridge warned, and Claude Code has not written the boundary yet.
    bridge_env.hook("SessionStart", {**base, "source": "compact"}, pid)
    stale = bridge_env.hook("UserPromptSubmit", {**base, "prompt": "<task-notification>x", "prompt_id": "p1"}, pid)
    assert "Context reached" not in _context(stale.stdout)

    with transcript.open("a") as f:
        f.write(boundary + _assistant(84_000))
    bridge_env.hook("PostToolUse", {**base, "tool_name": "Bash"}, pid)
    with transcript.open("a") as f:
        f.write(_assistant(605_000))
    fresh = bridge_env.hook("PostToolUse", {**base, "tool_name": "Bash"}, pid)
    assert "Context reached 605k tokens" in _context(fresh.stdout)


def test_a_subagent_never_takes_the_main_conversations_warning(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    config = Path(bridge_env.env["BRIDGE_CONFIG"])
    config.write_text(config.read_text() + "compact_at_tokens = 600000\n")
    pid = spawn_process()
    bridge_env.write_session_file(pid, "conv-sub")
    transcript = bridge_env.root / "sub.jsonl"
    transcript.write_text(_assistant(601_000))
    base = {"session_id": "conv-sub", "transcript_path": str(transcript)}

    sub = bridge_env.hook("PostToolUse", {**base, "tool_name": "Bash", "agent_id": "a0000001"}, pid)
    assert "Context reached" not in _context(sub.stdout)
    main = bridge_env.hook("PostToolUse", {**base, "tool_name": "Bash"}, pid)
    assert "Context reached 601k tokens" in _context(main.stdout)


def test_an_image_counts_as_an_image_not_as_its_base64(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    config = Path(bridge_env.env["BRIDGE_CONFIG"])
    config.write_text(config.read_text() + "compact_at_tokens = 600000\n")
    pid = spawn_process()
    bridge_env.write_session_file(pid, "conv-img")
    transcript = bridge_env.root / "img.jsonl"
    transcript.write_text(_assistant(475_000))
    base = {"session_id": "conv-img", "transcript_path": str(transcript)}
    screenshot = {"type": "image", "file": {"base64": "iVBOR" * 105_000, "type": "image/png"}}
    result = bridge_env.hook("PostToolUse", {**base, "tool_name": "Read", "tool_response": screenshot}, pid)
    assert "Context reached" not in _context(result.stdout)
    text = {"media_type": "text/plain", "data": "x" * 520_000}
    result = bridge_env.hook("PostToolUse", {**base, "tool_name": "mcp__x", "tool_response": text}, pid)
    assert "Context reached 605k tokens" in _context(result.stdout), "only image data is discounted"
    listed = estimate_pending_tokens("PostToolUse", {"tool_response": ["a"] * 200_000})
    assert listed >= 200_000, "structure counts as it is serialized"


IDLE = "──────\n\u276f\u00a0\n──────\n  Opus | 61% ctx\n  -- INSERT -- ⏵⏵ auto mode on\n"


def test_screen_is_idle_only_with_an_empty_prompt_and_nothing_in_the_way() -> None:
    assert map_screen_to_idle(IDLE)
    assert not map_screen_to_idle(IDLE.replace("\u276f\u00a0", "\u276f half-typed message"))
    assert not map_screen_to_idle("✻ Churning… (esc to interrupt)\n" + IDLE)
    assert not map_screen_to_idle(IDLE.replace("-- INSERT --", "-- NORMAL --"))
    assert not map_screen_to_idle("sam@mac ~ \u276f\u00a0\n"), "a shell prompt is not Claude Code's input box"
    assert not map_screen_to_idle(IDLE.replace("\u276f\u00a0\n", "\u276f\u00a0\n  second line of a draft\n"))
    assert not map_screen_to_idle(IDLE + "\u276f half-typed below an old prompt\n")
    assert not map_screen_to_idle("\u276f No, exit\n  Yes, I trust this folder\n Enter to confirm · Esc to cancel\n")


def test_in_agterm_bridge_types_compact_then_continue(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    config = Path(bridge_env.env["BRIDGE_CONFIG"])
    config.write_text(config.read_text() + "compact_at_tokens = 600000\n")
    agterm = {"AGTERM_SESSION_ID": "TAB-9", "AGTERM_PANE_ID": "PANE-9"}
    pid = spawn_process(env=agterm)
    bridge_env.write_session_file(pid, "conv-d")
    transcript = bridge_env.root / "d.jsonl"
    transcript.write_text(_assistant(600_500))
    base = {"session_id": "conv-d", "transcript_path": str(transcript)}

    asked = bridge_env.hook("PostToolUse", {**base, "tool_name": "Bash"}, pid, extra_env=agterm)
    assert "end your turn at once" in _context(asked.stdout)
    bridge_env.hook("Stop", {**base, "last_assistant_message": "saved"}, pid, extra_env=agterm)
    # Its Enter goes alone; read_text turns "\r\n" into "\n".
    _wait_for_log(bridge_env, "PANE-9 /compact\nsession type --target TAB-9 --pane-id PANE-9 \n")

    bridge_env.hook("SessionStart", {**base, "source": "compact"}, pid, extra_env=agterm)
    _wait_for_log(bridge_env, "you saved no notes with `bridge keep` before it. Continue where you left off")
    assert "are above" not in bridge_env.stub_log.read_text()
    deadline = time.monotonic() + 5
    with bridge_env.connect() as conn:
        while conn.execute("SELECT compact_phase FROM conversations").fetchone()[0] and time.monotonic() < deadline:
            time.sleep(0.1)
        assert conn.execute("SELECT compact_phase FROM conversations").fetchone()[0] is None
    # A compaction the user starts by hand is not followed by a typed "continue".
    bridge_env.hook("SessionStart", {**base, "source": "compact"}, pid, extra_env=agterm)
    # Past the two idle looks a wrongly started driver would need before typing.
    time.sleep(6)
    assert bridge_env.stub_log.read_text().count("Continue where you left off") == 1


def test_a_dropped_enter_is_pressed_again(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    config = Path(bridge_env.env["BRIDGE_CONFIG"])
    config.write_text(config.read_text() + "compact_at_tokens = 600000\n")
    agterm = {"AGTERM_SESSION_ID": "TAB-8", "AGTERM_PANE_ID": "PANE-8", "STUB_DROP_ENTER": "1"}
    pid = spawn_process(env=agterm)
    bridge_env.write_session_file(pid, "conv-e")
    transcript = bridge_env.root / "e.jsonl"
    transcript.write_text(_assistant(600_500))
    base = {"session_id": "conv-e", "transcript_path": str(transcript)}
    bridge_env.hook("PostToolUse", {**base, "tool_name": "Bash"}, pid, extra_env=agterm)
    assert bridge_env.cli("keep", "PR acme/web#7 waits for review", pid=pid).returncode == 0
    bridge_env.hook("Stop", {**base, "last_assistant_message": "saved"}, pid, extra_env=agterm)
    _wait_for_log(bridge_env, "PANE-8 /compact\nsession type --target TAB-8 --pane-id PANE-8 \n")

    bridge_env.hook("SessionStart", {**base, "source": "compact"}, pid, extra_env=agterm)
    _wait_for_log(bridge_env, "the notes you saved with `bridge keep` are above. Continue where you left off")
    assert wait_for(lambda: not Path(bridge_env.env["STUB_DRAFT"] + "-PANE-8").exists())
    log = bridge_env.stub_log.read_text()
    assert log.count("Continue where you left off") == 1
    # The lone Enters after it, the first one dropped: read_text turns "\r\n" into "\n".
    assert log.split("Continue where you left off")[1].count("--pane-id PANE-8 \n") == 2


def test_draft_is_the_text_inside_the_input_box() -> None:
    rule = "\u2500" * 20
    assert map_screen_to_draft(f"{rule}\n\u276f\u00a0\n{rule}\n  -- INSERT --") == ""
    assert map_screen_to_draft(f"output\n{rule}\n\u276f {CONTINUE_PROMPT}\n{rule}") == CONTINUE_PROMPT
    assert map_screen_to_draft(f"{rule}\n\u276f first half\n  second half\n{rule}") == "first half second half"
    assert map_screen_to_draft("\u276f a shell prompt") is None


def _wait_for_log(bridge_env: BridgeEnv, needle: str) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if bridge_env.stub_log.exists() and needle in bridge_env.stub_log.read_text():
            return
        time.sleep(0.2)
    raise AssertionError(
        f"{needle!r} never typed; log: {bridge_env.stub_log.read_text() if bridge_env.stub_log.exists() else ''}"
    )


def test_a_driven_compaction_whose_start_cannot_write_is_still_continued(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    config = Path(bridge_env.env["BRIDGE_CONFIG"])
    config.write_text(config.read_text() + "compact_at_tokens = 600000\n")
    agterm = {"AGTERM_SESSION_ID": "TAB-7", "AGTERM_PANE_ID": "PANE-7"}
    pid = spawn_process(env=agterm)
    bridge_env.write_session_file(pid, "conv-f")
    transcript = bridge_env.root / "f.jsonl"
    transcript.write_text(_assistant(600_500))
    base = {"session_id": "conv-f", "transcript_path": str(transcript)}
    bridge_env.hook("PostToolUse", {**base, "tool_name": "Bash"}, pid, extra_env=agterm)
    assert bridge_env.cli("keep", "PR acme/web#7 waits for review", pid=pid).returncode == 0
    bridge_env.hook("Stop", {**base, "last_assistant_message": "saved"}, pid, extra_env=agterm)
    _wait_for_log(bridge_env, "PANE-7 /compact\nsession type --target TAB-7 --pane-id PANE-7 \n")

    blocker = bridge_env.connect()
    blocker.execute("BEGIN IMMEDIATE")
    try:
        restored = bridge_env.hook("SessionStart", {**base, "source": "compact"}, pid, extra_env=agterm)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert "PR acme/web#7 waits for review" in _context(restored.stdout)
    _wait_for_log(bridge_env, "the notes you saved with `bridge keep` are above. Continue where you left off")
    with bridge_env.connect() as conn:
        assert wait_for(lambda: conn.execute("SELECT compact_phase FROM conversations").fetchone()[0] is None)
    with transcript.open("a") as f:
        f.write(json.dumps({"type": "system", "subtype": "compact_boundary"}) + "\n")
    bridge_env.hook("UserPromptSubmit", {**base, "prompt": CONTINUE_PROMPT, "prompt_id": "c1"}, pid, extra_env=agterm)
    with bridge_env.connect() as conn:
        warned_at, compacted_at = conn.execute("SELECT compact_warned_at, compacted_at FROM conversations").fetchone()
    assert (warned_at, compacted_at is not None) == (None, True), "the next cycle can warn again"
