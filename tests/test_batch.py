import re
import sqlite3
from collections.abc import Generator
from pathlib import Path
from unittest import mock

import pytest

from bridge.board import USER_CONVERSATION, Party, Recipient, flag_overdue, send_message, take_delivery
from bridge.cli import build_parser
from bridge.cli_board import print_wake_lines, spawn_when_ripe
from bridge.config import Config
from bridge.db import connect
from bridge.feed import BLOCKED_TAB, TURN_CUT, deliver_typed, is_batch_ripe, take_ripe_batches
from bridge.rewake import _has_wakeful_mail  # pyright: ignore[reportPrivateUsage]
from bridge.serve import wake_ripe_batches

T = 1_800_000_000_000
MIN = 60_000
BATCHED = Config(batch_count=10, batch_age_min=5)
OWNER = Party(USER_CONVERSATION, "dana")
WORKER = Party("conv-worker", "fix-login")
ANA = Recipient("ana", "conv-ana", 4242, "idle", None, False)
BEN = Recipient("ben", "conv-ben", 4343, "idle", None, False)


@pytest.fixture
def conn(tmp_path: Path) -> Generator[sqlite3.Connection]:
    db = connect(tmp_path / "bridge.db")
    for tab in (ANA, BEN):
        surface = f"s-{tab.handle}"
        db.execute("INSERT INTO surfaces (surface, handle, updated_at) VALUES (?, ?, 0)", (surface, tab.handle))
        db.execute(
            "INSERT INTO leads (name, display_name, scope, linear_refs, tint, surface, created_at, domains)"
            " VALUES (?, ?, 'scope', '[]', 'blue', ?, 0, '[]')",
            (tab.handle, tab.handle.capitalize(), surface),
        )
        db.execute(
            "INSERT INTO instances (instance_id, pid, proc_start_ms, conversation_id, surface, first_seen_at,"
            " last_observed_at) VALUES (?, ?, 1, ?, ?, 0, 0)",
            (f"{tab.pid}@{tab.handle}", tab.pid, tab.conversation_id, surface),
        )
    yield db
    db.close()


def _send(conn: sqlite3.Connection, at: int, goes_now: bool = False) -> int:
    return send_message(conn, OWNER, [ANA], "info", f"step at {at}", at, goes_now=goes_now).message_id


def _wakes(conn: sqlite3.Connection, config: Config, at: int) -> bool:
    return _has_wakeful_mail(conn, config, "conv-ana", "4242@ana", at)


def _starts_typer(conn: sqlite3.Connection, config: Config, message_id: int, at: int) -> tuple[list[int], bool]:
    """The messages whose typer the send spawns, and whether this message's typer, past its own check, goes for it:
    deliver_typed finds no agterm tab here and types nothing, but marks a message it went for."""
    with (
        mock.patch("bridge.cli_board.spawn_typed_delivery") as spawn,
        mock.patch("bridge.cli_board.now_ms", return_value=at),
    ):
        spawn_when_ripe(conn, config, message_id, [ANA])
    with mock.patch("bridge.feed.now_ms", return_value=at):
        deliver_typed(conn, config, message_id, "conv-ana")
    wanted = conn.execute("SELECT typing_wanted_at FROM message_recipients WHERE message_id = ?", (message_id,))
    return [c.args[0] for c in spawn.call_args_list], wanted.fetchone()[0] is not None


def test_nine_waiting_messages_wake_nothing_and_the_tenth_starts_the_typer_of_the_first(
    conn: sqlite3.Connection,
) -> None:
    sent = [_send(conn, T + i) for i in range(9)]

    assert not _wakes(conn, BATCHED, T + 10)
    assert all(_starts_typer(conn, BATCHED, m, T + 10) == ([], False) for m in sent)
    assert take_ripe_batches(conn, BATCHED, T + 10) == []

    tenth = _send(conn, T + 10)

    assert _wakes(conn, BATCHED, T + 10)
    assert _starts_typer(conn, BATCHED, tenth, T + 10) == ([sent[0]], True)


