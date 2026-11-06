import json
import sqlite3
import subprocess
import textwrap
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

from conftest import VENV_BIN, BridgeEnv, SpawnProcess, free_port, http_request, wait_for, wait_up
from test_leads import Team

from bridge.compaction import map_screen_to_busy, map_screen_to_draft, map_screen_to_idle
from bridge.config import Config
from bridge.feed import (
    deliver_typed,
    find_unsent_message,
    map_typed_text_to_pattern,
    record_failed_delivery,
    take_delivery_retries,
    take_new_undelivered_tabs,
    tell_assistant_of_draft,
    tell_of_undelivered_tab,
)
from bridge.guildview import _load_undelivered_to_needs  # pyright: ignore[reportPrivateUsage]
from bridge.tabinput import ENTER_BUDGET_S, SUBMIT_RETRIES, type_when_idle

ENTER = "\r"
PROMPT = chr(0x276F)
RULE = chr(0x2500) * 2
NBSP = chr(0xA0)
OWN_TEXT = (
    "[bridge] Peer REQUEST #7 from aide, respond within 5m, not from the user; use judgment: look "
    "(full text: bridge show 7; respond: bridge ack 7 | bridge decline 7)"
)
OWN_PATTERN = map_typed_text_to_pattern(OWN_TEXT)
SPINNER = f"{chr(0xB7)} Photosynthesizing{chr(0x2026)} (50s {chr(0xB7)} 4.2k tokens)"
QUOTED_SCREEN = (
    '[bridge] Peer message #9 from bridge, not from the user; use judgment: Tab "w" has been blocked for 2 min. '
    f"Screen: {SPINNER} {RULE * 60} {PROMPT} {RULE * 60} Opus 5.5 | 8% ctx (full text and answer: bridge show 9)"
)


class FakeClaudeBox:
    """Claude Code's input box per tab: typed keys add to it, Backspace takes one back, Enter submits."""

    def __init__(
        self,
        boxes: dict[str, str] | None = None,
        dropped_enters: int = 0,
        suggestions: set[str] | None = None,
        lagging_reads: int = 0,
        width: int | None = None,
    ) -> None:
        self.boxes = boxes or {}
        # Columns the box wraps its text at, under the two the prompt takes.
        self.width = width
        self.dropped_enters = dropped_enters
        # Grey text a key replaces instead of adding to.
        self.suggestions = suggestions or set()
        # Screen reads right after a probe key or its Backspace that still show the box as it was, as under load.
        self.lagging_reads = lagging_reads
        self.lag: dict[str, tuple[str, int]] = {}
        self.typed: list[str] = []
        self.submitted: list[str] = []

    def run(self, _: Config, args: list[str]) -> str | None:
        session = args[args.index("--target") + 1]
        text = self.boxes.get(session, "")
        if args[:2] == ["session", "text"]:
            if (lag := self.lag.get(session)) and lag[1] > 0:
                self.lag[session] = (lag[0], lag[1] - 1)
                text = lag[0]
            lines = text.split("\n")
            if self.width:
                lines = [part for line in lines for part in textwrap.wrap(line, self.width) or [""]]
            box = [PROMPT + NBSP + lines[0], *("  " + line for line in lines[1:])]
            return "\n".join([RULE, *box, RULE, "  -- INSERT --"])
        key = args[-1]
        self.typed.append(key)
        if self.lagging_reads and key in (".", "\x7f"):
            shown, left = self.lag.get(session, (text, 0))
            self.lag[session] = (shown if left else text, self.lagging_reads)
        if session in self.suggestions:
            self.suggestions.discard(session)
            text = ""
        if key == ENTER and self.dropped_enters:
            self.dropped_enters -= 1
        elif key == ENTER:
            self.submitted.append(text)
            self.boxes[session] = ""
        elif key == "\x7f":
            self.boxes[session] = text[:-1]
        else:
            self.boxes[session] = text + key
        return "ok"


class LateText(FakeClaudeBox):
    """Typed text shows in the box only seconds after the key went in, as on a heavily loaded machine."""

    def __init__(self, late_s: float, **kw: Any) -> None:
        super().__init__(**kw)
        self.late_s = late_s
        self.landing: dict[str, tuple[str, float]] = {}

    def run(self, _: Config, args: list[str]) -> str | None:
        session = args[args.index("--target") + 1]
        if (landing := self.landing.get(session)) and time.monotonic() >= landing[1]:
            del self.landing[session]
            self.boxes[session] = self.boxes.get(session, "") + landing[0]
        if args[:2] == ["session", "type"] and args[-1] not in (ENTER, ".", "\x7f"):
            self.typed.append(args[-1])
            self.landing[session] = (args[-1], time.monotonic() + self.late_s)
            return "ok"
        return super().run(_, args)


def _type(
    tmp_path: Path,
    fake: FakeClaudeBox,
    command: str = "hello",
    session: str = "S",
    wait_s: int = 5,
    still_wanted: Callable[[], bool] = lambda: True,
    **kw: object,
) -> str:
    with mock.patch("bridge.tabinput.run_agterm", fake.run), mock.patch("bridge.tabinput.state_dir", lambda: tmp_path):
        return type_when_idle(Config(), session, "P", command, still_wanted, wait_s=wait_s, **kw)  # type: ignore[arg-type]


def test_enter_goes_alone_once_the_text_shows_in_the_box(tmp_path: Path) -> None:
    fake = FakeClaudeBox()

    result = _type(tmp_path, fake)

    assert result == "typed"
    assert fake.typed == ["hello", ENTER]
    assert fake.submitted == ["hello"]


def test_a_dropped_enter_is_pressed_again_and_then_recorded_stuck_without_clearing(tmp_path: Path) -> None:
    fake = FakeClaudeBox(dropped_enters=99)

    result = _type(tmp_path, fake, wait_s=12)

    assert result == "stuck"
    assert fake.typed[0] == "hello" and set(fake.typed[1:]) == {ENTER}
    assert len(fake.typed) > SUBMIT_RETRIES + 2
    assert fake.boxes["S"] == "hello"


