import json
import sqlite3
from dataclasses import dataclass, replace
from typing import cast

CLAUDE_CODE_MAX_CHARS = 10_000
"""Claude Code saves a hook's additionalContext over this to a file and shows the model only a 2 KB preview."""
INJECT_MAX_CHARS = 9_000
_LATER_LINE_MAX_CHARS = 500
_MIN_CUT_CHARS = 1_000
_SEPARATOR = "\n\n"


@dataclass(frozen=True)
class Part:
    text: str
    name: str = ""
    """What the session is told is still coming when the part waits, e.g. "your notes"."""
    command: str = ""
    """Prints the part's source now, for a session that does not want to wait."""
    rebuilt: bool = False
    """Made afresh at every session start, so a copy still waiting from before is dropped then."""


@dataclass(frozen=True)
class Packed:
    shown: list[Part]
    later: list[Part]

    @property
    def room(self) -> int:
        """Characters still free for one more part, the line about waiting parts reserved."""
        used = sum(measure_chars(p.text) + len(_SEPARATOR) for p in self.shown)
        return INJECT_MAX_CHARS - _LATER_LINE_MAX_CHARS - used - len(_SEPARATOR)

    @property
    def text(self) -> str:
        line = convert_parts_to_later_line(self.later)
        return _SEPARATOR.join([*(p.text for p in self.shown), *([line] if line else [])])


def measure_chars(text: str) -> int:
    """Length as Claude Code counts it: UTF-16 code units, the length of a JavaScript string."""
    return len(text.encode("utf-16-le")) // 2


def cut_to_chars(text: str, limit: int) -> str:
    return text.encode("utf-16-le")[: 2 * max(limit, 0)].decode("utf-16-le", errors="ignore")


def cut_to_last_chars(text: str, limit: int) -> str:
    encoded = text.encode("utf-16-le")
    return encoded[len(encoded) - 2 * max(limit, 0) :].decode("utf-16-le", errors="ignore") if limit > 0 else ""


def pack_parts(parts: list[Part], budget: int = INJECT_MAX_CHARS) -> Packed:
    """Takes parts in priority order while they fit; the rest waits for the next event.

    A part too big for any message is cut, pointing at its command, rather than waiting forever.
    """
    room = budget - _LATER_LINE_MAX_CHARS
    shown: list[Part] = []
    later: list[Part] = []
    used = 0
    for part in parts:
        size = measure_chars(part.text) + len(_SEPARATOR)
        if used + size <= room:
            shown.append(part)
            used += size
        elif size > room and room - used >= _MIN_CUT_CHARS:
            cut = _cut_part(part, room - used - len(_SEPARATOR))
            shown.append(cut)
            used += measure_chars(cut.text) + len(_SEPARATOR)
        else:
            later.append(part)
    return Packed(shown, later)


def add_part(packed: Packed, part: Part) -> Packed:
    """One more part after packing: shown if it fits the room left, else it waits."""
    if measure_chars(part.text) <= packed.room:
        return Packed([*packed.shown, part], packed.later)
    return Packed(packed.shown, [*packed.later, part])


def _cut_part(part: Part, limit: int) -> Part:
    where = f"`{part.command}` prints all of it" if part.command else "the rest did not fit"
    marker = f"\n… (cut here to fit Claude Code's limit; {where})"
    return replace(part, text=cut_to_chars(part.text, limit - measure_chars(marker)) + marker)


def convert_parts_to_later_line(parts: list[Part]) -> str:
    if not parts:
        return ""
    names = list(dict.fromkeys(p.name or "other bridge notices" for p in parts))
    commands = list(dict.fromkeys(f"`{p.command}`" for p in parts if p.command))
    now = f" Now: {', '.join(commands)}." if commands else ""
    line = (
        f"[bridge] Left out of this message to stay under Claude Code's {CLAUDE_CODE_MAX_CHARS:,}-character limit, "
        f"and given with your next prompt or tool result: {'; '.join(names)}.{now}"
    )
    return cut_to_chars(line, _LATER_LINE_MAX_CHARS)


def cap_context(text: str) -> str:
    """The last guard: whatever was assembled, Claude Code never gets more than its limit with margin."""
    if measure_chars(text) <= INJECT_MAX_CHARS:
        return text
    marker = "\n… (bridge cut this message to fit Claude Code's limit)"
    return cut_to_chars(text, INJECT_MAX_CHARS - measure_chars(marker)) + marker


def take_later_parts(conn: sqlite3.Connection, conversation_id: str) -> list[Part]:
    row = conn.execute(
        "SELECT later_context FROM conversations WHERE conversation_id = ?", (conversation_id,)
    ).fetchone()
    if row is None or not row["later_context"]:
        return []
    conn.execute("UPDATE conversations SET later_context = NULL WHERE conversation_id = ?", (conversation_id,))
    return _map_json_to_parts(str(row["later_context"]))


def save_later_parts(conn: sqlite3.Connection, conversation_id: str, parts: list[Part]) -> None:
    text = json.dumps([[p.text, p.name, p.command, p.rebuilt] for p in parts], ensure_ascii=False) if parts else None
    conn.execute("UPDATE conversations SET later_context = ? WHERE conversation_id = ?", (text, conversation_id))


def _map_json_to_parts(text: str) -> list[Part]:
    try:
        raw: object = json.loads(text)
    except ValueError:
        return []
    if not isinstance(raw, list):
        return []
    parts: list[Part] = []
    for item in cast(list[object], raw):
        fields = cast(list[object], item) if isinstance(item, list) else []
        if len(fields) == 4 and all(isinstance(f, str) for f in fields[:3]):
            parts.append(Part(str(fields[0]), str(fields[1]), str(fields[2]), bool(fields[3])))
    return parts
