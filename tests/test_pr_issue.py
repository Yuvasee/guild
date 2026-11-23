import argparse
import io
import json
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from test_reviews import CONFIG as REVIEW_CONFIG
from test_reviews import add_session, make_pr

from bridge import linear_cli
from bridge.config import Config
from bridge.db import connect, transaction
from bridge.gbridge import may_need_guard
from bridge.gitinfo import RepoFacts
from bridge.hooks import handle_pre_tool_use
from bridge.install import convert_event_to_command, list_guard_globs
from bridge.issue_sync import LinearApi, LinearError
from bridge.linear_cli import (
    LinearCliError,
    add_linear_commands,
    comment_issue,
    create_issue,
    move_issue,
    run_linear,
    search_issues,
)
from bridge.refs import names_issue, needs_issue
from bridge.review_github import map_node_to_open_pr
from bridge.reviews import map_db_to_reviews, store_open_prs, tell_issue_missing
from bridge.title_guard import find_missing_issue_refusal

CONFIG = replace(REVIEW_CONFIG, issue_prefixes=("ACME", "OPS"))
FACTS = RepoFacts("/w/app", "dev/x", {"origin": "Org/app"})
SECRET = "lin_api_" + "k" * 30


def read_repo(_directory: str) -> RepoFacts | None:
    return FACTS


def refusal(command: str, conn: Any = None, config: Config = CONFIG, facts: RepoFacts | None = FACTS) -> str | None:
    return find_missing_issue_refusal(conn, config, command, "/w/app", lambda _d: facts)


def test_a_key_counts_in_the_branch_in_any_case_and_in_the_title_or_body_as_written() -> None:
    prefixes = ("ACME",)
    assert names_issue(prefixes, "dev/acme-12-export", "", "")
    assert names_issue(prefixes, "dev/x", "Export ACME-12", "")
    assert names_issue(prefixes, "dev/x", "", "Fixes ACME-12")
    assert not names_issue(prefixes, "dev/x", "acme-12", "TEAMACME-12 OTHER-3")
    assert names_issue((), "dev/x", "", ""), "no configured team: nothing is required"


def test_only_review_org_repos_need_an_issue_and_both_lists_exempt_one() -> None:
    assert needs_issue(CONFIG, "org/APP")
    assert not needs_issue(CONFIG, "Someone/app")
    assert not needs_issue(replace(CONFIG, pr_issue_exempt_repos=("Org/app",)), "org/app")
    assert not needs_issue(replace(CONFIG, review_excluded_repos=("org/app",)), "Org/app")
    assert not needs_issue(replace(CONFIG, issue_prefixes=()), "Org/app")


@pytest.mark.parametrize(
    "command",
    [
        "gh pr create --title 'Export' --body 'Adds export'",
        "gh pr create -t Export -b 'Adds export' -R Org/app",
        "cd /w/app && gh pr create --title Export --body x",
        "gh pr create --fill",
        "gh pr create -f",
        "gh pr create --fill-first --title Export",
        "GH_REPO=Org/app gh pr create -t Export -b x",
        "gh pr create -t Export -b \"$(cat <<'EOF'\nAdds export\nEOF\n)\"",
    ],
)
def test_gh_pr_create_naming_no_issue_is_refused(command: str) -> None:
    text = refusal(command)
    assert text is not None
    assert "names no issue (ACME-…, OPS-…) in its branch dev/x, title or body" in text
    assert "`guild linear search" in text and "'Fixes ACME-N'" in text


def test_fill_refusal_says_the_commits_are_not_read() -> None:
    assert "With --fill only the branch name is read" in str(refusal("gh pr create --fill"))
    assert "--fill" not in str(refusal("gh pr create --fill --body x"))


