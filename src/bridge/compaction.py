import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from bridge.registry import format_age

KEEP_MAX_CHARS = 8_000
"""With its header the list must fit one hook message (bridge.inject.INJECT_MAX_CHARS) on its own."""
COMPACT_AT_WINDOW_SHARE = 0.75
"""Share of a tab's own context window (from its status line) past which it compacts, if below compact_at_tokens."""
NEAR_MARGIN_SHARE = 4
"""Within a quarter of the threshold (150k of 600k), tool hooks check every call instead of every 30 s."""
FAILED_TURNS_DEAD = 3
CONTEXT_FULL_PCT = 90
"""Past this share of its window a tab is full: offered no work."""
RETRY_AFTER_MS = 30 * 60_000
"""A drive that waited in vain is tried again after this, by the watcher or by asking for notes again."""
COMPACT_WAIT_S = 3600
"""The owner's text in the box, a selected tab's grey suggestion or a long turn can hold a drive this long."""
TOO_LONG_TEXT = "Prompt is too long"
DEAD_TOO_LONG = "too-long"
DEAD_REFUSED = "refused"
DEAD_FAILING = "failing"
DEAD_LIMIT_SCREEN = "limit-screen"
DEAD_COMPACTABLE = frozenset({DEAD_TOO_LONG, DEAD_REFUSED, DEAD_LIMIT_SCREEN})
"""Deaths compaction cures; an outage, sleep or login failure is only told of: compacting would lose detail for
nothing."""
FULL_CONTEXT_SQL = "c.context_dead_why IN (" + ", ".join(f"'{d}'" for d in sorted(DEAD_COMPACTABLE)) + ")"
"""For conversations c whose mail waits for compaction."""
GENERIC_KEEP = (
    "Keep the user's decisions and constraints, exact identifiers (PRs, issues, branches, paths, commands), open "
    "tasks and promises, and the current step with its next action."
)
"""Given to /compact itself when the session could not save notes first."""
UNREAD_NOTE = (
    " Messages that reached this tab while its context was full never reached you; bridge gave them again with "
    "your notes, so some may repeat."
)
_TAIL_MAX = 4 * 1024 * 1024
_TRAILING_TAIL_MAX = 256 * 1024
PHASE_REQUESTED = "requested"
PHASE_COMPACTING = "compacting"
PHASE_CONTINUE = "continue"
CONTINUE_PROMPT = (
    "[bridge] Compaction is done and the notes you saved with `bridge keep` are above. Continue where you left off."
)
CONTINUE_PROMPTS = {
    "shown": CONTINUE_PROMPT,
    "none": "[bridge] Compaction is done; you saved no notes with `bridge keep` before it. Continue where you "
    "left off.",
    "cut": "[bridge] Compaction is done. The notes you saved with `bridge keep` did not fit above in full: "
    "`bridge keep --show` prints them. Continue where you left off.",
    "unread": "[bridge] Compaction is done, but bridge could not read your saved notes for you then: "
    "`bridge keep --show` prints them, `guild notes` your notes. Continue where you left off.",
}
"""By what reached the session at the compaction's start, so the prompt never points at notes that are not there."""
_EMPTY_PROMPT_RE = re.compile(r"^\u276f[\s\u00a0]*$")
_DIALOG_MARKERS = ("Enter to confirm", "Esc to cancel")
_BUSY_MARKERS = ("esc to interrupt", *_DIALOG_MARKERS, "-- NORMAL --", "Compacting")
_SPINNER_RE = re.compile(r"^[\u00b7\u2722\u2733\u2736\u273b\u273d*] \S[^(]*\u2026\s*\(\d")
"""A turn's spinner line, such as "· Photosynthesizing… (50s · ↓ 4.2k tokens)": it no longer says esc to interrupt.
Only at the line's start: the transcript indents what it quotes."""
_OPTION_LINE_RE = re.compile(r"^\u276f\s*\d+\.")
_FOOTER_RE = re.compile(r"^\s+(?:-- (?:INSERT|NORMAL|VISUAL) --|.*\u23f5\u23f5|\? for shortcuts|\S.* \| \d+% ctx\b)")
"""Claude Code's footer under the input box (mode, permissions, model and context): where a torn box ends."""
_STATUS_RE = re.compile(r"^\s+\S.* \| \d+% ctx\b")
CHARS_PER_TOKEN = 4
IMAGE_TOKENS = 3_000
"""An image in a tool result costs the model a few thousand tokens however long its base64 is."""


