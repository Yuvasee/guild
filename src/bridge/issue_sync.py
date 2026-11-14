import fcntl
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

from bridge.board import BODY_MAX_CHARS, SYSTEM_CONVERSATION, MessageStateError, Party, Recipient, send_message
from bridge.config import Config, read_overlay, state_dir
from bridge.db import transaction
from bridge.enrich import read_linear_key
from bridge.errlog import log_error
from bridge.feed import resolve_tab
from bridge.review_github import GitHubError, run_gh
from bridge.reviews import find_issue_lead

LINEAR_URL = "https://api.linear.app/graphql"
LINEAR_TIMEOUT_S = 20
LINEAR_BATCH = 40
PR_WINDOW_DAYS = 30
PR_PAGE_SIZE = 50
PR_PAGES_MAX = 10
EVIDENCE_SHOWN = 3

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


@dataclass(frozen=True)
class Skip:
    key: str
    reason: str
    grooming: bool = False


@dataclass
class SyncReport:
    moves: list[Move] = field(default_factory=list[Move])
    applied: list[Move] = field(default_factory=list[Move])
    skips: list[Skip] = field(default_factory=list[Skip])
    errors: list[Skip] = field(default_factory=list[Skip])
    sent: list[tuple[int, Recipient]] = field(default_factory=list[tuple[int, Recipient]])
    note: str = ""
    failed: bool = False
    """The sweep could not run: GitHub or the Linear key failed."""


class Linear(Protocol):
    def fetch_issues(self, keys: list[str]) -> dict[str, LinearIssue | str]: ...

    def move(self, issue_id: str, state_id: str) -> str: ...


FetchPrs = Callable[[Config, int], list[GitHubPr]]


def map_prefixes_to_key_re(prefixes: Iterable[str], ignore_case: bool) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(p) for p in prefixes)
    return re.compile(rf"(?<![A-Za-z0-9])(?:{alternatives})-\d+(?!\d)", re.IGNORECASE if ignore_case else 0)


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
    issue: LinearIssue, evidence: Evidence, moved_before: str | None = None, grooming_states: tuple[str, ...] = ()
) -> Move | Skip | None:
    """Forward only; never out of Done, Canceled or Duplicate; nothing but the state."""
    if is_terminal(issue.state):
        return None
    target = map_evidence_to_target(evidence)
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
    keys = sorted(evidence)
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
        decision = decide_move(issue, evidence[key], moved_before.get(key), config.grooming_states)
        if isinstance(decision, Move):
            report.moves.append(decision)
        elif isinstance(decision, Skip):
            report.skips.append(decision)
    if dry_run:
        return report
    with hold_sweep_lock() as held:
        if not held:
            report.note = "another issue sync is running; nothing moved"
            report.moves = []
            return report
        _apply_moves(conn, config, linear, evidence, moved_before, report, now)
        report.sent += tell_leads_of_grooming(conn, config, report.skips, evidence, now)
    return report


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


def _apply_moves(
    conn: sqlite3.Connection,
    config: Config,
    linear: Linear,
    evidence: dict[str, Evidence],
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
        move = decide_move(fresh, evidence[planned.key], moved_before.get(planned.key), config.grooming_states)
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
    by_lead: dict[str, tuple[str, list[str]]] = {}
    for move in moves:
        heads = map_prs_to_heads(evidence.get(move.key, Evidence()).prs)
        owner = find_issue_lead(conn, config, f"issue:{move.key}", list(move.pr_refs), list(move.closing_refs), heads)
        if owner is None:
            continue
        line = f"- {move.key}: {move.from_state} → {move.to_state.name} ({move.evidence})"
        by_lead.setdefault(owner.handle, (owner.conversation_id, []))[1].append(line)
    sent: list[tuple[int, Recipient]] = []
    for handle, (conversation_id, lines) in by_lead.items():
        head = "Issue sync moved your issues in Linear to match the real work (FYI, nothing to do unless it is wrong):"
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
    def __init__(self, api_key: str) -> None:
        self._key = api_key

    def fetch_issues(self, keys: list[str]) -> dict[str, LinearIssue | str]:
        results: dict[str, LinearIssue | str] = {}
        pending: list[str] = list(keys)
        # One unknown key nulls Linear's whole aliased answer: drop the failing ones and ask again; each round
        # removes at least one key, so this ends.
        while pending:
            aliases = {f"i{n}": key for n, key in enumerate(pending)}
            fields = " ".join(f"{a}: issue(id: {json.dumps(k)}) {{ {_ISSUE_FIELDS} }}" for a, k in aliases.items())
            try:
                raw = self._post({"query": f"query {{ {fields} }}"})
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
                results[key] = map_node_to_linear_issue(node, key) if node is not None else NOT_FOUND
            pending = []
        return results

    def move(self, issue_id: str, state_id: str) -> str:
        raw = self._post({"query": _MOVE_MUTATION, "variables": {"id": issue_id, "stateId": state_id}})
        update = _dig(raw, "data", "issueUpdate")
        if _dig(update, "success") is not True or _dig(update, "issue", "state", "id") != state_id:
            raise LinearError("Linear did not confirm the new state")
        return str(_dig(update, "issue", "state", "name") or "")

    def _post(self, payload: dict[str, object]) -> object:
        request = urllib.request.Request(
            LINEAR_URL,
            data=json.dumps(payload).encode(),
            headers={"Authorization": self._key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=LINEAR_TIMEOUT_S) as response:
                raw: object = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                raw = json.loads(exc.read())
            except ValueError:
                raise LinearError(f"linear HTTP {exc.code}") from None
        except (OSError, ValueError) as exc:
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
