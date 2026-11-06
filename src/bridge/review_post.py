import json
import os
import pwd
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

from bridge.board import BODY_MAX_CHARS, Recipient
from bridge.config import Config, state_dir
from bridge.db import now_ms, transaction
from bridge.leads import CLAUDE_PREFIX, REVIEW_DOC_SPEC, find_internal_names, list_internal_names
from bridge.review_worktree import DOC_SHA_CHARS, is_worth_preparing, map_row_to_doc_heads
from bridge.reviews import (
    COMMENT_MAX_CHARS,
    Owner,
    find_lead_of,
    find_owner,
    map_lead_to_owner,
    store_merge_blocks,
    tell_owner,
)

SEND_TOOL = "mcp__claude_ai_Slack__slack_send_message"
SEND_TIMEOUT_S = 180
SEND_ATTEMPTS = 2
CONNECTOR_RETRY_S = 20
SENDING_STALE_MS = 600_000
WEBHOOK_TIMEOUT_S = 30
SENTENCE_MAX_CHARS = 300
DESCRIPTION_MAX_WORDS = 7
NEVER_CALLED = "the send tool was never called"

WAITING_SENTENCE = "waiting-sentence"
WAITING_STACK = "waiting-stack"
WAITING_DRAFT = "waiting-draft"
SENDING = "sending"
POSTED = "posted"
POSTED_NO_LINK = "posted-link-unknown"
DRY_RUN = "dry-run"
FAILED = "failed"
INTERRUPTED = "interrupted"
EXTERNAL = "external"
_WAITING = (None, WAITING_SENTENCE, WAITING_STACK, WAITING_DRAFT)
_DONE = (POSTED, POSTED_NO_LINK, DRY_RUN)

_LINK_RE = re.compile(r"https://[\w.-]+\.slack\.com/archives/[A-Z0-9]+/p\d+[^\s\"'<>)\]\\]*")


class ReviewError(Exception):
    pass


@dataclass(frozen=True)
class SendOutcome:
    state: str
    link: str | None
    detail: str | None


def map_pr_to_slack_text(
    url: str, description: str, direct: bool, mention_ids: list[str], comment: str = "", slack: bool = True
) -> str:
    """'<link> - <description> - direct', then the people they picked and their comment to them. No agent prefix: in
    the review channel the post is theirs. Outside Slack a mention is a plain @handle."""
    text = f"{url} - {' '.join(description.split())}" + (" - direct" if direct else "")
    if not mention_ids:
        return text
    mentions = "".join(f" <@{who}>" if slack else f" @{who}" for who in mention_ids)
    return text + mentions + (f" {comment}" if comment else "")


def map_typed_comment_to_slack(text: str, people: dict[str, str], slack: bool = True) -> str:
    """Unescaped on purpose: they are its only author, so links and <@U…> they type render as Slack renders them. A
    typed @name of someone in the picker becomes a real mention; any other @word stays as typed."""
    comment = text.replace("\r\n", "\n").strip()
    if len(comment) > COMMENT_MAX_CHARS:
        raise ReviewError(f"the comment is {len(comment)} characters; at most {COMMENT_MAX_CHARS}")
    if not people:
        return comment
    ids = {name.casefold(): slack_id for name, slack_id in people.items()}
    names = "|".join(re.escape(name) for name in sorted(people, key=len, reverse=True))

    def map_match_to_mention(match: re.Match[str]) -> str:
        slack_id = ids.get(match.group(1).casefold())
        if slack_id is None:
            return match.group(0)
        return f"<@{slack_id}>" if slack else f"@{slack_id}"

    # Only a standalone @name: inside a link, an address or <@U…> it is part of something else.
    return re.sub(rf"(?<![^\s(\[\"'])@({names})(?!\w)", map_match_to_mention, comment, flags=re.IGNORECASE)


def is_short_description(text: str) -> bool:
    return 0 < len(text.split()) <= DESCRIPTION_MAX_WORDS


def is_published_elsewhere(row: sqlite3.Row) -> bool:
    """Only a known post. Reviewers on a PR do not count: whether it goes to Slack is their call."""
    return row["post_state"] == EXTERNAL


