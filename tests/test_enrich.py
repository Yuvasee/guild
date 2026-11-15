import io
import json
import os
import re
import urllib.request
from pathlib import Path
from typing import cast

import pytest
from conftest import requires_github

from bridge.config import Config
from bridge.enrich import (
    CommitStatus,
    IssueStatus,
    PrStatus,
    fetch_issues,
    fetch_prs,
    map_attachments_to_linked_prs,
    map_node_to_pr,
    read_linear_key,
)
from bridge.serve import map_refs_to_links
from bridge.signals import SessionRefs, find_signals, map_enrichment_to_status, map_pr_to_status, pick_issue_owner


def pr(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "title": "Fix it", "state": "OPEN", "draft": False, "review": None, "checks": "SUCCESS",
        "mergeable": "MERGEABLE", "merge_state": "CLEAN", "in_merge_queue": False, "stacked": False,
        "url": "https://github.com/acme/web/pull/7", "head": "feat/x", "head_oid": "c1", "updated_at": "t1",
        "mine": True,
    }  # fmt: skip
    base.update(overrides)
    return base


def test_pr_status_labels() -> None:
    assert map_pr_to_status(pr(state="MERGED")).label == "merged"
    assert map_pr_to_status(pr(checks="FAILURE", review="APPROVED")).label == "CI failing"
    assert map_pr_to_status(pr(draft=True, checks="FAILURE")).label == "draft · CI failing"
    assert map_pr_to_status(pr(merge_state="DIRTY")).label == "conflicts"
    assert map_pr_to_status(pr(review="CHANGES_REQUESTED", merge_state="BLOCKED")).label == "changes requested"
    assert map_pr_to_status(pr(checks="PENDING", merge_state="BLOCKED")).label == "CI running"
    assert map_pr_to_status(pr(stacked=True)).label == "stacked"
    assert map_pr_to_status(pr(in_merge_queue=True)).label == "in merge queue"
    assert map_pr_to_status(pr(review="APPROVED")).label == "ready"
    assert map_pr_to_status(pr(merge_state="BEHIND")).label == "behind base"
    assert map_pr_to_status(pr(review="APPROVED", merge_state="BLOCKED")).label == "approved, blocked"


def test_stale_data_is_labelled_and_never_ready() -> None:
    stale = pr(review="APPROVED", stale=True, error="gh failed: timeout")
    status = map_enrichment_to_status("pr:acme/web#7", stale)
    assert status is not None and status.label == "ready (stale)" and "gh failed" in status.title
    assert find_signals([SessionRefs("S", "s", "feat/x", ["pr:acme/web#7"])], {"pr:acme/web#7": stale}) == []


def test_someone_elses_pr_gives_no_signal_and_no_colour() -> None:
    theirs = pr(review="APPROVED", mine=False, author="bob")
    status = map_enrichment_to_status("pr:acme/web#7", theirs)
    assert status is not None and (status.label, status.tone) == ("ready", "neutral") and "by bob" in status.title
    failing = pr(checks="FAILURE", mine=False, author="bob")
    sessions = [SessionRefs("S", "s", "feat/x", ["pr:acme/web#7", "pr:acme/web#8"])]
    assert find_signals(sessions, {"pr:acme/web#7": theirs, "pr:acme/web#8": failing}) == []
    # Cached before authorship was fetched: unknown counts as someone else's until the next refresh.
    unknown = {k: v for k, v in pr(review="APPROVED").items() if k != "mine"}
    assert find_signals([SessionRefs("S", "s", "feat/x", ["pr:acme/web#7"])], {"pr:acme/web#7": unknown}) == []
    mine = map_enrichment_to_status("pr:acme/web#7", pr(review="APPROVED", mine=True))
    assert mine is not None and mine.tone == "good"


NOW = 10 * 3_600_000


def issue(state: str, linked_prs: list[dict[str, object]], state_type: str = "started") -> dict[str, object]:
    return {
        "title": "t", "state": state, "state_type": state_type, "url": "https://t",
        "linked_prs": linked_prs, "linked_complete": True,
    }  # fmt: skip


