import contextlib
import dataclasses
import json
import time
from collections import Counter
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from test_reviews import CONFIG, make_pr

from bridge.backport import BackportError, check_backport, find_named_prs, map_diff_to_patch_ids
from bridge.config import map_toml_to_config
from bridge.db import connect, transaction
from bridge.guard import find_merge_blocks, map_command_to_targets, map_merge_blocks_to_lines
from bridge.review_github import GitHubError

REPO = "Acme/Web"
BASES = ("release/*",)


def file_diff(path: str, line: int, old: str, new: str, context: str = "ctx") -> str:
    return (
        f"diff --git a/{path} b/{path}\nindex 1a2b3c..4d5e6f 100644\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -{line},3 +{line},3 @@\n {context}\n-{old}\n+{new}\n {context}\n"
    )


MAIN_FIX = file_diff("app/a.py", 10, "x = 1", "x = 2")
OTHER_FIX = file_diff("app/a.py", 80, "y = 1", "y = 2") + file_diff("app/b.py", 3, "z = 1", "z = 2")


class FakeGitHub:
    """Answers the gh calls of a backport check from per-PR fixtures; a number missing from `diffs` fails like gh."""

    def __init__(self, body: str, diffs: dict[int, str], kinds: dict[int, tuple[str, bool, str]], base: str) -> None:
        self.body, self.diffs, self.kinds, self.base = body, diffs, kinds, base

    def diff(self, repo: str, number: int, deadline: float) -> bytes:
        assert repo == REPO and deadline > time.monotonic()
        if number not in self.diffs:
            raise GitHubError("gh failed: no pull requests found")
        return self.diffs[number].encode()

    def __call__(self, args: list[str], timeout_s: float = 3) -> str:
        assert timeout_s <= 3.8
        query = next(a for a in args if a.startswith("query="))
        if "pullRequest(number:" in query:
            pr = {"title": "backport to 1.13", "body": self.body, "headRefName": "me/proj-1-backport-1.13",
                  "baseRefName": self.base}  # fmt: skip
            return json.dumps({"data": {"repository": {"defaultBranchRef": {"name": "main"}, "pullRequest": pr}}})
        nodes = {
            f"n{n}": {"__typename": kind, **({"merged": merged, "baseRefName": into} if kind == "PullRequest" else {})}
            for n, (kind, merged, into) in self.kinds.items()
        }
        return json.dumps({"data": {"repository": nodes}})


@contextlib.contextmanager
def stub(gh: FakeGitHub) -> Generator[None]:
    with mock.patch("bridge.backport.run_gh", side_effect=gh), mock.patch("bridge.backport._fetch_diff", gh.diff):
        yield


def check(gh: FakeGitHub) -> Any:
    with stub(gh):
        return check_backport(REPO, 59, BASES, time.monotonic() + 3.8)


def ids(diff: str | bytes) -> Counter[str]:
    return map_diff_to_patch_ids(diff.encode() if isinstance(diff, str) else diff, 5)


MERGED = ("PullRequest", True, "main")


def test_a_backport_names_main_prs_by_number_repo_ref_url_or_branch() -> None:
    body = (
        "Backport of https://github.com/acme/web/pull/52 and Acme/Web#46, see #60; not Other/Repo#7, "
        "github.com/Other/Repo/pull/8, &#38; or `#L12`; itself is #59."
    )
    assert find_named_prs(REPO, 59, "fix (#61)", body, "me/proj-56-backport-1.13") == (46, 52, 60, 61)
    assert find_named_prs(REPO, 59, "", "", "backport-pr-52-to-release-1.13") == (52,)
    assert find_named_prs(REPO, 59, "", "", "backport/1.13.x") == ()
    assert find_named_prs(REPO, 59, "", "#" + "9" * 4301, "") == ()


def test_patch_ids_ignore_line_numbers_and_blob_ids_but_not_whitespace_or_the_function() -> None:
    moved = file_diff("app/a.py", 200, "x = 1", "x = 2").replace("1a2b3c..4d5e6f", "999999..888888")
    assert ids(moved) == ids(MAIN_FIX)
    assert ids(file_diff("app/a.py", 10, "x = 1", "    x = 2")) != ids(MAIN_FIX)
    assert ids(file_diff("app/a.py", 10, "x = 1", "x = 2", "other")) != ids(MAIN_FIX)
    assert ids(MAIN_FIX.replace(" @@\n", " @@ def admin():\n")) != ids(MAIN_FIX.replace(" @@\n", " @@ def public():\n"))
    renamed = "diff --git a/old.py b/new.py\nsimilarity index 100%\nrename from old.py\nrename to new.py\n"
    assert sum(ids(renamed + MAIN_FIX).values()) == 2
    binary = "diff --git a/logo.png b/logo.png\nindex 1..2 100644\nBinary files a/logo.png and b/logo.png differ\n"
    with pytest.raises(BackportError, match="binary file"):
        ids(MAIN_FIX + binary)


