import fcntl
import json
import os
import re
import subprocess
import threading
import time
import unicodedata
from collections.abc import Callable, Generator
from contextlib import contextmanager
from itertools import takewhile
from pathlib import Path
from typing import Literal

from bridge.compaction import (
    map_screen_to_box_lines,
    map_screen_to_busy,
    map_screen_to_draft,
    map_screen_to_idle,
    map_screen_to_transcript,
    map_screen_to_vim_insert,
)
from bridge.config import Config, state_dir
from bridge.errlog import log_error, log_slow

TYPE_WAIT_S = 600
SUBMIT_RETRIES = 3
ENTER = "\r"
PROBE_KEY = "."
BACKSPACE = "\x7f"
KILL_TO_END = "\x0b"
KILL_TO_START = "\x15"


def map_lines_to_clear_keys(lines: int) -> str:
    """Ctrl-K takes the rest of the line, or at its end the break after it; Ctrl-U the line up to the cursor, or at its
    start the break before it. Twice per line each empties a box of that many lines in Claude Code 2.1.328's default
    mode and vim INSERT and NORMAL, wherever the cursor is; no Escape, which interrupts a turn, and no arrow, which
    recalls history or opens a panel."""
    return KILL_TO_END * 2 * lines + KILL_TO_START * 2 * lines


CLEAR_KEYS = map_lines_to_clear_keys(1)
STRAY_MAX_CHARS = 3
BOX_QUOTE_CHARS = 80
TYPED_HEADS = ("/compact", "[bridge]")
"""What bridge types first into a box: a box holding only a start of one is a try cut short."""
_KEY_RESIDUE_RE = re.compile(r"(?:\^\[|\x1b)*\[[0-9;]*[A-DFH~]|(?:\^\[|\x1b)+(?:O[A-DFH])?")
"""What an escape sequence leaves when the box takes it as text, such as "^[[A" or "[200~"."""
_STRAY_CATEGORIES = frozenset({"Pc", "Pd", "Ps", "Pe", "Pi", "Pf", "Po", "Sm", "Sc", "Sk", "Cc", "Cf"})
PROBE_SETTLE_S = 0.5
PROBE_LANDS_S = 5.0
TEXT_LANDS_S = 5.0
TEXT_LATE_S = 30.0
LOOK_EVERY_S = 0.3
PASTE_EDGE_CHARS = 40
BRIDGE_TEXT_PREFIX = "[bridge]"
PROMPT_GLYPH = "\u276f"
_BOX_RULE_RE = re.compile("[\u2500-\u257f]+")
_PASTE_CHIP_RE = re.compile(r"\[Pasted text #\d+(?: \+\d+ lines?)?\]")
CHIP_AT_CHARS = 801
"""Claude Code 2.1.322 folds an insert of this many characters (ASCII; one agterm injection) into a paste chip."""
ORPHAN_STEADY_S = 4.0
STUCK_ENTER_ROUNDS = 10
ENTER_BUDGET_S = 60.0
SLEEP_GAP_S = 30.0
AGTERM_TIMEOUT_S = 10
AGTERM_SLOW_S = 3.0
AGTERM_LOG_EVERY_S = 30.0

TypeResult = Literal["typed", "stuck", "unwanted", "failed", "timeout", "draft", "unproven"]
SubmitResult = Literal["sent", "pressed", "untouched"]
BoxCall = Literal["clear", "keep", "ask"]


def judge_box(box: str) -> BoxCall:
    """Text in a tab's input box: stray keys bridge may clear, someone's words it keeps, or neither, which a person
    decides. A word in any script is kept unless it only starts a command bridge types."""
    text = "".join(box.split())
    if len(text) >= 2 and any(head.startswith(text) for head in TYPED_HEADS):
        return "clear"
    if _KEY_RESIDUE_RE.fullmatch(text):
        return "clear"
    if any(ch.isalpha() for ch in text):
        return "keep"
    if len(text) <= STRAY_MAX_CHARS and all(unicodedata.category(ch) in _STRAY_CATEGORIES for ch in text):
        return "clear"
    return "ask"


def map_box_to_quote(box: str) -> str:
    return json.dumps(box if len(box) <= BOX_QUOTE_CHARS else box[:BOX_QUOTE_CHARS] + "…", ensure_ascii=False)


