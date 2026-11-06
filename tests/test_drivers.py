import json
import sqlite3
from pathlib import Path
from typing import cast

from conftest import BridgeEnv, SpawnProcess

from bridge.config import Remote
from bridge.drivers import Link, load_drivers, map_command_to_links, read_new_entries, scan_transcripts
from bridge.serve import attach_drivers, map_remote_to_session_refs

POD = Remote("devbox", "devbox", ("devbox-alt",))
USED_AT = 1_792_299_600_000


def names(command: str) -> list[str]:
    return [link.session for link in map_command_to_links(command, "c", (POD,))]


def test_links_from_ssh_commands() -> None:
    assert names("ssh -o BatchMode=yes devbox-alt 'tmux send-keys -t w-r2 -l \"go\"'") == ["w-r2"]
    assert names("until ssh devbox-alt 'tmux capture-pane -p -t =w-proj-30:0.0 | grep x'; do sleep 5; done") == [
        "w-proj-30"
    ]
    assert names("ssh devbox \"bash -lc '/tmp/resume-work.sh w-proj-32'\"") == ["w-proj-32"]
    assert names("ssh devbox 'ls ~/projects'") == []
    assert names("echo ssh devbox -t x") == []
    assert names("ssh other-host 'tmux attach -t x'") == []
    assert names("ssh devbox 'tmux attach -t -t'") == []


def test_ssh_and_tmux_forms() -> None:
    assert names("ssh dev@devbox 'tmux new-session -ds work -c ~/x'") == ["work"]
    assert names("/usr/bin/ssh -p 22 devbox tmux capture-pane -pt work2") == ["work2"]
    assert names("ssh devbox 'tmux attach -twork3'") == ["work3"]
    assert names("ssh devbox 'tmux send-keys -t \\$4 Enter'") == ["$4"]
    assert names("ssh devbox 'bash /tmp/resume-work.sh w-x'") == ["w-x"]
    assert names("ssh devbox <<'EOF'\ncd /tmp\ntmux send-keys -t heredoc-s Enter\nEOF\necho done") == ["heredoc-s"]


def test_not_driving() -> None:
    assert names('echo ssh devbox "tmux attach -t x"') == []
    assert names("ssh -J devbox other 'tmux attach -t x'") == []
    assert names("ssh devbox 'tmux send-keys -t real \"tmux attach -t payload\" Enter'") == ["real"]
    assert names("ssh devbox 'tmux send-keys -t real -l -- -tphantom'") == ["real"]
    assert names("ssh devbox true; cat <<EOF\ntmux attach -t local-only\nEOF") == []
    assert names("ssh devbox cat <<EOF\ntmux attach -t data-only\nEOF") == []


def test_more_remote_forms() -> None:
    assert names("ssh devbox bash <<'EOF'\ntmux attach -t via-bash\nEOF") == ["via-bash"]
    assert names("ssh devbox 'sudo -u dev tmux attach -t sudoed'") == ["sudoed"]
    assert names("ssh devbox '2>/dev/null tmux attach -t redirected'") == ["redirected"]
    assert names("ssh devbox '/tmp/resume-work.sh work.name'") == ["work.name"]
    assert names("ssh devbox 'tmux -- attach -t real'") == ["real"]
    assert names("ssh devbox bash -s <<'EOF'\ntmux attach -t via-s\nEOF") == ["via-s"]


