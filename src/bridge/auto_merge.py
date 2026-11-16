import json
import sqlite3
from collections.abc import Sequence
from typing import Any

from bridge.board import (
    SYSTEM_CONVERSATION,
    USER_ITEM_MAX_CHARS,
    AddressError,
    Party,
    Recipient,
    resolve_handle,
    send_message,
)
from bridge.config import Config
from bridge.db import transaction
from bridge.feed import REVIEW_QUEUE, FeedLink, add_post
from bridge.guard import MergeRefusal, find_standing_rule_block, map_wait_to_reason
from bridge.review_deps import map_states_to_blockers
from bridge.review_github import GitHubError, fetch_pr_states, run_gh
from bridge.reviews import (
    find_owner,
    list_stacked_on,
    load_open_siblings,
    map_row_to_dependency_blockers,
    map_siblings_to_wait,
    tell_owner,
)

SANDBOX_BLOCK = "PERSIST_SANDBOX in its body"
MERGE_TIMEOUT_S = 120
_LOGGED: set[tuple[str, str, str]] = set()
"""(ref, head, line) the service log already has: a dry run or a refusal is logged once per head, not every poll."""


def map_row_to_auto_merge_block(config: Config, row: sqlite3.Row, siblings: Sequence[sqlite3.Row] = ()) -> str | None:
    """The first reason, from the queue's copy, that the owner's standing rule keeps the PR from merging by itself;
    None: a candidate, to be checked live. Its siblings must each be ready on the same terms: they go together."""
    return map_row_to_own_auto_merge_block(config, row) or map_siblings_to_wait(
        siblings, lambda sibling: map_row_to_own_auto_merge_block(config, sibling, with_siblings=False)
    )


def map_row_to_own_auto_merge_block(config: Config, row: sqlite3.Row, with_siblings: bool = True) -> str | None:
    """The rule, siblings aside: a colleague approved (the review bot's approval or the owner's
    waiver does not count), CI green or none, the review bot's latest review is of the head and asks for no changes,
    GitHub says CLEAN, no PR its body names is open, not stacked, a draft or held, and nothing of the owner's open."""
    owner = config.owner_name
    if row["closed_at"] is not None:
        return "closed"
    if row["stack_parent"] is not None:
        return "stacked: it merges after the PR under it"
    if row["draft"]:
        return "a draft"
    if row["held_at"] is not None:
        return f"held for {owner}'s review"
    if row["merge_block"]:
        return f"{owner}'s comments are open ({row['merge_block']})"
    if not json.loads(str(row["approved_by"] or "[]")):
        return f"no colleague's approval ({config.review_bot_name}'s or a waiver does not count)"
    if not row["bot_cleared"]:
        return f"{config.review_bot_name} has not reviewed its newest commit without asking for changes"
    if row["checks"] not in (None, "SUCCESS"):
        return f"CI is {str(row['checks']).lower()}"
    if row["merge_state"] != "CLEAN":
        return f"GitHub's merge state is {row['merge_state'] or 'unknown'}, not CLEAN"
    if blockers := map_row_to_dependency_blockers(row, with_siblings):
        return blockers[0]
    if row["auto_merge_oid"] == row["head_oid"]:
        failed = f" and failed: {row['auto_merge_error']}" if row["auto_merge_error"] else ""
        return f"an automatic merge of this head already ran{failed}"
    if row["persist_sandbox"]:
        return SANDBOX_BLOCK
    return None


def find_live_block(conn: sqlite3.Connection, config: Config, row: sqlite3.Row) -> str | None:
    """The guard's live check under the standing rule, then the named PRs read again: None, it may merge now."""
    refusal = find_standing_rule_block(conn, config, row)
    if refusal is not None:
        return map_refusal_to_reason(refusal, config)
    blockers = find_live_dependency_blockers(conn, row)
    return blockers[0] if blockers else None


def map_refusal_to_reason(refusal: MergeRefusal, config: Config) -> str:
    if refusal.error:
        return f"GitHub could not be read ({refusal.error})"
    return "; ".join(map_wait_to_reason(wait, refusal, config.owner_name) for wait in refusal.waits)