def is_claude_vim() -> bool:
    """Claude Code's editorMode: settings.json's when it sets one, else the global config's."""
    home = os.environ.get("CLAUDE_CONFIG_DIR")
    for path in (Path(home or Path.home() / ".claude") / "settings.json", Path(home or Path.home()) / ".claude.json"):
        try:
            mode = json.loads(path.read_text()).get("editorMode")
        except (OSError, ValueError, AttributeError):
            continue
        if mode is not None:
            return mode == "vim"
    return False


def type_when_idle(
    config: Config,
    session: str,
    pane: str,
    command: str,
    still_wanted: Callable[[], bool],
    wait_s: int = TYPE_WAIT_S,
    claim: Callable[[], bool] | None = None,
    release: Callable[[], None] | None = None,
    may_probe_box: Callable[[], bool] | None = None,
    own_pattern: re.Pattern[str] | None = None,
    claim_left_unsent: Callable[[str], Callable[[bool], None] | None] | None = None,
    may_type: Callable[[], bool] | None = None,
    tell_unsure: Callable[[str], None] | None = None,
) -> TypeResult:
    """Types one line plus Enter into a Claude Code tab once its input box is empty and idle.

    Text typed here that stays in the box gets Enter again every few seconds while the screen allows it, up to
    STUCK_ENTER_ROUNDS rounds or ENTER_BUDGET_S after it went in, even past the end of the wait, for as long as the
    box holds that text alone and no turn or dialog holds it. "stuck" means it stayed after all of them, or never
    showed there alone; a later call finding this same text in the box presses only Enter.
    "unwanted" means still_wanted turned False first; "draft" that the wait ran out with text in the box that is
    bridge's or that a probe proved real, which is cleared only as stray keys: it may be the user's. "unproven" is
    text no probe could check, such as Claude Code's grey suggestion in a selected tab: nobody hears of it, nothing is
    typed over it.
    claim runs right before typing and must win for the text to go in; release undoes it when typing fails.
    may_probe_box allows telling Claude Code's grey suggestion from a real draft by pressing one key: both read as
    plain text, and a draft is never disturbed.
    own_pattern matches this text, spacing removed, as an earlier try typed it: a request's time left changes.
    Waits end by whichever clock ran further: macOS stops the monotonic one while the Mac sleeps, so a wait spanning a
    night lasted the night. One spanning a sleep ends right after wake, and nothing the screen showed before the sleep,
    or during a read the sleep cut, counts.
    claim_left_unsent claims the message whose text bridge typed and never submitted, when the box holds it unedited,
    and returns how to settle that claim. Such text that stays the same for ORPHAN_STEADY_S gets Enter alone where a
    probe key would be allowed, never a clear or an edit.
    may_type False holds this try back without a key or a screen read while something else may deliver the text;
    once text is claimed it is not asked.
    A draft a probe proved is judged: stray keys are cleared once and the wait goes on, someone's words are kept,
    and text neither goes to tell_unsure once while the wait goes on. A clear the box does not show gives "failed".
    With Claude Code in vim mode, no key goes in unless the footer shows INSERT.
    """
    command = map_text_to_typable(command)
    vim = is_claude_vim()

    def held(screen: str) -> bool:
        # Vim NORMAL mode has no footer marker and takes keys as commands: "." repeats the last edit into the draft.
        return map_screen_to_busy(screen) or (vim and not map_screen_to_vim_insert(screen))

    own_chip: str | None = None

    def left_by_earlier_try(box: str | None) -> bool:
        # Exact, spacing aside: any other change may be someone's edit, and Enter would submit it.
        if not box:
            return False
        if own_chip is not None and box == own_chip:
            return True
        squashed = _squash(box)
        return squashed == _squash(command) or (own_pattern is not None and own_pattern.fullmatch(squashed) is not None)

    def just_typed(box: str | None) -> bool:
        return holds_text(box, command)

    def just_typed_or_folded(box: str | None) -> bool:
        """Into a box that was empty, the chip Claude Code folds a long insert into is ours: nothing else typed it."""
        nonlocal own_chip
        if own_chip is None and is_paste_chip(box):
            own_chip = box
        return just_typed(box) or (own_chip is not None and box == own_chip)

    target = ["--target", session, "--pane-id", pane]
    waiting = _Stopwatch()
    looked = _Stopwatch()
    idle_before = False
    known_draft: str | None = None
    box: str | None = None
    orphan_since: tuple[str, float] | None = None
    orphans_pressed: set[str] = set()
    claimed = False
    pressed: str | None = None
    pressed_was_last = False
    entered: _Stopwatch | None = None
    cleared = False
    retyped = False
    told_unsure = False

    def attempt() -> TypeResult:
        nonlocal claimed, pressed, pressed_was_last, idle_before, known_draft, box, orphan_since, entered, looked
        nonlocal may_probe_box, cleared, retyped, told_unsure, vim, own_chip
        enter_rounds = 0
        busy = False
        while waiting.seconds() < wait_s or (entered is not None and entered.seconds() < ENTER_BUDGET_S):
            # The owner can switch vim on or off during an hour's wait.
            vim = is_claude_vim()
            if not still_wanted():
                return "unwanted"
            if not claimed and may_type is not None and not may_type():
                idle_before, known_draft, box, orphan_since, busy = False, None, None, None, False
                time.sleep(1)
                continue
            screen = _read_screen(config, target)
            if screen is None and (moved := _find_pane_session(config, pane)) and moved != target[1]:
                target[1] = moved
                if may_probe_box is not None:
                    may_probe_box = _also_unselected(may_probe_box, config, moved)
                screen = _read_screen(config, target)
            if looked.slept():
                # The Mac slept since the last look, or during this one: what showed is no proof of anything now.
                idle_before, known_draft, box, orphan_since, busy = False, None, None, None, False
                looked = _Stopwatch()
                continue
            looked = _Stopwatch()
            idle = screen is not None and (map_screen_to_idle(screen) and not held(screen))
            box = map_screen_to_draft(screen) if screen is not None and not idle else None
            busy = screen is not None and held(screen)
            # Past the wait only our text's Enter is left to press, and only while the box shows it free to take one.
            overtime = waiting.seconds() >= wait_s
            result: TypeResult | None = None
            if claimed and screen is not None and not left_by_earlier_try(box):
                # The text left the box: an Enter that took late shows it in the transcript, and nothing is typed twice.
                if pressed and not pressed_was_last and _is_last_prompt(screen, pressed):
                    return "typed"
                if idle or box == "" or overtime:
                    return "stuck"
                time.sleep(2)
                continue
            if left_by_earlier_try(box) and screen is not None and not busy:
                with _pane_lock(pane):
                    again = _read_screen(config, target)
                    left = map_screen_to_draft(again) if again is not None else None
                    if again is not None and not held(again) and left_by_earlier_try(left):
                        # This try or an earlier one left this very text unsent: only its Enter is missing.
                        if not still_wanted() or (not claimed and claim is not None and not claim()):
                            return "unwanted"
                        claimed, pressed = True, _squash(left or "")
                        pressed_was_last = _is_last_prompt(again, pressed)
                        if entered is None:
                            entered = _Stopwatch()
                        if is_box_padded(again):
                            # Read as our text alone, it holds an empty line too, and Enter may not run it: once, the
                            # box starts over.
                            if retyped:
                                return "stuck"
                            retyped, pressed = True, _squash(command)
                            # Typed again, a long text folds into a chip with a new number.
                            own_chip = None
                            enter_rounds += 1
                            result = _clear_and_type_again(
                                config, target, again, command, just_typed_or_folded, still_wanted, held
                            )
                        else:
                            submitted = _submit(config, target, left_by_earlier_try, still_wanted)
                            enter_rounds += submitted != "untouched"
                            result = "typed" if submitted == "sent" else "stuck"
                if result is None or (result == "stuck" and enter_rounds < STUCK_ENTER_ROUNDS):
                    time.sleep(2)
                    continue
                return result
            if claimed:
                if overtime:
                    return "stuck"
                # Our text, held by a turn or a dialog: only its Enter may follow, once the screen allows it.
                time.sleep(2)
                continue
            # The prefix alone is a try cut short, whose stray start the judge clears.
            if box and box.startswith(BRIDGE_TEXT_PREFIX) and box != BRIDGE_TEXT_PREFIX:
                # Another message's unsent text: a probe key would only add to it.
                known_draft = box
                if orphan_since is None or orphan_since[0] != box:
                    # Steady while awake: sleep shows no one left it alone.
                    orphan_since = (box, time.monotonic())
                elif (
                    box not in orphans_pressed
                    and time.monotonic() - orphan_since[1] >= ORPHAN_STEADY_S
                    and may_probe_box is not None
                    and claim_left_unsent is not None
                    and screen is not None
                    and not held(screen)
                    and may_probe_box()
                ):
                    allowed = may_probe_box
                    with _pane_lock(pane):
                        again = _read_screen(config, target)
                        if again is not None and map_screen_to_draft(again) == box and not held(again) and allowed():
                            # Left by a try that died or by older code: unchanged this long, nobody is typing it.
                            settle = claim_left_unsent(box)
                            if settle is not None:
                                # Its own message, not this one: this one going unwanted must not cut its Enter short.
                                submitted = _submit(
                                    config,
                                    target,
                                    lambda draft, left=box, allowed=allowed: draft == left and allowed(),
                                    lambda: True,
                                )
                                settle(submitted == "sent")
                                if submitted != "untouched":
                                    orphans_pressed.add(box)
                    time.sleep(2)
                    continue
            else:
                orphan_since = None
            if (
                box
                and box != known_draft
                and may_probe_box is not None
                and screen is not None
                and not held(screen)
                and may_probe_box()
            ):
                with _pane_lock(pane):
                    again = _read_screen(config, target)
                    # The user may have come to the tab while this waited for the lock.
                    verdict = (
                        _probe_box(config, target, box, may_probe_box)
                        if again is not None
                        and map_screen_to_draft(again) == box
                        and not held(again)
                        and may_probe_box()
                        else "moved"
                    )
                    call = judge_box(box) if verdict == "draft" else None
                    if call == "clear" and not cleared:
                        # Read again: the owner may have typed since the probe proved the box.
                        again = _read_screen(config, target)
                        if (
                            again is not None
                            and map_screen_to_draft(again) == box
                            and not held(again)
                            and may_probe_box()
                        ):
                            # Once: stray keys that come back are someone's doing.
                            cleared = True
                            verdict = _clear_box(config, target, again, box, may_probe_box, held)
                if verdict == "suggestion":
                    idle = True
                    idle_before = True
                elif verdict == "draft":
                    known_draft = box
                    if call != "keep" and tell_unsure is not None and not told_unsure:
                        told_unsure = True
                        tell_unsure(box)
                elif verdict == "unknown":
                    # The box is not as we left it, so the user is there or a key may be stuck: no more keys.
                    return "failed"
            # Right after compaction the box looks empty before Claude Code accepts a submit; one more look settles it.
            if idle and idle_before:
                with _pane_lock(pane):
                    again = _read_screen(config, target)
                    # Idle, or still the suggestion the probe found.
                    if again is not None and (
                        (map_screen_to_idle(again) and not held(again))
                        or (box and map_screen_to_draft(again) == box and not held(again) and not is_box_padded(again))
                    ):
                        # The tab can switch conversations during the screen read; the claim keeps a hook from also
                        # delivering.
                        if not still_wanted() or (claim is not None and not claim()):
                            return "unwanted"
                        claimed, pressed = True, _squash(command)
                        pressed_was_last = _is_last_prompt(again, pressed)
                        result = _type_and_submit(config, target, command, just_typed_or_folded, still_wanted)
                        entered = _Stopwatch()
                if result == "stuck":
                    # A dialog that opened, or an Enter that did not take: the loop above presses it again.
                    enter_rounds += 1
                    time.sleep(2)
                    continue
                if result is not None:
                    return result
                idle = False
            idle_before = idle
            time.sleep(2)
        if claimed:
            return "stuck"
        if looked.slept():
            return "timeout"
        # A turn or a dialog holding the box is neither a draft nor unproven text.
        if box and not idle_before and not busy:
            return "draft" if box == known_draft else "unproven"
        return "timeout"

    result = attempt()
    if claimed and result != "typed" and release is not None:
        release()
    return result


