import argparse
import re
import sqlite3
from collections.abc import Generator
from pathlib import Path
from unittest import mock

import pytest

from bridge.board import (
    DELIVERED_BODY_MAX_CHARS,
    USER_CONVERSATION,
    Party,
    Recipient,
    flag_overdue,
    send_message,
    take_delivery,
)
from bridge.cli import build_parser, cmd_deliver
from bridge.cli_board import print_wake_lines, spawn_when_ripe
from bridge.config import HIDDEN_NAME_MARKS, Config
from bridge.db import connect
from bridge.feed import (
    BLOCKED_TAB,
    RELAYED_ANSWER,
    TURN_CUT,
    deliver_typed,
    is_batch_ripe,
    record_failed_delivery,
    take_new_undelivered_tabs,
    take_ripe_batches,
    tell_of_undelivered_tab,
)
from bridge.rewake import _has_wakeful_mail  # pyright: ignore[reportPrivateUsage]
from bridge.serve import spawn_typers

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


def _spawned(conn: sqlite3.Connection, config: Config, message_id: int, at: int) -> list[int]:
    with (
        mock.patch("bridge.cli_board.spawn_typed_delivery") as spawn,
        mock.patch("bridge.cli_board.now_ms", return_value=at),
    ):
        spawn_when_ripe(conn, config, message_id, [ANA])
    return [c.args[0] for c in spawn.call_args_list]


def _typer_goes_for(
    conn: sqlite3.Connection, config: Config, message_id: int, at: int, conversation_id: str = "conv-ana"
) -> list[int]:
    """The messages marked meant to be typed once this message's typer ran, whoever spawned it: there is no agterm tab
    here, so it types nothing, but it marks the message it went for and any it passed over."""
    with mock.patch("bridge.feed.now_ms", return_value=at):
        cmd_deliver(argparse.Namespace(message_id=message_id, conversation_id=conversation_id), conn, config)
    rows = conn.execute("SELECT message_id FROM message_recipients WHERE typing_wanted_at IS NOT NULL ORDER BY 1")
    return [int(r[0]) for r in rows]


def test_nine_waiting_messages_wake_nothing_and_the_tenths_typer_types_the_first(conn: sqlite3.Connection) -> None:
    """Whoever spawns the typer (a send, the dashboard, a retry), it types the batch from its oldest message."""
    sent = [_send(conn, T + i) for i in range(9)]

    assert not _wakes(conn, BATCHED, T + 10)
    assert all(_spawned(conn, BATCHED, m, T + 10) == [] for m in sent)
    assert all(_typer_goes_for(conn, BATCHED, m, T + 10) == [] for m in sent)
    assert take_ripe_batches(conn, BATCHED, T + 10, {}) == []

    tenth = _send(conn, T + 10)

    assert _wakes(conn, BATCHED, T + 10)
    assert _spawned(conn, BATCHED, tenth, T + 10) == [tenth]
    assert _typer_goes_for(conn, BATCHED, tenth, T + 10) == [sent[0], tenth], "the first, and the tenth still counted"


def test_a_lone_message_is_woken_for_by_maintenance_at_five_minutes_and_only_once(conn: sqlite3.Connection) -> None:
    message = _send(conn, T)
    spawned_at: dict[str, int] = {}
    assert take_ripe_batches(conn, BATCHED, T + 4 * MIN, spawned_at) == []
    assert not _wakes(conn, BATCHED, T + 4 * MIN)

    [(woken, tab)] = take_ripe_batches(conn, BATCHED, T + 5 * MIN, spawned_at)
    assert (woken, tab.conversation_id) == (message, "conv-ana")
    assert _wakes(conn, BATCHED, T + 5 * MIN)

    assert take_ripe_batches(conn, BATCHED, T + 5 * MIN + 30_000, spawned_at) == [], "its typer may still wait for idle"
    conn.execute("UPDATE message_recipients SET typing_started_at = ? WHERE message_id = ?", (T + 6 * MIN, message))
    assert take_ripe_batches(conn, BATCHED, T + 8 * MIN, spawned_at) == [], "its typer types it"
    conn.execute("UPDATE message_recipients SET typing_started_at = NULL WHERE message_id = ?", (message,))
    assert [m for m, _ in take_ripe_batches(conn, BATCHED, T + 8 * MIN, spawned_at)] == [message], "it left: again"


def test_a_typer_refused_by_a_lock_leaves_the_batch_to_maintenance_once_the_tab_is_unlocked(
    conn: sqlite3.Connection,
) -> None:
    message = _send(conn, T)
    conn.execute(
        "INSERT INTO conversations (conversation_id, instance_id, first_seen_at, last_seen_at)"
        " VALUES ('conv-ana', '4242@ana', 0, 0)"
    )
    conn.execute("UPDATE surfaces SET tab_name = ? WHERE surface = 's-ana'", (f"notes {HIDDEN_NAME_MARKS[0]}",))
    with mock.patch("bridge.feed.now_ms", return_value=T + 5 * MIN):
        assert deliver_typed(conn, BATCHED, message, "conv-ana") is None

    assert take_ripe_batches(conn, BATCHED, T + 5 * MIN + 30_000, {}) == [], "a locked tab is outside everything"
    conn.execute("UPDATE surfaces SET tab_name = NULL WHERE surface = 's-ana'")
    assert [m for m, _ in take_ripe_batches(conn, BATCHED, T + 6 * MIN, {})] == [message]


