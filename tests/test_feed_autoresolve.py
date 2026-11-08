import json
import sqlite3
from pathlib import Path
from types import MappingProxyType

import pytest
from conftest import BridgeEnv, http_request

from bridge.config import Config
from bridge.db import connect, now_ms
from bridge.feed_autoresolve import ENDED_FRESH_MS, find_settled_items, list_open_item_prs, resolve_settled_items
from bridge.refs import extract_named_prs

LABELS = {"mono": "acme/monorepo", "infr": "acme/infra"}
CONFIG = Config(pr_repo_aliases=MappingProxyType({"acme/root": "acme/monorepo"}))
PR = "pr:acme/monorepo#38"
URL = "https://github.com/acme/monorepo/pull/38"
POSTED = 1_000_000
AT = 5_000_000


@pytest.mark.parametrize(
    ("text", "prs", "unpinned"),
    [
        (f"PR #38 (PROJ-56): merge blocked. {URL}", [PR], False),
        ("mono#38 waits for CI", [PR], False),
        ("acme/monorepo#38 and infr#7", [PR, "pr:acme/infra#7"], False),
        ("PR #38 merge blocked", [], True),
        (f"#64 superseded by {URL}", ["pr:acme/monorepo#64", PR], False),
        ("blocked on wat#12", [], True),
        (f"{URL} supersedes #64", [PR, "pr:acme/monorepo#64"], False),
        ("PROJ-56 needs a decision", [], False),
        (f"{URL}#issuecomment-1", [PR], False),
        (f"{URL} and #7", [PR, "pr:acme/monorepo#7"], False),
    ],
)
def test_named_prs(text: str, prs: list[str], unpinned: bool) -> None:
    assert extract_named_prs(text, LABELS) == (prs, unpinned)


def db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "bridge.db")
    conn.execute("INSERT INTO enrichment (ref, data, fetched_at, ok_at) VALUES ('pr:acme/monorepo#1', '{}', 0, 0)")
    return conn


def post(conn: sqlite3.Connection, kind: str, text: str, body: str = "", at: int = POSTED, conv: str = "conv-a") -> int:
    cursor = conn.execute(
        "INSERT INTO user_items (created_at, from_conversation, from_handle, severity, text, source, kind, body)"
        " VALUES (?, ?, 'a', 'blocker', ?, 'agent', ?, ?)",
        (at, conv, text, kind, body or None),
    )
    return int(cursor.lastrowid or 0)


def end_pr(conn: sqlite3.Connection, ref: str, state: str, closed_at: str, ok_at: int = AT) -> None:
    data = json.dumps({"state": state, "closed_at": closed_at})
    conn.execute(
        "INSERT OR REPLACE INTO enrichment (ref, data, fetched_at, ok_at) VALUES (?, ?, ?, ?)",
        (ref, data, ok_at, ok_at),
    )


# POSTED is 1970-01-01T00:16:40Z.
AFTER = "1970-01-01T00:17:00Z"
BEFORE = "1970-01-01T00:16:00Z"


def test_a_blocked_post_resolves_once_its_one_pr_merges_after_it(tmp_path: Path) -> None:
    conn = db(tmp_path)
    blocked = post(conn, "blocked", "PR #38 (PROJ-56): merge blocked by the classifier", f"Merge it: {URL}")
    assert list_open_item_prs(conn, CONFIG) == [PR]
    assert find_settled_items(conn, CONFIG, AT) == []
    end_pr(conn, PR, "MERGED", AFTER)
    [settled] = resolve_settled_items(conn, CONFIG, AT)
    assert (settled.item_id, settled.why) == (blocked, "resolved: mono#38 merged")
    row = conn.execute("SELECT resolved_at, resolved_why FROM user_items WHERE id = ?", (blocked,)).fetchone()
    assert tuple(row) == (AT, "resolved: mono#38 merged")
    assert list_open_item_prs(conn, CONFIG) == []


