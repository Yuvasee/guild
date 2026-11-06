import argparse
import json
import os
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest import mock

import pytest
from conftest import BridgeEnv, SpawnProcess
from test_leads import Team
from test_typing import ENTER, NBSP, PROMPT, SPINNER, FakeClaudeBox, _overdue_message_to  # pyright: ignore[reportPrivateUsage]

from bridge.cli import cmd_deliver
from bridge.compaction import map_screen_to_draft
from bridge.config import Config
from bridge.feed import (
    AGTERM_SILENT,
    SCREEN_UNREAD,
    StillLockedError,
    UndeliveredTab,
    convert_screen_to_hold_reason,
    deliver_typed,
    hold_typer_lock,
    prune_typer_locks,
    queue_retry_without_db,
    retry_when_locked,
    spawn_queued_retries,
    take_new_undelivered_tabs,
    tell_of_undelivered_tab,
)
from bridge.serve import DashboardState
from bridge.tabinput import ORPHAN_STEADY_S, type_when_idle
from bridge.wake import SLEEP_GAP_S, WakeWatch

EDGE = chr(0x2500) * 101
NIGHT_S = 8 * 3600.0


class MacClock:
    """time as tabinput sees it: sleeping the Mac moves the wall clock alone, as macOS stops the monotonic one."""

    def __init__(self) -> None:
        self.wall = 1_800_000_000.0
        self.mono = 1000.0
        self.night_at: float | None = None
        self.night_s = NIGHT_S

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def sleep(self, seconds: float) -> None:
        self.wall += seconds
        self.mono += seconds
        if self.night_at is not None and self.mono >= self.night_at:
            self.night_at = None
            self.wall += self.night_s


def _type_on(clock: MacClock, tmp_path: Path, run: Callable[[Config, list[str]], str | None], **kw: object) -> str:
    with (
        mock.patch("bridge.tabinput.run_agterm", run),
        mock.patch("bridge.tabinput.state_dir", lambda: tmp_path),
        mock.patch("bridge.tabinput.time", clock),
    ):
        return type_when_idle(Config(), "S", "P", "hello", lambda: True, wait_s=600, **kw)  # type: ignore[arg-type]


def _two_unsent_messages_screen() -> str:
    """The shape of a tab after two typers typed into one box overnight: a 14-line box in a 101-column pane,
    its top rule 18 lines above the screen's foot."""
    first = "[bridge] Peer message #41 from rev, not from the user; use judgment: " + "done with the sweep " * 26
    second = "[bridge] Peer message #42 from rev, not from the user; use judgment: " + "review doc reordered " * 24
    text = f"{first}(full text and answer: bridge show 41){second}(full text and answer: bridge show 42)"
    lines: list[str] = []
    line = ""
    words = text.split(" ")
    for word in words:
        if len(line) + len(word) + 1 > 97:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    lines.append(line)
    box = [f"{PROMPT} {lines[0]}", *(f"  {rest}" for rest in lines[1:])]
    return "\n".join(
        [
            "  the reply will go out as soon as the Mac is back.",
            "",
            "✻ Cogitated for 55s · done 14:00",
            " " * 20 + "new task? /clear to save 120.0k tokens",
            EDGE,
            *box,
            EDGE,
            "  Opus 5.5 | 39% ctx | ~/repo",
            "  -- INSERT -- ⏵⏵ bypass permissions on (shift+tab to cycle)",
        ]
    )


def test_a_box_holding_two_unsent_bridge_messages_reads_as_their_text() -> None:
    screen = _two_unsent_messages_screen()
    lines = [line for line in screen.splitlines() if line.strip()]
    assert len(lines) - lines.index(EDGE) > 14

    box = map_screen_to_draft(screen)

    assert box is not None and box.startswith("[bridge] Peer message #41")
    assert box.endswith("bridge show 42)")


def test_the_saved_screen_of_two_unsent_bridge_messages_reads_as_their_text() -> None:
    screen = (Path(__file__).parent / "screens" / "two-unsent-messages.txt").read_text(encoding="utf-8")

    box = map_screen_to_draft(screen)

    assert box is not None and box.startswith("[bridge] Peer message #41 from")
    assert "41)[bridge] Peer message #42 from" in box and box.endswith("bridge reply 42)")