def linked(number: int, status: str, closes: bool, merged_at: int | None) -> dict[str, object]:
    return {"ref": f"pr:acme/web#{number}", "status": status, "closes": closes, "merged_at": merged_at}


def test_signals_ready_ci_and_issue_drift_go_to_the_owning_session() -> None:
    enrichment: dict[str, dict[str, object]] = {
        "pr:acme/web#7": pr(review="APPROVED"),
        "pr:acme/web#6": pr(review="APPROVED", stacked=True, base="feat/w"),
        "pr:acme/web#5": pr(review="APPROVED", merge_state="BLOCKED"),
        "pr:acme/web#8": pr(checks="FAILURE", head="feat/y", head_oid="c2"),
        "pr:acme/web#9": pr(state="MERGED", head="feat/abc-1-z", title="Fix it"),
        "issue:ABC-1": issue("In Progress", [linked(9, "merged", True, 1_000), linked(4, "closed", False, None)]),
        "issue:ABC-2": issue("Done", [linked(9, "merged", True, 1_000)], state_type="completed"),
        "issue:ABC-11": issue("Duplicate", [linked(9, "merged", True, 1_000)], state_type="duplicate"),
        # Another PR on the issue is still open, so the merge of #9 says nothing about the issue.
        "issue:ABC-3": issue("Todo", [linked(9, "merged", True, 1_000), linked(10, "open", True, None)]),
        # Merged too recently: a follow-up PR may still come.
        "issue:ABC-4": issue("In Progress", [linked(11, "merged", True, NOW - 600_000)]),
        # Only mentioned by its PRs, never meant to be closed by them.
        "issue:ABC-5": issue("In Progress", [linked(12, "merged", False, 1_000)]),
        "issue:ABC-6": issue("In Progress", []),
        # In QA or In Review with nothing left open is as likely stale as any other open state.
        "issue:ABC-7": issue("In QA", [linked(13, "merged", True, 1_000)]),
        # Linear still says closed, but our fresh GitHub data says #8 is open again.
        "issue:ABC-8": issue("In Progress", [linked(8, "closed", False, None), linked(14, "merged", True, 1_000)]),
        # Linear lost the merge time: the quiet period cannot be checked.
        "issue:ABC-9": issue("In Progress", [linked(15, "merged", True, None)]),
        "issue:ABC-10": {**issue("In Progress", [linked(16, "merged", True, 1_000)]), "linked_complete": False},
    }
    sessions = [
        SessionRefs("S-captain", "captain", "main", ["pr:acme/web#8"], {"pr:acme/web#8": 50}),
        SessionRefs("S-other", "other", "main", ["pr:acme/web#7", "pr:acme/web#8"], {"pr:acme/web#8": 10}),
        SessionRefs("S-owner", "owner", "feat/x", ["pr:acme/web#7", "pr:acme/web#6", "pr:acme/web#5"]),
        SessionRefs(
            "S-merge",
            "merger",
            "feat/abc-1-z",
            ["pr:acme/web#9", *(f"issue:ABC-{n}" for n in range(1, 12))],
        ),
        SessionRefs("S-qa", "qa", "feat/abc-7", ["issue:ABC-7"]),
    ]

    found = find_signals(sessions, enrichment, at_ms=NOW)
    signals = {s.kind: s for s in found if s.kind != "drift"}
    drift = {s.key: s for s in found if s.kind == "drift"}

    assert set(signals) == {"ready", "ci"}
    assert signals["ready"].handle == "owner"
    assert signals["ready"].text == "web#7 Fix it", "the kind label already says what happened"
    assert signals["ci"].handle == "other"
    assert set(drift) == {"issue:ABC-1:done?:In Progress", "issue:ABC-7:done?:In QA"}, "dismissal holds until it moves"
    assert drift["issue:ABC-1:done?:In Progress"].text == (
        "ABC-1 t: still In Progress, though web#9 merged and no linked PR is open (1 closed unmerged)."
        " Move it to Done if the work is done, or give it a truthful status."
    )
    assert drift["issue:ABC-1:done?:In Progress"].handle == "merger"
    assert drift["issue:ABC-7:done?:In QA"].handle == "qa", "the session on its branch, not the one naming it first"
    assert len(found) == 4, "stacked and blocked PRs, and issues with open, recent or non-closing PRs raise nothing"