@contextmanager
def _pane_lock(pane: str) -> Generator[None]:
    """One writer per pane while keys go in, so /compact, "continue" and messages never interleave; never held while
    waiting for the tab, or one long wait would hold every other delivery back."""
    lock_dir = state_dir() / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / f"type-{pane or 'none'}.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _also_unselected(allowed: Callable[[], bool], config: Config, session: str) -> Callable[[], bool]:
    """The caller's check knows the tab by its old id only, which no longer reads as selected."""
    return lambda: allowed() and not is_tab_selected(config, session)


class _Stopwatch:
    """Time since its start by whichever clock ran further: macOS stops the monotonic one while the Mac sleeps, and
    the wall clock can be set back."""

    def __init__(self) -> None:
        self._wall, self._monotonic = time.time(), time.monotonic()

    def seconds(self) -> float:
        return max(time.time() - self._wall, time.monotonic() - self._monotonic)

    def slept(self) -> bool:
        return (time.time() - self._wall) - (time.monotonic() - self._monotonic) > SLEEP_GAP_S


def _find_pane_session(config: Config, pane: str) -> str | None:
    output = run_agterm(config, ["tree", "--json"])
    return map_tree_to_pane_session(output, pane) if output is not None else None


def map_tree_to_pane_session(output: str, pane: str) -> str | None:
    """The tab holding this pane now, from `tree --json`: a tab that stopped answering by its id may still be there."""
    try:
        tree = json.loads(output)["result"]["tree"]
        return next(
            str(tab["id"])
            for workspace in tree["workspaces"]
            for tab in workspace["sessions"]
            if any(surface.get("paneID") == pane for surface in tab.get("surfaces", []))
        )
    except (ValueError, KeyError, TypeError, AttributeError, StopIteration):
        return None