@dataclass(frozen=True)
class ContextReading:
    tokens: int
    at_ms: int | None
    """When the API reported this usage, from the transcript entry."""


@dataclass(frozen=True)
class FailedTurns:
    """The newest main-thread assistant entries that are Claude Code's stand-ins for a request the API refused."""

    count: int
    too_long: bool
    first_at_ms: int | None
    refused: bool
    """Every one an invalid request: the API refused what was sent, not the moment."""


@dataclass(frozen=True)
class TranscriptTail:
    reading: ContextReading | None
    failed: FailedTurns | None
    """None when the newest main-thread assistant entry is a real answer, or there is none."""


SYNTHETIC_MODEL = "<synthetic>"


def read_context_tokens(path: Path) -> ContextReading | None:
    tail = read_transcript_tail(path)
    return tail.reading if tail else None


def read_transcript_tail(path: Path) -> TranscriptTail | None:
    """Context size as the API last counted it (the newest main-thread assistant usage after the last compaction),
    and the failed requests since that answer.

    One bounded tail read; a usage entry beyond it, or only before a compaction boundary, gives no reading.
    """
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            start = max(0, f.tell() - _TAIL_MAX)
            f.seek(start)
            lines = f.read().split(b"\n")
    except OSError:
        return None
    count, too_long, first_at, refused = 0, False, None, True
    reading = None
    for raw in reversed(lines[1:] if start > 0 else lines):
        if b'"compact_boundary"' in raw:
            # Usage before the boundary describes the conversation as it was before compaction.
            break
        failure = _map_line_to_failure(raw)
        if failure is not None:
            count, too_long, first_at = count + 1, too_long or failure[0], failure[1] or first_at
            refused = refused and failure[2]
            continue
        reading = _map_line_to_reading(raw)
        if reading is not None:
            break
    return TranscriptTail(reading, FailedTurns(count, too_long, first_at, refused) if count else None)


@dataclass(frozen=True)
class TrailingFailure:
    error: str | None
    text: str
    at_ms: int


def read_trailing_failure(path: Path) -> TrailingFailure | None:
    """The main thread's last message, when it is Claude Code's stand-in for a failed request: no prompt since.

    One small tail read: the stand-in is short, and what follows it until the next prompt is too.
    """
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            start = max(0, f.tell() - _TRAILING_TAIL_MAX)
            f.seek(start)
            lines = f.read().split(b"\n")
    except OSError:
        return None
    for raw in reversed(lines[1:] if start > 0 else lines):
        if b'"user"' not in raw and b'"assistant"' not in raw:
            continue
        try:
            entry: object = json.loads(raw)
        except ValueError:
            continue
        data = cast(dict[str, object], entry) if isinstance(entry, dict) else {}
        if data.get("type") not in ("user", "assistant") or data.get("isSidechain"):
            continue
        at_ms = _map_timestamp_to_ms(data.get("timestamp"))
        if data.get("type") == "user" or data.get("isApiErrorMessage") is not True or at_ms is None:
            return None
        message = data.get("message")
        content = cast(dict[str, object], message).get("content") if isinstance(message, dict) else None
        blocks = cast(list[object], content) if isinstance(content, list) else []
        text = " ".join(str(cast(dict[str, object], b).get("text") or "") for b in blocks if isinstance(b, dict))
        error = data.get("error")
        return TrailingFailure(error if isinstance(error, str) else None, text, at_ms)
    return None


def map_failed_to_death(failed: FailedTurns | None) -> str | None:
    """Over its window, or unable to take a turn at all: either way nothing typed into the tab reaches the model."""
    if failed is None:
        return None
    if failed.too_long:
        return DEAD_TOO_LONG
    if failed.count < FAILED_TURNS_DEAD:
        return None
    return DEAD_REFUSED if failed.refused else DEAD_FAILING