def test_the_alert_for_two_unsent_bridge_messages_names_bridges_own_text() -> None:
    tab = UndeliveredTab("c", "denis", "S", "P", None, 41, 0, 2, None, False, False, False)

    reason = convert_screen_to_hold_reason(tab, _two_unsent_messages_screen())

    assert reason is not None and "bridge's own text of #41" in reason


def test_a_wait_that_spans_a_night_ends_right_after_wake(tmp_path: Path) -> None:
    clock = MacClock()
    clock.night_at = clock.mono + 10
    fake = FakeClaudeBox()
    reads: list[float] = []

    def in_a_turn(config: Config, args: list[str]) -> str | None:
        if args[:2] == ["session", "text"]:
            reads.append(clock.wall)
            return f"{SPINNER}\n\n{fake.run(config, args)}"
        return fake.run(config, args)

    result = _type_on(clock, tmp_path, in_a_turn)

    assert result == "timeout"
    assert len([at for at in reads if at > 1_800_000_000 + NIGHT_S]) == 0
    assert fake.typed == []


def test_a_draft_proven_before_the_night_is_not_reported_after_it(tmp_path: Path) -> None:
    clock = MacClock()
    clock.night_at = clock.mono + 10
    fake = FakeClaudeBox(boxes={"S": "my own draft"})

    result = _type_on(clock, tmp_path, fake.run, may_probe_box=lambda: True)

    assert result == "timeout"
    assert fake.boxes["S"] == "my own draft"


def test_a_draft_proven_without_a_night_is_reported_at_the_end_of_the_wait(tmp_path: Path) -> None:
    fake = FakeClaudeBox(boxes={"S": "my own draft"})

    result = _type_on(MacClock(), tmp_path, fake.run, may_probe_box=lambda: True)

    assert result == "draft"


def test_bridge_text_left_unsent_before_a_short_sleep_is_not_pressed_as_steady_right_after_it(tmp_path: Path) -> None:
    clock = MacClock()
    clock.night_at, clock.night_s = clock.mono + 1, 300.0
    left = "[bridge] Peer message #5 from aide, not from the user; use judgment: look (full text: bridge show 5)"
    fake = FakeClaudeBox(boxes={"S": left})
    pressed_at: list[float] = []
    looks: list[float] = []

    def run(config: Config, args: list[str]) -> str | None:
        if args[:2] == ["session", "text"]:
            looks.append(clock.mono)
        if args[:2] == ["session", "type"] and args[-1] == ENTER:
            pressed_at.append(clock.mono)
        return fake.run(config, args)

    def claim_left_unsent(box: str) -> Callable[[bool], None]:
        return lambda sent: None

    _type_on(clock, tmp_path, run, may_probe_box=lambda: True, claim_left_unsent=claim_left_unsent)

    assert fake.submitted[0] == left
    # Steady since the first look after the sleep, never since the one before it.
    assert pressed_at[0] - looks[1] >= ORPHAN_STEADY_S


def test_a_tab_that_stopped_answering_by_its_id_is_found_again_by_its_pane(tmp_path: Path) -> None:
    fake = FakeClaudeBox()
    tree = {"result": {"tree": {"workspaces": [{"sessions": [{"id": "NEW", "surfaces": [{"paneID": "P"}]}]}]}}}
    targets: list[str] = []

    def run(config: Config, args: list[str]) -> str | None:
        if args[:1] == ["tree"]:
            return json.dumps(tree)
        target = args[args.index("--target") + 1]
        targets.append(target)
        return None if target == "S" else fake.run(config, [*args[:2], "--target", "S", *args[4:]])

    result = _type_on(MacClock(), tmp_path, run)

    assert result == "typed"
    assert fake.submitted == ["hello"]
    assert targets[0] == "S" and set(targets[1:]) == {"NEW"}


def _tree(session: str, selected: bool = False) -> dict[str, object]:
    tab = {"id": session, "active": selected, "surfaces": [{"paneID": "P"}]}
    return {"result": {"tree": {"workspaces": [{"active": selected, "sessions": [tab]}]}}}


