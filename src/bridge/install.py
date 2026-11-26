import json
import os
import re
import shlex
import shutil
import time
from pathlib import Path
from typing import cast

from bridge.agent_prefix import LINEAR_ISSUE_TOOLS, ROBOT
from bridge.artifacts import ARTIFACT_TOOL, DRIVE_CREATE_TOOL
from bridge.config import state_dir
from bridge.guard import DEFAULT_TRIGGERS, PUSH_TRIGGER, convert_trigger_to_glob
from bridge.hooks import ARTIFACT_HOOK, HOOK_EVENTS, SKILL_TOOL
from bridge.rewake_marks import WAITER_SCRIPT

HOOK_TIMEOUT_S = 5
# A waiter lives as long as its idle tab; Claude Code ends it with the session.
REWAKE_TIMEOUT_S = 2_000_000
REWAKE_SUMMARY = "bridge mail"
REWAKE_EVENTS = ("SessionStart", "Stop")
BUSY_MARK_COMMAND = ': > "${BRIDGE_STATE_DIR:-$HOME/.local/state/bridge}/rewake/$CLAUDE_PID.busy" 2>/dev/null; exit 0'
_OURS_RE = re.compile(
    r"bridge-hook['\"]?\s+[A-Za-z]+|" + "|".join(re.escape(mark) for mark in (WAITER_SCRIPT, BUSY_MARK_COMMAND))
)
_GUARDED_EVENTS = frozenset({"PostToolUse", "PostToolUseFailure"})
_MATCHERS = {
    "Notification": "permission_prompt",
    "PreToolUse": "|".join(("Bash", ARTIFACT_TOOL, SKILL_TOOL, *LINEAR_ISSUE_TOOLS)),
}
# A robot-prefixed title reaches Python with no claim; a payload may carry the robot raw or JSON-escaped.
_ROBOT_GLOBS = f"*{ROBOT}*|*\\\\ud83e\\\\udd16*"
# So does any gh command that may set or remove a label: which label is the owner's is read in Python.
_LABEL_GLOBS = ("*gh*label*", "*gh*create*-*l*", "*gh*new*-*l*")
_PR_ISSUE_GLOBS = ("*gh*pr*create*", "*gh*pr*new*", "*gh*pr*edit*")
# A comment, review or thread reply on a PR: it may answer a colleague's comment no call handles yet.
_PR_ANSWER_GLOBS = ("*gh*pr*comment*", "*gh*pr*review*", "*gh*api*comments*", "*gh*api*reviews*")
# A gh call that spends core quota: a worker's is refused when the shared quota is low.
_GH_QUOTA_GLOBS = tuple(f"*gh*{sub}*" for sub in ("api", "run", "pr", "issue", "repo", "release", "search"))
# Killing processes by name or pattern is refused whether or not a claim exists.
_KILL_GLOBS = ("*pkill*", "*killall*", "*pgrep*")
# So is a kubectl, helm or k9s command that names no cluster, and one that changes the machine's default cluster.
_CLUSTER_GLOBS = ("*kubectl*", "*kubectx*", "*kubens*", "*helm*", "*k9s*")
# Claude's login (logout, its keychain entry, /logout typed into a tab), a '!' typed into a tab and AWS permission
# probes are refused too.
# Each names its program; "claude" must not be followed by / or -, as in the payload's transcript path.
_LOGIN_PROBE_GLOBS = (
    "*claude[!/-]*logout*", "*security*-password*laude*", "*agtermctl*logout*", "*tmux*send*logout*", "*aws*simulate-*",
    "*agtermctl*!*", "*tmux*send*!*",
)  # fmt: skip
# So is admin use on GitHub: gh pr merge --admin, a merge through the API, a ruleset or branch protection write.
_ADMIN_GLOBS = ("*gh*merge*admin*", "*gh*admin*merge*", "*gh*api*merge*", "*gh*api*uleset*", "*gh*api*rotection*")
# And gh run watch polling faster than once a minute: the account's API limit is shared.
_RUN_WATCH_GLOBS = ("*gh*run*watch*",)
# A checker piped into a filter loses its exit code; the hook adds a note, it never refuses.
_PIPE_GLOBS = (
    "*pre-commit*", "*pytest*", "*ruff*", "*mypy*", "*pyright*", "*git*commit*", "*npm*", "*pnpm*", "*yarn*",
    "*go*test*", "*cargo*", "*jest*", "*vitest*", "*eslint*", "*tsc*", "*tox*", "*nox*", "*make*",
)  # fmt: skip
_HEAVY_GLOBS = ("*uv*sync*", "*docker*build*")
"""Heavy jobs the pipe globs miss; only the Mac's gate has them, since a host's own load is not the Mac's."""
_CODEX_READ_GLOBS = ("*git*rebase*", "*git*reset*", "*git*checkout*", "*git*switch*", "*git*pull*")
"""Moves of a worktree's files, refused while Codex reads it; only the Mac's gate has them, as Codex runs there."""
_PYTHON_GLOBS = (
    *_LABEL_GLOBS, *_PR_ISSUE_GLOBS, *_PR_ANSWER_GLOBS, *_GH_QUOTA_GLOBS, *_KILL_GLOBS, *_CLUSTER_GLOBS,
    *_LOGIN_PROBE_GLOBS, *_ADMIN_GLOBS, *_RUN_WATCH_GLOBS, *_PIPE_GLOBS, *_HEAVY_GLOBS, *_CODEX_READ_GLOBS,
)  # fmt: skip
# A publish to claude.ai, matched by the tool's own field so a command that names the tool does not start Python; not a
# host glob, since gbridge sends every Artifact call to the Mac.
_ARTIFACT_GLOBS = f"""*'"tool_name":"{ARTIFACT_TOOL}"'*|*'"tool_name": "{ARTIFACT_TOOL}"'*"""
_SKILL_GLOBS = f"""*'"tool_name":"{SKILL_TOOL}"'*|*'"tool_name": "{SKILL_TOOL}"'*"""

