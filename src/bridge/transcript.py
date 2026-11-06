import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from bridge.config import claude_projects_dir

TAIL_BYTES = 2_000_000
# Claude Code writes the session's title entry on every turn, so the newest one is near the end.
TITLE_TAIL_BYTES = 256_000
_HARNESS_PREFIXES = ("<task-notification>", "<system-reminder>", "<local-command", "<command-name>", "Caveat:")


@dataclass(frozen=True)
class LastTurn:
    prompt: str | None
    reply: str | None


def find_transcript(conversation_id: str, known_path: str | None) -> Path | None:
    if known_path and Path(known_path).is_file():
        return Path(known_path)
    matches = list(claude_projects_dir().glob(f"*/{conversation_id}.jsonl"))
    return matches[0] if matches else None


def read_last_turn(path: Path) -> LastTurn:
    """The newest user-typed prompt and assistant text in a Claude transcript, read from its tail only."""
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - TAIL_BYTES))
        lines = f.read().decode(errors="replace").splitlines()
    if size > TAIL_BYTES:
        lines = lines[1:]
    prompt: str | None = None
    reply: str | None = None
    for line in lines:
        try:
            entry: object = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        data = cast(dict[str, object], entry)
        if data.get("isMeta") or data.get("isSidechain"):
            continue
        text = _text_of(data.get("message"))
        if not text:
            continue
        if data.get("type") == "user" and not text.lstrip().startswith(_HARNESS_PREFIXES):
            prompt = text
        elif data.get("type") == "assistant":
            reply = text
    return LastTurn(prompt=prompt, reply=reply)


def read_session_title(path: Path) -> str | None:
    """The session's name: the user's /rename if any, else Claude Code's generated title."""
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - TITLE_TAIL_BYTES))
        lines = f.read().decode(errors="replace").splitlines()
    custom: str | None = None
    generated: str | None = None
    for line in lines:
        if '"custom-title"' not in line and '"ai-title"' not in line:
            continue
        try:
            entry: object = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        data = cast(dict[str, object], entry)
        if data.get("type") == "custom-title" and isinstance(data.get("customTitle"), str):
            custom = str(data["customTitle"])
        elif data.get("type") == "ai-title" and isinstance(data.get("aiTitle"), str):
            generated = str(data["aiTitle"])
    title = " ".join((custom or generated or "").split())
    return title or None


def _text_of(message: object) -> str | None:
    if not isinstance(message, dict):
        return None
    content = cast(dict[str, object], message).get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    texts = [
        str(cast(dict[str, object], block).get("text", ""))
        for block in cast(list[object], content)
        if isinstance(block, dict) and cast(dict[str, object], block).get("type") == "text"
    ]
    joined = "\n".join(t for t in texts if t)
    return joined or None
