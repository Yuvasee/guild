"""The Linear adapter: issue state over Linear's GraphQL API with a personal key (`linear_api_key_file`)."""

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import cast

from bridge.config import Config
from bridge.trackers import IssueResults, IssueStatus, LinkedPr, iso_to_ms

_LINEAR_URL = "https://api.linear.app/graphql"
_LINEAR_KEY_RE = re.compile(r"[A-Za-z0-9_\-]{20,200}")


class LinearTracker:
    name = "linear"

    def __init__(self, config: Config) -> None:
        self.config = config

    def describe(self) -> str:
        key, problem = read_linear_key(self.config)
        if problem:
            return f"Linear ({problem})"
        return "Linear" if key else "Linear (no key: set linear_api_key_file in config.toml)"

    def fetch(self, refs: list[str]) -> IssueResults:
        key, problem = read_linear_key(self.config)
        if key:
            return fetch_issues(refs, key)
        return dict.fromkeys(refs, problem) if problem else {}


def fetch_issues(refs: list[str], api_key: str) -> IssueResults:
    results: IssueResults = {}
    pending = list(refs)
    # Linear's issue field is non-null: one unknown key nulls the whole batch, so drop the failing aliases and retry.
    # Each retry removes at least one ref, so this ends.
    while pending:
        failed = _fetch_issue_batch(pending, api_key, results)
        for ref in failed:
            results[ref] = "not found or no access"
        pending = [ref for ref in pending if ref not in results]
    return results


def _fetch_issue_batch(refs: list[str], api_key: str, results: IssueResults) -> set[str]:
    """Fills results for one aliased query; returns the refs Linear reported as failing when it nulled the batch."""
    keys = {f"i{n}": ref for n, ref in enumerate(refs)}
    fields = " ".join(
        f"{alias}: issue(id: {json.dumps(ref.removeprefix('issue:'))}) "
        "{ title url state { name type } assignee { name } "
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
            title=str(_get(node, "title") or ""),
            state=str(_get(state, "name") or ""),
            state_type=str(_get(state, "type") or ""),
            assignee=_opt(_get(_get(node, "assignee"), "name")),
            url=str(_get(node, "url") or ""),
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
                merged_at=iso_to_ms(merged_at),
            )
        )
    return linked, complete


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


def _get(obj: object, key: str) -> object:
    return cast(dict[str, object], obj).get(key) if isinstance(obj, dict) else None


def _as_dict(obj: object) -> dict[str, object] | None:
    return cast(dict[str, object], obj) if isinstance(obj, dict) else None


def _first(value: object) -> str | None:
    return str(cast(list[object], value)[0]) if isinstance(value, list) and value else None


def _opt(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
