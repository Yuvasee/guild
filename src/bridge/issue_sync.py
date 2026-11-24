import fcntl
import http.client
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

from bridge.asks import send_bodies_from_bridge, split_text_to_bodies
from bridge.board import BODY_MAX_CHARS, SYSTEM_CONVERSATION, MessageStateError, Party, Recipient, send_message
from bridge.config import Config, read_overlay, state_dir
from bridge.db import transaction
from bridge.domains import find_domain_owner, refresh_issue_projects, store_issue_projects
from bridge.enrich import read_linear_key
from bridge.errlog import log_error
from bridge.feed import resolve_tab
from bridge.refs import map_prefixes_to_key_re
from bridge.review_github import GitHubError, run_gh
from bridge.reviews import Owner, find_issue_lead

LINEAR_URL = "https://api.linear.app/graphql"
LINEAR_TIMEOUT_S = 20
LINEAR_BATCH = 40
PR_WINDOW_DAYS = 30
PR_PAGE_SIZE = 50
PR_PAGES_MAX = 10
READY_PAGE_SIZE = 100
READY_PAGES_MAX = 10
EVIDENCE_SHOWN = 3
QUESTION_COMMENTS_READ = 5
"""Enough to see past the bot's own follow-ups: a split assessment, a note that it moved the issue back."""
QUESTIONS_REMINDER_MS = 2 * 3600 * 1000

BACKLOG = "Backlog"
IN_PROGRESS = "In Progress"
IN_REVIEW = "In Review"
DONE = "Done"
_RANK_BY_NAME = {
    "backlog": 0,
    "triage": 0,
    "todo": 1,
    "blocked": 1,
    "grooming": 1,
    "technical design": 1,
    "in progress": 2,
    "in review": 3,
    "done": 4,
}
_RANK_BY_TYPE = {"triage": 0, "backlog": 0, "unstarted": 1}
_TERMINAL_TYPES = {"completed", "canceled", "duplicate"}
_TERMINAL_NAMES = {"done", "canceled", "cancelled", "duplicate"}
_CLOSING_WORDS = (
    r"close[sd]?|closing|fix(?:e[sd])?|fixing|resolve[sd]?|resolving|complete[sd]?|completing"
    r"|(?:last|final) of (?:\w+) PRs? (?:for|of|in|toward)"
)
_NTH = r"first|second|third|fourth|fifth|sixth|\d+(?:st|nd|rd|th)|one|two|three|four|five|six"
_COUNT = r"two|three|four|five|six|seven|eight|nine|ten|\d+|several"
# "First of four PRs for PROJ-1": one part of a larger change; "last" or "final" of them does finish it.
_NON_CLOSING_WORDS = (
    r"part of|refs?|references|related(?: to)?|contributes to|towards"
    rf"|(?:{_NTH}) of (?:{_COUNT}) PRs? (?:for|of|in|toward)"
)
_SEARCH_QUERY = f"""
query($q: String!, $after: String) {{
  search(query: $q, type: ISSUE, first: {PR_PAGE_SIZE}, after: $after) {{
    pageInfo {{ hasNextPage endCursor }}
    nodes {{ ... on PullRequest {{
      number title url state isDraft merged body headRefName baseRefName author {{ login }}
      repository {{ nameWithOwner defaultBranchRef {{ name }} }}
    }} }}
  }}
}}
"""
_ISSUE_FIELDS = (
    "id state { id name type } assignee { name isMe } team { key states(first: 100) { nodes { id name type } } }"
)
_READY_QUERY = f"""
query($teams: [String!], $states: [WorkflowStateFilter!], $after: String) {{
  issues(
    first: {READY_PAGE_SIZE}, after: $after,
    filter: {{ team: {{ key: {{ in: $teams }} }}, state: {{ or: $states }} }}
  ) {{
    pageInfo {{ hasNextPage endCursor }}
    nodes {{ identifier project {{ name }} }}
  }}
}}
"""
_COMMENT_FIELDS = "id createdAt body user { name displayName } botActor { name }"
_MOVE_MUTATION = (
    "mutation($id: String!, $stateId: String!) { issueUpdate(id: $id, input: { stateId: $stateId }) "
    "{ success issue { state { id name } } } }"
)


class LinearError(Exception):
    """Its text never carries the API key."""


@dataclass(frozen=True)
class GitHubPr:
    repo: str
    number: int
    title: str
    url: str
    state: str
    """OPEN, CLOSED or MERGED."""
    draft: bool
    body: str
    head: str
    base: str
    default_branch: str
    author: str

    @property
    def ref(self) -> str:
        return f"pr:{self.repo}#{self.number}"


@dataclass(frozen=True)
class PrEvidence:
    ref: str
    url: str
    state: str
    draft: bool
    closes: bool
    """Its branch or title names the issue, or its body does after Fixes/Closes/Resolves; not a Part of/Refs mention."""
    into_default: bool
    head: str = ""


@dataclass
class Evidence:
    prs: list[PrEvidence] = field(default_factory=list[PrEvidence])
    labels: list[str] = field(default_factory=list[str])
    """A lead's live work naming the issue: a plan, not proof that anyone works on it."""
    sessions: list[str] = field(default_factory=list[str])
    """Live sessions on a branch naming the issue."""


@dataclass(frozen=True)
class TeamState:
    id: str
    name: str
    type: str


@dataclass(frozen=True)
class LinearIssue:
    id: str
    key: str
    state: TeamState
    team_states: tuple[TeamState, ...]
    assignee: str | None = None
    assignee_is_me: bool = False
    """The assignee is the account the Linear key belongs to, the owner's."""


