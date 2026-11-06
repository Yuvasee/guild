import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from bridge.config import claude_sessions_dir


@dataclass(frozen=True)
class ClaudeSessionFile:
    pid: int
    session_id: str
    cwd: str | None
    started_at_ms: int | None
    kind: str | None
    name: str | None
    status: str | None
    status_updated_at_ms: int | None
    version: str | None = None
    peer_messaging: bool = False


def read_session_file(pid: int, sessions_dir: Path | None = None) -> ClaudeSessionFile | None:
    path = (sessions_dir or claude_sessions_dir()) / f"{pid}.json"
    try:
        raw: object = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return map_json_to_session_file(raw)


def list_session_files(sessions_dir: Path | None = None) -> list[ClaudeSessionFile]:
    directory = sessions_dir or claude_sessions_dir()
    result: list[ClaudeSessionFile] = []
    for path in directory.glob("*.json"):
        if not path.stem.isdigit():
            continue
        item = read_session_file(int(path.stem), directory)
        if item is not None:
            result.append(item)
    return result


def map_json_to_session_file(raw: object) -> ClaudeSessionFile | None:
    if not isinstance(raw, dict):
        return None
    data = cast(dict[str, object], raw)
    pid = data.get("pid")
    session_id = data.get("sessionId")
    if not isinstance(pid, int) or not isinstance(session_id, str):
        return None
    return ClaudeSessionFile(
        pid=pid,
        session_id=session_id,
        cwd=_opt_str(data.get("cwd")),
        started_at_ms=_opt_int(data.get("startedAt")),
        kind=_opt_str(data.get("kind")),
        name=_opt_str(data.get("name")),
        status=_opt_str(data.get("status")),
        status_updated_at_ms=_opt_int(data.get("statusUpdatedAt")),
        version=_opt_str(data.get("version")),
        peer_messaging=isinstance(data.get("messagingSocketPath"), str),
    )


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _opt_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
