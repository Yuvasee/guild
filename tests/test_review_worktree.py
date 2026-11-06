import dataclasses
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from conftest import BridgeEnv, http_request
from test_reviews import REF, add_session, make_pr, setup_pr

from bridge.board import BODY_MAX_CHARS
from bridge.config import Config
from bridge.db import transaction
from bridge.doc_nav import convert_doc_to_viewable
from bridge.review_post import ask_for_doc_updates, ask_for_docs
from bridge.review_worktree import (
    DETACHED,
    DIRTY,
    NO_CHECKOUT,
    READY,
    SSH_KEY_NOT_LOADED,
    DocError,
    StepFailed,
    fill_doc_heads,
    fill_doc_reviews,
    find_requirement_files,
    is_rebuilt_by_tools,
    is_worth_preparing,
    link_worktrees_to_prs,
    map_doc_to_served,
    map_requirements_to_consolidated,
    map_ssh_remote_to_https,
    pick_worktree_job,
    read_pinned_python,
    read_registered_doc,
    register_doc,
    run_worktree_job,
    write_editor_settings,
)
from bridge.reviews import map_db_to_reviews, store_open_prs

NOW = 1_793_000_000_000
UPDATED = "2026-10-26T00:00:00+00:00"
LONG_AGO = "2026-01-01T00:00:00+00:00"


def config_for(root: Path) -> Config:
    return Config(
        review_org="Org",
        review_author="me",
        review_checkouts_dir=str(root / "checkouts"),
        review_worktrees_dir=str(root / "wt"),
    )


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def move_pr(origin: Path, oid: str) -> None:
    git("update-ref", "refs/pull/7/head", oid, cwd=origin)
    git("update-ref", "refs/heads/dev/x", oid, cwd=origin)


def make_origin(root: Path) -> tuple[Path, str, str]:
    """A checkout whose origin has the PR's branch dev/x, with two commits to move between."""
    origin = root / "origin"
    origin.mkdir()
    git("init", "-q", "-b", "main", cwd=origin)
    git("config", "user.email", "t@e", cwd=origin)
    git("config", "user.name", "t", cwd=origin)
    (origin / "a.txt").write_text("1\n")
    git("add", ".", cwd=origin)
    git("commit", "-qm", "one", cwd=origin)
    first = git("rev-parse", "HEAD", cwd=origin)
    (origin / "a.txt").write_text("2\n")
    git("commit", "-qam", "two", cwd=origin)
    second = git("rev-parse", "HEAD", cwd=origin)
    move_pr(origin, second)
    (root / "checkouts").mkdir()
    git("clone", "-q", str(origin), str(root / "checkouts" / "app"), cwd=root)
    return origin, first, second


def large(**changes: object) -> dict[str, object]:
    return {"loc": 500, "updated_at": UPDATED, **changes}


def test_only_fresh_large_non_draft_unpublished_prs_are_worth_preparing(tmp_path: Path) -> None:
    config = config_for(tmp_path)

    def worth(name: str, **changes: object) -> bool:
        conn = setup_pr(tmp_path / name, **large(**changes)) if (tmp_path / name).mkdir() is None else None
        assert conn is not None
        return is_worth_preparing(conn.execute("SELECT * FROM review_prs").fetchone(), config, NOW)

    assert worth("ok")
    assert not worth("small", loc=99)
    assert worth("edge", loc=100)
    assert not worth("draft", draft=True)
    assert not worth("old", updated_at=LONG_AGO)
    assert worth("requested", requested=1)
    assert not worth("docs only", loc=0)


def test_published_by_the_owner_needs_no_worktree(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path, **large())
    with transaction(conn):
        conn.execute("UPDATE review_prs SET reviewed_at = 1")
    assert pick_worktree_job(conn, config_for(tmp_path), NOW) is None
    with transaction(conn):
        conn.execute("UPDATE review_prs SET reviewed_at = NULL, owner_reviewed_at = 1")
    assert pick_worktree_job(conn, config_for(tmp_path), NOW) is None


def test_a_worktree_is_made_on_its_own_branch_tracking_the_prs_and_opens_from_a_link(tmp_path: Path) -> None:
    _, _, second = make_origin(tmp_path)
    config = config_for(tmp_path)
    conn = setup_pr(tmp_path, head_oid=second, commits=((second, 1000),), **large())
    job = pick_worktree_job(conn, config, NOW)
    assert job is not None and job.action == "create" and job.path == tmp_path / "wt" / "review-app-7"
    run_worktree_job(conn, config, job, NOW)
    assert git("rev-parse", "HEAD", cwd=job.path) == second
    assert git("status", "-sb", cwd=job.path).startswith("## review/7...origin/dev/x")
    view = map_db_to_reviews(conn, config, NOW)["prs"][0]  # type: ignore[index]
    workspace = tmp_path / "wt" / "review-app-7.code-workspace"
    assert view["worktree_state"] == READY and view["worktree_url"] == f"vscode://file{workspace}"
    assert view["worktree_stale"] is False
    assert pick_worktree_job(conn, config, NOW) is None


def worktree_at(tmp_path: Path) -> tuple[sqlite3.Connection, Config, Path, str, str]:
    origin, first, second = make_origin(tmp_path)
    move_pr(origin, first)
    config = config_for(tmp_path)
    conn = setup_pr(tmp_path, head_oid=first, commits=((first, 1000),), **large())
    job = pick_worktree_job(conn, config, NOW)
    assert job is not None
    run_worktree_job(conn, config, job, NOW)
    move_pr(origin, second)
    with transaction(conn):
        store_open_prs(conn, [make_pr(head_oid=second, commits=((first, 1000), (second, 2000)), **large())], NOW)
    return conn, config, job.path, first, second


def test_new_commits_refresh_a_clean_worktree(tmp_path: Path) -> None:
    conn, config, path, _, second = worktree_at(tmp_path)
    (path / ".venv").mkdir()
    (path / ".venv" / "x").write_text("env\n")
    write_editor_settings(path, path / ".venv" / "bin" / "python", [])
    job = pick_worktree_job(conn, config, NOW)
    assert job is not None and job.action == "refresh"
    run_worktree_job(conn, config, job, NOW)
    assert git("rev-parse", "HEAD", cwd=path) == second
    assert pick_worktree_job(conn, config, NOW) is None


def force_push(origin: Path, first: str, text: str = "rewritten") -> str:
    git("checkout", "-q", "--detach", first, cwd=origin)
    (origin / "b.txt").write_text(f"{text}\n")
    git("add", ".", cwd=origin)
    git("commit", "-qm", "rewritten", cwd=origin)
    rewritten = git("rev-parse", "HEAD", cwd=origin)
    git("checkout", "-q", "main", cwd=origin)
    move_pr(origin, rewritten)
    return rewritten


def refresh_to(conn: sqlite3.Connection, config: Config, oid: str, at: int = NOW) -> None:
    with transaction(conn):
        store_open_prs(conn, [make_pr(head_oid=oid, commits=((oid, 3000),), **large())], at)
    job = pick_worktree_job(conn, config, at)
    assert job is not None and job.action == "refresh"
    run_worktree_job(conn, config, job, at)


def test_a_refresh_fast_forwards_its_branch_so_a_pull_there_works(tmp_path: Path) -> None:
    conn, config, path, _, second = worktree_at(tmp_path)
    refresh_to(conn, config, second)
    assert git("rev-parse", "HEAD", cwd=path) == second
    assert git("status", "-sb", cwd=path).splitlines()[0] == "## review/7...origin/dev/x"
    git("pull", "-q", "--ff-only", cwd=path)