@dataclass(frozen=True)
class Move:
    key: str
    issue_id: str
    from_state: str
    to_state: TeamState
    evidence: str
    pr_refs: tuple[str, ...]
    closing_refs: tuple[str, ...]
    ready: bool = False
    """Grooming left it ready in a lead's project; no work on it yet."""


@dataclass(frozen=True)
class Skip:
    key: str
    reason: str
    grooming: bool = False


@dataclass(frozen=True)
class Comment:
    id: str
    created_at: str
    body: str
    authors: frozenset[str]
    """Every name the comment's author goes by, lowercase."""


@dataclass(frozen=True)
class IssueComments:
    url: str
    newest: tuple[Comment, ...]


@dataclass
class SyncReport:
    moves: list[Move] = field(default_factory=list[Move])
    applied: list[Move] = field(default_factory=list[Move])
    skips: list[Skip] = field(default_factory=list[Skip])
    errors: list[Skip] = field(default_factory=list[Skip])
    sent: list[tuple[int, Recipient]] = field(default_factory=list[tuple[int, Recipient]])
    questions: list[str] = field(default_factory=list[str])
    """Who hears of the grooming bot's open questions, and of which, one line each."""
    note: str = ""
    failed: bool = False
    """The sweep could not run: GitHub or the Linear key failed."""


class Linear(Protocol):
    def fetch_issues(self, keys: list[str]) -> dict[str, LinearIssue | str]: ...

    def move(self, issue_id: str, state_id: str) -> str: ...

    def fetch_comments(self, issue_ids: list[str]) -> dict[str, IssueComments]: ...

    def fetch_keys_in_states(self, states: tuple[str, ...], teams: tuple[str, ...]) -> dict[str, str | None]: ...

    def fetch_issue_projects(self, keys: list[str]) -> dict[str, str | None]: ...


FetchPrs = Callable[[Config, int], list[GitHubPr]]


def map_pr_to_issue_keys(pr: GitHubPr, prefixes: tuple[str, ...]) -> dict[str, bool]:
    """Each issue key the PR names, and whether it says it finishes it. Only a closing word does (Fixes, Closes,
    Resolves, "last of four PRs for"): a key in the branch or title alone names an issue the PR works on, often one
    part of several. In the body only a key after a link word counts: prose like "deferred to PROJ-1" names work the
    PR does not do."""
    if not prefixes:
        return {}
    branch_keys = {k.upper() for k in map_prefixes_to_key_re(prefixes, ignore_case=True).findall(pr.head)}
    title_keys = set(map_prefixes_to_key_re(prefixes, ignore_case=False).findall(pr.title))
    title_partial = _find_keys_after(pr.title, _NON_CLOSING_WORDS, prefixes)
    body_closing = _find_keys_after(pr.body, _CLOSING_WORDS, prefixes)
    body_partial = _find_keys_after(pr.body, _NON_CLOSING_WORDS, prefixes)
    closing = (body_closing | _find_keys_after(pr.title, _CLOSING_WORDS, prefixes)) - title_partial - body_partial
    return {key: key in closing for key in branch_keys | title_keys | body_closing | body_partial}


def _find_keys_after(text: str, words: str, prefixes: tuple[str, ...]) -> set[str]:
    """Keys in a list right after one of the words, as in "Fixes PROJ-1, PROJ-2" or "Part of [PROJ-3](url)"."""
    key = map_prefixes_to_key_re(prefixes, ignore_case=False).pattern
    # A list stays on one line and ends at a sentence's end: "Fixes PROJ-1.\nPROJ-2 is deferred" closes PROJ-1 only.
    token = rf"[^\s,]*{key}(?:[^\s,.;!?]|[.;!?](?=\S))*"
    gap = r"[ \t]*,[ \t]*|[ \t]+and[ \t]+|[ \t]*&[ \t]*|[ \t]+"
    listed = re.compile(rf"(?i:\b(?:{words})\b)[ \t:*_]+({token}(?:(?:{gap}){token})*)")
    exact = map_prefixes_to_key_re(prefixes, ignore_case=False)
    return {k for match in listed.finditer(text) for k in exact.findall(match.group(1))}


def collect_evidence(conn: sqlite3.Connection, config: Config, prs: list[GitHubPr]) -> dict[str, Evidence]:
    evidence: dict[str, Evidence] = {}
    for pr in prs:
        if pr.author.lower() != config.review_author.lower():
            continue
        for key, closes in map_pr_to_issue_keys(pr, config.issue_prefixes).items():
            evidence.setdefault(key, Evidence()).prs.append(
                PrEvidence(
                    ref=pr.ref,
                    url=pr.url,
                    state=pr.state,
                    draft=pr.draft,
                    closes=closes,
                    into_default=pr.state == "MERGED" and pr.base == pr.default_branch,
                    head=pr.head,
                )
            )
    if not config.issue_prefixes:
        return evidence
    exact = map_prefixes_to_key_re(config.issue_prefixes, ignore_case=False)
    for row in conn.execute(
        "SELECT w.lead, w.name, w.refs FROM lead_work w JOIN leads l ON l.name = w.lead "
        "WHERE w.done_at IS NULL AND l.retired_at IS NULL"
    ):
        for ref in cast(list[object], json.loads(str(row["refs"]))):
            key = str(ref).removeprefix("issue:").upper()
            if str(ref).startswith("issue:") and exact.fullmatch(key):
                evidence.setdefault(key, Evidence()).labels.append(f"{row['lead']}'s live work '{row['name']}'")
    any_case = map_prefixes_to_key_re(config.issue_prefixes, ignore_case=True)
    for row in conn.execute("SELECT DISTINCT branch FROM instances WHERE ended_at IS NULL AND branch IS NOT NULL"):
        for key in {k.upper() for k in any_case.findall(str(row["branch"]))}:
            evidence.setdefault(key, Evidence()).sessions.append(f"a live session on branch {row['branch']}")
    return evidence


