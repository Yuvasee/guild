import random
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from test_reviews import CONFIG, make_pr

from bridge.db import connect
from bridge.review_github import GitHubError, OpenPr, RoundOver, bound_gh_round, run_gh
from bridge.reviews import (
    ROUND_LIMIT_S,
    SLOW_ROUND_S,
    list_refs_with_new_activity,
    poll_review_queue,
    read_github_failure,
)

PRS = [make_pr(ref=f"pr:Org/app#{n}", number=n, url=f"https://github.com/Org/app/pull/{n}") for n in range(1, 9)]


class FakeGh:
    """gh on a fake clock: 'hang' answers nothing until it is killed at its timeout, 'fast' answers an empty page in
    `seconds`, and so do the first `fast_calls` calls of 'hang'. Any other mode is the PR numbers whose reads hang."""

    def __init__(self, mode: str, seconds: float = 1.0, fast_calls: int = 0) -> None:
        self.mode, self.seconds, self.fast_calls, self.clock, self.timeouts = (
            mode,
            seconds,
            fast_calls,
            0.0,
            list[float](),
        )

    def monotonic(self) -> float:
        return self.clock

    def run(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        timeout = float(kwargs["timeout"])
        self.timeouts.append(timeout)
        hangs = self.mode == "hang" or (
            self.mode != "fast" and any(f"/pulls/{n}/" in a for a in args for n in self.mode)
        )
        if hangs and len(self.timeouts) > self.fast_calls:
            self.clock += timeout
            raise subprocess.TimeoutExpired(args, timeout)
        self.clock += self.seconds
        return subprocess.CompletedProcess(args, 0, stdout="[]", stderr="")


def poll_round(conn: Any, gh: FakeGh, now: int, prs: list[OpenPr] = PRS) -> list[object]:
    with (
        mock.patch("bridge.reviews.fetch_open_prs", return_value=prs),
        mock.patch("bridge.review_github.subprocess.run", side_effect=gh.run),
        mock.patch("bridge.review_github.time.monotonic", side_effect=gh.monotonic),
    ):
        return list(poll_review_queue(conn, CONFIG, now, None, None))


def read_slow_log(tmp_path: Path) -> str:
    path = tmp_path / "slow.log"
    return path.read_text() if path.exists() else ""


def test_a_round_on_a_hanging_gh_stops_at_its_limit_logs_itself_and_the_next_round_reads_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    conn = connect(tmp_path / "t.db")
    gh = FakeGh("hang")
    poll_round(conn, gh, 1000)
    assert gh.clock <= ROUND_LIMIT_S, "no call runs past the round's limit"
    calls = ROUND_LIMIT_S // 60
    assert len(gh.timeouts) == calls, "each PR's comment read waits out its 60 s timeout until the limit"
    failure = read_github_failure(conn)
    assert failure is not None, "the 'GitHub is failing' notice reads this"
    assert failure == (
        f"the review poll round ran past {ROUND_LIMIT_S // 60} min and stopped after {calls} gh calls ({calls} failed);"
        " the next round starts over",
        1000,
    )
    assert f"review poll\nround took {ROUND_LIMIT_S} s: {calls} gh calls, {calls} failed\n" in read_slow_log(tmp_path)
    assert len(list_refs_with_new_activity(conn)) == len(PRS), "every unread PR is still due"

    gh = FakeGh("fast")
    poll_round(conn, gh, 2000)
    assert len(gh.timeouts) == 3 * len(PRS), "inline comments, conversation comments and reviews of each PR"
    assert list_refs_with_new_activity(conn) == []
    assert read_github_failure(conn) is None, "a round that reads GitHub ends the failure"


def test_a_call_the_limit_cuts_short_ends_the_round(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    conn = connect(tmp_path / "t.db")
    gh = FakeGh("hang", seconds=30.0, fast_calls=1)
    poll_round(conn, gh, 1000)
    assert gh.timeouts[-1] == 30.0, "the last call gets only what is left of the round"
    assert gh.clock == ROUND_LIMIT_S
    failure = read_github_failure(conn)
    assert (
        failure is not None
        and f"stopped after {len(gh.timeouts)} gh calls ({len(gh.timeouts) - 1} failed)" in failure[0]
    )


def test_a_quick_round_logs_nothing_and_a_slow_one_does(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    conn = connect(tmp_path / "t.db")
    poll_round(conn, FakeGh("fast", seconds=1.0), 1000)
    assert read_slow_log(tmp_path) == ""
    changed = [replace(pr, updated_at="2026-11-03T10:00:00Z") for pr in PRS]
    seconds = SLOW_ROUND_S / (3 * len(PRS)) + 1
    poll_round(conn, FakeGh("fast", seconds=seconds), 2000, changed)
    took = round(3 * len(PRS) * seconds)
    assert f"round took {took} s: {3 * len(PRS)} gh calls, 0 failed" in read_slow_log(tmp_path)
    assert read_github_failure(conn) is None, "slow but within the limit is not a failure"


def test_gh_outside_a_round_is_neither_cut_nor_refused() -> None:
    gh = FakeGh("hang")
    with (
        mock.patch("bridge.review_github.subprocess.run", side_effect=gh.run),
        mock.patch("bridge.review_github.time.monotonic", side_effect=gh.monotonic),
    ):
        with pytest.raises(GitHubError, match="TimeoutExpired"):
            run_gh(["api", "x"])
        with bound_gh_round(0), pytest.raises(RoundOver):
            run_gh(["api", "x"])
        with pytest.raises(GitHubError, match="TimeoutExpired"):
            run_gh(["api", "x"])
    assert gh.timeouts == [60.0, 60.0], "the round's limit is gone once it ends"


def test_prs_whose_reads_always_hang_hold_back_none_of_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path))
    random.seed(7)
    conn = connect(tmp_path / "t.db")
    hanging = "12345"
    for now in range(1000, 1010):
        poll_round(conn, FakeGh(hanging), now)
    due = {ref for ref, *_ in list_refs_with_new_activity(conn)}
    assert due == {f"pr:Org/app#{n}" for n in hanging}, "each round reaches other PRs before the limit"
