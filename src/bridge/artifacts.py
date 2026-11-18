import json
import re
import sqlite3
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from bridge.config import Config
from bridge.leads import find_conversation_tab
from bridge.pages import is_artifact_url
from bridge.pins import Pin, PinError, record_artifact_publish
from bridge.registry import map_env_to_surface

ARTIFACT_TOOL = "Artifact"
DRIVE_CREATE_TOOL = "mcp__claude_ai_Google_Drive__create_file"
_DRIVE_EDITORS = {
    "application/vnd.google-apps.document": "document",
    "application/vnd.google-apps.presentation": "presentation",
    "application/vnd.google-apps.spreadsheet": "spreadsheets",
}
UNKNOWN_SESSION = "a session bridge never saw"
# A result with no structured payload still says where it went: "Published <path> at <url> (Version …".
_PUBLISHED_AT = re.compile(r"\bPublished \S.*? at (https://claude\.ai/(?:code/)?artifact/[A-Za-z0-9_-]+)")


@dataclass(frozen=True)
class ArtifactPublish:
    url: str
    title: str


@dataclass(frozen=True)
class TranscriptPublish:
    conversation_id: str
    at_ms: int
    publish: ArtifactPublish


@dataclass(frozen=True)
class TranscriptScan:
    publishes: list[TranscriptPublish]
    undated: int


@dataclass(frozen=True)
class BackfillResult:
    stored: list[tuple[TranscriptPublish, Pin]]
    locked: int
    unknown_tab: int
    held_by_page: int
    refused: list[str]


def map_tool_call_to_publish(tool_input: object, tool_response: object) -> ArtifactPublish | None:
    """A page or a type-made Artifact that a publish created or updated; None for reads, lists, asset uploads and
    failures."""
    given = _as_object(tool_input)
    if given is None or given.get("action") not in (None, "publish") or given.get("asset"):
        return None
    response = _as_object(tool_response)
    url: object = None
    title: object = None
    if response is not None:
        url, title = response.get("url"), response.get("title")
    elif match := _PUBLISHED_AT.search(_map_response_to_text(tool_response)):
        url = match.group(1)
    if not isinstance(url, str) or not is_artifact_url(url):
        return None
    for candidate in (title, given.get("title")):
        if isinstance(candidate, str) and candidate.strip():
            return ArtifactPublish(url, candidate)
    path = given.get("file_path")
    return ArtifactPublish(url, Path(path).stem if isinstance(path, str) and path else url)


def map_drive_create_to_publish(tool_response: object) -> ArtifactPublish | None:
    """The created file's link, built from its id: the connector's answer carries no view URL."""
    created = _as_object(tool_response)
    if created is None:
        try:
            created = _as_object(json.loads(_map_response_to_text(tool_response)))
        except ValueError:
            return None
    if created is None or not isinstance(file_id := created.get("id"), str) or not file_id:
        return None
    editor = _DRIVE_EDITORS.get(str(created.get("mimeType") or ""))
    url = (
        f"https://docs.google.com/{editor}/d/{file_id}/edit"
        if editor
        else f"https://drive.google.com/file/d/{file_id}/view"
    )
    title = created.get("title")
    return ArtifactPublish(url, title if isinstance(title, str) and title.strip() else url)


def _as_object(value: object) -> dict[str, object] | None:
    return cast(dict[str, object], value) if isinstance(value, dict) else None


def _map_response_to_text(response: object) -> str:
    if isinstance(response, str):
        return response
    if isinstance(response, list):
        parts = (_as_object(part) for part in cast(list[object], response))
        return "\n".join(str(part.get("text", "")) for part in parts if part is not None)
    return ""