def record_publish(
    conn: sqlite3.Connection,
    ref: str,
    by: str,
    now: int,
    direct: bool = False,
    mention_ids: tuple[str, ...] = (),
    comment: str = "",
) -> None:
    """Their "done reviewing": it stands whatever happens to the post after it. On a draft it also marks it ready;
    bridge leaves the PR on GitHub to its owner, who is told once the post is out. What they picked in the form shapes
    the post until it is sent."""
    row = conn.execute("SELECT * FROM review_prs WHERE ref = ? AND closed_at IS NULL", (ref,)).fetchone()
    if row is None:
        raise ReviewError(f"{ref} is not an open PR in the queue")
    if is_published_elsewhere(row):
        raise ReviewError("already published")
    if row["post_state"] in _WAITING:
        record_post_choice(conn, ref, direct, mention_ids, comment)
    if row["reviewed_at"] is not None:
        return
    conn.execute(
        "UPDATE review_prs SET reviewed_at = ?, reviewed_by = ?, reviewed_oid = ? WHERE ref = ?",
        (now, by, row["head_oid"], ref),
    )
    if row["draft"]:
        _set_ready(conn, ref, by, now)
    store_merge_blocks(conn)


def record_post_choice(
    conn: sqlite3.Connection, ref: str, direct: bool, mention_ids: tuple[str, ...], comment: str = ""
) -> None:
    """A comment speaks to the people mentioned, so without them it is dropped."""
    conn.execute(
        "UPDATE review_prs SET post_direct = ?, post_mentions = ?, post_comment = ? WHERE ref = ?",
        (int(direct), json.dumps(list(mention_ids)), (comment or None) if mention_ids else None, ref),
    )


def _set_ready(conn: sqlite3.Connection, ref: str, by: str, now: int) -> None:
    conn.execute(
        "UPDATE review_prs SET ready_at = coalesce(ready_at, ?), ready_by = coalesce(ready_by, ?) WHERE ref = ?",
        (now, by, ref),
    )


def record_sentence(conn: sqlite3.Connection, config: Config, ref: str, sentence: str, by: str, now: int) -> None:
    row = conn.execute("SELECT title FROM review_prs WHERE ref = ?", (ref,)).fetchone()
    if row is None:
        raise ReviewError(f"{ref} is not in the queue")
    text = " ".join(sentence.split())
    if text.startswith(CLAUDE_PREFIX.split()[0]):
        raise ReviewError(f"no '{CLAUDE_PREFIX}' on Slack: the post is {config.owner_name}'s")
    if not is_short_description(text) or len(text) > SENTENCE_MAX_CHARS:
        raise ReviewError(
            f"at most {DESCRIPTION_MAX_WORDS} words, as the review channel shows '<link> - <description>'; this has "
            f"{len(text.split())}"
        )
    if text.casefold().rstrip(".") == " ".join(str(row["title"]).split()).casefold():
        raise ReviewError("the post already shows the title; say in a few words what the PR does")
    names = list_internal_names(conn, config)
    if found := find_internal_names(text, names):
        raise ReviewError(
            f"colleagues read this post, so it names nothing of ours: not {', '.join(found)} "
            f"(none of {', '.join(names)})"
        )
    conn.execute(
        "UPDATE review_prs SET sentence = ?, sentence_by = ?, sentence_at = ? WHERE ref = ?", (text, by, now, ref)
    )


def record_published_elsewhere(conn: sqlite3.Connection, ref: str, link: str, now: int) -> None:
    cursor = conn.execute(
        "UPDATE review_prs SET post_state = ?, post_link = ?, post_at = ?, post_detail = NULL WHERE ref = ?",
        (EXTERNAL, link, now, ref),
    )
    if cursor.rowcount == 0:
        raise ReviewError(f"{ref} is not in the queue")
    store_merge_blocks(conn)


def reopen_unsent_post(conn: sqlite3.Connection, ref: str) -> None:
    """By a person: after looking in the channel when a sender stopped, or to send for real what a dry run logged.
    A post that reached the send tool is never reopened, whatever became of its link."""
    cursor = conn.execute(
        "UPDATE review_prs SET post_state = NULL, post_detail = NULL WHERE ref = ? AND post_state IN (?, ?)",
        (ref, INTERRUPTED, DRY_RUN),
    )
    if cursor.rowcount == 0:
        raise ReviewError(f"{ref} has no stopped or dry-run post")