def test_text_that_stays_after_every_enter_of_a_round_gets_another_round_seconds_later(tmp_path: Path) -> None:
    fake = FakeClaudeBox(dropped_enters=SUBMIT_RETRIES + 1)

    result = _type(tmp_path, fake, wait_s=20)

    assert result == "typed"
    assert fake.typed == ["hello", *[ENTER] * (SUBMIT_RETRIES + 2)]
    assert fake.submitted == ["hello"]


def _after_the_first_round(fake: FakeClaudeBox, then: Callable[[], None]) -> None:
    """Runs then at the loop's first screen read after a full round of Enters."""
    run = fake.run
    reads_after = 0

    def watch(config: Config, args: list[str]) -> str | None:
        nonlocal reads_after
        if args[:2] == ["session", "text"] and fake.typed.count(ENTER) == SUBMIT_RETRIES + 1:
            reads_after += 1
            if reads_after == 2:
                then()
        return run(config, args)

    fake.run = watch  # type: ignore[method-assign]


def test_text_that_leaves_the_box_between_rounds_and_shows_in_the_transcript_counts_as_typed(tmp_path: Path) -> None:
    fake = FakeClaudeBox(dropped_enters=99)
    released: list[bool] = []
    run = fake.run
    landed = False

    def late_submit() -> None:
        nonlocal landed
        fake.boxes["S"], landed = "", True

    def with_transcript(config: Config, args: list[str]) -> str | None:
        screen = run(config, args)
        return f"{PROMPT} hello\n{screen}" if landed and args[:2] == ["session", "text"] else screen

    fake.run = with_transcript  # type: ignore[method-assign]
    _after_the_first_round(fake, late_submit)
    result = _type(tmp_path, fake, wait_s=20, release=lambda: released.append(True))

    assert result == "typed"
    assert fake.typed == ["hello", *[ENTER] * (SUBMIT_RETRIES + 1)]
    assert released == []


def test_text_that_leaves_the_box_unseen_is_stuck_and_never_typed_twice(tmp_path: Path) -> None:
    fake = FakeClaudeBox(dropped_enters=99)
    released: list[bool] = []
    _after_the_first_round(fake, lambda: fake.boxes.update(S=""))

    result = _type(tmp_path, fake, wait_s=20, release=lambda: released.append(True))

    assert result == "stuck"
    assert fake.typed == ["hello", *[ENTER] * (SUBMIT_RETRIES + 1)]
    assert released == [True]


def test_text_already_in_the_transcript_does_not_prove_a_late_submit(tmp_path: Path) -> None:
    fake = FakeClaudeBox(dropped_enters=99)
    run = fake.run

    def with_old_mention(config: Config, args: list[str]) -> str | None:
        screen = run(config, args)
        return f"{PROMPT} hello\n{screen}" if args[:2] == ["session", "text"] else screen

    fake.run = with_old_mention  # type: ignore[method-assign]
    _after_the_first_round(fake, lambda: fake.boxes.update(S=""))
    result = _type(tmp_path, fake, wait_s=20)

    assert result == "stuck"
    assert fake.typed == ["hello", *[ENTER] * (SUBMIT_RETRIES + 1)]


def test_text_the_user_shortened_after_a_dropped_enter_gets_no_more_enters(tmp_path: Path) -> None:
    fake = FakeClaudeBox(dropped_enters=99)
    _after_the_first_round(fake, lambda: fake.boxes.update(S=OWN_TEXT.replace(" use judgment:", "")))

    result = _type(tmp_path, fake, OWN_TEXT, wait_s=12, may_probe_box=lambda: True)

    assert result == "stuck"
    assert fake.typed == [OWN_TEXT, *[ENTER] * (SUBMIT_RETRIES + 1)]


def test_a_suggestion_after_text_left_the_box_unseen_gets_no_probe_and_no_second_typing(tmp_path: Path) -> None:
    fake = FakeClaudeBox(dropped_enters=99)
    _after_the_first_round(fake, lambda: fake.boxes.update(S="try the tests"))

    result = _type(tmp_path, fake, wait_s=12, may_probe_box=lambda: True)

    assert result == "stuck"
    assert fake.typed == ["hello", *[ENTER] * (SUBMIT_RETRIES + 1)]


def test_a_claim_held_across_rounds_is_released_when_the_message_goes_unwanted(tmp_path: Path) -> None:
    fake = FakeClaudeBox(dropped_enters=99)
    wanted = True
    released: list[bool] = []

    def no_longer() -> None:
        nonlocal wanted
        wanted = False

    _after_the_first_round(fake, no_longer)
    result = _type(tmp_path, fake, wait_s=20, still_wanted=lambda: wanted, release=lambda: released.append(True))

    assert result == "unwanted"
    assert released == [True]


def test_busy_reads_a_live_spinner_and_a_ruled_dialog_but_not_quotes_of_them() -> None:
    rule, spinner = RULE * 10, f"{chr(0xB7)} Photosynthesizing{chr(0x2026)} (50s {chr(0xB7)} 4.2k tokens)"
    empty_box = f"{rule}\n{PROMPT} \n{rule}"

    assert map_screen_to_busy(f"{spinner}\n{empty_box}")
    assert not map_screen_to_busy(f"  {spinner}\n{empty_box}")
    assert map_screen_to_busy(f"{rule}\n{PROMPT} 1. Yes\n  2. No\n  Enter to confirm {chr(0xB7)} Esc to cancel\n{rule}")
    assert not map_screen_to_busy(f"{rule}\n{PROMPT} [bridge] quoting: Esc to cancel\n{rule}")


def test_a_draft_under_an_indented_rule_is_neither_an_empty_box_nor_cut_at_it() -> None:
    rule = RULE * 10
    screen = f"{rule}\n{PROMPT}\n  {RULE}\n  my notes: Esc to cancel\n{rule}\n  -- INSERT --"

    assert not map_screen_to_idle(screen)
    assert map_screen_to_draft(screen) == f"{RULE} my notes: Esc to cancel"