@pytest.mark.parametrize(
    "command",
    [
        "gh pr create --title 'ACME-12 Export' --body x",
        "gh pr create --title Export --body 'Fixes ACME-12'",
        "gh pr create --title Export --body \"$(cat <<'EOF'\nFixes OPS-3\nEOF\n)\"",
        "gh pr create --title Export --head dev/acme-12-export --body x",
        "gh pr create --title Export --head me:acme-12 --body x",
        'gh pr create --title Export --body "$BODY"',
        "gh pr create --title Export --body-file -",
        'gh pr create --title Export --body-file "$F"',
        "gh pr create --title Export --body-file /nonexistent/body.md",
        'cd "$DIR" && gh pr create --title Export --body x',
        "gh pr create -R Someone/app --title Export --body x",
        "gh pr list",
        "gh pr view 7 --json body",
        "echo gh pr create",
        "gh pr create --help",
        "gh pr create -h",
        "git switch -c dev/acme-12 && gh pr create -t Export -b x",
        "git checkout dev/y; gh pr create -t Export -b x",
        "(cd /w/other && pwd); gh pr create -t Export -b x",
        "false && cd /w/other; gh pr create -t Export -b x",
        "make && cd /w/other && gh pr create -t Export -b x",
    ],
)
def test_gh_pr_create_naming_an_issue_or_unreadable_passes(command: str) -> None:
    assert refusal(command) is None


def test_the_branch_of_the_checkout_counts() -> None:
    assert refusal("gh pr create -t Export -b x", facts=replace(FACTS, branch="dev/acme-7")) is None
    assert refusal("gh pr create -t Export -b x", facts=None) is None, "no checkout: no repo, nothing judged"


def test_a_body_file_is_read(tmp_path: Path) -> None:
    named = tmp_path / "named.md"
    named.write_text("Fixes ACME-12\n")
    bare = tmp_path / "bare.md"
    bare.write_text("Adds export\n")
    assert refusal(f"gh pr create -t Export --body-file {named}") is None
    assert refusal(f"gh pr create -t Export -F {bare}") is not None
    rewritten = f"printf 'Fixes ACME-12' > {bare}; gh pr create -t Export -F {bare}"
    assert refusal(rewritten) is None, "a body file the command may write first is not judged"
    on_host = find_missing_issue_refusal(
        cast(sqlite3.Connection, None),
        CONFIG,
        f"gh pr create -t Export -F {bare}",
        "/w/app",
        read_repo,
        read_files=False,
    )
    assert on_host is None, "a file on another machine is not read here"


def test_exempt_and_excluded_repos_pass() -> None:
    command = "gh pr create -t Export -b x"
    assert refusal(command, config=replace(CONFIG, pr_issue_exempt_repos=("org/APP",))) is None
    assert refusal(command, config=replace(CONFIG, review_excluded_repos=("Org/app",))) is None


