import json
import re
import sqlite3
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from bridge.agent_prefix import CLAUDE_PREFIX
from bridge.backport import BackportVerdict, check_backport, is_backport_base
from bridge.board import (
    BODY_MAX_CHARS,
    SYSTEM_CONVERSATION,
    AddressError,
    Party,
    Recipient,
    convert_item_id_to_question_ref,
    resolve_handle,
    send_message,
)
from bridge.config import Config, read_overlay
from bridge.db import transaction
from bridge.enrich import PrStatus, fetch_prs
from bridge.feed import REVIEW_QUEUE, resolve_tab
from bridge.guard import (
    BLOCKED_ON_GITHUB,
    CI_PENDING,
    CI_RED,
    CONFLICTS,
    GITHUB_STALE_S,
    HELD,
    NOT_APPROVED,
    NOT_OWNER_REVIEWED,
    OWNER_COMMENTS_OPEN,
    READY,
    is_approved_row,
    list_merge_waits,
    map_row_to_merge_facts,
    sync_claims_flag,
)
from bridge.leads import find_lead_on_surface, is_conversation_locked, list_leads
from bridge.refs import map_ref_to_label
from bridge.registry import record_tab_parent
from bridge.review_github import (
    UNKNOWN_DIFF,
    DocDiff,
    FetchedComment,
    GitHubError,
    OpenPr,
    ReviewThreads,
    fetch_ci_blocked_by,
    fetch_comments,
    fetch_doc_diff,
    fetch_open_prs,
    fetch_review_threads,
    list_loc_excluded,
)
from bridge.review_marks import convert_reviews_to_text, map_text_to_reviews
from bridge.review_worktree import (
    DOC_SHA_CHARS,
    REVIEW_LARGE_LOC,
    map_doc_to_diff_key,
    map_row_to_doc_behind,
    map_row_to_doc_heads,
    map_worktree_to_workspace,
)

GITHUB_EVERY_S = 90
PARENT_WALK_MAX = 5
WAKES_PER_HOUR_MAX = 3
HOUR_MS = 3_600_000
CLOCK_SKEW_MS = 60_000
SETTLE_MS = 30_000
OPAQUE_MARK_WINDOW_MS = 600_000
VISIBLE_MARK_WINDOW_MS = 90_000
REMOTE_ACTIVE_WINDOW_MS = 900_000
MARKS_KEPT_MS = 7 * 24 * 3_600_000
BRANCH_WRITES_KEPT_MS = 30 * 24 * 3_600_000
WAITING_COMMAND_WINDOW_MS = 2 * HOUR_MS
MARK_COMMAND_CHARS = 16_000
BODY_PREFIX_CHARS = 60
COMMENT_MAX_CHARS = 300

USAGE_GAP = "description lacks How this will be used"
_ISSUE_KEY_RE = re.compile(r"\b[A-Z]{2,}-\d+\b")
WAITING_FOR_OWNER = "waiting for you"
FIXED = "fixed"
COMMENTS_OPEN = "comments open"
_STATE_ORDER = {WAITING_FOR_OWNER: 0, FIXED: 1, COMMENTS_OPEN: 2, READY: 3, "published": 4}
WAITING_FOR_YOU = "waiting"
PUBLISHED = "published"
DRAFTS = "drafts"
BACKPORTS = "backports"
_GROUP_ORDER = (WAITING_FOR_YOU, READY, BACKPORTS, PUBLISHED, DRAFTS)
BACKPORT_CHECK_BUDGET_S = 20.0
BACKPORT_CHECKS_PER_POLL = 2
"""Checks run serially before comments and wakes, so a round waits at most this many budgets for them."""
BACKPORT_RECHECK_MS = 15 * 60_000
"""A refused check runs again this often on the same head: a named PR may merge meanwhile."""
LOC_RULE = 1
"""Raised when what counts as a line changes, so cached counts are made again."""
HIS = "his"
AGENT = "agent"
UNCLEAR = "unclear"
OLD = "old"
COLLEAGUE = "colleague"
REVIEWER = "reviewer"
_WAKING = (HIS, COLLEAGUE, REVIEWER)
APPROVED = "APPROVED"
CHANGES_REQUESTED = "CHANGES_REQUESTED"
AGENT_PREFIX = CLAUDE_PREFIX
QUOTE_MAX_CHARS = 140
HIS_QUOTE_MAX_CHARS = 80
ANSWERED_SHOWN_MS = 24 * HOUR_MS
"""How long an agent's answer to one of the owner's threads stays on the page: the day the owner looks back on."""
_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")

_PERMALINK_TS_RE = re.compile(r"/archives/\w+/p(\d{10})(\d{6})(?:\D|$)")
_REPO_PATH_RE = re.compile(r"repos/([\w.-]+/[\w.-]+)/(?:pulls|issues)/(\d+)/")
_REPO_ONLY_RE = re.compile(r"repos/([\w.-]+/[\w.-]+)/")
_PR_CMD_RE = re.compile(
    r"\bgh\s+pr\s+(?:comment|review)\b(?:\s+(-\S+\s+\S+\s+)*(?:(\d+)|\S*github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)))?"
)
_API_WRITE_RE = re.compile(r"\bgh\s+api\b[^\n;|&]*?(?:/comments|/replies|/reviews)\b[^\n;|&]*")
_API_WRITE_FLAG_RE = re.compile(r"(?:-X\s*POST|--method\s+POST|\s-[fF]\s|--field|--raw-field|--input)")
_API_NOT_CREATE_RE = re.compile(r"(?:-X\s*|--method[\s=]+)(?:PATCH|PUT|DELETE|GET)\b", re.I)
_QUOTED_RE = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")
_SHELL_RUNS_NEXT_RE = re.compile(r"(?:\s-[a-z]*c|\beval)\s+$")
_GRAPHQL_WRITE_RE = re.compile(
    r"\bgh\s+api\b[^\n;|&]*?\bgraphql\b.*\b(?:addPullRequestReview\w*|addComment|submitPullRequestReview)\b", re.S
)
_GIT_WRITE_RE = re.compile(r"\bgit((?:\s+-[Cc]\s+\S+)*)\s+(commit|push)\b([^\n;&|]*)")
_WAITING_COMMAND_RE = re.compile(r"--watch\b|\bsleep\b|\buntil\b|\bwhile\b|\bwait\b")
_OPAQUE_BODY_RE = re.compile(
    r"--body-file|-F\s+\S*=@|body=@|--input|-[fF]\s+body=[\"']?\$|--body\s+[\"']?\$[^(]|-b\s+[\"']?\$[^(]"
)


@dataclass(frozen=True)
class Owner:
    handle: str
    conversation_id: str
    surface: str | None
    lead: str | None
    via: str
    """claim, branch, ref, lead or pushed: how the session was found."""


@dataclass(frozen=True)
class Mark:
    at: int
    command: str


@dataclass(frozen=True)
class WakeItem:
    verdict: str
    author: str
    state: str | None
    url: str
    body: str


def store_open_prs(conn: sqlite3.Connection, prs: list[OpenPr], now: int) -> None:
    for pr in prs:
        conn.execute(
            """
            INSERT INTO review_prs (ref, repo, number, title, url, draft, head, head_oid, base, stack_parent, loc,
                                    checks, requested, approved_by, bot_approved, others_reviewed, merge_state,
                                    in_merge_queue, commit_oids, updated_at, first_seen_at,
                                    fetched_at, threads_key, loc_rule, body_reviews, depends_on, usage_missing,
                                    bot_cleared)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (ref) DO UPDATE SET
                title = excluded.title, url = excluded.url, draft = excluded.draft, head = excluded.head,
                head_oid = excluded.head_oid, base = excluded.base, stack_parent = excluded.stack_parent,
                loc = excluded.loc, loc_rule = excluded.loc_rule, checks = excluded.checks,
                requested = excluded.requested, body_reviews = excluded.body_reviews, depends_on = excluded.depends_on,
                usage_missing = excluded.usage_missing,
                approved_by = excluded.approved_by, bot_approved = excluded.bot_approved,
                bot_cleared = excluded.bot_cleared,
                ci_blocked_by = CASE WHEN excluded.checks = 'FAILURE' THEN ci_blocked_by END,
                ci_blocked_for = CASE WHEN excluded.checks = 'FAILURE' THEN ci_blocked_for END,
                others_reviewed = max(coalesce(others_reviewed, 0), excluded.others_reviewed),
                merge_state = excluded.merge_state, in_merge_queue = excluded.in_merge_queue,
                commit_oids = excluded.commit_oids,
                updated_at = excluded.updated_at, fetched_at = excluded.fetched_at, closed_at = NULL,
                threads_key = excluded.threads_key,
                ready_at = CASE WHEN excluded.draft THEN ready_at END,
                ready_by = CASE WHEN excluded.draft THEN ready_by END,
                draft_asked_at = CASE WHEN excluded.draft THEN draft_asked_at END
            """,
            (
                pr.ref, pr.repo, pr.number, pr.title, pr.url, int(pr.draft), pr.head, pr.head_oid, pr.base,
                pr.stack_parent, pr.loc, pr.checks, pr.requested, json.dumps(pr.approved_by), int(pr.bot_approved),
                int(pr.others_reviewed),
                pr.merge_state, int(pr.in_merge_queue), json.dumps(pr.commit_oids), pr.updated_at, now, now,
                map_pr_to_threads_key(pr), LOC_RULE, convert_reviews_to_text(pr.body_reviews), pr.depends_on,
                pr.usage_missing, int(pr.bot_cleared),
            ),
        )  # fmt: skip
        if not pr.open_threads:
            store_review_threads(conn, pr.ref, map_pr_to_threads_key(pr), ReviewThreads(0, ()))
        # Every commit, not only the head: two fixes between polls are two rounds.
        conn.executemany(
            "INSERT OR IGNORE INTO review_heads (ref, oid, committed_at) VALUES (?, ?, ?)",
            [(pr.ref, oid, at) for oid, at in pr.commits],
        )
    conn.execute(
        """
        INSERT INTO review_poll (id, ok_at) VALUES (1, ?)
        ON CONFLICT (id) DO UPDATE SET ok_at = excluded.ok_at, error = NULL, error_at = NULL
        """,
        (now,),
    )


def record_poll_error(conn: sqlite3.Connection, error: str, now: int) -> None:
    conn.execute(
        """
        INSERT INTO review_poll (id, error, error_at) VALUES (1, ?, ?)
        ON CONFLICT (id) DO UPDATE SET error = excluded.error, error_at = excluded.error_at
        """,
        (error, now),
    )


def list_missing_refs(conn: sqlite3.Connection, prs: list[OpenPr]) -> list[str]:
    seen = {pr.ref for pr in prs}
    return [
        str(r[0]) for r in conn.execute("SELECT ref FROM review_prs WHERE closed_at IS NULL") if str(r[0]) not in seen
    ]


def mark_closed(conn: sqlite3.Connection, refs: list[str], now: int) -> None:
    conn.executemany(
        "UPDATE review_prs SET closed_at = ? WHERE ref = ? AND closed_at IS NULL", [(now, r) for r in refs]
    )


def map_pr_to_threads_key(pr: OpenPr) -> str:
    """What changes when a thread does: a reply moves the PR's time, resolving or reopening one its flag."""
    return f"{pr.updated_at}|{pr.thread_flags}"


def list_refs_with_new_threads(conn: sqlite3.Connection) -> list[tuple[str, str, int, str]]:
    rows = conn.execute(
        """
        SELECT ref, repo, number, threads_key FROM review_prs
        WHERE closed_at IS NULL AND threads_key IS NOT NULL AND threads_key IS NOT threads_read_for
        """
    ).fetchall()
    return [(str(r["ref"]), str(r["repo"]), int(r["number"]), str(r["threads_key"])) for r in rows]


