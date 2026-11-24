import json
import subprocess
from pathlib import Path
from typing import Any, cast
from unittest import mock

import pytest
from conftest import BridgeEnv, SpawnProcess
from test_leads import Team
from test_reviews import CONFIG, REF, add_session, comment, make_pr, ready_view, setup_pr, store

from bridge.attention import Project, map_page_to_ranked_page
from bridge.config import Config
from bridge.db import connect, transaction
from bridge.gitinfo import RepoFacts
from bridge.guard import (
    HELD,
    HostCheckout,
    HostRepoReader,
    Target,
    add_merge_base_targets,
    find_merge_blocks,
    make_host_repo_reader,
    map_command_to_targets,
    map_merge_blocks_to_lines,
)
from bridge.hooks import handle_pre_tool_use
from bridge.review_post import ReviewError, record_hold, record_publish, record_published_elsewhere, record_unhold
from bridge.reviews import READY, WAITING_FOR_YOU, map_db_to_reviews, store_merge_blocks, store_open_prs

OWNER = Config(review_org="Org", review_author="me", review_bot_logins=("bot",), owner_name="Theo")


def hold(conn: Any, ref: str = REF, why: str = "wants to read the migration first", now: int = 1500) -> None:
    with transaction(conn):
        record_hold(conn, CONFIG, ref, why, "eli", now)


def test_a_held_approved_green_pr_waits_for_the_owner_and_its_lead_hears_once_the_hold_ends(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path, approved_by=("mira",))
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
    assert ready_view(conn)[0]["approval_left"] == "your review"
    hold(conn)

    view, sent = ready_view(conn)
    assert view["merge_path"] is None and view["group"] == WAITING_FOR_YOU and sent == []
    assert view["approval_left"] == "held for your review" and view["can_publish"]
    assert view["held"] == "held for the owner" and view["held_why"] == "wants to read the migration first"
    store(conn, comment("p1", at=1400, body="An old remark"), comment("p2", at=1450, body="🤖 Claude: Fixed."))
    assert ready_view(conn)[0]["held"] is not None

    store(conn, comment("p3", at=2000, body="Looks right, merge it"), comment("p4", at=2200, body="🤖 Claude: Ok."))
    view, sent = ready_view(conn)
    assert view["held"] is None and view["merge_path"] is None and view["approval_left"] == "your review"
    with transaction(conn):
        record_publish(conn, REF, "theo", 2200)
    view, sent = ready_view(conn)
    assert view["merge_path"] == READY and len(sent) == 1


@pytest.mark.parametrize(("publish", "group"), [("page", READY), ("post", "published")])
def test_publishing_ends_a_hold_and_a_published_pr_cannot_be_held(tmp_path: Path, publish: str, group: str) -> None:
    conn = setup_pr(tmp_path, approved_by=("mira",))
    hold(conn)
    with transaction(conn):
        if publish == "page":
            record_publish(conn, REF, "theo", 1600)
        else:
            record_published_elsewhere(conn, REF, "https://x.slack.com/archives/C1/p1", 1600)

    view, _ = ready_view(conn)
    assert view["held"] is None and view["group"] == group, "only the owner's Publish, not a post, makes it ready"
    with pytest.raises(ReviewError, match="already published"):
        hold(conn)