def test_a_lone_message_is_woken_for_by_maintenance_at_five_minutes_and_only_once(conn: sqlite3.Connection) -> None:
    message = _send(conn, T)
    assert take_ripe_batches(conn, BATCHED, T + 4 * MIN) == []
    assert not _wakes(conn, BATCHED, T + 4 * MIN)

    [(woken, tab)] = take_ripe_batches(conn, BATCHED, T + 5 * MIN)
    assert (woken, tab.conversation_id) == (message, "conv-ana")
    assert _wakes(conn, BATCHED, T + 5 * MIN)
    with mock.patch("bridge.feed.now_ms", return_value=T + 5 * MIN):
        deliver_typed(conn, BATCHED, message, "conv-ana")

    assert take_ripe_batches(conn, BATCHED, T + 5 * MIN + 30_000) == [], "its typer owns it now"


def test_maintenance_wakes_a_batch_by_its_oldest_message(conn: sqlite3.Connection) -> None:
    sent = [_send(conn, T + i * MIN) for i in range(3)]

    assert [m for m, _ in take_ripe_batches(conn, BATCHED, T + 5 * MIN)] == [sent[0]]


def test_a_now_message_wakes_at_once(conn: sqlite3.Connection) -> None:
    _send(conn, T)
    urgent = _send(conn, T + 1000, goes_now=True)

    assert _wakes(conn, BATCHED, T + 1000)
    assert _starts_typer(conn, BATCHED, urgent, T + 1000)[1]


@pytest.mark.parametrize("post_type", [BLOCKED_TAB, TURN_CUT])
def test_bridges_compaction_request_and_turn_cut_resume_wake_at_once(conn: sqlite3.Connection, post_type: str) -> None:
    control = send_message(conn, Party("system", "bridge"), [ANA], "info", "your context is full", T).message_id
    conn.execute("UPDATE messages SET post_type = ? WHERE id = ?", (post_type, control))

    assert is_batch_ripe(conn, BATCHED, "conv-ana", T)


def test_a_busy_tabs_hook_gets_each_message_as_it_comes(conn: sqlite3.Connection) -> None:
    message = send_message(conn, WORKER, [ANA], "info", "deployed to staging", T).message_id

    context = take_delivery(conn, "conv-ana", T, False).context or ""

    assert [int(n) for n in re.findall(r"^#(\d+) ", context, re.MULTILINE)] == [message]


def test_batching_off_wakes_for_each_message(conn: sqlite3.Connection) -> None:
    unbatched = Config(batch_age_min=0)
    _send(conn, T)
    message = _send(conn, T + 1000)

    assert take_ripe_batches(conn, unbatched, T + 10 * MIN) == [], "maintenance starts no typer of its own"
    assert _wakes(conn, unbatched, T)
    assert _starts_typer(conn, unbatched, message, T) == ([message], True), "each send its own typer, as on main"


def test_a_request_is_overdue_only_past_the_longest_batch_wait(conn: sqlite3.Connection) -> None:
    send_message(conn, WORKER, [ANA], "request", "review my PR", T, respond_within_s=60)

    assert flag_overdue(conn, T + 6 * MIN, 5 * MIN) == 0
    assert flag_overdue(conn, T + 6 * MIN + 1, 5 * MIN) == 1


def test_the_sender_is_told_a_held_message_waits_for_the_batch(
    conn: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    message = send_message(conn, WORKER, [ANA], "info", "deployed to staging", T).message_id

    with (
        mock.patch("bridge.cli_board.find_lead_recipients", return_value=[ANA]),
        mock.patch("bridge.cli_board.find_worker_recipients", return_value=[]),
        mock.patch("bridge.cli_board.find_assistant_recipients", return_value=[]),
        mock.patch("bridge.cli_board.spawn_typed_delivery") as spawn,
        mock.patch("bridge.cli_board.now_ms", return_value=T),
    ):
        print_wake_lines(conn, BATCHED, WORKER, message, [ANA], [])

    assert capsys.readouterr().out == "ana gets this with its next batch (10 waiting or 5 min).\n"
    assert not spawn.called


@pytest.mark.parametrize("command", ["send ana hello", "reply 7 hello", "board hello"])
def test_send_reply_and_board_take_now(command: str) -> None:
    assert build_parser(Config()).parse_args([*command.split(), "--now"]).now


def test_one_tabs_failed_wake_does_not_stop_the_others() -> None:
    with (
        mock.patch("bridge.serve.write_hint"),
        mock.patch("bridge.serve.spawn_typed_delivery", side_effect=[OSError("no processes"), None]) as spawn,
        mock.patch("bridge.serve.log_error"),
    ):
        wake_ripe_batches([(1, ANA), (2, BEN)])

    assert [c.args[0] for c in spawn.call_args_list] == [1, 2]
