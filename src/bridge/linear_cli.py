import argparse
import re
import sqlite3
import sys
from pathlib import Path
from typing import cast

from bridge.agent_prefix import CLAUDE_PREFIX, TITLE_RULE, is_prefixed
from bridge.config import Config
from bridge.enrich import read_linear_key
from bridge.issue_sync import LinearApi, LinearError, LinearIssue, find_team_state

SEARCH_LIMIT = 20
_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*-\d+")
_SEARCH = """
query($term: String!, $first: Int!, $filter: IssueFilter) {
  searchIssues(term: $term, first: $first, filter: $filter) { nodes { identifier title url state { name } } }
}
"""
_SHOW = """
query($id: String!) {
  issue(id: $id) {
    identifier title url description state { name } team { key } assignee { name } project { name }
  }
}
"""
_ISSUE_ID = "query($id: String!) { issue(id: $id) { id identifier } }"
_TEAM = "query($key: String!) { teams(filter: { key: { eq: $key } }) { nodes { id } } }"
_PROJECT = """
query($name: String!, $team: String!) {
  projects(filter: { name: { eqIgnoreCase: $name }, accessibleTeams: { some: { key: { eq: $team } } } }, first: 5) {
    nodes { id }
  }
}
"""
_CREATE = """
mutation($input: IssueCreateInput!) { issueCreate(input: $input) { success issue { identifier url } } }
"""
_COMMENT = """
mutation($issueId: String!, $body: String!) {
  commentCreate(input: { issueId: $issueId, body: $body }) { success comment { url } }
}
"""


class LinearCliError(Exception):
    pass