def test_after_a_force_push_a_clean_worktree_is_reset_and_one_with_owner_commit_is_left(tmp_path: Path) -> None:
    conn, config, path, first, _ = worktree_at(tmp_path)
    rewritten = force_push(tmp_path / "origin", first)
    refresh_to(conn, config, rewritten)
    assert git("rev-parse", "HEAD", cwd=path) == rewritten
    assert conn.execute("SELECT wt_state FROM review_prs").fetchone()[0] == READY

    git("config", "user.email", "t@e", cwd=path)
    git("config", "user.name", "t", cwd=path)
    (path / "mine.txt").write_text("x\n")
    git("add", ".", cwd=path)
    git("commit", "-qm", "by hand", cwd=path)
    mine = git("rev-parse", "HEAD", cwd=path)
    again = force_push(tmp_path / "origin", first, "rewritten again")
    refresh_to(conn, config, again, NOW + 1)
    assert git("rev-parse", "HEAD", cwd=path) == mine
    row = conn.execute("SELECT wt_state, wt_detail FROM review_prs").fetchone()
    assert (row["wt_state"], row["wt_detail"]) == (DIRTY, "local commits, not refreshed")


def test_a_detached_worktree_from_before_is_put_on_its_branch(tmp_path: Path) -> None:
    conn, config, path, first, _ = worktree_at(tmp_path)
    git("checkout", "-q", "--detach", first, cwd=path)
    git("branch", "-q", "-D", "review/7", cwd=path)
    with transaction(conn):
        store_open_prs(conn, [make_pr(head_oid=first, commits=((first, 1000),), **large())], NOW)
        conn.execute("UPDATE review_prs SET wt_state = ?, wt_oid = ?", (DETACHED, first))
    move_pr(tmp_path / "origin", first)
    job = pick_worktree_job(conn, config, NOW)
    assert job is not None and job.action == "refresh"
    run_worktree_job(conn, config, job, NOW)
    assert git("status", "-sb", cwd=path).splitlines()[0] == "## review/7...origin/dev/x"
    assert conn.execute("SELECT wt_state FROM review_prs").fetchone()[0] == READY
    assert pick_worktree_job(conn, config, NOW) is None


def test_env_editor_and_ignored_files_do_not_hold_a_refresh_back_and_owner_new_file_does(tmp_path: Path) -> None:
    conn, config, path, _, second = worktree_at(tmp_path)
    vendored = path / ".venv" / "lib" / "setuptools"
    vendored.mkdir(parents=True)
    (vendored / "script (dev).tmpl").write_text("x\n")
    (path / ".vscode").mkdir()
    (path / ".vscode" / "launch.json").write_text("{}\n")
    exclude = Path(git("rev-parse", "--git-path", "info/exclude", cwd=path))
    exclude = exclude if exclude.is_absolute() else path / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("build/\n")
    (path / "build").mkdir()
    (path / "build" / "out file.bin").write_text("x\n")
    refresh_to(conn, config, second)
    assert git("rev-parse", "HEAD", cwd=path) == second

    (path / "notes.txt").write_text("mine\n")
    origin = tmp_path / "origin"
    (origin / "a.txt").write_text("3\n")
    git("checkout", "-q", "dev/x", cwd=origin)
    (origin / "a.txt").write_text("3\n")
    git("commit", "-qam", "three", cwd=origin)
    third = git("rev-parse", "HEAD", cwd=origin)
    git("checkout", "-q", "main", cwd=origin)
    move_pr(origin, third)
    refresh_to(conn, config, third, NOW + 1)
    assert git("rev-parse", "HEAD", cwd=path) == second
    assert "notes.txt" in conn.execute("SELECT wt_detail FROM review_prs").fetchone()[0]


def commit_on_pr(origin: Path, name: str, text: str) -> str:
    git("checkout", "-q", "dev/x", cwd=origin)
    (origin / name).write_text(text)
    git("add", ".", cwd=origin)
    git("commit", "-qm", name, cwd=origin)
    oid = git("rev-parse", "HEAD", cwd=origin)
    git("checkout", "-q", "main", cwd=origin)
    move_pr(origin, oid)
    return oid


def test_a_new_head_that_would_overwrite_owner_ignored_file_is_not_applied(tmp_path: Path) -> None:
    conn, config, path, _, second = worktree_at(tmp_path)
    refresh_to(conn, config, second)
    exclude = Path(git("rev-parse", "--git-path", "info/exclude", cwd=path))
    exclude = exclude if exclude.is_absolute() else path / exclude
    exclude.write_text(".env\n")
    (path / ".env").write_text("local secret\n")
    third = commit_on_pr(tmp_path / "origin", ".env", "from the PR\n")
    refresh_to(conn, config, third, NOW + 1)
    assert (path / ".env").read_text() == "local secret\n" and git("rev-parse", "HEAD", cwd=path) == second
    assert ".env" in conn.execute("SELECT wt_detail FROM review_prs").fetchone()[0]


def test_its_branch_holding_owner_commit_is_never_reset_while_the_worktree_is_detached(tmp_path: Path) -> None:
    conn, config, path, first, second = worktree_at(tmp_path)
    git("config", "user.email", "t@e", cwd=path)
    git("config", "user.name", "t", cwd=path)
    (path / "mine.txt").write_text("x\n")
    git("add", ".", cwd=path)
    git("commit", "-qm", "by hand", cwd=path)
    mine = git("rev-parse", "HEAD", cwd=path)
    git("checkout", "-q", "--detach", first, cwd=path)
    refresh_to(conn, config, second)
    assert git("rev-parse", "review/7", cwd=path) == mine
    assert "local commits on review/7" in conn.execute("SELECT wt_detail FROM review_prs").fetchone()[0]


def test_switching_to_owner_branch_during_the_fetch_stops_the_refresh(tmp_path: Path) -> None:
    conn, config, path, first, second = worktree_at(tmp_path)
    from bridge import review_worktree

    real = review_worktree._fetch_head_branch  # pyright: ignore[reportPrivateUsage]

    def fetch_then_switch(checkout: Path, job: Any) -> None:
        real(checkout, job)
        git("checkout", "-q", "-b", "mine", cwd=path)

    with mock.patch.object(review_worktree, "_fetch_head_branch", side_effect=fetch_then_switch):
        refresh_to(conn, config, second)
    assert git("rev-parse", "mine", cwd=path) == first
    assert "on another branch, mine," in conn.execute("SELECT wt_detail FROM review_prs").fetchone()[0]


def test_a_same_named_branch_that_is_not_the_prs_head_is_refused(tmp_path: Path) -> None:
    conn, config, path, first, second = worktree_at(tmp_path)
    git("update-ref", "refs/heads/dev/x", first, cwd=tmp_path / "origin")
    refresh_to(conn, config, second)
    assert git("rev-parse", "HEAD", cwd=path) == first
    row = conn.execute("SELECT wt_state, wt_detail FROM review_prs").fetchone()
    assert row["wt_state"] == "failed" and "fork" in row["wt_detail"]


def test_a_dirty_worktree_is_left_and_says_so(tmp_path: Path) -> None:
    conn, config, path, first, _ = worktree_at(tmp_path)
    (path / "a.txt").write_text("local edit\n")
    job = pick_worktree_job(conn, config, NOW)
    assert job is not None
    run_worktree_job(conn, config, job, NOW)
    assert git("rev-parse", "HEAD", cwd=path) == first and (path / "a.txt").read_text() == "local edit\n"
    views: Any = map_db_to_reviews(conn, config, NOW)["prs"]
    view = views[0]
    assert view["worktree_state"] == DIRTY and "a.txt" in str(view["worktree_detail"])
    assert view["worktree_stale"] is True
    assert pick_worktree_job(conn, config, NOW) is None
    assert pick_worktree_job(conn, config, NOW + 3_700_000) is not None