def test_an_issue_done_signal_goes_where_the_router_says_and_nowhere_when_it_finds_no_owner() -> None:
    enrichment: dict[str, dict[str, object]] = {
        "issue:ABC-1": issue(
            "Backlog", [linked(9, "merged", True, 1_000), linked(4, "closed", False, None)], "backlog"
        ),
        "issue:ABC-2": issue("In Review", [linked(5, "merged", True, 1_000)]),
    }
    sessions = [SessionRefs("S-worker", "worker", "feat/abc-1", ["issue:ABC-1", "issue:ABC-2"])]
    asked: list[tuple[str, list[str], list[str]]] = []

    def route(ref: str, linked_refs: list[str], closing: list[str]) -> tuple[str | None, str | None] | None:
        asked.append((ref, linked_refs, closing))
        return ("S-lead", "lead") if ref == "issue:ABC-1" else None

    found = {s.key: s for s in find_signals(sessions, enrichment, at_ms=NOW, route_issue=route)}

    assert (found["issue:ABC-1:done?:Backlog"].surface, found["issue:ABC-1:done?:Backlog"].handle) == ("S-lead", "lead")
    assert "issue:ABC-2:done?:In Review" not in found, "a session that only mentions it is no owner"
    assert asked[0] == ("issue:ABC-1", ["pr:acme/web#9", "pr:acme/web#4"], ["pr:acme/web#9"])


def test_an_issue_with_many_merged_prs_names_a_few_and_counts_the_rest() -> None:
    enrichment: dict[str, dict[str, object]] = {
        "issue:ABC-1": issue("In Progress", [linked(n, "merged", True, 1_000) for n in range(1, 101)]),
    }
    [signal] = find_signals([SessionRefs("S", "s", "feat/abc-1", ["issue:ABC-1"])], enrichment, at_ms=NOW)
    assert "web#1, web#2, web#3, web#4, web#5 and 95 more merged" in signal.text


def test_issue_owner_matches_the_whole_key_in_the_branch() -> None:
    sessions = [
        SessionRefs("A", "ten", "feat/abc-10-x", ["issue:ABC-1"], {"issue:ABC-1": 1}),
        SessionRefs("B", "one", "feat/abc-1-x", ["issue:ABC-1"], {"issue:ABC-1": 9}),
    ]
    owner = pick_issue_owner("issue:ABC-1", sessions)
    assert owner is not None and owner.handle == "one"
    assert pick_issue_owner("issue:ABC-1", sessions[:1]) is None, "naming it first makes no owner"


def proj_77(**assignee: object) -> dict[str, object]:
    """A colleague's issue whose four closing PRs merged long ago, only named in a lead's message."""
    prs = [linked(n, "merged", True, 1_000) for n in (970, 71, 72, 73)]
    return {**issue("Technical Design", prs, "unstarted"), **assignee}


def test_an_issue_assigned_to_a_colleague_raises_no_drift_whoever_would_own_it() -> None:
    sessions = [SessionRefs("S-eli", "eli", "feat/proj-77", ["issue:PROJ-77"], {"issue:PROJ-77": 1})]

    def route(ref: str, linked_refs: list[str], closing: list[str]) -> tuple[str | None, str | None] | None:
        return ("S-eli", "eli")

    def drift(data: dict[str, object]) -> list[str]:
        found = find_signals(sessions, {"issue:PROJ-77": data}, at_ms=NOW, route_issue=route)
        return [s.handle or "" for s in found if s.kind == "drift"]

    assert drift(proj_77(assignee="Kai Doe", assignee_is_me=False)) == []
    assert drift(proj_77(assignee="Kai Doe")) == [], "cached before isMe was read: a colleague's until refreshed"
    assert drift(proj_77(assignee="Theo Roe", assignee_is_me=True)) == ["eli"]
    assert drift(proj_77(assignee=None)) == ["eli"], "an unassigned issue is the team's"


