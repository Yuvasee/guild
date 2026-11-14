import shlex
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass

_DROP_WITH_VALUE = frozenset({"--session-id"})
_DROP_WITH_OPTIONAL_VALUE = frozenset({"-r", "--resume", "--from-pr", "--teleport", "--cloud"})
_DROP_FLAGS = frozenset({"-c", "--continue", "--fork-session", "-p", "--print", "--bg", "--background"})
_VALUED = frozenset(
    {
        "--agent", "--agents", "--append-system-prompt", "--append-system-prompt-file", "--autocompact",
        "--debug-file", "--effort", "--environment", "--fallback-model", "--input-format",
        "--json-schema", "--max-budget-usd", "--model", "-n", "--name", "--output-format",
        "--permission-mode", "--permission-prompts", "--permission-prompt-tool", "--plugin-url",
        "--remote-control-session-name-prefix", "--setting-sources", "--settings", "--system-prompt",
        "--system-prompt-file", "--system-prompt-snapshot",
    }
)  # fmt: skip
_OPTIONAL_VALUED = frozenset({"-d", "--debug", "--prompt-suggestions", "--remote-control", "-w", "--worktree"})
_VARIADIC = frozenset(
    {
        "--add-dir", "--allowedTools", "--allowed-tools", "--betas", "--disallowedTools",
        "--disallowed-tools", "--file", "--mcp-config", "--plugin-dir", "--tools",
    }
)  # fmt: skip


_ALLOWED_TOOLS = frozenset({"--allowedTools", "--allowed-tools"})
_SELF_GRANTED_TOOLS = "mcp__computer-use__"


@dataclass(frozen=True)
class PinResult:
    command: str
    ok: bool
    detail: str


def build_restore_command(argv: list[str], conversation_id: str) -> str:
    """Rebuilds a claude launch line that resumes conversation_id, dropping positional prompts."""
    if not argv:
        raise ValueError("empty argv")
    executable = argv[0] if "/" in argv[0] else (shutil.which(argv[0]) or argv[0])
    kept: list[str] = [executable]
    args = argv[1:]
    i = 0
    while i < len(args):
        token = args[i]
        flag = token.split("=", 1)[0]
        takes_next = i + 1 < len(args) and not args[i + 1].startswith("-") and "=" not in token
        if flag in _DROP_FLAGS:
            i += 1
        elif flag in _DROP_WITH_VALUE:
            i += 1 if "=" in token else 2
        elif flag in _DROP_WITH_OPTIONAL_VALUE:
            i += 2 if takes_next else 1
        elif flag in _VALUED:
            kept.extend(args[i : i + (1 if "=" in token else 2)])
            i += 1 if "=" in token else 2
        elif flag in _OPTIONAL_VALUED:
            kept.extend(args[i : i + (2 if takes_next else 1)])
            i += 2 if takes_next else 1
        elif flag in _VARIADIC:
            end = i + 1
            while "=" not in token and end < len(args) and not args[end].startswith("-"):
                end += 1
            if not _is_self_granted(flag, args[i:end]):
                kept.extend(args[i:end])
            i = end
        elif token.startswith("-"):
            kept.append(token)
            i += 1
        else:
            i += 1
    kept.extend(["--resume", conversation_id])
    return shlex.join(kept)


def _is_self_granted(flag: str, group: list[str]) -> bool:
    # Claude Code adds these to its own argv at every in-place restart, so they pile up past agterm's 1024-byte limit.
    values = [group[0].split("=", 1)[1]] if "=" in group[0] else group[1:]
    return flag in _ALLOWED_TOOLS and bool(values) and all(v.startswith(_SELF_GRANTED_TOOLS) for v in values)


def pin_restore_command(command: str, env: Mapping[str, str], agtermctl: str) -> PinResult:
    session = env.get("AGTERM_SESSION_ID")
    if not session:
        return PinResult(command=command, ok=False, detail="not inside agterm")
    args = [agtermctl, "session", "restore", command, "--target", session]
    if pane_id := env.get("AGTERM_PANE_ID"):
        args += ["--pane-id", pane_id]
    if socket := env.get("AGTERM_SOCKET"):
        args += ["--socket", socket]
    try:
        completed = subprocess.run(args, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return PinResult(command=command, ok=False, detail=str(exc))
    detail = (completed.stdout + completed.stderr).strip()
    return PinResult(command=command, ok=completed.returncode == 0, detail=detail)
