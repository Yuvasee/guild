import json
import os
import re
import shlex
import shutil
import time
from pathlib import Path
from typing import cast

from bridge.config import state_dir
from bridge.guard import DEFAULT_TRIGGERS
from bridge.hooks import HOOK_EVENTS

HOOK_TIMEOUT_S = 5
_OURS_RE = re.compile(r"bridge-hook['\"]?\s+[A-Za-z]+")
_GUARDED_EVENTS = frozenset({"PostToolUse", "PostToolUseFailure"})
_MATCHERS = {"Notification": "permission_prompt", "PreToolUse": "Bash"}

type JsonObject = dict[str, object]


def install_hooks(settings_path: Path, executable: str, triggers: tuple[str, ...] = DEFAULT_TRIGGERS) -> list[str]:
    settings = _load(settings_path)
    hooks = _strip_ours(settings)
    for event in HOOK_EVENTS:
        groups = _groups(hooks, event)
        command = convert_event_to_command(executable, event, triggers)
        group: JsonObject = {"hooks": [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT_S}]}
        if event in _MATCHERS:
            group["matcher"] = _MATCHERS[event]
        groups.append(group)
        hooks[event] = groups
    settings["hooks"] = hooks
    _save(settings_path, settings)
    return list(HOOK_EVENTS)


def convert_event_to_command(executable: str, event: str, triggers: tuple[str, ...] = DEFAULT_TRIGGERS) -> str:
    direct = f"{shlex.quote(executable)} {event}"
    if event == "PreToolUse":
        # Every shell command passes through here; Python starts only if a claim or a held PR exists and the
        # text could match.
        cases = "|".join(convert_trigger_to_glob(t) for t in dict.fromkeys(triggers))
        return (
            '[ -f "${BRIDGE_STATE_DIR:-$HOME/.local/state/bridge}/claims-active" ] || exit 0; p=$(cat); '
            f'case "$p" in {cases}) printf \'%s\' "$p" | exec {direct};; esac; exit 0'
        )
    if event not in _GUARDED_EVENTS:
        return direct
    # Tool hooks fire constantly; a shell test of the hint file keeps the common case to a few ms.
    return (
        'h="${BRIDGE_STATE_DIR:-$HOME/.local/state/bridge}/hints/$CLAUDE_PID"; '
        '{ [ ! -f "$h" ] || { read -r due < "$h"; [ "$(date +%s)" -ge "${due:-0}" ]; }; } && exec '
        f"{direct}; exit 0"
    )


def convert_trigger_to_glob(trigger: str) -> str:
    """An unquoted case pattern: `*` stays a wildcard, spaces match any run of characters."""
    if not re.fullmatch(r"[A-Za-z0-9 *_./-]+", trigger):
        raise ValueError(f"guard trigger {trigger!r} may only use letters, digits, spaces and * _ . / -")
    return "*" + "*".join(trigger.split()) + "*"


def uninstall_hooks(settings_path: Path) -> None:
    settings = _load(settings_path)
    settings["hooks"] = _strip_ours(settings)
    _save(settings_path, settings)


def find_hook_executable() -> str:
    found = shutil.which("bridge-hook")
    if found is None:
        raise RuntimeError("bridge-hook is not on PATH; install with `uv tool install -e <repo>`")
    return found


def _strip_ours(settings: JsonObject) -> JsonObject:
    raw_hooks = settings.get("hooks", {})
    hooks: JsonObject = dict(cast(JsonObject, raw_hooks)) if isinstance(raw_hooks, dict) else {}
    for event in list(hooks):
        kept_groups: list[object] = []
        for group in _groups(hooks, event):
            if not isinstance(group, dict):
                kept_groups.append(group)
                continue
            group_obj = cast(JsonObject, group)
            raw_entries = group_obj.get("hooks", [])
            entries = cast(list[object], raw_entries) if isinstance(raw_entries, list) else []
            kept = [e for e in entries if not _is_ours(e)]
            if kept:
                kept_groups.append({**group_obj, "hooks": kept})
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    return hooks


def _is_ours(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    command = cast(JsonObject, entry).get("command")
    return isinstance(command, str) and bool(_OURS_RE.search(command))


def _groups(hooks: JsonObject, event: str) -> list[object]:
    raw = hooks.get(event, [])
    return list(cast(list[object], raw)) if isinstance(raw, list) else []


def _load(path: Path) -> JsonObject:
    if not path.exists():
        return {}
    raw: object = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path} is not a JSON object")
    return cast(JsonObject, raw)


def _save(path: Path, settings: JsonObject) -> None:
    if path.exists():
        backups = state_dir() / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backups / f"{path.name}.{time.strftime('%Y%m%d-%H%M%S')}")
    tmp = path.with_name(f".{path.name}.bridge-tmp")
    tmp.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