def test_a_blocked_issue_raises_no_drift() -> None:
    sessions = [SessionRefs("S-eli", "eli", "feat/proj-77", ["issue:PROJ-77"], {"issue:PROJ-77": 1})]
    prs = [linked(n, "merged", True, 1_000) for n in (970, 71)]

    def drift(state: str) -> int:
        found = find_signals(sessions, {"issue:PROJ-77": issue(state, prs, "started")}, at_ms=NOW)
        return sum(s.kind == "drift" for s in found)

    assert drift("In Progress") == 1
    assert drift("Blocked") == 0


def test_linear_says_whether_the_assignee_is_the_key_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    def answer(assignee: dict[str, object] | None) -> bytes:
        node: dict[str, object] = {
            "title": "t", "url": "u", "state": {"name": "Todo", "type": "unstarted"}, "assignee": assignee,
            "attachments": {"nodes": [], "pageInfo": {"hasNextPage": False}},
        }  # fmt: skip
        return json.dumps({"data": {"i0": node}}).encode()

    sent: list[bytes] = []
    replies: list[bytes] = []

    class Response(io.BytesIO):
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_: object) -> None:
            return None

    def urlopen(request: urllib.request.Request, timeout: float) -> Response:
        sent.append(cast(bytes, request.data))
        return Response(replies.pop(0))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    statuses: list[object] = []
    assignees: list[dict[str, object] | None] = [
        {"name": "Kai Doe", "isMe": False},
        {"name": "Theo", "isMe": True},
        None,
    ]
    for assignee in assignees:
        replies.append(answer(assignee))
        statuses.append(fetch_issues(["issue:PROJ-77"], "lin_api_x")["issue:PROJ-77"])
    assert [(s.assignee, s.assignee_is_me) for s in statuses if isinstance(s, IssueStatus)] == [
        ("Kai Doe", False),
        ("Theo", True),
        (None, False),
    ]
    assert b"assignee { name isMe }" in sent[0]


def test_linked_prs_from_linear_attachments() -> None:
    def gh(**meta: object) -> dict[str, object]:
        return {"sourceType": "github", "metadata": meta}

    merged = {"status": "merged", "linkKind": "closes", "mergedAt": "2026-10-18T07:35:08Z"}
    nodes: list[object] = [gh(repoLogin="acme", repoName="web", number="7", **merged), {"sourceType": "slack"}]
    linked, complete = map_attachments_to_linked_prs(nodes)
    assert [(p.ref, p.closes, p.merged_at is not None) for p in linked] == [("pr:acme/web#7", True, True)]
    assert complete
    assert map_attachments_to_linked_prs([gh(repoLogin="acme", number=8, status="open")])[1] is False


def test_malformed_linear_key_is_reported_without_echo(tmp_path: Path) -> None:
    key_file = tmp_path / "key"
    key_file.write_text("lin_api_SECRETVALUE1234567890\nsecond line")
    key, problem = read_linear_key(Config(linear_api_key_file=str(key_file)))
    assert key is None and problem is not None and "SECRETVALUE" not in problem


def test_linear_key_from_env_file_line(tmp_path: Path) -> None:
    key_file = tmp_path / "linear"
    key_file.write_text('LINEAR_API_KEY="lin_api_fake_test_key_0000"\n')  # gitleaks:allow
    assert read_linear_key(Config(linear_api_key_file=str(key_file))) == ("lin_api_fake_test_key_0000", None)


@requires_github
def test_fetch_prs_from_github() -> None:
    """Real GitHub: a merged public PR, and a number that does not exist."""
    results = fetch_prs(["pr:cli/cli#1", "pr:cli/cli#999999999"])
    merged = results["pr:cli/cli#1"]
    assert isinstance(merged, PrStatus)
    assert (merged.state, merged.head, merged.stacked) == ("MERGED", "gh-pr", False)
    assert merged.merge_state and merged.head_oid
    assert merged.author and not merged.mine
    assert results["pr:cli/cli#999999999"] == "not found or no access"