def retry_failed_post(conn: sqlite3.Connection, ref: str) -> None:
    cursor = conn.execute(
        "UPDATE review_prs SET post_state = NULL, post_detail = NULL WHERE ref = ? AND post_state = ?", (ref, FAILED)
    )
    if cursor.rowcount == 0:
        raise ReviewError("nothing to retry on this PR")


def advance_posts(conn: sqlite3.Connection, config: Config, now: int) -> tuple[list[str], list[tuple[int, Recipient]]]:
    """(refs claimed for sending, owners to tell). Each ref is claimed here, once, before any sender starts."""
    to_send: list[str] = []
    told: list[tuple[int, Recipient]] = []
    for row in conn.execute(
        """
        SELECT * FROM review_prs
        WHERE closed_at IS NULL AND (reviewed_at IS NOT NULL OR (post_state = 'external' AND post_link IS NOT NULL))
        """
    ).fetchall():
        ref, state = str(row["ref"]), row["post_state"]
        if state == SENDING and now - int(row["post_started_at"] or 0) > SENDING_STALE_MS:
            _set_post(conn, ref, INTERRUPTED, "the sender stopped; look in the channel before posting again")
        elif state in _WAITING:
            next_state = _map_row_to_post_step(row)
            if next_state == SENDING:
                conn.execute(
                    "UPDATE review_prs SET post_state = ?, post_started_at = ?, post_detail = NULL WHERE ref = ?",
                    (SENDING, now, ref),
                )
                to_send.append(ref)
            else:
                _set_post(conn, ref, next_state, None)
                if next_state == WAITING_SENTENCE and row["sentence_asked_at"] is None:
                    told.extend(_ask_for_sentence(conn, config, row, now))
        elif (
            (state in _DONE or (state == EXTERNAL and row["post_link"]))
            and row["reviewers_told_at"] is None
            and row["others_reviewed"] is not None
            and row["post_seen_at"] is None
            and (row["draft"] or needs_reviewers(row) or row["post_direct"])
        ):
            told.extend(_tell_to_request_reviewers(conn, config, row, now))
    return to_send, told


def needs_reviewers(row: sqlite3.Row) -> bool:
    """A person who reviewed may have asked to hold, and GitHub no longer lists them as requested. A post they sent
    direct goes to nobody in particular, so it needs none."""
    return not row["post_direct"] and not row["requested"] and not row["others_reviewed"]


def _map_row_to_post_step(row: sqlite3.Row) -> str:
    if is_published_elsewhere(row):
        return EXTERNAL
    if row["draft"] and not row["ready_at"]:
        return WAITING_DRAFT
    if not row["sentence"] or not is_short_description(str(row["sentence"])):
        return WAITING_SENTENCE
    if row["stack_parent"]:
        return WAITING_STACK
    return SENDING


def _set_post(conn: sqlite3.Connection, ref: str, state: str, detail: str | None) -> None:
    conn.execute("UPDATE review_prs SET post_state = ?, post_detail = ? WHERE ref = ?", (state, detail, ref))


def _ask_for_sentence(
    conn: sqlite3.Connection, config: Config, row: sqlite3.Row, now: int
) -> list[tuple[int, Recipient]]:
    owner = find_owner(conn, config, str(row["ref"]), str(row["repo"]), str(row["head"]))
    if owner is None:
        return []
    conn.execute("UPDATE review_prs SET sentence_asked_at = ? WHERE ref = ?", (now, row["ref"]))
    text = (
        f"{config.owner_name_at_start} is done reviewing {row['url']} ({row['title']}); its review-channel post waits "
        f"for you. Give its description, at most {DESCRIPTION_MAX_WORDS} words, what it does, not its title and "
        f'with no agent prefix: guild review sentence {row["repo"]}#{row["number"]} "…"'
    )
    return [tell_owner(conn, owner, text, now)]


def _tell_to_request_reviewers(
    conn: sqlite3.Connection, config: Config, row: sqlite3.Row, now: int
) -> list[tuple[int, Recipient]]:
    owner = find_owner(conn, config, str(row["ref"]), str(row["repo"]), str(row["head"]))
    if owner is None:
        return []
    conn.execute("UPDATE review_prs SET reviewers_told_at = ? WHERE ref = ?", (now, row["ref"]))
    return [tell_owner(conn, owner, map_row_to_published_text(row, config.owner_name_at_start), now)]


