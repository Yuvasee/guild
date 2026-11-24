import json
import re
import sqlite3
from typing import cast

from bridge.config import Config, RestrictedPath
from bridge.leads import find_lead_of
from bridge.review_deps import NEGATED_RE

WHY_LINES_MAX = 5
_OK_LINE_RE = re.compile(r"^\s*OK:\s*#(\d{1,15})\s*$", re.MULTILINE)
_OK_WORD_RE = re.compile(r"\bOK\b", re.IGNORECASE)
_REFUSAL_RE = re.compile(r"\b(?:no|not\s+yet|hold)\b", re.IGNORECASE)
_SECTION_END_RE = re.compile(r"^\s*(#{1,6}\s|\*\*.*\*\*\s*$|<details|---|[^\n:]{1,80}:\s*why\s*:?\s*$)", re.IGNORECASE)


def map_row_to_files(row: sqlite3.Row) -> list[str]:
    try:
        files: object = json.loads(str(row["files"] or "[]"))
    except ValueError:
        return []
    return [str(f) for f in cast(list[object], files)] if isinstance(files, list) else []


def list_touched(config: Config, files: list[str]) -> list[RestrictedPath]:
    return [r for r in config.restricted_paths if any(r.covers(f) for f in files)]


def find_restricted_block(
    conn: sqlite3.Connection,
    config: Config,
    touched: list[RestrictedPath],
    body: str,
    branch: str | None,
    author_surface: str | None,
) -> str | None:
    """Why a PR changing these restricted paths may not open or merge: its body lacks a '<label>: why' section of
    1-5 lines or a valid 'OK: #N'. None: it may."""
    assistant = config.assistant_handle
    for restricted in touched:
        why = _read_why(body, restricted.label)
        if why is None:
            missing = f"its body has no section '{restricted.label}: why'"
        elif not why:
            missing = f"its section '{restricted.label}: why' is empty"
        elif len(why) > WHY_LINES_MAX:
            missing = f"its section '{restricted.label}: why' has {len(why)} lines, more than {WHY_LINES_MAX}"
        elif not _has_assistant_ok(conn, assistant, body, branch, author_surface):
            missing = (
                f"its body has no 'OK: #N', N {assistant}'s bridge message saying OK to its author's team or naming "
                "its branch"
            )
        else:
            continue
        return (
            f"this PR changes {restricted.path} ({restricted.label}, {restricted.owner}'s team) and {missing}. "
            f"Ask {assistant} for an OK with a why of at most {WHY_LINES_MAX} lines (what breaks, why it cannot be "
            f"done outside {restricted.label}), then put it under '{restricted.label}: why' and 'OK: #N' in the body."
        )
    return None


def _read_why(body: str, label: str) -> list[str] | None:
    """The non-empty lines under the '<label>: why' heading, up to the next heading, bold line, fold, rule or other
    why; None when the body has no such heading."""
    heading = re.compile(rf"^\s*(?:#+\s*)?(?:\*\*)?{re.escape(label)}:\s*why\s*:?\s*(?:\*\*)?\s*$", re.IGNORECASE)
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if not heading.match(line):
            continue
        section: list[str] = []
        for following in lines[index + 1 :]:
            if _SECTION_END_RE.match(following):
                break
            if following.strip() and not _OK_LINE_RE.match(following):
                section.append(following)
        return section
    return None


def is_ok_message(text: str) -> bool:
    """An OK no word turns around: 'not OK', or 'no', 'not yet' or 'hold' anywhere makes it none."""
    if _REFUSAL_RE.search(text):
        return False
    return any(not NEGATED_RE.search(text[: m.start()]) for m in _OK_WORD_RE.finditer(text))


def _has_assistant_ok(
    conn: sqlite3.Connection, assistant: str, body: str, branch: str | None, author_surface: str | None
) -> bool:
    """An 'OK: #N' line whose message the assistant sent, not cancelled, saying OK, and either naming the PR's branch
    or sent to its author's team: the author's tab, the lead above it, or any tab under that lead."""
    team: set[str] | None = None
    for number in _OK_LINE_RE.findall(body):
        row = conn.execute(
            "SELECT from_handle, body FROM messages WHERE id = ? AND cancelled_at IS NULL", (int(number),)
        ).fetchone()
        if row is None or str(row["from_handle"]).casefold() != assistant.casefold():
            continue
        text = str(row["body"])
        if not is_ok_message(text):
            continue
        if branch and re.search(rf"(?<![\w/.-]){re.escape(branch)}(?![\w/.-])", text):
            return True
        if team is None:
            team = _map_surface_to_team(conn, author_surface)
        # By the conversation it went to, not its handle: a handle may pass to another tab since.
        recipients = conn.execute(
            "SELECT DISTINCT i.surface FROM message_recipients r JOIN instances i "
            "ON i.conversation_id = r.conversation_id WHERE r.message_id = ? AND i.surface IS NOT NULL",
            (int(number),),
        )
        if any(_map_surface_to_team(conn, str(r[0])) & team for r in recipients):
            return True
    return False


def _map_surface_to_team(conn: sqlite3.Connection, surface: str | None) -> set[str]:
    """The tab and the lead above it, which names the team; a tab under no lead is a team of its own."""
    if surface is None:
        return set()
    lead = find_lead_of(conn, surface)[1]
    return {lead} if lead is not None else {surface}


def find_session_surface(conn: sqlite3.Connection, conversation_id: str | None) -> str | None:
    if not conversation_id:
        return None
    row = conn.execute(
        "SELECT surface FROM instances WHERE conversation_id = ? AND surface IS NOT NULL "
        "ORDER BY proc_start_ms DESC LIMIT 1",
        (conversation_id,),
    ).fetchone()
    return str(row[0]) if row else None


def find_pr_restricted_block(conn: sqlite3.Connection, config: Config, row: sqlite3.Row, body: str) -> str | None:
    """find_restricted_block for a queued PR at merge: its files as the queue read them, its owner as author."""
    touched = list_touched(config, map_row_to_files(row))
    if not touched:
        return None
    # reviews imports guard, which imports this module.
    from bridge.reviews import find_owner

    owner = find_owner(conn, config, str(row["ref"]), str(row["repo"]), str(row["head"]))
    return find_restricted_block(conn, config, touched, body, str(row["head"]), owner.surface if owner else None)