def test_what_stays_open(tmp_path: Path) -> None:
    conn = db(tmp_path)
    post(conn, "blocked", "Deploy broken after the merge", f"{URL}")
    post(conn, "blocked", "mono#38 and mono#65 both wait")
    post(conn, "blocked", f"{URL} and #7 both wait")
    post(conn, "blocked", "PR #38 blocked")
    post(conn, "blocked", "PROJ-56 blocked")
    post(conn, "decision", "Also fix the old endpoint?", URL)
    post(conn, "blocked", "OK to update the stack it adds?", URL)
    post(conn, "blocked", "mono#38 rollout blocked", "May I enable production access?")
    end_pr(conn, PR, "MERGED", BEFORE)
    end_pr(conn, "pr:acme/monorepo#65", "MERGED", AFTER)
    assert find_settled_items(conn, CONFIG, AT) == []
    end_pr(conn, PR, "CLOSED", AFTER, ok_at=AT - ENDED_FRESH_MS - 1)
    assert find_settled_items(conn, CONFIG, AT) == []
    conn.execute("UPDATE enrichment SET ok_at = ?, error = 'gh failed' WHERE ref = ?", (AT, PR))
    assert find_settled_items(conn, CONFIG, AT) == []
    end_pr(conn, PR, "MERGED", AFTER)
    # Only the first names one PR, asks nothing, and was posted before the merge.
    assert [s.text for s in find_settled_items(conn, CONFIG, AT)] == ["Deploy broken after the merge"]


def test_a_decision_resolves_once_its_pr_ended_and_its_author_posted_done(tmp_path: Path) -> None:
    conn = db(tmp_path)
    decision = post(conn, "decision", "Merge mono#38 now or wait for review?")
    asking = post(conn, "blocked", "mono#38 blocked: OK to also update the stack?")
    other = post(conn, "decision", "mono#38: also backport?", conv="conv-b")
    post(conn, "done", "PROJ-64 closed", f"#64 superseded by {URL}", at=POSTED + 10)
    post(conn, "done", "wat#12 done", f"{URL} still waits", at=POSTED + 20)
    post(conn, "done", "Earlier: mono#38 opened", at=POSTED - 1)
    end_pr(conn, PR, "MERGED", AFTER)
    assert find_settled_items(conn, CONFIG, AT) == []
    done = post(conn, "done", "PROJ-56 merged", f"{URL} merged. Supersedes #64 (closed).", at=POSTED + 36)
    settled = {s.item_id: s.why for s in resolve_settled_items(conn, CONFIG, AT)}
    why = f"resolved: mono#38 merged, its author posted done (#{done})"
    assert settled == {decision: why, asking: why}
    assert other not in settled


def test_a_done_alone_settles_nothing_while_the_pr_is_open(tmp_path: Path) -> None:
    conn = db(tmp_path)
    post(conn, "decision", "mono#38: deploy approval?")
    post(conn, "done", "mono#38 review completed; deployment approval still needed", at=POSTED + 1)
    assert find_settled_items(conn, CONFIG, AT) == []


def test_a_post_the_owner_put_back_stays(bridge_env: BridgeEnv, dashboard: str) -> None:
    conn = connect(bridge_env.state_dir / "bridge.db")
    blocked = post(conn, "blocked", f"Blocked: {URL}")
    end_pr(conn, PR, "CLOSED", AFTER, ok_at=now_ms())
    resolve_settled_items(conn, Config(), now_ms())
    assert conn.execute("SELECT resolved_why FROM user_items WHERE id = ?", (blocked,)).fetchone()[0] == (
        "resolved: mono#38 closed"
    )
    http_request(dashboard + f"/api/items/{blocked}/unresolve", post=True)
    row = conn.execute("SELECT resolved_at, resolved_why, auto_resolve_off FROM user_items WHERE id = ?", (blocked,))
    assert tuple(row.fetchone()) == (None, None, 1)
    assert resolve_settled_items(conn, Config(), now_ms()) == []
