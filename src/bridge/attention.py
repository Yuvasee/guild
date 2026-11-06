import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import cast

from bridge.reviews import FIXED, WAITING_FOR_OWNER, WAITING_FOR_YOU

MARKED = 3
QUESTION_MINUTES = 1
MINUTES_PER_LINE = 0.1
MINUTES_ROUNDED_TO = 5
PROJECT_NAME_MAX_CHARS = 40
ESTIMATE_RULE = "Rough, from size only: a minute per 10 lines, at least 5 minutes. A question, 1 minute."
_ISSUE_KEY_RE = re.compile(r"[A-Z]{2,}-\d+")
_NO_PROJECT = 1 << 30


class PriorityError(Exception):
    pass


@dataclass(frozen=True)
class Project:
    name: str
    issue_keys: tuple[str, ...]


@dataclass(frozen=True)
class Question:
    id: int
    asked_at: int
    urgent: bool
    asked_by: str


@dataclass(frozen=True)
class QueuedPr:
    ref: str
    label: str
    group: str
    state: str
    loc: int
    first_seen_at: int
    fixed_at: int | None
    stack_parent: str | None
    issue_keys: tuple[str, ...]
    resolve_threads: int = 0
    not_ready: bool = False
    """Its reviews are missing, so it does not wait for the owner yet."""
    depends_on: str | None = None
    """The PR its body says it merges only after."""


@dataclass(frozen=True)
class Ranked:
    key: str
    mark: int | None
    why: str
    estimate: str | None


def map_streams_to_ranking(questions: list[Question], prs: list[QueuedPr], projects: list[Project]) -> list[Ranked]:
    """PRs only the owner's own answered threads keep from merging, in any group, then urgent questions, other
    questions, PRs with fixes after the owner's comments, other PRs waiting for the owner. A PR with the owner's
    comments still open waits on the lead that owns it, so it is not ranked, like a PR whose reviews are missing,
    published PRs and drafts in progress. A PR whose parent, its stack base or the PR it merges after, is listed in
    its group follows that parent, and is not ranked while that parent is not."""
    parents = map_prs_to_parents(prs)
    blocked_by_each = _map_prs_to_stack_children(prs)
    by_ref = {pr.ref: pr for pr in prs}
    rankable = _find_rankable_prs(prs, parents)
    prs = [pr for pr in prs if pr.ref in rankable]
    to_resolve = sorted((pr for pr in prs if pr.resolve_threads), key=lambda pr: (pr.first_seen_at, pr.ref))
    urgent = sorted((q for q in questions if q.urgent), key=lambda q: (q.asked_at, q.id))
    others = sorted((q for q in questions if not q.urgent), key=lambda q: (q.asked_at, q.id))
    waiting = [pr for pr in prs if pr.group == WAITING_FOR_YOU and not pr.resolve_threads and not pr.not_ready]
    fixed = sorted((pr for pr in waiting if pr.state == FIXED), key=lambda pr: (-(pr.fixed_at or 0), pr.loc, pr.ref))
    position = _map_projects_to_positions(projects)

    def sort_key(pr: QueuedPr) -> tuple[int, bool, int, int, str]:
        return (
            _find_project_position(pr, position),
            not blocked_by_each.get(pr.ref),
            pr.loc,
            pr.first_seen_at,
            pr.ref,
        )

    rest = sorted((pr for pr in waiting if pr.state == WAITING_FOR_OWNER), key=sort_key)
    unmarked = [
        *(Ranked(pr.ref, None, _describe_threads_to_resolve(pr), _convert_minutes_to_estimate(QUESTION_MINUTES))
          for pr in to_resolve),
        *(Ranked(f"question:{q.id}", None, "Urgent question.", _convert_minutes_to_estimate(QUESTION_MINUTES))
          for q in urgent),
        *(Ranked(f"question:{q.id}", None, f"A question: about a minute, and it unblocks {q.asked_by}. Oldest first.",
                 _convert_minutes_to_estimate(QUESTION_MINUTES))
          for q in others),
        *(Ranked(pr.ref, None, "Fixes were pushed after your comments, so your context is still fresh. Newest fix "
                 "first.", None)
          for pr in fixed),
        *(Ranked(pr.ref, None, _describe_waiting_pr(pr, projects, position, blocked_by_each),
                 _convert_lines_to_estimate(pr.loc))
          for pr in rest),
    ]  # fmt: skip
    unmarked = [
        Ranked(item.key, None, f"{item.why} {describe_parent(by_ref[item.key], by_ref[parents[item.key]])}",
               item.estimate)
        if item.key in parents else item
        for item in convert_items_to_parent_order(unmarked, lambda item: item.key, parents)
    ]  # fmt: skip
    return [
        Ranked(item.key, index + 1 if index < MARKED else None, item.why, item.estimate)
        for index, item in enumerate(unmarked)
    ]