def test_a_tab_found_again_by_its_pane_gets_no_probe_key_while_selected(tmp_path: Path) -> None:
    fake = FakeClaudeBox(boxes={"S": "try the tests"}, suggestions={"S"})

    def run(config: Config, args: list[str]) -> str | None:
        if args[:1] == ["tree"]:
            return json.dumps(_tree("NEW", selected=True))
        if args[args.index("--target") + 1] == "S":
            return None
        return fake.run(config, [*args[:2], "--target", "S", *args[4:]])

    result = _type_on(MacClock(), tmp_path, run, may_probe_box=lambda: True)

    assert result == "unproven"
    assert fake.typed == []


def test_a_sleep_during_a_screen_read_drops_that_read(tmp_path: Path) -> None:
    clock = MacClock()
    fake = FakeClaudeBox()
    reads = [0]

    def run(config: Config, args: list[str]) -> str | None:
        if args[:1] == ["tree"]:
            return None
        if args[:2] == ["session", "text"]:
            reads[0] += 1
            if reads[0] == 2:
                clock.wall += NIGHT_S
        return fake.run(config, args)

    result = _type_on(clock, tmp_path, run)

    assert result == "timeout"
    assert fake.typed == []


def test_a_sleep_during_the_last_look_before_typing_types_nothing(tmp_path: Path) -> None:
    clock = MacClock()
    fake = FakeClaudeBox()
    reads = [0]

    def run(config: Config, args: list[str]) -> str | None:
        if args[:2] == ["session", "text"]:
            reads[0] += 1
            if reads[0] == 3:
                # The look under the pane lock: the Mac sleeps, the user writes a draft, the read shows the old box.
                stale = fake.run(config, args)
                clock.wall += NIGHT_S
                fake.boxes["S"] = "user draft"
                return stale
        return fake.run(config, args)

    result = _type_on(clock, tmp_path, run)

    assert result == "timeout"
    assert fake.typed == []
    assert fake.boxes["S"] == "user draft"


def test_a_wall_clock_set_back_does_not_stretch_the_wait_for_typed_text(tmp_path: Path) -> None:
    clock = MacClock()
    fake = FakeClaudeBox()
    typed_at: list[float] = []

    def run(config: Config, args: list[str]) -> str | None:
        if args[:2] == ["session", "type"]:
            fake.typed.append(args[-1])
            typed_at.append(clock.mono)
            return "ok"
        if typed_at and len(typed_at) == 1:
            # Set back once, while the typed text is awaited.
            typed_at.append(clock.mono)
            clock.wall -= 3600
        return fake.run(config, args)

    result = _type_on(clock, tmp_path, run, claim=lambda: True)

    assert result == "failed"
    assert fake.typed == ["hello"]
    assert clock.mono - typed_at[0] < 60


def test_one_typer_per_message_and_conversation(tmp_path: Path) -> None:
    with mock.patch("bridge.feed.state_dir", lambda: tmp_path):
        with hold_typer_lock(7, "conv") as first, hold_typer_lock(7, "conv") as second:
            other_tab = hold_typer_lock(7, "conv-b").__enter__()
        with hold_typer_lock(7, "conv") as after:
            pass

    assert (first, second, other_tab, after) == (True, False, True, True)


def test_old_typer_locks_go_and_a_held_one_stays(tmp_path: Path) -> None:
    with mock.patch("bridge.feed.state_dir", lambda: tmp_path):
        with hold_typer_lock(1, "c"):
            pass
        with hold_typer_lock(2, "c") as held:
            day_ago = time.time() - 2 * 86_400
            for name in ("deliver-1-c.lock", "deliver-2-c.lock"):
                os.utime(tmp_path / "locks" / name, (day_ago, day_ago))
            prune_typer_locks()
            left = sorted(p.name for p in (tmp_path / "locks").glob("deliver-*"))

    assert held
    assert left == ["deliver-2-c.lock"]


class Clocks:
    def __init__(self) -> None:
        self.wall = 1_800_000_000.0
        self.mono = 1000.0

    def awake(self, seconds: float) -> None:
        self.wall += seconds
        self.mono += seconds


def test_the_delay_counts_from_the_last_wake() -> None:
    clocks = Clocks()
    watch = WakeWatch(lambda: clocks.wall, lambda: clocks.mono)
    clocks.awake(30)
    before = watch.counted_from()
    clocks.wall += NIGHT_S
    clocks.awake(1)
    woke_at = int(clocks.wall * 1000)
    woke = watch.counted_from()
    clocks.awake(30)
    later = watch.counted_from()
    clocks.wall += SLEEP_GAP_S / 2
    jitter = watch.counted_from()

    assert before == 0
    assert woke == woke_at
    assert later == woke and jitter == woke