def map_row_to_published_text(row: sqlite3.Row, owner_at_start: str) -> str:
    """Workers hold marking a PR ready as needing the owner's permission, so the message must say their click gave
    it."""
    url = row["url"]
    mark = "mark it ready on GitHub" if row["draft"] else ""
    request = "request its reviewers" if needs_reviewers(row) else ""
    steps = " and ".join(step for step in (mark, request) if step)
    where = row["post_link"] or "in the review channel"
    direct = (
        f" {owner_at_start} sent it direct: it needs no requested reviewers, so request none and do not ask."
        if row["post_direct"]
        else ""
    )
    if not row["reviewed_at"]:
        return f"{url} is published ({where}). Now {steps}, right away, because the post already links there."
    if row["post_state"] == DRY_RUN:
        head = f"{owner_at_start} pressed Publish on {url}. Nothing was posted (a dry run, or no chat set up)."
        return (
            head
            + (f" {owner_at_start}'s click is the permission to {steps}, so do not stop to ask again." if steps else "")
            + direct
        )
    head = f"{owner_at_start} pressed Publish on {url} and the review post is out ({where})."
    if not steps:
        return head + direct
    return (
        f"{head} {owner_at_start}'s click is the permission to {steps}, so do not stop to ask again: do it right away, "
        f"because the post already links there.{direct}"
    )


DOC_HEAD_RULE = (
    'Put <meta name="pr-head" content="<full sha of the PR head it describes>"> in its <head>: bridge shows the doc as '
    "current only while that equals the PR's head, and asks you to update it after each push."
)
TITLE_SLOT = "\x00"


def fit_title(text: str, title: str) -> str:
    """Puts the title where TITLE_SLOT is, cut to what the message limit leaves: a message too long to send would roll
    back every review step of the poll, again each poll."""
    room = BODY_MAX_CHARS - len(text) + len(TITLE_SLOT)
    short = title if len(title) <= room else title[: max(room - 1, 0)].rstrip() + "…"
    return text.replace(TITLE_SLOT, short[: max(room, 0)])[:BODY_MAX_CHARS]


DOC_REVIEWS_RULE = (
    "First run a Codex and a Claude review, read-only, in parallel, and give the doc a section headed Reviews: each "
    "review, its findings one per line, each outcome (`bridge` skill); until it names both, the PR is not ready."
)
DOC_UPDATE_EVERY_MS = 15 * 60_000


def ask_for_doc_updates(conn: sqlite3.Connection, config: Config, now: int) -> list[tuple[int, Recipient]]:
    """One request per new head of a PR whose doc is behind it, at most one every 15 minutes, so a run of pushes
    asks once for where it ended."""
    told: list[tuple[int, Recipient]] = []
    for row in conn.execute(
        """
        SELECT * FROM review_prs WHERE closed_at IS NULL AND doc_file IS NOT NULL
        AND (doc_update_for IS NULL OR doc_update_for != head_oid) AND (doc_update_at IS NULL OR doc_update_at <= ?)
        """,
        (now - DOC_UPDATE_EVERY_MS,),
    ).fetchall():
        at, current = map_row_to_doc_heads(row)
        if current or not is_worth_preparing(row, config, now):
            continue
        owner = find_owner(conn, config, str(row["ref"]), str(row["repo"]), str(row["head"]))
        if owner is None:
            continue
        conn.execute(
            "UPDATE review_prs SET doc_update_for = ?, doc_update_at = ? WHERE ref = ?",
            (row["head_oid"], now, row["ref"]),
        )
        name = f"{row['repo']}#{row['number']}"
        text = (
            f"{row['url']} ({row['title']}) moved to {str(row['head_oid'])[:DOC_SHA_CHARS]}, and "
            f"{config.owner_name}'s review doc is at {at[:DOC_SHA_CHARS] or 'an unknown commit'}. Update it for what "
            f"changed, as {REVIEW_DOC_SPEC} lays out; {config.owner_name} has not reviewed it yet, so no Changes since "
            f"the last review section. {DOC_HEAD_RULE} Keep its Reviews section current. Then register it again: "
            f"guild review doc {name} /path/file.html."
        )
        told.append(tell_owner(conn, owner, text, now))
    return told