def map_prs_to_parents(prs: list[QueuedPr]) -> dict[str, str]:
    """Each PR's parent, its stack base or else the PR its body names, when that parent is listed in the same group."""
    by_ref = {pr.ref.lower(): pr for pr in prs}
    parents: dict[str, str] = {}
    for pr in prs:
        for named in (pr.stack_parent, pr.depends_on):
            parent = by_ref.get((named or "").lower())
            if parent is not None and parent.ref != pr.ref and parent.group == pr.group:
                parents[pr.ref] = parent.ref
                break
    return parents


def describe_parent(pr: QueuedPr, parent: QueuedPr) -> str:
    if (pr.stack_parent or "").lower() == parent.ref.lower():
        return f"Stacked on {parent.label}, so it follows it."
    return f"Merges after {parent.label}, so it follows it."


def _find_rankable_prs(prs: list[QueuedPr], parents: dict[str, str]) -> set[str]:
    """An open PR cannot jump ahead of an open parent that is not ranked yet: its reviews missing, or the owner's
    comments open. Past review, the parent needs nothing from the owner, so a child's threads stay ranked."""
    waiting = {pr.ref for pr in prs if pr.group == WAITING_FOR_YOU}
    rankable = {
        pr.ref
        for pr in prs
        if pr.resolve_threads
        or (pr.group == WAITING_FOR_YOU and not pr.not_ready and pr.state in (FIXED, WAITING_FOR_OWNER))
    }
    changed = True
    while changed:
        held = {ref for ref in rankable & waiting if ref in parents and parents[ref] not in rankable}
        rankable -= held
        changed = bool(held)
    return rankable


def convert_items_to_parent_order[T](items: list[T], key: Callable[[T], str], parents: dict[str, str]) -> list[T]:
    """Each item right after its parent, in the order the children had; the rest keep their order. A cycle of
    parents keeps the order of its first item."""
    keys = {key(item) for item in items}
    children: dict[str, list[T]] = {}
    for item in items:
        parent = parents.get(key(item))
        if parent in keys:
            children.setdefault(parent, []).append(item)
    ordered: list[T] = []
    placed: set[str] = set()

    def place(item: T) -> None:
        if key(item) in placed:
            return
        placed.add(key(item))
        ordered.append(item)
        for child in children.get(key(item), []):
            place(child)

    for item in items:
        if parents.get(key(item)) not in keys:
            place(item)
    for item in items:
        place(item)
    return ordered


def _map_prs_to_stack_children(prs: list[QueuedPr]) -> dict[str, list[str]]:
    children: dict[str, list[str]] = {}
    for pr in prs:
        if pr.stack_parent:
            children.setdefault(pr.stack_parent, []).append(pr.label)
    return children


def _map_projects_to_positions(projects: list[Project]) -> dict[str, int]:
    position: dict[str, int] = {}
    for index, project in enumerate(projects):
        for key in project.issue_keys:
            position.setdefault(key.upper(), index)
    return position


def _find_project_position(pr: QueuedPr, position: dict[str, int]) -> int:
    return min((position[key.upper()] for key in pr.issue_keys if key.upper() in position), default=_NO_PROJECT)


def _describe_waiting_pr(
    pr: QueuedPr, projects: list[Project], position: dict[str, int], children: dict[str, list[str]]
) -> str:
    index = _find_project_position(pr, position)
    project = (
        f"project {projects[index].name}, {index + 1} of {len(projects)}" if index != _NO_PROJECT else "no project"
    )
    blocks = f"; blocks {', '.join(children[pr.ref])}" if children.get(pr.ref) else ""
    return f"Open: {project}{blocks}; {pr.loc} lines, smaller first, then older."


def _describe_threads_to_resolve(pr: QueuedPr) -> str:
    threads = "your thread" if pr.resolve_threads == 1 else f"your {pr.resolve_threads} threads"
    return f"Resolve {threads}: answered, and all that keeps it from merging."


def _convert_lines_to_estimate(loc: int) -> str:
    rounded = max(1, round(loc * MINUTES_PER_LINE / MINUTES_ROUNDED_TO)) * MINUTES_ROUNDED_TO
    return _convert_minutes_to_estimate(rounded)