def test_gh_pr_edit_that_drops_the_last_key_is_refused(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.db")
    with transaction(conn):
        store_open_prs(conn, [make_pr(issue_missing=False)], 500)
    dropped = refusal("gh pr edit 7 --body 'Adds export'", conn)
    assert dropped is not None and "Org/app#7's branch dev/x, title or new body" in dropped
    assert refusal("gh pr edit --body 'Adds export'", conn) is not None, "the checkout's branch names the PR"
    assert refusal("gh pr edit https://github.com/Org/app/pull/7 -b x", conn) is not None
    assert refusal("gh pr edit 7 --body 'Fixes ACME-3'", conn) is None
    assert refusal("gh pr edit 7 --title 'ACME-3 Export' --body x", conn) is None
    assert refusal("gh pr edit 7 --title Export", conn) is None, "the old body is not stored: it may hold the key"
    assert refusal("gh pr edit 8 --body x", conn) is None, "not in the queue"
    exempt = replace(CONFIG, pr_issue_exempt_repos=("Org/app",))
    other_checkout = RepoFacts("/w/web", "main", {"origin": "Org/web"})
    url_edit = "gh pr edit https://github.com/Org/app/pull/7 -b x"
    assert refusal(url_edit, conn, config=exempt, facts=other_checkout) is None, "the URL's repo is the one judged"
    with transaction(conn):
        conn.execute("UPDATE review_prs SET issue_missing = 1")
    assert refusal("gh pr edit 7 --body x", conn) is None, "it named none before: the queue tells its owner"


def test_the_hook_denies_with_the_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = connect(tmp_path / "t.db")
    monkeypatch.setattr("bridge.title_guard.read_repo_facts", read_repo)
    payload: dict[str, object] = {
        "session_id": "c", "cwd": "/w/app", "tool_name": "Bash",
        "tool_input": {"command": "gh pr create -t Export -b x"},
    }  # fmt: skip
    decision = json.loads(handle_pre_tool_use(conn, payload, CONFIG))["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert decision["permissionDecisionReason"].startswith("bridge: this PR names no issue")


@pytest.mark.parametrize("command", ["gh pr create -t Export -b x", "gh pr edit 7 -b x", "gh pr new --fill"])
def test_the_installed_gate_runs_the_hook_for_pr_create_and_edit(command: str) -> None:
    assert may_need_guard(list_guard_globs(), command)


def node(**changes: Any) -> Any:
    base: dict[str, Any] = {
        "number": 7, "title": "Export", "url": "u", "isDraft": False, "updatedAt": "U",
        "headRefName": "dev/x", "headRefOid": "b2", "baseRefName": "main", "additions": 5, "deletions": 1,
        "repository": {"nameWithOwner": "Org/app"}, "baseRef": None, "commits": {"nodes": []},
        "files": {"pageInfo": {"hasNextPage": False}, "nodes": []},
        "reviewRequests": {"nodes": []}, "reviews": {"nodes": []}, "latestReviews": {"nodes": []},
        "body": "Adds export.",
    }  # fmt: skip
    return {**base, **changes}


def test_the_poll_reads_whether_a_pr_names_an_issue() -> None:
    assert map_node_to_open_pr(node(), CONFIG).issue_missing
    assert not map_node_to_open_pr(node(body="Fixes ACME-1"), CONFIG).issue_missing
    assert not map_node_to_open_pr(node(headRefName="dev/acme-1"), CONFIG).issue_missing
    assert not map_node_to_open_pr(node(), replace(CONFIG, pr_issue_exempt_repos=("Org/app",))).issue_missing


def test_a_pr_naming_no_issue_is_marked_and_its_owner_told_once_per_head(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.db")

    def poll(pr_changes: dict[str, Any], now: int) -> list[str]:
        with transaction(conn):
            store_open_prs(conn, [make_pr(**pr_changes)], now)
            sent = tell_issue_missing(conn, CONFIG, now)
        return [str(conn.execute("SELECT body FROM messages WHERE id = ?", (m,)).fetchone()[0]) for m, _ in sent]

    no_owner = poll({"issue_missing": True}, 1000)
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
    first = poll({"issue_missing": True}, 2000)
    again = poll({"issue_missing": True}, 3000)
    view: Any = map_db_to_reviews(conn, CONFIG, 3000)
    new_head = poll({"issue_missing": True, "head_oid": "a2", "commits": (("a2", 2500),)}, 4000)
    named = poll({"issue_missing": False, "head_oid": "a2", "commits": (("a2", 2500),)}, 5000)
    view_named: Any = map_db_to_reviews(conn, CONFIG, 5000)
    dropped_again = poll({"issue_missing": True, "head_oid": "a2", "commits": (("a2", 2500),)}, 6000)

    assert no_owner == []
    assert first == [
        "https://github.com/Org/app/pull/7 (T) names no Linear issue; its CI needs one: `guild linear search|create`, "
        "then add 'Fixes ACME-N' to the body."
    ]
    assert again == []
    assert view["prs"][0]["issue_missing"] is True and view_named["prs"][0]["issue_missing"] is False
    assert len(new_head) == 1 and named == []
    assert len(dropped_again) == 1, "once it named one, losing it is told again"
    back_to_first_head = poll({"issue_missing": True}, 7000)
    assert len(back_to_first_head) == 1, "the marks were forgotten when it named one"
    assert poll({"issue_missing": True, "head_oid": "a2", "commits": (("a2", 2500),)}, 8000) == []
    assert poll({"issue_missing": True}, 9000) == [], "a head told before is not told again"


class FakeLinear:
    def __init__(self, answers: dict[str, object]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.moved: list[tuple[str, str]] = []

    def query(self, query: str, variables: dict[str, object]) -> object:
        self.calls.append((query, variables))
        for word, answer in self.answers.items():
            if word in query:
                return answer
        raise LinearError("linear error: unexpected query")

    def fetch_issues(self, keys: list[str]) -> dict[str, object]:
        from bridge.issue_sync import LinearIssue, TeamState

        states = (TeamState("s1", "Todo", "unstarted"), TeamState("s2", "In Progress", "started"),
                  TeamState("s3", "Grooming", "unstarted"))  # fmt: skip
        current = states[2] if keys[0] == "ACME-9" else states[0]
        return {keys[0]: LinearIssue("id-1", keys[0], current, states)}

    def move(self, issue_id: str, state_id: str) -> str:
        self.moved.append((issue_id, state_id))
        return "In Progress"


def args(**values: object) -> argparse.Namespace:
    return argparse.Namespace(**values)


def test_search_prints_one_line_per_issue_and_filters_by_team() -> None:
    api = FakeLinear(
        {"searchIssues": {"searchIssues": {"nodes": [
            {"identifier": "ACME-12", "title": "Export", "url": "https://t/ACME-12", "state": {"name": "Todo"}},
        ]}}}
    )  # fmt: skip
    lines = search_issues(api, args(text="export", team="acme"), CONFIG)  # type: ignore[arg-type]
    assert lines == ["ACME-12  [Todo]  Export  https://t/ACME-12"]
    assert api.calls[0][1]["filter"] == {"team": {"key": {"eq": "ACME"}}}
    empty = FakeLinear({"searchIssues": {"searchIssues": {"nodes": []}}})
    assert search_issues(empty, args(text="x", team=None), CONFIG) == ["no issue matches"]  # type: ignore[arg-type]
    assert "filter" not in empty.calls[0][1]


def test_create_finds_team_and_project_and_keeps_the_description_unprefixed(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeLinear(
        {
            "teams(": {"teams": {"nodes": [{"id": "team-1"}]}},
            "projects(": {"projects": {"nodes": [{"id": "proj-1"}]}},
            "issueCreate": {
                "issueCreate": {"success": True, "issue": {"identifier": "ACME-13", "url": "https://t/13"}}
            },
        }
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("Export fails on empty rows.\n"))
    lines = create_issue(
        cast(LinearApi, api), args(team="acme", title="Export fails", project="Exports", description="-"), CONFIG
    )
    assert lines == ["ACME-13  https://t/13"]
    assert api.calls[1][1] == {"name": "Exports", "team": "ACME"}
    sent = api.calls[-1][1]["input"]
    assert sent == {
        "teamId": "team-1", "title": "Export fails", "projectId": "proj-1",
        "description": "Export fails on empty rows.",
    }  # fmt: skip
    with pytest.raises(LinearCliError, match="never at the start of a PR, issue or commit title"):
        create_issue(api, args(team="ACME", title="🤖 Claude: x", project=None, description=None), CONFIG)  # type: ignore[arg-type]
    none = FakeLinear({"teams(": {"teams": {"nodes": []}}})
    with pytest.raises(LinearCliError, match="no team with the key NOPE"):
        create_issue(none, args(team="nope", title="x", project=None, description=None), CONFIG)  # type: ignore[arg-type]


def test_an_issues_first_comment_has_no_prefix_and_a_reply_gets_one() -> None:
    created = {"commentCreate": {"commentCreate": {"success": True, "comment": {"url": "https://t/c1"}}}}
    issue = {"id": "id-1", "identifier": "ACME-12"}
    first = FakeLinear({"issue(id": {"issue": {**issue, "comments": {"nodes": []}}}, **created})
    body = "🤖 Claude: Scope is the export only."
    lines = comment_issue(first, args(key="acme-12", body=body), CONFIG)  # type: ignore[arg-type]
    assert lines == ["commented: https://t/c1"]
    assert first.calls[0][1] == {"id": "ACME-12"}
    assert first.calls[1][1] == {"issueId": "id-1", "body": "Scope is the export only."}
    replied = FakeLinear({"issue(id": {"issue": {**issue, "comments": {"nodes": [{"id": "c0"}]}}}, **created})
    comment_issue(replied, args(key="acme-12", body="Done."), CONFIG)  # type: ignore[arg-type]
    assert replied.calls[1][1] == {"issueId": "id-1", "body": "🤖 Claude: Done."}
    api = first
    with pytest.raises(LinearCliError, match="is not an issue key"):
        comment_issue(api, args(key="12", body="x"), CONFIG)  # type: ignore[arg-type]


def test_move_by_state_name_and_never_out_of_grooming() -> None:
    api = FakeLinear({})
    config = replace(CONFIG, grooming_states=("grooming",))
    assert move_issue(api, args(key="ACME-12", state="in progress"), config) == [  # type: ignore[arg-type]
        "ACME-12: Todo -> In Progress"
    ]
    assert api.moved == [("id-1", "s2")]
    assert move_issue(api, args(key="ACME-12", state="todo"), config) == ["ACME-12 is already in Todo"]  # type: ignore[arg-type]
    with pytest.raises(LinearCliError, match="its states: Todo, In Progress, Grooming"):
        move_issue(api, args(key="ACME-12", state="Shipped"), config)  # type: ignore[arg-type]
    with pytest.raises(LinearCliError, match="only through grooming"):
        move_issue(api, args(key="ACME-9", state="Todo"), config)  # type: ignore[arg-type]


def test_the_key_comes_from_its_file_and_never_reaches_the_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key_file = tmp_path / "linear.key"
    parser = argparse.ArgumentParser()
    add_linear_commands(parser.add_subparsers(dest="command"))
    parsed = parser.parse_args(["linear", "show", "ACME-12"])

    missing = run_linear(parsed, None, replace(CONFIG, linear_api_key_file=str(key_file)))  # type: ignore[arg-type]
    assert missing == 2 and str(key_file) in capsys.readouterr().err

    key_file.write_text(f"LINEAR_API_KEY={SECRET}\n")
    seen: list[str] = []

    class Api(FakeLinear):
        def __init__(self, key: str) -> None:
            seen.append(key)
            super().__init__({})

        def query(self, query: str, variables: dict[str, object]) -> object:
            raise LinearError("linear HTTP 401")

    monkeypatch.setattr(linear_cli, "LinearApi", Api)
    failed = run_linear(parsed, None, replace(CONFIG, linear_api_key_file=str(key_file)))  # type: ignore[arg-type]
    out = capsys.readouterr()
    assert failed == 2 and seen == [SECRET]
    assert out.err == "guild linear: linear HTTP 401\n"
    assert SECRET not in out.out + out.err


@pytest.mark.parametrize("command", ["gh pr create -t Export -b x", "gh pr edit 7 -b x"])
def test_the_macs_shell_gate_starts_the_hook_for_pr_create_and_edit(command: str, tmp_path: Path) -> None:
    ran = tmp_path / "ran"
    hook = tmp_path / "hook"
    hook.write_text(f"#!/bin/sh\ncat > {ran}\n")
    hook.chmod(0o755)
    gate = convert_event_to_command(str(hook), "PreToolUse")
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    env = {"PATH": "/usr/bin:/bin", "BRIDGE_STATE_DIR": str(tmp_path)}
    subprocess.run(["/bin/sh", "-c", gate], input=payload, env=env, text=True, check=True)
    assert ran.read_text() == payload


@pytest.mark.parametrize(
    ("base", "refused"),
    [
        ("main", False),
        ("master", False),
        ("release/1.4.x", False),
        ("dev/x", True),
        ("develop", True),
        ("$BASE", False),
    ],
)
def test_a_pr_goes_to_main_or_a_release_branch_and_one_on_another_pr_is_sent_to_a_stack(
    tmp_path: Path, base: str, refused: bool
) -> None:
    """dev/x is an open PR's branch: a plain PR on it is still refused, as only `gh stack link` makes a real stack."""
    conn = connect(tmp_path / "t.db")
    with transaction(conn):
        store_open_prs(conn, [make_pr()], 500)
    for command in (f"gh pr create -t 'ACME-1 Export' -b x --base {base}", f"gh pr edit 8 -B {base}"):
        text = refusal(command, conn)
        assert (text is not None and f"base {base} is not main" in text and "gh stack link" in text) is refused


def test_the_base_rule_spares_repos_outside_the_org_and_a_pr_with_no_base(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.db")
    assert refusal("gh pr create -t 'ACME-1 Export' -b x --base develop -R Someone/app", conn) is None
    assert refusal("gh pr create -t 'ACME-1 Export' -b x", conn) is None