def add_linear_commands(sub: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:  # pyright: ignore[reportPrivateUsage]
    linear = sub.add_parser("linear", help="Linear issues through its API, for when the Linear connector fails")
    linear_sub = linear.add_subparsers(dest="linear_command", required=True)
    p = linear_sub.add_parser("search", help="up to 20 issues matching the text, best match first")
    p.add_argument("text")
    p.add_argument("--team", help="a team key, e.g. ACME")
    p.set_defaults(func=run_linear, linear_run=search_issues)
    p = linear_sub.add_parser("show", help="one issue: state, team, project, assignee, link and description")
    p.add_argument("key")
    p.set_defaults(func=run_linear, linear_run=show_issue)
    p = linear_sub.add_parser("create", help="a new issue; prints its key and link. Search for a duplicate first")
    p.add_argument("--team", required=True, help="a team key, e.g. ACME")
    p.add_argument("--title", required=True)
    p.add_argument("--project", help="the project's name")
    p.add_argument("--description", help="its text, or - to read it from stdin")
    p.set_defaults(func=run_linear, linear_run=create_issue)
    p = linear_sub.add_parser("comment", help="a comment on an issue")
    p.add_argument("key")
    p.add_argument("body", help="its text, or - to read it from stdin")
    p.set_defaults(func=run_linear, linear_run=comment_issue)
    p = linear_sub.add_parser("move", help="an issue to another state of its team, by the state's name")
    p.add_argument("key")
    p.add_argument("state")
    p.set_defaults(func=run_linear, linear_run=move_issue)


def run_linear(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    del conn
    try:
        for line in args.linear_run(open_linear(config), args, config):
            print(line)
    except (LinearCliError, LinearError) as exc:
        print(f"guild linear: {exc}", file=sys.stderr)
        return 2
    return 0


def open_linear(config: Config) -> LinearApi:
    key, problem = read_linear_key(config)
    if problem:
        raise LinearCliError(problem)
    if key is None:
        where = Path(config.linear_api_key_file).expanduser() if config.linear_api_key_file else None
        raise LinearCliError(
            f"no Linear API key: {where} is missing or unreadable"
            if where
            else "no Linear API key: set linear_api_key_file in the config"
        )
    return LinearApi(key)


def search_issues(api: LinearApi, args: argparse.Namespace, config: Config) -> list[str]:
    del config
    variables: dict[str, object] = {"term": str(args.text), "first": SEARCH_LIMIT}
    if args.team:
        variables["filter"] = {"team": {"key": {"eq": str(args.team).upper()}}}
    nodes = _nodes(_get(api.query(_SEARCH, variables), "searchIssues"))
    if not nodes:
        return ["no issue matches"]
    return [f"{n.get('identifier')}  [{_get(n.get('state'), 'name')}]  {n.get('title')}  {n.get('url')}" for n in nodes]


def show_issue(api: LinearApi, args: argparse.Namespace, config: Config) -> list[str]:
    del config
    issue = _get(api.query(_SHOW, {"id": _read_key(args.key)}), "issue")
    if not isinstance(issue, dict):
        raise LinearCliError(f"{_read_key(args.key)}: not found or no access")
    node = cast(dict[str, object], issue)
    lines = [
        f"{node.get('identifier')}  {node.get('title')}",
        f"state: {_get(node.get('state'), 'name')}",
        f"team: {_get(node.get('team'), 'key')}",
        f"project: {_get(node.get('project'), 'name') or '-'}",
        f"assignee: {_get(node.get('assignee'), 'name') or '-'}",
        f"url: {node.get('url')}",
    ]
    description = str(node.get("description") or "").strip()
    return [*lines, "", description] if description else lines


def create_issue(api: LinearApi, args: argparse.Namespace, config: Config) -> list[str]:
    del config
    title = str(args.title).strip()
    if not title:
        raise LinearCliError("the title is empty")
    if is_prefixed(title):
        raise LinearCliError(f"{TITLE_RULE}; retitle it")
    team = str(args.team).upper()
    teams = _nodes(_get(api.query(_TEAM, {"key": team}), "teams"))
    if len(teams) != 1:
        raise LinearCliError(f"no team with the key {team}")
    issue_input: dict[str, object] = {"teamId": teams[0].get("id"), "title": title}
    if args.project:
        projects = _nodes(_get(api.query(_PROJECT, {"name": str(args.project), "team": team}), "projects"))
        if len(projects) != 1:
            found = "none" if not projects else f"{len(projects)}"
            raise LinearCliError(f"{team}'s projects named '{args.project}': {found}; give its exact name")
        issue_input["projectId"] = projects[0].get("id")
    if args.description is not None:
        issue_input["description"] = _with_prefix(_read_text(args.description))
    created = _get(api.query(_CREATE, {"input": issue_input}), "issueCreate")
    if _get(created, "success") is not True:
        raise LinearCliError("Linear did not confirm the new issue")
    issue = _get(created, "issue")
    return [f"{_get(issue, 'identifier')}  {_get(issue, 'url')}"]


def comment_issue(api: LinearApi, args: argparse.Namespace, config: Config) -> list[str]:
    del config
    body = _read_text(args.body)
    if not body.strip():
        raise LinearCliError("the comment is empty")
    issue_id = _find_issue_id(api, _read_key(args.key))
    created = _get(api.query(_COMMENT, {"issueId": issue_id, "body": _with_prefix(body)}), "commentCreate")
    if _get(created, "success") is not True:
        raise LinearCliError("Linear did not confirm the comment")
    return [f"commented: {_get(created, 'comment', 'url')}"]


def move_issue(api: LinearApi, args: argparse.Namespace, config: Config) -> list[str]:
    key = _read_key(args.key)
    found = api.fetch_issues([key])[key]
    if not isinstance(found, LinearIssue):
        raise LinearCliError(f"{key}: {found}")
    target = find_team_state(found, str(args.state))
    if target is None:
        names = ", ".join(s.name for s in found.team_states)
        raise LinearCliError(f"{key}'s team has no state '{args.state}'; its states: {names}")
    if target.id == found.state.id:
        return [f"{key} is already in {found.state.name}"]
    if found.state.name.lower() in config.grooming_states:
        raise LinearCliError(f"{key} is in {found.state.name}: it leaves that state only through grooming, not by hand")
    return [f"{key}: {found.state.name} -> {api.move(found.id, target.id)}"]


def _find_issue_id(api: LinearApi, key: str) -> str:
    issue = _get(api.query(_ISSUE_ID, {"id": key}), "issue")
    issue_id = _get(issue, "id")
    if not isinstance(issue_id, str):
        raise LinearCliError(f"{key}: not found or no access")
    return issue_id


def _read_key(raw: object) -> str:
    key = str(raw).strip()
    if not _KEY_RE.fullmatch(key):
        raise LinearCliError(f"'{key}' is not an issue key like ACME-12")
    return key.upper()


def _read_text(value: object) -> str:
    return sys.stdin.read() if value == "-" else str(value)


def _with_prefix(text: str) -> str:
    text = text.strip()
    if not text or text.startswith(CLAUDE_PREFIX):
        return text
    return f"{CLAUDE_PREFIX} {text}"


def _get(obj: object, *path: str) -> object:
    for step in path:
        obj = cast(dict[str, object], obj).get(step) if isinstance(obj, dict) else None
    return obj


def _nodes(connection: object) -> list[dict[str, object]]:
    nodes = _get(connection, "nodes")
    if not isinstance(nodes, list):
        return []
    return [cast(dict[str, object], n) for n in cast(list[object], nodes) if isinstance(n, dict)]