def close_pr(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        conn.execute("UPDATE review_prs SET closed_at = ?", (NOW,))


def test_a_closed_prs_clean_worktree_is_removed(tmp_path: Path) -> None:
    conn, config, path, _, _ = worktree_at(tmp_path)
    close_pr(conn)
    job = pick_worktree_job(conn, config, NOW)
    assert job is not None and job.action == "remove"
    run_worktree_job(conn, config, job, NOW)
    assert not path.exists()
    assert conn.execute("SELECT wt_path FROM review_prs").fetchone()[0] is None
    assert git("branch", "--list", "review/7", cwd=tmp_path / "checkouts" / "app") == ""
    assert pick_worktree_job(conn, config, NOW + 4_000_000) is None


def test_a_closed_pr_whose_folder_is_gone_loses_its_branch_too(tmp_path: Path) -> None:
    conn, config, path, _, _ = worktree_at(tmp_path)
    checkout = tmp_path / "checkouts" / "app"
    git("worktree", "remove", "--force", str(path), cwd=checkout)
    close_pr(conn)
    job = pick_worktree_job(conn, config, NOW)
    assert job is not None and job.action == "remove"
    run_worktree_job(conn, config, job, NOW)
    assert git("branch", "--list", "review/7", cwd=checkout) == ""


def test_a_folder_deleted_by_hand_still_loses_its_branch(tmp_path: Path) -> None:
    import shutil

    conn, config, path, _, _ = worktree_at(tmp_path)
    shutil.rmtree(path)
    close_pr(conn)
    job = pick_worktree_job(conn, config, NOW)
    assert job is not None and job.action == "remove"
    run_worktree_job(conn, config, job, NOW)
    assert git("branch", "--list", "review/7", cwd=tmp_path / "checkouts" / "app") == ""


def test_a_closed_prs_worktree_with_spaced_names_under_its_env_is_removed(tmp_path: Path) -> None:
    conn, config, path, _, _ = worktree_at(tmp_path)
    vendored = path / ".venv" / "lib" / "python3.12" / "site-packages" / "setuptools"
    (vendored / "_vendor" / "jaraco" / "text").mkdir(parents=True)
    for name in ("_vendor/jaraco/text/Lorem ipsum.txt", "launcher manifest.xml", "script (dev).tmpl"):
        (vendored / name).write_text("x\n")
    exclude = Path(git("rev-parse", "--git-path", "info/exclude", cwd=path))
    exclude = exclude if exclude.is_absolute() else path / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(".venv/\n")
    close_pr(conn)
    job = pick_worktree_job(conn, config, NOW)
    assert job is not None and job.action == "remove"
    run_worktree_job(conn, config, job, NOW)
    assert not path.exists()


def test_a_closed_prs_dirty_worktree_stays(tmp_path: Path) -> None:
    conn, config, path, _, _ = worktree_at(tmp_path)
    (path / "note.txt").write_text("keep\n")
    close_pr(conn)
    job = pick_worktree_job(conn, config, NOW)
    assert job is not None
    run_worktree_job(conn, config, job, NOW)
    assert (path / "note.txt").exists()
    assert conn.execute("SELECT wt_state FROM review_prs").fetchone()[0] == DIRTY


def test_a_local_commit_is_never_moved_away_from_or_removed(tmp_path: Path) -> None:
    conn, config, path, _, second = worktree_at(tmp_path)
    git("config", "user.email", "t@e", cwd=path)
    git("config", "user.name", "t", cwd=path)
    (path / "mine.txt").write_text("x\n")
    git("add", ".", cwd=path)
    git("commit", "-qm", "by hand", cwd=path)
    mine = git("rev-parse", "HEAD", cwd=path)
    job = pick_worktree_job(conn, config, NOW)
    assert job is not None and job.action == "refresh"
    run_worktree_job(conn, config, job, NOW)
    assert git("rev-parse", "HEAD", cwd=path) == mine != second
    close_pr(conn)
    job = pick_worktree_job(conn, config, NOW + 3_700_000)
    assert job is not None and job.action == "remove"
    run_worktree_job(conn, config, job, NOW + 3_700_000)
    assert path.exists()
    assert "local commits" in conn.execute("SELECT wt_detail FROM review_prs").fetchone()[0]


def test_git_config_cannot_hide_an_untracked_file_from_the_guard(tmp_path: Path) -> None:
    conn, config, path, _, _ = worktree_at(tmp_path)
    git("config", "status.showUntrackedFiles", "no", cwd=path)
    (path / "scratch.py").write_text("x\n")
    close_pr(conn)
    job = pick_worktree_job(conn, config, NOW)
    assert job is not None
    run_worktree_job(conn, config, job, NOW)
    assert (path / "scratch.py").exists()


def test_owner_editor_files_count_as_local_work_and_ours_do_not(tmp_path: Path) -> None:
    _, _, path, _, _ = worktree_at(tmp_path)
    (path / ".venv").mkdir()
    python = path / ".venv" / "bin" / "python"
    write_editor_settings(path, python, [])
    from bridge import review_worktree

    dirty = review_worktree._list_dirty_files  # pyright: ignore[reportPrivateUsage]
    assert dirty(path) == []
    settings = path / ".vscode" / "settings.json"
    settings.write_text('{"mine": true}\n')
    write_editor_settings(path, python, [])
    assert settings.read_text() == '{"mine": true}\n'
    assert dirty(path) == [".vscode/settings.json"]
    settings.unlink()
    write_editor_settings(path, python, [])
    (path / ".vscode" / "launch.json").write_text("{}\n")
    assert ".vscode/launch.json" in dirty(path)


def test_a_failed_removal_does_not_hold_up_the_rest(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    conn = setup_pr(tmp_path, **large())
    with transaction(conn):
        conn.execute("UPDATE review_prs SET closed_at = 1, wt_path = '/x', wt_state = 'failed', wt_at = ?", (NOW,))
    assert pick_worktree_job(conn, config, NOW) is None
    assert pick_worktree_job(conn, config, NOW + 3_700_000) is not None


def test_a_failed_install_keeps_the_worktree_known_and_is_tried_again(tmp_path: Path) -> None:
    _, _, second = make_origin(tmp_path)
    config = config_for(tmp_path)
    conn = setup_pr(tmp_path, head_oid=second, commits=((second, 1000),), **large())
    job = pick_worktree_job(conn, config, NOW)
    assert job is not None
    with mock.patch("bridge.review_worktree.prepare_python_env", side_effect=StepFailed("uv broke")):
        run_worktree_job(conn, config, job, NOW)
    row = conn.execute("SELECT wt_path, wt_oid, wt_state FROM review_prs").fetchone()
    assert (row["wt_path"], row["wt_oid"], row["wt_state"]) == (str(job.path), second, "failed")
    assert pick_worktree_job(conn, config, NOW) is None
    retry = pick_worktree_job(conn, config, NOW + 3_700_000)
    assert retry is not None and retry.action == "refresh"
    run_worktree_job(conn, config, retry, NOW + 3_700_000)
    assert conn.execute("SELECT wt_state FROM review_prs").fetchone()[0] == READY


def test_a_cleaned_up_worktree_recovers_with_the_head_unchanged(tmp_path: Path) -> None:
    conn, config, path, _, second = worktree_at(tmp_path)
    (path / "a.txt").write_text("edit\n")
    job = pick_worktree_job(conn, config, NOW)
    assert job is not None
    run_worktree_job(conn, config, job, NOW)
    (path / "a.txt").unlink()
    git("checkout", "-q", "a.txt", cwd=path)
    job = pick_worktree_job(conn, config, NOW + 3_700_000)
    assert job is not None
    run_worktree_job(conn, config, job, NOW + 3_700_000)
    row = conn.execute("SELECT wt_state, wt_oid FROM review_prs").fetchone()
    assert (row["wt_state"], row["wt_oid"]) == (READY, second)


def test_the_shared_checkouts_fetch_head_is_left_alone(tmp_path: Path) -> None:
    _, _, second = make_origin(tmp_path)
    checkout = tmp_path / "checkouts" / "app"
    git("fetch", "-q", "origin", "main", cwd=checkout)
    before = (checkout / ".git" / "FETCH_HEAD").read_text()
    config = config_for(tmp_path)
    conn = setup_pr(tmp_path, head_oid=second, commits=((second, 1000),), **large())
    job = pick_worktree_job(conn, config, NOW)
    assert job is not None
    run_worktree_job(conn, config, job, NOW)
    assert (checkout / ".git" / "FETCH_HEAD").read_text() == before


def test_a_worktree_link_survives_reserved_characters(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    conn = setup_pr(tmp_path, **large())
    with transaction(conn):
        conn.execute("UPDATE review_prs SET wt_path = '/Users/me/Review#2/a b', wt_oid = 'a1', wt_state = 'ready'")
    view: Any = map_db_to_reviews(conn, config, NOW)["prs"]
    assert view[0]["worktree_url"] == "vscode://file/Users/me/Review%232/a%20b"


def test_with_no_recorded_commit_a_local_one_is_still_seen(tmp_path: Path) -> None:
    conn, config, path, _, _ = worktree_at(tmp_path)
    git("config", "user.email", "t@e", cwd=path)
    git("config", "user.name", "t", cwd=path)
    (path / "mine.txt").write_text("x\n")
    git("add", ".", cwd=path)
    git("commit", "-qm", "by hand", cwd=path)
    mine = git("rev-parse", "HEAD", cwd=path)
    with transaction(conn):
        conn.execute("UPDATE review_prs SET wt_path = NULL, wt_oid = NULL, wt_state = NULL")
    job = pick_worktree_job(conn, config, NOW)
    assert job is not None and job.action == "create"
    run_worktree_job(conn, config, job, NOW)
    assert git("rev-parse", "HEAD", cwd=path) == mine
    assert conn.execute("SELECT wt_state FROM review_prs").fetchone()[0] == DIRTY


def test_a_head_already_at_the_pr_is_not_a_local_commit(tmp_path: Path) -> None:
    conn, config, path, _, second = worktree_at(tmp_path)
    git("checkout", "-q", "--detach", second, cwd=path)
    job = pick_worktree_job(conn, config, NOW)
    assert job is not None
    run_worktree_job(conn, config, job, NOW)
    assert conn.execute("SELECT wt_state FROM review_prs").fetchone()[0] == READY


def test_an_ignored_file_is_kept_and_caches_do_not_count(tmp_path: Path) -> None:
    conn, config, path, _, _ = worktree_at(tmp_path)
    exclude = Path(git("rev-parse", "--git-path", "info/exclude", cwd=path))
    exclude = exclude if exclude.is_absolute() else path / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("launch.json\n__pycache__/\n")
    (path / "__pycache__").mkdir()
    (path / "__pycache__" / "x.pyc").write_text("x")
    close_pr(conn)
    (path / "launch.json").write_text("{}\n")
    job = pick_worktree_job(conn, config, NOW)
    assert job is not None
    run_worktree_job(conn, config, job, NOW)
    assert (path / "launch.json").exists()
    (path / "launch.json").unlink()
    job = pick_worktree_job(conn, config, NOW + 3_700_000)
    assert job is not None
    run_worktree_job(conn, config, job, NOW + 3_700_000)
    assert not path.exists()


def test_a_tracked_file_under_a_cache_named_folder_still_counts(tmp_path: Path) -> None:
    origin, _, _ = make_origin(tmp_path)
    (origin / "fixtures" / "node_modules").mkdir(parents=True)
    (origin / "fixtures" / "node_modules" / "index.js").write_text("1\n")
    git("add", "-f", ".", cwd=origin)
    git("commit", "-qm", "fixture", cwd=origin)
    head = git("rev-parse", "HEAD", cwd=origin)
    move_pr(origin, head)
    config = config_for(tmp_path)
    conn = setup_pr(tmp_path, head_oid=head, commits=((head, 1000),), **large())
    job = pick_worktree_job(conn, config, NOW)
    assert job is not None
    run_worktree_job(conn, config, job, NOW)
    (job.path / "fixtures" / "node_modules" / "index.js").write_text("edited\n")
    close_pr(conn)
    removal = pick_worktree_job(conn, config, NOW)
    assert removal is not None
    run_worktree_job(conn, config, removal, NOW)
    assert (job.path / "fixtures" / "node_modules" / "index.js").read_text() == "edited\n"


def test_a_filesystem_error_gets_a_cooldown(tmp_path: Path) -> None:
    (tmp_path / "checkouts" / "app" / ".git").mkdir(parents=True)
    config = config_for(tmp_path)
    conn = setup_pr(tmp_path, **large())
    job = pick_worktree_job(conn, config, NOW)
    assert job is not None
    with mock.patch("bridge.review_worktree._create_worktree", side_effect=PermissionError("no")):
        run_worktree_job(conn, config, job, NOW)
    assert conn.execute("SELECT wt_state FROM review_prs").fetchone()[0] == "failed"
    assert pick_worktree_job(conn, config, NOW) is None


def test_no_checkout_is_said_and_not_tried_each_round(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    conn = setup_pr(tmp_path, **large())
    job = pick_worktree_job(conn, config, NOW)
    assert job is not None
    run_worktree_job(conn, config, job, NOW)
    assert conn.execute("SELECT wt_state FROM review_prs").fetchone()[0] == NO_CHECKOUT
    assert pick_worktree_job(conn, config, NOW) is None


def test_a_checkout_cloned_after_no_checkout_is_used_at_the_next_pass(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    conn = setup_pr(tmp_path, **large())
    job = pick_worktree_job(conn, config, NOW)
    assert job is not None
    run_worktree_job(conn, config, job, NOW)
    assert pick_worktree_job(conn, config, NOW + 60_000) is None
    _, _, second = make_origin(tmp_path)
    with transaction(conn):
        store_open_prs(conn, [make_pr(head_oid=second, commits=((second, 1000),), **large())], NOW)
    job = pick_worktree_job(conn, config, NOW + 60_000)
    assert job is not None and job.action == "create"
    run_worktree_job(conn, config, job, NOW + 60_000)
    assert conn.execute("SELECT wt_state FROM review_prs").fetchone()[0] == READY


def test_a_failed_step_keeps_the_row_going(tmp_path: Path) -> None:
    (tmp_path / "checkouts" / "app" / ".git").mkdir(parents=True)
    config = config_for(tmp_path)
    conn = setup_pr(tmp_path, **large())
    job = pick_worktree_job(conn, config, NOW)
    assert job is not None
    run_worktree_job(conn, config, job, NOW)
    row = conn.execute("SELECT wt_state, wt_detail FROM review_prs").fetchone()
    assert row["wt_state"] == "failed" and row["wt_detail"]


def test_requirements_come_from_each_service_with_the_first_pin_winning(tmp_path: Path) -> None:
    (tmp_path / "svc-a").mkdir()
    (tmp_path / "svc-b").mkdir()
    (tmp_path / "svc-a" / "requirements.txt").write_text("# c\nFlask==2.0  # why\nfoo_bar>=1\n-r other.txt\n./local\n")
    (tmp_path / "svc-b" / "requirements.txt").write_text("flask==3.0\nzed\n")
    (tmp_path / "svc-b" / "deep" / "er").mkdir(parents=True)
    (tmp_path / "svc-b" / "deep" / "er" / "requirements.txt").write_text("nested\n")
    (tmp_path / "web" / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "web" / "node_modules" / "x" / "requirements.txt").write_text("nope\n")
    files = find_requirement_files(tmp_path)
    assert map_requirements_to_consolidated(files) == ["Flask==2.0", "foo_bar>=1", "nested", "zed"]


def test_python_env_asks_for_at_least_3_11_and_selects_the_interpreter(tmp_path: Path) -> None:
    from bridge import review_worktree

    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "requirements.txt").write_text("zed\n")
    calls: list[list[str]] = []

    def fake(command: list[str], timeout_s: int) -> str:
        calls.append(command)
        if command[:2] == ["uv", "venv"]:
            (tmp_path / ".venv" / "bin").mkdir(parents=True)
            (tmp_path / ".venv" / "bin" / "python").write_text("")
        return ""

    with mock.patch.object(review_worktree, "_run", side_effect=fake):
        review_worktree.prepare_python_env(tmp_path)
        review_worktree.prepare_python_env(tmp_path)
    assert calls[0][:5] == ["uv", "venv", "-q", "--python", ">=3.11"]
    assert sum(c[:3] == ["uv", "pip", "install"] for c in calls) == 1
    settings = json.loads((tmp_path / ".vscode" / "settings.json").read_text())
    assert settings["python.defaultInterpreterPath"] == str(tmp_path / ".venv" / "bin" / "python")
    assert str(tmp_path / "svc") in settings["python.analysis.extraPaths"]
    assert str(tmp_path / "svc" / "deep") not in settings["python.analysis.extraPaths"]


def test_pins_that_conflict_across_services_fall_back_to_names(tmp_path: Path) -> None:
    from bridge import review_worktree

    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "requirements.txt").write_text(
        "pywin32==306; sys_platform == 'win32'\nuvicorn[standard]==1.0\nzed>=2\n"
    )
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").write_text("")
    installed: list[str] = []

    def fake(command: list[str], timeout_s: int) -> str:
        installed.append((tmp_path / ".venv" / "review-requirements.txt").read_text())
        if len(installed) == 1:
            raise review_worktree.StepFailed("requirements are unsatisfiable")
        return ""

    with mock.patch.object(review_worktree, "_run", side_effect=fake):
        review_worktree.prepare_python_env(tmp_path)
    assert installed[1] == "pywin32; sys_platform == 'win32'\nuvicorn[standard]\nzed\n"


def test_a_repo_without_python_gets_no_env(tmp_path: Path) -> None:
    from bridge import review_worktree

    with mock.patch.object(review_worktree, "_run") as run:
        review_worktree.prepare_python_env(tmp_path)
    run.assert_not_called()
    assert not (tmp_path / ".vscode").exists()


def test_the_owner_is_asked_for_a_doc_once_and_only_when_it_counts(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    conn = setup_pr(tmp_path, **large())
    with transaction(conn):
        assert ask_for_docs(conn, config, NOW) == []
        add_session(conn, "c1", "worker", branch="dev/x")
        assert len(ask_for_docs(conn, config, NOW)) == 1
        assert ask_for_docs(conn, config, NOW) == []
    body = conn.execute("SELECT body FROM messages").fetchone()[0]
    assert "guild review doc Org/app#7" in body and "does not sell" in body and "guild review sentence" in body
    assert 'name="pr-head"' in body and "~/.claude/skills/bridge/review-doc.md lays it out" in body
    assert (
        "every Cost and Risk clear without guessing" in body
        and "marked observed, reproduced or from-code" in body
        and "no glossary or legend" in body
    )
    assert len(body) <= BODY_MAX_CHARS


def test_a_small_pr_gets_no_doc_request(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    conn = setup_pr(tmp_path, loc=50, updated_at=UPDATED)
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
        assert ask_for_docs(conn, config, NOW) == []


def test_a_doc_is_copied_in_and_goes_stale_with_new_commits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path / "state"))
    config = config_for(tmp_path)
    conn = setup_pr(tmp_path, **large())
    source = tmp_path / "doc.html"
    source.write_text("<h1>v1</h1>")
    with transaction(conn):
        register_doc(conn, REF, source, NOW)
    source.write_text("<h1>changed after</h1>")
    assert read_registered_doc(conn, "Org/app", 7) == b"<h1>v1</h1>"
    view = map_db_to_reviews(conn, config, NOW)["prs"][0]  # type: ignore[index]
    assert (view["doc_url"], view["doc_stale"]) == ("/reviews/Org/app/7/doc", False)
    with transaction(conn):
        store_open_prs(conn, [make_pr(head_oid="b2", **large())], NOW)
    stale: Any = map_db_to_reviews(conn, config, NOW)["prs"]
    assert stale[0]["doc_stale"] is True


def test_a_doc_whose_head_marker_matches_the_pr_is_current_however_it_was_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path / "state"))
    config = config_for(tmp_path)
    head = "c0ffee123" + "0" * 31
    conn = setup_pr(tmp_path, **large())
    source = tmp_path / "doc.html"
    source.write_text(f'<html><head><meta content="{head}" name="pr-head"></head><body><h1>v2</h1></body></html>')
    with transaction(conn):
        register_doc(conn, REF, source, NOW)
    views: Any = map_db_to_reviews(conn, config, NOW)["prs"]
    assert (views[0]["doc_stale"], views[0]["doc_at"], views[0]["pr_at"]) == (True, "c0ffee123", "a1")
    served = map_doc_to_served(conn, "Org/app", 7)
    assert served is not None and b"<body><div" in served and b"doc at c0ffee123, PR at a1</div>" in served
    with transaction(conn):
        store_open_prs(conn, [make_pr(head_oid=head, **large())], NOW)
    views = map_db_to_reviews(conn, config, NOW)["prs"]
    assert views[0]["doc_stale"] is False
    assert map_doc_to_served(conn, "Org/app", 7) == convert_doc_to_viewable(source.read_bytes())


def test_a_served_doc_gets_its_contents_bar_under_the_stale_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path / "state"))
    conn = setup_pr(tmp_path, **large())
    source = tmp_path / "doc.html"
    original = b'<html><head><meta name="pr-head" content="c0ffee123"></head><body><h2>In short</h2><h2>Reviews</h2>'
    source.write_bytes(original)
    with transaction(conn):
        register_doc(conn, REF, source, NOW)
    served = map_doc_to_served(conn, "Org/app", 7)
    assert served is not None and source.read_bytes() == original
    assert served.index(b"PR at a1</div>") < served.index(b'<nav id="bridge-doc-nav"') < served.index(b"<h2 id=")
    assert b'<a href="#in-short">In short</a>' in served and b'<h2 id="reviews">Reviews</h2>' in served