CLEANUP_WITHIN_MS = 7 * 24 * 3_600_000


def ask_to_clean_up(conn: sqlite3.Connection, config: Config, now: int) -> list[tuple[int, Recipient]]:
    """Once a PR is merged or closed and its review worktree is gone or kept, its owner cleans up its own, and hears
    why the owner's was kept, since the row leaves the table."""
    told: list[tuple[int, Recipient]] = []
    for row in conn.execute(
        """
        SELECT * FROM review_prs WHERE closed_at IS NOT NULL AND closed_at > ? AND cleanup_told_at IS NULL
        AND (wt_path IS NULL OR (wt_state IN ('dirty', 'failed', 'no-checkout') AND wt_at >= closed_at))
        """,
        (now - CLEANUP_WITHIN_MS,),
    ).fetchall():
        owner = find_owner(conn, config, str(row["ref"]), str(row["repo"]), str(row["head"]))
        if owner is None:
            continue
        conn.execute("UPDATE review_prs SET cleanup_told_at = ? WHERE ref = ?", (now, row["ref"]))
        text = (
            f"{row['url']} ({row['title']}) is merged or closed. Remove your own worktrees and local branch for it, "
            "with git worktree remove and git branch -d, never rm -rf; leave alone any with uncommitted work or "
            "commits not on the remote."
        )
        if row["wt_path"]:
            kept = row["wt_detail"] or row["wt_state"]
            text += f" {config.owner_name_at_start}'s review worktree {row['wt_path']} was kept: {kept}."
        elif row["wt_detail"]:
            text += f" From {config.owner_name}'s review worktree: {row['wt_detail']}."
        told.append(tell_owner(conn, owner, text, now))
    return told


CONFLICTS_EVERY_MS = 3 * 3_600_000
WORK_HOURS = (9, 19)
CONFLICTING = "DIRTY"
BEHIND = "BEHIND"


def is_working_time(now: int, config: Config) -> bool:
    zone = ZoneInfo(config.work_timezone) if config.work_timezone else None
    local = datetime.fromtimestamp(now / 1000, zone)
    return local.weekday() in config.work_days and WORK_HOURS[0] <= local.hour < WORK_HOURS[1]


def ask_to_resolve_conflicts(conn: sqlite3.Connection, config: Config, now: int) -> list[tuple[int, Recipient]]:
    """Every 3 hours of the working week (work_days, work_timezone), the lead owning each PR GitHub calls conflicting,
    or behind where the base must be merged in, is asked once per head to merge it. GitHub reports BEHIND only where
    that is required."""
    if not is_working_time(now, config):
        return []
    poll = conn.execute("SELECT conflicts_checked_at FROM review_poll WHERE id = 1").fetchone()
    if poll is None or now - int(poll["conflicts_checked_at"] or 0) < CONFLICTS_EVERY_MS:
        return []
    conn.execute("UPDATE review_poll SET conflicts_checked_at = ? WHERE id = 1", (now,))
    told: list[tuple[int, Recipient]] = []
    for row in conn.execute(
        """
        SELECT * FROM review_prs p WHERE closed_at IS NULL AND NOT draft AND merge_state IN (?, ?)
        AND (conflict_asked_oid IS NULL OR conflict_asked_oid != head_oid)
        """,
        (CONFLICTING, BEHIND),
    ).fetchall():
        owner = _find_owning_lead(conn, config, row)
        if owner is None:
            continue
        conn.execute(
            "UPDATE review_prs SET conflict_asked_oid = ?, conflict_asked_at = ? WHERE ref = ?",
            (row["head_oid"], now, row["ref"]),
        )
        base, head = row["base"], row["head"]
        what = f"has merge conflicts with {base}" if row["merge_state"] == CONFLICTING else f"is behind {base}"
        text = (
            f"{row['url']} ({row['title']}) {what}. Merge {base} into {head} and resolve it: a merge, never a rebase "
            "or force push; change no meaning without saying so on the PR; let CI run again. Then report the result "
            'in one line: bridge board "…" --type report.'
        )
        told.append(tell_owner(conn, owner, text, now))
    return told