def test_busy_reads_only_the_screens_newest_lines_once_submitted_prompts_leave_them() -> None:
    rule = RULE * 10
    prompt = "\n".join([f"{PROMPT} long prompt", *(f"  line {n}" for n in range(13))])
    screen = f"{chr(0x23FA)} Press Esc to cancel the dialog.\n{prompt}\n{chr(0x23FA)} Done.\n{rule}\n{PROMPT} \n{rule}"

    assert not map_screen_to_busy(screen)
    assert map_screen_to_idle(screen)


def test_a_submitted_prompt_that_starts_above_the_tail_still_quotes_its_markers() -> None:
    lines = [f"{PROMPT} submitted prompt", *["  wrapped text"] * 12, "  -- NORMAL --", f"{chr(0x23FA)} Done."]
    screen = "\n".join([*lines, RULE * 10, f"{PROMPT} ", RULE * 10])

    assert map_screen_to_idle(screen)


def test_text_quoting_a_dialog_is_submitted(tmp_path: Path) -> None:
    fake = FakeClaudeBox()
    quoting = f"[bridge] Peer message #9: Do you want to proceed? {PROMPT} 1. Yes 2. No Esc to cancel, Tab to amend"

    result = _type(tmp_path, fake, quoting)

    assert result == "typed"
    assert fake.typed == [quoting.replace(PROMPT, ">"), ENTER]


def test_text_quoting_a_screen_whose_rules_wrap_to_a_line_start_gets_its_enter(tmp_path: Path) -> None:
    fake = FakeClaudeBox(width=40)

    result = _type(tmp_path, fake, QUOTED_SCREEN)

    assert result == "typed"
    assert fake.typed[1:] == [ENTER]
    assert len(fake.submitted) == 1
    assert RULE[0] not in fake.submitted[0] and PROMPT not in fake.submitted[0]


def test_a_long_text_in_a_narrow_pane_whose_box_outgrows_the_screens_tail_gets_its_enter(tmp_path: Path) -> None:
    fake = FakeClaudeBox(width=50)
    long_text = OWN_TEXT.replace("look", "look " + "at the quoted words " * 30)

    result = _type(tmp_path, fake, long_text)

    assert result == "typed"
    assert fake.typed[1:] == [ENTER]
    assert len(fake.submitted) == 1


def test_a_line_the_user_adds_under_a_rule_of_their_own_gets_no_enter(tmp_path: Path) -> None:
    fake = FakeClaudeBox(dropped_enters=SUBMIT_RETRIES + 1)
    _after_the_first_round(fake, lambda: fake.boxes.update(S=f"hello\n{RULE}\nmy words"))

    result = _type(tmp_path, fake, wait_s=20)

    assert result == "stuck"
    assert fake.submitted == []
    assert fake.boxes["S"] == f"hello\n{RULE}\nmy words"


def test_a_mode_marker_quoted_in_a_submitted_prompt_does_not_read_as_busy(tmp_path: Path) -> None:
    fake = FakeClaudeBox()
    run = fake.run

    def with_quote_above(config: Config, args: list[str]) -> str | None:
        screen = run(config, args)
        if args[:2] == ["session", "text"]:
            quote = f"{PROMPT} [bridge] Peer message #8: its screen reads\n  -- NORMAL -- and Esc to cancel"
            return f"{quote}\n\n{chr(0x23FA)} Noted.\n\n{screen}"
        return screen

    fake.run = with_quote_above  # type: ignore[method-assign]
    result = _type(tmp_path, fake)

    assert result == "typed"
    assert fake.typed == ["hello", ENTER]


def _busy_for(fake: FakeClaudeBox, seconds: float) -> list[float]:
    """In a turn until the returned deadline, which the test may move."""
    run = fake.run
    idle_at = [time.monotonic() + seconds]

    def in_a_turn(config: Config, args: list[str]) -> str | None:
        screen = run(config, args)
        if args[:2] == ["session", "text"] and time.monotonic() < idle_at[0]:
            return f"{SPINNER}\n\n{screen}"
        return screen

    fake.run = in_a_turn  # type: ignore[method-assign]
    return idle_at


def test_text_typed_at_the_end_of_the_wait_gets_its_enter_rounds_past_it(tmp_path: Path) -> None:
    fake = FakeClaudeBox(dropped_enters=SUBMIT_RETRIES + 1)
    _busy_for(fake, 1)
    released: list[bool] = []

    result = _type(tmp_path, fake, wait_s=5, claim=lambda: True, release=lambda: released.append(True))

    assert result == "typed"
    assert fake.typed == ["hello", *[ENTER] * (SUBMIT_RETRIES + 2)]
    assert fake.submitted == ["hello"]
    assert released == []


def test_past_the_wait_a_turn_that_takes_the_box_ends_the_enter_rounds(tmp_path: Path) -> None:
    fake = FakeClaudeBox(dropped_enters=99)
    idle_at = _busy_for(fake, 1)
    _after_the_first_round(fake, lambda: idle_at.__setitem__(0, float("inf")))
    started = time.monotonic()

    result = _type(tmp_path, fake, wait_s=5)

    assert result == "stuck"
    assert time.monotonic() - started < ENTER_BUDGET_S / 2
    assert fake.typed == ["hello", *[ENTER] * (SUBMIT_RETRIES + 1)]
    assert fake.boxes["S"] == "hello"


def test_a_tab_in_a_turn_whose_spinner_names_no_esc_gets_no_keys(tmp_path: Path) -> None:
    fake = FakeClaudeBox()
    run = fake.run

    def in_a_turn(config: Config, args: list[str]) -> str | None:
        screen = run(config, args)
        if args[:2] == ["session", "text"]:
            return f"{chr(0xB7)} Photosynthesizing{chr(0x2026)} (50s {chr(0xB7)} 4.2k tokens)\n\n{screen}"
        return screen

    fake.run = in_a_turn  # type: ignore[method-assign]
    result = _type(tmp_path, fake, wait_s=3)

    assert result == "timeout"
    assert fake.typed == []