def test_fetch_issues_from_linear() -> None:
    key_file = os.environ.get("BRIDGE_TEST_LINEAR_KEY_FILE")
    issue = os.environ.get("BRIDGE_TEST_LINEAR_ISSUE")
    if not key_file or not issue:
        pytest.skip("set BRIDGE_TEST_LINEAR_KEY_FILE and BRIDGE_TEST_LINEAR_ISSUE to test against Linear")
    key, problem = read_linear_key(Config(linear_api_key_file=key_file))
    assert key and problem is None
    missing = [f"issue:PROJ-99999{n}" for n in range(5)] + ["issue:NOPE-999999"]
    results = fetch_issues([*missing, f"issue:{issue}"], key)
    status = results[f"issue:{issue}"]
    assert isinstance(status, IssueStatus)
    assert status.state and status.state_type and status.url.startswith("https://linear.app/")
    assert all(re.fullmatch(r"pr:[\w.-]+/[\w.-]+#\d+", p.ref) and p.status for p in status.linked_prs)
    assert all(results[ref] == "not found or no access" for ref in missing)


def test_links_mark_merged_closed_and_done_refs() -> None:
    enrichment: dict[str, dict[str, object]] = {
        "pr:acme/web#1": {"state": "MERGED", "title": "a"},
        "pr:acme/web#2": {"state": "OPEN", "title": "b"},
        "issue:ABC-1": {"state": "Done", "state_type": "completed"},
        "issue:ABC-2": {"state": "In Progress", "state_type": "started"},
    }
    links = map_refs_to_links(None, None, list(enrichment), Config(), enrichment)
    assert {str(link["label"]): link["done"] for link in links} == {
        "web#1": True,
        "web#2": False,
        "ABC-1": True,
        "ABC-2": False,
    }


def test_a_prs_signal_goes_to_the_owner_its_router_names_and_else_to_the_session_that_touched_it() -> None:
    sessions = [
        SessionRefs("S-charlie", "charlie", None, ["pr:acme/web#7"], {"pr:acme/web#7": 1}),
        SessionRefs("S-worker", "worker", None, ["pr:acme/web#7"], {"pr:acme/web#7": 5}),
    ]
    enrichment = {"pr:acme/web#7": pr(checks="FAILURE")}
    asked: list[str] = []

    def route(ref: str, data: dict[str, object]) -> tuple[str | None, str | None] | None:
        asked.append(ref)
        return ("S-alex", "alex") if data.get("head") == "feat/x" else None

    [routed] = find_signals(sessions, enrichment, route_pr=route)
    [fallback] = find_signals(sessions, {"pr:acme/web#7": pr(checks="FAILURE", head="other")}, route_pr=route)
    quiet = find_signals(sessions, {"pr:acme/web#7": pr(merge_state="BLOCKED")}, route_pr=route)

    assert (routed.kind, routed.surface, routed.handle) == ("ci", "S-alex", "alex")
    assert (fallback.surface, fallback.handle) == ("S-charlie", "charlie")
    assert quiet == [] and asked == ["pr:acme/web#7"] * 2, "no owner lookup for a PR with nothing to say"


def test_red_ci_github_refused_to_run_tells_the_lead_why_and_asks_no_fix_only_at_that_head() -> None:
    sessions = [SessionRefs("S", "s", "feat/x", ["pr:Acme/Web#7"])]
    red = pr(checks="FAILURE", head_oid="c1")

    def signals(blocked: dict[str, tuple[str, str]] | None) -> list[tuple[str, str, str, str]]:
        found = find_signals(sessions, {"pr:Acme/Web#7": red}, ci_blocked=blocked)
        return [(s.kind, s.severity, s.key, s.text) for s in found]

    reason = "CI didn't run: GitHub billing"
    assert signals({"pr:acme/web#7": ("c1", reason)}) == [
        (
            "ci stopped",
            "info",
            "pr:Acme/Web#7:ci stopped:c1",
            "web#7 Fix it: CI didn't run: GitHub billing, not a code failure: rerun the failed jobs once GitHub runs "
            "them again.",
        )
    ], "no ci signal, which asks the lead to get CI green"
    assert signals({"pr:acme/web#7": ("c0", reason)}) == [("ci", "action", "pr:Acme/Web#7:ci:c1", "web#7 Fix it")]
    assert signals(None) == [("ci", "action", "pr:Acme/Web#7:ci:c1", "web#7 Fix it")]


