import json
import re
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from bridge.config import Config
from bridge.db import transaction
from bridge.leads import Lead, list_leads
from bridge.refs import map_prefixes_to_key_re

PROJECT_FRESH_MS = 6 * 3_600_000
_ANY_KEY_RE = re.compile(r"\b[A-Z]{2,}-\d+\b")

FetchProjects = Callable[[list[str]], dict[str, str | None]]


def map_name_to_slug(name: str) -> str:
    """A domain and a project name compared as slugs: 'issue-detector' is the project 'Issue Detector'."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


@dataclass(frozen=True)
class DomainOwner:
    lead: Lead
    project: str


def find_domain_owner(conn: sqlite3.Connection, key: str) -> DomainOwner | None:
    """The one lead whose domains hold the issue's project, from the database only: a priority project listing the
    issue, else the issue's cached Linear project, held by a domain of that name or by a priority project most of whose
    listed issues are in it. None when no lead or more than one does. A Linear project ref as a domain is not matched:
    Linear's API gives a project no such ref."""
    key = key.upper()
    leads = [lead for lead in list_leads(conn) if lead.domains]
    if not leads:
        return None
    priority = _read_priority(conn)
    listed = [
        DomainOwner(lead, d) for lead in leads for d in lead.domains if key in priority.get(map_name_to_slug(d), ())
    ]  # fmt: skip
    if listed:
        return listed[0] if len({o.lead.name for o in listed}) == 1 else None
    cached = _read_cached(conn, [key, *(k for keys in priority.values() for k in keys)])
    project = cached.get(key)
    if project is None:
        return None

    def holds(domain: str) -> bool:
        sample = priority.get(map_name_to_slug(domain))
        if sample is None:
            return map_name_to_slug(domain) == map_name_to_slug(project)
        return _find_most_common([cached.get(k) for k in sample]) == project

    holders = [lead for lead in leads if any(holds(d) for d in lead.domains)]
    return DomainOwner(holders[0], project) if len(holders) == 1 else None


def refresh_issue_projects(conn: sqlite3.Connection, keys: list[str], now: int, fetch: FetchProjects) -> list[str]:
    """Asks Linear, outside any transaction, for the projects of the issues no priority list places and of the listed
    issues, whose majority is each priority project's Linear project, unless known in the last few hours. The keys it
    asked for; a failed fetch raises, and the cache keeps what it had."""
    priority = _read_priority(conn)
    listed = {k for keys_ in priority.values() for k in keys_}
    unlisted = [k.upper() for k in keys if k.upper() not in listed]
    if not unlisted:
        return []
    wanted = list(dict.fromkeys([*unlisted, *(k for keys_ in priority.values() for k in keys_)]))
    fresh = {
        str(r[0])
        for r in conn.execute(
            f"SELECT issue FROM issue_projects WHERE fetched_at > ? AND issue IN ({','.join('?' * len(wanted))})",
            (now - PROJECT_FRESH_MS, *wanted),
        )
    }
    due = [k for k in wanted if k not in fresh]
    if not due:
        return []
    found = fetch(due)
    with transaction(conn):
        conn.executemany(
            "INSERT INTO issue_projects (issue, project, fetched_at) VALUES (?, ?, ?) ON CONFLICT (issue) DO UPDATE "
            "SET project = excluded.project, fetched_at = excluded.fetched_at",
            [(k, found.get(k), now) for k in due],
        )
    return due


def find_issue_keys(config: Config, text: str) -> set[str]:
    """The issue keys a PR's title or branch names: of the configured teams, in any case, as branches are written."""
    if not config.issue_prefixes:
        return set(_ANY_KEY_RE.findall(text.upper()))
    return {k.upper() for k in map_prefixes_to_key_re(config.issue_prefixes, ignore_case=True).findall(text)}


def _read_priority(conn: sqlite3.Connection) -> dict[str, tuple[str, ...]]:
    return {
        map_name_to_slug(str(r["name"])): tuple(cast(list[str], json.loads(str(r["issue_keys"]))))
        for r in conn.execute("SELECT name, issue_keys FROM project_priority ORDER BY position")
    }


def _read_cached(conn: sqlite3.Connection, keys: list[str]) -> dict[str, str | None]:
    wanted = list(dict.fromkeys(keys))
    rows = conn.execute(
        f"SELECT issue, project FROM issue_projects WHERE issue IN ({','.join('?' * len(wanted))})", wanted
    )
    return {str(r["issue"]): r["project"] for r in rows}


def _find_most_common(names: list[str | None]) -> str | None:
    """None on a tie: a priority project split evenly between Linear projects names neither."""
    ranked = Counter(n for n in names if n).most_common(2)
    if not ranked or (len(ranked) == 2 and ranked[0][1] == ranked[1][1]):
        return None
    return ranked[0][0]