def test_its_own_unsent_text_from_an_earlier_try_gets_only_the_enter(tmp_path: Path) -> None:
    fake = FakeClaudeBox({"S": OWN_TEXT})
    claims: list[bool] = []

    def claim() -> bool:
        claims.append(True)
        return True

    # The retry's wording differs: a request's time left has moved on.
    retry_text = OWN_TEXT.replace("5m", "1m overdue")
    result = _type(tmp_path, fake, retry_text, may_probe_box=lambda: True, claim=claim, own_pattern=OWN_PATTERN)

    assert result == "typed"
    assert fake.typed == [ENTER]
    assert fake.submitted == [OWN_TEXT]
    assert claims == [True]


def test_its_own_text_an_older_typer_left_with_a_screens_glyphs_gets_only_the_enter(tmp_path: Path) -> None:
    left = OWN_TEXT.replace("look", f"look {RULE} {PROMPT} {RULE}")
    fake = FakeClaudeBox({"S": left})

    result = _type(tmp_path, fake, left, claim=lambda: True)

    assert result == "typed"
    assert fake.typed == [ENTER]
    assert fake.submitted == [left]


def test_its_own_text_with_someone_elses_typing_after_it_gets_no_enter(tmp_path: Path) -> None:
    fake = FakeClaudeBox({"S": OWN_TEXT + " and sam's words"})

    result = _type(
        tmp_path, fake, OWN_TEXT, wait_s=8, may_probe_box=lambda: True, own_pattern=OWN_PATTERN, **_left_unsent([])
    )

    assert result == "draft"
    assert fake.typed == []


def test_its_own_text_with_an_edit_in_the_middle_gets_no_enter(tmp_path: Path) -> None:
    fake = FakeClaudeBox({"S": OWN_TEXT.replace("look", "look, and my own note")})

    result = _type(tmp_path, fake, OWN_TEXT, wait_s=3, own_pattern=OWN_PATTERN)

    assert result == "draft"
    assert fake.typed == []


def test_no_enter_goes_into_a_dialog_that_opened_after_the_text(tmp_path: Path) -> None:
    fake = FakeClaudeBox()
    run = fake.run

    def with_dialog(config: Config, args: list[str]) -> str | None:
        screen = run(config, args)
        if args[:2] == ["session", "text"] and "hello" in fake.boxes.get("S", ""):
            return f"{screen}\n  Enter to confirm"
        return screen

    fake.run = with_dialog  # type: ignore[method-assign]
    result = _type(tmp_path, fake)

    assert result == "stuck"
    assert fake.typed == ["hello"]


def test_text_held_by_a_dialog_gets_its_enter_once_the_dialog_closes(tmp_path: Path) -> None:
    fake = FakeClaudeBox()
    run = fake.run
    closes_at = time.monotonic() + 4

    def dialog_for_a_while(config: Config, args: list[str]) -> str | None:
        screen = run(config, args)
        if args[:2] == ["session", "text"] and fake.boxes.get("S") and time.monotonic() < closes_at:
            return f"{screen}\n  Esc to cancel"
        return screen

    fake.run = dialog_for_a_while  # type: ignore[method-assign]
    result = _type(tmp_path, fake, wait_s=20)

    assert result == "typed"
    assert fake.typed == ["hello", ENTER]


def test_text_typed_after_a_dropped_enter_is_not_counted_as_submitted(tmp_path: Path) -> None:
    fake = FakeClaudeBox(dropped_enters=1)
    run = fake.run

    def user_types_after_the_enter(config: Config, args: list[str]) -> str | None:
        if args[:2] == ["session", "type"] and args[-1] == ENTER:
            out = run(config, args)
            fake.boxes["S"] += " and more"
            return out
        return run(config, args)

    fake.run = user_types_after_the_enter  # type: ignore[method-assign]
    result = _type(tmp_path, fake)

    assert result == "stuck"
    assert fake.typed == ["hello", ENTER]
    assert fake.boxes["S"] == "hello and more"


def test_no_probe_key_once_the_user_comes_to_the_tab_during_the_wait_for_the_lock(tmp_path: Path) -> None:
    fake = FakeClaudeBox({"S": "park it"})
    answers = iter([True])

    result = _type(tmp_path, fake, wait_s=3, may_probe_box=lambda: next(answers, False))

    assert fake.typed == []
    assert result == "unproven"


OTHER_TEXT = OWN_TEXT.replace("#7", "#6").replace(" 7", " 6") + "\n...."


def _left_unsent(settled: list[bool]) -> dict[str, Any]:
    def claim_left_unsent(box: str) -> Callable[[bool], None] | None:
        return settled.append if "".join(box.split()) == "".join(OTHER_TEXT.split()) else None

    return {"claim_left_unsent": claim_left_unsent}


def test_text_no_probe_could_check_in_a_selected_tab_is_unproven_and_gets_no_keys(tmp_path: Path) -> None:
    fake = FakeClaudeBox({"S": "Ok, starting review 45"})

    result = _type(tmp_path, fake, wait_s=3, may_probe_box=lambda: False)

    assert result == "unproven"
    assert fake.typed == []


def test_a_ruled_dialog_holding_the_box_is_a_timeout_not_unproven_text(tmp_path: Path) -> None:
    fake = FakeClaudeBox({"S": "1. Yes\n2. No\nEnter to confirm"})

    result = _type(tmp_path, fake, wait_s=3, may_probe_box=lambda: True)

    assert result == "timeout"
    assert fake.typed == []


def test_another_messages_unsent_text_gets_no_probe_key(tmp_path: Path) -> None:
    fake = FakeClaudeBox({"S": OTHER_TEXT})

    result = _type(tmp_path, fake, OWN_TEXT, wait_s=3, may_probe_box=lambda: True, own_pattern=OWN_PATTERN)

    assert result == "draft"
    assert fake.typed == []
    assert fake.boxes["S"] == OTHER_TEXT