def map_state_to_rank(state: TeamState) -> int | None:
    """None for a state this sweep cannot place, e.g. a team's own started state like QA."""
    by_name = _RANK_BY_NAME.get(state.name.lower())
    return by_name if by_name is not None else _RANK_BY_TYPE.get(state.type)


def is_terminal(state: TeamState) -> bool:
    return state.type in _TERMINAL_TYPES or state.name.lower() in _TERMINAL_NAMES


def map_evidence_to_target(evidence: Evidence) -> tuple[str, list[str]] | None:
    """The state the real work puts the issue in, with what shows it; None when nothing does. Live work naming the
    issue holds Done back: a title or branch key is on every PR of an issue, the last one or not. A lead's work label
    alone moves nothing forward: it names a plan, and an issue nobody works on is not In Progress."""
    open_prs = [p for p in evidence.prs if p.state == "OPEN"]
    merged = [p for p in evidence.prs if p.state == "MERGED"]
    closing_merged = [p for p in merged if p.closes and p.into_default]
    if not open_prs and closing_merged and not evidence.labels and not evidence.sessions:
        return DONE, [f"merged PR {p.url}" for p in closing_merged]
    ready = [p for p in open_prs if not p.draft]
    # Only a PR that finishes the issue puts it in review; one part of several leaves it in progress.
    if finishing := [p for p in ready if p.closes]:
        return IN_REVIEW, [f"open PR {p.url}" for p in finishing]
    started = [f"{'draft' if p.draft else 'open'} PR {p.url}" + ("" if p.closes else " (part of it)") for p in open_prs]
    started += [f"merged PR {p.url}" + ("" if p.closes else " (part of it)") for p in merged]
    started += evidence.sessions
    return (IN_PROGRESS, started) if started else None


def find_team_state(issue: LinearIssue, name: str) -> TeamState | None:
    return next((s for s in issue.team_states if s.name.lower() == name.lower()), None)


def decide_move(
    issue: LinearIssue,
    evidence: Evidence,
    moved_before: str | None = None,
    grooming_states: tuple[str, ...] = (),
    ready_states: tuple[str, ...] = (),
    ready_reason: str | None = None,
) -> Move | Skip | None:
    """Forward only; never out of Done, Canceled or Duplicate; nothing but the state. ready_reason: the issue's project
    is a lead's, so once grooming leaves it in a ready state it starts with no work on it yet."""
    if is_terminal(issue.state):
        return None
    target = map_evidence_to_target(evidence)
    state = issue.state.name.lower()
    ready = target is None and bool(ready_reason) and state in ready_states and state not in grooming_states
    if ready:
        target = IN_PROGRESS, [str(ready_reason)]
    if target is None:
        return None
    wanted, shown = target
    to_state = find_team_state(issue, wanted)
    if to_state is None and wanted == IN_REVIEW:
        to_state = find_team_state(issue, IN_PROGRESS)
    if to_state is None:
        return Skip(issue.key, f"its team has no {wanted} state")
    to_rank = _RANK_BY_NAME[to_state.name.lower()]
    from_rank = map_state_to_rank(issue.state)
    if from_rank is None:
        return Skip(issue.key, f"state {issue.state.name} has no place in the order; {wanted} not applied")
    if from_rank >= to_rank:
        return None
    where = f"{issue.state.name} → {to_state.name}"
    before = _RANK_BY_NAME.get((moved_before or "").lower(), -1)
    if from_rank < before and to_rank <= before:
        return Skip(issue.key, f"set back by hand after the sync moved it to {moved_before}; {where} not applied")
    if issue.assignee and not issue.assignee_is_me:
        return Skip(issue.key, f"assigned to {issue.assignee}, theirs to move: {where} not applied")
    # An issue in a grooming state moves on once its grooming is done, never by hand or by a sweep.
    if issue.state.name.lower() in grooming_states:
        return Skip(issue.key, f"skipped (grooming rule): {where}; {_join_evidence(shown)}", grooming=True)
    return Move(
        key=issue.key,
        issue_id=issue.id,
        from_state=issue.state.name,
        to_state=to_state,
        evidence=_join_evidence(shown),
        pr_refs=tuple(p.ref for p in evidence.prs),
        closing_refs=tuple(p.ref for p in evidence.prs if p.closes),
        ready=ready,
    )


def _join_evidence(shown: list[str]) -> str:
    more = len(shown) - EVIDENCE_SHOWN
    return "; ".join(shown[:EVIDENCE_SHOWN]) + (f"; {more} more" if more > 0 else "")