def _read_screen(config: Config, target: list[str]) -> str | None:
    """None too for a read a sleep cut: it may show the screen from before, and nothing is done on that."""
    reading = _Stopwatch()
    screen = run_agterm(config, ["session", "text", *target, "--lines", "40"])
    return None if reading.slept() else screen


def _probe_box(config: Config, target: list[str], box: str, still_allowed: Callable[[], bool]) -> str:
    """A key replaces a suggestion and adds to a draft; either way it is taken back before this returns.

    "suggestion" or "draft" only when the box is verified back as it was and the user has not come to the tab;
    anything else is "unknown", and the caller must not touch the box again. The key is taken back whenever the
    box shows exactly it added, however late the screen shows it; a box that changed otherwise gets no key.
    """
    before = _squash(box)
    # Spacing is not compared: the key can land on a line of its own after a newline in the box. A box that is the
    # key alone looks the same before a replacing key lands, so only the added key counts there.
    with_key = {PROBE_KEY, before + PROBE_KEY} - {before}
    run_agterm(config, ["session", "type", *target, PROBE_KEY])
    # A frame showing neither is waited out, giving up on one left the key in the owner's draft, unless the owner came.
    after = _await_box(config, target, lambda now: now in with_key, lambda now: still_allowed())
    if after is None or not still_allowed():
        return "unknown"
    if run_agterm(config, ["session", "type", *target, BACKSPACE]) is None:
        return "unknown"
    # A suggestion the key replaced may come back or not; a draft must be exactly as it was.
    back = {before, ""} if after == PROBE_KEY else {before}
    restored = _await_box(config, target, lambda now: now in back, lambda now: now == after)
    if restored is None or not still_allowed():
        return "unknown"
    return "suggestion" if after == PROBE_KEY else "draft"


