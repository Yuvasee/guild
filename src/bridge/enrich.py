import json
import re
import sqlite3
import subprocess
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import cast

from bridge.agent_prefix import strip_agent_prefix
from bridge.config import Config
from bridge.db import transaction
from bridge.registry import RECENT_REF_TURNS, current_ref_sql

PR_FRESH_MS = 120_000
ISSUE_FRESH_MS = 300_000
FAILED_RETRY_MS = 120_000
STALE_AFTER_MS = 600_000
BATCH_SIZE = 40
_LINEAR_URL = "https://api.linear.app/graphql"
_LINEAR_KEY_RE = re.compile(r"[A-Za-z0-9_\-]{20,200}")
_PR_FIELDS = """
  number title state isDraft url headRefName headRefOid baseRefName reviewDecision mergeable
  mergeStateStatus isInMergeQueue updatedAt closedAt viewerDidAuthor author { login }
  baseRef { associatedPullRequests(states: OPEN, first: 1) { totalCount } }
  commits(last: 1) { nodes { commit {
    statusCheckRollup { state contexts(first: 0) { checkRunCountsByState { state count } } }
    status { contexts { context state description targetUrl } }
  } } }
"""
# Check runs in these states turn the head commit's rollup red.
_RED_CHECK_RUN_STATES = frozenset({"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "CANCELLED", "ACTION_REQUIRED"})


@dataclass(frozen=True)
class CommitStatus:
    context: str
    state: str
    """ERROR, FAILURE, PENDING, SUCCESS or EXPECTED."""
    description: str
    url: str


@dataclass(frozen=True)
class PrStatus:
    title: str
    state: str
    """OPEN, CLOSED or MERGED."""
    draft: bool
    review: str | None
    """APPROVED, CHANGES_REQUESTED, REVIEW_REQUIRED or None."""
    checks: str | None
    """Head-commit rollup: SUCCESS, FAILURE, ERROR, PENDING, EXPECTED or None when there are no checks."""
    mergeable: str | None
    merge_state: str | None
    """GitHub's verdict including branch protection: CLEAN, BLOCKED, BEHIND, UNSTABLE, DIRTY, DRAFT, …"""
    in_merge_queue: bool
    url: str
    head: str
    head_oid: str
    base: str
    stacked: bool
    """The base branch is the head of another open PR, so this one cannot land on its own."""
    updated_at: str
    author: str = ""
    mine: bool = False
    """The user's gh account opened it; anyone else's PR is theirs to merge and fix."""
    closed_at: str = ""
    """When it was merged or closed; empty while open."""
    statuses: list[CommitStatus] = field(default_factory=list[CommitStatus])
    """The head commit's commit statuses (not check runs), as their senders set them."""
    red_check_runs: int | None = None
    """Head-commit check runs in a failing state; None when GitHub did not say."""


@dataclass(frozen=True)
class LinkedPr:
    ref: str
    status: str
    """Linear's view of the PR: open, draft, merged, closed, …"""
    closes: bool
    """The PR says it closes the issue (magic word or branch), not just mentions it."""
    merged_at: int | None


@dataclass(frozen=True)
class IssueStatus:
    title: str
    state: str
    state_type: str
    """Linear workflow type: triage, backlog, unstarted, started, completed or canceled."""
    assignee: str | None
    url: str
    assignee_is_me: bool = False
    """The assignee is the account the Linear key belongs to, the owner's; an issue of anyone else is theirs."""
    linked_prs: list[LinkedPr] = field(default_factory=list[LinkedPr])
    """Every GitHub PR Linear has attached to the issue, from any session or none."""
    linked_complete: bool = False
    """False when the list may miss PRs (more pages, unreadable entries): nothing may be concluded from it."""


Results = dict[str, PrStatus | IssueStatus | str]


def enrich_due(conn: sqlite3.Connection, config: Config, at_ms: int, force: bool = False) -> dict[str, int]:
    """Refreshes GitHub and Linear state for refs that live sessions, claims or open blocked posts point at; providers
    fail separately."""
    refs = collect_watched_refs(conn)
    fetched = {
        str(r["ref"]): (int(r["fetched_at"]), r["error"] is not None)
        for r in conn.execute("SELECT ref, fetched_at, error FROM enrichment")
    }

    def due(ref: str, fresh_ms: int) -> bool:
        if force or ref not in fetched:
            return True
        at, failed = fetched[ref]
        return at_ms - at > (FAILED_RETRY_MS if failed else fresh_ms)

    prs = [r for r in refs if r.startswith("pr:") and due(r, PR_FRESH_MS)]
    issues = [r for r in refs if r.startswith("issue:") and due(r, ISSUE_FRESH_MS)]
    pr_results: Results = {}
    for start in range(0, len(prs), BATCH_SIZE):
        pr_results.update(fetch_prs(prs[start : start + BATCH_SIZE]))
    store_results(conn, pr_results, at_ms)
    key, key_problem = read_linear_key(config)
    issue_results: Results = {}
    if issues and key:
        for start in range(0, len(issues), BATCH_SIZE):
            issue_results.update(fetch_issues(issues[start : start + BATCH_SIZE], key))
    elif issues and key_problem:
        issue_results = dict.fromkeys(issues, key_problem)
    store_results(conn, issue_results, at_ms)
    return {"prs": len(prs), "issues": len(issue_results), "linear_key": int(bool(key))}


def store_results(conn: sqlite3.Connection, results: Results, at_ms: int) -> None:
    """A failure keeps the last good data but not its freshness: ok_at only moves on success."""
    with transaction(conn):
        for ref, result in results.items():
            if isinstance(result, str):
                conn.execute(
                    """
                    INSERT INTO enrichment (ref, data, fetched_at, error) VALUES (?, NULL, ?, ?)
                    ON CONFLICT (ref) DO UPDATE SET fetched_at = excluded.fetched_at, error = excluded.error
                    """,
                    (ref, at_ms, result),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO enrichment (ref, data, fetched_at, ok_at, error) VALUES (?, ?, ?, ?, NULL)
                    ON CONFLICT (ref) DO UPDATE SET
                        data = excluded.data, fetched_at = excluded.fetched_at, ok_at = excluded.ok_at, error = NULL
                    """,
                    (ref, json.dumps(asdict(result)), at_ms, at_ms),
                )


def collect_watched_refs(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        f"""
        SELECT DISTINCT r.ref FROM refs r
        JOIN conversations c ON c.conversation_id = r.conversation_id
        JOIN instances i ON i.conversation_id = c.conversation_id AND i.ended_at IS NULL
        LEFT JOIN enrichment e ON e.ref = r.ref
        WHERE {current_ref_sql(f"c.turns - {RECENT_REF_TURNS}", for_polling=True)}
          AND (r.ref LIKE 'pr:%' OR r.ref LIKE 'issue:%')
        UNION
        SELECT resource FROM claims WHERE resource LIKE 'pr:%' OR resource LIKE 'issue:%'
        UNION
        SELECT ref FROM remote_refs WHERE ref LIKE 'pr:%' OR ref LIKE 'issue:%'
        UNION
        SELECT r.value FROM lead_work w, json_each(w.refs) r
        WHERE w.done_at IS NULL AND (r.value LIKE 'pr:%' OR r.value LIKE 'issue:%')
        """
    ).fetchall()
    return sorted({str(r[0]) for r in rows})


def fetch_prs(refs: list[str]) -> Results:
    """One GraphQL round trip for many PRs; a ref that is not a PR comes back as an error string."""
    by_repo: dict[str, list[tuple[str, int]]] = {}
    for ref in refs:
        repo, _, number = ref.removeprefix("pr:").partition("#")
        if "/" in repo and number.isdigit():
            by_repo.setdefault(repo, []).append((ref, int(number)))
    if not by_repo:
        return {}
    parts: list[str] = []
    aliases: dict[str, str] = {}
    for r, (repo, items) in enumerate(by_repo.items()):
        owner, name = repo.split("/", 1)
        fields: list[str] = []
        for p, (ref, number) in enumerate(items):
            aliases[f"r{r}.p{p}"] = ref
            fields.append(f"p{p}: pullRequest(number: {number}) {{ ...pr }}")
        repo_args = f"owner: {json.dumps(owner)}, name: {json.dumps(name)}"
        parts.append(f"r{r}: repository({repo_args}) {{ {' '.join(fields)} }}")
    query = f"query {{ {' '.join(parts)} }} fragment pr on PullRequest {{ {_PR_FIELDS} }}"
    try:
        completed = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return dict.fromkeys(aliases.values(), f"gh failed: {type(exc).__name__}")
    if not completed.stdout.strip().startswith("{"):
        last_line = (completed.stderr.strip().splitlines() or ["no output"])[-1][:200]
        return dict.fromkeys(aliases.values(), f"gh failed: {last_line}")
    data = _get(json.loads(completed.stdout), "data")
    results: Results = {}
    for path, ref in aliases.items():
        repo_alias, pr_alias = path.split(".")
        node = _as_dict(_get(_get(data, repo_alias), pr_alias))
        results[ref] = map_node_to_pr(node) if node is not None else "not found or no access"
    return results


def map_node_to_pr(node: dict[str, object]) -> PrStatus:
    commits = _get(_get(node, "commits"), "nodes")
    last = cast(list[object], commits)[-1] if isinstance(commits, list) and commits else None
    commit = _get(last, "commit")
    rollup = _get(_get(commit, "statusCheckRollup"), "state")
    parents = _get(_get(_get(node, "baseRef"), "associatedPullRequests"), "totalCount")
    return PrStatus(
        title=strip_agent_prefix(str(_get(node, "title") or "")),
        state=str(_get(node, "state") or ""),
        draft=bool(_get(node, "isDraft")),
        review=_opt(_get(node, "reviewDecision")),
        checks=_opt(rollup),
        mergeable=_opt(_get(node, "mergeable")),
        merge_state=_opt(_get(node, "mergeStateStatus")),
        in_merge_queue=bool(_get(node, "isInMergeQueue")),
        url=str(_get(node, "url") or ""),
        head=str(_get(node, "headRefName") or ""),
        head_oid=str(_get(node, "headRefOid") or ""),
        base=str(_get(node, "baseRefName") or ""),
        stacked=_get(node, "state") == "OPEN" and isinstance(parents, int) and parents > 0,
        updated_at=str(_get(node, "updatedAt") or ""),
        author=str(_get(_get(node, "author"), "login") or ""),
        mine=_get(node, "viewerDidAuthor") is True,
        closed_at=str(_get(node, "closedAt") or ""),
        statuses=map_node_to_statuses(_get(_get(commit, "status"), "contexts")),
        red_check_runs=map_counts_to_red_check_runs(
            _get(_get(_get(commit, "statusCheckRollup"), "contexts"), "checkRunCountsByState")
        ),
    )


def map_node_to_statuses(contexts: object) -> list[CommitStatus]:
    found: list[CommitStatus] = []
    for raw in cast(list[object], contexts) if isinstance(contexts, list) else []:
        if isinstance(raw, dict):
            item = cast(dict[str, object], raw)
            found.append(
                CommitStatus(
                    str(item.get("context") or ""),
                    str(item.get("state") or ""),
                    str(item.get("description") or ""),
                    str(item.get("targetUrl") or ""),
                )
            )
    return found


def map_counts_to_red_check_runs(counts: object) -> int | None:
    if not isinstance(counts, list):
        return None
    total = 0
    for raw in cast(list[object], counts):
        item = cast(dict[str, object], raw) if isinstance(raw, dict) else {}
        count = item.get("count")
        if item.get("state") in _RED_CHECK_RUN_STATES and isinstance(count, int):
            total += count
    return total


def fetch_issues(refs: list[str], api_key: str) -> Results:
    results: Results = {}
    pending = list(refs)
    # Linear's issue field is non-null: one unknown key nulls the whole batch, so drop the failing aliases and retry.
    # Each retry removes at least one ref, so this ends.
    while pending:
        failed = _fetch_issue_batch(pending, api_key, results)
        for ref in failed:
            results[ref] = "not found or no access"
        pending = [ref for ref in pending if ref not in results]
    return results


def _fetch_issue_batch(refs: list[str], api_key: str, results: Results) -> set[str]:
    """Fills results for one aliased query; returns the refs Linear reported as failing when it nulled the batch."""
    keys = {f"i{n}": ref for n, ref in enumerate(refs)}
    fields = " ".join(
        f"{alias}: issue(id: {json.dumps(ref.removeprefix('issue:'))}) "
        "{ title url state { name type } assignee { name isMe } "
        "attachments(first: 100) { nodes { sourceType metadata } pageInfo { hasNextPage } } }"
        for alias, ref in keys.items()
    )
    body = json.dumps({"query": f"query {{ {fields} }}"}).encode()
    request = urllib.request.Request(
        _LINEAR_URL, data=body, headers={"Authorization": api_key, "Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw: object = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            raw = json.loads(exc.read())
        except ValueError:
            results.update(dict.fromkeys(refs, f"linear HTTP {exc.code}"))
            return set()
    except (OSError, ValueError) as exc:
        # Exception text can echo request headers; keep only the type.
        results.update(dict.fromkeys(refs, f"linear failed: {type(exc).__name__}"))
        return set()
    data = _get(raw, "data")
    errors = _get(raw, "errors")
    if data is None and isinstance(errors, list):
        heads = [_first(_get(e, "path")) for e in cast(list[object], errors)]
        failed = {keys[h] for h in heads if h in keys}
        if failed:
            return failed
    for alias, ref in keys.items():
        node = _as_dict(_get(data, alias))
        if node is None:
            results[ref] = "not found or no access"
            continue
        state = _get(node, "state")
        linked, complete = map_attachments_to_linked_prs(_get(_get(node, "attachments"), "nodes"))
        results[ref] = IssueStatus(
            title=strip_agent_prefix(str(_get(node, "title") or "")),
            state=str(_get(state, "name") or ""),
            state_type=str(_get(state, "type") or ""),
            assignee=_opt(_get(_get(node, "assignee"), "name")),
            url=str(_get(node, "url") or ""),
            assignee_is_me=_get(_get(node, "assignee"), "isMe") is True,
            linked_prs=linked,
            linked_complete=complete and _get(_get(_get(node, "attachments"), "pageInfo"), "hasNextPage") is False,
        )
    return set()


def map_attachments_to_linked_prs(nodes: object) -> tuple[list[LinkedPr], bool]:
    """GitHub PRs attached to an issue, and whether every GitHub attachment could be read."""
    linked: list[LinkedPr] = []
    complete = isinstance(nodes, list)
    for node in cast(list[object], nodes) if isinstance(nodes, list) else []:
        if _get(node, "sourceType") != "github":
            continue
        meta = _get(node, "metadata")
        owner, repo, number = _get(meta, "repoLogin"), _get(meta, "repoName"), _get(meta, "number")
        if isinstance(number, str) and number.isdigit():
            number = int(number)
        if not (isinstance(owner, str) and isinstance(repo, str) and isinstance(number, int)):
            # A GitHub attachment that is not a readable PR (or a commit, an issue) could be the open one.
            complete = complete and _get(meta, "number") is None and _get(meta, "status") is None
            continue
        merged_at = _get(meta, "mergedAt")
        linked.append(
            LinkedPr(
                ref=f"pr:{owner}/{repo}#{number}",
                status=str(_get(meta, "status") or ""),
                closes=_get(meta, "linkKind") == "closes",
                merged_at=_iso_to_ms(merged_at),
            )
        )
    return linked, complete


def _iso_to_ms(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value).timestamp() * 1000)
    except ValueError:
        return None


def read_linear_key(config: Config) -> tuple[str | None, str | None]:
    """Returns (key, problem); a problem message never contains the key itself."""
    if not config.linear_api_key_file:
        return None, None
    path = Path(config.linear_api_key_file).expanduser()
    try:
        key = path.read_text().strip()
    except OSError:
        return None, None
    # Also accepts a one-line env file (NAME=key), the way secrets are often kept.
    name, sep, value = key.partition("=")
    if sep and re.fullmatch(r"[A-Za-z_]\w*", name):
        key = value.strip().strip("'\"")
    if not _LINEAR_KEY_RE.fullmatch(key):
        return None, f"Linear key in {path} is malformed (expected one token on one line)"
    return key, None


def load_enrichment(conn: sqlite3.Connection, at_ms: int) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for row in conn.execute("SELECT ref, data, ok_at, error FROM enrichment"):
        data: object = json.loads(row["data"]) if row["data"] else {}
        entry: dict[str, object] = dict(cast(dict[str, object], data)) if isinstance(data, dict) else {}
        entry["ok_at"] = row["ok_at"]
        entry["stale"] = row["ok_at"] is None or at_ms - int(row["ok_at"]) > STALE_AFTER_MS
        if row["error"]:
            entry["error"] = str(row["error"])
        result[str(row["ref"])] = entry
    return result


def provider_freshness(enrichment: dict[str, dict[str, object]], watched: list[str]) -> dict[str, object]:
    """Per provider: the latest success (is the provider reachable at all) and how many refs fail on their own.

    A ref that is not found fails forever without meaning the provider is down, so health uses the newest success.
    """
    result: dict[str, object] = {}
    for provider, prefix in (("github", "pr:"), ("linear", "issue:")):
        entries = [enrichment[r] for r in watched if r.startswith(prefix) and r in enrichment]
        ok = [int(str(e["ok_at"])) for e in entries if e.get("ok_at") is not None]
        result[provider] = {
            "newest_ok_at": max(ok) if ok else None,
            "failing": sum(1 for e in entries if "error" in e),
            "watched": len(entries),
        }
    return result


def _as_dict(obj: object) -> dict[str, object] | None:
    return cast(dict[str, object], obj) if isinstance(obj, dict) else None


def _get(obj: object, key: str) -> object:
    return cast(dict[str, object], obj).get(key) if isinstance(obj, dict) else None


def _first(value: object) -> str | None:
    return str(cast(list[object], value)[0]) if isinstance(value, list) and value else None


def _opt(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