def test_a_doc_registered_before_markers_were_read_gets_its_marker_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path / "state"))
    config = config_for(tmp_path)
    conn = setup_pr(tmp_path, head_oid="c3" * 20, **large())
    source = tmp_path / "doc.html"
    source.write_text(f'<meta name="pr-head" content="{"c3" * 20}"><h1>doc</h1>')
    with transaction(conn):
        register_doc(conn, REF, source, NOW)
        conn.execute("UPDATE review_prs SET doc_head = NULL, doc_oid = 'old'")
        fill_doc_heads(conn)
    views: Any = map_db_to_reviews(conn, config, NOW)["prs"]
    assert views[0]["doc_stale"] is False


def test_each_new_head_asks_the_owner_once_to_update_the_doc_and_the_row_says_updating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path / "state"))
    config = config_for(tmp_path)
    conn = setup_pr(tmp_path, **large())
    source = tmp_path / "doc.html"
    source.write_text('<meta name="pr-head" content="a1a1a1a1a1">')
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
        conn.execute("UPDATE review_prs SET head_oid = 'a1a1a1a1a1'")
        register_doc(conn, REF, source, NOW)
        assert ask_for_doc_updates(conn, config, NOW) == []
        store_open_prs(conn, [make_pr(head_oid="b2b2b2b2b2", **large())], NOW)
        assert len(ask_for_doc_updates(conn, config, NOW)) == 1
        assert ask_for_doc_updates(conn, config, NOW + 3_600_000) == []
    body = conn.execute("SELECT body FROM messages ORDER BY id DESC").fetchone()[0]
    assert "moved to b2b2b2b2b" in body and "doc is at a1a1a1a1a" in body and 'name="pr-head"' in body
    assert "as ~/.claude/skills/bridge/review-doc.md lays out" in body and "no Changes since the last review" in body
    assert "guild review doc Org/app#7" in body and len(body) <= BODY_MAX_CHARS
    views: Any = map_db_to_reviews(conn, config, NOW)["prs"]
    assert views[0]["doc_updating"] is True
    with transaction(conn):
        store_open_prs(conn, [make_pr(head_oid="c3c3c3c3c3", **large())], NOW)
        assert ask_for_doc_updates(conn, config, NOW + 60_000) == []
        assert len(ask_for_doc_updates(conn, config, NOW + 16 * 60_000)) == 1