def run_issue_sync(
    conn: sqlite3.Connection,
    config: Config,
    now: int,
    dry_run: bool,
    fetch_prs: FetchPrs | None = None,
    linear: Linear | None = None,
) -> SyncReport:
    report = SyncReport()
    if not (config.review_org and config.review_author and config.issue_prefixes):
        report.note = "review_org, review_author and issue_prefixes must all be set; nothing to sync"
        return report
    try:
        prs = (fetch_prs or fetch_my_prs)(config, now)
    except GitHubError as exc:
        report.note = f"reading PRs failed, nothing moved: {exc}"
        report.failed = True
        _log_failure(dry_run, f"issue sync: {report.note}")
        return report
    evidence = collect_evidence(conn, config, prs)
    if linear is None:
        key, problem = read_linear_key(config)
        if key is None:
            report.note = problem or "no Linear API key (linear_api_key_file); nothing to sync"
            if problem:
                report.failed = True
                _log_failure(dry_run, f"issue sync: {problem}")
            return report
        linear = LinearApi(key)
    ready = find_ready_issues(conn, config, linear, now, report, dry_run)
    keys = sorted({*evidence, *ready})
    issues: dict[str, LinearIssue | str] = {}
    for start in range(0, len(keys), LINEAR_BATCH):
        issues.update(linear.fetch_issues(keys[start : start + LINEAR_BATCH]))
    moved_before = read_applied()
    for key in keys:
        issue = issues.get(key, "no answer from Linear")
        if isinstance(issue, str):
            if issue != NOT_FOUND:
                report.errors.append(Skip(key, issue))
                _log_failure(dry_run, f"issue sync: {key} skipped: {issue}")
            continue
        decision = decide_move(
            issue,
            evidence.get(key, Evidence()),
            moved_before.get(key),
            config.grooming_states,
            config.ready_states,
            ready.get(key),
        )
        if isinstance(decision, Move):
            report.moves.append(decision)
        elif isinstance(decision, Skip):
            report.skips.append(decision)
    notices = plan_question_notices(conn, config, linear, issues, evidence, now, dry_run)
    if dry_run:
        report.questions = [f"{n.to}: {n.line}" for n in notices]
        return report
    with hold_sweep_lock() as held:
        if not held:
            report.note = "another issue sync is running; nothing moved"
            report.moves = []
            return report
        _apply_moves(conn, config, linear, evidence, ready, moved_before, report, now)
        report.sent += tell_leads_of_grooming(conn, config, report.skips, evidence, now)
        sent, report.questions = tell_of_questions(conn, config, notices, now)
        report.sent += sent
    return report


def find_ready_issues(
    conn: sqlite3.Connection, config: Config, linear: Linear, now: int, report: SyncReport, dry_run: bool
) -> dict[str, str]:
    """The issues in a ready state of a project one lead owns, the same owner a notice on it goes to, each with why
    it may start. Their projects go to the cache that owner lookup reads."""
    if not config.ready_states:
        return {}
    try:
        projects = linear.fetch_keys_in_states(config.ready_states, config.issue_prefixes)
    except LinearError as exc:
        report.errors.append(Skip("ready states", f"reading the issues in them failed: {exc}"))
        _log_failure(dry_run, f"issue sync: reading the issues in ready states failed: {exc}")
        return {}
    store_issue_projects(conn, projects, now)
    try:
        # A priority project's Linear project is the majority of its listed issues': each must be known, not only the
        # ready ones.
        refresh_issue_projects(conn, list(projects), now, linear.fetch_issue_projects)
    except LinearError as exc:
        report.errors.append(Skip("ready states", f"reading the projects of priority issues failed: {exc}"))
        _log_failure(dry_run, f"issue sync: reading the projects of priority issues failed: {exc}")
        return {}
    ready: dict[str, str] = {}
    for key in projects:
        owner = find_domain_owner(conn, key)
        if owner is not None:
            ready[key] = f"ready after grooming; {owner.project} is {owner.lead.display_name}'s project"
    return ready


GROOMING_HEAD = (
    "These issues of yours are in a grooming state, but work on them exists. Issue sync never moves an issue out of "
    "one: settle each in the tracker."
)


def tell_leads_of_grooming(
    conn: sqlite3.Connection, config: Config, skips: list[Skip], evidence: dict[str, Evidence], now: int
) -> list[tuple[int, Recipient]]:
    """Once per issue: its lead hears that work exists while it waits in a grooming state."""
    told = read_grooming_told()
    by_lead: dict[str, tuple[str, list[str]]] = {}
    for skip in skips:
        if not skip.grooming or skip.key in told:
            continue
        prs = evidence.get(skip.key, Evidence()).prs
        refs = [p.ref for p in prs]
        closing = [p.ref for p in prs if p.closes]
        owner = find_issue_lead(conn, config, f"issue:{skip.key}", refs, closing, map_prs_to_heads(prs))
        if owner is None:
            continue
        by_lead.setdefault(owner.handle, (owner.conversation_id, []))[1].append(f"- {skip.key}")
        told.add(skip.key)
    sent: list[tuple[int, Recipient]] = []
    head = read_overlay(config, "issue-sync-grooming", GROOMING_HEAD, owner=config.owner_name)
    with transaction(conn):
        for handle, (conversation_id, lines) in by_lead.items():
            recipient = resolve_tab(conn, conversation_id, handle)
            try:
                result = send_message(
                    conn,
                    Party(SYSTEM_CONVERSATION, "bridge"),
                    [recipient],
                    "info",
                    _fit("\n".join([head, *lines])),
                    now,
                )
            except MessageStateError as exc:
                log_error("issue sync", f"telling {handle} of grooming issues failed: {exc}")
                continue
            sent.append((result.message_id, recipient))
    write_grooming_told(told)
    return sent


def grooming_told_path() -> Path:
    return state_dir() / "issue-sync-grooming-told.json"


def read_grooming_told() -> set[str]:
    try:
        raw: object = json.loads(grooming_told_path().read_text())
    except (OSError, ValueError):
        return set()
    return {str(k) for k in cast(list[object], raw)} if isinstance(raw, list) else set()


def write_grooming_told(told: set[str]) -> None:
    path = grooming_told_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(sorted(told)) + "\n")
    os.replace(tmp, path)