def one_line_diff(new: bytes) -> bytes:
    return b"diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+" + new + b"\n"


def test_patch_ids_read_bytes_as_git_does() -> None:
    # Only LF ends a line: a line separator, CR or form feed inside content can't start a hunk or drop what follows it.
    assert ids(one_line_diff("x\u2028@@ -1 +1 @@ p=1".encode())) != ids(
        one_line_diff("x\u2028@@ -1 +1 @@ p=2".encode())
    )
    assert ids(one_line_diff(b"new\r+evil")) != ids(one_line_diff(b"new"))
    assert ids(one_line_diff(b"\xff")) != ids(one_line_diff(b"\xfe"))
    with pytest.raises(BackportError, match="outside any file or hunk"):
        ids(one_line_diff(b"new") + b"+extra\n")
    with pytest.raises(BackportError, match="do not match its header"):
        ids(one_line_diff(b"new")[:-5])
    with pytest.raises(BackportError, match="outside any file"):
        ids(b"garbage\n" + one_line_diff(b"new"))


def test_a_backport_with_the_named_main_prs_identical_diff_needs_no_approval() -> None:
    single = FakeGitHub("Backport of #52.", {59: MAIN_FIX, 52: MAIN_FIX}, {52: MERGED}, "release/1.13.x")
    assert check(single).reason == "backport of #52, identical diff: no approval needed"
    assert check(single).waived
    # Two main PRs touching the same file: the backport's hunks are the union of theirs, in any order.
    combined = FakeGitHub(
        "Backport of #46 and https://github.com/Acme/Web/pull/60; fixes #12.",
        {59: OTHER_FIX + MAIN_FIX, 46: MAIN_FIX, 60: OTHER_FIX},
        {46: MERGED, 60: MERGED, 12: ("Issue", False, "")},
        "release/1.13.x",
    )  # fmt: skip
    assert check(combined).reason == "backport of #46 + #60, identical combined diff: no approval needed"


@pytest.mark.parametrize(
    ("body", "diffs", "kinds", "reason"),
    [
        ("Backport of #52.", {59: MAIN_FIX + OTHER_FIX, 52: MAIN_FIX}, {52: MERGED}, "backport diff differs from #52"),
        ("Backport of #46 + #60.", {59: MAIN_FIX, 46: MAIN_FIX, 60: OTHER_FIX}, {46: MERGED, 60: MERGED},
         "backport diff differs from #46 + #60"),
        ("A clean cherry-pick.", {}, {}, "no merged main PR named"),
        ("Fixes #12.", {}, {12: ("Issue", False, "")}, "no merged main PR named"),
        ("Backport of #52.", {59: MAIN_FIX, 52: MAIN_FIX}, {52: ("PullRequest", False, "main")},
         "#52 is not merged into main"),
        ("Backport of #52.", {59: MAIN_FIX, 52: MAIN_FIX}, {52: ("PullRequest", True, "release/1.12.x")},
         "#52 is not merged into main"),
        ("Backport of #52.", {52: MAIN_FIX}, {52: MERGED}, "the backport check failed: gh failed: no pull requests"),
        ("Backport of #52.", {59: "", 52: ""}, {52: MERGED}, "the backport's diff is empty"),
    ],
)  # fmt: skip
def test_a_backport_keeps_the_approval_rule_unless_its_diff_is_proven_identical(
    body: str, diffs: dict[int, str], kinds: dict[int, tuple[str, bool, str]], reason: str
) -> None:
    verdict = check(FakeGitHub(body, diffs, kinds, "release/1.13.x"))
    assert not verdict.waived and verdict.reason.startswith(reason)


def test_gh_failing_or_time_running_out_keeps_the_approval_rule() -> None:
    with mock.patch("bridge.backport.run_gh", side_effect=GitHubError("gh failed: timeout")):
        verdict = check_backport(REPO, 59, BASES, time.monotonic() + 3.8)
    assert verdict is not None and (verdict.waived, verdict.reason) == (
        False,
        "the backport check failed: gh failed: timeout",
    )
    with (
        mock.patch("bridge.backport.run_gh", side_effect=RuntimeError("bug")),
        mock.patch("bridge.backport.log_error") as logged,
    ):
        broken = check_backport(REPO, 59, BASES, time.monotonic() + 3.8)
    assert broken is not None and not broken.waived and "internal error" in broken.reason
    logged.assert_called_once()
    with mock.patch("bridge.backport.run_gh") as gh:
        late = check_backport(REPO, 59, BASES, time.monotonic())
    gh.assert_not_called()
    assert late is not None and late.reason == "the backport check failed: it ran out of time"


