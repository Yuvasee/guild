import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from bridge.asks import map_words_to_sendable, send_from_bridge
from bridge.backport import is_backport_base
from bridge.board import Recipient
from bridge.config import Config
from bridge.guard import READY
from bridge.leads import REVIEW_DOC_SPEC
from bridge.review_post import (
    FAILED,
    INTERRUPTED,
    WAITING_DRAFT,
    WAITING_SENTENCE,
    WAITING_STACK,
    map_stalled_post_to_reason,
)
from bridge.review_worktree import is_worth_preparing
from bridge.reviews import (
    HIS,
    SETTLE_MS,
    WAKES_PER_HOUR_MAX,
    count_recent_wakes,
    find_owner,
    read_github_failure,
)

POST_STALL_AFTER_MS = 15 * 60_000
COMMENT_STALL_AFTER_MS = 10 * 60_000
READY_STALL_AFTER_MS = 30 * 60_000
DOC_STALL_AFTER_MS = 60 * 60_000
OWNER_STALL_AFTER_MS = 15 * 60_000
WEAKEST_OWNER_VIA = "ref"
"""find_owner's last resort among live sessions: the one that mentioned the PR last."""
_STALLED_POSTS = (WAITING_SENTENCE, WAITING_STACK, WAITING_DRAFT, FAILED, INTERRUPTED)
_SEEN = ":seen"
_TO = ">to:"
TITLE_MAX_CHARS = 200
WHY_MAX_CHARS = 1000
"""Quoted errors can be long; the cut keeps To unblock inside one message."""


@dataclass(frozen=True)
class Waiting:
    pr: sqlite3.Row
    key: str
    """Its signal_posts key, one per PR and state: present once the assistant is told; others get a suffix."""
    since: int | None
    """None: since the guard first saw it, kept under key + ':seen'."""


@dataclass(frozen=True)
class Watch:
    prefix: str
    limit_ms: int
    find: Callable[[sqlite3.Connection, Config, int], list[Waiting]]
    """Everything in this state now, however short its wait: whatever is missing has moved on."""
    headline: Callable[[Config, Waiting, int, int], str]
    reason: Callable[[sqlite3.Connection, Config, Waiting, int, int | None], tuple[str, str]]
    """Why it waits, and what unblocks it; given when the transcript scan last finished, as tell_of_stalls is."""


def tell_of_stalls(
    conn: sqlite3.Connection, config: Config, now: int, scan_finished_at: int | None = None
) -> list[tuple[int, Recipient]]:
    """Once per PR and state past its limit, the assistant and whoever hears bridge's own failures each hear why it
    waits and what unblocks it. One not reachable is tried again next round. A state that moves on is forgotten, so
    its return is told again. scan_finished_at: when serve's transcript scan last finished, 0 for none since serve
    started, None when not known."""
    told: list[tuple[int, Recipient]] = []
    handles = list(dict.fromkeys(h for h in (config.assistant_handle, config.auto_merge_failures_to) if h))
    for watch in WATCHES:
        waiting = watch.find(conn, config, now)
        _forget_moved_on(conn, watch.prefix, {w.key for w in waiting})
        for w in waiting:
            since = w.since if w.since is not None else _read_first_seen(conn, w.key, now)
            if now - since < watch.limit_ms:
                continue
            due = [
                h
                for h in handles
                if not conn.execute(
                    "SELECT 1 FROM signal_posts WHERE key = ?", (_map_key_to_told(w.key, h, config),)
                ).fetchone()
            ]
            if not due:
                continue
            why, unblock = watch.reason(conn, config, w, now, scan_finished_at)
            text = map_words_to_sendable(
                conn,
                f"{w.pr['url']} ({_cut(str(w.pr['title']), TITLE_MAX_CHARS)}): "
                f"{watch.headline(config, w, since, now)}. Why: {_cut(why, WHY_MAX_CHARS)}. To unblock: {unblock}. "
                "bridge tells you once for this state.",
            )
            for handle in due:
                delivered = send_from_bridge(conn, config, handle, text, now)
                if delivered:
                    conn.execute(
                        "INSERT INTO signal_posts (key, message_id, posted_at) VALUES (?, ?, ?)",
                        (_map_key_to_told(w.key, handle, config), delivered[0][0], now),
                    )
                    told.extend(delivered)
    return told