def test_another_messages_text_left_unsent_gets_enter_alone_and_then_this_one_is_typed(tmp_path: Path) -> None:
    fake = FakeClaudeBox({"S": OTHER_TEXT})
    recorded: list[bool] = []
    left = _left_unsent(recorded)

    result = _type(tmp_path, fake, OWN_TEXT, wait_s=20, may_probe_box=lambda: True, own_pattern=OWN_PATTERN, **left)

    assert result == "typed"
    assert fake.typed == [ENTER, OWN_TEXT, ENTER]
    assert fake.submitted == [OTHER_TEXT, OWN_TEXT]
    assert recorded == [True]


def test_another_messages_text_in_a_selected_tab_gets_no_enter(tmp_path: Path) -> None:
    fake = FakeClaudeBox({"S": OTHER_TEXT})
    recorded: list[bool] = []
    left = _left_unsent(recorded)

    result = _type(tmp_path, fake, OWN_TEXT, wait_s=8, may_probe_box=lambda: False, own_pattern=OWN_PATTERN, **left)

    assert result == "draft"
    assert fake.typed == []


def test_another_messages_text_that_keeps_changing_gets_no_enter(tmp_path: Path) -> None:
    fake = FakeClaudeBox({"S": OTHER_TEXT})
    recorded: list[bool] = []
    left = _left_unsent(recorded)
    run = fake.run

    def still_being_typed(config: Config, args: list[str]) -> str | None:
        if args[:2] == ["session", "text"]:
            fake.boxes["S"] += "x"
        return run(config, args)

    fake.run = still_being_typed  # type: ignore[method-assign]
    result = _type(tmp_path, fake, OWN_TEXT, wait_s=8, may_probe_box=lambda: True, own_pattern=OWN_PATTERN, **left)

    assert result == "draft"
    assert fake.typed == []


def test_another_messages_text_whose_enter_does_not_take_gets_it_once(tmp_path: Path) -> None:
    fake = FakeClaudeBox({"S": OTHER_TEXT}, dropped_enters=99)
    recorded: list[bool] = []
    left = _left_unsent(recorded)

    result = _type(tmp_path, fake, OWN_TEXT, wait_s=12, may_probe_box=lambda: True, own_pattern=OWN_PATTERN, **left)

    assert result == "draft"
    assert fake.typed == [ENTER] * (SUBMIT_RETRIES + 1)
    assert fake.boxes["S"] == OTHER_TEXT
    assert recorded == [False]


def test_no_more_enters_on_another_messages_text_once_the_user_comes_to_the_tab(tmp_path: Path) -> None:
    fake = FakeClaudeBox({"S": OTHER_TEXT}, dropped_enters=99)
    recorded: list[bool] = []
    left = _left_unsent(recorded)
    selected = False

    def may_probe_box() -> bool:
        return not selected

    run = fake.run

    def user_comes_after_the_enter(config: Config, args: list[str]) -> str | None:
        nonlocal selected
        if args[:2] == ["session", "type"] and args[-1] == ENTER:
            selected = True
        return run(config, args)

    fake.run = user_comes_after_the_enter  # type: ignore[method-assign]
    result = _type(tmp_path, fake, OWN_TEXT, wait_s=10, may_probe_box=may_probe_box, own_pattern=OWN_PATTERN, **left)

    assert result == "draft"
    assert fake.typed == [ENTER]
    assert recorded == [False]


def test_the_probe_key_is_taken_back_when_it_lands_on_a_line_of_its_own(tmp_path: Path) -> None:
    fake = FakeClaudeBox({"S": "park it\n"})

    result = _type(tmp_path, fake, wait_s=3, may_probe_box=lambda: True)

    assert result == "draft"
    assert fake.typed == [".", "\x7f"]
    assert fake.boxes["S"] == "park it\n"


def test_a_probe_key_the_screen_shows_late_is_still_taken_back(tmp_path: Path) -> None:
    fake = FakeClaudeBox({"S": "Ok, starting review 45"}, suggestions={"S"}, lagging_reads=2)

    result = _type(tmp_path, fake, wait_s=8, may_probe_box=lambda: True)

    assert fake.typed[:2] == [".", "\x7f"]
    assert result == "typed"
    assert fake.submitted == ["hello"]


def test_a_probe_key_that_never_shows_gets_no_other_key(tmp_path: Path) -> None:
    fake = FakeClaudeBox({"S": "Ok, starting review 45"}, suggestions={"S"}, lagging_reads=99)

    result = _type(tmp_path, fake, wait_s=8, may_probe_box=lambda: True)

    assert result == "failed"
    assert fake.typed == ["."]


def test_a_box_that_changed_beyond_the_probe_key_is_left_as_it_is(tmp_path: Path) -> None:
    class UserTypesToo(FakeClaudeBox):
        def run(self, _: Config, args: list[str]) -> str | None:
            result = super().run(_, args)
            if args[-1] == ".":
                self.boxes["S"] += "x"
            return result

    fake = UserTypesToo({"S": "park it"})

    result = _type(tmp_path, fake, wait_s=3, may_probe_box=lambda: True)

    assert result == "failed"
    assert fake.typed == ["."]
    assert fake.boxes["S"] == "park it.x"


def test_a_box_holding_just_the_probe_key_keeps_it_when_the_probe_never_lands(tmp_path: Path) -> None:
    class DropsTheProbe(FakeClaudeBox):
        def run(self, _: Config, args: list[str]) -> str | None:
            if args[-1] == ".":
                self.typed.append(".")
                return "ok"
            return super().run(_, args)

    fake = DropsTheProbe({"S": "."})

    result = _type(tmp_path, fake, wait_s=8, may_probe_box=lambda: True)

    assert result == "failed"
    assert fake.typed == ["."]
    assert fake.boxes["S"] == "."


def test_a_box_that_changes_after_the_probe_is_taken_back_gets_nothing_typed(tmp_path: Path) -> None:
    class UserTypesAfterTheBackspace(FakeClaudeBox):
        def run(self, _: Config, args: list[str]) -> str | None:
            result = super().run(_, args)
            if args[-1] == "\x7f":
                self.boxes["S"] = "hint."
            elif self.boxes.get("S") == "hint." and args[:2] == ["session", "text"]:
                self.boxes["S"] = "hint"
            return result

    fake = UserTypesAfterTheBackspace({"S": "hint"}, suggestions={"S"})

    result = _type(tmp_path, fake, wait_s=8, may_probe_box=lambda: True)

    assert result == "failed"
    assert fake.typed == [".", "\x7f"]