GROOMING_QUESTIONS_HEAD = (
    "{bot} has open questions on these issues. Answer under its comment on each issue; it moves the issue on when "
    "none are left."
)


@dataclass(frozen=True)
class QuestionNotice:
    comment_id: str
    line: str
    owner: Owner | None
    """None: no live lead owns the issue, and the assistant hears."""
    to: str
    reminder: bool


def plan_question_notices(
    conn: sqlite3.Connection,
    config: Config,
    linear: Linear,
    issues: dict[str, LinearIssue | str],
    evidence: dict[str, Evidence],
    now: int,
    dry_run: bool,
) -> list[QuestionNotice]:
    """Once per bot comment that asks questions and is still the newest word on its issue, and once more after
    QUESTIONS_REMINDER_MS with no reply. A newer such comment is a new one: its notice is the reminder."""
    if not (config.grooming_bot and config.grooming_questions_re):
        return []
    held = [
        i
        for i in issues.values()
        if isinstance(i, LinearIssue)
        and i.state.name.lower() in config.grooming_states
        and not (i.assignee and not i.assignee_is_me)
    ]
    comments: dict[str, IssueComments] = {}
    try:
        for start in range(0, len(held), LINEAR_BATCH):
            comments.update(linear.fetch_comments([i.id for i in held[start : start + LINEAR_BATCH]]))
    except LinearError as exc:
        _log_failure(dry_run, f"issue sync: reading grooming comments failed: {exc}")
        return []
    pattern = re.compile(config.grooming_questions_re)
    told = read_questions_told()
    notices: list[QuestionNotice] = []
    for issue in held:
        found = comments.get(issue.id)
        asked = find_open_questions(found.newest, config.grooming_bot, pattern) if found else None
        if found is None or asked is None:
            continue
        comment, count = asked
        reminder = comment.id in told
        if not is_question_due(told.get(comment.id), now):
            continue
        prs = evidence.get(issue.key, Evidence()).prs
        owner = find_issue_lead(
            conn,
            config,
            f"issue:{issue.key}",
            [p.ref for p in prs],
            [p.ref for p in prs if p.closes],
            map_prs_to_heads(prs),
        )
        how_many = f"{count} open question{'' if count == 1 else 's'}"
        line = f"- {issue.key}: {how_many}" + (", still unanswered" if reminder else "") + f" {found.url}"
        to = owner.handle if owner else config.assistant_handle
        notices.append(QuestionNotice(comment.id, line, owner, to, reminder))
    return notices


def is_question_due(seen: dict[str, int] | None, now: int) -> bool:
    return seen is None or (not seen["reminded"] and now - seen["told_at"] >= QUESTIONS_REMINDER_MS)


def find_open_questions(newest: tuple[Comment, ...], bot: str, pattern: re.Pattern[str]) -> tuple[Comment, int] | None:
    """The bot's question comment when no one wrote after it; the bot's own later notes, such as the rest of a split
    assessment, do not count as an answer."""
    for comment in sorted(newest, key=lambda c: c.created_at, reverse=True):
        if bot.lower() not in comment.authors:
            return None
        match = pattern.match(comment.body)
        if match is None:
            continue
        count = int(match.group(1)) if (match.group(1) or "").isdigit() else 0
        return (comment, count) if count > 0 else None
    return None


def tell_of_questions(
    conn: sqlite3.Connection, config: Config, notices: list[QuestionNotice], now: int
) -> tuple[list[tuple[int, Recipient]], list[str]]:
    """Under the sweep lock; the told file is read again, so a sweep that planned from an older one sends nothing
    twice. It is written before the messages commit: a failed write sends nothing."""
    told = read_questions_told()
    by_to: dict[str, list[QuestionNotice]] = {}
    for notice in notices:
        if is_question_due(told.get(notice.comment_id), now):
            by_to.setdefault(notice.to, []).append(notice)
    if not by_to:
        return [], []
    head = read_overlay(config, "issue-sync-grooming-questions", GROOMING_QUESTIONS_HEAD, bot=config.grooming_bot)
    sent: list[tuple[int, Recipient]] = []
    lines: list[str] = []
    with transaction(conn):
        for to, mine in by_to.items():
            text = "\n".join([head, *(n.line for n in mine)])
            owner = mine[0].owner
            delivered = (
                send_bodies_from_bridge(conn, config, to, text, now)
                if owner is None
                else _send_bodies_to_owner(conn, owner, text, now)
            )
            bodies = "\n".join(body for body, _, _ in delivered)
            sent += [(message_id, recipient) for _, message_id, recipient in delivered]
            for notice in mine:
                if notice.line in bodies:
                    told[notice.comment_id] = {"told_at": now, "reminded": int(notice.reminder)}
                    lines.append(f"{to}: {notice.line}")
                else:
                    log_error("issue sync", f"telling {to} of grooming questions failed: {notice.line}")
        write_questions_told(told)
    return sent, lines


def _send_bodies_to_owner(
    conn: sqlite3.Connection, owner: Owner, text: str, now: int
) -> list[tuple[str, int, Recipient]]:
    recipient = resolve_tab(conn, owner.conversation_id, owner.handle)
    delivered: list[tuple[str, int, Recipient]] = []
    for body in split_text_to_bodies(text, BODY_MAX_CHARS):
        try:
            result = send_message(conn, Party(SYSTEM_CONVERSATION, "bridge"), [recipient], "info", body, now)
        except MessageStateError:
            continue
        delivered.append((body, result.message_id, recipient))
    return delivered


def questions_told_path() -> Path:
    return state_dir() / "issue-sync-grooming-questions-told.json"


