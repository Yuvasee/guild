import os
import re
from pathlib import Path

import pytest
from conftest import needs_github

from bridge.config import Config
from bridge.enrich import IssueStatus, PrStatus, fetch_prs
from bridge.serve import map_refs_to_links
from bridge.signals import SessionRefs, find_signals, map_enrichment_to_status, map_pr_to_status, pick_issue_owner
from bridge.tracker_linear import fetch_issues, map_attachments_to_linked_prs, read_linear_key


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
        # Another PR on the issue is still open, so the merge of #9 says nothing about the issue.
        "issue:ABC-3": issue("Todo", [linked(9, "merged", True, 1_000), linked(10, "open", True, None)]),
        # Merged too recently: a follow-up PR may still come.
        "issue:ABC-4": issue("In Progress", [linked(11, "merged", True, NOW - 600_000)]),
        # Only mentioned by its PRs, never meant to be closed by them.
        "issue:ABC-5": issue("In Progress", [linked(12, "merged", False, 1_000)]),
        "issue:ABC-6": issue("In Progress", []),
        # Waiting in QA after a merge is the expected flow, not a forgotten issue.
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
            ["pr:acme/web#9", *(f"issue:ABC-{n}" for n in range(1, 11))],
        ),
    ]

    found = find_signals(sessions, enrichment, at_ms=NOW)
    signals = {s.kind: s for s in found}

    assert set(signals) == {"ready", "ci", "drift"}
    assert signals["ready"].handle == "owner"
    assert signals["ready"].text == "web#7 Fix it", "the kind label already says what happened"
    assert signals["ci"].handle == "other"
    assert signals["drift"].text == "ABC-1 t: 1 of 2 linked PRs merged, none open; still In Progress"
    assert signals["drift"].key == "issue:ABC-1:done?:In Progress", "dismissal holds until the issue moves"
    assert signals["drift"].handle == "merger"
    assert len(found) == 3, "stacked and blocked PRs, and issues with open, recent or non-closing PRs raise nothing"


def test_issue_owner_matches_the_whole_key_in_the_branch() -> None:
    sessions = [
        SessionRefs("A", "ten", "feat/abc-10-x", ["issue:ABC-1"], {"issue:ABC-1": 1}),
        SessionRefs("B", "one", "feat/abc-1-x", ["issue:ABC-1"], {"issue:ABC-1": 9}),
    ]
    assert pick_issue_owner("issue:ABC-1", sessions).handle == "one"


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
    key_file.write_text('LINEAR_API_KEY="lin_api_fake_test_key_0000"\n')
    assert read_linear_key(Config(linear_api_key_file=str(key_file))) == ("lin_api_fake_test_key_0000", None)


@needs_github
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