def test_text_that_shows_in_the_box_seconds_late_keeps_its_claim_and_gets_its_enter(tmp_path: Path) -> None:
    fake = LateText(6)
    released: list[bool] = []

    result = _type(tmp_path, fake, wait_s=40, claim=lambda: True, release=lambda: released.append(True))

    assert result == "typed"
    assert fake.submitted == ["hello"]
    assert released == []


def test_text_that_never_shows_is_released_after_the_longer_watch(tmp_path: Path) -> None:
    fake = LateText(999)
    released: list[bool] = []

    with mock.patch("bridge.tabinput.TEXT_LATE_S", 7.0):
        result = _type(tmp_path, fake, wait_s=40, claim=lambda: True, release=lambda: released.append(True))

    assert result == "failed"
    assert fake.typed == ["hello"]
    assert released == [True]


def test_a_long_wait_on_one_tab_does_not_hold_back_typing_into_the_same_pane(tmp_path: Path) -> None:
    fake = FakeClaudeBox({"WAITS": "a draft"})
    waiter = threading.Thread(target=_type, args=(tmp_path, fake, "/compact", "WAITS", 6))
    waiter.start()
    time.sleep(0.5)
    started = time.monotonic()

    result = _type(tmp_path, fake, "hello", "IDLE")
    took = time.monotonic() - started
    waiter.join()

    assert took < 5, "typing waited for the other wait to end"
    assert result == "typed"


def _overdue_message_to(bridge_env: BridgeEnv, handle: str, pid: int, typed: bool = True) -> int:
    sent = bridge_env.cli("send", handle, "please look", pid=pid)
    assert sent.returncode == 0, sent.stderr
    with bridge_env.connect() as conn:
        message_id = int(conn.execute("SELECT max(id) FROM messages").fetchone()[0])
        # Let the spawned delivery finish before the row is put back to undelivered.
        assert not typed or wait_for(
            lambda: (
                conn.execute(
                    "SELECT 1 FROM message_recipients WHERE message_id = ?"
                    " AND offered_at IS NOT NULL AND typing_started_at IS NULL",
                    (message_id,),
                ).fetchone()
                is not None
            ),
            15,
        )
        conn.execute(
            "UPDATE message_recipients SET offered_at = NULL, acked_at = NULL WHERE message_id = ?", (message_id,)
        )
        conn.execute("UPDATE messages SET created_at = 1000 WHERE id = ?", (message_id,))
        if not typed:
            conn.execute("UPDATE message_recipients SET typing_wanted_at = 1000 WHERE message_id = ?", (message_id,))
    return message_id


def _overdue_board_post(bridge_env: BridgeEnv, pid: int, text: str) -> int:
    posted = bridge_env.cli("board", "--type", "report", text, pid=pid)
    assert posted.returncode == 0, posted.stderr
    with bridge_env.connect() as conn:
        message_id = int(conn.execute("SELECT max(id) FROM messages").fetchone()[0])
        conn.execute("UPDATE messages SET created_at = 1000 WHERE id = ?", (message_id,))
    return message_id