def _clear_box(
    config: Config,
    target: list[str],
    screen: str,
    box: str,
    still_allowed: Callable[[], bool],
    held: Callable[[str], bool],
) -> str:
    """Gives "cleared" once the box shows empty or a grey suggestion a probe proves; anything else is "unknown", and the
    caller must not touch the box again."""
    before = _squash(box)
    lines = map_screen_to_box_lines(screen)
    if lines is None or run_agterm(config, ["session", "type", *target, map_lines_to_clear_keys(len(lines))]) is None:
        return "unknown"
    after = _await_box(config, target, lambda now: now != before, lambda now: still_allowed())
    if after is None or not still_allowed():
        return "unknown"
    if after == "":
        return "cleared"
    # The probe key is vim's repeat in NORMAL mode, which the owner may have entered meanwhile.
    now = _read_screen(config, target)
    if now is None or held(now) or map_screen_to_draft(now) is None:
        return "unknown"
    return "cleared" if _probe_box(config, target, after, still_allowed) == "suggestion" else "unknown"


def _clear_and_type_again(
    config: Config,
    target: list[str],
    screen: str,
    command: str,
    ours: Callable[[str | None], bool],
    still_wanted: Callable[[], bool],
    held: Callable[[str], bool],
) -> TypeResult:
    lines = map_screen_to_box_lines(screen)
    draft = map_screen_to_draft(screen)
    if lines is None or draft is None:
        return "failed"
    left = _squash(draft)
    if run_agterm(config, ["session", "type", *target, map_lines_to_clear_keys(len(lines))]) is None:
        return "failed"
    if _await_box(config, target, lambda now: now == "", lambda now: now == left) is None:
        return "failed"
    if not still_wanted():
        return "unwanted"
    # Vim may have gone to NORMAL, or a dialog opened, while the box emptied.
    again = _read_screen(config, target)
    if again is None or not map_screen_to_idle(again) or held(again):
        return "failed"
    return _type_and_submit(config, target, command, ours, still_wanted)