def capture_artifact_publish(
    conn: sqlite3.Connection, config: Config, payload: Mapping[str, object], env: Mapping[str, str], at_ms: int
) -> Pin | None:
    """Pins what a session's Artifact tool published or its Google Drive connector created, unless its tab or
    workspace is private (🔒)."""
    tool = payload.get("tool_name")
    if tool == ARTIFACT_TOOL:
        publish = map_tool_call_to_publish(payload.get("tool_input"), payload.get("tool_response"))
    elif tool == DRIVE_CREATE_TOOL:
        publish = map_drive_create_to_publish(payload.get("tool_response"))
    else:
        return None
    if publish is None:
        return None
    tab = find_conversation_tab(conn, str(payload.get("session_id") or ""))
    surface = map_env_to_surface(env)
    here = (
        conn.execute("SELECT tab_name, workspace_name FROM surfaces WHERE surface = ?", (surface.surface,)).fetchone()
        if surface is not None
        else None
    )
    if (surface is not None and here is None) or any(_may_be_private(config, row) for row in (tab, here)):
        return None
    by = str(tab["handle"]) if tab is not None and tab["handle"] else UNKNOWN_SESSION
    return record_artifact_publish(conn, publish.url, publish.title, by, at_ms)


def _may_be_private(config: Config, row: sqlite3.Row | None) -> bool:
    """A tab bridge has not read the names of yet may be a 🔒 one."""
    if row is None:
        return False
    names = (row["tab_name"], row["workspace_name"])
    return all(name is None for name in names) or config.hides(*names)


def scan_transcripts(root: Path, since_ms: int) -> TranscriptScan:
    """Each Artifact publish in Claude Code transcripts under root, at the time its result was written."""
    publishes: list[TranscriptPublish] = []
    undated = 0
    for path in sorted(root.glob("**/*.jsonl")):
        try:
            if path.stat().st_mtime * 1000 < since_ms:
                continue
        except OSError:
            continue
        for entry in _read_tool_results(path):
            conversation_id, timestamp, publish = entry
            if timestamp is None:
                undated += 1
                continue
            if timestamp >= since_ms:
                publishes.append(TranscriptPublish(conversation_id, timestamp, publish))
    publishes.sort(key=lambda p: p.at_ms)
    return TranscriptScan(publishes, undated)


def _read_tool_results(path: Path) -> Iterator[tuple[str, int | None, ArtifactPublish]]:
    uses: dict[str, object] = {}
    try:
        lines = path.open(errors="replace")
    except OSError:
        return
    with lines:
        for line in lines:
            if '"Artifact"' not in line and "claude.ai/" not in line:
                continue
            try:
                entry = _as_object(json.loads(line))
            except ValueError:
                continue
            message = _as_object(entry.get("message")) if entry is not None else None
            content = message.get("content") if message is not None else None
            if entry is None or not isinstance(content, list):
                continue
            for part in (_as_object(p) for p in cast(list[object], content)):
                if part is None:
                    continue
                if part.get("type") == "tool_use" and part.get("name") == ARTIFACT_TOOL:
                    uses[str(part.get("id"))] = part.get("input")
                elif part.get("type") == "tool_result" and str(part.get("tool_use_id")) in uses:
                    if part.get("is_error"):
                        continue
                    response = entry.get("toolUseResult") or part.get("content")
                    publish = map_tool_call_to_publish(uses[str(part.get("tool_use_id"))], response)
                    if publish is not None:
                        conversation_id = str(entry.get("sessionId") or path.stem)
                        yield conversation_id, _map_timestamp_to_ms(entry.get("timestamp")), publish


def _map_timestamp_to_ms(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def backfill_artifact_publishes(
    conn: sqlite3.Connection, config: Config, publishes: list[TranscriptPublish]
) -> BackfillResult:
    """Oldest first, as the hook would have pinned them; a conversation whose tab is unknown now is left out, since
    nothing shows it was not private."""
    stored: list[tuple[TranscriptPublish, Pin]] = []
    locked = unknown_tab = held_by_page = 0
    refused: list[str] = []
    for item in publishes:
        tab = find_conversation_tab(conn, item.conversation_id)
        if tab is None:
            unknown_tab += 1
            continue
        if _may_be_private(config, tab):
            locked += 1
            continue
        by = str(tab["handle"]) if tab["handle"] else UNKNOWN_SESSION
        try:
            pin = record_artifact_publish(conn, item.publish.url, item.publish.title, by, item.at_ms)
        except PinError as error:
            refused.append(f"{item.publish.url}: {error}")
            continue
        if pin is None:
            held_by_page += 1
        else:
            stored.append((item, pin))
    return BackfillResult(stored, locked, unknown_tab, held_by_page, refused)
