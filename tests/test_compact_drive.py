import argparse
import json
import sqlite3
import time
from unittest import mock

import pytest
from conftest import BridgeEnv, SpawnProcess
from test_leads import Team
from test_typing import NBSP, PROMPT, RULE, FakeClaudeBox
from test_watcher_context import _screen  # pyright: ignore[reportPrivateUsage]

from bridge.board import Recipient
from bridge.cli import cmd_compact_drive
from bridge.compaction import (
    COMPACT_AT_WINDOW_SHARE,
    PHASE_COMPACTING,
    PHASE_CONTINUE,
    RETRY_AFTER_MS,
    ContextReading,
    read_phase,
    record_context,
)
from bridge.config import Config, state_dir
from bridge.context_watch import list_context_tabs, record_context_screens
from bridge.db import transaction
from bridge.tabinput import CLEAR_KEYS

DRAFT = "Give me a short summary of the week in HTML"


def _drive(
    bridge_env: BridgeEnv,
    fake: FakeClaudeBox,
    phase: str = "compact",
    config: Config | None = None,
    tree: str | None = "",
    by_screen: bool = False,
    vim: bool = False,
) -> tuple[int, list[tuple[int, Recipient]]]:
    """Drives conv-a (lead alex in TAB-A) with old-b as the assistant, its tab unselected."""
    spawned: list[tuple[int, Recipient]] = []
    tree_text = bridge_env.stub_tree.read_text() if tree == "" else tree
    with bridge_env.connect() as conn:
        instance = conn.execute("SELECT instance_id FROM instances WHERE conversation_id = 'conv-a'").fetchone()[0]
        args = argparse.Namespace(
            phase=phase, instance_id=instance, conversation_id="conv-a", keep="shown", reopened=False,
            generic_keep=False, claim=False, by_screen=by_screen,
        )  # fmt: skip
        with (
            mock.patch("bridge.tabinput.run_agterm", fake.run),
            mock.patch("bridge.watcher.run_agterm", lambda *_: tree_text),
            mock.patch("bridge.cli.is_tab_selected", lambda *_: False),
            mock.patch("bridge.cli.spawn_typed_delivery", lambda m, rs: spawned.extend((m, r) for r in rs)),
            mock.patch("bridge.cli.COMPACT_WAIT_S", 6),
            mock.patch("bridge.tabinput.is_claude_vim", lambda: vim),
        ):
            code = cmd_compact_drive(args, conn, config or Config(assistant_handle="old-b"))
    return code, spawned


def _start_cycle(bridge_env: BridgeEnv, phase: str = PHASE_COMPACTING) -> None:
    with bridge_env.connect() as conn, transaction(conn):
        conn.execute(
            "UPDATE conversations SET compact_phase = ?, compact_warned_at = 650000, compact_requested_at = 1"
            " WHERE conversation_id = 'conv-a'",
            (phase,),
        )


def _log(name: str) -> str:
    path = state_dir() / name
    return path.read_text() if path.exists() else ""


def _asked_again(conn: sqlite3.Connection, at_ms: int = 1) -> str | None:
    """What the next hook past the threshold would give the session."""
    reading = ContextReading(700_000, int(time.time() * 1000))
    return record_context(
        conn, "conv-a", reading, 0, 600_000, COMPACT_AT_WINDOW_SHARE, can_deliver=True, driven=True, at_ms=at_ms
    )