def store_review_threads(conn: sqlite3.Connection, ref: str, key: str, threads: ReviewThreads) -> None:
    conn.execute(
        """
        UPDATE review_prs SET open_threads = ?, his_open_threads = ?, his_threads = ?, threads_read_for = ?
        WHERE ref = ?
        """,
        (threads.open, len(threads.his), json.dumps([asdict(t) for t in threads.his]), key, ref),
    )


def list_refs_with_new_activity(conn: sqlite3.Connection) -> list[tuple[str, str, int, str]]:
    """Only PRs GitHub says changed: this job shares the account's API budget with every agent."""
    rows = conn.execute(
        """
        SELECT ref, repo, number, updated_at FROM review_prs
        WHERE closed_at IS NULL AND (comments_read_for IS NULL OR comments_read_for != updated_at)
        """
    ).fetchall()
    return [(str(r["ref"]), str(r["repo"]), int(r["number"]), str(r["updated_at"])) for r in rows]


def store_comments(
    conn: sqlite3.Connection, config: Config, ref: str, updated_at: str, comments: list[FetchedComment]
) -> None:
    for comment in comments:
        if is_worth_keeping(config, comment):
            conn.execute(
                """
                INSERT INTO review_comments (ref, comment_id, created_at, body, url, parent_id, parent_author, author,
                                             state, commit_id, review_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (ref, comment_id) DO UPDATE SET
                    author = coalesce(author, excluded.author), review_id = coalesce(review_id, excluded.review_id)
                """,
                (ref, comment.comment_id, comment.created_at, comment.body, comment.url, comment.parent_id,
                 comment.parent_author, comment.author, comment.state, comment.commit_id, comment.review_id),
            )  # fmt: skip
    conn.execute("UPDATE review_prs SET comments_read_for = ? WHERE ref = ?", (updated_at, ref))


def is_worth_keeping(config: Config, comment: FetchedComment) -> bool:
    """The owner's account's words and reviews; the review bot's comments and change requests, not its approvals; a
    person's words and verdicts. Apps such as CI are nobody's review."""
    if not comment.author or comment.state in ("DISMISSED", "PENDING"):
        return False
    if is_his_account(config, comment.author):
        return True
    # Theirs with no text and no verdict only wraps inline comments, which are read one by one.
    if comment.state == "COMMENTED" and not comment.body.strip():
        return False
    if is_review_bot(config, comment.author):
        return comment.state != APPROVED
    return not comment.by_app


def is_his_account(config: Config, login: str) -> bool:
    return login.casefold() == config.review_author.casefold()


def is_review_bot(config: Config, login: str) -> bool:
    return login.casefold() in {bot.casefold() for bot in config.review_bot_logins}


def map_command_to_comment_targets(command: str) -> list[tuple[str | None, int | None]]:
    """(repo, number) of each PR a shell command comments on under the account; None means it cannot be read."""
    command = command.replace("\\\n", " ")
    targets: list[tuple[str | None, int | None]] = []
    for match in _PR_CMD_RE.finditer(command):
        number = match.group(2) or match.group(4)
        targets.append((match.group(3), int(number) if number else None))
    for match in _API_WRITE_RE.finditer(command):
        text = match.group(0)
        # Editing or deleting a comment posts no new one.
        if not _API_WRITE_FLAG_RE.search(text) or _API_NOT_CREATE_RE.search(_convert_command_to_shell_words(text)):
            continue
        path = _REPO_PATH_RE.search(text)
        repo_only = _REPO_ONLY_RE.search(text)
        targets.append(
            (path.group(1), int(path.group(2))) if path else (repo_only.group(1) if repo_only else None, None)
        )
    if _GRAPHQL_WRITE_RE.search(command):
        targets.append((None, None))
    return list(dict.fromkeys(targets))


def record_agent_marks(conn: sqlite3.Connection, conversation_id: str, commands: list[tuple[str, int]]) -> None:
    for command, at in commands:
        for repo, number in map_command_to_comment_targets(command):
            conn.execute(
                "INSERT INTO review_agent_marks (at, conversation_id, repo, number, command) VALUES (?, ?, ?, ?, ?)",
                (at, conversation_id, repo, number, command[:MARK_COMMAND_CHARS]),
            )


def map_command_to_branch_writes(command: str) -> list[str | None]:
    """Branches a shell command commits or pushes to; None is the session's own branch. Every commit carries the
    same GitHub account, so a session's own git commands are what tells who wrote to a branch."""
    branches: list[str | None] = []
    for match in _GIT_WRITE_RE.finditer(command.replace("\\\n", " ")):
        args = match.group(3).split()
        if "--dry-run" in args or ("-n" in args and match.group(2) == "push"):
            continue
        positional = [arg for arg in args if not arg.startswith("-")]
        if match.group(2) == "push" and len(positional) >= 2:
            target = positional[-1].split(":")[-1].removeprefix("+").removeprefix("refs/heads/")
            branches.append(None if target in ("", "HEAD") else target)
        else:
            branches.append(None)
    return branches


def record_branch_writes(conn: sqlite3.Connection, conversation_id: str, commands: list[tuple[str, int]]) -> None:
    row = conn.execute(
        "SELECT branch FROM instances WHERE conversation_id = ? ORDER BY last_observed_at DESC LIMIT 1",
        (conversation_id,),
    ).fetchone()
    own = row["branch"] if row else None
    for command, at in commands:
        for branch in map_command_to_branch_writes(command):
            if branch or own:
                conn.execute(
                    """
                    INSERT INTO review_branch_writes (conversation_id, branch, at) VALUES (?, ?, ?)
                    ON CONFLICT (conversation_id, branch) DO UPDATE SET at = max(at, excluded.at)
                    """,
                    (conversation_id, branch or own, at),
                )


def record_host_pushes(
    conn: sqlite3.Connection, config: Config, conversation_id: str, pushes: list[tuple[str, str]], at: int
) -> None:
    """A host worker's transcript stays on its host: the push the guard passed is what tells bridge it wrote there.
    Only pushes to the session's own repo count, as the table names a branch that find_owner reads as in that repo."""
    row = conn.execute(
        "SELECT repo FROM instances WHERE conversation_id = ? ORDER BY last_observed_at DESC LIMIT 1",
        (conversation_id,),
    ).fetchone()
    own = {str(r).lower() for r in (row["repo"], config.pr_repo_for(row["repo"])) if r} if row else set[str]()
    for repo, branch in pushes:
        if repo.lower() not in own:
            continue
        conn.execute(
            """
            INSERT INTO review_branch_writes (conversation_id, branch, at) VALUES (?, ?, ?)
            ON CONFLICT (conversation_id, branch) DO UPDATE SET at = max(at, excluded.at)
            """,
            (conversation_id, branch, at),
        )


def load_marks(conn: sqlite3.Connection, repo: str, number: int) -> list[Mark]:
    rows = conn.execute(
        """
        SELECT at, command FROM review_agent_marks
        WHERE (repo IS NULL OR lower(repo) = lower(?)) AND (number IS NULL OR number = ?)
        """,
        (repo, number),
    ).fetchall()
    return [Mark(int(r["at"]), str(r["command"])) for r in rows]


def _convert_command_to_shell_words(command: str) -> str:
    """Drops quoted prose, such as a comment's body, and keeps a quoted word or a script handed to bash -c or eval."""

    def keep(match: re.Match[str]) -> str:
        inner = match.group(0)[1:-1]
        if re.fullmatch(r"[\w.-]*", inner) or _SHELL_RUNS_NEXT_RE.search(command[: match.start()]):
            return inner
        return "''"

    return _QUOTED_RE.sub(keep, command)


def _convert_text_to_comparable(text: str) -> str:
    return "".join(ch for ch in text if ch not in "\\\"'`" and not ch.isspace())


def classify_comment(
    created_at: int,
    body: str,
    parent_id: str | None,
    parent_author: str | None,
    parent_verdict: str | None,
    author: str,
    marks: list[Mark],
    remote_active: str | None,
    prefix_from_ms: int = 0,
) -> tuple[str, str | None]:
    """Whose words these are, under an account the agents share. Agents start what they post with the prefix, and the
    owner never does, so since that rule the prefix decides. Before it, the sessions' commands were the only clue, and
    every doubt landed short of a wake: a false "owner's" would wake a worker for its own reply."""
    if body.lstrip().startswith(AGENT_PREFIX):
        return AGENT, f"it starts with {AGENT_PREFIX}"
    if created_at >= prefix_from_ms:
        return HIS, None
    wanted = _convert_text_to_comparable(body)[:BODY_PREFIX_CHARS]
    near_visible = False
    for mark in marks:
        age = created_at - mark.at
        if age < -CLOCK_SKEW_MS:
            continue
        # A mark dates when the command began, and it may have waited on CI before posting.
        if wanted and wanted in _convert_text_to_comparable(mark.command):
            return AGENT, "a session's command posted this text"
        if age <= OPAQUE_MARK_WINDOW_MS and _OPAQUE_BODY_RE.search(mark.command):
            return AGENT, "a session posted a comment here at that time"
        # Only the shell can wait: "until" inside a comment's text is prose.
        waited = age <= WAITING_COMMAND_WINDOW_MS and _WAITING_COMMAND_RE.search(
            _convert_command_to_shell_words(mark.command)
        )
        if age <= VISIBLE_MARK_WINDOW_MS or waited:
            near_visible = True
    if near_visible:
        return UNCLEAR, "a session posted a comment here at that time"
    if parent_id is not None and (parent_author != author or parent_verdict != HIS):
        return UNCLEAR, "a reply on a thread the owner did not start; agents answer those under the owner's account"
    if remote_active:
        return UNCLEAR, f"a {remote_active} session was active on this PR; bridge cannot see its commands"
    return HIS, None


def find_remote_active(conn: sqlite3.Connection, ref: str, head: str, at: int) -> str | None:
    """The host of a remote session active on the PR around that time."""
    for row in conn.execute(
        """
        SELECT s.host, s.data, s.activity_at, EXISTS (
            SELECT 1 FROM remote_refs r WHERE r.host = s.host AND r.key = s.key AND r.ref = ?) AS on_ref
        FROM remote_sessions s
        """,
        (ref,),
    ):
        activity = row["activity_at"]
        if activity is None or abs(at - int(activity)) > REMOTE_ACTIVE_WINDOW_MS:
            continue
        if row["on_ref"] or f'"branch": {json.dumps(head)}' in str(row["data"]):
            return str(row["host"])
    return None


def classify_pending_comments(conn: sqlite3.Connection, config: Config, scanned_from: int) -> None:
    """A comment waits until a transcript scan that began after it has run, or its session's mark could still be
    unread. Without any scan there is no verdict and no wake."""
    rows = conn.execute(
        """
        SELECT c.ref, c.comment_id, c.created_at, c.body, c.parent_id, c.parent_author, c.author, c.state,
               c.commit_id, c.quiet, p.repo, p.number, p.head, p.first_seen_at, p.others_from, p.approved_by
        FROM review_comments c JOIN review_prs p ON p.ref = c.ref
        WHERE c.verdict IS NULL AND c.created_at + ? < ?
        ORDER BY c.created_at
        """,
        (SETTLE_MS, scanned_from),
    ).fetchall()
    for row in rows:
        ref = str(row["ref"])
        created_at = int(row["created_at"])
        if row["author"] is not None and not is_his_account(config, str(row["author"])):
            verdict, reason = classify_others_comment(conn, config, row)
        elif created_at < int(row["first_seen_at"]):
            verdict, reason = OLD, None
        elif row["state"] is not None and not str(row["body"]).strip():
            found = classify_review_by_its_comments(conn, ref, str(row["comment_id"]))
            if found is None:
                continue
            verdict, reason = found
        else:
            parent = conn.execute(
                "SELECT verdict FROM review_comments WHERE ref = ? AND comment_id = ?", (ref, row["parent_id"])
            ).fetchone()
            verdict, reason = classify_comment(
                created_at,
                str(row["body"]),
                row["parent_id"],
                row["parent_author"],
                parent["verdict"] if parent else None,
                config.review_author,
                load_marks(conn, str(row["repo"]), int(row["number"])),
                find_remote_active(conn, ref, str(row["head"]), created_at),
                config.agent_prefix_from_ms,
            )
        # Reread once after the marking fix: what turns out the owner's is older than the fix, and an agent answered it.
        told_at = created_at if row["quiet"] and verdict in _WAKING else None
        owner_asked_at = created_at if row["quiet"] and verdict == UNCLEAR else None
        conn.execute(
            """
            UPDATE review_comments SET verdict = ?, reason = ?, told_at = ?, owner_asked_at = ?
            WHERE ref = ? AND comment_id = ?
            """,
            (verdict, reason, told_at, owner_asked_at, ref, row["comment_id"]),
        )
    record_owner_reviews(conn, config)