STALL_CONTEXT = "Reviewbot / stall"


def stalled(state: str = "ERROR", red_check_runs: int | None = 0, *others: dict[str, str]) -> dict[str, object]:
    status = {"context": STALL_CONTEXT, "state": state, "description": "no verdict after one retry", "url": ""}
    return pr(checks="ERROR", head_oid="c9", statuses=[status, *others], red_check_runs=red_check_runs)


def test_a_red_stall_status_is_its_own_signal_and_alone_it_brings_no_ci_line() -> None:
    sessions = [SessionRefs("S", "s", "feat/x", ["pr:acme/web#7"])]

    def kinds(data: dict[str, object], context: str = STALL_CONTEXT) -> list[str]:
        return sorted(s.kind for s in find_signals(sessions, {"pr:acme/web#7": data}, stall_context=context))

    [signal] = find_signals(sessions, {"pr:acme/web#7": stalled()}, stall_context=STALL_CONTEXT, bot_name="ReviewBot")
    assert (signal.kind, signal.key) == ("stall", "pr:acme/web#7:stall:c9")
    assert signal.text == "ReviewBot stalled on web#7: no verdict after one retry"
    assert kinds(stalled("FAILURE")) == ["stall"]
    assert kinds(stalled(red_check_runs=2)) == ["ci", "stall"], "a failing check run is its own news"
    other = {"context": "deploy", "state": "FAILURE", "description": "", "url": ""}
    assert kinds(stalled("ERROR", 0, other)) == ["ci", "stall"]
    assert kinds(stalled(red_check_runs=None)) == ["ci", "stall"], "cached before runs were counted: cannot tell"
    assert kinds(pr(statuses=[{"context": STALL_CONTEXT, "state": "SUCCESS"}])) == ["ready"], "the verdict landed"
    assert kinds(stalled(), context="") == ["ci"], "no stall watch configured"
    assert kinds({**stalled(), "mine": False}) == [], "someone else's PR"


def test_github_status_contexts_and_failing_check_runs_are_read_from_the_head_commit() -> None:
    commit = {
        "statusCheckRollup": {
            "state": "ERROR",
            "contexts": {"checkRunCountsByState": [
                {"state": "SUCCESS", "count": 30}, {"state": "FAILURE", "count": 1}, {"state": "TIMED_OUT", "count": 2},
            ]},
        },
        "status": {"contexts": [
            {"context": STALL_CONTEXT, "state": "ERROR", "description": "stalled", "targetUrl": "https://x/run"},
        ]},
    }  # fmt: skip
    status = map_node_to_pr({"state": "OPEN", "commits": {"nodes": [{"commit": commit}]}})
    bare = map_node_to_pr({"state": "OPEN", "commits": {"nodes": [{"commit": {"statusCheckRollup": None}}]}})

    assert status.statuses == [CommitStatus(STALL_CONTEXT, "ERROR", "stalled", "https://x/run")]
    assert (status.red_check_runs, status.checks) == (3, "ERROR")
    assert (bare.statuses, bare.red_check_runs) == ([], None)


def test_a_stall_on_a_pr_only_a_leads_work_names_goes_where_its_router_says() -> None:
    declared = [SessionRefs(None, None, None, ["pr:acme/web#7"])]

    def route(ref: str, data: dict[str, object]) -> tuple[str | None, str | None] | None:
        return ("S-alex", "alex")

    [signal] = find_signals(declared, {"pr:acme/web#7": stalled()}, route_pr=route, stall_context=STALL_CONTEXT)
    assert (signal.kind, signal.surface) == ("stall", "S-alex")