type JsonObject = dict[str, object]


def install_hooks(
    settings_path: Path,
    executable: str,
    waiter_script: Path | None = None,
) -> list[str]:
    """waiter_script, the installed copy of rewake.sh, adds the hooks that deliver mail to an idle tab as a new turn."""
    settings = _load(settings_path)
    hooks = _strip_ours(settings)
    for event in HOOK_EVENTS:
        groups = _groups(hooks, event)
        command = convert_event_to_command(executable, event)
        group: JsonObject = {"hooks": [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT_S}]}
        if event in _MATCHERS:
            group["matcher"] = _MATCHERS[event]
        groups.append(group)
        hooks[event] = groups
    artifact: JsonObject = {
        "matcher": f"{ARTIFACT_TOOL}|{DRIVE_CREATE_TOOL}",
        "hooks": [
            {"type": "command", "command": f"{shlex.quote(executable)} {ARTIFACT_HOOK}", "timeout": HOOK_TIMEOUT_S}
        ],
    }
    hooks["PostToolUse"] = [*_groups(hooks, "PostToolUse"), artifact]
    if waiter_script is not None:
        for event, group in map_waiter_to_hook_groups(waiter_script, executable).items():
            hooks[event] = [*_groups(hooks, event), group]
    settings["hooks"] = hooks
    _save(settings_path, settings)
    return list(HOOK_EVENTS)


def map_waiter_to_hook_groups(waiter_script: Path, executable: str) -> dict[str, JsonObject]:
    """A waiter starts with the session and at each turn's end; a prompt marks the tab busy, in the shell alone."""
    groups: dict[str, JsonObject] = {
        event: {
            "hooks": [
                {
                    "type": "command",
                    "command": f"sh {shlex.quote(str(waiter_script))} {shlex.quote(executable)} {event}",
                    "asyncRewake": True,
                    "rewakeSummary": REWAKE_SUMMARY,
                    "timeout": REWAKE_TIMEOUT_S,
                }
            ]
        }
        for event in REWAKE_EVENTS
    }
    groups["UserPromptSubmit"] = {
        "hooks": [{"type": "command", "command": BUSY_MARK_COMMAND, "timeout": HOOK_TIMEOUT_S}]
    }
    return groups


def convert_event_to_command(executable: str, event: str) -> str:
    direct = f"{shlex.quote(executable)} {event}"
    if event == "PreToolUse":
        # Every shell command passes through here; Python starts only for a robot (a title to check), a push (an empty
        # file to refuse) or, if a claim exists, for text that could match a trigger.
        cases = "|".join(convert_trigger_to_glob(t) for t in DEFAULT_TRIGGERS)
        # exec in a pipeline replaces only its subshell, so exit before the claims gate runs the hook again.
        run = f"printf '%s' \"$p\" | {direct}; exit 0;;"
        push = convert_trigger_to_glob(PUSH_TRIGGER)
        always = "|".join([_ROBOT_GLOBS, _ARTIFACT_GLOBS, _SKILL_GLOBS, push, *_PYTHON_GLOBS])
        return (
            f'p=$(cat); case "$p" in {always}) {run} esac; '
            '[ -f "${BRIDGE_STATE_DIR:-$HOME/.local/state/bridge}/claims-active" ] || exit 0; '
            f'case "$p" in {cases}) {run} esac; exit 0'
        )
    if event not in _GUARDED_EVENTS:
        return direct
    # Tool hooks fire constantly; a shell test of the hint file keeps the common case to a few ms.
    return (
        'h="${BRIDGE_STATE_DIR:-$HOME/.local/state/bridge}/hints/$CLAUDE_PID"; '
        '{ [ ! -f "$h" ] || { read -r due < "$h"; [ "$(date +%s)" -ge "${due:-0}" ]; }; } && exec '
        f"{direct}; exit 0"
    )


def list_guard_globs() -> list[str]:
    """The shell guard's gate: a command matching none of these cannot push, merge, set a robot-prefixed title, change
    a label, open or edit a PR, comment on one, kill processes by name, reach a Kubernetes cluster, touch Claude's
    login, probe AWS permissions, use admin rights on GitHub or watch a run."""
    # The host's gate matches a literal pipe, so a checker run without one is not sent to the Mac.
    plain = [g for g in _PYTHON_GLOBS if g not in (*_PIPE_GLOBS, *_HEAVY_GLOBS, *_CODEX_READ_GLOBS)]
    return [
        *(convert_trigger_to_glob(t) for t in DEFAULT_TRIGGERS),
        f"*{ROBOT}*",
        *plain,
        *(f"{g}|*" for g in _PIPE_GLOBS),
    ]


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