def record_owner_reviews(conn: sqlite3.Connection, config: Config) -> None:
    """His own words on GitHub are his review, as much as one sent from the page: when his latest came. Read from all
    his comments each time, so those classified before bridge kept this count too."""
    conn.execute(
        """
        UPDATE review_prs SET owner_reviewed_by = ?, owner_reviewed_at = (
            SELECT max(created_at) FROM review_comments c WHERE c.ref = review_prs.ref AND c.verdict = ?)
        WHERE closed_at IS NULL AND ref IN (SELECT ref FROM review_comments WHERE verdict = ?)
        """,
        (config.review_author, HIS, HIS),
    )


def classify_review_by_its_comments(
    conn: sqlite3.Connection, ref: str, review_id: str
) -> tuple[str, str | None] | None:
    """A review with no text of its own is whoever wrote its inline comments; a reply on GitHub makes one too. None
    while any of them waits for a verdict."""
    verdicts = [
        r[0]
        for r in conn.execute("SELECT verdict FROM review_comments WHERE ref = ? AND review_id = ?", (ref, review_id))
    ]
    if not verdicts:
        return OLD, "a review with no text and no comments"
    if None in verdicts:
        return None
    if HIS in verdicts:
        return HIS, None
    if all(v in (AGENT, OLD) for v in verdicts):
        return AGENT, "its comments are an agent's"
    return UNCLEAR, "its comments are unclear"


def classify_others_comment(conn: sqlite3.Connection, config: Config, row: sqlite3.Row) -> tuple[str, str | None]:
    """A colleague's or the review bot's. Only what came after bridge began reading them wakes anyone, except a
    colleague's standing APPROVED verdict: an approved PR waits on its owner however old that verdict is."""
    author = str(row["author"])
    standing = row["state"] == APPROVED and author in json.loads(str(row["approved_by"] or "[]"))
    read_from = max(int(row["first_seen_at"]), int(row["others_from"] or 0))
    if int(row["created_at"]) < read_from and not standing:
        return OLD, None
    if row["state"] in (APPROVED, CHANGES_REQUESTED):
        told = conn.execute(
            """
            SELECT 1 FROM review_comments WHERE ref = ? AND author = ? AND state = ? AND commit_id IS ?
            AND comment_id != ? AND verdict IN (?, ?)
            """,
            (row["ref"], author, row["state"], row["commit_id"], row["comment_id"], COLLEAGUE, REVIEWER),
        ).fetchone()
        if told:
            return OLD, "the same verdict on the same head already woke the owner"
    return (REVIEWER if is_review_bot(config, author) else COLLEAGUE), None


def take_comment_wakes(conn: sqlite3.Connection, config: Config, now: int) -> list[tuple[int, Recipient]]:
    """New comments and verdicts per PR, in one message to its owner. With no owner they wait for one."""
    sent: list[tuple[int, Recipient]] = []
    marks = ", ".join("?" * len(_WAKING))
    refs = conn.execute(
        f"""
        SELECT DISTINCT c.ref FROM review_comments c JOIN review_prs p ON p.ref = c.ref
        WHERE c.verdict IN ({marks}) AND c.told_at IS NULL AND p.closed_at IS NULL
        """,
        _WAKING,
    ).fetchall()
    for ref_row in refs:
        ref = str(ref_row[0])
        recent = conn.execute(
            """
            SELECT count(DISTINCT told_at) FROM review_comments
            WHERE ref = ? AND told_at > ? AND quiet IS NULL
              AND NOT (verdict = ? AND state IS NOT NULL AND trim(body) = '')
            """,
            (ref, now - HOUR_MS, HIS),
        ).fetchone()[0]
        if int(recent) >= WAKES_PER_HOUR_MAX:
            # A review bot answering every push ends here. The owner's and colleagues' words are what this is for, so
            # they wait for the hour to pass and go out in one message; a misread agent reply then wakes at most 3 an
            # hour.
            conn.execute(
                "UPDATE review_comments SET verdict = ?, reason = ? WHERE ref = ? AND verdict = ? AND told_at IS NULL",
                (UNCLEAR, f"more than {WAKES_PER_HOUR_MAX} wakes on this PR in an hour", ref, REVIEWER),
            )
            continue
        pr = conn.execute("SELECT * FROM review_prs WHERE ref = ?", (ref,)).fetchone()
        owner = find_owner(conn, config, ref, str(pr["repo"]), str(pr["head"]))
        if owner is None:
            continue
        rows = conn.execute(
            f"""
            SELECT comment_id, verdict, author, state, body, url FROM review_comments
            WHERE ref = ? AND verdict IN ({marks}) AND told_at IS NULL ORDER BY created_at
            """,
            (ref, *_WAKING),
        ).fetchall()
        approvers = set(json.loads(str(pr["approved_by"] or "[]")))
        withdrawn = [r["comment_id"] for r in rows if r["state"] == APPROVED and r["author"] not in approvers]
        conn.executemany(
            "UPDATE review_comments SET verdict = ?, reason = ? WHERE ref = ? AND comment_id = ?",
            [(OLD, "no longer approved", ref, comment_id) for comment_id in withdrawn],
        )
        rows = [r for r in rows if r["comment_id"] not in withdrawn]
        # The owner's review with no text only wraps comments already in the message, or told before.
        silent = [r for r in rows if r["verdict"] == HIS and r["state"] is not None and not str(r["body"]).strip()]
        conn.executemany(
            "UPDATE review_comments SET told_at = ? WHERE ref = ? AND comment_id = ?",
            [(now, ref, r["comment_id"]) for r in silent],
        )
        rows = [r for r in rows if r not in silent]
        if not rows:
            continue
        items = [
            WakeItem(str(r["verdict"]), str(r["author"] or ""), r["state"], str(r["url"]), str(r["body"])) for r in rows
        ]
        wake = map_items_to_wake(str(pr["url"]), str(pr["title"]), items, config)
        sent.append(tell_owner(conn, owner, wake, now))
        conn.executemany(
            """
            INSERT INTO colleague_calls (ref, comment_id, owner_handle, owner_lead) VALUES (?, ?, ?, ?)
            ON CONFLICT (ref, comment_id) DO UPDATE SET owner_handle = excluded.owner_handle,
                                                        owner_lead = excluded.owner_lead
            """,
            [(ref, r["comment_id"], owner.handle, owner.lead) for r in rows if r["verdict"] == COLLEAGUE],
        )
        conn.executemany(
            "UPDATE review_comments SET told_at = ? WHERE ref = ? AND comment_id = ?",
            [(now, ref, r["comment_id"]) for r in rows],
        )
    return [*sent, *ask_owners_about_unplaced(conn, config, now)]


def ask_owners_about_unplaced(conn: sqlite3.Connection, config: Config, now: int) -> list[tuple[int, Recipient]]:
    """What bridge cannot place under the owner's account is not shown to them: its owner looks, once per comment."""
    sent: list[tuple[int, Recipient]] = []
    rows = conn.execute(
        """
        SELECT c.ref, c.comment_id, c.author, c.body, c.url FROM review_comments c JOIN review_prs p ON p.ref = c.ref
        WHERE c.verdict = ? AND c.owner_asked_at IS NULL AND p.closed_at IS NULL ORDER BY c.created_at
        """,
        (UNCLEAR,),
    ).fetchall()
    by_ref: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        if row["author"] is None or is_his_account(config, str(row["author"])):
            by_ref.setdefault(str(row["ref"]), []).append(row)
    for ref, comments in by_ref.items():
        pr = conn.execute("SELECT * FROM review_prs WHERE ref = ?", (ref,)).fetchone()
        owner = find_owner(conn, config, ref, str(pr["repo"]), str(pr["head"]))
        if owner is None:
            continue
        head = (
            f"On {pr['url']} ({pr['title']}) bridge cannot tell whether these comments under {config.owner_name}'s "
            f"account are {config.owner_name}'s or an agent's, so {config.owner_name} does not see them. Check each: "
            f"if it is {config.owner_name}'s, act on it; if a session of ours wrote it, nothing to do."
        )
        items = [WakeItem(HIS, "", None, str(c["url"]), str(c["body"])) for c in comments]
        sent.append(tell_owner(conn, owner, map_items_to_text(head, str(pr["url"]), items), now))
        conn.executemany(
            "UPDATE review_comments SET owner_asked_at = ? WHERE ref = ? AND comment_id = ?",
            [(now, ref, c["comment_id"]) for c in comments],
        )
    return sent


def map_items_to_wake(url: str, title: str, items: list[WakeItem], config: Config) -> str:
    others = [i for i in items if i.verdict != HIS]
    approvers = sorted({i.author for i in others if i.state == APPROVED})
    requesters = sorted({_map_item_to_name(i) for i in others if i.state == CHANGES_REQUESTED})
    commenters = sorted({_map_item_to_name(i) for i in others if i.state not in (APPROVED, CHANGES_REQUESTED)})
    bot_words = any(i.verdict == REVIEWER and i.state != APPROVED for i in others)
    colleague_words = [i for i in others if i.verdict == COLLEAGUE and (i.state != APPROVED or i.body.strip())]
    colleague_requesters = sorted({i.author for i in colleague_words if i.state == CHANGES_REQUESTED})
    lines = [f"New on {url} ({title}):"]
    if any(i.verdict == HIS for i in items):
        lines.append(
            f"{config.owner_name_at_start} commented. Read it, fix what it asks and push; bridge shows the fixes on "
            f"{config.owner_name}'s queue."
        )
    if requesters:
        lines.append(f"{', '.join(requesters)} requested changes.")
    if commenters:
        lines.append(f"{', '.join(commenters)} commented.")
    if bot_words:
        lines.append(
            "Fix what this PR introduced, defer the rest with a reason in a reply, and ask "
            f"{config.owner_name} only for decisions."
        )
    if colleague_words:
        lines.append(map_config_to_colleague_rule(config))
    if colleague_requesters:
        lines.append(
            f"{', '.join(colleague_requesters)} requested changes: substantive, so reply and push for it are held "
            f"until {config.owner_name} chooses."
        )
    if approvers:
        rule = read_overlay(config, "merge-rule", "the merge rule")
        lines.append(f"{', '.join(approvers)} approved: apply {rule}.")
    lines.append(
        f"Each comment or description body you post on GitHub starts with '{AGENT_PREFIX}' (a title never does). "
        "Run gh in plain sight in a Bash command, not "
        f"from a script, so bridge can tell your words from {config.owner_name}'s."
    )
    return map_items_to_text(" ".join(lines), url, items)


def map_config_to_colleague_rule(config: Config) -> str:
    owner, start = config.owner_name, config.owner_name_at_start
    return (
        f"{start} decides what we say to colleagues. Each colleague comment is yours to call: a small one (a typo, a "
        'rename, a factual question) you handle, then run `guild comment small <link> "what you did"`. A substantive '
        "one (a design change, changes requested, anything you would decline or argue with, anything that grows "
        f"scope; unsure = substantive) gets no reply and no push until {owner} chooses: run "
        f'`guild comment draft <link> --reply-file <path> --plan "…"`. {start} sees the comment, your draft and plan, '
        "and picks Send as is, Edit or I'll answer myself; bridge posts the reply after that and tells you. A draft "
        "says what happens to the PR (merge, keep, close), not only the facts. The page "
        f"shows {owner} each call."
    )


