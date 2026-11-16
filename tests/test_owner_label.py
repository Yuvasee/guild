import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from conftest import VENV_BIN, BridgeEnv, free_port, http_request, wait_up
from test_reviews import CONFIG, REF, make_pr, setup_pr

from bridge.db import transaction
from bridge.review_github import OWNER_LABEL_COLOR, GitHubError, add_label, map_node_to_open_pr
from bridge.reviews import poll_review_queue, store_open_prs

LABEL = "owner-reviewed"
LABELLED = replace(CONFIG, review_owner_label=LABEL)
ADD = ["api", "-X", "POST", "repos/Org/app/issues/7/labels", "-f", f"labels[]={LABEL}"]


def publish(conn: Any) -> None:
    with transaction(conn):
        conn.execute("UPDATE review_prs SET reviewed_at = 600, reviewed_by = 'theo' WHERE ref = ?", (REF,))


def poll(conn: Any, config: Any, prs: list[Any]) -> mock.Mock:
    with (
        mock.patch("bridge.reviews.fetch_open_prs", return_value=prs),
        mock.patch("bridge.reviews.add_label") as added,
    ):
        poll_review_queue(conn, config, 900, None, None)
    return added


def test_the_poll_reads_each_prs_labels() -> None:
    node: dict[str, Any] = {
        "repository": {"nameWithOwner": "Org/app"}, "number": 7, "title": "T", "url": "u", "isDraft": False,
        "headRefName": "dev/x", "headRefOid": "a1", "baseRefName": "main", "additions": 1, "deletions": 0,
        "files": {"pageInfo": {"hasNextPage": False}, "nodes": []}, "commits": {"nodes": []},
        "reviewRequests": {"nodes": []}, "reviews": {"nodes": []}, "latestReviews": {"nodes": []},
        "updatedAt": "2026-11-02T10:00:00Z", "labels": {"nodes": [{"name": "bug"}, {"name": LABEL}]},
    }  # fmt: skip
    assert map_node_to_open_pr(node, CONFIG).labels == ("bug", LABEL)
    assert map_node_to_open_pr({**node, "labels": None}, CONFIG).labels == ()


def test_a_published_pr_without_the_label_gets_it_at_the_poll_once(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    assert poll(conn, LABELLED, [make_pr()]).call_count == 0, "not published yet"
    publish(conn)
    assert poll(conn, LABELLED, [make_pr()]).call_args_list == [mock.call("Org/app", 7, LABEL)]
    assert poll(conn, LABELLED, [make_pr(labels=("Owner-Reviewed",))]).call_count == 0, "GitHub ignores case"


def test_with_no_label_configured_the_poll_labels_nothing(tmp_path: Path) -> None:
    conn = setup_pr(tmp_path)
    publish(conn)
    assert poll(conn, CONFIG, [make_pr()]).call_count == 0


def test_a_failed_label_is_logged_and_tried_again_at_the_next_poll(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = setup_pr(tmp_path)
    publish(conn)
    with (
        mock.patch("bridge.reviews.fetch_open_prs", return_value=[make_pr()]),
        mock.patch("bridge.reviews.add_label", side_effect=GitHubError("gh failed: HTTP 502")) as added,
    ):
        poll_review_queue(conn, LABELLED, 900, None, None)
        poll_review_queue(conn, LABELLED, 900, None, None)
    assert added.call_count == 2
    assert capsys.readouterr().out.count("owner label on Org/app#7 failed: gh failed: HTTP 502") == 2


def test_adding_the_label_is_one_call_when_the_repo_has_it() -> None:
    with mock.patch("bridge.review_github.run_gh", return_value="[]") as gh:
        add_label("Org/app", 7, LABEL)
    assert gh.call_args_list == [mock.call(ADD)]


def test_a_repo_without_the_label_gets_it_created_then_added() -> None:
    with mock.patch("bridge.review_github.run_gh", side_effect=[GitHubError("gh failed: HTTP 422"), "", "[]"]) as gh:
        add_label("Org/app", 7, LABEL)
    create = gh.call_args_list[1].args[0]
    assert create[:5] == ["label", "create", LABEL, "-R", "Org/app"] and OWNER_LABEL_COLOR in create
    assert gh.call_args_list[2] == mock.call(ADD)


def test_a_label_that_cannot_be_added_raises_for_the_caller_to_log() -> None:
    with (
        mock.patch("bridge.review_github.run_gh", side_effect=GitHubError("gh failed: HTTP 403")),
        pytest.raises(GitHubError),
    ):
        add_label("Org/app", 7, LABEL)


STUB_GH = """#!/bin/sh
echo "$@" >> "{log}"
case "$*" in
  "label create"*) touch "{made}" ;;
  "api -X POST"*) [ -f "{made}" ] || {{ echo 'HTTP 422' >&2; exit 1; }} ;;
esac
"""


def _wait_for(path: Path, lines: int) -> list[str]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        found = path.read_text().splitlines() if path.exists() else []
        if len(found) >= lines:
            return found
        time.sleep(0.05)
    return path.read_text().splitlines() if path.exists() else []


@pytest.mark.parametrize("fails", [False, True])
def test_publish_labels_the_pr_and_a_failure_does_not_stop_it(bridge_env: BridgeEnv, fails: bool) -> None:
    log, made = bridge_env.root / "gh.log", bridge_env.root / "label-made"
    gh = bridge_env.root / "bin" / "gh"
    gh.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexit 1\n' if fails else STUB_GH.format(log=log, made=made))
    gh.chmod(0o755)
    config = bridge_env.root / "config.toml"
    config.write_text(config.read_text() + f'review_owner_label = "{LABEL}"\n')
    conn = bridge_env.connect()
    with transaction(conn):
        store_open_prs(conn, [make_pr(), make_pr(ref="pr:Org/app#8", number=8)], 500)
    port = free_port()
    server = subprocess.Popen(
        [str(VENV_BIN / "bridge"), "serve", "--port", str(port)], env=bridge_env.env, stdout=subprocess.PIPE, text=True
    )
    try:
        wait_up(f"http://127.0.0.1:{port}")
        http_request(f"http://127.0.0.1:{port}/api/reviews/Org/app/7/publish", post=True)
        http_request(f"http://127.0.0.1:{port}/api/reviews/Org/app/8/retry", post=True, expect=409)
        calls = _wait_for(log, 3)
        time.sleep(0.3)
    finally:
        server.terminate()
        out, _ = server.communicate()
    reviewed = dict(conn.execute("SELECT number, reviewed_at IS NOT NULL FROM review_prs").fetchall())
    assert reviewed == {7: 1, 8: 0}
    if fails:
        assert "owner label on Org/app#7 failed" in out
        return
    assert [c.split()[:3] for c in calls] == [["api", "-X", "POST"], ["label", "create", LABEL], ["api", "-X", "POST"]]
    assert all("issues/7/labels" in c for c in (calls[0], calls[2])) and len(calls) == 3


def test_a_host_worker_sends_label_commands_to_the_mac_guard() -> None:
    from bridge import gbridge
    from bridge.install import list_guard_globs

    globs = list_guard_globs()
    assert gbridge.may_need_guard(globs, f"gh pr edit 7 --add-label {LABEL}")
    assert gbridge.may_need_guard(globs, f"gh pr create -t Fix -b b -l {LABEL}")
    assert gbridge.may_need_guard(globs, f"gh pr new -t Fix -b b -l {LABEL}")
    assert gbridge.may_need_guard(globs, f"gh pr create -dl {LABEL} -t Fix")
    assert gbridge.may_need_guard(globs, f"gh api -X DELETE repos/Org/app/issues/7/labels/{LABEL}")