def test_only_html_files_register(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path / "state"))
    conn = setup_pr(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    with pytest.raises(DocError), transaction(conn):
        register_doc(conn, REF, secret, NOW)
    with pytest.raises(DocError), transaction(conn):
        register_doc(conn, REF, tmp_path / "missing.html", NOW)


def test_the_page_serves_registered_docs_only_and_sandboxed(
    bridge_env: BridgeEnv, dashboard: str, tmp_path: Path
) -> None:
    conn = bridge_env.connect()
    with transaction(conn):
        store_open_prs(conn, [make_pr()], 500)
    http_request(f"{dashboard}/reviews/Org/app/7/doc", expect=404)
    http_request(f"{dashboard}/reviews/Org/app/8/doc", expect=404)
    http_request(f"{dashboard}/reviews/..%2F..%2Fetc/passwd/7/doc", expect=404)
    source = tmp_path / "d.html"
    source.write_text("<h1>doc</h1>")
    done = bridge_env.cli("sessions")
    assert done.returncode == 0
    env = bridge_env.env
    with mock.patch.dict("os.environ", {"BRIDGE_STATE_DIR": env["BRIDGE_STATE_DIR"]}), transaction(conn):
        register_doc(conn, REF, source, NOW)
    import urllib.request

    with urllib.request.urlopen(f"{dashboard}/reviews/Org/app/7/doc", timeout=5) as response:
        served = response.read()
        assert served.endswith(b"<h1>doc</h1>") and b'<style id="bridge-doc-view">' in served
        assert response.headers["Content-Security-Policy"] == "sandbox allow-scripts"
        assert response.headers["Cache-Control"] == "no-store"


def test_a_worktree_gets_a_workspace_beside_it_that_opens_on_its_pr(tmp_path: Path) -> None:
    _, _, second = make_origin(tmp_path)
    config = config_for(tmp_path)
    conn = setup_pr(tmp_path, head_oid=second, commits=((second, 1000),), **large())
    job = pick_worktree_job(conn, config, NOW)
    assert job is not None

    def make_env(root: Path, setup_script: str) -> None:
        (root / ".venv" / "bin").mkdir(parents=True)
        (root / ".venv" / "bin" / "python").write_text("")
        (root / ".venv" / ".gitignore").write_text("*\n")

    with mock.patch("bridge.review_worktree.prepare_python_env", side_effect=make_env):
        run_worktree_job(conn, config, job, NOW)
    workspace = json.loads((tmp_path / "wt" / "review-app-7.code-workspace").read_text())
    assert workspace == {
        "folders": [{"path": str(job.path)}],
        "settings": {
            "workbench.secondarySideBar.defaultVisibility": "hidden",
            "githubPullRequests.focusedMode": "overview",
            "python.defaultInterpreterPath": str(job.path / ".venv" / "bin" / "python"),
            "python.useEnvironmentsExtension": False,
        },
    }
    checkout = tmp_path / "checkouts" / "app"
    assert git("config", "--local", "branch.review/7.github-pr-owner-number", cwd=checkout) == "Org#app#7"
    assert git("status", "--porcelain", "--untracked-files=all", cwd=job.path) == ""

    close_pr(conn)
    removal = pick_worktree_job(conn, config, NOW)
    assert removal is not None and removal.action == "remove"
    run_worktree_job(conn, config, removal, NOW)
    assert not (tmp_path / "wt" / "review-app-7.code-workspace").exists()
    assert "github-pr-owner-number" not in git("config", "--local", "--list", cwd=checkout)


def test_a_worktree_from_before_workspaces_gets_one_without_a_refresh(tmp_path: Path) -> None:
    _, _, second = make_origin(tmp_path)
    config = config_for(tmp_path)
    conn = setup_pr(tmp_path, head_oid=second, commits=((second, 1000),), **large())
    job = pick_worktree_job(conn, config, NOW)
    assert job is not None
    run_worktree_job(conn, config, job, NOW)
    workspace = tmp_path / "wt" / "review-app-7.code-workspace"
    workspace.unlink()
    git("config", "--local", "--unset", "branch.review/7.github-pr-owner-number", cwd=job.path)
    view: Any = map_db_to_reviews(conn, config, NOW)["prs"]
    assert view[0]["worktree_url"] == f"vscode://file{job.path}"
    assert pick_worktree_job(conn, config, NOW) is None
    link_worktrees_to_prs(conn)
    assert json.loads(workspace.read_text())["folders"] == [{"path": str(job.path)}]
    assert git("config", "--local", "branch.review/7.github-pr-owner-number", cwd=job.path) == "Org#app#7"
    view = map_db_to_reviews(conn, config, NOW)["prs"]
    assert view[0]["worktree_url"] == f"vscode://file{workspace}"


FAKE_GIT = r"""#!/bin/bash
echo "$*" >> "$FAKE_GIT_LOG"
if [[ " $* " == *" fetch "* ]]; then
  args=()
  for a in "$@"; do
    case "$a" in
      origin)
        echo "git@github.com: Permission denied (publickey)." >&2
        echo "fatal: Could not read from remote repository." >&2
        echo "and the repository exists." >&2
        exit 128;;
      https://github.com/Org/app.git)
        if [ -n "$FAKE_HTTPS_FAILS" ]; then echo "fatal: Authentication failed" >&2; exit 128; fi
        [ "$GIT_TERMINAL_PROMPT" = 0 ] || exit 99
        args+=("$FAKE_ORIGIN"); continue;;
    esac
    args+=("$a")
  done
  exec "$REAL_GIT" "${args[@]}"
fi
exec "$REAL_GIT" "$@"
"""


def with_ssh_refusing_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[sqlite3.Connection, Config, Path]:
    """GitHub's SSH says no key, as with an empty agent; its HTTPS URL is served from the local origin."""
    origin, _, second = make_origin(tmp_path)
    checkout = tmp_path / "checkouts" / "app"
    git("remote", "set-url", "origin", "git@github.com:Org/app.git", cwd=checkout)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").write_text(FAKE_GIT)
    (bin_dir / "git").chmod(0o755)
    log = tmp_path / "git.log"
    monkeypatch.setenv("REAL_GIT", git("--exec-path", cwd=tmp_path) + "/git")
    monkeypatch.setenv("FAKE_ORIGIN", str(origin))
    monkeypatch.setenv("FAKE_GIT_LOG", str(log))
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    conn = setup_pr(tmp_path, head_oid=second, commits=((second, 1000),), **large())
    return conn, config_for(tmp_path), log


def test_a_fetch_refused_for_want_of_an_ssh_key_is_retried_over_https_with_ghs_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, config, log = with_ssh_refusing_git(tmp_path, monkeypatch)
    job = pick_worktree_job(conn, config, NOW)
    assert job is not None
    run_worktree_job(conn, config, job, NOW)
    assert conn.execute("SELECT wt_state, wt_detail FROM review_prs").fetchone()[:] == (READY, None)
    retry = next(line for line in log.read_text().splitlines() if "https://" in line)
    assert "-c credential.helper= -c credential.helper=!gh auth git-credential fetch" in retry
    assert "+refs/pull/7/head:refs/remotes/origin/pr/7" in retry
    checkout = tmp_path / "checkouts" / "app"
    assert git("remote", "get-url", "origin", cwd=checkout) == "git@github.com:Org/app.git"
    assert "credential" not in git("config", "--local", "--list", cwd=checkout)


def test_when_https_fails_too_the_detail_says_to_load_the_ssh_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, config, _ = with_ssh_refusing_git(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_HTTPS_FAILS", "1")
    job = pick_worktree_job(conn, config, NOW)
    assert job is not None
    run_worktree_job(conn, config, job, NOW)
    assert conn.execute("SELECT wt_state, wt_detail FROM review_prs").fetchone()[:] == ("failed", SSH_KEY_NOT_LOADED)


def test_a_fetch_failing_for_another_reason_is_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn, config, log = with_ssh_refusing_git(tmp_path, monkeypatch)
    git("remote", "set-url", "origin", str(tmp_path / "nowhere"), cwd=tmp_path / "checkouts" / "app")
    monkeypatch.setenv("FAKE_ORIGIN", str(tmp_path / "nowhere"))
    (tmp_path / "bin" / "git").write_text(FAKE_GIT.replace("      origin)", "      never)"))
    job = pick_worktree_job(conn, config, NOW)
    assert job is not None
    run_worktree_job(conn, config, job, NOW)
    state, detail = conn.execute("SELECT wt_state, wt_detail FROM review_prs").fetchone()
    assert state == "failed" and "git fetch failed" in detail
    assert "https://" not in log.read_text()


def test_ssh_remotes_map_to_https_and_others_do_not() -> None:
    assert map_ssh_remote_to_https("git@github.com:Org/app.git") == "https://github.com/Org/app.git"
    assert map_ssh_remote_to_https("git@github.com:Org/app") == "https://github.com/Org/app.git"
    assert map_ssh_remote_to_https("ssh://git@github.com/Org/app.git") == "https://github.com/Org/app.git"
    assert map_ssh_remote_to_https("ssh://git@github.com:22/Org/app.git") == "https://github.com/Org/app.git"
    assert map_ssh_remote_to_https("https://github.com/Org/app.git") is None
    assert map_ssh_remote_to_https("/tmp/origin") is None


def test_a_large_pr_is_ready_for_the_owner_only_once_its_doc_names_codex_and_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bridge.attention import map_page_to_ranked_page

    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path / "state"))
    config = config_for(tmp_path)
    conn = setup_pr(tmp_path, **large())
    source = tmp_path / "doc.html"

    def view() -> tuple[dict[str, Any], dict[str, Any]]:
        reviews = map_db_to_reviews(conn, config, NOW)
        page: Any = map_page_to_ranked_page({"needs": []}, reviews, [])
        return page["reviews"]["prs"][0], {r["key"]: r for r in page["attention"]["ranking"]}

    pr, ranking = view()
    assert pr["reviews_gap"] == "no doc yet" and REF not in ranking
    for html, gap, ran in (
        ("<h1>Doc</h1><p>codex, Claude</p>", "doc lacks Reviews section", []),
        ("<h2>Reviews</h2><p>ReviewBot: none</p>", "Codex / Claude review missing", ["ReviewBot"]),
        ("<h2>Reviews</h2><p>Codex: 2 fixed in abc</p>", "Claude review missing", ["Codex"]),
    ):
        source.write_text(html)
        with transaction(conn):
            register_doc(conn, REF, source, NOW)
        pr, ranking = view()
        assert (pr["reviews_gap"], pr["reviews_ran"]) == (gap, ran) and REF not in ranking
    source.write_text("<h2>Reviews</h2><p>Codex: fixed in abc. Claude: deferred, PROJ-1</p>")
    with transaction(conn):
        register_doc(conn, REF, source, NOW)
    pr, ranking = view()
    assert pr["reviews_gap"] is None and pr["reviews_ran"] == ["Codex", "Claude"] and ranking[REF]["mark"] == 1