def test_a_board_post_that_waits_for_the_next_hook_by_design_raises_no_alert(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    team.enroll("Boris", "old-b")
    config = Config(assistant_handle="nobody")
    quiet = _overdue_board_post(bridge_env, team.b, "status: the deploy is green")
    with bridge_env.connect() as conn:
        reached = conn.execute("SELECT handle FROM message_recipients WHERE message_id = ?", (quiet,)).fetchall()
        not_typed = take_new_undelivered_tabs(conn, config, 1000 + 11 * 60_000)
    mention = _overdue_board_post(bridge_env, team.b, "@alex the deploy is green")
    with bridge_env.connect() as conn:
        assert wait_for(
            lambda: (
                conn.execute(
                    "SELECT 1 FROM message_recipients WHERE message_id = ? AND typing_wanted_at IS NOT NULL",
                    (mention,),
                ).fetchone()
                is not None
            ),
            15,
        )
        conn.execute(
            "UPDATE message_recipients SET offered_at = NULL, acked_at = NULL WHERE message_id = ?", (mention,)
        )
        typed = take_new_undelivered_tabs(conn, config, 1000 + 11 * 60_000)

    assert [r["handle"] for r in reached] == ["alex"]
    assert not_typed == []
    assert [(t.handle, t.oldest_message_id) for t in typed] == [("alex", mention)]


def test_a_lead_tab_with_mail_overdue_tells_the_assistant_once_per_episode_with_the_reason(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    config = Config(assistant_handle="old-b")
    message_id = _overdue_message_to(bridge_env, "alex", team.b)
    screen = f"{RULE}\n{PROMPT} [bridge] Peer message #{message_id} from old-b: please look\n{RULE}"
    at = 1000 + 11 * 60_000
    with bridge_env.connect() as conn:
        early = take_new_undelivered_tabs(conn, config, 1000 + 9 * 60_000)
        tabs = take_new_undelivered_tabs(conn, config, at)
        told = tell_of_undelivered_tab(conn, config, tabs[0], screen, at)
        again = take_new_undelivered_tabs(conn, config, at + 60_000)
        body = conn.execute("SELECT body, post_type FROM messages WHERE id = ?", (told[0] if told else 0,)).fetchone()
        conn.execute("UPDATE message_recipients SET offered_at = 5 WHERE message_id = ?", (message_id,))
        over = take_new_undelivered_tabs(conn, config, at + 120_000)
        conn.execute("UPDATE message_recipients SET offered_at = NULL WHERE message_id = ?", (message_id,))
        conn.execute("UPDATE conversations SET compact_phase = 'compacting' WHERE conversation_id = 'conv-a'")
        next_episode = take_new_undelivered_tabs(conn, config, at + 180_000)
        compacting = tell_of_undelivered_tab(conn, config, next_episode[0], None, at + 180_000)
        compacting_body = conn.execute("SELECT body FROM messages WHERE id = ?", (compacting[0] if compacting else 0,))

        assert early == []
        assert [t.handle for t in tabs] == ["alex"]
        assert told is not None and told[1].handle == "old-b"
        assert body["post_type"] == "to-assistant"
        assert f"not received #{message_id} for 11 min" in body["body"]
        assert f"bridge's own text of #{message_id}, typed but not submitted: press Enter there" in body["body"]
        assert again == []
        assert over == []
        assert [t.handle for t in next_episode] == ["alex"]
        assert "It is compacting." in compacting_body.fetchone()[0]


def test_the_assistants_own_tab_with_mail_overdue_shows_on_the_page(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    config_file = Path(bridge_env.env["BRIDGE_CONFIG"])
    config_file.write_text(config_file.read_text() + 'assistant_handle = "old-b"\n')
    team = Team(bridge_env, spawn_process)
    config = Config(assistant_handle="old-b")
    message_id = _overdue_message_to(bridge_env, "old-b", team.a, typed=False)
    with bridge_env.connect() as conn:
        tabs = take_new_undelivered_tabs(conn, config, 1000 + 11 * 60_000)
        told = tell_of_undelivered_tab(conn, config, tabs[0], None, 1000 + 11 * 60_000)
        to_assistant = conn.execute("SELECT count(*) FROM messages WHERE from_handle = 'bridge'").fetchone()[0]
    port = free_port()
    server = subprocess.Popen([str(VENV_BIN / "bridge"), "serve", "--port", str(port)], env=bridge_env.env)
    try:
        wait_up(f"http://127.0.0.1:{port}")
        needs = json.loads(http_request(f"http://127.0.0.1:{port}/api/guild"))["needs"]
    finally:
        server.terminate()
        server.wait()

    assert told is None and to_assistant == 0
    assert [n["type"] for n in needs if n.get("lead") == "old-b"] == ["undelivered"]
    assert any(f"not received #{message_id}" in str(n["text"]) for n in needs)


def test_any_message_to_a_lead_that_could_not_be_typed_is_retried(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    message_id = _overdue_message_to(bridge_env, "alex", team.b)
    with bridge_env.connect() as conn:
        conn.execute("UPDATE messages SET post_type = 'report' WHERE id = ?", (message_id,))
        record_failed_delivery(conn, message_id, "conv-a", 1000)
        retries = take_delivery_retries(conn, 1000 + 700_000)

    assert [(m, r.handle) for m, r in retries] == [(message_id, "alex")]


def test_an_alert_episode_survives_a_typing_claim_and_waits_for_a_reachable_assistant(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    message_id = _overdue_message_to(bridge_env, "alex", team.b)
    at = 1000 + 11 * 60_000
    with bridge_env.connect() as conn:
        tabs = take_new_undelivered_tabs(conn, Config(assistant_handle="old-b"), at)
        away = tell_of_undelivered_tab(conn, Config(assistant_handle="nobody"), tabs[0], None, at)
        retried = take_new_undelivered_tabs(conn, Config(assistant_handle="old-b"), at + 30_000)
        told = tell_of_undelivered_tab(conn, Config(assistant_handle="old-b"), retried[0], None, at + 30_000)
        conn.execute(
            "UPDATE message_recipients SET offered_at = 5, acked_at = 5, typing_started_at = 5 WHERE message_id = ?",
            (message_id,),
        )
        while_typing = take_new_undelivered_tabs(conn, Config(assistant_handle="old-b"), at + 60_000)
        conn.execute(
            "UPDATE message_recipients SET offered_at = NULL, acked_at = NULL, typing_started_at = NULL"
            " WHERE message_id = ?",
            (message_id,),
        )
        after_release = take_new_undelivered_tabs(conn, Config(assistant_handle="old-b"), at + 90_000)
        alerts = conn.execute("SELECT count(*) FROM messages WHERE from_handle = 'bridge'").fetchone()[0]

    assert away is None
    assert [t.handle for t in retried] == ["alex"]
    assert told is not None
    assert while_typing == [] and after_release == []
    assert alerts == 1


def test_mail_that_arrives_while_the_screen_is_read_raises_no_alert(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    message_id = _overdue_message_to(bridge_env, "alex", team.b)
    config = Config(assistant_handle="old-b")
    at = 1000 + 11 * 60_000
    with bridge_env.connect() as conn:
        tabs = take_new_undelivered_tabs(conn, config, at)
        conn.execute("UPDATE message_recipients SET offered_at = 5 WHERE message_id = ?", (message_id,))
        told = tell_of_undelivered_tab(conn, config, tabs[0], None, at)

    assert told is None


def test_only_another_messages_unedited_text_is_left_unsent_and_its_claim_closes_or_restores_the_row(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    message_id = _overdue_message_to(bridge_env, "alex", team.b)
    seen: dict[str, Any] = {}

    def capture(*args: Any, **kw: Any) -> str:
        seen.update(kw, text=args[3])
        return "unwanted"

    def row(conn: sqlite3.Connection) -> tuple[object, ...]:
        return tuple(
            conn.execute(
                "SELECT offered_at, acked_at, typing_started_at, stuck_at FROM message_recipients WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        )

    alerts = Config(assistant_handle="nobody")
    with bridge_env.connect() as conn:
        with mock.patch("bridge.feed.type_when_idle", capture):
            deliver_typed(conn, Config(agtermctl=str(bridge_env.root / "agtermctl")), message_id, "conv-a")
        text = str(seen["text"])
        verdicts = [
            find_unsent_message(conn, "conv-a", box) == message_id
            for box in (
                text.replace(", not", ",\n  not") + "\n....",
                text + " and my words",
                text.replace("please", "please do not"),
                text.replace(f"#{message_id}", f"#{message_id + 1}"),
            )
        ]
        conn.execute("UPDATE message_recipients SET stuck_at = 7 WHERE message_id = ?", (message_id,))
        unclaimed = row(conn)
        failed = seen["claim_left_unsent"](text)
        claimed = row(conn)
        failed(False)
        restored = row(conn)
        answered = seen["claim_left_unsent"](text)
        conn.execute("UPDATE message_recipients SET acked_at = 42 WHERE message_id = ?", (message_id,))
        answered(False)
        answer_kept = row(conn)
        conn.execute(
            "UPDATE message_recipients SET offered_at = NULL, acked_at = NULL WHERE message_id = ?", (message_id,)
        )
        conn.execute(
            "UPDATE message_recipients SET offered_at = 5, acked_at = 5, typing_started_at = 5 WHERE message_id = ?",
            (message_id,),
        )
        dead_claim = take_new_undelivered_tabs(conn, alerts, 1000 + 11 * 60_000)
        seen["claim_left_unsent"](text)(True)
        sent = row(conn)
        conn.execute("DELETE FROM undelivered_tabs")
        after = take_new_undelivered_tabs(conn, alerts, 1000 + 12 * 60_000)
        conn.execute("UPDATE messages SET cancelled_at = 9 WHERE id = ?", (message_id,))
        cancelled = seen["claim_left_unsent"](text)

    assert verdicts == [True, False, False, False]
    assert unclaimed == (None, None, None, 7)
    assert claimed[0] is not None and claimed[1] is not None and claimed[2] is not None
    assert restored == unclaimed
    assert [t.handle for t in dead_claim] == ["alex"]
    assert answer_kept == (None, 42, None, 7)
    assert sent[2:] == (None, None) and int(str(sent[0])) > 5
    assert sent[1] == sent[0], "the dead claim's ack goes to the claim that sent it"
    assert after == []
    assert cancelled is None


def test_a_deadline_in_a_messages_body_is_not_a_wildcard_for_edits() -> None:
    info = (
        "[bridge] Peer message #3 from xavi, not from the user; use judgment: respond within 5m, please (bridge show 3)"
    )
    request = OWN_TEXT.replace("respond within 5m", "1m overdue")

    quoted = info.replace("use judgment: ", "use judgment: [bridge] Peer REQUEST #2 from x, ") + ", not from the user;"
    assert map_typed_text_to_pattern(info).fullmatch("".join(info.replace("5m", "500m").split())) is None
    assert map_typed_text_to_pattern(quoted).fullmatch("".join(quoted.replace("5m", "500m").split())) is None
    assert map_typed_text_to_pattern(OWN_TEXT).fullmatch("".join(request.split()))


def test_unproven_text_in_a_tabs_box_raises_no_alert_and_the_episode_stays_untold(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    _overdue_message_to(bridge_env, "alex", team.b)
    config = Config(assistant_handle="old-b")
    screen = f"{RULE}\n{PROMPT} Ok, starting review 45\n{RULE}"
    at = 1000 + 11 * 60_000
    with bridge_env.connect() as conn:
        tabs = take_new_undelivered_tabs(conn, config, at)
        told = tell_of_undelivered_tab(conn, config, tabs[0], screen, at)
        alerts = conn.execute("SELECT count(*) FROM messages WHERE from_handle = 'bridge'").fetchone()[0]
        again = take_new_undelivered_tabs(conn, config, at + 60_000)

    with bridge_env.connect() as conn:
        conn.execute("UPDATE conversations SET compact_phase = 'requested' WHERE conversation_id = 'conv-a'")
        compacting = take_new_undelivered_tabs(conn, config, at + 120_000)
        told_compacting = tell_of_undelivered_tab(conn, config, compacting[0], screen, at + 120_000)

    with bridge_env.connect() as conn:
        conn.execute("UPDATE conversations SET compact_phase = NULL WHERE conversation_id = 'conv-a'")
        conn.execute("UPDATE message_recipients SET draft_told_at = 5 WHERE conversation_id = 'conv-a'")
        proven = take_new_undelivered_tabs(conn, config, at + 180_000)
        told_proven = tell_of_undelivered_tab(conn, config, proven[0], screen, at + 180_000)
        body = conn.execute("SELECT body FROM messages WHERE id = ?", (told_proven[0] if told_proven else 0,))
        conn.execute("DELETE FROM undelivered_tabs")
        conn.execute("UPDATE message_recipients SET unproven_at = 9 WHERE conversation_id = 'conv-a'")
        later = take_new_undelivered_tabs(conn, config, at + 240_000)
        told_later = tell_of_undelivered_tab(conn, config, later[0], screen, at + 240_000)

    assert told_compacting is None
    assert told_proven is not None
    assert told_later is None
    assert "holds a draft ('Ok, starting review 45')" in body.fetchone()[0]
    assert told is None and alerts == 0
    assert [t.handle for t in again] == ["alex"]


def test_a_users_message_held_only_by_unproven_text_shows_no_notice_on_the_page(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    message_id = _overdue_message_to(bridge_env, "alex", team.b)
    with bridge_env.connect() as conn:
        conn.execute("UPDATE messages SET from_conversation = 'user' WHERE id = ?", (message_id,))
        record_failed_delivery(conn, message_id, "conv-a", 2000, unproven=True)
        quiet = _load_undelivered_to_needs(conn, Config())
        retries = take_delivery_retries(conn, 2000 + 700_000)
        record_failed_delivery(conn, message_id, "conv-a", 3000)
        shown = _load_undelivered_to_needs(conn, Config())

    assert quiet == []
    assert [m for m, _ in retries] == [message_id]
    assert [n["lead"] for n in shown] == ["alex"]


def test_a_draft_proven_in_the_assistants_own_tab_is_marked_without_a_message_to_itself(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    config_file = Path(bridge_env.env["BRIDGE_CONFIG"])
    config_file.write_text(config_file.read_text() + 'assistant_handle = "old-b"\n')
    team = Team(bridge_env, spawn_process)
    message_id = _overdue_message_to(bridge_env, "old-b", team.a, typed=False)
    with bridge_env.connect() as conn:
        told = tell_assistant_of_draft(conn, Config(assistant_handle="old-b"), message_id, "conv-b", 2000)
        marked = conn.execute(
            "SELECT draft_told_at FROM message_recipients WHERE message_id = ?", (message_id,)
        ).fetchone()[0]

    assert told is None
    assert marked == 2000