def _find_owning_lead(conn: sqlite3.Connection, config: Config, row: sqlite3.Row) -> Owner | None:
    """The lead above the owning session; the session itself when no lead is live."""
    owner = find_owner(conn, config, str(row["ref"]), str(row["repo"]), str(row["head"]))
    if owner is None or owner.via == "lead":
        return owner
    display, surface = find_lead_of(conn, owner.surface)
    lead = map_lead_to_owner(conn, config, surface, display) if surface else None
    return lead or owner


def ask_to_mark_drafts_ready(conn: sqlite3.Connection, config: Config, now: int) -> list[tuple[int, Recipient]]:
    """A draft is not on the owner's table. The lead that owns it is asked once per draft spell to mark it ready on
    GitHub when it is."""
    told: list[tuple[int, Recipient]] = []
    for row in conn.execute(
        "SELECT * FROM review_prs WHERE closed_at IS NULL AND draft AND reviewed_at IS NULL AND draft_asked_at IS NULL"
    ).fetchall():
        owner = find_owner(conn, config, str(row["ref"]), str(row["repo"]), str(row["head"]))
        if owner is None:
            continue
        conn.execute("UPDATE review_prs SET draft_asked_at = ? WHERE ref = ?", (now, row["ref"]))
        text = (
            f"{row['url']} ({row['title']}) is a draft on GitHub, so it is not on {config.owner_name}'s review table. "
            f"When it is ready for review, mark it ready there: gh pr ready {row['number']} --repo {row['repo']}. If "
            'it is not ready yet, say why in one line: bridge board "…" --type report.'
        )
        told.append(tell_owner(conn, owner, text, now))
    return told


def ask_for_docs(conn: sqlite3.Connection, config: Config, now: int) -> list[tuple[int, Recipient]]:
    """Once per large PR, of whoever owns it now; a PR with no owner is asked for when one appears."""
    told: list[tuple[int, Recipient]] = []
    for row in conn.execute(
        "SELECT * FROM review_prs WHERE closed_at IS NULL AND doc_file IS NULL AND doc_asked_at IS NULL"
    ).fetchall():
        if not is_worth_preparing(row, config, now):
            continue
        owner = find_owner(conn, config, str(row["ref"]), str(row["repo"]), str(row["head"]))
        if owner is None:
            continue
        conn.execute("UPDATE review_prs SET doc_asked_at = ? WHERE ref = ?", (now, row["ref"]))
        name = f"{row['repo']}#{row['number']}"
        text = (
            f"{row['url']} ({TITLE_SLOT}) is large, so {config.owner_name}'s review needs a document. Write a local "
            f"HTML file exactly as {REVIEW_DOC_SPEC} lays it out: essence visible, details folded; its h2 "
            "sections in order, with diagrams; internal names as click terms, no glossary or legend; every Cost and "
            "Risk clear without guessing; each reason marked observed, reproduced or from-code. It informs, it does "
            "not sell. "
            f"{DOC_REVIEWS_RULE} "
            f"Register it: guild review doc {name} /path/file.html. {DOC_HEAD_RULE} "
            f"Also give its review-channel description now, at most {DESCRIPTION_MAX_WORDS} words, what it does, not "
            f'its title: guild review sentence {name} "…"'
        )
        told.append(tell_owner(conn, owner, fit_title(text, str(row["title"])), now))
    return told


def map_events_to_outcome(stdout: str, channel_id: str, text: str) -> SendOutcome:
    """Only tool events count. Without the connector the model has been seen to invent a tool result in its text."""
    calls: dict[object, dict[str, Any]] = {}
    results: dict[object, dict[str, Any]] = {}
    for block in read_event_blocks(stdout):
        if block.get("type") == "tool_use" and block.get("name") == SEND_TOOL:
            calls[block.get("id")] = block
        elif block.get("type") == "tool_result" and block.get("tool_use_id") in calls:
            results[block.get("tool_use_id")] = block
    if not calls:
        return SendOutcome(FAILED, None, f"{NEVER_CALLED}; nothing was posted")
    # A call with no answer may have gone through, so only an explicit error on every call means nothing was sent.
    sent_ids = [call_id for call_id in calls if not (results.get(call_id) or {}).get("is_error")]
    if not sent_ids:
        last = results[list(calls)[-1]]
        return SendOutcome(FAILED, None, f"the send tool returned an error: {str(last.get('content'))[:200]}")
    notes: list[str] = []
    if len(sent_ids) > 1:
        notes.append(f"the send tool went through {len(sent_ids)} times")
    sent: Any = calls[sent_ids[0]].get("input") or {}
    if sent.get("channel_id") != channel_id or sent.get("message") != text:
        notes.append("the text or channel sent differs from what was asked")
    answers = " ".join(str((results.get(call_id) or {}).get("content")) for call_id in sent_ids)
    link = _LINK_RE.search(answers.replace("\\\\/", "/").replace("\\/", "/"))
    if link is None:
        return SendOutcome(POSTED_NO_LINK, None, "; ".join(notes) or "sent; its link could not be read")
    return SendOutcome(POSTED, link.group(0), "; ".join(notes) or None)