def test_a_small_pr_shows_its_bodys_reviews_and_is_never_held_back(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    conn = setup_pr(tmp_path, loc=40, body_reviews=("Codex",))
    pr: Any = map_db_to_reviews(conn, config, NOW)["prs"][0]  # type: ignore[index]
    assert pr["reviews_gap"] is None and pr["reviews_ran"] == ["Codex"]


def test_a_doc_registered_before_reviews_were_read_gets_them_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path / "state"))
    conn = setup_pr(tmp_path, **large())
    source = tmp_path / "doc.html"
    source.write_text("<h2>Reviews</h2><p>Codex, Claude</p>")
    with transaction(conn):
        register_doc(conn, REF, source, NOW)
        conn.execute("UPDATE review_prs SET doc_reviews = NULL")
        fill_doc_reviews(conn)
    assert conn.execute("SELECT doc_reviews FROM review_prs").fetchone()[0] == '["Codex", "Claude"]'


def test_a_long_title_keeps_the_doc_request_under_the_message_limit(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    conn = setup_pr(tmp_path, title="T" * 400, **large())
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x")
        assert len(ask_for_docs(conn, config, NOW)) == 1
    body = conn.execute("SELECT body FROM messages").fetchone()[0]
    assert "a Codex and a Claude review" in body and "section headed Reviews" in body and len(body) <= BODY_MAX_CHARS


def test_a_long_repo_and_title_still_fit_the_doc_request(tmp_path: Path) -> None:
    repo = "acme-co/customer-onboarding-service-infrastructure-with-a-very-long-name-indeed"
    config = Config(review_org="Org", review_author="me", owner_name="Theo")
    conn = setup_pr(tmp_path, ref=f"pr:{repo}#1234", repo=repo, number=1234, url=f"https://github.com/{repo}/pull/1234",
                    title="T" * 300, **large())  # fmt: skip
    with transaction(conn):
        add_session(conn, "c1", "worker", branch="dev/x", repo=repo)
        assert len(ask_for_docs(conn, config, NOW)) == 1
    body = conn.execute("SELECT body FROM messages").fetchone()[0]
    assert len(body) <= BODY_MAX_CHARS and "TTT" in body and "…" in body and body.endswith('"…"')


def make_setup_script(root: Path) -> Path:
    """Writes .venv and the editor settings, as a repo's own setup does, and logs each run."""
    script = root / "tools" / "setup.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "#!/bin/sh\nset -e\necho run >> runs.log\nmkdir -p .venv/bin .vscode\n: > .venv/bin/python\n"
        "[ -f .vscode/settings.json ] || echo '{\"theirs\": 1}' > .vscode/settings.json\n"
    )
    script.chmod(0o755)
    return script