def _add_worker(conn: sqlite3.Connection) -> Recipient:
    conn.execute("INSERT INTO surfaces (surface, handle, updated_at) VALUES ('s-wk', 'fix-login', 0)")
    conn.execute(
        "INSERT INTO instances (instance_id, pid, proc_start_ms, conversation_id, surface, first_seen_at,"
        " last_observed_at) VALUES ('77@wk', 77, 1, 'conv-worker', 's-wk', 0, 0)"
    )
    return Recipient("fix-login", "conv-worker", 77, "idle", None, False)


@pytest.mark.parametrize(("to_worker", "urgent"), [(False, TURN_CUT), (True, TURN_CUT), (False, "now")])
def test_mail_that_goes_now_behind_older_mail_is_told_of_as_undelivered_by_its_own_time(
    conn: sqlite3.Connection, to_worker: bool, urgent: str
) -> None:
    tab = _add_worker(conn) if to_worker else ANA
    send_message(conn, OWNER, [tab], "info", "look at the logs", T)
    body = "Your last turn was cut by Mac sleep at 09:50: go on."
    resume = send_message(conn, Party("system", "bridge"), [tab], "info", body, T + MIN, goes_now=urgent == "now")
    if urgent == TURN_CUT:
        conn.execute("UPDATE messages SET post_type = ? WHERE id = ?", (TURN_CUT, resume.message_id))

    _typer_goes_for(conn, BATCHED, resume.message_id, T + MIN, tab.conversation_id)

    assert take_new_undelivered_tabs(conn, BATCHED, T + 6 * MIN - 1) == []
    [told] = take_new_undelivered_tabs(conn, BATCHED, T + 6 * MIN)
    assert (told.conversation_id, told.cut is not None) == (tab.conversation_id, urgent == TURN_CUT)


def test_a_relayed_answer_a_typer_went_for_does_not_hold_back_a_workers_later_batch(conn: sqlite3.Connection) -> None:
    worker = _add_worker(conn)
    relayed = send_message(conn, Party("conv-ben", "ben"), [worker], "info", "the owner said: yes", T).message_id
    conn.execute("UPDATE messages SET post_type = ? WHERE id = ?", (RELAYED_ANSWER, relayed))
    with mock.patch("bridge.feed.now_ms", return_value=T):  # its typer ran before batching, which left it marked
        assert deliver_typed(conn, Config(batch_age_min=0), relayed, "conv-worker") is None
    record_failed_delivery(conn, relayed, "conv-worker", T)
    assert conn.execute(
        "SELECT delivery_failed_at FROM message_recipients WHERE message_id = ?", (relayed,)
    ).fetchone()[0]
    owner = send_message(conn, OWNER, [worker], "info", "and ship it", T + 1000).message_id

    assert [m for m, _ in take_ripe_batches(conn, BATCHED, T + 6 * MIN, {})] == [owner]


def test_an_old_failure_no_longer_typed_does_not_hold_back_a_later_batch(conn: sqlite3.Connection) -> None:
    peer = send_message(conn, WORKER, [ANA], "info", "deployed to staging", T).message_id
    record_failed_delivery(conn, peer, "conv-ana", T + 1000)
    conn.execute("UPDATE leads SET retired_at = 1 WHERE name = 'ana'")
    owner = _send(conn, T + 2000)

    assert [m for m, _ in take_ripe_batches(conn, BATCHED, T + 6 * MIN, {})] == [owner]


def test_maintenance_wakes_a_batch_by_its_oldest_message(conn: sqlite3.Connection) -> None:
    sent = [_send(conn, T + i * MIN) for i in range(3)]

    assert [m for m, _ in take_ripe_batches(conn, BATCHED, T + 5 * MIN, {})] == [sent[0]]


def test_a_now_message_wakes_at_once(conn: sqlite3.Connection) -> None:
    _send(conn, T)
    urgent = _send(conn, T + 1000, goes_now=True)

    assert _wakes(conn, BATCHED, T + 1000)
    assert _spawned(conn, BATCHED, urgent, T + 1000) == [urgent]


@pytest.mark.parametrize("post_type", [BLOCKED_TAB, TURN_CUT])
def test_bridges_compaction_request_and_turn_cut_resume_wake_at_once(conn: sqlite3.Connection, post_type: str) -> None:
    control = send_message(conn, Party("system", "bridge"), [ANA], "info", "your context is full", T).message_id
    conn.execute("UPDATE messages SET post_type = ? WHERE id = ?", (post_type, control))

    assert is_batch_ripe(conn, BATCHED, "conv-ana", T)


