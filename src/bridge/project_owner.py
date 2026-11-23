import sqlite3
import sys
import traceback
from typing import Protocol

from bridge.asks import send_from_bridge
from bridge.board import Recipient
from bridge.cli_board import nudge_recipients
from bridge.config import Config
from bridge.db import now_ms, transaction
from bridge.domains import find_domain_owner, find_issue_keys, refresh_issue_projects
from bridge.enrich import read_linear_key
from bridge.errlog import log_error
from bridge.feed import spawn_typed_delivery
from bridge.identity import ensure_current_instance
from bridge.issue_sync import LinearApi, LinearError
from bridge.leads import Lead, find_lead_on_surface, list_leads
from bridge.reviews import find_lead_of

LINEAR_CHECK_TIMEOUT_S = 5
PROJECTS_RETRY_MS = 10 * 60_000


class IssueProjects(Protocol):
    def fetch_issue_projects(self, keys: list[str]) -> dict[str, str | None]: ...


def warn_on_taken_issues(
    conn: sqlite3.Connection, config: Config, taker: Lead, refs: list[str], api: IssueProjects | None = None
) -> None:
    """A lead naming an issue of another lead's project is warned, and that lead and the assistant are told, once per
    issue and taker. A warning, never a refusal: the command it rides on has already succeeded, so no failure here
    may fail it."""
    try:
        _warn_on_taken_issues(conn, config, taker, refs, api)
    except Exception as exc:
        _log_failed_check(exc)


def warn_on_claimed_issue(conn: sqlite3.Connection, config: Config, ref: str) -> None:
    """As warn_on_taken_issues, for the lead of the session that claimed it."""
    try:
        taker = find_lead_of_caller(conn)
        if taker is not None:
            _warn_on_taken_issues(conn, config, taker, [ref], None)
    except Exception as exc:
        _log_failed_check(exc)


def _log_failed_check(exc: Exception) -> None:
    print(f"note: the check of whose project this is failed: {type(exc).__name__}", file=sys.stderr)
    log_error("project owner check", traceback.format_exc())


def _warn_on_taken_issues(
    conn: sqlite3.Connection, config: Config, taker: Lead, refs: list[str], api: IssueProjects | None
) -> None:
    taken = {r.removeprefix("issue:").upper() for r in refs if r.startswith("issue:")}
    told = {str(r[0]) for r in conn.execute("SELECT issue FROM issue_takes WHERE taker = ?", (taker.name,))}
    keys = sorted(taken - told)
    others = [lead for lead in list_leads(conn) if lead.name != taker.name and lead.domains]
    if not keys or not others:
        return
    try:
        refresh_issue_projects(conn, keys, now_ms(), lambda due: (api or _open_api(config)).fetch_issue_projects(due))
    except LinearError as exc:
        print(f"note: Linear not reached ({exc}): {', '.join(keys)} placed by the priority lists only", file=sys.stderr)
    takings = [
        (key, found)
        for key in keys
        if (found := find_domain_owner(conn, key)) is not None and found.lead.name != taker.name
    ]
    sent: list[tuple[int, Recipient]] = []
    with transaction(conn):
        for key, found in takings:
            owner, project, at = found.lead.display_name, found.project, now_ms()
            print(
                f"warning: {key} is in {project}, {owner}'s project: agree with them first",
                file=sys.stderr,
            )
            recorded = conn.execute(
                "INSERT OR IGNORE INTO issue_takes (issue, taker, owner, project, at) VALUES (?, ?, ?, ?, ?)",
                (key, taker.name, found.lead.name, project, at),
            )
            if recorded.rowcount == 0:
                continue
            to_owner = f"{taker.display_name} took {key} from your project {project}."
            told_owner = send_from_bridge(conn, config, found.lead.name, to_owner, at)
            missed = "" if told_owner else f" {owner} was not told: no live session."
            to_assistant = f"{taker.display_name} took {key} from {owner}'s project {project}.{missed}"
            told_assistant = send_from_bridge(conn, config, config.assistant_handle, to_assistant, at)
            if not told_owner and not told_assistant:
                # Only the taker heard of it: the next command naming the issue tries again.
                conn.execute("DELETE FROM issue_takes WHERE issue = ? AND taker = ?", (key, taker.name))
            sent += [*told_owner, *told_assistant]
    for message_id, recipient in sent:
        nudge_recipients([recipient])
        spawn_typed_delivery(message_id, [recipient])


def refresh_open_pr_projects(conn: sqlite3.Connection, config: Config, now: int) -> None:
    """Before the review poll routes notices: the Linear projects of the issues open PRs name, so routing reads only
    the cache. No Linear key configured: nothing to do. A failure raises; the poll waits PROJECTS_RETRY_MS."""
    key, problem = read_linear_key(config)
    if (key is None and problem is None) or not config.issue_prefixes:
        return
    rows = conn.execute("SELECT title, head FROM review_prs WHERE closed_at IS NULL")
    keys = sorted({k for r in rows for k in find_issue_keys(config, f"{r['title']} {r['head']}")})
    refresh_issue_projects(conn, keys, now, lambda due: _open_api(config).fetch_issue_projects(due))


def find_lead_of_caller(conn: sqlite3.Connection) -> Lead | None:
    """The lead this session is, or the lead above it: a worker's claim counts as its lead's."""
    row = conn.execute(
        "SELECT surface FROM instances WHERE instance_id = ?", (ensure_current_instance(conn),)
    ).fetchone()
    _, surface = find_lead_of(conn, row["surface"] if row else None)
    return find_lead_on_surface(conn, surface)


def find_domain_overlaps(lead: Lead, leads: list[Lead]) -> list[str]:
    return [
        f"{domain} is already {other.display_name}'s domain: one lead per project, the assistant decides"
        for domain in lead.domains
        for other in leads
        if other.name != lead.name and domain.lower() in {d.lower() for d in other.domains}
    ]


def _open_api(config: Config) -> IssueProjects:
    key, problem = read_linear_key(config)
    if key is None:
        raise LinearError(problem or "no Linear API key")
    return LinearApi(key, LINEAR_CHECK_TIMEOUT_S)