def test_a_repos_own_setup_replaces_the_generic_env_and_its_settings(tmp_path: Path) -> None:
    from bridge import review_worktree

    make_setup_script(tmp_path)
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "requirements.txt").write_text("zed\n")
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").write_text("")
    (tmp_path / ".venv" / "review-requirements.sha256").write_text("x")
    (tmp_path / ".venv" / "generic-only").write_text("")
    write_editor_settings(tmp_path, tmp_path / ".venv" / "bin" / "python", [tmp_path / "svc" / "requirements.txt"])

    review_worktree.prepare_python_env(tmp_path, "tools/setup.sh")
    assert not (tmp_path / ".venv" / "generic-only").exists()
    assert json.loads((tmp_path / ".vscode" / "settings.json").read_text()) == {"theirs": 1}
    assert review_worktree._is_our_settings(tmp_path)  # pyright: ignore[reportPrivateUsage]

    review_worktree.prepare_python_env(tmp_path, "tools/setup.sh")
    assert (tmp_path / "runs.log").read_text() == "run\n"
    (tmp_path / "svc" / "requirements.txt").write_text("zed==2\n")
    review_worktree.prepare_python_env(tmp_path, "tools/setup.sh")
    assert (tmp_path / "runs.log").read_text() == "run\nrun\n"
    (tmp_path / "svc" / "requirements-dev.txt").write_text("pytest\n")
    review_worktree.prepare_python_env(tmp_path, "tools/setup.sh")
    assert (tmp_path / "runs.log").read_text() == "run\nrun\nrun\n"