def _map_key_to_told(key: str, handle: str, config: Config) -> str:
    """The assistant's mark is the bare key, as the post check kept it before the guard."""
    return key if handle == config.assistant_handle else f"{key}{_TO}{handle}"


def _forget_moved_on(conn: sqlite3.Connection, prefix: str, present: set[str]) -> None:
    for (key,) in conn.execute(
        "SELECT key FROM signal_posts WHERE substr(key, 1, ?) = ?", (len(prefix), prefix)
    ).fetchall():
        if key.removesuffix(_SEEN) not in present and key.partition(_TO)[0] not in present:
            conn.execute("DELETE FROM signal_posts WHERE key = ?", (key,))


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _read_first_seen(conn: sqlite3.Connection, key: str, now: int) -> int:
    seen = conn.execute("SELECT posted_at FROM signal_posts WHERE key = ?", (key + _SEEN,)).fetchone()
    if seen is not None:
        return int(seen[0])
    conn.execute("INSERT INTO signal_posts (key, message_id, posted_at) VALUES (?, 0, ?)", (key + _SEEN, now))
    return now


def _format_at(config: Config, at: int) -> str:
    zone = ZoneInfo(config.work_timezone) if config.work_timezone else None
    return datetime.fromtimestamp(at / 1000, zone).strftime("%m-%d %H:%M")


def _format_wait(ms: int) -> str:
    minutes = max(0, ms) // 60_000
    return f"{minutes} min" if minutes < 120 else f"{minutes // 60} h {minutes % 60} min"


def _name(pr: sqlite3.Row) -> str:
    return f"{pr['repo']}#{pr['number']}"


def _claim_hint(pr: sqlite3.Row) -> str:
    return (
        f"the session that owns it claims it, bridge claim {pr['ref']}, or its lead names it in its work: guild work "
        f"set <name> {_name(pr)}"
    )


def find_stalled_posts(conn: sqlite3.Connection, config: Config, now: int) -> list[Waiting]:
    """A post seen in the channel, as one whose sender stopped after sending, is recorded on the next round instead."""
    rows = conn.execute(
        f"""
        SELECT * FROM review_prs p WHERE closed_at IS NULL AND reviewed_at IS NOT NULL
        AND post_state IN ({", ".join("?" * len(_STALLED_POSTS))})
        AND NOT EXISTS (SELECT 1 FROM review_slack_posts s WHERE s.repo = lower(p.repo) AND s.number = p.number)
        """,
        _STALLED_POSTS,
    ).fetchall()
    return [
        Waiting(row, f"post-stall:{row['ref']}:{row['post_state']}", int(row["reviewed_at"]))
        for row in rows
        if not is_backport_base(config.backport_bases, str(row["base"]))
    ]


def _headline_post(config: Config, w: Waiting, since: int, now: int) -> str:
    return (
        f"{config.owner_name} pressed Publish at {_format_at(config, since)} and its {config.review_channel_name} "
        f"post is still not out ({w.pr['post_state']})"
    )


def _reason_post(
    conn: sqlite3.Connection, config: Config, w: Waiting, now: int, scan_finished_at: int | None
) -> tuple[str, str]:
    return map_stalled_post_to_reason(conn, config, w.pr)


