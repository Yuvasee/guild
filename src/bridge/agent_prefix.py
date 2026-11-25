import re
from typing import cast

ROBOT = "🤖"
CLAUDE_PREFIX = f"{ROBOT} Claude:"
TITLE_RULE = (
    f"'{CLAUDE_PREFIX}' goes only at the start of comment and description bodies on GitHub and of replies on Linear "
    "(not a Linear description or an issue's first comment), never at the start of a PR, issue or commit title"
)
LINEAR_ISSUE_TOOLS = ("mcp__claude_ai_Linear__save_issue",)
ARTIFACT_TOOL = "Artifact"
# Actions that publish nothing; a missing action is a publish, and delete is refused with publishing.
ARTIFACT_READ_ACTIONS = frozenset({"read", "list", "open", "quickstart"})


def is_artifact_publish(payload: dict[str, object]) -> bool:
    if payload.get("tool_name") != ARTIFACT_TOOL:
        return False
    tool_input = payload.get("tool_input")
    action = cast(dict[str, object], tool_input).get("action") if isinstance(tool_input, dict) else None
    return action not in ARTIFACT_READ_ACTIONS


_LEADING_PREFIX_RE = re.compile(rf"^\s*{ROBOT}(?:\s*Claude\s*:)?\s*")


def strip_agent_prefix(title: str) -> str:
    """A title with a leading robot or robot-and-Claude prefix taken off; any other title as it is."""
    stripped = _LEADING_PREFIX_RE.sub("", title, count=1)
    return stripped if stripped else title


def is_prefixed(title: str) -> bool:
    return title.lstrip().startswith(ROBOT)