def test_a_published_pr_never_shows_as_waiting_even_with_a_stale_hold(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    hold(conn)
    with transaction(conn):
        conn.execute("UPDATE review_prs SET post_state = 'external'")
    prs = cast(list[dict[str, Any]], map_db_to_reviews(conn, CONFIG, 2000)["prs"])
    assert prs[0]["group"] == "published"


def test_a_hold_needs_a_reason_an_open_pr_and_unhold_needs_a_hold(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path, bot_approved=True)
    with pytest.raises(ReviewError, match="say why"):
        hold(conn, why="  ")
    with pytest.raises(ReviewError, match="not an open PR"):
        hold(conn, ref="pr:Org/app#8")
    (tmp_path / "d").mkdir()
    with pytest.raises(ReviewError, match="gh pr ready first"):
        hold(setup_pr(tmp_path / "d", draft=True))
    with pytest.raises(ReviewError, match="not held"), transaction(conn):
        record_unhold(conn, REF)
    hold(conn)
    hold(conn, why="second reason", now=9000)
    row = conn.execute("SELECT held_at, held_why FROM review_prs").fetchone()
    assert (row["held_at"], row["held_why"]) == (1500, "second reason")
    with transaction(conn):
        record_unhold(conn, REF)
    assert ready_view(conn)[0]["approval_left"] == "your review"


def test_the_merge_guard_refuses_a_held_pr_without_asking_github(tmp_path: Path) -> None:
    web = tmp_path / "web"
    conn = connect(tmp_path / "t.db")
    with transaction(conn):
        store_open_prs(conn, [make_pr(ref="pr:Acme/Web#12", repo="Acme/Web", number=12, approved_by=("mira",))], 500)
        conn.execute("INSERT OR REPLACE INTO review_poll (id, ok_at) VALUES (1, 1000)")
    hold(conn, ref="pr:Acme/Web#12")
    targets = map_command_to_targets("gh pr merge https://github.com/Acme/Web/pull/12", str(web))

    with mock.patch("bridge.review_github.fetch_merge_facts") as fetch:
        [refused] = find_merge_blocks(conn, CONFIG, targets, 1000)
    fetch.assert_not_called()
    assert refused.waits == (HELD,)
    [line] = map_merge_blocks_to_lines([refused], "Theo", "Theo")
    assert "Acme/Web#12 is not mergeable yet: held for Theo's review (wants to read the migration first)" in line


def test_a_held_parent_rejoins_open_by_priority_and_its_dependent_follows_it(tmp_path: Path) -> None:
    code = make_pr(ref="pr:Org/mono#50", repo="Org/shop", number=50, title="PROJ-40 SSO", loc=50,
                   approved_by=("mira",))  # fmt: skip
    docs = make_pr(ref="pr:Org/docs#22", repo="Org/docs", number=22, loc=0, depends_on="Org/mono#50")
    other = make_pr(ref="pr:Org/mono#51", repo="Org/shop", number=51, loc=20)
    conn = connect(tmp_path / "t.db")
    with transaction(conn):
        store_open_prs(conn, [code, docs, other], 500)
        store_merge_blocks(conn)
    priority = [Project("sso", ("PROJ-40",))]

    def page() -> tuple[list[tuple[str, str]], list[str]]:
        shown: Any = map_page_to_ranked_page({"needs": []}, map_db_to_reviews(conn, OWNER, 2000), priority)
        return [(pr["ref"], pr["group"]) for pr in shown["reviews"]["prs"]], [
            item["key"] for item in shown["attention"]["ranking"]
        ]

    _, ranked = page()
    assert code.ref in ranked, "approved but not reviewed by the owner: it waits for them"
    hold(conn, ref=code.ref)
    rows, ranked = page()
    assert rows == [(code.ref, "waiting"), (docs.ref, "waiting"), (other.ref, "waiting")]
    assert ranked == [code.ref, docs.ref, other.ref]


def test_only_the_prs_lead_or_the_assistant_holds_it(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    team = Team(bridge_env, spawn_process)
    team.enroll()
    ref = "pr:acme/web#9"
    with bridge_env.connect() as conn, transaction(conn):
        store_open_prs(conn, [make_pr(ref=ref, repo="acme/web", number=9, url="https://github.com/acme/web/pull/9")], 1)
    assert team.guild("work", "set", "Export worker", "acme/web#9", pid=team.a).returncode == 0

    stranger = team.guild("review", "hold", "acme/web#9", "the owner asked", pid=team.b)
    held = team.guild("review", "hold", "acme/web#9", "the owner asked", pid=team.a)
    listed = json.loads(team.guild("review", "list").stdout)["prs"][0]
    assert bridge_env.cli("handle", "aide", pid=team.b).returncode == 0
    unheld = team.guild("review", "unhold", "9", pid=team.b)

    assert stranger.returncode == 2 and "only the PR's lead or aide holds it; it is Alex's" in stranger.stderr
    assert held.returncode == 0, held.stderr
    assert listed["held"] == "held for the owner" and listed["held_by"] == "alex"
    assert unheld.returncode == 0, unheld.stderr


def _git_repo(path: Path, branch: str = "dev/x") -> Path:
    path.mkdir(parents=True)
    for args in (
        ["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
        ["remote", "add", "origin", "https://github.com/Org/app.git"], ["commit", "-q", "--allow-empty", "-m", "s"],
        ["switch", "-q", "-c", branch],
    ):  # fmt: skip
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    return path


def _verdict(conn: Any, command: str, cwd: Path | str, reader: HostRepoReader | None = None) -> str | None:
    payload: dict[str, object] = {
        "session_id": "c1", "cwd": str(cwd), "tool_name": "Bash", "tool_input": {"command": command},
    }  # fmt: skip
    out = handle_pre_tool_use(conn, payload, OWNER, reader)
    return json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"] if out else None


@pytest.mark.parametrize(
    "command",
    [
        "git push",
        "git push origin dev/x",
        "git push --force-with-lease origin HEAD",
        "git push origin fix-local:dev/x",
        "git push origin +refs/heads/other:refs/heads/dev/x",
        "git push --all",
        "git push origin --delete dev/x",
        "gh pr update-branch 7",
        "gh pr update-branch",
        "gh api -X PUT repos/Org/app/pulls/7/update-branch",
        "gh api repos/Org/app/merges -f base=dev/x -f head=main",
        "gh api -X PATCH repos/Org/app/git/refs/heads/dev/x -f sha=abc -F force=true",
        "gh api -X PATCH repos/Org/app/git/refs/heads/dev/x --input body.json",
    ],
)
def test_a_write_to_a_held_prs_branch_is_refused_and_says_why_and_what_to_do(tmp_path: Path, command: str) -> None:
    conn = setup_pr(tmp_path, approved_by=("mira",))
    hold(conn, now=1_000)
    repo = _git_repo(tmp_path / "app")

    with mock.patch("bridge.hooks.time.time_ns", return_value=(1_000 + 3 * 3_600_000) * 1_000_000):
        reason = _verdict(conn, command, repo)

    assert reason is not None
    assert reason.startswith(
        "bridge: dev/x is the branch of Org/app#7, held for Theo's review by eli 3 h ago: wants to read the migration "
        "first. Nothing is pushed to it while Theo reads it"
    )
    assert "Keep the fix in a local commit or on a local branch" in reason
    assert "`guild review unhold Org/app#7`" in reason


@pytest.mark.parametrize(
    "command",
    [
        "git push origin dev/y",
        "git push origin dev/x:dev/x-next",
        "git push --dry-run origin dev/x",
        "gh pr update-branch 8",
        "gh api repos/Org/app/merges -f base=dev/y -f head=main",
        "gh api repos/Org/app/git/refs/heads/dev/x",
    ],
)
def test_a_write_to_another_branch_or_a_read_of_the_held_one_passes(tmp_path: Path, command: str) -> None:
    conn = setup_pr(tmp_path, approved_by=("mira",))
    hold(conn)

    assert _verdict(conn, command, _git_repo(tmp_path / "app")) is None


def _setup_two_prs(tmp_path: Path, **second: Any) -> Any:
    conn = connect(tmp_path / "t.db")
    other = make_pr(ref="pr:Org/app#8", number=8, url="https://github.com/Org/app/pull/8", **second)
    with transaction(conn):
        store_open_prs(conn, [make_pr(approved_by=("mira",)), other], 500)
    hold(conn)
    return conn


@pytest.mark.parametrize("command", ["gh pr merge 8", "gh pr merge dev/y", "gh api -X PUT repos/Org/app/pulls/8/merge"])
def test_merging_a_queued_pr_into_the_held_branch_is_refused(tmp_path: Path, command: str) -> None:
    conn = _setup_two_prs(tmp_path, head="dev/y", base="dev/x", approved_by=("mira",))

    reason = _verdict(conn, command, _git_repo(tmp_path / "app"))

    assert reason is not None and reason.startswith("bridge: dev/x is the branch of Org/app#7")


def test_merging_a_pr_known_only_to_the_enrichment_cache_into_the_held_branch_is_refused(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path, approved_by=("mira",))
    hold(conn)
    with transaction(conn):
        conn.execute(
            "INSERT INTO enrichment (ref, data, fetched_at) VALUES ('pr:Org/app#8', ?, 1)",
            (json.dumps({"base": "dev/x", "head": "dev/y", "state": "OPEN"}),),
        )

    repo = _git_repo(tmp_path / "app")

    reason = _verdict(conn, "gh pr merge 8", repo)
    targets = add_merge_base_targets(conn, map_command_to_targets("gh pr merge 8", str(repo)))

    assert reason is not None and reason.startswith("bridge: dev/x is the branch of Org/app#7")
    assert Target("branch:org/app:dev/x", "gh pr merge (base branch)") in targets


@pytest.mark.parametrize("command", ["gh pr update-branch 8", "gh api -X PUT repos/Org/app/pulls/8/update-branch"])
def test_updating_another_pr_that_shares_the_held_head_is_refused(tmp_path: Path, command: str) -> None:
    conn = _setup_two_prs(tmp_path, head="dev/x", base="release")

    reason = _verdict(conn, command, _git_repo(tmp_path / "app"))

    assert reason is not None and reason.startswith("bridge: dev/x is the branch of Org/app#7")


def test_closing_the_held_pr_with_its_branch_is_refused_and_closing_it_alone_is_not_this_rules_call(
    tmp_path: Path,
) -> None:
    conn = setup_pr(tmp_path, approved_by=("mira",))
    hold(conn)
    repo = _git_repo(tmp_path / "app")

    deleted = _verdict(conn, "gh pr close 7 --delete-branch", repo)
    short = _verdict(conn, "gh pr close dev/x -d", repo)

    assert deleted is not None and "(gh pr close --delete-branch would change it)" in deleted
    assert short is not None
    assert _verdict(conn, "gh pr close 7", repo) is None


def test_a_ref_creation_before_a_merge_no_longer_hides_the_merge_from_the_guard(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path, approved_by=("mira",))
    hold(conn)

    reason = _verdict(
        conn, "gh api -X POST repos/Org/app/git/refs -f ref=refs/heads/new -f sha=a1; gh pr merge 7 -R Org/app",
        _git_repo(tmp_path / "app"),
    )  # fmt: skip

    assert reason is not None and "Org/app#7 is not mergeable yet: held for Theo's review" in reason


def test_a_held_branch_is_open_to_pushes_again_once_unheld_or_once_the_owner_comments(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path, approved_by=("mira",))
    repo = _git_repo(tmp_path / "app")
    hold(conn)
    with transaction(conn):
        record_unhold(conn, REF)
    assert _verdict(conn, "git push origin dev/x", repo) is None

    hold(conn, now=1_500)
    store(conn, comment("p1", at=1_400, body="An old remark"))
    ready_view(conn)
    assert _verdict(conn, "git push origin dev/x", repo) is not None

    store(conn, comment("p2", at=2_000, body="Rename the column first"))
    ready_view(conn)
    assert _verdict(conn, "git push origin dev/x", repo) is None


@pytest.mark.parametrize(
    "command",
    [
        "git push origin dev/x",
        "git push --force-with-lease",
        "gh api -X PUT repos/Org/app/pulls/7/update-branch",
        "gh api graphql -f query='mutation { updatePullRequestBranch(input: {pullRequestId: \"PR_1\"}) { x } }'",
    ],
)
def test_a_host_workers_write_to_a_held_branch_is_refused_the_same_way(tmp_path: Path, command: str) -> None:
    conn = setup_pr(tmp_path, approved_by=("mira",))
    hold(conn)
    facts = RepoFacts("/home/dev/app", "dev/x", {"origin": "Org/app"})
    reader = make_host_repo_reader([HostCheckout("/home/dev/app", facts, facts.repo_root)], ("/home/dev",))

    reason = _verdict(conn, command, "/home/dev/app", reader)
    other = _verdict(conn, "git push origin dev/y", "/home/dev/app", reader)

    assert reason is not None and other is None
    if "graphql" not in command and "/update-branch" not in command:
        assert reason.startswith("bridge: dev/x is the branch of Org/app#7, held for Theo's review by eli")


def test_a_host_worker_updates_a_pr_that_is_not_held(tmp_path: Path) -> None:
    conn = _setup_two_prs(tmp_path, head="dev/y")
    facts = RepoFacts("/home/dev/app", "dev/y", {"origin": "Org/app"})
    reader = make_host_repo_reader([HostCheckout("/home/dev/app", facts, facts.repo_root)], ("/home/dev",))

    assert _verdict(conn, "gh pr update-branch 8", "/home/dev/app", reader) is None
    held = _verdict(conn, "gh pr update-branch 7", "/home/dev/app", reader)
    assert held is not None and held.startswith("bridge: dev/x is the branch of Org/app#7")
