import re

ROBOT = "🤖"
CLAUDE_PREFIX = f"{ROBOT} Claude:"
TITLE_RULE = (
    f"'{CLAUDE_PREFIX}' goes only at the start of comment and description bodies on GitHub and Linear, never at the "
    "start of a PR, issue or commit title"
)
LINEAR_ISSUE_TOOLS = ("mcp__claude_ai_Linear__save_issue",)
_LEADING_PREFIX_RE = re.compile(rf"^\s*{ROBOT}(?:\s*Claude\s*:)?\s*")


def strip_agent_prefix(title: str) -> str:
    """A title with a leading robot or robot-and-Claude prefix taken off; any other title as it is."""
    stripped = _LEADING_PREFIX_RE.sub("", title, count=1)
    return stripped if stripped else title


def is_prefixed(title: str) -> bool:
    return title.lstrip().startswith(ROBOT)