def _map_line_to_failure(raw: bytes) -> tuple[bool, int | None, bool] | None:
    """(said the prompt is too long, when, an invalid request) for a main-thread stand-in of a failed request."""
    if b'"isApiErrorMessage"' not in raw or b'"assistant"' not in raw:
        return None
    try:
        entry: object = json.loads(raw)
    except ValueError:
        return None
    data = cast(dict[str, object], entry) if isinstance(entry, dict) else {}
    if data.get("type") != "assistant" or data.get("isSidechain") or data.get("isApiErrorMessage") is not True:
        return None
    message = data.get("message")
    content = cast(dict[str, object], message).get("content") if isinstance(message, dict) else None
    blocks = cast(list[object], content) if isinstance(content, list) else []
    texts = [str(cast(dict[str, object], b).get("text") or "") for b in blocks if isinstance(b, dict)]
    too_long = any(TOO_LONG_TEXT in t for t in texts)
    return too_long, _map_timestamp_to_ms(data.get("timestamp")), data.get("error") == "invalid_request"


def estimate_pending_tokens(event: str, payload: dict[str, object]) -> int:
    """What the next request adds before the API counts it: the new prompt or tool result, roughly."""
    pending = payload.get("prompt") if event == "UserPromptSubmit" else payload.get("tool_response")
    if pending is None:
        return 0
    text = pending if isinstance(pending, str) else json.dumps(pending, ensure_ascii=False)
    images, image_chars = _measure_images(pending)
    return (len(text) - image_chars) // CHARS_PER_TOKEN + images * IMAGE_TOKENS


def _measure_images(value: object) -> tuple[int, int]:
    """(images, characters of their base64) in a tool result."""
    if isinstance(value, dict):
        fields = cast(dict[str, object], value)
        image = _find_image_field(fields)
        found = [_measure_images(v) for k, v in fields.items() if k != image]
        data = fields.get(image) if image else None
        if isinstance(data, str):
            found.append((1, len(data)))
        return sum(n for n, _ in found), sum(c for _, c in found)
    if isinstance(value, list):
        found = [_measure_images(v) for v in cast(list[object], value)]
        return sum(n for n, _ in found), sum(c for _, c in found)
    return 0, 0


def _find_image_field(fields: dict[str, object]) -> str | None:
    """Read gives {"base64", "type": "image/…"}; an API image block {"media_type", "data"}, an MCP one "mimeType"."""
    for kind_key, data_key in (("type", "base64"), ("media_type", "data"), ("mimeType", "data")):
        kind = fields.get(kind_key)
        if isinstance(kind, str) and kind.startswith("image/") and data_key in fields:
            return data_key
    return None


def _map_line_to_reading(raw: bytes) -> ContextReading | None:
    if b'"usage"' not in raw or b'"assistant"' not in raw:
        return None
    try:
        entry: object = json.loads(raw)
    except ValueError:
        return None
    data = cast(dict[str, object], entry) if isinstance(entry, dict) else {}
    if data.get("type") != "assistant" or data.get("isSidechain"):
        return None
    message = data.get("message")
    fields = cast(dict[str, object], message) if isinstance(message, dict) else {}
    # Claude Code writes its own stand-in for a request that failed (not logged in, prompt too long), with zero usage:
    # it says nothing of the context's size.
    if fields.get("model") == SYNTHETIC_MODEL:
        return None
    usage = fields.get("usage")
    if not isinstance(usage, dict):
        return None
    counts = cast(dict[str, object], usage)
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
    tokens = sum(int(v) for k in keys if isinstance(v := counts.get(k), int))
    return ContextReading(tokens, _map_timestamp_to_ms(data.get("timestamp")))


def _map_timestamp_to_ms(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value).timestamp() * 1000)
    except ValueError:
        return None


def map_window_to_compact_at(compact_at_tokens: int, share: float, window: int | None) -> int:
    """A 1M tab keeps the configured threshold; a 200k one compacts before its window ends, not at 600k."""
    if not compact_at_tokens or not window:
        return compact_at_tokens
    return min(compact_at_tokens, int(window * share))


def estimate_window(tokens: int, pct: int) -> int:
    """The status line floors the percentage, so the window is above this; the low side compacts earlier."""
    return tokens * 100 // (pct + 1)