def test_a_busy_tabs_hook_gets_each_message_as_it_comes(conn: sqlite3.Connection) -> None:
    message = send_message(conn, WORKER, [ANA], "info", "deployed to staging", T).message_id

    context = take_delivery(conn, "conv-ana", T, False).context or ""

    assert [int(n) for n in re.findall(r"^#(\d+) ", context, re.MULTILINE)] == [message]


def test_a_240_char_info_message_reaches_the_hook_whole(conn: sqlite3.Connection) -> None:
    body = "word " * 47 + "ends."
    message = send_message(conn, WORKER, [ANA], "info", body, T).message_id

    context = take_delivery(conn, "conv-ana", T, False).context or ""

    assert len(body) == 240
    assert f'#{message} info from fix-login: "{body}"' in context
    assert "full text" not in context


def test_a_3000_char_message_is_cut_with_the_hint_to_read_the_rest(conn: sqlite3.Connection) -> None:
    message = send_message(conn, WORKER, [ANA], "info", "x", T).message_id
    conn.execute("UPDATE messages SET body = ? WHERE id = ?", ("x" * 3000, message))

    context = take_delivery(conn, "conv-ana", T, False, budget_bytes=10_000).context or ""

    hint = f"… (full text: bridge show {message})"
    assert f'{hint}"' in context
    assert [len(k) for k in re.findall(r"x{100,}", context)] == [DELIVERED_BODY_MAX_CHARS - len(hint)]


def test_whole_bodies_still_leave_the_rest_waiting_when_the_budget_is_full(conn: sqlite3.Connection) -> None:
    ids = [send_message(conn, WORKER, [ANA], "info", f"m{i} " + "y" * 1500, T + i).message_id for i in range(3)]

    first = take_delivery(conn, "conv-ana", T + 10, False)
    later = take_delivery(conn, "conv-ana", T + 10 + 3_600_000, False)

    assert first.leftover
    assert f"#{ids[0]} " in (first.context or "") and f"#{ids[2]} " not in (first.context or "")
    assert f"#{ids[2]} " in (later.context or "") or later.leftover


def test_batching_off_wakes_for_each_message(conn: sqlite3.Connection) -> None:
    unbatched = Config(batch_age_min=0)
    _send(conn, T)
    message = _send(conn, T + 1000)

    assert take_ripe_batches(conn, unbatched, T + 10 * MIN, {}) == [], "maintenance starts no typer of its own"
    assert _wakes(conn, unbatched, T)
    assert _spawned(conn, unbatched, message, T) == [message]
    assert _typer_goes_for(conn, unbatched, message, T) == [message], "each message its own typer, as on main"


def test_a_request_is_overdue_only_past_the_longest_batch_wait(conn: sqlite3.Connection) -> None:
    send_message(conn, WORKER, [ANA], "request", "review my PR", T, respond_within_s=60)

    assert flag_overdue(conn, T + 6 * MIN, 5 * MIN) == 0
    assert flag_overdue(conn, T + 6 * MIN + 1, 5 * MIN) == 1


def test_a_lead_is_told_of_as_undelivered_only_past_the_longest_batch_wait(conn: sqlite3.Connection) -> None:
    message = _send(conn, T)
    conn.execute("UPDATE message_recipients SET typing_wanted_at = ? WHERE message_id = ?", (T + 5 * MIN, message))

    assert take_new_undelivered_tabs(conn, BATCHED, T + 10 * MIN - 1) == []
    assert [t.oldest_message_id for t in take_new_undelivered_tabs(conn, BATCHED, T + 10 * MIN)] == [message]


def test_the_undelivered_alert_rechecks_with_the_same_wait(conn: sqlite3.Connection) -> None:
    config = Config(batch_count=10, batch_age_min=5, assistant_handle="ana")
    first, second = _send(conn, T), _send(conn, T + 4 * MIN)
    conn.execute("UPDATE message_recipients SET typing_wanted_at = ?", (T + 5 * MIN,))
    [tab] = take_new_undelivered_tabs(conn, config, T + 10 * MIN)
    conn.execute("UPDATE message_recipients SET acked_at = ? WHERE message_id = ?", (T + 10 * MIN, first))

    assert tell_of_undelivered_tab(conn, config, tab, None, T + 10 * MIN) is None
    assert conn.execute("SELECT told_at FROM undelivered_tabs").fetchone()[0] is None, f"#{second} waited 1 min more"


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


def test_one_tabs_failed_spawn_does_not_stop_the_others() -> None:
    """serve's maintenance spawns its retries and its ripe batches this way."""
    with (
        mock.patch("bridge.serve.write_hint"),
        mock.patch("bridge.serve.spawn_typed_delivery", side_effect=[OSError("no processes"), None]) as spawn,
        mock.patch("bridge.serve.log_error"),
    ):
        spawn_typers([(1, ANA), (2, BEN)], hint=True)

    assert [c.args[0] for c in spawn.call_args_list] == [1, 2]