def read_questions_told() -> dict[str, dict[str, int]]:
    """Per bot comment id: when its notice went out, and whether its reminder did."""
    try:
        raw: object = json.loads(questions_told_path().read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    told: dict[str, dict[str, int]] = {}
    for key, value in cast(dict[object, object], raw).items():
        entry = cast(dict[object, object], value) if isinstance(value, dict) else {}
        told_at = entry.get("told_at")
        # An entry it cannot read counts as told and reminded: silence, never a repeat.
        told[str(key)] = (
            {"told_at": told_at, "reminded": int(bool(entry.get("reminded")))}
            if isinstance(told_at, int)
            else {"told_at": 0, "reminded": 1}
        )
    return told


def write_questions_told(told: dict[str, dict[str, int]]) -> None:
    path = questions_told_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(told, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _apply_moves(
    conn: sqlite3.Connection,
    config: Config,
    linear: Linear,
    evidence: dict[str, Evidence],
    ready: dict[str, str],
    moved_before: dict[str, str],
    report: SyncReport,
    now: int,
) -> None:
    """Each issue is read again just before its move, so a state someone set since the read is never overwritten."""
    for planned in report.moves:
        fresh = linear.fetch_issues([planned.key]).get(planned.key, "no answer from Linear")
        if isinstance(fresh, str):
            report.errors.append(Skip(planned.key, f"reading it again failed: {fresh}"))
            _log_failure(False, f"issue sync: {planned.key} skipped: reading it again failed: {fresh}")
            continue
        move = decide_move(
            fresh,
            evidence.get(planned.key, Evidence()),
            moved_before.get(planned.key),
            config.grooming_states,
            config.ready_states,
            ready.get(planned.key),
        )
        if not isinstance(move, Move) or move.to_state.id != planned.to_state.id:
            continue
        try:
            linear.move(move.issue_id, move.to_state.id)
        except LinearError as exc:
            report.errors.append(Skip(move.key, f"moving to {move.to_state.name} failed: {exc}"))
            _log_failure(False, f"issue sync: {move.key} not moved to {move.to_state.name}: {exc}")
            continue
        report.applied.append(move)
        moved_before[move.key] = move.to_state.name
        _append_log(f"{move.key} {move.from_state} -> {move.to_state.name} | {move.evidence}", now)
    if report.applied:
        write_applied(moved_before)
        with transaction(conn):
            report.sent = tell_leads_of_moves(conn, config, report.applied, evidence, now)


MOVES_HEAD = "Issue sync moved your issues in Linear to match the real work (FYI, nothing to do unless it is wrong):"
READY_MOVES_HEAD = (
    "Issue sync moved your issues in Linear to match the real work, or to In Progress once grooming left them ready: "
    "yours to work on now (FYI, nothing to do unless it is wrong):"
)


@contextmanager
def hold_sweep_lock() -> Generator[bool]:
    """True while this process holds the sweep; serve's sweep and a CLI one never apply at the same time."""
    path = state_dir() / "issue-sync.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def tell_leads_of_moves(
    conn: sqlite3.Connection, config: Config, moves: list[Move], evidence: dict[str, Evidence], now: int
) -> list[tuple[int, Recipient]]:
    """One message per lead per sweep; an issue with no live lead is told to no one."""
    by_lead: dict[str, tuple[str, list[str], bool]] = {}
    for move in moves:
        heads = map_prs_to_heads(evidence.get(move.key, Evidence()).prs)
        owner = find_issue_lead(conn, config, f"issue:{move.key}", list(move.pr_refs), list(move.closing_refs), heads)
        if owner is None:
            continue
        line = f"- {move.key}: {move.from_state} → {move.to_state.name} ({move.evidence})"
        _, lines, ready = by_lead.get(owner.handle, (owner.conversation_id, [], False))
        by_lead[owner.handle] = (owner.conversation_id, [*lines, line], ready or move.ready)
    sent: list[tuple[int, Recipient]] = []
    for handle, (conversation_id, lines, ready) in by_lead.items():
        head = READY_MOVES_HEAD if ready else MOVES_HEAD
        text = _fit("\n".join([head, *lines]))
        recipient = resolve_tab(conn, conversation_id, handle)
        try:
            result = send_message(conn, Party(SYSTEM_CONVERSATION, "bridge"), [recipient], "info", text, now)
        except MessageStateError as exc:
            log_error("issue sync", f"telling {handle} of moved issues failed: {exc}")
            continue
        sent.append((result.message_id, recipient))
    return sent


def map_prs_to_heads(prs: list[PrEvidence]) -> dict[str, dict[str, object]]:
    """A PR outside the review queue is found by its branch alone: the router reads it as enrichment's head."""
    return {p.ref: {"head": p.head} for p in prs if p.head}


def _fit(text: str) -> str:
    return text if len(text) <= BODY_MAX_CHARS else text[: BODY_MAX_CHARS - 1] + "…"


def map_report_to_lines(report: SyncReport, dry_run: bool) -> list[str]:
    lines: list[str] = []
    if report.note:
        lines.append(report.note)
    verb = "would move" if dry_run else "moved"
    done = report.moves if dry_run else report.applied
    lines += [f"{verb} {m.key}: {m.from_state} -> {m.to_state.name} | {m.evidence}" for m in done]
    lines += [f"skip {s.key}: {s.reason}" for s in report.skips]
    lines += [f"error {s.key}: {s.reason}" for s in report.errors]
    lines.append(f"{len(done)} {verb}, {len(report.skips)} skipped, {len(report.errors)} errors")
    lines += [f"{'would tell' if dry_run else 'told'} {q}" for q in report.questions]
    return lines


def log_path() -> Path:
    return state_dir() / "issue-sync.log"


def applied_path() -> Path:
    return state_dir() / "issue-sync.json"


def read_applied() -> dict[str, str]:
    """The state the sync last moved each issue to, so a hand move back is respected."""
    try:
        raw: object = json.loads(applied_path().read_text())
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in cast(dict[object, object], raw).items()} if isinstance(raw, dict) else {}