def find_live_dependency_blockers(conn: sqlite3.Connection, row: sqlite3.Row) -> tuple[str, ...]:
    """Its named PRs read again, leaving out its siblings: they are open by definition, and ready by the time it
    comes to this."""
    siblings = {ref.removeprefix("pr:").casefold() for ref in json.loads(str(row["siblings"] or "[]"))}
    named = [r for r in json.loads(str(row["named_prs"] or "[]")) if r.casefold() not in siblings]
    must = [r for r in json.loads(str(row["must_merge"] or "[]")) if r.casefold() not in siblings]
    if not named and not must:
        return ()
    children: dict[str, list[str]] = {}
    for child in conn.execute(
        "SELECT ref, stack_parent FROM review_prs WHERE closed_at IS NULL AND stack_parent IS NOT NULL"
    ):
        children.setdefault(str(child["stack_parent"]).casefold(), []).append(str(child["ref"]).casefold())
    skipped = list_stacked_on(children, str(row["ref"]).casefold())
    return map_states_to_blockers(tuple(named), tuple(must), skipped, fetch_pr_states([*named, *must]))


def list_open_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM review_prs WHERE closed_at IS NULL ORDER BY first_seen_at, repo, number"
    ).fetchall()


def map_db_to_dry_run(conn: sqlite3.Connection, config: Config) -> list[tuple[str, str | None]]:
    """(ref, why not) for each open queue PR, None for one that would merge; reads GitHub, writes nothing."""
    verdicts: list[tuple[str, str | None]] = []
    for row in list_open_rows(conn):
        block = map_row_to_auto_merge_block(config, row, load_open_siblings(conn, row))
        verdicts.append((str(row["ref"]), block if block is not None else find_live_block(conn, config, row)))
    return verdicts


def run_auto_merge(conn: sqlite3.Connection, config: Config, now: int) -> list[tuple[int, Recipient]]:
    """One round after the queue poll: merges at most one PR, the first that passes the live check; a dry run only
    logs. Each head is tried once, so a failure is reported once and never looped on. Returns messages to deliver."""
    if config.auto_merge == "off":
        return []
    dry = config.auto_merge == "dry-run"
    sent: list[tuple[int, Recipient]] = []
    for row in list_open_rows(conn):
        ref, head = str(row["ref"]), str(row["head_oid"])
        block = map_row_to_auto_merge_block(config, row, load_open_siblings(conn, row))
        if block == SANDBOX_BLOCK and not dry and row["sandbox_told_oid"] != head:
            with transaction(conn):
                sent.extend(tell_sandbox(conn, config, row, now))
        if block is not None or (dry and (ref, head, "would merge") in _LOGGED):
            continue
        live = find_live_block(conn, config, row)
        if live is not None:
            log_once(ref, head, f"auto-merge: {ref} not merged: {live}")
            continue
        if dry:
            log_once(ref, head, "would merge")
            print(f"auto-merge dry run: would merge {ref} at {head}", flush=True)
            continue
        return [*sent, *merge_pr(conn, config, row, now)]
    return sent


def log_once(ref: str, head: str, line: str) -> None:
    if (ref, head, line) not in _LOGGED:
        _LOGGED.add((ref, head, line))
        if line != "would merge":
            print(line, flush=True)


def merge_pr(conn: sqlite3.Connection, config: Config, row: sqlite3.Row, now: int) -> list[tuple[int, Recipient]]:
    ref, url, head = str(row["ref"]), str(row["url"]), str(row["head_oid"])
    # Recorded before the merge runs, so a crash or a failure never brings the same head back.
    with transaction(conn):
        conn.execute("UPDATE review_prs SET auto_merge_oid = ?, auto_merge_error = NULL WHERE ref = ?", (head, ref))
    try:
        run_gh(["pr", "merge", url, "--squash", "--match-head-commit", head], MERGE_TIMEOUT_S)
        merged: Any = json.loads(run_gh(["pr", "view", url, "--json", "state,mergeCommit"]))
        if merged.get("state") != "MERGED":
            raise GitHubError(f"gh pr merge returned, but the PR is {merged.get('state')} (a merge queue?)")
        merge_commit: Any = merged.get("mergeCommit") or {}
        commit = str(merge_commit.get("oid") or "")
    except (GitHubError, ValueError, AttributeError) as exc:
        with transaction(conn):
            conn.execute("UPDATE review_prs SET auto_merge_error = ? WHERE ref = ?", (str(exc), ref))
            return tell_failure(conn, config, row, str(exc), now)
    print(f"auto-merge: merged {ref} at {head} as {commit or 'an unread commit'}", flush=True)
    with transaction(conn):
        post_merged(conn, config, row, commit, now)
        return tell_merged(conn, config, row, commit, now)