def _convert_minutes_to_estimate(minutes: int) -> str:
    return f"~{minutes} min" if minutes < 60 else f"~{round(minutes / 60)} h"


def map_arg_to_project(text: str) -> Project:
    name, sep, keys = text.partition("=")
    name = " ".join(name.split())
    issue_keys = tuple(key.strip().upper() for key in keys.split(",") if key.strip())
    if not sep or not name or len(name) > PROJECT_NAME_MAX_CHARS or not issue_keys:
        raise PriorityError(f"{text!r}: write name=PROJ-1,PROJ-2")
    bad = [key for key in issue_keys if not _ISSUE_KEY_RE.fullmatch(key)]
    if bad:
        raise PriorityError(f"not an issue key: {', '.join(bad)}")
    return Project(name, issue_keys)


def store_projects(conn: sqlite3.Connection, projects: list[Project], set_by: str, at_ms: int) -> None:
    if len({project.name.lower() for project in projects}) != len(projects):
        raise PriorityError("a project is named twice")
    conn.execute("DELETE FROM project_priority")
    conn.executemany(
        "INSERT INTO project_priority (position, name, issue_keys, set_at, set_by) VALUES (?, ?, ?, ?, ?)",
        [(i, p.name, json.dumps(p.issue_keys), at_ms, set_by) for i, p in enumerate(projects)],
    )


def load_projects(conn: sqlite3.Connection) -> list[Project]:
    return [
        Project(str(r["name"]), tuple(cast(list[str], json.loads(str(r["issue_keys"])))))
        for r in conn.execute("SELECT name, issue_keys FROM project_priority ORDER BY position")
    ]


def map_page_to_ranked_page(
    guild: dict[str, object], reviews: dict[str, object], projects: list[Project]
) -> dict[str, object]:
    """Everything the page lists keeps to the ranking's order; unmarked items stay in order, without a number."""
    needs = cast(list[dict[str, object]], guild["needs"])
    prs = cast(list[dict[str, object]], reviews["prs"])
    queued = [map_review_to_queued_pr(pr) for pr in prs]
    ranking = map_streams_to_ranking(
        [_map_need_to_question(need) for need in needs if _is_open_question(need)], queued, projects
    )
    place = {item.key: index for index, item in enumerate(ranking)}
    ordered = convert_items_to_parent_order(
        sorted(prs, key=lambda pr: place.get(str(pr["ref"]), len(place))),
        lambda pr: str(pr["ref"]),
        map_prs_to_parents(queued),
    )
    return {
        **guild,
        "needs": sorted(needs, key=lambda need: _map_need_to_place(need, place)),
        "reviews": {**reviews, "prs": ordered},
        "attention": {
            "ranking": [asdict(item) for item in ranking],
            "priority": [asdict(project) for project in projects],
            "estimate_rule": ESTIMATE_RULE,
        },
    }


def _is_open_question(need: dict[str, object]) -> bool:
    return need["type"] == "post" and not need.get("delivering")


def _map_need_to_place(need: dict[str, object], place: dict[str, int]) -> int:
    if _is_open_question(need):
        return place[f"question:{need['id']}"]
    return len(place) + (1 if need.get("delivering") else 0)


def _map_need_to_question(need: dict[str, object]) -> Question:
    return Question(
        id=int(str(need["id"])),
        asked_at=int(str(need["at"])),
        urgent=bool(need.get("urgent")),
        asked_by=str(need["from"]),
    )


def map_review_to_queued_pr(pr: dict[str, object]) -> QueuedPr:
    fixed_at = pr.get("fixed_at")
    stack_parent = pr.get("stack_parent")
    return QueuedPr(
        ref=str(pr["ref"]),
        label=str(pr["label"]),
        group=str(pr["group"]),
        state=str(pr["state"]),
        loc=int(str(pr["loc"])),
        first_seen_at=int(str(pr["first_seen_at"])),
        fixed_at=int(str(fixed_at)) if fixed_at is not None else None,
        stack_parent=str(stack_parent) if stack_parent else None,
        issue_keys=tuple(cast(list[str], pr["issue_keys"])),
        resolve_threads=len(cast(list[object], pr.get("resolve_threads") or [])) if pr.get("resolve_is_last") else 0,
        not_ready=bool(pr.get("reviews_gap")),
        depends_on=str(pr["depends_on"]) if pr.get("depends_on") else None,
    )