def _holds_only_empty_lines(screen: str) -> bool:
    lines = map_screen_to_box_lines(screen)
    return lines is not None and len(lines) > 1 and not any(lines)


def is_box_padded(screen: str) -> bool:
    """The box holds an empty line beside other lines, which its draft does not show."""
    lines = map_screen_to_box_lines(screen)
    return lines is not None and len(lines) > 1 and "" in lines


def _await_box(
    config: Config, target: list[str], done: Callable[[str], bool], pending: Callable[[str], bool]
) -> str | None:
    """The box, spacing removed, once done holds. None once it shows anything neither done nor pending, or when
    PROBE_LANDS_S pass first: a loaded machine can show a key seconds after it went in. A slow read is waited out,
    not cut short: giving up early is what leaves a key in the box. "" is a box of one empty line: one of several
    empty lines is no answer yet."""
    started = _Stopwatch()
    while True:
        time.sleep(PROBE_SETTLE_S)
        screen = _read_screen(config, target)
        draft = map_screen_to_draft(screen) if screen is not None and not _holds_only_empty_lines(screen) else None
        now = _squash(draft) if draft is not None else None
        if now is not None and done(now):
            return now
        if now is not None and not pending(now):
            return None
        if started.seconds() >= PROBE_LANDS_S:
            return None


def is_tab_selected(config: Config, session: str) -> bool:
    output = run_agterm(config, ["tree", "--json"])
    if output is None:
        return True
    try:
        tree = json.loads(output)["result"]["tree"]
        return any(
            workspace.get("active") and tab.get("active") and tab.get("id") == session
            for workspace in tree["workspaces"]
            for tab in workspace["sessions"]
        )
    except (ValueError, KeyError, TypeError, AttributeError):
        return True


def _type_and_submit(
    config: Config,
    target: list[str],
    command: str,
    ours: Callable[[str | None], bool],
    still_wanted: Callable[[], bool],
) -> TypeResult:
    """Enter goes alone once the text shows in the box: in the same injection, a burst Claude Code reads in pieces is
    taken as a paste, and its Enter becomes a newline."""
    if run_agterm(config, ["session", "type", *target, command]) is None:
        return "failed"
    started = _Stopwatch()
    box: str | None = None
    while True:
        time.sleep(LOOK_EVERY_S)
        screen = _read_screen(config, target)
        box = map_screen_to_draft(screen) if screen is not None else None
        if ours(box):
            break
        waited = started.seconds()
        # Not ours alone: an Enter could submit what someone else typed, or nothing.
        if box and waited >= TEXT_LANDS_S:
            return "stuck"
        # An empty box may yet show the text on a loaded machine; the claim holds until then, so no hook adds it.
        if waited >= TEXT_LATE_S or (waited >= TEXT_LANDS_S and not still_wanted()):
            return "failed"
    return "typed" if _submit(config, target, ours, still_wanted) == "sent" else "stuck"


def _submit(
    config: Config, target: list[str], ours: Callable[[str | None], bool], still_wanted: Callable[[], bool]
) -> SubmitResult:
    """Enter only while our text sits in a box with no dialog, again when a redraw drops one; "sent" once the box
    empties after an Enter, "untouched" when no Enter went in. Anything else in the box, a dialog or an unreadable
    screen gets no more keys."""
    presses = 0
    while True:
        screen = _read_screen(config, target)
        if screen is None:
            break
        draft = map_screen_to_draft(screen)
        # A started turn can hide the box behind its spinner.
        if presses and (draft == "" or (draft is None and map_screen_to_busy(screen))):
            return "sent"
        if not still_wanted():
            return "sent" if presses else "untouched"
        # A dialog would take an Enter as its answer; an empty line beside our text may keep Enter from running it.
        if not ours(draft) or is_box_padded(screen) or map_screen_to_busy(screen) or presses > SUBMIT_RETRIES:
            break
        if run_agterm(config, ["session", "type", *target, ENTER]) is None:
            break
        presses += 1
        time.sleep(1)
    return "pressed" if presses else "untouched"