def find_unrouted_comments(conn: sqlite3.Connection, config: Config, now: int) -> list[Waiting]:
    """A comment with no verdict waits from the end of its settle time; the owner's comment not yet told, from when
    it was written."""
    rows = conn.execute(
        """
        SELECT p.*, min(CASE WHEN c.verdict IS NULL THEN c.created_at + ? ELSE c.created_at END) AS stall_since,
               sum(c.verdict IS NULL) AS stall_unclassified, sum(c.verdict IS NOT NULL) AS stall_untold
        FROM review_comments c JOIN review_prs p ON p.ref = c.ref
        WHERE p.closed_at IS NULL AND (c.verdict IS NULL OR (c.verdict = ? AND c.told_at IS NULL))
        GROUP BY p.ref
        """,
        (SETTLE_MS, HIS),
    ).fetchall()
    return [Waiting(row, f"stall:comment:{row['ref']}", int(row["stall_since"])) for row in rows]


def _headline_comments(config: Config, w: Waiting, since: int, now: int) -> str:
    return f"a comment has waited {_format_wait(now - since)} to reach anyone"


def _reason_comments(
    conn: sqlite3.Connection, config: Config, w: Waiting, now: int, scan_finished_at: int | None
) -> tuple[str, str]:
    pr = w.pr
    whys: list[str] = []
    unblocks: list[str] = []
    if unclassified := int(pr["stall_unclassified"]):
        github = read_github_failure(conn)
        also = (
            f"; GitHub is failing too: the review poll's last read of it failed at {_format_at(config, github[1])}: "
            f"{github[0]}, so newer comments are not read either"
            if github
            else ""
        )
        scan = (
            ""
            if scan_finished_at is None
            else "; no transcript scan has finished since serve started"
            if scan_finished_at == 0
            else f"; the transcript scan last finished at {_format_at(config, scan_finished_at)}"
        )
        whys.append(
            f"{unclassified} comment(s) have no verdict, so no one was told; no round has classified it: that needs a "
            f"transcript scan begun after it, then a round that commits{scan}{also}"
        )
        unblocks.append(
            "fix the error that bridge's serve.log shows under 'review queue poll failed' or 'transcript scan failed'"
        )
    if untold := int(pr["stall_untold"]):
        owner = find_owner(conn, config, str(pr["ref"]), str(pr["repo"]), str(pr["head"]))
        if owner is None:
            whys.append(
                f"{untold} of {config.owner_name}'s comment(s) wait for an owner: bridge finds no session that owns "
                "the PR"
            )
            unblocks.append(_claim_hint(pr))
        elif count_recent_wakes(conn, str(pr["ref"]), now) >= WAKES_PER_HOUR_MAX:
            whys.append(
                f"{untold} of {config.owner_name}'s comment(s) wait: the PR had {WAKES_PER_HOUR_MAX} wakes in the last "
                "hour, so the rest go out when it passes"
            )
            unblocks.append("nothing, unless it is urgent: then tell its owner")
        else:
            whys.append(
                f"{untold} of {config.owner_name}'s comment(s) are due to {owner.handle}, but no round has sent them"
            )
            unblocks.append("fix the error that bridge's serve.log shows under 'review queue poll failed'")
    return "; ".join(whys), "; ".join(dict.fromkeys(unblocks))


def find_ready_unmerged(conn: sqlite3.Connection, config: Config, now: int) -> list[Waiting]:
    rows = conn.execute("SELECT * FROM review_prs WHERE closed_at IS NULL AND merge_path = ?", (READY,)).fetchall()
    return [Waiting(row, f"stall:ready:{row['ref']}", None) for row in rows]


def _headline_ready(config: Config, w: Waiting, since: int, now: int) -> str:
    return f"bridge's merge rule has found it ready to merge for {_format_wait(now - since)}, and it is still open"