def map_items_to_text(head: str, url: str, items: list[WakeItem]) -> str:
    """Fits one message: as many links as fit, the rest counted, and each shown text gets an equal share of what is
    left. A message too long to send would fail every poll's wakes."""
    more = f"\n\n…and {len(items)} more on {url}"
    budget = BODY_MAX_CHARS - len(head) - len(more) - 2
    shown: list[tuple[str, WakeItem]] = []
    for item in items:
        label = item.url if item.verdict == HIS else f"{_map_item_to_name(item)}: {item.url}"
        if sum(len(lbl) + 7 for lbl, _ in shown) + len(label) + 7 > budget:
            break
        shown.append((label, item))
    share = max((budget - sum(len(lbl) + 7 for lbl, _ in shown)) // max(len(shown), 1), 0)
    blocks = [f"{label}\n{item.body[:share]}" + ("…" if len(item.body) > share else "") for label, item in shown]
    hidden = len(items) - len(shown)
    return f"{head}\n\n" + "\n\n".join(blocks) + (f"\n\n…and {hidden} more on {url}" if hidden else "")


def _map_item_to_name(item: WakeItem) -> str:
    return f"{item.author} (review bot)" if item.verdict == REVIEWER else item.author


def tell_owner(conn: sqlite3.Connection, owner: Owner, text: str, now: int) -> tuple[int, Recipient]:
    recipient = resolve_tab(conn, owner.conversation_id, owner.handle)
    result = send_message(conn, Party(SYSTEM_CONVERSATION, "bridge"), [recipient], "info", text, now)
    conn.execute("UPDATE messages SET post_type = ? WHERE id = ?", (REVIEW_QUEUE, result.message_id))
    return result.message_id, recipient


def prune_marks(conn: sqlite3.Connection, now: int) -> None:
    conn.execute("DELETE FROM review_agent_marks WHERE at < ?", (now - MARKS_KEPT_MS,))
    conn.execute("DELETE FROM review_branch_writes WHERE at < ?", (now - BRANCH_WRITES_KEPT_MS,))


def record_tab_parents(conn: sqlite3.Connection, tree: dict[str, Any] | None) -> None:
    """A worker's lead must stay known after its tab is gone from agterm's tree."""
    workspaces: Any = (tree or {}).get("workspaces") or []
    for workspace in workspaces:
        tabs: Any = workspace.get("sessions") or []
        for tab in tabs:
            if tab.get("parent"):
                record_tab_parent(conn, str(tab.get("id")), str(tab["parent"]))


def find_lead_of(conn: sqlite3.Connection, surface: str | None) -> tuple[str | None, str | None]:
    """(display name, surface) of the lead above a tab, itself included."""
    for _ in range(PARENT_WALK_MAX):
        if surface is None:
            return None, None
        lead = find_lead_on_surface(conn, surface)
        if lead is not None:
            return lead.display_name, lead.surface
        row = conn.execute(
            """
            SELECT parent.surface FROM surfaces s
            JOIN tab_parents t ON t.agterm_session = s.agterm_session
            JOIN surfaces parent ON parent.agterm_session = t.parent_session
            WHERE s.surface = ?
            """,
            (surface,),
        ).fetchone()
        surface = row[0] if row else None
    return None, None


def find_owner(
    conn: sqlite3.Connection, config: Config, ref: str, repo: str, head: str, owned_only: bool = False
) -> Owner | None:
    """Who every notice about the PR goes to. The session that owns it: the live one holding its claim, a lead whose
    declared work names the PR, the live one that committed or pushed to its branch (on it first, else the latest
    writer), an ended one that did. Only when no session owns it, and not owned_only: a lead whose work or scope names
    its issue, then an ended session that sat on its branch and named it, the live session that mentioned it last, an
    ended one. A live worker keeps its PR's notices, since it fixes what they ask; an ended one hands them to the lead
    that ran it. A locked tab is never an owner, and neither is the assistant, who mentions every PR and owns none."""

    def may_own(row: sqlite3.Row) -> bool:
        return row["handle"] != config.assistant_handle and not is_conversation_locked(
            conn, config, str(row["conversation_id"])
        )

    def from_session(row: sqlite3.Row, via: str) -> Owner:
        display = find_lead_of(conn, row["surface"])[0]
        return Owner(str(row["handle"]), str(row["conversation_id"]), row["surface"], display, via)

    claimed = _find_owner_from_claim(conn, ref, may_own)
    if claimed is not None:
        return from_session(*claimed)
    declared = _find_lead_from_work(conn, config, ref, head, by_issue=False)
    if declared is not None:
        return declared
    for row in conn.execute(
        """
        SELECT i.conversation_id, i.surface, i.repo, s.handle FROM review_branch_writes w
        JOIN instances i ON i.conversation_id = w.conversation_id AND i.ended_at IS NULL
        JOIN surfaces s ON s.surface = i.surface
        WHERE w.branch = ?1 AND s.handle IS NOT NULL
        ORDER BY i.branch IS ?1 DESC, w.at DESC, coalesce(i.last_activity_at, 0) DESC
        """,
        (head,),
    ):
        if config.pr_repo_for(row["repo"]) == repo and may_own(row):
            return from_session(row, "branch")
    ended = _find_lead_of_ended_writer(conn, config, ref, repo, head, wrote=True)
    if ended is not None or owned_only:
        return ended
    by_issue = _find_lead_from_work(conn, config, ref, head, by_issue=True)
    if by_issue is not None:
        return by_issue
    on_branch = _find_lead_of_ended_writer(conn, config, ref, repo, head, wrote=False)
    if on_branch is not None:
        return on_branch
    for row in conn.execute(
        """
        SELECT i.conversation_id, i.surface, s.handle FROM refs r
        JOIN instances i ON i.conversation_id = r.conversation_id AND i.ended_at IS NULL
        JOIN surfaces s ON s.surface = i.surface
        WHERE r.ref = ? AND s.handle IS NOT NULL
        ORDER BY r.last_seen_at DESC
        """,
        (ref,),
    ):
        if may_own(row):
            return from_session(row, "ref")
    for row in conn.execute(
        """
        SELECT i.surface, r.conversation_id FROM refs r
        JOIN conversations c ON c.conversation_id = r.conversation_id
        JOIN instances i ON i.instance_id = c.instance_id
        WHERE r.ref = ? AND i.surface IS NOT NULL
        ORDER BY r.last_seen_at DESC
        """,
        (ref,),
    ):
        if row["conversation_id"] and is_conversation_locked(conn, config, str(row["conversation_id"])):
            continue
        display, lead_surface = find_lead_of_ended(conn, row["surface"], row["conversation_id"])
        if lead_surface is None:
            continue
        owner = map_lead_to_owner(conn, config, lead_surface, display)
        if owner is not None:
            return owner
    return None


def find_last_pusher(conn: sqlite3.Connection, config: Config, repo: str, head: str) -> Owner | None:
    """The session that last committed or pushed to the PR's branch while it is live, its lead once it has ended. Only
    the latest writer in the PR's repo counts: an earlier one may have handed the branch on, so when the latest maps
    to no one the caller falls back to the PR's owner."""
    rows = conn.execute(
        """
        SELECT i.conversation_id, i.surface, i.repo, i.ended_at, s.handle FROM review_branch_writes w
        JOIN instances i ON i.instance_id = (
            SELECT instance_id FROM instances WHERE conversation_id = w.conversation_id
            ORDER BY last_observed_at DESC LIMIT 1
        )
        LEFT JOIN surfaces s ON s.surface = i.surface
        WHERE w.branch = ? ORDER BY w.at DESC
        """,
        (head,),
    ).fetchall()
    row = next((r for r in rows if r["repo"] is None or config.pr_repo_for(r["repo"]) == repo), None)
    if row is None:
        return None
    if is_conversation_locked(conn, config, str(row["conversation_id"])):
        return None
    if row["ended_at"] is None:
        if not row["handle"] or row["handle"] == config.assistant_handle:
            return None
        display = find_lead_of(conn, row["surface"])[0]
        return Owner(str(row["handle"]), str(row["conversation_id"]), row["surface"], display, "pushed")
    display, lead_surface = find_lead_of_ended(conn, row["surface"], str(row["conversation_id"]))
    return map_lead_to_owner(conn, config, lead_surface, display) if lead_surface else None


def _find_lead_of_ended_writer(
    conn: sqlite3.Connection, config: Config, ref: str, repo: str, head: str, wrote: bool
) -> Owner | None:
    """By its lead, an ended session that wrote to the PR's branch; else (not wrote) one that sat on the branch and
    named the PR, which is less: a reviewer does that too."""
    query = (
        """
        SELECT i.conversation_id, i.surface, i.repo FROM review_branch_writes w
        JOIN instances i ON i.conversation_id = w.conversation_id
        WHERE w.branch = ?1 AND i.ended_at IS NOT NULL AND i.surface IS NOT NULL
        ORDER BY w.at DESC, coalesce(i.last_activity_at, i.last_observed_at, 0) DESC
        """
        if wrote
        else """
        SELECT i.conversation_id, i.surface, i.repo FROM refs r
        JOIN instances i ON i.conversation_id = r.conversation_id
        WHERE r.ref = ?2 AND i.branch = ?1 AND i.ended_at IS NOT NULL AND i.surface IS NOT NULL
        ORDER BY coalesce(i.last_activity_at, i.last_observed_at, 0) DESC
        """
    )
    for row in conn.execute(query, (head,) if wrote else (head, ref)):
        if row["repo"] is not None and config.pr_repo_for(row["repo"]) != repo:
            continue
        if is_conversation_locked(conn, config, str(row["conversation_id"])):
            continue
        display, lead_surface = find_lead_of_ended(conn, row["surface"], str(row["conversation_id"]))
        owner = map_lead_to_owner(conn, config, lead_surface, display) if lead_surface else None
        if owner is not None:
            return owner
    return None


def find_owner_of_ref(
    conn: sqlite3.Connection, config: Config, ref: str, github_head: str, owned_only: bool = False
) -> Owner | None:
    """find_owner for a PR known by its ref alone: the queue's row names its repo and branch, else GitHub's head."""
    row = conn.execute("SELECT ref, repo, head FROM review_prs WHERE lower(ref) = lower(?)", (ref,)).fetchone()
    if row is not None:
        return find_owner(conn, config, str(row["ref"]), str(row["repo"]), str(row["head"]), owned_only)
    return find_owner(conn, config, ref, ref.removeprefix("pr:").partition("#")[0], github_head, owned_only)


def find_lead_of_ended(
    conn: sqlite3.Connection, surface: str | None, conversation_id: str | None
) -> tuple[str | None, str | None]:
    """As find_lead_of; a closed worker's tab and its parent link are gone, but its close recorded the lead."""
    display, lead_surface = find_lead_of(conn, surface)
    if lead_surface is not None or conversation_id is None:
        return display, lead_surface
    row = conn.execute(
        """
        SELECT l.display_name, l.surface FROM closed_workers c JOIN leads l ON l.name = c.parent_lead
        WHERE c.conversation_id = ? AND l.retired_at IS NULL ORDER BY c.closed_at DESC LIMIT 1
        """,
        (conversation_id,),
    ).fetchone()
    return (str(row["display_name"]), row["surface"]) if row is not None else (None, None)


def find_issue_lead(
    conn: sqlite3.Connection,
    config: Config,
    issue_ref: str,
    linked: list[str],
    closing: list[str],
    enrichment: Mapping[str, Mapping[str, object]],
) -> Owner | None:
    """The lead an issue's notices go to, in a PR's owner order: the lead above the live session holding the issue's
    claim; a lead whose declared work names the issue or one of its linked PRs; the lead above a live session on a
    branch named after the issue, else above the owner of a closing PR; the lead of an ended session on such a branch;
    a lead whose scope names the issue. Only a lead can act on an issue, so a worker hands it up. Naming an issue in a
    prompt or a message makes no one its owner. None when no live lead is found."""
    key = issue_ref.removeprefix("issue:")
    wanted = {r.lower() for r in (issue_ref, *linked)}
    leads = list_leads(conn)

    def lead_above(surface: str | None, conversation_id: str | None = None) -> Owner | None:
        display, lead_surface = find_lead_of_ended(conn, surface, conversation_id)
        return map_lead_to_owner(conn, config, lead_surface, display) if lead_surface else None

    def may_own(row: sqlite3.Row) -> bool:
        return row["handle"] != config.assistant_handle and not is_conversation_locked(
            conn, config, str(row["conversation_id"])
        )

    claimed = _find_owner_from_claim(conn, issue_ref, may_own)
    if claimed is not None and (owner := lead_above(claimed[0]["surface"])) is not None:
        return owner
    by_name = {lead.name: lead for lead in leads}
    for chunk in conn.execute("SELECT lead, refs FROM lead_work WHERE done_at IS NULL ORDER BY updated_at DESC"):
        lead = by_name.get(str(chunk["lead"]))
        if lead is None or not wanted & {str(r).lower() for r in json.loads(str(chunk["refs"]))}:
            continue
        owner = map_lead_to_owner(conn, config, lead.surface, lead.display_name)
        if owner is not None:
            return owner
    on_branch = _list_sessions_on_issue_branch(conn, key)
    for row in on_branch:
        if row["ended_at"] is None and may_own(row) and (owner := lead_above(row["surface"])) is not None:
            return owner
    for ref in closing:
        head = str((enrichment.get(ref) or {}).get("head") or "")
        found = find_owner_of_ref(conn, config, ref, head, owned_only=True)
        owner = lead_above(found.surface) if found else None
        if owner is not None:
            return owner
    for row in on_branch:
        if row["ended_at"] is None or is_conversation_locked(conn, config, str(row["conversation_id"])):
            continue
        if (owner := lead_above(row["surface"], str(row["conversation_id"]))) is not None:
            return owner
    for lead in leads:
        if key.upper() in _ISSUE_KEY_RE.findall(f"{lead.scope} {lead.linear_refs}".upper()):
            owner = map_lead_to_owner(conn, config, lead.surface, lead.display_name)
            if owner is not None:
                return owner
    return None


def _list_sessions_on_issue_branch(conn: sqlite3.Connection, key: str) -> list[sqlite3.Row]:
    """Sessions whose branch names the whole issue key (PROJ-1 is not PROJ-10), live first, the latest active first."""
    pattern = re.compile(rf"(?<![a-z0-9]){re.escape(key.lower())}(?![0-9])")
    rows = conn.execute(
        """
        SELECT i.conversation_id, i.surface, i.branch, i.ended_at, s.handle FROM instances i
        LEFT JOIN surfaces s ON s.surface = i.surface
        WHERE instr(lower(i.branch), ?) > 0 AND i.surface IS NOT NULL AND i.conversation_id IS NOT NULL
        ORDER BY i.ended_at IS NOT NULL, coalesce(i.last_activity_at, i.last_observed_at, 0) DESC
        """,
        (key.lower(),),
    )
    return [r for r in rows if pattern.search(str(r["branch"]).lower())]


def _find_owner_from_claim(
    conn: sqlite3.Connection, ref: str, may_own: Callable[[sqlite3.Row], bool]
) -> tuple[sqlite3.Row, str] | None:
    """A live session holding the PR's claim is working on it right now, whatever the branch name says."""
    for row in conn.execute(
        """
        SELECT i.conversation_id, i.surface, s.handle FROM claims c
        JOIN instances i ON i.conversation_id = c.holder_conversation AND i.ended_at IS NULL
        JOIN surfaces s ON s.surface = i.surface
        WHERE lower(c.resource) = lower(?) AND s.handle IS NOT NULL
        ORDER BY c.renewed_at DESC
        """,
        (ref,),
    ):
        if may_own(row):
            return row, "claim"
    return None


def _find_lead_from_work(conn: sqlite3.Connection, config: Config, ref: str, head: str, by_issue: bool) -> Owner | None:
    """The lead whose live work names the PR; by_issue, the lead whose work, else scope, names an issue its title or
    branch names."""
    row = conn.execute("SELECT title FROM review_prs WHERE ref = ?", (ref,)).fetchone()
    keys = set(_ISSUE_KEY_RE.findall(f"{row['title'] if row else ''} {head}".upper())) if by_issue else set[str]()
    wanted = {f"issue:{key}" for key in keys} if by_issue else {ref}
    # Of two leads whose work names the same PR or issue, the one that declared it last is the one on it now.
    named: dict[str, int] = {}
    for chunk in conn.execute("SELECT lead, refs, updated_at FROM lead_work WHERE done_at IS NULL"):
        if wanted & {str(r) for r in json.loads(str(chunk["refs"]))}:
            lead_name, updated = str(chunk["lead"]), int(chunk["updated_at"] or 0)
            named[lead_name] = max(named.get(lead_name, 0), updated)
    leads = list_leads(conn)
    matches = [sorted((lead for lead in leads if lead.name in named), key=lambda lead: -named[lead.name])]
    if by_issue:
        matches.append(
            [lead for lead in leads if keys & set(_ISSUE_KEY_RE.findall(f"{lead.scope} {lead.linear_refs}".upper()))]
        )
    for found in matches:
        for lead in found:
            owner = map_lead_to_owner(conn, config, lead.surface, lead.display_name)
            if owner is not None:
                return owner
    return None


def map_lead_to_owner(
    conn: sqlite3.Connection, config: Config, surface: str | None, display: str | None
) -> Owner | None:
    live = conn.execute(
        """
        SELECT i.conversation_id, s.handle FROM instances i JOIN surfaces s ON s.surface = i.surface
        WHERE i.surface = ? AND i.ended_at IS NULL AND i.conversation_id IS NOT NULL AND s.handle IS NOT NULL
        ORDER BY i.proc_start_ms DESC LIMIT 1
        """,
        (surface,),
    ).fetchone()
    if live is None or live["handle"] == config.assistant_handle:
        return None
    if is_conversation_locked(conn, config, str(live["conversation_id"])):
        return None
    return Owner(str(live["handle"]), str(live["conversation_id"]), surface, display, "lead")


def map_events_to_rounds(comment_times: list[int], head_times: list[int]) -> tuple[int, bool]:
    """(finished rounds, comments still open). A head committed before the owner's comment is not a fix for it, whenever
    it was pushed."""
    events = sorted([(t, 0) for t in comment_times] + [(t, 1) for t in head_times])
    rounds = 0
    open_comments = False
    for _, is_head in events:
        if not is_head:
            open_comments = True
        elif open_comments:
            rounds += 1
            open_comments = False
    return rounds, open_comments


def load_known_loc(conn: sqlite3.Connection) -> dict[tuple[str, str], int]:
    """Counts made under an older rule are made again, even for the same head."""
    return {
        (str(r["ref"]), str(r["head_oid"])): int(r["loc"])
        for r in conn.execute("SELECT ref, head_oid, loc FROM review_prs WHERE loc_rule = ?", (LOC_RULE,))
    }


def poll_review_queue(
    conn: sqlite3.Connection, config: Config, now: int, tree: dict[str, Any] | None, scanned_from: int | None
) -> list[tuple[int, Recipient]]:
    """One round of the background job. GitHub is read with no transaction open; returns the wakes to deliver."""
    try:
        prs = fetch_open_prs(config, load_known_loc(conn))
    except GitHubError as exc:
        with transaction(conn):
            record_poll_error(conn, str(exc), now)
        prs = None
    if prs is not None:
        excluded = {r.casefold() for r in config.review_excluded_repos}
        prs = [pr for pr in prs if pr.repo.casefold() not in excluded]
        if excluded:
            with transaction(conn):
                drop_excluded_repos(conn, excluded)
        missing = list_missing_refs(conn, prs)
        gone = [
            ref
            for ref, status in (fetch_prs(missing) if missing else {}).items()
            if isinstance(status, PrStatus) and status.state != "OPEN"
        ]
        with transaction(conn):
            store_open_prs(conn, prs, now)
            mark_closed(conn, gone, now)
    for ref, repo, number, key in list_backports_to_check(conn, config, now)[:BACKPORT_CHECKS_PER_POLL]:
        verdict = check_backport(repo, number, config.backport_bases, time.monotonic() + BACKPORT_CHECK_BUDGET_S)
        with transaction(conn):
            store_backport_check(conn, ref, key, verdict, now)
    for ref, repo, number, updated_at in list_refs_with_new_activity(conn):
        try:
            comments = fetch_comments(repo, number)
        except GitHubError as exc:
            print(f"review comments of {ref} failed: {exc}", flush=True)
            continue
        with transaction(conn):
            store_comments(conn, config, ref, updated_at, comments)
    for ref, repo, number, key in list_refs_with_new_threads(conn):
        try:
            threads = fetch_review_threads(config, repo, number)
        except GitHubError as exc:
            print(f"review threads of {ref} failed: {exc}", flush=True)
            continue
        with transaction(conn):
            store_review_threads(conn, ref, key, threads)
    for ref, repo, head in list_ci_blocks_to_check(conn):
        try:
            blocked_by = fetch_ci_blocked_by(repo, head)
        except GitHubError as exc:
            print(f"review CI checks of {ref} failed: {exc}", flush=True)
            continue
        with transaction(conn):
            store_ci_blocked_by(conn, ref, head, blocked_by)
    for ref, repo, doc_commit, head in list_docs_to_compare(conn):
        try:
            diff = fetch_doc_diff(repo, doc_commit, head)
        except GitHubError as exc:
            # Recorded as unknown, so a commit GitHub no longer has is not asked for each poll: the doc reads as behind.
            print(f"review doc compare of {ref} failed: {exc}", flush=True)
            diff = UNKNOWN_DIFF
        with transaction(conn):
            store_doc_diff(conn, ref, map_doc_to_diff_key(doc_commit, head), diff)
    with transaction(conn):
        record_tab_parents(conn, tree)
        if scanned_from is not None:
            classify_pending_comments(conn, config, scanned_from)
            prune_marks(conn, now)
        store_merge_blocks(conn)
        return [*take_comment_wakes(conn, config, now), *tell_ready_to_merge(conn, config, now)]


def list_docs_to_compare(conn: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    """(ref, repo, doc commit, head) of open PRs whose doc names another commit than the head and has no compare for
    the two yet."""
    found: list[tuple[str, str, str, str]] = []
    for row in conn.execute("SELECT * FROM review_prs WHERE closed_at IS NULL AND doc_file IS NOT NULL").fetchall():
        at, current = map_row_to_doc_heads(row)
        head = str(row["head_oid"])
        if at and not current and row["doc_diff_for"] != map_doc_to_diff_key(at, head):
            found.append((str(row["ref"]), str(row["repo"]), at, head))
    return found


def store_doc_diff(conn: sqlite3.Connection, ref: str, key: str, diff: DocDiff) -> None:
    conn.execute(
        """
        UPDATE review_prs SET doc_diff_for = ?, doc_diff_commits = ?, doc_diff_lines = ?, doc_diff_cosmetic = ?
        WHERE ref = ?
        """,
        (key, diff.commits, diff.lines, int(diff.cosmetic), ref),
    )


def drop_excluded_repos(conn: sqlite3.Connection, excluded: set[str]) -> None:
    """Rows of a repo taken out of the queue go silently: dropping them is not the PR closing."""
    for row in conn.execute("SELECT ref, repo FROM review_prs").fetchall():
        if str(row["repo"]).casefold() in excluded:
            conn.execute("DELETE FROM review_prs WHERE ref = ?", (row["ref"],))


def list_backports_to_check(conn: sqlite3.Connection, config: Config, now: int) -> list[tuple[str, str, int, str]]:
    """(ref, repo, number, check key) of open backports whose diff check is for another head or base, or refused a
    while ago; never checked first, then the oldest check."""
    if not config.backport_bases:
        return []
    return [
        (str(r["ref"]), str(r["repo"]), int(r["number"]), map_row_to_backport_key(r))
        for r in conn.execute(
            """
            SELECT ref, repo, number, head_oid, base, backport_key, backport_waived, backport_at FROM review_prs
            WHERE closed_at IS NULL ORDER BY backport_at IS NOT NULL, backport_at
            """
        )
        if is_backport_base(config.backport_bases, str(r["base"]))
        and (
            r["backport_key"] != map_row_to_backport_key(r)
            or (not r["backport_waived"] and int(r["backport_at"] or 0) <= now - BACKPORT_RECHECK_MS)
        )
    ]


def map_row_to_backport_key(row: sqlite3.Row) -> str:
    """What a check's verdict holds for: its head and its base."""
    return f"{row['head_oid']} {row['base']}"


def store_backport_check(
    conn: sqlite3.Connection, ref: str, key: str, verdict: BackportVerdict | None, now: int
) -> None:
    conn.execute(
        """
        UPDATE review_prs SET backport_key = ?, backport_waived = ?, backport_reason = ?, backport_at = ?
        WHERE ref = ?
        """,
        (key, bool(verdict and verdict.waived), verdict.reason if verdict else None, now, ref),
    )


def map_row_to_merge_block(conn: sqlite3.Connection, row: sqlite3.Row) -> str | None:
    """Why the owner's rule forbids merging, whatever the approvals: a thread they started is unresolved, or their last
    word has neither an answer nor a commit after it. None when nothing of theirs is open. Stored without a name: the
    page reads it to the owner, the merge guard to a lead."""
    reasons: list[str] = []
    if not _are_threads_current(row):
        reasons.append("review threads could not be read yet")
    threads = int(row["his_open_threads"] or 0)
    if threads:
        reasons.append(f"{threads} review thread{'s are' if threads > 1 else ' is'} unresolved")
    if _is_his_last_word_unanswered(conn, str(row["ref"])):
        reasons.append("the last comment has no answer and no commit after it")
    return "; ".join(reasons) or None


def _are_threads_current(row: sqlite3.Row) -> bool:
    return row["threads_key"] is None or row["threads_key"] == row["threads_read_for"]


def _is_his_last_word_unanswered(conn: sqlite3.Connection, ref: str) -> bool:
    his_last = conn.execute(
        "SELECT max(created_at) FROM review_comments WHERE ref = ? AND verdict = ?", (ref, HIS)
    ).fetchone()[0]
    if his_last is None:
        return False
    answered = conn.execute(
        """
        SELECT max(at) FROM (
            SELECT created_at AS at FROM review_comments WHERE ref = ?1 AND verdict IN (?2, ?3, ?4)
            UNION ALL SELECT committed_at FROM review_heads WHERE ref = ?1)
        """,
        (ref, AGENT, COLLEAGUE, REVIEWER),
    ).fetchone()[0]
    return answered is None or int(answered) < int(his_last)


def map_row_to_threads_to_resolve(conn: sqlite3.Connection, config: Config, row: sqlite3.Row) -> list[dict[str, str]]:
    """The owner's open threads, each with its reply quoted, when they are all answered and nothing else of theirs
    blocks the merge: only resolving them is left, and that is their move. Empty while any of them still waits on its
    owner."""
    threads: list[dict[str, Any]] = json.loads(str(row["his_threads"] or "[]"))
    if not _are_threads_current(row) or not threads or len(threads) != int(row["his_open_threads"] or 0):
        return []
    if not all(_is_thread_answered(conn, config, str(row["ref"]), thread) for thread in threads):
        return []
    if _is_his_last_word_unanswered(conn, str(row["ref"])):
        return []
    return [{"url": str(t["url"]), "quote": map_reply_to_quote(str(t["last_body"]))} for t in threads]


def is_resolving_all_that_is_left(row: sqlite3.Row) -> bool:
    """Nothing but the owner's threads stands between the PR and a merge: ready, approved, green, no conflicts, and no
    thread of anyone else's open."""
    return (
        not row["draft"]
        and row["checks"] in (None, "SUCCESS")
        and bool(json.loads(str(row["approved_by"] or "[]")))
        and row["merge_state"] != "DIRTY"
        and int(row["open_threads"] or 0) == int(row["his_open_threads"] or 0)
    )


def _is_thread_answered(conn: sqlite3.Connection, config: Config, ref: str, thread: dict[str, Any]) -> bool:
    if not thread["replies"]:
        return False
    if not is_his_account(config, str(thread["last_author"])):
        return True
    if str(thread["last_body"]).lstrip().startswith(AGENT_PREFIX):
        return True
    verdict = conn.execute(
        "SELECT verdict FROM review_comments WHERE ref = ? AND comment_id = ?", (ref, thread["last_id"])
    ).fetchone()
    return verdict is not None and verdict[0] == AGENT


def map_reply_to_quote(body: str, limit: int = QUOTE_MAX_CHARS) -> str:
    text = " ".join(body.strip().removeprefix(AGENT_PREFIX).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def map_row_to_answered_threads(
    conn: sqlite3.Connection, row: sqlite3.Row, now: int, shown: list[dict[str, str]]
) -> list[dict[str, object]]:
    """The owner's threads an agent answered in the last day, with the commits the answer names. An agent resolves a
    thread under the owner's login too, so the answer, not the resolver, tells that the owner's point was handled."""
    ref = str(row["ref"])
    replies = conn.execute(
        """
        SELECT h.url, h.body AS his_body, r.body, r.created_at FROM review_comments h
        JOIN review_comments r ON r.ref = h.ref AND r.parent_id = h.comment_id AND r.verdict = ?
        WHERE h.ref = ? AND h.verdict = ? AND h.parent_id IS NULL
        ORDER BY r.created_at
        """,
        (AGENT, ref, HIS),
    ).fetchall()
    latest = {str(r["url"]): r for r in replies}
    shown_urls = {t["url"] for t in shown}
    open_urls = {str(t["url"]) for t in json.loads(str(row["his_threads"] or "[]"))}
    oids = [str(r[0]) for r in conn.execute("SELECT oid FROM review_heads WHERE ref = ?", (ref,))]
    answered: list[dict[str, object]] = []
    for url, reply in latest.items():
        if url in shown_urls or int(reply["created_at"]) < now - ANSWERED_SHOWN_MS:
            continue
        commits = [
            {"sha": sha, "url": f"{row['url']}/commits/{oid}"}
            for sha in dict.fromkeys(_SHA_RE.findall(str(reply["body"])))
            if (oid := next((o for o in oids if o.startswith(sha)), None))
        ]
        answered.append(
            {
                "url": url,
                "his": map_reply_to_quote(str(reply["his_body"]), HIS_QUOTE_MAX_CHARS),
                "quote": map_reply_to_quote(str(reply["body"])),
                "commits": commits,
                "resolved": url not in open_urls if _are_threads_current(row) else None,
            }
        )
    return answered


def store_merge_blocks(conn: sqlite3.Connection) -> None:
    end_reviewed_holds(conn)
    for row in conn.execute("SELECT * FROM review_prs WHERE closed_at IS NULL").fetchall():
        block = map_row_to_merge_block(conn, row)
        # The owner's comments reopen the telling, so once they are settled the lead hears it is ready again.
        conn.execute(
            """
            UPDATE review_prs SET merge_block = ?1, merge_path = ?2,
                merge_told_at = CASE WHEN ?1 IS NULL THEN merge_told_at END
            WHERE ref = ?3
            """,
            (block, map_row_to_merge_path(row, block), row["ref"]),
        )
    # The shell hook reaches the guard only while its flag is up.
    sync_claims_flag(conn)


def end_reviewed_holds(conn: sqlite3.Connection) -> None:
    """A hold waits for the owner's review: their comment or review on GitHub after it, or the PR published."""
    conn.execute(
        """
        UPDATE review_prs SET held_at = NULL, held_by = NULL, held_why = NULL
        WHERE held_at IS NOT NULL AND (
            reviewed_at IS NOT NULL OR post_state = 'external'
            OR EXISTS (SELECT 1 FROM review_comments c
                       WHERE c.ref = review_prs.ref AND c.verdict = ? AND c.created_at >= review_prs.held_at)
        )
        """,
        (HIS,),
    )


def map_row_to_merge_path(row: sqlite3.Row, merge_block: str | None) -> str | None:
    """READY when the owner's rule lets the PR's lead merge it: reviewed by the owner, approved, CI green, not a draft,
    no conflicts and nothing of the owner's open. A Slack post alone is no review."""
    return None if list_merge_waits(map_row_to_merge_facts(row, merge_block)) else READY


def is_approved(row: sqlite3.Row) -> bool:
    """GitHub-approved, by a colleague or by the review bot alone, or the owner's answered decision stands in."""
    return (
        bool(row["bot_approved"])
        or bool(json.loads(str(row["approved_by"] or "[]")))
        or row["approval_waived_at"] is not None
    )


def map_row_to_approvers(row: sqlite3.Row, config: Config) -> list[str]:
    """Whose standing verdict on GitHub is APPROVED: colleagues by login, the review bot by its name."""
    people: list[str] = json.loads(str(row["approved_by"] or "[]"))
    return [*people, config.review_bot_name] if row["bot_approved"] else people


_APPROVAL_LEFT = {
    NOT_OWNER_REVIEWED: "your review",
    HELD: "held for your review",
    OWNER_COMMENTS_OPEN: "your comments open",
    CI_RED: "CI red",
    CI_PENDING: "CI pending",
    CONFLICTS: "conflicts",
    BLOCKED_ON_GITHUB: "blocked on GitHub",
}


def map_row_to_approval_left(row: sqlite3.Row, threads_to_resolve: int) -> str | None:
    """What an approved PR still waits for before its lead merges it. None when it is not approved, or is a draft,
    which waits on its owner first."""
    if not is_approved(row) or row["draft"]:
        return None
    if threads_to_resolve:
        return "resolve your threads"
    waits = list_merge_waits(map_row_to_merge_facts(row, row["merge_block"]))
    return _APPROVAL_LEFT[waits[0]] if waits else "ready to merge (lead)"


def tell_ready_to_merge(conn: sqlite3.Connection, config: Config, now: int) -> list[tuple[int, Recipient]]:
    """Once per PR, its owner is told it may merge; with no owner it waits for one."""
    sent: list[tuple[int, Recipient]] = []
    for row in conn.execute(
        "SELECT * FROM review_prs WHERE closed_at IS NULL AND merge_path = ? AND merge_told_at IS NULL", (READY,)
    ).fetchall():
        owner = find_owner(conn, config, str(row["ref"]), str(row["repo"]), str(row["head"]))
        if owner is None:
            continue
        ready = read_overlay(config, "ready-to-merge", "ready to merge")
        text = (
            f"{row['url']} ({row['title']}) is {ready}: approved on GitHub by "
            f"{', '.join(map_row_to_approvers(row, config))}, CI green or none, no conflicts, and no comment of "
            f"{config.owner_name}'s open. Merge it once you judge it safe: gh pr merge {row['url']}. "
            f"Deploys still need {config.owner_name}."
        )
        sent.append(tell_owner(conn, owner, text, now))
        conn.execute("UPDATE review_prs SET merge_told_at = ? WHERE ref = ?", (now, row["ref"]))
    return sent


class MergeOkRefused(Exception):
    pass


def can_ok_merge(config: Config, row: sqlite3.Row) -> bool:
    """The owner may OK its merge from the page: see map_row_to_merge_ok_needs."""
    return map_row_to_merge_ok_needs(row) is None


def map_row_to_merge_ok_needs(row: sqlite3.Row) -> str | None:
    """What keeps the Merge button off, or None: open, not a draft, not stacked (a stacked PR merges after the one
    under it), approved by the review bot at its head or by a colleague, CI green or none, and GitHub would not refuse
    it. A backport gets the button on the same terms."""
    if not is_mergeable_but_for_ci(row):
        return map_row_to_unmergeable_why(row)
    if row["checks"] not in (None, "SUCCESS"):
        return "CI is not green"
    if row["merge_state"] == "BLOCKED":
        return "GitHub would refuse it: branch protection wants an approval it does not have yet"
    return None


def is_mergeable_but_for_ci(row: sqlite3.Row) -> bool:
    return row["closed_at"] is None and not row["draft"] and row["stack_parent"] is None and is_approved_row(row)


def map_row_to_unmergeable_why(row: sqlite3.Row) -> str:
    if row["closed_at"] is not None:
        return "closed"
    if row["draft"]:
        return "a draft"
    if row["stack_parent"] is not None:
        return "stacked: it merges after the PR under it"
    return "needs a colleague's approval or the review bot's approval of its newest commit"


def map_row_to_merge_blocked_by_github(row: sqlite3.Row) -> str | None:
    """Why the Merge button shows disabled: CI is red only because GitHub refused to run it at this head."""
    if row["checks"] != "FAILURE" or row["ci_blocked_for"] != row["head_oid"] or not is_mergeable_but_for_ci(row):
        return None
    return row["ci_blocked_by"]


def list_ci_blocks_to_check(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """(ref, repo, head) of PRs that would get the Merge button but for red CI, not yet read at this head or showing a
    reason: a re-run keeps the head, and one that fails for real between two polls never shows the rollup change."""
    rows = conn.execute(
        """
        SELECT * FROM review_prs WHERE closed_at IS NULL AND checks = 'FAILURE'
        AND (ci_blocked_for IS NOT head_oid OR ci_blocked_by IS NOT NULL)
        """
    ).fetchall()
    return [(str(r["ref"]), str(r["repo"]), str(r["head_oid"])) for r in rows if is_mergeable_but_for_ci(r)]


def store_ci_blocked_by(conn: sqlite3.Connection, ref: str, head_oid: str, blocked_by: str | None) -> None:
    conn.execute(
        "UPDATE review_prs SET ci_blocked_by = ?, ci_blocked_for = ? WHERE ref = ? AND head_oid = ?",
        (blocked_by, head_oid, ref, head_oid),
    )


def load_merge_ok(conn: sqlite3.Connection, ref: str, head_oid: str) -> dict[str, object] | None:
    """The owner's OK for the PR's head, else their latest; `current` is false when it was given for another head.
    A head force-pushed back to one already OKed has its OK again."""
    row = conn.execute(
        "SELECT * FROM review_merge_oks WHERE ref = ? ORDER BY head_oid = ? DESC, given_at DESC LIMIT 1",
        (ref, head_oid),
    ).fetchone()
    if row is None:
        return None
    return {
        "by": row["given_by"],
        "at": row["given_at"],
        "sent_to": row["sent_to"],
        "no_owner": bool(row["no_owner"]),
        "head": str(row["head_oid"])[:DOC_SHA_CHARS],
        "current": row["head_oid"] == head_oid,
    }


def record_merge_ok(
    conn: sqlite3.Connection, config: Config, ref: str, head_oid: str, by: str, now: int
) -> tuple[int, Recipient]:
    """The owner OKs merging the PR at this head: its owning session is told to merge it, or, with none, the assistant
    is asked to find one. Bridge itself never merges."""
    row = conn.execute("SELECT * FROM review_prs WHERE ref = ?", (ref,)).fetchone()
    if row is None or row["closed_at"] is not None:
        raise MergeOkRefused(f"{ref} is not an open PR in the queue")
    if not can_ok_merge(config, row):
        raise MergeOkRefused(f"{ref} cannot get a merge OK: {map_row_to_merge_ok_needs(row)}")
    if head_oid != row["head_oid"]:
        raise MergeOkRefused(f"{ref} has new commits since the page showed it; look at them first")
    if conn.execute("SELECT 1 FROM review_merge_oks WHERE ref = ? AND head_oid = ?", (ref, head_oid)).fetchone():
        raise MergeOkRefused(f"the merge OK for {ref} at this commit was already sent")
    owner = find_owner(conn, config, ref, str(row["repo"]), str(row["head"]))
    try:
        recipient = (
            resolve_tab(conn, owner.conversation_id, owner.handle)
            if owner is not None
            else resolve_handle(conn, config.assistant_handle)
        )
    except AddressError as exc:
        raise MergeOkRefused(f"no session owns {ref}, and {config.assistant_handle} cannot be reached: {exc}") from exc
    text = map_merge_ok_to_text(config, row, owner is None)
    result = send_message(conn, Party(SYSTEM_CONVERSATION, "bridge"), [recipient], "info", text, now)
    conn.execute("UPDATE messages SET post_type = ? WHERE id = ?", (REVIEW_QUEUE, result.message_id))
    conn.execute(
        "INSERT INTO review_merge_oks (ref, head_oid, given_by, given_at, sent_to, no_owner, message_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ref, head_oid, by, now, recipient.handle, int(owner is None), result.message_id),
    )
    # The press is the owner's word for this head only, so the guard lets the merge through until a new commit.
    conn.execute("UPDATE review_prs SET merge_ok_oid = ? WHERE ref = ?", (head_oid, ref))
    return result.message_id, recipient


def map_merge_ok_to_text(config: Config, row: sqlite3.Row, no_owner: bool) -> str:
    url, oid = row["url"], row["head_oid"]
    command = f"gh pr merge {url} --match-head-commit {oid}"
    given = (
        f"{config.owner_name_at_start} OKs merging {url} ({row['title']}) at {oid}: they pressed Merge on the Guild "
        f"page, as it is approved and CI is green. A new commit voids this OK."
    )
    if no_owner:
        return (
            f"{given} No session owns it, so it came to you: find its lead and pass this on, to merge it now through "
            f"the guard as usual ({command})."
        )
    return f"{given} Merge it now, through the guard as usual: {command}. Deploys still need {config.owner_name}."


def map_db_to_reviews(conn: sqlite3.Connection, config: Config, now: int) -> dict[str, object]:
    """The queue as the page shows it, read only from the database: a page poll makes no GitHub call."""
    poll = conn.execute("SELECT ok_at, error, error_at FROM review_poll WHERE id = 1").fetchone()
    rows = conn.execute("SELECT * FROM review_prs WHERE closed_at IS NULL ORDER BY updated_at DESC").fetchall()
    return {
        "enabled": bool(config.review_org),
        "ok_at": poll["ok_at"] if poll else None,
        "github": {"ok_at": poll["ok_at"] if poll else None, "every_s": GITHUB_EVERY_S, "stale_s": GITHUB_STALE_S},
        "error": poll["error"] if poll else None,
        "large_loc": REVIEW_LARGE_LOC,
        "loc_excluded": list(list_loc_excluded(config)),
        "dry_run": config.review_post_dry_run,
        "mention_people": [name for name, _ in config.review_mention_people],
        "comment_max_chars": COMMENT_MAX_CHARS,
        "rules": [
            f"Lines are additions plus deletions of code, leaving out docs, images and "
            f"{', '.join(list_loc_excluded(config))}.",
            f"At {REVIEW_LARGE_LOC} lines or more a PR gets a document and a worktree: non-draft, not yet "
            "published, updated in the last 14 days.",
            f"Agents post under your account too, and start each comment and description with '{AGENT_PREFIX}' "
            "(never a title); every other comment under it is yours.",
            "Your comments, the comments and change requests of colleagues and the review bot, and a PR a "
            f"colleague approved wake the PR's owner; at most {WAKES_PER_HOUR_MAX} wakes a PR an hour.",
            "Open: not a draft on GitHub, not published and not ready to merge, or held by its lead for your own "
            "review until you comment or review on GitHub or it is published. Ready to merge "
            f"(lead): approved on GitHub (a colleague or {config.review_bot_name}), CI green or none, not a draft, no "
            "conflicts, and "
            "no comment of yours open; its lead merges it without your review or a post, and is told once. Published: "
            "you pressed Publish, or a post is on record. Drafts in progress: drafts on GitHub, whose owners are "
            "asked once to mark them ready there when they are.",
            *(
                [
                    f"Backports (lead merges): PRs onto {', '.join(config.backport_bases)}. They never enter your "
                    "queue: no number, no Slack post, no doc or worktree. Each is checked against the merged "
                    "default-branch PRs it names (per-hunk git patch-id); an identical one needs no colleague's "
                    "sign-off, and the rest of the merge rule still applies."
                ]
                if config.backport_bases
                else []
            ),
            f"GitHub is read every {GITHUB_EVERY_S} s: one GraphQL search per 50 PRs at 4 of the account's 5,000 "
            "points an hour, so about 160 an hour, plus REST reads of comments and a 2-point read of review threads "
            "only for PRs whose time or open threads changed.",
            f"{convert_to_sentence_start(config.review_channel_name)} is read every minute with a Slack token in "
            "~/.secrets/slack, else every 5 minutes through "
            "the Slack connector. Any post there with a PR's link, by anyone, publishes the PR, and nobody is told to "
            "request reviewers for it.",
            "Numbers 1 to 3 run across questions and PRs: first PRs where only your own answered threads block the "
            "merge, so resolving them is all that is left; then urgent questions, other questions oldest first, PRs "
            "with fixes after your comments, then other PRs waiting for you by project priority, stacked parents "
            "first, smaller, older. PRs with your comments still open wait on their owner and get no number, and so "
            "do PRs ready to merge: their leads merge them.",
            f"A PR of {REVIEW_LARGE_LOC} lines or more is ready for you once its doc has a Reviews section"
            f"{convert_reviews_to_naming(config.required_reviews)}; until then it gets no number. Under that, the PR "
            "body lists them.",
            "A PR whose title or description says it only enables something (off by default, an opt-in flag, behind a "
            "flag, step N of M, …) is ready for you once its description has a section headed How this will be used; "
            "its owner is told once. A line 'Not an enabling PR: <reason>' turns the check off; drafts are not "
            "checked.",
        ],
        "prs": sorted((map_row_to_review(conn, config, row, now) for row in rows), key=map_review_to_sort_key),
    }


def map_review_to_sort_key(pr: dict[str, object]) -> tuple[int, int]:
    """Group by group; within one, by state and then the newest first, as the sort is stable."""
    return _GROUP_ORDER.index(str(pr["group"])), _STATE_ORDER.get(str(pr["state"]), len(_STATE_ORDER))


def map_row_to_group(row: sqlite3.Row, config: Config) -> tuple[str, str]:
    """(group, why, in words). A draft waits for the owner only once GitHub says it is ready for review. A backport
    never enters their queue."""
    if is_backport_base(config.backport_bases, str(row["base"])):
        return BACKPORTS, f"a backport onto {row['base']}: its lead merges it once the merge rule holds"
    if row["merge_path"] == READY:
        return READY, "approved, CI green or none, nothing of yours open: its lead may merge it"
    if row["reviewed_at"]:
        return PUBLISHED, "you pressed Publish"
    if row["post_state"] == "external":
        return PUBLISHED, "a post is on record"
    if row["held_at"] is not None and not row["draft"]:
        return WAITING_FOR_YOU, "its lead holds it for your own review"
    if row["draft"]:
        return DRAFTS, "a draft on GitHub; it joins Open once its owner marks it ready there"
    return WAITING_FOR_YOU, "open and not published"


def map_row_to_reviews_ran(row: sqlite3.Row) -> tuple[str, ...] | None:
    """The reviews its Reviews section names: the doc's for a large PR, the body's under that. None: no section."""
    if int(row["loc"]) >= REVIEW_LARGE_LOC:
        return map_text_to_reviews(row["doc_reviews"]) if row["doc_file"] else None
    return map_text_to_reviews(row["body_reviews"])


def convert_to_sentence_start(text: str) -> str:
    return text[:1].upper() + text[1:]


def convert_reviews_to_naming(required: tuple[str, ...]) -> str:
    """' naming a Codex and a Claude review', or nothing when no review is required by name."""
    return f" naming {' and '.join(f'a {name}' for name in required)} review" if required else ""


def map_row_to_reviews_gap(row: sqlite3.Row, config: Config) -> str | None:
    """Why a large PR is not ready for the owner: its doc must have a Reviews section naming each required review.
    None when it is ready, or small: a small PR's list goes in its body, unchecked."""
    if int(row["loc"]) < REVIEW_LARGE_LOC:
        return None
    if not row["doc_file"]:
        return "no doc yet"
    ran = map_row_to_reviews_ran(row)
    if ran is None:
        return "doc lacks Reviews section"
    missing = [name for name in config.required_reviews if name not in ran]
    return f"{' / '.join(missing)} review missing" if missing else None


def map_row_to_usage_gap(row: sqlite3.Row) -> str | None:
    """Why a PR that says it only enables something is not ready for the owner; a draft is not checked yet."""
    return None if row["draft"] or not row["usage_missing"] else USAGE_GAP


def map_row_to_review(conn: sqlite3.Connection, config: Config, row: sqlite3.Row, now: int) -> dict[str, object]:
    ref = str(row["ref"])
    his = [
        int(r[0])
        for r in conn.execute("SELECT created_at FROM review_comments WHERE ref = ? AND verdict = ?", (ref, HIS))
    ]
    heads = [int(r[0]) for r in conn.execute("SELECT committed_at FROM review_heads WHERE ref = ?", (ref,))]
    rounds, comments_open = map_events_to_rounds(his, heads)
    his_last_at = max(his, default=None)
    doc_at, doc_current = map_row_to_doc_heads(row)
    owner = find_owner(conn, config, ref, str(row["repo"]), str(row["head"]))
    # Reviewers on a PR do not make it published: the owner decides, and only a known post counts.
    group, group_why = map_row_to_group(row, config)
    resolve_threads = map_row_to_threads_to_resolve(conn, config, row)
    published = group == PUBLISHED
    if published:
        state = "published"
    elif group == BACKPORTS:
        state = map_row_to_backport_state(row)
    elif comments_open:
        state = COMMENTS_OPEN
    elif group == READY:
        state = READY
    else:
        state = FIXED if rounds else WAITING_FOR_OWNER
    return {
        "ref": ref,
        "label": map_ref_to_label(ref),
        "repo": row["repo"],
        "number": row["number"],
        "title": row["title"],
        "url": row["url"],
        "draft": bool(row["draft"]),
        "loc": row["loc"],
        "large": int(row["loc"]) >= REVIEW_LARGE_LOC,
        "checks": row["checks"],
        "state": state,
        "rounds": rounds,
        "fixed_at": max(heads) if state == FIXED else None,
        "first_seen_at": row["first_seen_at"],
        "issue_keys": _load_issue_keys_of_pr(conn, ref, str(row["title"]), owner),
        "open_threads": int(row["open_threads"] or 0),
        "his_open_threads": int(row["his_open_threads"] or 0),
        "his_last_at": his_last_at,
        "commits_after_his": sum(1 for t in heads if his_last_at is not None and t > his_last_at),
        "owner": owner.handle if owner else None,
        "owner_surface": owner.surface if owner else None,
        "lead": owner.lead if owner else None,
        "group": group,
        "group_why": group_why,
        "approved_by": json.loads(str(row["approved_by"])),
        "approvers": map_row_to_approvers(row, config),
        "approval_left": map_row_to_approval_left(row, len(resolve_threads)),
        "merge_state": "in queue" if row["in_merge_queue"] else row["merge_state"],
        "conflict_asked_at": row["conflict_asked_at"] if row["conflict_asked_oid"] == row["head_oid"] else None,
        "base": row["base"],
        "merge_block": row["merge_block"],
        "resolve_threads": resolve_threads,
        "answered_threads": map_row_to_answered_threads(conn, row, now, resolve_threads),
        "resolve_is_last": is_resolving_all_that_is_left(row),
        "can_publish": group in (WAITING_FOR_YOU, READY) and not row["reviewed_at"] and row["post_state"] != "external",
        "merge_path": row["merge_path"],
        "head_oid": row["head_oid"],
        "can_ok_merge": can_ok_merge(config, row),
        "merge_ok_needs": map_row_to_merge_ok_needs(row),
        "merge_blocked_by_github": map_row_to_merge_blocked_by_github(row),
        "merge_ok": load_merge_ok(conn, ref, str(row["head_oid"])),
        "merge_ok_to": owner.handle if owner else config.assistant_handle,
        "approval_waived": (
            f"waived by {config.owner_name} ({convert_item_id_to_question_ref(row['approval_waived_decision'])})"
            if row["approval_waived_at"] is not None
            else None
        ),
        "held": f"held for {config.owner_name}" if row["held_at"] is not None else None,
        "held_why": row["held_why"],
        "held_by": row["held_by"],
        "held_at": row["held_at"],
        "backport_check": map_row_to_backport_check(row) if group == BACKPORTS else None,
        "bot_approved": bool(row["bot_approved"]),
        "reviews_ran": list(map_row_to_reviews_ran(row) or ()),
        "reviews_gap": None
        if group == BACKPORTS
        else "; ".join(g for g in (map_row_to_reviews_gap(row, config), map_row_to_usage_gap(row)) if g) or None,
        "reviewed_at": row["reviewed_at"],
        "reviewed_by": row["reviewed_by"],
        "reviewed_oid": row["reviewed_oid"],
        "commits_since": count_commits_since(str(row["commit_oids"]), row["reviewed_oid"]),
        "has_sentence": bool(row["sentence"]),
        "description": row["sentence"],
        "stack_parent": row["stack_parent"],
        "depends_on": f"pr:{row['depends_on']}" if row["depends_on"] else None,
        "depends_on_label": map_ref_to_label(f"pr:{row['depends_on']}") if row["depends_on"] else None,
        "post_state": row["post_state"],
        "post_detail": row["post_detail"],
        "post_link": row["post_link"],
        "post_at": row["post_at"],
        "posted_at": map_row_to_posted_ms(row),
        "can_retry": row["post_state"] == "failed" and group != BACKPORTS,
        "can_repost": group != BACKPORTS
        and (row["post_state"] == "interrupted" or (row["post_state"] == "dry-run" and not config.review_post_dry_run)),
        "has_doc": bool(row["doc_file"]),
        "doc_url": f"/reviews/{row['repo']}/{row['number']}/doc" if row["doc_file"] and group != BACKPORTS else None,
        "doc_stale": bool(row["doc_file"]) and not doc_current,
        "doc_at": doc_at[:DOC_SHA_CHARS] if row["doc_file"] else None,
        "pr_at": str(row["head_oid"])[:DOC_SHA_CHARS],
        "doc_behind": map_row_to_doc_behind(row) if row["doc_file"] and not doc_current else None,
        "worktree_path": row["wt_path"],
        "worktree_url": map_worktree_to_url(Path(str(row["wt_path"]))) if row["wt_path"] else None,
        "worktree_state": row["wt_state"],
        "worktree_detail": row["wt_detail"],
        "worktree_stale": bool(row["wt_path"]) and row["wt_oid"] != row["head_oid"],
    }


def map_row_to_backport_check(row: sqlite3.Row) -> dict[str, object] | None:
    """The identical-diff check's last verdict, when one was made; `current` is false once the head moved past it."""
    if row["backport_reason"] is None:
        return None
    return {
        "waived": bool(row["backport_waived"]),
        "reason": row["backport_reason"],
        "current": row["backport_key"] == map_row_to_backport_key(row),
        "at": row["backport_at"],
    }


def map_worktree_to_url(path: Path) -> str:
    """Its workspace when it has one, which opens on the PR with the chat sidebar closed; the folder until then."""
    workspace = map_worktree_to_workspace(path)
    return f"vscode://file{urllib.parse.quote(str(workspace if workspace.exists() else path))}"


def _load_issue_keys_of_pr(conn: sqlite3.Connection, ref: str, title: str, owner: Owner | None) -> list[str]:
    """What its title names, and the issues of the owning lead's chunks of work that name this PR."""
    keys = set(_ISSUE_KEY_RE.findall(title.upper()))
    if owner is not None and owner.lead:
        for chunk in conn.execute(
            "SELECT refs FROM lead_work WHERE lead = ? AND done_at IS NULL", (owner.lead.lower(),)
        ):
            refs = [str(chunk_ref) for chunk_ref in json.loads(str(chunk["refs"]))]
            if ref in refs:
                keys.update(chunk_ref.removeprefix("issue:") for chunk_ref in refs if chunk_ref.startswith("issue:"))
    return sorted(keys)


def map_row_to_posted_ms(row: sqlite3.Row) -> int | None:
    """When the Slack message went out: its permalink's ts, else bridge's own send time; a dry run posted nothing."""
    match = _PERMALINK_TS_RE.search(str(row["post_link"] or ""))
    if match:
        return int(match.group(1)) * 1000 + int(match.group(2)) // 1000
    if row["post_state"] in ("posted", "posted-link-unknown"):
        return row["post_at"]
    return None


def count_commits_since(commit_oids: str, oid: str | None) -> int | None:
    """None when there is nothing to count from, or a force-push removed that commit from the history."""
    oids: list[str] = json.loads(commit_oids)
    if oid is None or oid not in oids:
        return None
    return len(oids) - 1 - oids.index(oid)


def record_approval_waiver(conn: sqlite3.Connection, ref: str, decision: int, by: str, now: int) -> str:
    """The owner's answered decision stands in for an approving GitHub review; returns their answer, quoted back."""
    item = conn.execute("SELECT kind, answer FROM user_items WHERE id = ?", (decision,)).fetchone()
    if item is None or item["kind"] not in ("decision", "blocked"):
        raise ValueError(f"#{decision} is not a decision or blocked post on the page")
    if not str(item["answer"] or "").strip():
        raise ValueError(f"#{decision} has no answer from the owner yet")
    row = conn.execute("SELECT closed_at FROM review_prs WHERE ref = ?", (ref,)).fetchone()
    if row is None or row["closed_at"] is not None:
        raise ValueError(f"{ref} is not an open PR in the queue")
    conn.execute(
        "UPDATE review_prs SET approval_waived_at = ?, approval_waived_by = ?, approval_waived_decision = ? "
        "WHERE ref = ?",
        (now, by, decision, ref),
    )
    return str(item["answer"])


def clear_approval_waiver(conn: sqlite3.Connection, ref: str) -> None:
    conn.execute(
        "UPDATE review_prs SET approval_waived_at = NULL, approval_waived_by = NULL, approval_waived_decision = NULL "
        "WHERE ref = ?",
        (ref,),
    )


_BACKPORT_WAIT_WORDS = {
    NOT_APPROVED: "an approving review or a waiver",
    NOT_OWNER_REVIEWED: "the owner's word",
}


def map_row_to_backport_state(row: sqlite3.Row) -> str:
    """A backport is its lead's to merge, never the owner's to review: its state says what the lead waits on."""
    waits = list_merge_waits(map_row_to_merge_facts(row, row["merge_block"]))
    if not waits:
        return "lead merges: ready"
    return "lead merges: waiting on " + ", ".join(_BACKPORT_WAIT_WORDS.get(w, w) for w in waits)