def read_event_blocks(stdout: str) -> Iterator[dict[str, Any]]:
    for line in stdout.splitlines():
        try:
            event: Any = json.loads(line)
        except ValueError:
            continue
        message: Any = cast(dict[str, Any], event).get("message") if isinstance(event, dict) else None
        content: Any = cast(dict[str, Any], message).get("content") if isinstance(message, dict) else None
        for block in cast(list[Any], content) if isinstance(content, list) else []:
            if isinstance(block, dict):
                yield cast(dict[str, Any], block)


def build_send_command(config: Config, channel_id: str, text: str) -> list[str]:
    prompt = (
        f"Call {SEND_TOOL} exactly once with exactly this JSON as its input, changing nothing, then stop:\n"
        + json.dumps({"channel_id": channel_id, "message": text}, ensure_ascii=False)
    )
    return [
        shutil.which("claude") or "claude", "-p", prompt,
        "--model", config.review_post_model,
        "--setting-sources", "user",
        "--settings", '{"disableAllHooks":true}',
        "--tools", "",
        "--allowedTools", SEND_TOOL,
        "--permission-mode", "dontAsk",
        "--max-turns", "2",
        "--no-session-persistence",
        "--output-format", "stream-json",
        "--verbose",
    ]  # fmt: skip


def build_send_env() -> dict[str, str]:
    """launchd sets neither USER nor LOGNAME, and without them claude reads as logged out. claude -p starts its turn
    before the claude.ai connectors connect unless told to wait, and the model then only writes the call as text."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "AGTERM_"))}
    user = pwd.getpwuid(os.getuid()).pw_name
    return {**env, "USER": user, "LOGNAME": user, "MCP_CONNECTION_NONBLOCKING": "false"}


def send_to_slack(config: Config, text: str, still_claimed: Callable[[], bool] = lambda: True) -> SendOutcome:
    channel = config.review_channel_id
    if not channel:
        return SendOutcome(FAILED, None, "review_channel_id is not set")
    outcome = SendOutcome(FAILED, None, "not attempted")
    for attempt in range(SEND_ATTEMPTS):
        if attempt:
            # The connector can still be connecting at start; an attempt that never called the tool posted nothing.
            time.sleep(CONNECTOR_RETRY_S)
            if not still_claimed():
                return outcome
        try:
            done = subprocess.run(
                build_send_command(config, channel, text),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                env=build_send_env(),
                cwd=state_dir(),
                stdin=subprocess.DEVNULL,
                timeout=SEND_TIMEOUT_S,
                check=False,
            )
            stdout = done.stdout
        except subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or b"").decode("utf-8", "replace")
        except OSError as exc:
            return SendOutcome(FAILED, None, f"claude did not start: {type(exc).__name__}")
        outcome = map_events_to_outcome(stdout, channel, text)
        if outcome.state != FAILED or NEVER_CALLED not in (outcome.detail or ""):
            return outcome
    return outcome


def send_to_chat(config: Config, text: str, still_claimed: Callable[[], bool] = lambda: True) -> SendOutcome:
    """The chat adapter: Slack through the claude.ai connector, a JSON-lines file, or an incoming webhook."""
    if config.chat_adapter == "slack":
        return send_to_slack(config, text, still_claimed)
    if config.chat_adapter == "file":
        path = Path(config.chat_file).expanduser() if config.chat_file else state_dir() / "chat-posts.log"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as log:
                log.write(json.dumps({"at": now_ms(), "text": text}, ensure_ascii=False) + "\n")
        except OSError as exc:
            return SendOutcome(FAILED, None, f"chat file {path}: {type(exc).__name__}")
        return SendOutcome(POSTED, None, f"written to {path}")
    if config.chat_adapter == "webhook":
        return send_to_webhook(config.chat_webhook_url, text)
    return SendOutcome(FAILED, None, "no chat is set up (config chat)")


def send_to_webhook(url: str, text: str) -> SendOutcome:
    """Slack, Mattermost and Rocket.Chat read "text", Discord reads "content"; each ignores the other."""
    body = json.dumps({"text": text, "content": text}).encode()
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=WEBHOOK_TIMEOUT_S) as response:
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        return SendOutcome(FAILED, None, f"webhook HTTP {exc.code}")
    except (OSError, ValueError) as exc:
        # The URL is a secret: keep only the exception type.
        return SendOutcome(FAILED, None, f"webhook failed: {type(exc).__name__}")
    return SendOutcome(POSTED, None, f"webhook HTTP {status}")


def run_review_post(conn: sqlite3.Connection, config: Config, ref: str, claim: int | None = None) -> SendOutcome | None:
    """The detached sender. It acts only on its own claim, and sends outside any transaction. A sender that was stopped
    and comes back after its claim expired finds a newer claim, or none, and does nothing."""
    row = conn.execute("SELECT * FROM review_prs WHERE ref = ? AND post_state = ?", (ref, SENDING)).fetchone()
    if row is None or (claim is not None and row["post_started_at"] != claim):
        return None
    own_claim = row["post_started_at"]
    # Things can change between the claim and this start, by a poll of GitHub.
    step = _map_row_to_post_step(row)
    if step != SENDING:
        # Only its own claim: a post recorded by hand meanwhile must stay recorded.
        with transaction(conn):
            conn.execute(
                "UPDATE review_prs SET post_state = ? WHERE ref = ? AND post_state = ? AND post_started_at = ?",
                (step, ref, SENDING, own_claim),
            )
        return None
    text = map_pr_to_slack_text(
        str(row["url"]),
        str(row["sentence"]),
        bool(row["post_direct"]),
        json.loads(str(row["post_mentions"] or "[]")),
        str(row["post_comment"] or ""),
        slack=config.chat_adapter not in ("file", "webhook"),
    )
    if config.review_post_dry_run or config.chat_adapter == "none":
        with (state_dir() / "review-posts.log").open("a") as log:
            log.write(
                json.dumps({"at": now_ms(), "ref": ref, "channel": config.review_channel_id, "text": text}) + "\n"
            )
        outcome = SendOutcome(DRY_RUN, None, None)
    else:

        def still_claimed() -> bool:
            current = conn.execute(
                "SELECT post_state, post_started_at FROM review_prs WHERE ref = ?", (ref,)
            ).fetchone()
            return current["post_state"] == SENDING and current["post_started_at"] == own_claim

        if not still_claimed():
            return None
        outcome = send_to_chat(config, text, still_claimed)
    with transaction(conn):
        conn.execute(
            """
            UPDATE review_prs SET post_state = ?, post_link = ?, post_detail = ?, post_at = ?
            WHERE ref = ? AND post_state = ? AND post_started_at = ?
            """,
            (outcome.state, outcome.link, outcome.detail, now_ms(), ref, SENDING, own_claim),
        )
    return outcome


def spawn_review_post(ref: str, claim: int) -> None:
    subprocess.Popen(
        [sys.executable, "-m", "bridge", "_review-post", ref, str(claim)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def map_arg_to_ref(conn: sqlite3.Connection, text: str) -> str:
    """A PR URL, repo#n, org/repo#n or a number that names one PR in the queue."""
    match = re.search(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)", text) or re.fullmatch(
        r"(?:([\w./-]+)#)?#?(\d+)", text.strip()
    )
    if match is None:
        raise ReviewError(f"not a PR: {text}")
    repo, number = match.group(1), int(match.group(2))
    rows = [
        str(r["ref"])
        for r in conn.execute("SELECT ref, repo FROM review_prs WHERE number = ?", (number,))
        if repo is None or str(r["repo"]) == repo or str(r["repo"]).endswith(f"/{repo}")
    ]
    if len(rows) != 1:
        raise ReviewError(f"{text} names {len(rows)} PRs in the queue")
    return rows[0]
