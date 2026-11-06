"""Task tracker adapters: where an issue ref (`issue:ABC-12`) gets its title, state, link and linked PRs.

Bridge finds issue keys in text itself (`issue_prefixes`, refs.py); a tracker only answers for keys it is given.
`tracker` in config.toml picks one: "tasks" (a local Markdown or JSON file, the default), "exec" (any executable
that speaks JSON on stdin and stdout), "linear", or "none". docs/adapters.md has the contract.
"""

import json
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol, cast

from bridge.config import Config, config_path

STATE_TYPES = ("triage", "backlog", "unstarted", "started", "completed", "canceled")
EXEC_TIMEOUT_S = 30
NOT_FOUND = "not found or no access"
_MD_TASK_RE = re.compile(r"^\s*[-*+]\s+\[(.)\]\s+([A-Za-z][A-Za-z0-9]*-\d+)\b[\s:.\-\u2013\u2014]*(.*?)\s*$")
_MD_URL_RE = re.compile(r"\s*<?(https?://\S+?)>?\s*$")
_MD_MARKS = {" ": "unstarted", "x": "completed", "X": "completed", "-": "canceled", "~": "started", "/": "started"}
_MD_STATE_NAMES = {"unstarted": "Todo", "started": "In progress", "completed": "Done", "canceled": "Canceled"}
_PR_REF_RE = re.compile(r"(?:pr:)?([\w.-]+/[\w.-]+)#(\d+)$")
_PR_URL_RE = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")


@dataclass(frozen=True)
class LinkedPr:
    ref: str
    status: str
    """The tracker's view of the PR: open, draft, merged, closed, …"""
    closes: bool
    """The PR says it closes the issue (magic word or branch), not just mentions it."""
    merged_at: int | None


@dataclass(frozen=True)
class IssueStatus:
    title: str
    state: str
    """The tracker's own name for the state, shown as is."""
    state_type: str
    """One of STATE_TYPES: what bridge reasons with."""
    assignee: str | None
    url: str
    linked_prs: list[LinkedPr] = field(default_factory=list[LinkedPr])
    """Every PR the tracker has attached to the issue, from any session or none."""
    linked_complete: bool = False
    """False when the list may miss PRs: nothing may be concluded from it."""


IssueResults = dict[str, IssueStatus | str]


class Tracker(Protocol):
    name: str

    def describe(self) -> str:
        """One line for `bridge enrich`: which tracker, and what is wrong with its setup if anything."""
        ...

    def fetch(self, refs: list[str]) -> IssueResults:
        """refs are `issue:KEY`. Each answered ref maps to its status or to an error string; a ref left out is not
        stored, so the page shows nothing for it."""
        ...


def load_tracker(config: Config) -> Tracker | None:
    if config.tracker == "tasks":
        return TasksFileTracker(resolve_tasks_file(config))
    if config.tracker == "exec":
        return ExecTracker(config.tracker_command)
    if config.tracker == "linear":
        from bridge.tracker_linear import LinearTracker

        return LinearTracker(config)
    return None


def resolve_tasks_file(config: Config) -> Path:
    return Path(config.tasks_file).expanduser() if config.tasks_file else config_path().parent / "tasks.md"


class TasksFileTracker:
    """A Markdown checklist or a JSON file, read at each poll. A missing file answers nothing."""

    name = "tasks"

    def __init__(self, path: Path) -> None:
        self.path = path

    def describe(self) -> str:
        return f"tasks file {self.path}" + ("" if self.path.exists() else " (missing)")

    def fetch(self, refs: list[str]) -> IssueResults:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            issues = map_json_text_to_issues(text) if self.path.suffix == ".json" else map_markdown_to_issues(text)
        except ValueError as exc:
            return dict.fromkeys(refs, f"tasks file {self.path.name}: {exc}")
        return {ref: issues.get(ref.removeprefix("issue:").upper(), NOT_FOUND) for ref in refs}