def write_applied(applied: dict[str, str]) -> None:
    path = applied_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(applied, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _append_log(line: str, now: int) -> None:
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now / 1000))
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{stamp} {line}\n")
    except OSError:
        pass


def _log_failure(dry_run: bool, line: str) -> None:
    """A dry run writes nothing; it prints its errors instead."""
    if dry_run:
        return
    _append_log(f"ERROR {line}", int(time.time() * 1000))
    log_error("issue sync", line)


def fetch_my_prs(config: Config, now: int) -> list[GitHubPr]:
    """All open PRs of the author, and every one updated in the window; raises rather than return part of them."""
    since = (datetime.fromtimestamp(now / 1000, UTC) - timedelta(days=PR_WINDOW_DAYS)).strftime("%Y-%m-%d")
    base = f"is:pr author:{config.review_author} org:{config.review_org}"
    found: dict[str, GitHubPr] = {}
    for query in (f"{base} is:open archived:false", f"{base} updated:>={since}"):
        for pr in _search_prs(query):
            found[pr.ref] = pr
    return list(found.values())


def _search_prs(query: str) -> list[GitHubPr]:
    prs: list[GitHubPr] = []
    after: str | None = None
    for _ in range(PR_PAGES_MAX):
        args = ["api", "graphql", "-f", f"query={_SEARCH_QUERY}", "-f", f"q={query}"]
        if after:
            args += ["-f", f"after={after}"]
        try:
            search: Any = json.loads(run_gh(args))["data"]["search"]
            prs.extend(map_node_to_github_pr(node) for node in search["nodes"] if node)
        except (ValueError, KeyError, TypeError) as exc:
            raise GitHubError(f"unexpected answer from GitHub: {type(exc).__name__}") from exc
        if not search["pageInfo"]["hasNextPage"]:
            return prs
        after = str(search["pageInfo"]["endCursor"])
    raise GitHubError(f"more than {PR_PAGES_MAX} pages of PRs for {query!r}")


def map_node_to_github_pr(node: Any) -> GitHubPr:
    repository: Any = node["repository"]
    default: Any = repository.get("defaultBranchRef") or {}
    author: Any = node.get("author") or {}
    return GitHubPr(
        repo=str(repository["nameWithOwner"]),
        number=int(node["number"]),
        title=str(node["title"]),
        url=str(node["url"]),
        state="MERGED" if node.get("merged") else str(node["state"]),
        draft=bool(node.get("isDraft")),
        body=str(node.get("body") or ""),
        head=str(node["headRefName"]),
        base=str(node["baseRefName"]),
        default_branch=str(default.get("name") or ""),
        author=str(author.get("login") or ""),
    )


NOT_FOUND = "not found or no access"


class LinearApi:
    def __init__(self, api_key: str, timeout_s: float = LINEAR_TIMEOUT_S) -> None:
        self._key = api_key
        self._timeout_s = timeout_s

    def fetch_issues(self, keys: list[str]) -> dict[str, LinearIssue | str]:
        return {
            key: node if isinstance(node, str) else map_node_to_linear_issue(node, key)
            for key, node in self.fetch_issue_nodes(keys, _ISSUE_FIELDS).items()
        }

    def fetch_issue_projects(self, keys: list[str]) -> dict[str, str | None]:
        """Each issue's Linear project name, None for no project or no such issue; any other failure raises."""
        projects: dict[str, str | None] = {}
        for key, node in self.fetch_issue_nodes(keys, "project { name }").items():
            if isinstance(node, str) and node != NOT_FOUND:
                raise LinearError(node)
            name = _dig(node, "project", "name")
            projects[key] = name if isinstance(name, str) and name else None
        return projects

    def fetch_issue_nodes(self, keys: list[str], fields: str) -> dict[str, object]:
        """Each key's issue node with the given fields, or why it has none as a string."""
        results: dict[str, object] = {}
        pending: list[str] = list(keys)
        # One unknown key nulls Linear's whole aliased answer: drop the failing ones and ask again; each round
        # removes at least one key, so this ends.
        while pending:
            aliases = {f"i{n}": key for n, key in enumerate(pending)}
            selection = " ".join(f"{a}: issue(id: {json.dumps(k)}) {{ {fields} }}" for a, k in aliases.items())
            try:
                raw = self._post({"query": f"query {{ {selection} }}"})
            except LinearError as exc:
                results.update(dict.fromkeys(pending, str(exc)))
                return results
            data = _dig(raw, "data")
            errors = _dig(raw, "errors")
            if data is None and isinstance(errors, list):
                failed: dict[str, str] = {}
                for error in cast(list[object], errors):
                    alias = str(_dig(error, "path", 0))
                    if alias in aliases:
                        failed[aliases[alias]] = map_linear_error_to_result(error)
                if not failed:
                    results.update(dict.fromkeys(pending, "Linear refused the query"))
                    return results
                results.update(failed)
                pending = [k for k in pending if k not in failed]
                continue
            for alias, key in aliases.items():
                node = _dig(data, alias)
                results[key] = node if node is not None else NOT_FOUND
            pending = []
        return results

    def fetch_keys_in_states(self, states: tuple[str, ...], teams: tuple[str, ...]) -> dict[str, str | None]:
        """Each issue of the teams in one of the states, with its project name (None: no project); raises rather
        than return part of them."""
        found: dict[str, str | None] = {}
        after: str | None = None
        wanted = [{"name": {"eqIgnoreCase": s}} for s in states]
        for _ in range(READY_PAGES_MAX):
            data = self.query(_READY_QUERY, {"teams": list(teams), "states": wanted, "after": after})
            nodes = _dig(data, "issues", "nodes")
            if not isinstance(nodes, list):
                raise LinearError("unexpected issues answer from Linear")
            for node in cast(list[object], nodes):
                key, project = _dig(node, "identifier"), _dig(node, "project", "name")
                if isinstance(key, str):
                    found[key] = project if isinstance(project, str) and project else None
            if _dig(data, "issues", "pageInfo", "hasNextPage") is not True:
                return found
            after = str(_dig(data, "issues", "pageInfo", "endCursor"))
        raise LinearError(f"more than {READY_PAGES_MAX * READY_PAGE_SIZE} issues in the ready states")

    def move(self, issue_id: str, state_id: str) -> str:
        raw = self._post({"query": _MOVE_MUTATION, "variables": {"id": issue_id, "stateId": state_id}})
        update = _dig(raw, "data", "issueUpdate")
        if _dig(update, "success") is not True or _dig(update, "issue", "state", "id") != state_id:
            raise LinearError("Linear did not confirm the new state")
        return str(_dig(update, "issue", "state", "name") or "")

    def fetch_comments(self, issue_ids: list[str]) -> dict[str, IssueComments]:
        """Each issue's newest comments, replies among them, in one query."""
        # Linear lists comments newest first.
        comments = f"comments(first: {QUESTION_COMMENTS_READ}) {{ nodes {{ {_COMMENT_FIELDS} }} }}"
        fields = " ".join(f"i{n}: issue(id: {json.dumps(i)}) {{ id url {comments} }}" for n, i in enumerate(issue_ids))
        data = self.query(f"query {{ {fields} }}", {})
        found: dict[str, IssueComments] = {}
        for n in range(len(issue_ids)):
            node: Any = _dig(data, f"i{n}")
            if node is None:
                continue
            try:
                found[str(node["id"])] = IssueComments(
                    url=str(node["url"]),
                    newest=tuple(map_node_to_comment(c) for c in node["comments"]["nodes"]),
                )
            except (KeyError, TypeError) as exc:
                raise LinearError("unexpected comments answer from Linear") from exc
        return found

    def query(self, query: str, variables: dict[str, object]) -> object:
        """The answer's data; any error Linear names is raised."""
        raw = self._post({"query": query, "variables": variables})
        errors = _dig(raw, "errors")
        if _dig(errors, 0) is not None:
            message = str(_dig(errors, 0, "message") or "unknown")
            raise LinearError(f"linear error: {message[:200]}")
        return _dig(raw, "data")

    def _post(self, payload: dict[str, object]) -> object:
        request = urllib.request.Request(
            LINEAR_URL,
            data=json.dumps(payload).encode(),
            headers={"Authorization": self._key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                raw: object = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                raw = json.loads(exc.read())
            except (OSError, ValueError, http.client.HTTPException):
                raise LinearError(f"linear HTTP {exc.code}") from None
        except (OSError, ValueError, http.client.HTTPException) as exc:
            # Exception text can echo request headers; keep only the type.
            raise LinearError(f"linear failed: {type(exc).__name__}") from None
        if not isinstance(raw, dict):
            raise LinearError("linear answered with no object")
        return cast(object, raw)


def _dig(obj: object, *path: str | int) -> object:
    for step in path:
        if isinstance(step, int):
            obj = (
                cast(list[object], obj)[step] if isinstance(obj, list) and len(cast(list[object], obj)) > step else None
            )
        else:
            obj = cast(dict[str, object], obj).get(step) if isinstance(obj, dict) else None
    return obj


def map_linear_error_to_result(error: object) -> str:
    """NOT_FOUND only when Linear says so; anything else is a failure to report, not a missing issue."""
    message = str(_dig(error, "message") or "")
    code = str(_dig(error, "extensions", "code") or "")
    if "not found" in message.lower() or "not found" in str(_dig(error, "extensions", "userPresentableMessage") or ""):
        return NOT_FOUND
    return f"linear error: {code or 'unknown'}: {message[:120]}"


def map_node_to_linear_issue(node: Any, key: str) -> LinearIssue | str:
    try:
        state: Any = node["state"]
        team: Any = node["team"]
        return LinearIssue(
            id=str(node["id"]),
            key=key,
            state=TeamState(str(state["id"]), str(state["name"]), str(state["type"])),
            team_states=tuple(TeamState(str(s["id"]), str(s["name"]), str(s["type"])) for s in team["states"]["nodes"]),
            assignee=str(assignee["name"]) if (assignee := node.get("assignee")) else None,
            assignee_is_me=bool(assignee) and assignee.get("isMe") is True,
        )
    except (KeyError, TypeError):
        return "unexpected issue answer from Linear"


def map_node_to_comment(node: Any) -> Comment:
    names = [_dig(node, "user", "name"), _dig(node, "user", "displayName"), _dig(node, "botActor", "name")]
    return Comment(
        id=str(node["id"]),
        created_at=str(node["createdAt"]),
        body=str(node.get("body") or ""),
        authors=frozenset(str(n).lower() for n in names if n),
    )