def record_context(
    conn: sqlite3.Connection,
    conversation_id: str,
    reading: ContextReading,
    pending_tokens: int,
    compact_at_tokens: int,
    window_share: float,
    can_deliver: bool,
    driven: bool,
    at_ms: int,
) -> str | None:
    """Stores the context size; at the threshold returns the save-your-notes request, once per compaction cycle.

    When bridge can drive the tab (agterm), the session is asked to end its turn so compaction follows at once.
    """
    row = conn.execute(
        "SELECT compact_warned_at, compacted_at, context_window, compact_phase, compact_requested_at"
        " FROM conversations WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    if row is None:
        return None
    compacted_at = row["compacted_at"]
    if compacted_at is not None and reading.at_ms is not None and reading.at_ms < compacted_at:
        # The first hooks after compaction can run before Claude Code writes the boundary and read the old size.
        return None
    tokens = reading.tokens + pending_tokens
    conn.execute("UPDATE conversations SET context_tokens = ? WHERE conversation_id = ?", (tokens, conversation_id))
    compact_at = map_window_to_compact_at(compact_at_tokens, window_share, row["context_window"])
    requested = row["compact_requested_at"]
    # A drive that ended without a compaction left the mark; once its retry pause passed the request goes out again.
    retry = driven and row["compact_phase"] is None and requested is not None and at_ms - requested >= RETRY_AFTER_MS
    if (row["compact_warned_at"] is not None and not retry) or not can_deliver or not compact_at or tokens < compact_at:
        return None
    conn.execute(
        "UPDATE conversations SET compact_warned_at = ?, compact_phase = ?, compact_requested_at = ?"
        " WHERE conversation_id = ?",
        (tokens, PHASE_REQUESTED if driven else None, at_ms, conversation_id),
    )
    return convert_tokens_to_request(tokens, driven)


def is_near_threshold(conn: sqlite3.Connection, conversation_id: str, compact_at_tokens: int, share: float) -> bool:
    row = conn.execute(
        "SELECT context_tokens, compact_warned_at, context_window FROM conversations WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    if not compact_at_tokens or row is None or row["compact_warned_at"] is not None or row["context_tokens"] is None:
        return False
    compact_at = map_window_to_compact_at(compact_at_tokens, share, row["context_window"])
    return int(row["context_tokens"]) >= compact_at - compact_at // NEAR_MARGIN_SHARE


def convert_tokens_to_request(tokens: int, driven: bool, pct: int | None = None) -> str:
    size = f"{pct}% of its window" if pct is not None else f"{tokens // 1000}k tokens"
    save = (
        f"[bridge] Context reached {size}: time to compact this conversation, and compaction "
        "loses detail. Right now, save what must survive in a short list with `bridge keep` (stdin; it replaces "
        "the previous list): the user's decisions and constraints, exact identifiers (PRs, issues, branches, "
        "paths, commands), open tasks and promises, the current step and its next action."
    )
    if driven:
        return save + (
            " Then end your turn at once with one short line for the user, not a summary (a turn without text makes "
            "Claude Code ask you to go on): bridge compacts the conversation right after, gives the list back, and "
            "tells you to continue."
        )
    return save + " Claude Code will compact soon; bridge gives the list back after compaction."


def is_request_delivered(conn: sqlite3.Connection, conversation_id: str) -> bool:
    """A request the hook gave is delivered by then; one serve sent as a message once it is offered or typed."""
    row = conn.execute(
        """
        SELECT r.offered_at, r.acked_at FROM conversations c
        JOIN message_recipients r ON r.message_id = c.compact_request_message AND r.conversation_id = c.conversation_id
        WHERE c.conversation_id = ?
        """,
        (conversation_id,),
    ).fetchone()
    return row is None or row["offered_at"] is not None or row["acked_at"] is not None


def take_phase(conn: sqlite3.Connection, conversation_id: str, expected: str, then: str | None) -> bool:
    """Moves the driven compaction to its next phase if it is where the caller expects; False means not ours."""
    cursor = conn.execute(
        "UPDATE conversations SET compact_phase = ? WHERE conversation_id = ? AND compact_phase = ?",
        (then, conversation_id, expected),
    )
    return cursor.rowcount == 1


def map_env_to_can_drive(env: Mapping[str, str]) -> bool:
    """bridge can type into the tab only inside agterm, where it knows the session and the pane."""
    return bool(env.get("AGTERM_SESSION_ID") and env.get("AGTERM_PANE_ID"))


def map_screen_to_idle(screen: str) -> bool:
    """Claude Code waits for input: its input box (the last prompt line, between two rules) is empty,
    and there is no spinner, dialog or vim normal mode. A shell prompt or a draft never matches."""
    tail = [line for line in screen.splitlines() if line.strip()][-14:]
    prompts = [i for i, line in enumerate(tail) if line.lstrip().startswith("\u276f")]
    if not prompts:
        return False
    # The box's edges as the draft reads them: an indented rule is the user's text, not an empty box's end.
    box = _find_input_box(tail)
    if box is None or box != (prompts[-1], prompts[-1] + 1) or not _EMPTY_PROMPT_RE.match(tail[box[0]]):
        return False
    if map_screen_to_box_lines(screen) != [""]:
        return False
    # The whole screen: a submitted prompt whose first line is above the tail still quotes, not shows, its markers.
    return not map_screen_to_busy(screen)


def map_screen_to_waiting(screen: str) -> bool:
    """Claude Code waits for input with its box empty or not: its grey suggestion reads as typed text, which only the
    drive's probe tells from a draft before anything is typed over it."""
    return map_screen_to_box_lines(screen) is not None and not map_screen_to_busy(screen)


def map_screen_to_busy(screen: str) -> bool:
    """A spinner, a dialog or vim normal mode: keys sent now would not reach the input box as typed text."""
    tail = _find_live_tail(screen)
    return any(marker in line for line in tail for marker in _BUSY_MARKERS) or any(
        _SPINNER_RE.match(line) for line in tail
    )


def map_screen_to_vim_insert(screen: str) -> bool:
    """The footer under the input box says vim INSERT; Claude Code 2.1.328 marks no other vim mode."""
    lines = [line for line in screen.splitlines() if line.strip()]
    box = _find_input_box(lines)
    return box is not None and any(_VIM_INSERT_RE.match(line) for line in lines[box[1] :])


_VIM_INSERT_RE = re.compile(r"^\s+-- INSERT --")


def map_screen_to_dialog(screen: str) -> bool:
    return any(marker in line for line in _find_live_tail(screen) for marker in _DIALOG_MARKERS)


def _find_live_tail(screen: str) -> list[str]:
    """The screen's last lines without what only quotes a dialog.

    The input box itself is left out, a message typed there may quote a dialog, unless it opens on a numbered choice:
    then it is a dialog's list. So are prompts submitted above it, which keep quoting it.
    """
    lines = [line for line in screen.splitlines() if line.strip()]
    box = _find_input_box(lines)
    quoted: set[int] = set()
    if box is not None and not _OPTION_LINE_RE.match(lines[box[0]].lstrip()):
        quoted = _find_submitted_prompt_lines(lines[: box[0]])
        lines = lines[: box[0]] + lines[box[1] :]
    # The window is taken before the prompts leave it, so older lines never slide into it.
    return [line for i, line in enumerate(lines) if i >= len(lines) - 14 and i not in quoted]


def _is_box_text_hidden(lines: list[str], box: tuple[int, int]) -> bool:
    """A frame torn so that the box's row shows empty above its rule and the box's glyph sits on the status line under
    it: what the box holds is on no row. Typed text quoting a status line has no empty prompt line over its rule."""
    start = box[0]
    return (
        start >= 2
        and _EMPTY_PROMPT_RE.match(lines[start - 2]) is not None
        and _STATUS_RE.match(" " + lines[start].lstrip()[1:]) is not None
    )


def is_box_text_hidden(screen: str) -> bool:
    lines = [line for line in screen.splitlines() if line.strip()]
    box = _find_input_box(lines)
    return box is not None and _is_box_text_hidden(lines, box)


def map_screen_to_draft(screen: str) -> str | None:
    """Text in Claude Code's input box (the last prompt line under a rule, up to the next rule); None without a box.

    Only a box that closes near the screen's foot counts, however far up a long text in a narrow pane makes it start.
    """
    lines = [line for line in screen.splitlines() if line.strip()]
    box = _find_input_box(lines)
    if box is None or box[1] < len(lines) - 14 or _is_box_text_hidden(lines, box):
        return None
    body = [_CLEAR_HINT_RE.sub("", line).strip() for line in lines[box[0] : box[1]]]
    return " ".join(body).lstrip("\u276f").replace("\u00a0", " ").strip()


def map_screen_to_box_lines(screen: str) -> list[str] | None:
    """The input box's lines as shown, empty ones kept, which the draft drops: a box holding an empty line over
    "/compact" reads as the draft "/compact", and one holding two empty lines as an empty box. None without a box."""
    raw = screen.splitlines()
    kept = [i for i, line in enumerate(raw) if line.strip()]
    lines = [raw[i] for i in kept]
    box = _find_input_box(lines)
    if box is None or box[1] < len(lines) - 14 or _is_box_text_hidden(lines, box):
        return None
    start = kept[box[0]]
    # Closed by a rule, every row up to it is the box's; a torn box's blank rows may be the frame's.
    closed = box[1] < len(lines) and lines[box[1]].startswith("\u2500")
    end = kept[box[1]] if closed else kept[box[1] - 1] + 1
    body = [_CLEAR_HINT_RE.sub("", line).strip() for line in raw[start:end]]
    body[0] = body[0].strip().lstrip("\u276f").strip()
    return body


_CLEAR_HINT_RE = re.compile(r"\s{2,}new task\? /clear to save \S+ tokens\b.*$")
"""Claude Code's right-aligned hint above the box: a frame drawn wider can leave it on the box line, as no text."""


def map_screen_to_transcript(screen: str) -> str:
    """The screen without the input box's text."""
    lines = [line for line in screen.splitlines() if line.strip()]
    box = _find_input_box(lines)
    return "\n".join(lines[: box[0]] + lines[box[1] :] if box is not None else lines)


_MENU_ROW_RE = re.compile(r"^\s+\u276f\s*/\S+\s{2,}\S")
"""The highlighted row of the slash-command list under the box, the prompt glyph and "/compact    Free up context…":
it sits right under the box's closing rule and is no prompt."""


def _find_input_box(lines: list[str]) -> tuple[int, int] | None:
    """From the last prompt line under a rule up to, not including, the rule that closes it.

    A rule starts at the line's edge: the box indents its text, so a rule typed or wrapped into it is no edge.
    """
    starts = [
        i
        for i, line in enumerate(lines)
        if line.lstrip().startswith("\u276f")
        and i > 0
        and lines[i - 1].startswith("\u2500")
        and not _MENU_ROW_RE.match(line)
    ]
    if not starts:
        return None
    for end in range(starts[-1], len(lines)):
        if lines[end].startswith("\u2500"):
            return starts[-1], end
    # A torn frame (a banner shifted it) can draw the box without its closing rule; Claude Code's footer then ends it.
    for end in range(starts[-1] + 1, len(lines)):
        if _FOOTER_RE.match(lines[end]):
            return starts[-1], end
    if _EMPTY_PROMPT_RE.match(lines[starts[-1]]):
        return starts[-1], starts[-1] + 1
    return None


def _find_submitted_prompt_lines(lines: list[str]) -> set[int]:
    """The transcript's submitted prompts: each a prompt line at the edge and the indented lines under it."""
    quoted: set[int] = set()
    in_prompt = False
    for i, line in enumerate(lines):
        in_prompt = (line.startswith("\u276f") and not _OPTION_LINE_RE.match(line)) or (
            in_prompt and line[:1].isspace()
        )
        if in_prompt:
            quoted.add(i)
    return quoted


def read_phase(conn: sqlite3.Connection, conversation_id: str) -> str | None:
    row = conn.execute(
        "SELECT compact_phase FROM conversations WHERE conversation_id = ?", (conversation_id,)
    ).fetchone()
    return str(row["compact_phase"]) if row and row["compact_phase"] else None


def give_up_driving(conn: sqlite3.Connection, conversation_id: str) -> None:
    """Back to the start of the cycle: the next prompt asks again, and Claude Code's auto-compact stays the backstop."""
    conn.execute(
        "UPDATE conversations SET compact_phase = NULL, compact_warned_at = NULL WHERE conversation_id = ?",
        (conversation_id,),
    )


def end_driving(conn: sqlite3.Connection, conversation_id: str, at_ms: int) -> None:
    """A drive that waited in vain ends the cycle without a new request at once: what held it most likely still holds
    it. Dated from now, the request and serve's watcher both try again once RETRY_AFTER_MS has passed."""
    conn.execute(
        "UPDATE conversations SET compact_phase = NULL, compact_requested_at = ? WHERE conversation_id = ?",
        (at_ms, conversation_id),
    )


def reset_after_compaction(conn: sqlite3.Connection, conversation_id: str, at_ms: int) -> int:
    """Returns how many messages that reached the tab while its context was full are given again."""
    reopened = reopen_unread_messages(conn, conversation_id)
    conn.execute(
        """
        UPDATE conversations SET compact_warned_at = NULL, context_tokens = NULL, compacted_at = ?,
            context_pct = NULL, context_pct_at = NULL, compact_requested_at = NULL, compact_request_message = NULL,
            context_dead_at = NULL, context_dead_why = NULL, context_unread_from = NULL,
            context_dead_alerted_at = NULL
        WHERE conversation_id = ?
        """,
        (at_ms, conversation_id),
    )
    conn.execute(
        "DELETE FROM signal_posts WHERE key IN (?, ?)",
        (_told_key(TOLD_FORCED, conversation_id), _told_key(TOLD_HELD, conversation_id)),
    )
    return reopened


TOLD_FORCED = "compact-forced"
TOLD_HELD = "compact-held"


def _told_key(kind: str, conversation_id: str) -> str:
    return f"{kind}:{conversation_id}"


def take_told_once(conn: sqlite3.Connection, kind: str, conversation_id: str, at_ms: int) -> bool:
    """True the first time per compaction cycle: retried drives would otherwise repeat the same notice."""
    cursor = conn.execute(
        "INSERT OR IGNORE INTO signal_posts (key, message_id, posted_at) VALUES (?, 0, ?)",
        (_told_key(kind, conversation_id), at_ms),
    )
    return cursor.rowcount == 1


def record_failed_turns(conn: sqlite3.Connection, conversation_id: str, tail: TranscriptTail) -> None:
    """From the hook's transcript read: a death is dated once, and a real answer since ends it."""
    death = map_failed_to_death(tail.failed)
    if death is None:
        if tail.failed is None and is_context_dead(conn, conversation_id):
            # It answered again (a larger window, the API back): what it missed meanwhile comes with its next hook.
            reopen_unread_messages(conn, conversation_id)
            clear_death(conn, conversation_id)
        return
    failed = cast(FailedTurns, tail.failed)
    # Whatever reached the tab after the model last answered was in a request that failed.
    unread_from = tail.reading.at_ms if tail.reading and tail.reading.at_ms else failed.first_at_ms
    record_death(conn, conversation_id, death, failed.first_at_ms, unread_from)


def record_death(
    conn: sqlite3.Connection, conversation_id: str, why: str, dead_at: int | None, unread_from: int | None
) -> None:
    conn.execute(
        """
        UPDATE conversations SET context_dead_why = ?, context_dead_at = coalesce(context_dead_at, ?),
            context_unread_from = coalesce(context_unread_from, ?)
        WHERE conversation_id = ?
        """,
        (why, dead_at, unread_from, conversation_id),
    )


def clear_death(conn: sqlite3.Connection, conversation_id: str) -> None:
    conn.execute(
        "UPDATE conversations SET context_dead_at = NULL, context_dead_why = NULL, context_unread_from = NULL,"
        " context_dead_alerted_at = NULL WHERE conversation_id = ? AND context_dead_why IS NOT NULL",
        (conversation_id,),
    )


def is_context_dead(conn: sqlite3.Connection, conversation_id: str) -> bool:
    row = conn.execute(
        "SELECT context_dead_why FROM conversations WHERE conversation_id = ?", (conversation_id,)
    ).fetchone()
    return row is not None and row["context_dead_why"] is not None


def is_context_full(conn: sqlite3.Connection, conversation_id: str) -> bool:
    """Mail waits only for a full context: after an API error the next message is what lets it answer again."""
    row = conn.execute(
        "SELECT context_dead_why FROM conversations WHERE conversation_id = ?", (conversation_id,)
    ).fetchone()
    return row is not None and row["context_dead_why"] in DEAD_COMPACTABLE


def reopen_before_clear(conn: sqlite3.Connection, conversation_id: str) -> None:
    """/clear carries the tab's mail to the new conversation; what the dead one never read goes as unread."""
    reopen_unread_messages(conn, conversation_id)
    clear_death(conn, conversation_id)


def map_death_to_why(death: str) -> str:
    if death in DEAD_COMPACTABLE:
        return "context full: it cannot take a turn until compacted"
    return "cannot take a turn: its last ones failed with API errors"


def read_context_death(conn: sqlite3.Connection, conversation_id: str | None) -> str | None:
    """Why the conversation can take no turn, if so."""
    if conversation_id is None:
        return None
    row = conn.execute(
        "SELECT context_dead_why FROM conversations WHERE conversation_id = ?", (conversation_id,)
    ).fetchone()
    return map_death_to_why(str(row["context_dead_why"])) if row and row["context_dead_why"] else None


def read_context_full(conn: sqlite3.Connection, conversation_id: str | None) -> str | None:
    """Why the conversation has no room for more work: it can take no turn, or it is nearly at its window."""
    if death := read_context_death(conn, conversation_id):
        return death
    row = conn.execute("SELECT context_pct FROM conversations WHERE conversation_id = ?", (conversation_id,)).fetchone()
    if row is not None and row["context_pct"] is not None and int(row["context_pct"]) >= CONTEXT_FULL_PCT:
        return f"context full: {row['context_pct']}% of its window"
    return None


def reopen_unread_messages(conn: sqlite3.Connection, conversation_id: str) -> int:
    """Messages offered or typed after the model last answered went into requests that failed: they are made
    deliverable again. Nothing the model did can have closed them, since it took no turn; a live typer's claim stays."""
    row = conn.execute(
        "SELECT context_unread_from FROM conversations WHERE conversation_id = ?", (conversation_id,)
    ).fetchone()
    if row is None or row["context_unread_from"] is None:
        return 0
    cursor = conn.execute(
        """
        UPDATE message_recipients
        SET offered_at = NULL, acked_at = NULL, stuck_at = NULL, delivery_failed_at = NULL
        WHERE conversation_id = ?1 AND offered_at >= ?2 AND typing_started_at IS NULL
          AND done_at IS NULL AND declined_at IS NULL AND (acked_at IS NULL OR acked_at >= ?2)
          AND message_id IN (SELECT id FROM messages WHERE cancelled_at IS NULL)
        """,
        (conversation_id, int(row["context_unread_from"])),
    )
    return cursor.rowcount


def save_keep(conn: sqlite3.Connection, conversation_id: str, text: str, at_ms: int) -> None:
    conn.execute(
        """
        INSERT INTO keep_notes (conversation_id, text, saved_at) VALUES (?, ?, ?)
        ON CONFLICT (conversation_id) DO UPDATE SET text = excluded.text, saved_at = excluded.saved_at
        """,
        (conversation_id, text, at_ms),
    )


def load_keep(conn: sqlite3.Connection, conversation_id: str) -> tuple[str, int] | None:
    row = conn.execute("SELECT text, saved_at FROM keep_notes WHERE conversation_id = ?", (conversation_id,)).fetchone()
    return (str(row["text"]), int(row["saved_at"])) if row else None


def list_dropped_lines(previous: str, text: str) -> list[str]:
    """Lines of the replaced list the new one no longer has; reworded lines count as dropped."""
    kept = {line.strip() for line in text.splitlines()}
    return [line for line in previous.splitlines() if line.strip() and line.strip() not in kept]


def convert_keep_to_context(text: str, saved_at: int, at_ms: int) -> str:
    return (
        f"[bridge] Notes you saved with `bridge keep` {format_age(at_ms - saved_at)} ago, before this conversation "
        "was compacted. Treat them as part of the conversation; where the summary above disagrees, check which "
        f"is newer.\n{text}"
    )
