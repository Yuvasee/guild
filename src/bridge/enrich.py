import json
import sqlite3
import subprocess
from dataclasses import asdict, dataclass
from typing import cast

from bridge.config import Config
from bridge.db import transaction
from bridge.registry import RECENT_REF_TURNS, current_ref_sql
from bridge.trackers import IssueStatus, LinkedPr, load_tracker

PR_FRESH_MS = 120_000
ISSUE_FRESH_MS = 300_000
FAILED_RETRY_MS = 120_000
STALE_AFTER_MS = 600_000
BATCH_SIZE = 40
_PR_FIELDS = """
  number title state isDraft url headRefName headRefOid baseRefName reviewDecision mergeable
  mergeStateStatus isInMergeQueue updatedAt viewerDidAuthor author { login }
  baseRef { associatedPullRequests(states: OPEN, first: 1) { totalCount } }
  commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
"""


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


__all__ = ["IssueStatus", "LinkedPr", "PrStatus"]

Results = dict[str, PrStatus | IssueStatus | str]


def enrich_due(conn: sqlite3.Connection, config: Config, at_ms: int, force: bool = False) -> dict[str, int]:
    """Refreshes GitHub and tracker state for refs that live sessions or claims point at; each fails on its own."""
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
    tracker = load_tracker(config)
    issue_results: Results = {}
    if issues and tracker is not None:
        for start in range(0, len(issues), BATCH_SIZE):
            issue_results.update(tracker.fetch(issues[start : start + BATCH_SIZE]))
    store_results(conn, issue_results, at_ms)
    return {"prs": len(prs), "issues": len(issue_results)}


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
    return sorted(str(r[0]) for r in rows)


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
    rollup = _get(_get(_get(last, "commit"), "statusCheckRollup"), "state")
    parents = _get(_get(_get(node, "baseRef"), "associatedPullRequests"), "totalCount")
    return PrStatus(
        title=str(_get(node, "title") or ""),
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
    )


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
    for provider, prefix in (("github", "pr:"), ("tracker", "issue:")):
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


def _opt(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
