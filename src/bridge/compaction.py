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
NEAR_MARGIN_TOKENS = 150_000
"""Within this distance of the threshold, tool hooks check every call instead of every 30 s."""
_TAIL_MAX = 4 * 1024 * 1024
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
_BUSY_MARKERS = ("esc to interrupt", "Enter to confirm", "Esc to cancel", "-- NORMAL --", "Compacting")
_SPINNER_RE = re.compile(r"^[\u00b7\u2722\u2733\u2736\u273b\u273d*] \S[^(]*\u2026\s*\(\d")
"""A turn's spinner line, such as "· Photosynthesizing… (50s · ↓ 4.2k tokens)": it no longer says esc to interrupt.
Only at the line's start: the transcript indents what it quotes."""
_OPTION_LINE_RE = re.compile(r"^\u276f\s*\d+\.")
CHARS_PER_TOKEN = 4
IMAGE_TOKENS = 3_000
"""An image in a tool result costs the model a few thousand tokens however long its base64 is."""


@dataclass(frozen=True)
class ContextReading:
    tokens: int
    at_ms: int | None
    """When the API reported this usage, from the transcript entry."""


def read_context_tokens(path: Path) -> ContextReading | None:
    """Context size as the API last counted it: the newest main-thread assistant usage after the last compaction.

    One bounded tail read; a usage entry beyond it, or only before a compaction boundary, gives None.
    """
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            start = max(0, f.tell() - _TAIL_MAX)
            f.seek(start)
            lines = f.read().split(b"\n")
    except OSError:
        return None
    for raw in reversed(lines[1:] if start > 0 else lines):
        if b'"compact_boundary"' in raw:
            # Usage before the boundary describes the conversation as it was before compaction.
            return None
        reading = _map_line_to_reading(raw)
        if reading is not None:
            return reading
    return None


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
    usage = cast(dict[str, object], message).get("usage") if isinstance(message, dict) else None
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


def record_context(
    conn: sqlite3.Connection,
    conversation_id: str,
    reading: ContextReading,
    pending_tokens: int,
    compact_at: int,
    can_deliver: bool,
    driven: bool,
) -> str | None:
    """Stores the context size; at the threshold returns the save-your-notes request, once per compaction cycle.

    When bridge can drive the tab (agterm), the session is asked to end its turn so compaction follows at once.
    """
    row = conn.execute(
        "SELECT compact_warned_at, compacted_at FROM conversations WHERE conversation_id = ?", (conversation_id,)
    ).fetchone()
    if row is None:
        return None
    compacted_at = row["compacted_at"]
    if compacted_at is not None and reading.at_ms is not None and reading.at_ms < compacted_at:
        # The first hooks after compaction can run before Claude Code writes the boundary and read the old size.
        return None
    tokens = reading.tokens + pending_tokens
    conn.execute("UPDATE conversations SET context_tokens = ? WHERE conversation_id = ?", (tokens, conversation_id))
    if row["compact_warned_at"] is not None or not can_deliver or not compact_at or tokens < compact_at:
        return None
    conn.execute(
        "UPDATE conversations SET compact_warned_at = ?, compact_phase = ? WHERE conversation_id = ?",
        (tokens, PHASE_REQUESTED if driven else None, conversation_id),
    )
    return convert_tokens_to_request(tokens, driven)


def is_near_threshold(conn: sqlite3.Connection, conversation_id: str, compact_at: int) -> bool:
    row = conn.execute(
        "SELECT context_tokens, compact_warned_at FROM conversations WHERE conversation_id = ?", (conversation_id,)
    ).fetchone()
    if not compact_at or row is None or row["compact_warned_at"] is not None or row["context_tokens"] is None:
        return False
    return int(row["context_tokens"]) >= compact_at - NEAR_MARGIN_TOKENS


def convert_tokens_to_request(tokens: int, driven: bool) -> str:
    save = (
        f"[bridge] Context reached {tokens // 1000}k tokens: time to compact this conversation, and compaction "
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
    # The whole screen: a submitted prompt whose first line is above the tail still quotes, not shows, its markers.
    return not map_screen_to_busy(screen)


def map_screen_to_busy(screen: str) -> bool:
    """A spinner, a dialog or vim normal mode: keys sent now would not reach the input box as typed text.

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
    tail = [line for i, line in enumerate(lines) if i >= len(lines) - 14 and i not in quoted]
    return any(marker in line for line in tail for marker in _BUSY_MARKERS) or any(
        _SPINNER_RE.match(line) for line in tail
    )


def map_screen_to_draft(screen: str) -> str | None:
    """Text in Claude Code's input box (the last prompt line under a rule, up to the next rule); None without a box.

    Only a box that closes near the screen's foot counts, however far up a long text in a narrow pane makes it start.
    """
    lines = [line for line in screen.splitlines() if line.strip()]
    box = _find_input_box(lines)
    if box is None or box[1] < len(lines) - 14:
        return None
    body = [line.strip() for line in lines[box[0] : box[1]]]
    return " ".join(body).lstrip("\u276f").replace("\u00a0", " ").strip()


def map_screen_to_transcript(screen: str) -> str:
    """The screen without the input box's text."""
    lines = [line for line in screen.splitlines() if line.strip()]
    box = _find_input_box(lines)
    return "\n".join(lines[: box[0]] + lines[box[1] :] if box is not None else lines)


def _find_input_box(lines: list[str]) -> tuple[int, int] | None:
    """From the last prompt line under a rule up to, not including, the rule that closes it.

    A rule starts at the line's edge: the box indents its text, so a rule typed or wrapped into it is no edge.
    """
    starts = [
        i
        for i, line in enumerate(lines)
        if line.lstrip().startswith("\u276f") and i > 0 and lines[i - 1].startswith("\u2500")
    ]
    if not starts:
        return None
    for end in range(starts[-1], len(lines)):
        if lines[end].startswith("\u2500"):
            return starts[-1], end
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


def reset_after_compaction(conn: sqlite3.Connection, conversation_id: str, at_ms: int) -> None:
    conn.execute(
        "UPDATE conversations SET compact_warned_at = NULL, context_tokens = NULL, compacted_at = ?"
        " WHERE conversation_id = ?",
        (at_ms, conversation_id),
    )


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