def test_a_pr_into_a_branch_that_is_no_backport_base_is_not_checked() -> None:
    assert check(FakeGitHub("Backport of #52.", {}, {}, "main")) is None


def test_the_guard_lets_an_identical_backport_merge_unapproved_and_says_why(tmp_path: Path) -> None:
    from test_guard import repo

    web = repo(tmp_path / "web")
    conn = connect(tmp_path / "t.db")
    with transaction(conn):
        store = make_pr(ref="pr:Acme/Web#59", repo=REPO, number=59, head="feat/x", base="release/1.13.x")
        from bridge.reviews import store_open_prs

        store_open_prs(conn, [store], 500)
    targets = map_command_to_targets("gh pr merge 59", str(web))
    config = dataclasses.replace(CONFIG, backport_bases=BASES)
    gh = FakeGitHub("Backport of #52.", {59: MAIN_FIX, 52: MAIN_FIX}, {52: MERGED}, "release/1.13.x")
    waived: list[str] = []
    with (
        mock.patch("bridge.guard.fetch_merge_facts", return_value=(False, False, "SUCCESS", "CLEAN", 0, "a1", (), ())),
        stub(gh),
    ):
        [unreviewed] = find_merge_blocks(conn, config, targets, 10**13, waived)
        assert unreviewed.waits == ("not reviewed by the owner",), "an identical diff never stands in for their word"
    with transaction(conn):
        conn.execute("UPDATE review_prs SET reviewed_at = 600")
    with (
        mock.patch("bridge.guard.fetch_merge_facts", return_value=(False, False, "SUCCESS", "CLEAN", 0, "a1", (), ())),
        stub(gh),
    ):
        assert find_merge_blocks(conn, config, targets, 10**13, waived) == []
        assert waived == ["bridge: Acme/Web#59 may merge: backport of #52, identical diff: no approval needed."]
        # Off unless configured.
        [plain] = find_merge_blocks(conn, CONFIG, targets, 10**13)
        assert plain.waits == ("not approved",) and plain.backport is None
    # Every other condition still applies.
    with (
        mock.patch("bridge.guard.fetch_merge_facts", return_value=(False, False, "FAILURE", "CLEAN", 1, "a1", (), ())),
        stub(gh),
    ):
        [red] = find_merge_blocks(conn, config, targets, 10**13)
    assert red.waits == ("comments open", "CI red")
    [red_line] = map_merge_blocks_to_lines([red], "the owner", "The owner")
    assert "CI red. It needs no approval: backport of #52, identical diff: no approval needed. " in red_line
    gh.diffs[59] = MAIN_FIX + OTHER_FIX
    with (
        mock.patch("bridge.guard.fetch_merge_facts", return_value=(False, False, "SUCCESS", "CLEAN", 0, "a1", (), ())),
        stub(gh),
    ):
        [differs] = find_merge_blocks(conn, config, targets, 10**13)
    [line] = map_merge_blocks_to_lines([differs], "the owner", "The owner")
    assert "Acme/Web#59 is not mergeable yet: not approved (backport diff differs from #52; a backport" in line


def test_the_guard_keeps_all_its_github_reads_inside_the_hooks_budget(tmp_path: Path) -> None:
    from test_guard import repo

    web = repo(tmp_path / "web")
    conn = connect(tmp_path / "t.db")
    with transaction(conn):
        from bridge.reviews import store_open_prs

        store_open_prs(conn, [make_pr(ref="pr:Acme/Web#59", repo=REPO, number=59, head="feat/x")], 500)
        conn.execute("UPDATE review_prs SET reviewed_at = 600")
    targets = map_command_to_targets("gh pr merge 59", str(web))
    with mock.patch(
        "bridge.guard.fetch_merge_facts", return_value=(False, True, None, "CLEAN", 0, "a1", (), ())
    ) as fetch:
        assert find_merge_blocks(conn, CONFIG, targets, 10**13) == []
    assert 0 < fetch.call_args.args[3] <= 3
    with (
        mock.patch("bridge.guard.MERGE_CHECK_BUDGET_S", 0.1),
        mock.patch("bridge.guard.fetch_merge_facts") as late,
    ):
        [refused] = find_merge_blocks(conn, CONFIG, targets, 10**13)
    late.assert_not_called()
    assert refused.error == "out of time before reading GitHub"


def test_backport_bases_are_read_from_config() -> None:
    assert map_toml_to_config({}).backport_bases == ()
    assert map_toml_to_config({"backport_bases": ["release/*"]}).backport_bases == ("release/*",)
    with pytest.raises(ValueError, match="backport_bases"):
        map_toml_to_config({"backport_bases": "release/*"})
