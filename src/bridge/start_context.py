import contextlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from bridge.compaction import convert_keep_to_context, load_keep
from bridge.config import Config, state_dir
from bridge.dream import read_dream
from bridge.inject import Packed, Part
from bridge.leads import (
    Lead,
    assistant_dir,
    convert_config_to_assistant_role,
    convert_lead_to_role,
    convert_notes_to_lead_section,
    convert_notes_to_section,
    find_lead_on_surface,
    is_assistant_surface,
    is_conversation_locked,
    is_surface_locked,
    lead_dir,
    list_leads,
    read_notes,
)

UNREADABLE_START_LINE = (
    "[bridge] This session started (or was compacted) while bridge's database was busy, so the notes you saved with "
    "`bridge keep`, your role and your notes did not reach you; bridge gives them with your next prompt or tool "
    "result. Now: `bridge keep --show`, `guild notes`."
)
KEEP_COMMAND = "bridge keep --show"
_SAFE_ID_RE = re.compile(r"^[\w.-]{1,200}$")


@dataclass(frozen=True)
class PendingStart:
    """A session start whose database work failed, for the conversation's next hook to make up."""

    source: str
    at_ms: int
    shown: bool
    """The start's parts reached the session, read without the write lock; only recording them is left."""
    later: list[Part]
    """What did not fit that message, to wait in later_context like any other part."""


def convert_keep_to_part(text: str, saved_at: int, at_ms: int) -> Part:
    return Part(convert_keep_to_context(text, saved_at, at_ms), "the notes you saved with `bridge keep`", KEEP_COMMAND)


def map_packed_to_keep_status(keep: Part | None, packed: Packed | None) -> str:
    """For the typed "continue": it may say the list is above only when the whole list went into this message."""
    if keep is None:
        return "none"
    return "shown" if packed is not None and keep in packed.shown else "cut"


def convert_lead_to_parts(lead: Lead, config: Config) -> list[Part]:
    parts = [Part(convert_lead_to_role(lead, config), "your lead role", rebuilt=True)]
    if dream := read_dream(lead_dir(lead.name)):
        parts.append(Part(dream, "your dream from last night", rebuilt=True))
    notes = convert_notes_to_lead_section(read_notes(lead_dir(lead.name)))
    parts.append(Part(notes, "your notes", "guild notes", True))
    return parts


def convert_assistant_to_parts(conn: sqlite3.Connection, config: Config) -> list[Part]:
    parts = [Part(convert_config_to_assistant_role(config, list_leads(conn)), "your role", rebuilt=True)]
    if dream := read_dream(assistant_dir()):
        parts.append(Part(dream, "your dream from last night", rebuilt=True))
    if (notes := read_notes(assistant_dir())).strip():
        parts.append(Part(convert_notes_to_section(notes), "your notes", "guild notes", True))
    return parts


def read_start_parts(
    conn: sqlite3.Connection, config: Config, surface: str | None, conversation_id: str, source: str, at_ms: int
) -> list[Part]:
    """What a session start gives, by reads alone, in the order the hook packs it: keep, role, dream, notes."""
    if is_conversation_locked(conn, config, conversation_id):
        return []
    parts: list[Part] = []
    if source == "compact" and (kept := load_keep(conn, conversation_id)):
        parts.append(convert_keep_to_part(kept[0], kept[1], at_ms))
    lead = None if is_surface_locked(conn, config, surface) else find_lead_on_surface(conn, surface)
    if lead is not None:
        parts += convert_lead_to_parts(lead, config)
    elif is_assistant_surface(conn, config, surface):
        parts += convert_assistant_to_parts(conn, config)
    return parts


def pending_start_path(conversation_id: str) -> Path | None:
    return state_dir() / "start-pending" / f"{conversation_id}.json" if _SAFE_ID_RE.match(conversation_id) else None


def write_pending_start(conversation_id: str, pending: PendingStart) -> None:
    path = pending_start_path(conversation_id)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    later = [[p.text, p.name, p.command, p.rebuilt] for p in pending.later]
    data = {"source": pending.source, "at_ms": pending.at_ms, "shown": pending.shown, "later": later}
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(path)


@dataclass(frozen=True)
class TakenStart:
    pending: PendingStart
    path: Path
    """The marker renamed for this process: a newer start's marker can still be written beside it."""


def has_pending_start(conversation_id: str) -> bool:
    path = pending_start_path(conversation_id)
    return path is not None and (path.exists() or bool(_list_orphans(path)))


def take_pending_start(conversation_id: str) -> TakenStart | None:
    """The newest start waiting, or one whose taker died before it could record it; older ones are dropped."""
    path = pending_start_path(conversation_id)
    if path is None:
        return None
    taken = path.with_name(f"{path.name}.taken-{os.getpid()}-{time.time_ns()}")
    for candidate in [path, *_list_orphans(path)]:
        try:
            candidate.rename(taken)
        except OSError:
            continue
        for orphan in _list_orphans(path):
            orphan.unlink(missing_ok=True)
        pending = _map_text_to_pending(taken.read_text())
        if pending is None:
            taken.unlink(missing_ok=True)
            return None
        return TakenStart(pending, taken)
    return None


def finish_taken_start(taken: TakenStart) -> None:
    taken.path.unlink(missing_ok=True)


def put_back_taken_start(conversation_id: str, taken: TakenStart) -> None:
    """Back under its own name, unless a newer start's marker took that name meanwhile."""
    path = pending_start_path(conversation_id)
    if path is not None:
        with contextlib.suppress(FileExistsError):
            os.link(taken.path, path)
    taken.path.unlink(missing_ok=True)


def clear_pending_start(conversation_id: str) -> None:
    path = pending_start_path(conversation_id)
    if path is not None:
        for stale in [path, *_list_orphans(path)]:
            stale.unlink(missing_ok=True)


def _list_orphans(path: Path) -> list[Path]:
    if not path.parent.exists():
        return []
    return [p for p in path.parent.glob(f"{path.name}.taken-*") if not _is_alive(p.name.rsplit("taken-", 1)[1])]


def _is_alive(taker: str) -> bool:
    pid = taker.split("-", 1)[0]
    if not pid.isdigit():
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _map_text_to_pending(text: str) -> PendingStart | None:
    try:
        raw: object = json.loads(text)
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    data = cast(dict[str, object], raw)
    source, at_ms, shown, later = data.get("source"), data.get("at_ms"), data.get("shown"), data.get("later")
    if not isinstance(source, str) or not isinstance(at_ms, int) or not isinstance(later, list):
        return None
    parts = [
        Part(str(f[0]), str(f[1]), str(f[2]), bool(f[3]))
        for item in cast(list[object], later)
        if isinstance(item, list) and len(f := cast(list[object], item)) == 4
    ]
    return PendingStart(source, at_ms, shown is True, parts)