def test_a_wall_clock_set_back_leaves_no_count_starting_in_the_future() -> None:
    clocks = Clocks()
    watch = WakeWatch(lambda: clocks.wall, lambda: clocks.mono)
    clocks.wall += NIGHT_S
    clocks.awake(1)
    watch.counted_from()
    clocks.wall -= 3600
    clocks.awake(1)

    assert watch.counted_from() == int(clocks.wall * 1000)


def test_the_delay_counts_from_when_agterm_stopped_answering_and_then_from_its_return() -> None:
    clocks = Clocks()
    watch = WakeWatch(lambda: clocks.wall, lambda: clocks.mono)
    clocks.awake(60)
    watch.note_agterm(False)
    stopped = int(clocks.wall * 1000)
    clocks.awake(300)
    watch.note_agterm(False)
    silent = watch.counted_from()
    clocks.awake(60)
    watch.note_agterm(True)
    back = int(clocks.wall * 1000)

    assert silent == stopped
    assert watch.counted_from() == back


def test_an_alert_read_with_agterm_silent_says_so_and_never_that_the_tab_is_gone() -> None:
    state = DashboardState(Config())
    tab = UndeliveredTab("c", "denis", "S", "P", None, 1, 0, 1, None, False, False, False)

    def silent(config: Config, args: list[str]) -> str | None:
        return None

    with mock.patch("bridge.serve.run_agterm", silent):
        screen, why = state._read_undelivered_tab(tab)  # pyright: ignore[reportPrivateUsage]

    assert (screen, why) == (None, AGTERM_SILENT)
    assert convert_screen_to_hold_reason(tab, screen, why) == "agterm is not answering"


def test_an_alert_reads_a_tab_found_again_by_its_pane() -> None:
    tree = {"result": {"tree": {"workspaces": [{"sessions": [{"id": "NEW", "surfaces": [{"paneID": "P"}]}]}]}}}

    def run(config: Config, args: list[str]) -> str | None:
        if args[:1] == ["tree"]:
            return json.dumps(tree)
        return f"{EDGE}\n{PROMPT}{NBSP}\n{EDGE}" if args[args.index("--target") + 1] == "NEW" else None

    state = DashboardState(Config())
    tab = UndeliveredTab("c", "denis", "S", "P", None, 1, 0, 1, None, False, False, False)

    with mock.patch("bridge.serve.run_agterm", run):
        screen, _ = state._read_undelivered_tab(tab)  # pyright: ignore[reportPrivateUsage]

    assert screen is not None and PROMPT in screen


def test_an_alert_for_a_tab_agterm_shows_but_cannot_read_never_says_it_is_gone() -> None:
    def run(config: Config, args: list[str]) -> str | None:
        return json.dumps(_tree("S")) if args[:1] == ["tree"] else None

    tab = UndeliveredTab("c", "denis", "S", "P", None, 1, 0, 1, None, False, False, False)

    with mock.patch("bridge.serve.run_agterm", run):
        screen, why = DashboardState(Config())._read_undelivered_tab(tab)  # pyright: ignore[reportPrivateUsage]

    assert (screen, why) == (None, SCREEN_UNREAD)


def test_a_screen_read_after_agterm_was_silent_restarts_the_count() -> None:
    clocks = Clocks()
    state = DashboardState(Config())
    state._wake = WakeWatch(lambda: clocks.wall, lambda: clocks.mono)  # pyright: ignore[reportPrivateUsage]
    state._wake.note_agterm(False)  # pyright: ignore[reportPrivateUsage]
    clocks.awake(900)
    tab = UndeliveredTab("c", "denis", "S", "P", None, 1, 0, 1, None, False, False, False)

    def idle(config: Config, args: list[str]) -> str | None:
        return f"{EDGE}\n{PROMPT}{NBSP}\n{EDGE}"

    with mock.patch("bridge.serve.run_agterm", idle):
        state._read_undelivered_tab(tab)  # pyright: ignore[reportPrivateUsage]

    assert state._wake.counted_from() == int(clocks.wall * 1000)  # pyright: ignore[reportPrivateUsage]