def test_settings_of_owner_survive_the_repos_setup(tmp_path: Path) -> None:
    from bridge import review_worktree

    script = make_setup_script(tmp_path)
    script.write_text(script.read_text().replace("[ -f .vscode/settings.json ] || ", ""))
    (tmp_path / ".vscode").mkdir()
    (tmp_path / ".vscode" / "settings.json").write_text('{"mine": 1}\n')
    review_worktree.prepare_python_env(tmp_path, "tools/setup.sh")
    assert (tmp_path / ".vscode" / "settings.json").read_text() == '{"mine": 1}\n'
    assert not review_worktree._is_our_settings(tmp_path)  # pyright: ignore[reportPrivateUsage]


def test_a_branch_without_the_setup_script_gets_the_generic_env(tmp_path: Path) -> None:
    from bridge import review_worktree

    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "requirements.txt").write_text("zed\n")
    calls: list[list[str]] = []

    def fake(command: list[str], timeout_s: int) -> str:
        calls.append(command)
        (tmp_path / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
        return ""

    with mock.patch.object(review_worktree, "_run", side_effect=fake):
        review_worktree.prepare_python_env(tmp_path, "tools/setup.sh")
    assert calls[0][:2] == ["uv", "venv"]


def test_the_generic_env_uses_the_python_the_repo_pins(tmp_path: Path) -> None:
    from bridge import review_worktree

    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "requirements.txt").write_text("zed\n")
    (tmp_path / "pyrightconfig.json").write_text('{"pythonVersion": "3.11"}')
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("")
    (venv / "pyvenv.cfg").write_text("home = x\nversion_info = 3.12.10\n")
    (venv / "review-requirements.sha256").write_text("x")
    write_editor_settings(tmp_path, venv / "bin" / "python", [tmp_path / "svc" / "requirements.txt"])
    calls: list[list[str]] = []

    def fake(command: list[str], timeout_s: int) -> str:
        calls.append(command)
        if command[:2] == ["uv", "venv"]:
            (venv / "bin").mkdir(parents=True)
            (venv / "bin" / "python").write_text("")
            (venv / "pyvenv.cfg").write_text("version_info = 3.11.12\n")
        return ""

    with mock.patch.object(review_worktree, "_run", side_effect=fake):
        review_worktree.prepare_python_env(tmp_path)
        review_worktree.prepare_python_env(tmp_path)
    assert [c[:5] for c in calls if c[:2] == ["uv", "venv"]] == [["uv", "venv", "-q", "--python", "3.11"]]
    assert review_worktree._is_our_settings(tmp_path)  # pyright: ignore[reportPrivateUsage]


def test_the_pinned_python_comes_from_python_version_then_pyright(tmp_path: Path) -> None:
    assert read_pinned_python(tmp_path) == ""
    (tmp_path / "pyrightconfig.json").write_text('{"pythonVersion": "3.11"}')
    assert read_pinned_python(tmp_path) == "3.11"
    (tmp_path / ".python-version").write_text("3.12.4\n")
    assert read_pinned_python(tmp_path) == "3.12.4"
    (tmp_path / ".python-version").write_text("pypy\n")
    (tmp_path / "pyrightconfig.json").write_text("// jsonc\n{}")
    assert read_pinned_python(tmp_path) == ""


def test_a_worktree_with_the_generic_env_is_refreshed_once_its_repo_has_a_setup_script(tmp_path: Path) -> None:
    _, _, second = make_origin(tmp_path)
    config = config_for(tmp_path)
    conn = setup_pr(tmp_path, head_oid=second, commits=((second, 1000),), **large())
    job = pick_worktree_job(conn, config, NOW)
    assert job is not None and job.setup_script == ""
    run_worktree_job(conn, config, job, NOW)
    assert pick_worktree_job(conn, config, NOW) is None

    config = dataclasses.replace(config, review_setup_scripts=(("org/APP", "tools/setup.sh"),))
    assert pick_worktree_job(conn, config, NOW) is None
    make_setup_script(job.path)
    refresh = pick_worktree_job(conn, config, NOW)
    assert refresh is not None and refresh.action == "refresh" and refresh.setup_script == "tools/setup.sh"
    (job.path / ".venv").mkdir()
    (job.path / ".venv" / "review-setup.sha256").write_text("x")
    assert pick_worktree_job(conn, config, NOW) is None


def test_a_generic_env_on_another_python_than_the_repo_pins_is_refreshed(tmp_path: Path) -> None:
    _, _, second = make_origin(tmp_path)
    config = config_for(tmp_path)
    conn = setup_pr(tmp_path, head_oid=second, commits=((second, 1000),), **large())
    job = pick_worktree_job(conn, config, NOW)
    assert job is not None
    run_worktree_job(conn, config, job, NOW)
    (job.path / ".venv").mkdir()
    (job.path / ".venv" / "review-requirements.sha256").write_text("x")
    (job.path / ".venv" / "pyvenv.cfg").write_text("version_info = 3.12.1\n")
    (job.path / ".python-version").write_text("3.12\n")
    assert pick_worktree_job(conn, config, NOW) is None
    (job.path / ".python-version").write_text("3.11\n")
    refresh = pick_worktree_job(conn, config, NOW)
    assert refresh is not None and refresh.action == "refresh"


def test_an_outdated_workspace_is_rewritten_without_a_refresh(tmp_path: Path) -> None:
    _, _, second = make_origin(tmp_path)
    config = config_for(tmp_path)
    conn = setup_pr(tmp_path, head_oid=second, commits=((second, 1000),), **large())
    job = pick_worktree_job(conn, config, NOW)
    assert job is not None
    run_worktree_job(conn, config, job, NOW)
    (job.path / ".venv" / "bin").mkdir(parents=True)
    (job.path / ".venv" / "bin" / "python").write_text("")
    link_worktrees_to_prs(conn)
    workspace = tmp_path / "wt" / "review-app-7.code-workspace"
    settings = json.loads(workspace.read_text())["settings"]
    assert settings["python.useEnvironmentsExtension"] is False

    theirs = {
        "folders": [{"path": str(job.path)}, {"path": "/x"}],
        "settings": {"python.useEnvironmentsExtension": True},
    }
    workspace.write_text(json.dumps({**theirs, "launch": {"configurations": []}}))
    link_worktrees_to_prs(conn)
    kept = json.loads(workspace.read_text())
    assert kept["folders"] == theirs["folders"] and kept["launch"] == {"configurations": []}
    assert kept["settings"]["python.useEnvironmentsExtension"] is True
    assert kept["settings"]["githubPullRequests.focusedMode"] == "overview"
    workspace.write_text("// mine\n{}")
    link_worktrees_to_prs(conn)
    assert workspace.read_text() == "// mine\n{}"


def test_a_timed_out_step_stops_what_it_started(tmp_path: Path) -> None:
    from bridge import review_worktree

    pid_file = tmp_path / "pid"
    with pytest.raises(StepFailed, match="TimeoutExpired"):
        review_worktree._run(  # pyright: ignore[reportPrivateUsage]
            ["sh", "-c", f"sleep 30 & echo $! > {pid_file}; wait"], 1
        )
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        for _ in range(50):
            os.kill(pid, 0)
            subprocess.run(["sleep", "0.1"], check=True)


def test_egg_info_from_editable_installs_is_rebuilt_by_tools() -> None:
    assert is_rebuilt_by_tools("svc/acmeco.egg-info/PKG-INFO")
    assert not is_rebuilt_by_tools("svc/eggs/info.py")


def test_setup_scripts_must_name_a_repo_and_a_path_inside_it() -> None:
    from bridge.config import map_toml_to_config

    config = map_toml_to_config({"review_setup_scripts": {"Org/app": "infra/setup.sh"}})
    assert config.review_setup_scripts == (("Org/app", "infra/setup.sh"),)
    for bad in ({"app": "x.sh"}, {"Org/app": "/abs.sh"}, {"Org/app": "../x.sh"}, {"Org/app": ""}):
        with pytest.raises(ValueError, match="review_setup_scripts"):
            map_toml_to_config({"review_setup_scripts": bad})