def post_merged(conn: sqlite3.Connection, config: Config, row: sqlite3.Row, commit: str, now: int) -> int:
    repo, number = str(row["repo"]), int(row["number"])
    rule = (
        f"rule {config.auto_merge_decision}" if config.auto_merge_decision else f"{config.owner_name}'s standing rule"
    )
    tail = f" as {commit[:7] or 'an unread commit'} ({rule})"
    head = f"merged {repo}#{number} "
    room = USER_ITEM_MAX_CHARS - len(head) - len(tail)
    title = str(row["title"])
    title = title if len(title) <= room else title[: max(room - 1, 0)] + "…"
    links = [FeedLink(f"{repo}#{number}", str(row["url"]))]
    if commit:
        links.append(FeedLink(commit[:7], f"https://github.com/{repo}/commit/{commit}"))
    body = (
        f"Approved by {', '.join(json.loads(str(row['approved_by'])))}; CI green or none; {config.review_bot_name} "
        f"cleared the head; no PR its body names is open. Merged, not deployed: its lead was told."
    )
    return add_post(conn, Party(SYSTEM_CONVERSATION, "bridge"), "done", f"{head}{title}{tail}", body, links, [], now)


def tell_merged(
    conn: sqlite3.Connection, config: Config, row: sqlite3.Row, commit: str, now: int
) -> list[tuple[int, Recipient]]:
    cited = f" ({config.auto_merge_decision})" if config.auto_merge_decision else ""
    text = (
        f"{row['url']} ({row['title']}) is merged as {commit[:7] or 'a commit bridge could not read'}: bridge merged "
        f"it under {config.owner_name}'s standing rule{cited}, as a colleague approved it, CI is green, "
        f"{config.review_bot_name} cleared its head and no PR it names is open. Merged is not deployed: moving its "
        "issue, any backport and the deploy follow-ups are yours."
    )
    return tell_owner_or_assistant(conn, config, row, text, now)


def tell_sandbox(conn: sqlite3.Connection, config: Config, row: sqlite3.Row, now: int) -> list[tuple[int, Recipient]]:
    text = (
        f"{row['url']} ({row['title']}) is ready to auto-merge except PERSIST_SANDBOX in its body; remove it (or ask "
        f"{config.owner_name}) and it merges."
    )
    conn.execute("UPDATE review_prs SET sandbox_told_oid = head_oid WHERE ref = ?", (row["ref"],))
    return tell_owner_or_assistant(conn, config, row, text, now)


def tell_owner_or_assistant(
    conn: sqlite3.Connection, config: Config, row: sqlite3.Row, text: str, now: int
) -> list[tuple[int, Recipient]]:
    """Its owning session, else the assistant; with neither reachable the notice is only logged, as the feed line or
    the row already records what happened."""
    owner = find_owner(conn, config, str(row["ref"]), str(row["repo"]), str(row["head"]))
    try:
        if owner is not None:
            return [tell_owner(conn, owner, text, now)]
        return [send_to_handle(conn, config.assistant_handle, f"No session owns it, so it came to you: {text}", now)]
    except AddressError as exc:
        print(f"auto-merge: nobody to tell about {row['ref']}: {exc}", flush=True)
        return []


def tell_failure(
    conn: sqlite3.Connection, config: Config, row: sqlite3.Row, error: str, now: int
) -> list[tuple[int, Recipient]]:
    text = (
        f"Automatic merge of {row['url']} ({row['title']}) at {row['head_oid']} did not end in a merge bridge could "
        f"confirm: {error}. Check the PR. It is not tried again at this head: a new commit, or a merge by hand, "
        "settles it."
    )
    try:
        return [send_to_handle(conn, config.auto_merge_failures_to or config.assistant_handle, text, now)]
    except AddressError:
        title = f"automatic merge failed: {row['ref']}"[:USER_ITEM_MAX_CHARS]
        add_post(
            conn,
            Party(SYSTEM_CONVERSATION, "bridge"),
            "insight",
            title,
            text,
            [FeedLink("PR", str(row["url"]))],
            [],
            now,
        )
        return []


def send_to_handle(conn: sqlite3.Connection, handle: str, text: str, now: int) -> tuple[int, Recipient]:
    recipient = resolve_handle(conn, handle)
    result = send_message(conn, Party(SYSTEM_CONVERSATION, "bridge"), [recipient], "info", text, now)
    conn.execute("UPDATE messages SET post_type = ? WHERE id = ?", (REVIEW_QUEUE, result.message_id))
    return result.message_id, recipient