def test_mail_that_waited_through_a_night_is_overdue_only_ten_minutes_after_wake(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    config = Config(assistant_handle="old-b")
    message_id = _overdue_message_to(bridge_env, "alex", team.b)
    woke = 1000 + 400 * 60_000
    with bridge_env.connect() as conn:
        just_woke = take_new_undelivered_tabs(conn, config, woke + 60_000, counted_from=woke)
        awake = take_new_undelivered_tabs(conn, config, woke + 11 * 60_000, counted_from=woke)
        told = tell_of_undelivered_tab(conn, config, awake[0], None, woke + 11 * 60_000)
        body = conn.execute("SELECT body FROM messages WHERE id = ?", (told[0] if told else 0,)).fetchone()[0]

    assert just_woke == []
    assert [t.oldest_message_id for t in awake] == [message_id]
    assert f"not received #{message_id} for 11 min since the Mac woke or agterm answered again" in body


LOCKED = sqlite3.OperationalError("database is locked")


def test_a_locked_database_is_waited_out() -> None:
    calls: list[int] = []

    def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise LOCKED
        return "done"

    with mock.patch("bridge.feed.time.sleep"):
        assert retry_when_locked("claim", flaky) == "done"
    assert len(calls) == 3


def test_a_database_that_stays_locked_is_logged_and_given_up(tmp_path: Path) -> None:
    def locked() -> str:
        raise LOCKED

    with (
        mock.patch("bridge.feed.time.sleep"),
        mock.patch("bridge.errlog.state_dir", lambda: tmp_path),
        pytest.raises(StillLockedError),
    ):
        retry_when_locked("#7 to c: claim", locked)

    assert "#7 to c: claim: gave up, database is locked" in (tmp_path / "errors.log").read_text()


def test_other_database_errors_are_not_retried() -> None:
    calls: list[int] = []

    def broken() -> str:
        calls.append(1)
        raise sqlite3.OperationalError("no such table: x")

    with mock.patch("bridge.feed.time.sleep"), pytest.raises(sqlite3.OperationalError):
        retry_when_locked("claim", broken)
    assert len(calls) == 1


def _deliver_with_locked(tmp_path: Path, locked_sql: str, typer: Callable[..., str]) -> str:
    def execute(sql: str, *args: object) -> mock.MagicMock:
        if sql.lstrip().startswith(locked_sql):
            raise LOCKED
        return mock.MagicMock()

    conn = mock.MagicMock()
    conn.execute.side_effect = execute
    row = {
        "from_conversation": "conv-b",
        "from_handle": "b",
        "body": "look",
        "kind": "message",
        "respond_by": None,
        "agterm_session": "S",
        "surface": "P",
        "instance_id": "i",
    }
    with (
        mock.patch("bridge.feed.time.sleep"),
        mock.patch("bridge.errlog.state_dir", lambda: tmp_path),
        mock.patch("bridge.feed._find_typable_row", return_value=row),
        mock.patch("bridge.feed.is_delivery_owner", return_value=True),
        mock.patch("bridge.feed.is_surface_locked", return_value=False),
        mock.patch("bridge.feed.type_when_idle", typer),
    ):
        return str(deliver_typed(conn, Config(), 7, "conv-a"))


def test_a_typer_whose_claim_stays_locked_fails_for_the_retry_pass_instead_of_going_quiet(tmp_path: Path) -> None:
    def typer(*args: object, claim: Callable[[], bool], **kw: object) -> str:
        return "unwanted" if not claim() else "typed"

    result = _deliver_with_locked(tmp_path, "BEGIN", typer)

    assert result == "failed"
    assert "#7 to conv-a: claim: gave up" in (tmp_path / "errors.log").read_text()


def test_a_typer_that_cannot_read_whether_it_is_wanted_stops_and_fails(tmp_path: Path) -> None:
    def typer(
        config: Config, session: str, pane: str, text: str, still_wanted: Callable[[], bool], **kw: object
    ) -> str:
        return "unwanted" if not still_wanted() else "typed"

    result = _deliver_with_locked(tmp_path, "SELECT 1 FROM message_recipients", typer)

    assert result == "failed"
    assert "check it is still wanted: gave up" in (tmp_path / "errors.log").read_text()


def test_a_claimed_typer_that_cannot_read_whether_it_is_wanted_stops_as_stuck_and_gives_the_claim_back(
    tmp_path: Path,
) -> None:
    pressed: list[str] = []

    def typer(*args: object, claim: Callable[[], bool], **kw: object) -> str:
        still_wanted = cast(Callable[[], bool], args[4])
        claim()
        if still_wanted():
            pressed.append(ENTER)
        return "typed"

    released: list[str] = []

    def execute(sql: str, *args: object) -> mock.MagicMock:
        if sql.lstrip().startswith("SELECT 1 FROM message_recipients"):
            raise LOCKED
        if "offered_at = NULL" in sql:
            released.append(sql)
        cursor = mock.MagicMock()
        cursor.rowcount = 1
        return cursor

    conn = mock.MagicMock()
    conn.execute.side_effect = execute
    row = {"from_conversation": "conv-b", "from_handle": "b", "body": "look", "kind": "message", "respond_by": None}
    row |= {"agterm_session": "S", "surface": "P", "instance_id": "i"}
    with (
        mock.patch("bridge.feed.time.sleep"),
        mock.patch("bridge.errlog.state_dir", lambda: tmp_path),
        mock.patch("bridge.feed._find_typable_row", return_value=row),
        mock.patch("bridge.feed.type_when_idle", typer),
    ):
        result = deliver_typed(conn, Config(), 7, "conv-a")

    assert result == "stuck"
    assert pressed == []
    assert len(released) == 1


def test_a_delivery_the_database_never_let_record_is_started_again_by_serve(tmp_path: Path) -> None:
    spawned: list[tuple[int, str]] = []

    def spawn(message_id: int, conversation_id: str) -> None:
        spawned.append((message_id, conversation_id))

    with mock.patch("bridge.feed.state_dir", lambda: tmp_path), mock.patch("bridge.feed._spawn_typer", spawn):
        queue_retry_without_db(7, "5eedface-5016")
        spawn_queued_retries()
        spawn_queued_retries()

    assert spawned == [(7, "5eedface-5016")]


def test_a_lookup_locked_for_minutes_is_waited_out_before_the_typer_starts(tmp_path: Path) -> None:
    calls: list[int] = []

    def find(*args: object) -> None:
        calls.append(1)
        if len(calls) < 10:
            raise LOCKED

    with (
        mock.patch("bridge.feed.time.sleep"),
        mock.patch("bridge.errlog.state_dir", lambda: tmp_path),
        mock.patch("bridge.feed._find_typable_row", find),
    ):
        result = deliver_typed(mock.MagicMock(), Config(), 7, "conv-a")

    assert result is None and len(calls) == 10


def test_a_delivery_queued_without_the_database_lets_go_of_its_typer_lock_first(tmp_path: Path) -> None:
    lock_free_when_queued: list[bool] = []

    def queue(message_id: int, conversation_id: str) -> None:
        with hold_typer_lock(message_id, conversation_id) as free:
            lock_free_when_queued.append(free)

    def locked(*args: object) -> None:
        raise LOCKED

    args = argparse.Namespace(message_id="7", conversation_id="conv-a")
    with (
        mock.patch("bridge.feed.state_dir", lambda: tmp_path),
        mock.patch("bridge.errlog.state_dir", lambda: tmp_path),
        mock.patch("bridge.feed.time.sleep"),
        mock.patch("bridge.cli.deliver_typed", return_value="failed"),
        mock.patch("bridge.cli.transaction", side_effect=locked),
        mock.patch("bridge.cli.queue_retry_without_db", queue),
    ):
        code = cmd_deliver(args, mock.MagicMock(), Config())

    assert code == 1
    assert lock_free_when_queued == [True]


def test_a_queued_delivery_whose_typer_cannot_start_stays_queued(tmp_path: Path) -> None:
    def cannot_spawn(message_id: int, conversation_id: str) -> None:
        raise OSError("fork failed")

    with mock.patch("bridge.feed.state_dir", lambda: tmp_path), mock.patch("bridge.feed._spawn_typer", cannot_spawn):
        queue_retry_without_db(7, "conv-a")
        spawn_queued_retries()

    assert [p.name for p in (tmp_path / "typer-retries").iterdir()] == ["7-conv-a"]