def test_offsets_survive_rewrites_and_line_boundaries(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    first = _bash("ssh devbox 'tmux attach -t a'", "2026-10-18T05:00:00Z")
    transcript.write_text(first + _bash("ssh devbox 'tmux attach -t b'", "2026-10-18T05:00:01Z"))
    # A first-scan command window that starts exactly on a line boundary keeps that line.
    entries = read_new_entries(transcript, -1, USED_AT, first_scan_bytes=transcript.stat().st_size - len(first))
    assert [c for c, _ in entries.commands] == ["ssh devbox 'tmux attach -t b'"]
    assert entries.offset == transcript.stat().st_size
    transcript.write_text(_bash("ssh devbox 'tmux attach -t c'", "2026-10-18T05:00:02Z"))
    rewritten = read_new_entries(transcript, entries.offset, USED_AT)
    assert [c for c, _ in rewritten.commands] == ["ssh devbox 'tmux attach -t c'"]


def test_untimed_and_future_entries(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        _bash("ssh devbox 'tmux attach -t a'") + _bash("ssh devbox 'tmux attach -t b'", "2099-01-01T00:00:00Z")
    )
    assert read_new_entries(transcript, 0, 5_000).commands == [("ssh devbox 'tmux attach -t b'", 5_000)]


def test_pr_links_from_early_turns_stay_on_the_session(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    """An open PR stays with the session that created it, however long ago; merged and foreign links do not."""
    pid = spawn_process()
    transcript = bridge_env.root / "t.jsonl"
    links = [
        {"type": "pr-link", "prNumber": n, "prRepository": "acme/infra", "timestamp": "2026-10-15T14:36:16Z",
         "sessionId": session}
        for n, session in ((36, "conv-p"), (35, "conv-p"), (34, "conv-other"))
    ]  # fmt: skip
    filler = _bash("echo filler " + "x" * 6_000_000, "2026-10-16T00:00:00Z")
    transcript.write_text("".join(json.dumps(link) + "\n" for link in links) + filler)
    for n in range(2):
        bridge_env.hook(
            "UserPromptSubmit",
            {"session_id": "conv-p", "prompt": f"turn {n}", "prompt_id": f"p{n}", "transcript_path": str(transcript)},
            pid,
        )
    with bridge_env.connect() as conn:
        scan_transcripts(conn, (), USED_AT)
        # Once merged, an old PR link is history, not something the session is working on.
        conn.execute(
            "INSERT INTO enrichment (ref, data, fetched_at, ok_at) VALUES (?, ?, 0, 0)",
            ("pr:acme/infra#36", json.dumps({"state": "MERGED"})),
        )
        conn.commit()
    sessions = json.loads(bridge_env.cli("sessions", "--json").stdout)
    assert [r for r in sessions[0]["refs"] if r.startswith("pr:")] == ["pr:acme/infra#35"]


def test_signals_go_to_the_latest_driver() -> None:
    local: list[dict[str, object]] = [
        {"conversation_id": "old", "handle": "old", "surface": "A"},
        {"conversation_id": "new", "handle": "new", "surface": "B"},
    ]
    remote: list[dict[str, object]] = [
        {"handle": "devbox/w", "tab_name": "w", "status": "idle", "agent": "claude", "branch": None, "refs": [],
         "remote": {"host": "devbox", "tmux_id": "$1"}},
    ]  # fmt: skip
    links = [Link("old", "devbox", "w", 1), Link("new", "devbox", "$1", 2), Link("new", "devbox", "w", 2)]
    attach_drivers(local, remote, links)
    assert remote[0]["driven_by"] == ["new", "old"]
    assert len(cast(list[object], local[1]["drives"])) == 1
    assert map_remote_to_session_refs(remote[0], local).handle == "new"


def test_scan_is_incremental_and_links_show_on_live_remote_sessions(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    pid = spawn_process()
    transcript = bridge_env.root / "t.jsonl"
    transcript.write_text(_bash("ssh devbox 'tmux send-keys -t s11 -l hi'", "2026-10-18T05:00:00.000Z"))
    bridge_env.hook(
        "UserPromptSubmit", {"session_id": "conv-d", "prompt": "x", "transcript_path": str(transcript)}, pid
    )
    with bridge_env.connect() as conn:
        scan_transcripts(conn, (POD,), USED_AT + 3_600_000)
        with transcript.open("a") as f:
            f.write(_bash("ssh devbox-alt 'tmux capture-pane -p -t s12'", "2026-10-18T05:30:00.000Z"))
            f.write(_bash("ssh devbox 'tmux capture-pane -p -t s11'", "2026-10-18T04:00:00.000Z"))
        scan_transcripts(conn, (POD,), USED_AT + 3_600_000)
        links = {(link.session, link.conversation_id, link.used_at) for link in load_drivers(conn, 0)}
        offset = conn.execute("SELECT offset FROM transcript_scans").fetchone()[0]
    # Times come from the transcript entry; an older command never moves a link back.
    assert links == {("s11", "conv-d", USED_AT), ("s12", "conv-d", USED_AT + 1_800_000)}
    with bridge_env.connect() as conn:
        assert [link.session for link in load_drivers(conn, USED_AT + 1)] == ["s12"]
    assert offset == transcript.stat().st_size

    local: list[dict[str, object]] = [{"conversation_id": "conv-d", "handle": "hq", "surface": "S"}]
    remote: list[dict[str, object]] = [
        {"handle": "devbox/s11", "tab_name": "s11", "status": "active", "agent": "codex",
         "remote": {"host": "devbox", "tmux_id": "$4"}},
    ]  # fmt: skip
    attach_drivers(local, remote, list(load_drivers(_reopen(bridge_env), 0)))
    assert local[0]["drives"] == [
        {
            "handle": "devbox/s11",
            "host": "devbox",
            "tmux_id": "$4",
            "status": "active",
            "agent": "codex",
            "used_at": USED_AT,
        }
    ]
    assert remote[0]["driven_by"] == ["hq"]


def _bash(command: str, timestamp: str | None = None) -> str:
    entry: dict[str, object] = {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": command}}]},
    }
    if timestamp:
        entry["timestamp"] = timestamp
    return json.dumps(entry) + "\n"


def _reopen(bridge_env: BridgeEnv) -> sqlite3.Connection:
    return bridge_env.connect()