class ExecTracker:
    """Runs the configured command once per poll with {"version": 1, "op": "fetch", "keys": [...]} on stdin and
    reads {"issues": {KEY: issue}, "errors": {KEY: "why"}} from stdout."""

    name = "exec"

    def __init__(self, argv: tuple[str, ...]) -> None:
        self.argv = argv

    def describe(self) -> str:
        return f"tracker command {' '.join(self.argv)}" if self.argv else "exec tracker without tracker_command"

    def fetch(self, refs: list[str]) -> IssueResults:
        if not self.argv or not refs:
            return dict.fromkeys(refs, "tracker_command is not set") if refs else {}
        keys = [ref.removeprefix("issue:") for ref in refs]
        argv = [os.path.expanduser(self.argv[0]), *self.argv[1:]]
        request = json.dumps({"version": 1, "op": "fetch", "keys": keys})
        try:
            done = subprocess.run(
                argv, input=request, capture_output=True, text=True, timeout=EXEC_TIMEOUT_S, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return dict.fromkeys(refs, f"tracker command failed: {type(exc).__name__}")
        if done.returncode != 0:
            last = (done.stderr.strip().splitlines() or [f"exit {done.returncode}"])[-1][:200]
            return dict.fromkeys(refs, f"tracker command failed: {last}")
        try:
            answer: object = json.loads(done.stdout)
            issues = map_json_to_issues(answer)
        except ValueError as exc:
            return dict.fromkeys(refs, f"tracker command answered no valid JSON: {str(exc)[:200]}")
        raw_errors = _get(answer, "errors")
        errors = cast(dict[str, object], raw_errors) if isinstance(raw_errors, dict) else {}
        results: IssueResults = {}
        for ref, key in zip(refs, keys, strict=True):
            found = issues.get(key.upper())
            results[ref] = found if found is not None else str(errors.get(key) or NOT_FOUND)[:200]
        return results


def map_markdown_to_issues(text: str) -> dict[str, IssueStatus]:
    """`- [ ] ABC-1 Title`; [x] done, [-] canceled, [~] or [/] in progress; a URL at the end of the line is its link."""
    issues: dict[str, IssueStatus] = {}
    for line in text.splitlines():
        match = _MD_TASK_RE.match(line)
        if match is None:
            continue
        mark, key, rest = match.groups()
        url_match = _MD_URL_RE.search(rest)
        url = url_match.group(1) if url_match else ""
        title = rest[: url_match.start()] if url_match else rest
        state_type = _MD_MARKS.get(mark, "unstarted")
        issues[key.upper()] = IssueStatus(title.strip(), _MD_STATE_NAMES[state_type], state_type, None, url)
    return issues


def map_json_text_to_issues(text: str) -> dict[str, IssueStatus]:
    return map_json_to_issues(json.loads(text))


def map_json_to_issues(data: object) -> dict[str, IssueStatus]:
    """{"issues": {KEY: issue}}, a bare {KEY: issue}, or a list of issues that each carry "key"."""
    if isinstance(data, dict) and "issues" in data:
        data = cast(dict[str, object], data)["issues"]
    if isinstance(data, list):
        items = cast(list[object], data)
        keyed = {str(_get(i, "key")): i for i in items if isinstance(_get(i, "key"), str)}
    elif isinstance(data, dict):
        keyed = cast(dict[str, object], data)
    else:
        raise ValueError("expected an object or a list of issues")
    return {key.upper(): map_json_to_issue(cast(object, v)) for key, v in keyed.items() if isinstance(v, dict)}


def map_json_to_issue(obj: object) -> IssueStatus:
    state = str(_get(obj, "state") or "")
    state_type = str(_get(obj, "state_type") or "")
    if state_type not in STATE_TYPES:
        state_type = guess_state_type(state)
    linked = [p for p in (_map_json_to_linked_pr(item) for item in _list(_get(obj, "linked_prs"))) if p is not None]
    assignee = _get(obj, "assignee")
    return IssueStatus(
        title=str(_get(obj, "title") or ""),
        state=state or state_type,
        state_type=state_type,
        assignee=assignee if isinstance(assignee, str) and assignee else None,
        url=str(_get(obj, "url") or ""),
        linked_prs=linked,
        linked_complete=_get(obj, "linked_complete") is True,
    )


def guess_state_type(state: str) -> str:
    words = state.lower()
    for state_type, hints in (
        ("canceled", ("cancel", "won't", "wont", "duplicate", "rejected")),
        ("completed", ("done", "closed", "complete", "resolved", "shipped", "merged", "released")),
        ("started", ("progress", "doing", "review", "started", "active", "qa", "testing")),
        ("backlog", ("backlog", "icebox", "later")),
        ("triage", ("triage", "inbox", "new")),
    ):
        if any(hint in words for hint in hints):
            return state_type
    return "unstarted"


def _map_json_to_linked_pr(item: object) -> LinkedPr | None:
    ref = str(_get(item, "ref") or _get(item, "url") or "")
    match = _PR_URL_RE.search(ref) or _PR_REF_RE.match(ref)
    if match is None:
        return None
    merged_at = _get(item, "merged_at")
    return LinkedPr(
        ref=f"pr:{match.group(1)}#{match.group(2)}",
        status=str(_get(item, "status") or "").lower(),
        closes=_get(item, "closes") is True,
        merged_at=merged_at if isinstance(merged_at, int) and not isinstance(merged_at, bool) else iso_to_ms(merged_at),
    )


def iso_to_ms(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value).timestamp() * 1000)
    except ValueError:
        return None


def _get(obj: object, key: str) -> object:
    return cast(Mapping[str, object], obj).get(key) if isinstance(obj, dict) else None


def _list(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []
