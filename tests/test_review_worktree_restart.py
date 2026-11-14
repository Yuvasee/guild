import fcntl
import subprocess
from pathlib import Path
from unittest import mock

import pytest
from test_review_worktree import NOW, config_for, large
from test_reviews import REF, make_pr

from bridge.db import connect, transaction
from bridge.review_worktree import (
    PREPARING,
    READY,
    is_worktree_job_running,
    pick_worktree_job,
    run_next_worktree_job,
    run_worktree_job,
    spawn_worktree_job,
    worktree_job_lock,
)
from bridge.reviews import store_open_prs


def _stale_ready_pair(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.db")
    newer = make_pr(ref="pr:Org/app#8", number=8, url="https://github.com/Org/app/pull/8", head_oid="n2", **large())
    older = make_pr(head_oid="o2", **large(updated_at="2026-11-01T10:00:00Z"))
    with transaction(conn):
        store_open_prs(conn, [newer, older], NOW)
        for ref, at in (("pr:Org/app#8", 5000), (REF, 1000)):
            conn.execute(
                "UPDATE review_prs SET wt_path = ?, wt_oid = 'x1', wt_state = 'ready', wt_at = ? WHERE ref = ?",
                (str(tmp_path / ref.replace(":", "_")), at, ref),
            )
    conn.close()


def test_a_refresh_killed_midway_reads_preparing_never_ready_and_goes_to_the_back(tmp_path: Path) -> None:
    """As before a restart cut its install: the process died, so nothing after the start was written."""
    _stale_ready_pair(tmp_path)
    config = config_for(tmp_path)
    conn = connect(tmp_path / "t.db")
    job = pick_worktree_job(conn, config, NOW)
    assert job is not None and job.ref == REF

    with (
        mock.patch("bridge.review_worktree._refresh_worktree", side_effect=SystemExit("killed")),
        pytest.raises(SystemExit),
    ):
        run_worktree_job(conn, config, job, NOW)
    state = conn.execute("SELECT wt_state, wt_oid, wt_path FROM review_prs WHERE ref = ?", (REF,)).fetchone()
    following = pick_worktree_job(conn, config, NOW + 1)
    with transaction(conn):
        conn.execute("UPDATE review_prs SET wt_oid = head_oid, wt_state = ? WHERE ref = 'pr:Org/app#8'", (READY,))
    again = pick_worktree_job(conn, config, NOW + 2)

    assert state["wt_state"] == PREPARING and state["wt_oid"] == "x1" and state["wt_path"]
    assert following is not None and following.ref == "pr:Org/app#8", "the one cut off waits behind the rest"
    assert again is not None and (again.ref, again.action) == (REF, "refresh"), "and is taken up again, not left"


def test_one_runner_at_a_time_and_serve_sees_it_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    _stale_ready_pair(tmp_path)
    conn = connect(tmp_path / "t.db")
    worktree_job_lock().parent.mkdir(parents=True, exist_ok=True)
    with worktree_job_lock().open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        running = is_worktree_job_running()
        with mock.patch("bridge.review_worktree.run_worktree_job") as run:
            second = run_next_worktree_job(conn, config_for(tmp_path))
    with mock.patch("bridge.review_worktree.run_worktree_job") as run_alone:
        alone = run_next_worktree_job(conn, config_for(tmp_path))

    assert running and not is_worktree_job_running()
    assert second is False and not run.called, "a second runner leaves at once"
    assert alone is True and run_alone.call_args.args[2].ref == REF


def test_the_runner_starts_detached_with_its_output_in_a_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pipe to serve would end the install at its next line once serve is restarted (SIGPIPE)."""
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    with mock.patch("bridge.review_worktree.subprocess.Popen") as popen:
        spawn_worktree_job()
    kwargs = popen.call_args.kwargs

    assert popen.call_args.args[0][-1] == "_worktree-job"
    assert kwargs["start_new_session"] is True
    assert kwargs["stdout"] not in (subprocess.PIPE, None) and kwargs["stderr"] not in (subprocess.PIPE, None)
    assert Path(kwargs["stdout"].name) == tmp_path / "locks" / "worktree-job.log"
