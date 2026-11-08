import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from bridge.config import Config
from bridge.db import transaction
from bridge.refs import extract_named_prs, map_ref_to_label, map_repos_to_labels

ENDED_FRESH_MS = 600_000
_URL_RE = re.compile(r"https?://\S+")


@dataclass(frozen=True)
class SettledItem:
    item_id: int
    kind: str
    from_handle: str
    text: str
    why: str


@dataclass(frozen=True)
class EndedPr:
    state: str
    """MERGED or CLOSED."""
    ended_at: int


def load_repo_labels(conn: sqlite3.Connection, config: Config) -> dict[str, str]:
    repos = {*config.pr_repo_aliases.keys(), *config.pr_repo_aliases.values(), *config.repo_labels.keys()}
    repos.update(repo for repo, _ in config.review_setup_scripts)
    repos.update(str(r[0]) for r in conn.execute("SELECT DISTINCT repo FROM review_prs"))
    for (ref,) in conn.execute("SELECT ref FROM enrichment WHERE ref LIKE 'pr:%'"):
        repos.add(str(ref).removeprefix("pr:").rpartition("#")[0])
    return map_repos_to_labels(repos, config.repo_labels)


def _map_item_to_text(row: sqlite3.Row) -> str:
    links: object = json.loads(row["links"]) if row["links"] else []
    urls = [str(cast(dict[str, object], link).get("url", "")) for link in cast(list[object], links)]
    return "\n".join([str(row["text"]), str(row["body"] or ""), *urls])


def find_single_pr(row: sqlite3.Row, labels: dict[str, str]) -> str | None:
    """The one PR a feed item names in its title, body or links; None when it names none, several, or one unpinned."""
    prs, unpinned = extract_named_prs(_map_item_to_text(row), labels)
    return prs[0] if len(prs) == 1 and not unpinned else None


def _list_open_items(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, created_at, from_conversation, from_handle, kind, text, body, links FROM user_items
        WHERE source = 'agent' AND kind IN ('blocked', 'decision') AND resolved_at IS NULL AND answered_at IS NULL
          AND auto_resolve_off = 0
        ORDER BY id
        """
    ).fetchall()


def list_open_item_prs(conn: sqlite3.Connection, config: Config) -> list[str]:
    """PRs whose end may settle an open post, so enrichment keeps their state current."""
    labels = load_repo_labels(conn, config)
    prs = {find_single_pr(row, labels) for row in _list_open_items(conn)}
    return sorted(pr for pr in prs if pr)


def _ends_with_its_pr(row: sqlite3.Row) -> bool:
    # "OK to update the stack?" asks for more than the merge, though posted as blocked.
    asked = _URL_RE.sub("", f"{row['text']}\n{row['body'] or ''}")
    return row["kind"] == "blocked" and "?" not in asked


def load_ended_prs(conn: sqlite3.Connection, at_ms: int) -> dict[str, EndedPr]:
    """From a fetch that just succeeded: a closed PR can be reopened, and a failing one may have been."""
    ended: dict[str, EndedPr] = {}
    for row in conn.execute(
        "SELECT ref, data FROM enrichment WHERE ref LIKE 'pr:%' AND data IS NOT NULL AND error IS NULL AND ok_at >= ?",
        (at_ms - ENDED_FRESH_MS,),
    ):
        data = cast(dict[str, object], json.loads(row["data"]))
        state, closed_at = data.get("state"), data.get("closed_at")
        if state in ("MERGED", "CLOSED") and isinstance(closed_at, str) and closed_at:
            ended[str(row["ref"])] = EndedPr(str(state), int(datetime.fromisoformat(closed_at).timestamp() * 1000))
    return ended


def find_settled_items(conn: sqlite3.Connection, config: Config, at_ms: int) -> list[SettledItem]:
    """Open questions about one PR that its end, merged or closed after the post, has settled.

    A blocked post settles on the end alone: what blocked the PR no longer can. A decision, or a blocked post that
    asks a question anywhere, also needs its author's later done post leading with that PR: a PR's end does not say
    which way the owner would have chosen, and the choice may reach past it.
    """
    open_items = _list_open_items(conn)
    if not open_items:
        return []
    labels = load_repo_labels(conn, config)
    ended = load_ended_prs(conn, at_ms)
    done_prs: dict[str, list[tuple[int, int, str]]] = {}
    for row in conn.execute(
        "SELECT id, created_at, from_conversation, text, body, links FROM user_items "
        "WHERE source = 'agent' AND kind = 'done' AND created_at >= ?",
        (min(int(r["created_at"]) for r in open_items),),
    ):
        prs, unpinned = extract_named_prs(_map_item_to_text(row), labels)
        # Only the PR a done post leads with is the one it reports; "supersedes #64" names another.
        if prs and not unpinned:
            entry = (int(row["id"]), int(row["created_at"]), prs[0])
            done_prs.setdefault(str(row["from_conversation"]), []).append(entry)
    settled: list[SettledItem] = []
    for row in open_items:
        pr = find_single_pr(row, labels)
        if pr is None:
            continue
        label = map_ref_to_label(pr, config.repo_labels)
        created_at = int(row["created_at"])
        end = ended.get(pr)
        if end is None or end.ended_at <= created_at:
            continue
        why: str | None = None
        if _ends_with_its_pr(row):
            why = f"resolved: {label} {end.state.lower()}"
        else:
            done = next(
                (
                    done_id
                    for done_id, done_at, done_pr in done_prs.get(str(row["from_conversation"]), [])
                    if done_pr == pr and done_at > created_at
                ),
                None,
            )
            if done is not None:
                why = f"resolved: {label} {end.state.lower()}, its author posted done (#{done})"
        if why:
            settled.append(
                SettledItem(int(row["id"]), str(row["kind"]), str(row["from_handle"]), str(row["text"]), why)
            )
    return settled


def resolve_settled_items(conn: sqlite3.Connection, config: Config, at_ms: int) -> list[SettledItem]:
    with transaction(conn):
        settled = find_settled_items(conn, config, at_ms)
        for item in settled:
            conn.execute(
                "UPDATE user_items SET resolved_at = ?, resolved_why = ? WHERE id = ? AND resolved_at IS NULL",
                (at_ms, item.why, item.item_id),
            )
    return settled