@pytest.mark.parametrize("vim", [False, True])
def test_the_owners_draft_holds_the_drive_tells_the_assistant_once_and_logs_no_error(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, vim: bool
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    _start_cycle(bridge_env)
    fake = FakeClaudeBox({"TAB-A": DRAFT})

    code, spawned = _drive(bridge_env, fake, vim=vim)
    with bridge_env.connect() as conn, transaction(conn):
        phase = read_phase(conn, "conv-a")
        body = conn.execute("SELECT body FROM messages ORDER BY id DESC LIMIT 1").fetchone()[0]
        items = conn.execute("SELECT count(*) FROM user_items").fetchone()[0]
        again = _asked_again(conn)

    assert code == 0
    assert fake.boxes["TAB-A"] == DRAFT, "the probe key was taken back"
    assert fake.typed == [".", "\x7f"]
    assert phase is None
    assert [r.handle for _, r in spawned] == ["old-b"]
    assert 'could not type /compact in tab "Old A" (TAB-A) within 0 min: its input box holds text' in body
    assert items == 0
    assert again is None, "the session is not asked to save its notes again within the retry pause"
    assert "compact-drive" not in _log("errors.log")
    assert _log("slow.log").count("compact not typed for conv-a") == 1


def test_after_the_retry_pause_the_session_is_asked_for_its_notes_again(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    _start_cycle(bridge_env)
    _drive(bridge_env, FakeClaudeBox({"TAB-A": DRAFT}))
    ended_at = int(time.time() * 1000)
    with bridge_env.connect() as conn, transaction(conn):
        early = _asked_again(conn, ended_at + RETRY_AFTER_MS - 60_000)
        late = _asked_again(conn, ended_at + RETRY_AFTER_MS + 60_000)
        repeated = _asked_again(conn, ended_at + RETRY_AFTER_MS + 120_000)

    assert early is None
    assert late is not None and "save what must survive" in late
    assert repeated is None, "asked once per retry"


def test_a_retried_drive_held_by_the_same_draft_types_only_the_probe_and_tells_no_one_again(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    fake = FakeClaudeBox({"TAB-A": DRAFT})

    _start_cycle(bridge_env)
    _, first = _drive(bridge_env, fake)
    _start_cycle(bridge_env)
    _, second = _drive(bridge_env, fake)

    assert fake.boxes["TAB-A"] == DRAFT
    assert set(fake.typed) <= {".", "\x7f"}, "nothing but the probe key and its Backspace"
    assert [r.handle for _, r in first] == ["old-b"]
    assert second == []


def test_a_held_continue_leaves_the_next_held_compact_its_notice(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    fake = FakeClaudeBox({"TAB-A": DRAFT})

    _start_cycle(bridge_env, PHASE_CONTINUE)
    _, after_compaction = _drive(bridge_env, fake, phase="continue")
    _start_cycle(bridge_env)
    _, next_cycle = _drive(bridge_env, fake)

    assert [r.handle for _, r in after_compaction] == ["old-b"]
    assert [r.handle for _, r in next_cycle] == ["old-b"]


def test_a_safety_net_drive_types_over_the_grey_suggestion_of_a_tab_whose_refused_prompt_left_it_active(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    with bridge_env.connect() as conn, transaction(conn):
        conn.execute("UPDATE instances SET status = 'active' WHERE conversation_id = 'conv-a'")
    hook_drive = FakeClaudeBox({"TAB-A": "continue"}, suggestions={"TAB-A"})
    net_drive = FakeClaudeBox({"TAB-A": "continue"}, suggestions={"TAB-A"})

    _start_cycle(bridge_env)
    _drive(bridge_env, hook_drive)
    _start_cycle(bridge_env)
    code, _ = _drive(bridge_env, net_drive, by_screen=True)

    assert hook_drive.typed == [], "a drive by status waits for the hook that never comes"
    assert code == 0
    assert net_drive.submitted == ["/compact"]


@pytest.mark.parametrize("vim", [False, True])
def test_a_stray_key_left_in_the_box_is_cleared_and_compact_typed_with_no_notice(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, vim: bool
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    _start_cycle(bridge_env)
    fake = FakeClaudeBox({"TAB-A": "."})

    code, spawned = _drive(bridge_env, fake, vim=vim)

    assert code == 0
    assert fake.typed == [".", "\x7f", CLEAR_KEYS, "/compact", "\r"]
    assert fake.submitted == ["/compact"]
    assert spawned == []


def test_text_bridge_cannot_judge_tells_the_assistant_once_at_the_start_and_not_again_at_the_end(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    _start_cycle(bridge_env)
    fake = FakeClaudeBox({"TAB-A": "42"})

    code, spawned = _drive(bridge_env, fake)
    with bridge_env.connect() as conn:
        bodies = [r[0] for r in conn.execute("SELECT body FROM messages ORDER BY id")]
        phase = read_phase(conn, "conv-a")

    assert code == 0
    assert fake.boxes["TAB-A"] == "42"
    assert "/compact" not in fake.typed
    assert [r.handle for _, r in spawned] == ["old-b"]
    assert len(bodies) == 1
    assert 'bridge is waiting to type /compact in tab "Old A" (TAB-A): its input box holds "42"' in bodies[0]
    assert phase is None


def test_a_probe_key_bridge_cannot_verify_is_one_error_and_one_notice_per_cycle(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    class OwnerTypesToo(FakeClaudeBox):
        def run(self, _: Config, args: list[str]) -> str | None:
            result = super().run(_, args)
            if args[-1] == ".":
                self.boxes["TAB-A"] += "x"
            return result

    team = Team(bridge_env, spawn_process)
    team.enroll()
    _start_cycle(bridge_env)
    fake = OwnerTypesToo({"TAB-A": DRAFT})

    code, spawned = _drive(bridge_env, fake)
    with bridge_env.connect() as conn, transaction(conn):
        again = _asked_again(conn)
        body = conn.execute("SELECT body FROM messages ORDER BY id DESC LIMIT 1").fetchone()[0]

    assert code == 1
    assert fake.typed == ["."]
    assert [r.handle for _, r in spawned] == ["old-b"]
    assert "look for a stray character in the box" in body
    assert 'could not type /compact in tab "Old A" (TAB-A): a key' in body, "no wait it never had"
    assert again is None
    assert _log("errors.log").count("compact not typed for conv-a: failed") == 1


@pytest.mark.parametrize("vim", [False, True])
def test_a_tab_busy_for_the_whole_wait_is_no_error(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess, vim: bool
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    _start_cycle(bridge_env)
    fake = FakeClaudeBox({"TAB-A": "1. Yes\n2. No\nEnter to confirm"})

    code, _ = _drive(bridge_env, fake, vim=vim)

    assert code == 0
    assert fake.typed == []
    assert "compact-drive" not in _log("errors.log")
    assert "compact not typed for conv-a in 0 min: timeout" in _log("slow.log")


class GlyphOnStatus(FakeClaudeBox):
    """A frame torn so that the box's glyph and its text are drawn over the status line, as a lead tab showed."""

    STATUS = "Opus 5.5 | 56% ctx | 37% wk | ~/acme-co"

    def run(self, _: Config, args: list[str]) -> str | None:
        if args[:2] != ["session", "text"]:
            return super().run(_, args)
        box = self.boxes.get(args[args.index("--target") + 1], "")
        drawn = box + self.STATUS[len(box) :]
        return f"{PROMPT}{NBSP}\n{RULE}\n{PROMPT}{NBSP}{drawn}\n  -- INSERT -- \u23f5\u23f5 bypass permissions on\n"


def test_a_torn_frame_that_shows_the_status_line_as_the_box_gets_no_probe_key(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    _start_cycle(bridge_env)
    fake = GlyphOnStatus()

    code, spawned = _drive(bridge_env, fake, vim=True)
    with bridge_env.connect() as conn:
        body = conn.execute("SELECT body FROM messages ORDER BY id DESC LIMIT 1").fetchone()[0]

    assert fake.typed == [], "a key would land in a box the screen does not show, beyond any check"
    assert code == 0
    assert [r.handle for _, r in spawned] == ["old-b"]
    assert "its screen showed no readable input box" in body
    assert "compact-drive" not in _log("errors.log")


def test_with_no_one_to_tell_the_owner_hears_on_the_page(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    _start_cycle(bridge_env)

    _, spawned = _drive(bridge_env, FakeClaudeBox({"TAB-A": DRAFT}), config=Config(assistant_handle="nobody"))
    with bridge_env.connect() as conn:
        texts = [r[0] for r in conn.execute("SELECT text FROM user_items")]

    assert spawned == []
    assert texts == ["alex: bridge could not type /compact (draft); do it by hand once the box is clear"]


def test_mail_that_wakes_the_tab_after_compaction_ends_the_continue_quietly(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    _start_cycle(bridge_env, PHASE_CONTINUE)

    team.prompt(team.a, "conv-a", "<task-notification>\n<summary>bridge mail</summary>\n</task-notification>")
    code, spawned = _drive(bridge_env, FakeClaudeBox(), phase="continue")
    with bridge_env.connect() as conn:
        phase = read_phase(conn, "conv-a")

    assert phase is None
    assert code == 0
    assert spawned == []
    assert "compact-drive" not in _log("errors.log") + _log("slow.log")


def test_claude_codes_own_compact_output_does_not_end_the_continue(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    _start_cycle(bridge_env, PHASE_CONTINUE)

    team.prompt(team.a, "conv-a", "<local-command-stdout>Compacted</local-command-stdout>")
    with bridge_env.connect() as conn:
        phase = read_phase(conn, "conv-a")

    assert phase == PHASE_CONTINUE


def test_serve_does_not_drive_the_tab_again_right_after_a_drive_waited_in_vain(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    _start_cycle(bridge_env)
    _drive(bridge_env, FakeClaudeBox({"TAB-A": DRAFT}))
    tree = json.loads(bridge_env.stub_tree.read_text())["result"]["tree"]
    screen = _screen("  Opus 5.5 | 95% ctx | ~/projects/bridge")
    at = int(time.time() * 1000) + 60_000
    with bridge_env.connect() as conn, transaction(conn):
        tabs = list_context_tabs(conn, Config(), tree)
        actions = record_context_screens(conn, Config(compact_at_tokens=600_000), tabs, {"conv-a": (screen, at)}, at)

    assert actions.drives == []


def test_the_assistant_hears_it_when_agterms_tree_cannot_be_read(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    _start_cycle(bridge_env)

    _, spawned = _drive(bridge_env, FakeClaudeBox({"TAB-A": DRAFT}), tree=None)
    with bridge_env.connect() as conn:
        items = conn.execute("SELECT count(*) FROM user_items").fetchone()[0]

    assert [r.handle for _, r in spawned] == ["old-b"]
    assert items == 0