def _reason_ready(
    conn: sqlite3.Connection, config: Config, w: Waiting, now: int, scan_finished_at: int | None
) -> tuple[str, str]:
    pr = w.pr
    failed = f"; the automatic merge failed: {pr['auto_merge_error']}" if pr["auto_merge_error"] else ""
    if pr["merge_told_at"] is not None:
        why = f"its owner was told at {_format_at(config, int(pr['merge_told_at']))} and has not merged it{failed}"
        return why, f"its owner merges it once judged safe, gh pr merge {pr['url']}, or says why it waits"
    owner = find_owner(conn, config, str(pr["ref"]), str(pr["repo"]), str(pr["head"]))
    if owner is not None:
        why = f"it is due to be told to {owner.handle}, but no round has sent it{failed}"
        return why, "fix the error that bridge's serve.log shows under 'review queue poll failed'"
    return f"no owner was told: bridge finds no session that owns the PR{failed}", f"{_claim_hint(pr)}; then it is told"


def find_unregistered_docs(conn: sqlite3.Connection, config: Config, now: int) -> list[Waiting]:
    """Only while the PR still needs a doc: not a draft, not reviewed yet, still fresh."""
    rows = conn.execute(
        """
        SELECT * FROM review_prs
        WHERE closed_at IS NULL AND doc_asked_at IS NOT NULL AND doc_file IS NULL AND doc_at IS NULL
        """
    ).fetchall()
    return [
        Waiting(row, f"stall:doc:{row['ref']}", int(row["doc_asked_at"]))
        for row in rows
        if is_worth_preparing(row, config, now)
    ]


def _headline_doc(config: Config, w: Waiting, since: int, now: int) -> str:
    return (
        f"bridge asked for its review doc at {_format_at(config, since)}, {_format_wait(now - since)} ago, and none "
        "is registered"
    )


def _reason_doc(
    conn: sqlite3.Connection, config: Config, w: Waiting, now: int, scan_finished_at: int | None
) -> tuple[str, str]:
    pr = w.pr
    owner = find_owner(conn, config, str(pr["ref"]), str(pr["repo"]), str(pr["head"]))
    who = f"its owner now is {owner.handle}" if owner is not None else "bridge now finds no session that owns it"
    why = f"the PR is large, so {config.owner_name}'s review needs a document; {who}"
    return why, (
        f"its owner writes it as {REVIEW_DOC_SPEC} lays out and registers it: guild review doc {_name(pr)} "
        "/path/file.html"
    )


def find_guessed_owners(conn: sqlite3.Connection, config: Config, now: int) -> list[Waiting]:
    found: list[Waiting] = []
    for row in conn.execute("SELECT * FROM review_prs WHERE closed_at IS NULL").fetchall():
        owner = find_owner(conn, config, str(row["ref"]), str(row["repo"]), str(row["head"]))
        if owner is not None and owner.via == WEAKEST_OWNER_VIA:
            found.append(Waiting(row, f"stall:owner:{row['ref']}", None))
    return found


def _headline_owner(config: Config, w: Waiting, since: int, now: int) -> str:
    return f"its owner has been only a guess for {_format_wait(now - since)}"


def _reason_owner(
    conn: sqlite3.Connection, config: Config, w: Waiting, now: int, scan_finished_at: int | None
) -> tuple[str, str]:
    pr = w.pr
    owner = find_owner(conn, config, str(pr["ref"]), str(pr["repo"]), str(pr["head"]))
    why = (
        f"its notices go to {owner.handle if owner else 'no one'} only because that live session mentioned it last; "
        "nothing stronger names an owner: no claim on it, no write to its branch, no lead's work naming it"
    )
    return why, _claim_hint(pr)


WATCHES = (
    Watch("post-stall:", POST_STALL_AFTER_MS, find_stalled_posts, _headline_post, _reason_post),
    Watch("stall:comment:", COMMENT_STALL_AFTER_MS, find_unrouted_comments, _headline_comments, _reason_comments),
    Watch("stall:ready:", READY_STALL_AFTER_MS, find_ready_unmerged, _headline_ready, _reason_ready),
    Watch("stall:doc:", DOC_STALL_AFTER_MS, find_unregistered_docs, _headline_doc, _reason_doc),
    Watch("stall:owner:", OWNER_STALL_AFTER_MS, find_guessed_owners, _headline_owner, _reason_owner),
)