def is_paste_chip(box: str | None) -> bool:
    """One chip alone, as "[Pasted text #3]" or "[Pasted text #3 +18 lines]"; with other text beside it, it is not."""
    return box is not None and _PASTE_CHIP_RE.fullmatch(box) is not None


def holds_text(box: str | None, text: str) -> bool:
    """Wrapping can split a word across lines, and a long paste may show only its two ends."""
    if not box:
        return False
    have, want = _squash(box), _squash(text)
    if have == want:
        return True
    edge = PASTE_EDGE_CHARS
    return (
        len(want) > 2 * edge and have.startswith(want[:edge]) and have.endswith(want[-edge:]) and len(have) < len(want)
    )


def _is_last_prompt(screen: str, squashed: str) -> bool:
    """The transcript's newest submitted prompt starts with all of this text; a quote of it elsewhere does not count."""
    lines = map_screen_to_transcript(screen).splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(PROMPT_GLYPH)]
    if not starts:
        return False
    prompt = [lines[starts[-1]].removeprefix(PROMPT_GLYPH)]
    prompt += takewhile(lambda line: line[:1].isspace(), lines[starts[-1] + 1 :])
    return _squash(" ".join(prompt)).startswith(squashed)


def _squash(text: str) -> str:
    # Mapped as typing maps it, so text that older code typed with a screen's glyphs still reads as ours.
    return "".join(_map_glyphs_to_plain(text).split())


def _map_glyphs_to_plain(text: str) -> str:
    return _BOX_RULE_RE.sub("-", text).replace(PROMPT_GLYPH, ">")


def map_text_to_typable(text: str) -> str:
    """One line of printable text: newlines would submit early, and control characters act as keys (Esc, Ctrl-U).

    A quoted screen's rules and prompt glyph become plain marks: wrapped onto a line's start in the box, they read as
    the box's own edges, and the box would never show the text whole to get its Enter.
    """
    return " ".join(_map_glyphs_to_plain("".join(ch if ch.isprintable() else " " for ch in text)).split())


class _AgtermTrouble:
    """Timed-out and unstartable agtermctl calls of this process, or slow ones, logged at most every
    AGTERM_LOG_EVERY_S with a count, so a hung agterm shows without one line per screen read."""

    def __init__(self, log: Callable[[str, str], None] = log_error) -> None:
        self.logged_at = 0.0
        self.unlogged = 0
        self._log = log
        self._lock = threading.Lock()

    def note(self, args: list[str], outcome: str, seconds: float) -> None:
        with self._lock:
            self.unlogged += 1
            now = time.monotonic()
            if self.logged_at and now - self.logged_at < AGTERM_LOG_EVERY_S:
                return
            more = f" ({self.unlogged - 1} more since the last line)" if self.unlogged > 1 else ""
            self.logged_at, self.unlogged = now, 0
        self._log("agterm", f"agtermctl {' '.join(args[:2])}: {outcome} after {seconds:.1f} s{more}")


_agterm_trouble = _AgtermTrouble()
_agterm_slow = _AgtermTrouble(log_slow)


def run_agterm(config: Config, args: list[str]) -> str | None:
    started = time.monotonic()
    try:
        # Explicit UTF-8: hooks can run under a C locale, which would garble the prompt glyph.
        done = subprocess.run(
            [config.agtermctl, *args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=AGTERM_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _agterm_trouble.note(args, "timed out", time.monotonic() - started)
        return None
    except OSError as exc:
        _agterm_trouble.note(args, f"failed to start ({exc})", time.monotonic() - started)
        return None
    seconds = time.monotonic() - started
    # A non-zero exit is agterm answering (a closed tab, say); only silence and slowness mean it is in trouble.
    if seconds >= AGTERM_SLOW_S:
        _agterm_slow.note(args, "slow", seconds)
    return done.stdout if done.returncode == 0 else None
