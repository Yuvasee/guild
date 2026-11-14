import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import cast

from bridge.agent_prefix import CLAUDE_PREFIX as AGENT_PREFIX
from bridge.board import (
    BODY_MAX_CHARS,
    USER_CONVERSATION,
    USER_ITEM_MAX_CHARS,
    MessageStateError,
    Party,
    SendResult,
    send_message,
)
from bridge.config import Config
from bridge.db import transaction
from bridge.errors import UsageError
from bridge.feed import FeedLink, add_post, resolve_tab
from bridge.refs import map_ref_to_label
from bridge.review_github import GitHubError
from bridge.reviews import AGENT, APPROVED, CHANGES_REQUESTED, COLLEAGUE, COMMENT_MAX_CHARS, find_owner

SMALL = "small"
DRAFT = "draft"
FEED_FOR_MS = 14 * 24 * 3_600_000
FEED_MAX = 60
NOTE_MAX_CHARS = 1000
BREACH_LATCHED_FOR_MS = 24 * 3_600_000
QUOTED_PREFIX_CHARS = 40
DRAFT_MAX_CHARS = 8000
QUOTED_LINES = 4
ERROR_MAX_CHARS = 300
UNFINISHED_AFTER_MS = 5 * 60_000
CLOCK_SECOND_MS = 1000
_LINK_ID_RE = re.compile(r"#(?:discussion_r(\d+)|issuecomment-(\d+)|pullrequestreview-(\d+))$")
_ID_RE = re.compile(r"([pir]?)(\d{1,20})")
_MARKDOWN_MARKS_RE = re.compile(r"\*\*|`")
_LINK_PREFIXES = ("p", "i", "r")

UNDECIDED = "undecided"
SUBSTANTIVE = "substantive"
HANDLED = "small"
WAITING = "draft"
POSTING = "posting"
FAILED = "failed"
SENT = "as-is"
EDITED = "edited"
HIMSELF = "himself"
APPROVAL = "approved"
_OPEN_STATUSES = frozenset({UNDECIDED, SUBSTANTIVE, WAITING, FAILED})

_FEED_SQL = """
    SELECT c.ref, c.comment_id, c.created_at, c.body, c.url, c.author, c.state, c.told_at, c.parent_id, c.review_id,
           p.repo, p.number, p.url AS pr_url, p.title, p.closed_at, p.head,
           k.owner_handle, k.owner_lead, k.call, k.note, k.draft, k.called_at, k.called_by, k.item_id, k.choice,
           k.reply, k.chosen_at, k.reply_url, k.reply_error, k.pushed_while_held_at, k.replied_while_held_at
    FROM review_comments c JOIN review_prs p ON p.ref = c.ref
    LEFT JOIN colleague_calls k ON k.ref = c.ref AND k.comment_id = c.comment_id
"""


def map_arg_to_comment_key(conn: sqlite3.Connection, arg: str) -> tuple[str, str]:
    """(ref, comment id) from the comment's GitHub link or its id: p123 inline, i123 conversation, r123 review, or
    the bare number."""
    arg = arg.strip()
    if arg.startswith(("https://", "http://")):
        row = conn.execute("SELECT ref, comment_id FROM review_comments WHERE url = ?", (arg,)).fetchone()
        if row is not None:
            return str(row["ref"]), str(row["comment_id"])
        match = _LINK_ID_RE.search(arg)
        if match is None:
            raise UsageError(f"not a link to a PR comment or review: {arg}")
        candidates = [prefix + digits for prefix, digits in zip(_LINK_PREFIXES, match.groups(), strict=True) if digits]
    else:
        match = _ID_RE.fullmatch(arg)
        if match is None:
            raise UsageError(f"not a comment id or link: {arg}")
        candidates = [arg] if match.group(1) else [prefix + arg for prefix in _LINK_PREFIXES]
    marks = ",".join("?" * len(candidates))
    rows = conn.execute(
        f"SELECT ref, comment_id FROM review_comments WHERE comment_id IN ({marks})", candidates
    ).fetchall()
    if not rows:
        raise UsageError(f"bridge has not read the comment {arg}; it reads open PRs about every 90 seconds")
    if len(rows) > 1:
        raise UsageError(f"{arg} names {len(rows)} comments; use its link")
    return str(rows[0]["ref"]), str(rows[0]["comment_id"])


def load_colleague_comment(conn: sqlite3.Connection, ref: str, comment_id: str) -> sqlite3.Row:
    row = conn.execute(_FEED_SQL + " WHERE c.ref = ? AND c.comment_id = ?", (ref, comment_id)).fetchone()
    verdict = conn.execute(
        "SELECT verdict FROM review_comments WHERE ref = ? AND comment_id = ?", (ref, comment_id)
    ).fetchone()
    if row is None or verdict is None or verdict["verdict"] != COLLEAGUE:
        why = "has no verdict yet" if verdict is not None and verdict["verdict"] is None else "is not a colleague's"
        raise UsageError(f"{comment_id} on {ref} {why}; only a colleague's comment takes a call")
    return row


def record_small_call(conn: sqlite3.Connection, ref: str, comment_id: str, note: str, by: Party, now: int) -> None:
    note = note.strip()
    if not note:
        raise UsageError('say what you did: guild comment small <link> "what I did"')
    if len(note) > NOTE_MAX_CHARS:
        raise UsageError(f"what you did is {len(note)} characters; keep it under {NOTE_MAX_CHARS}")
    row = load_colleague_comment(conn, ref, comment_id)
    if row["state"] == CHANGES_REQUESTED:
        raise UsageError("a change request is substantive: no reply and no push until the owner chooses")
    if row["call"] == DRAFT:
        raise UsageError("this comment already has a draft reply before the owner; it waits for the owner's choice")
    conn.execute(
        """
        INSERT INTO colleague_calls (ref, comment_id, call, note, called_at, called_by, called_conversation)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (ref, comment_id) DO UPDATE SET call = excluded.call, note = excluded.note,
            called_at = excluded.called_at, called_by = excluded.called_by,
            called_conversation = excluded.called_conversation
        """,
        (ref, comment_id, SMALL, note, now, by.handle, by.conversation_id),
    )


def map_row_to_status(row: sqlite3.Row) -> str:
    if row["choice"] is not None:
        if row["choice"] == HIMSELF:
            return HIMSELF
        return str(row["choice"]) if row["reply_url"] else POSTING
    if row["call"] == DRAFT:
        return FAILED if row["reply_error"] else WAITING
    if row["call"] == SMALL:
        return HANDLED
    if row["state"] == CHANGES_REQUESTED:
        return SUBSTANTIVE
    if row["state"] == APPROVED and not str(row["body"]).strip():
        return APPROVAL
    return UNDECIDED


def convert_markdown_to_plain(text: str) -> str:
    return _MARKDOWN_MARKS_RE.sub("", text)


def map_rows_to_feed_entries(
    conn: sqlite3.Connection, config: Config, rows: list[sqlite3.Row]
) -> list[dict[str, object]]:
    """The PR's current owner, as the review queue routes it, only for rows whose wake recorded none: once per PR."""
    owners: dict[str, str | None] = {}
    for row in rows:
        ref = str(row["ref"])
        if row["owner_lead"] or row["owner_handle"] or ref in owners:
            continue
        owner = find_owner(conn, config, ref, str(row["repo"]), str(row["head"]))
        owners[ref] = (owner.lead or owner.handle) if owner else None
    return [map_row_to_feed_entry(row, owners.get(str(row["ref"]))) for row in rows]


def map_row_to_breaches(row: sqlite3.Row) -> list[str]:
    breaches: list[str] = []
    if row["pushed_while_held_at"] is not None:
        breaches.append("pushed while held")
    if row["replied_while_held_at"] is not None:
        breaches.append("replied while held")
    return breaches


def map_row_to_feed_entry(row: sqlite3.Row, current_owner: str | None = None) -> dict[str, object]:
    """`current_owner` is the PR's owner now, shown when the wake recorded none (comments read before it did)."""
    body = convert_markdown_to_plain(str(row["body"])).strip()
    status = map_row_to_status(row)
    breaches = map_row_to_breaches(row)
    pr_open = row["closed_at"] is None
    return {
        "ref": row["ref"],
        "label": map_ref_to_label(str(row["ref"])),
        "pr_url": row["pr_url"],
        "pr_title": row["title"],
        "pr_open": pr_open,
        "comment_id": row["comment_id"],
        "review_id": row["review_id"],
        "parent_id": row["parent_id"],
        "url": row["url"],
        "author": row["author"],
        "review": row["state"],
        "at": row["created_at"],
        "quote": body if len(body) <= COMMENT_MAX_CHARS else body[: COMMENT_MAX_CHARS - 1].rstrip() + "…",
        "status": status,
        "stands_out": bool(breaches) or (pr_open and status in _OPEN_STATUSES),
        "lead": row["owner_lead"] or row["owner_handle"] or current_owner,
        "note": row["note"],
        "draft": row["draft"],
        "reply": row["reply"],
        "reply_url": row["reply_url"],
        "reply_error": row["reply_error"],
        "item_id": row["item_id"],
        "by": row["called_by"],
        "called_at": row["called_at"],
        "chosen_at": row["chosen_at"],
        "breaches": breaches,
    }


def map_db_to_colleague_feed(conn: sqlite3.Connection, config: Config, now: int) -> list[dict[str, object]]:
    """Colleagues' comments and reviews on our PRs, newest first, with the lead's call on each; one still open on an
    open PR stays however old."""
    rows = conn.execute(
        _FEED_SQL + " WHERE c.verdict = ? AND c.created_at > ? ORDER BY c.created_at DESC LIMIT ?",
        (COLLEAGUE, now - FEED_FOR_MS, FEED_MAX),
    ).fetchall()
    entries = map_rows_to_feed_entries(conn, config, rows)
    shown = {(e["ref"], e["comment_id"]) for e in entries}
    entries += [e for e in _list_open_entries(conn, config) if (e["ref"], e["comment_id"]) not in shown]
    mark_needs_attention(conn, entries)
    return sorted(entries, key=lambda e: int(cast(int, e["at"])), reverse=True)


def mark_needs_attention(conn: sqlite3.Connection, entries: list[dict[str, object]]) -> None:
    """What the page shows: a comment or change request on an open PR with no reply and no draft, a draft waiting
    for the owner, a reply that failed to post. An inline comment an agent already answered in its thread is settled,
    and so is a change request once each of its own comments is."""
    waiting: dict[tuple[object, object], bool] = {}
    reviews: list[dict[str, object]] = []
    for entry in entries:
        needs = bool(entry["pr_open"]) and entry["status"] in _OPEN_STATUSES
        if needs and entry["status"] == UNDECIDED and _is_answered_in_thread(conn, entry):
            needs = False
        entry["needs_attention"] = needs
        if str(entry["comment_id"]).startswith("r"):
            reviews.append(entry)
        elif entry["review_id"]:
            key = (entry["ref"], entry["review_id"])
            waiting[key] = waiting.get(key, False) or needs
    for review in reviews:
        key = (review["ref"], review["comment_id"])
        if review["status"] == SUBSTANTIVE and waiting.get(key) is False:
            review["needs_attention"] = False


def _is_answered_in_thread(conn: sqlite3.Connection, entry: dict[str, object]) -> bool:
    if not str(entry["comment_id"]).startswith("p"):
        return False
    root = entry["parent_id"] or entry["comment_id"]
    return (
        conn.execute(
            "SELECT 1 FROM review_comments WHERE ref = ? AND verdict = ? AND parent_id = ? AND created_at > ? LIMIT 1",
            (entry["ref"], AGENT, root, entry["at"]),
        ).fetchone()
        is not None
    )


def list_open_calls(
    conn: sqlite3.Connection, config: Config, me: Party, lead: str | None, everyone: bool, now: int
) -> list[dict[str, object]]:
    """Those still waiting on a lead or on the owner, on open PRs: by default the ones told to this session or its
    lead."""
    return [
        entry
        for entry in _list_open_entries(conn, config)
        if everyone or entry["lead"] in (me.handle, lead) or entry["by"] == me.handle
    ]


def _list_open_entries(conn: sqlite3.Connection, config: Config) -> list[dict[str, object]]:
    rows = conn.execute(
        _FEED_SQL + " WHERE c.verdict = ? AND p.closed_at IS NULL ORDER BY c.created_at DESC", (COLLEAGUE,)
    ).fetchall()
    entries = map_rows_to_feed_entries(conn, config, rows)
    return [entry for entry in entries if entry["status"] in _OPEN_STATUSES]


def latch_held_breaches(conn: sqlite3.Connection, now: int) -> None:
    """A commit GitHub shows on the PR, or an agent's reply to the comment, made while its reply waits for the owner.
    A local commit is no breach until it is pushed; a commit made before the hold and pushed during it is missed."""
    rows = conn.execute(
        _FEED_SQL
        + """
        WHERE c.verdict = ? AND c.told_at IS NOT NULL
          AND (k.call = ? OR (coalesce(k.call, '') != ? AND c.state = ?))
          AND (k.chosen_at IS NULL OR k.chosen_at > ?)
          AND (k.pushed_while_held_at IS NULL OR k.replied_while_held_at IS NULL)
        """,
        (COLLEAGUE, DRAFT, SMALL, CHANGES_REQUESTED, now - BREACH_LATCHED_FOR_MS),
    ).fetchall()
    for row in rows:
        start, end = int(row["told_at"]), int(row["chosen_at"] or now)
        pushed = row["pushed_while_held_at"]
        if pushed is None:
            pushed = conn.execute(
                "SELECT min(committed_at) FROM review_heads WHERE ref = ? AND committed_at > ? AND committed_at < ?",
                (row["ref"], start, end),
            ).fetchone()[0]
        # GitHub dates comments to the second: the owner's own reply can read as up to a second before the choice.
        replied = row["replied_while_held_at"] or _find_reply_while_held(conn, row, start, end - CLOCK_SECOND_MS)
        if pushed is None and replied is None:
            continue
        conn.execute(
            """
            INSERT INTO colleague_calls (ref, comment_id, pushed_while_held_at, replied_while_held_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (ref, comment_id) DO UPDATE SET pushed_while_held_at = excluded.pushed_while_held_at,
                                                        replied_while_held_at = excluded.replied_while_held_at
            """,
            (row["ref"], row["comment_id"], pushed, replied),
        )


def _find_reply_while_held(conn: sqlite3.Connection, row: sqlite3.Row, start: int, end: int) -> int | None:
    """An inline comment's reply is in its thread. A review or conversation comment has none, so an agent's PR
    comment counts only when it quotes the comment: naming the colleague may answer their other, small comment."""
    if str(row["comment_id"]).startswith("p"):
        root = row["parent_id"] or row["comment_id"]
        found = conn.execute(
            """
            SELECT min(created_at) FROM review_comments
            WHERE ref = ? AND verdict = ? AND (parent_id = ? OR comment_id = ?) AND created_at > ? AND created_at < ?
            """,
            (row["ref"], AGENT, root, root, start, end),
        ).fetchone()[0]
        return int(found) if found is not None else None
    quoted = " ".join(str(row["body"]).split())[:QUOTED_PREFIX_CHARS].casefold()
    for reply in conn.execute(
        """
        SELECT created_at, body FROM review_comments
        WHERE ref = ? AND verdict = ? AND substr(comment_id, 1, 1) IN ('i', 'r') AND created_at > ? AND created_at < ?
        ORDER BY created_at
        """,
        (row["ref"], AGENT, start, end),
    ):
        text = " ".join(str(reply["body"]).split()).casefold()
        if quoted and quoted in text:
            return int(reply["created_at"])
    return None


def map_entry_to_line(entry: dict[str, object]) -> str:
    words = {
        UNDECIDED: "undecided",
        SUBSTANTIVE: "changes requested: substantive, draft a reply for the owner",
        WAITING: "draft waits for the owner",
        FAILED: f"posting the reply failed: {entry['reply_error']}",
    }
    review = f" ({entry['review']})" if entry["review"] else ""
    return (
        f"{entry['comment_id']} {entry['label']} {entry['author']}{review}: {words.get(str(entry['status']), '')}\n"
        f"  {entry['url']}\n  {str(entry['quote']).replace(chr(10), ' ')}"
    )


@dataclass(frozen=True)
class ChosenReply:
    ref: str
    comment_id: str
    repo: str
    number: int
    author: str
    url: str
    body: str
    choice: str
    reply: str | None
    item_id: int
    thread_id: str
    tried: str | None = None
    """The text of an earlier try that failed: GitHub may have taken it anyway."""


def record_draft(
    conn: sqlite3.Connection,
    config: Config,
    ref: str,
    comment_id: str,
    draft: str,
    plan: str,
    by: Party,
    now: int,
) -> int:
    """Puts the colleague's words, the draft and the plan before the owner as a Needs-you item; a new draft replaces
    one still waiting. Returns the item's id."""
    draft, plan = _strip_prefix(draft), plan.strip()
    if not draft:
        raise UsageError("the reply file is empty")
    if len(draft) > DRAFT_MAX_CHARS:
        raise UsageError(f"the draft is {len(draft)} characters; keep it under {DRAFT_MAX_CHARS}")
    if not plan:
        raise UsageError('say what you plan to do: --plan "…"')
    if len(plan) > NOTE_MAX_CHARS:
        raise UsageError(f"the plan is {len(plan)} characters; keep it under {NOTE_MAX_CHARS}")
    row = load_colleague_comment(conn, ref, comment_id)
    if row["choice"] is not None:
        raise UsageError(f"{config.owner_name} already chose for this comment ({row['choice']})")
    if row["item_id"] is not None:
        conn.execute(
            "UPDATE user_items SET resolved_at = ? WHERE id = ? AND resolved_at IS NULL", (now, row["item_id"])
        )
    label = map_ref_to_label(ref)
    title = f"Reply to {row['author']} on {label}?"[:USER_ITEM_MAX_CHARS]
    link = FeedLink(f"{row['author']}'s {'review' if comment_id.startswith('r') else 'comment'}", str(row["url"]))
    try:
        item_id = add_post(conn, by, "decision", title, f"Plan: {plan}", [link], [], now)
    except MessageStateError as exc:
        raise UsageError(str(exc)) from exc
    conn.execute(
        """
        INSERT INTO colleague_calls (ref, comment_id, call, note, draft, called_at, called_by, called_conversation,
                                     item_id, reply_error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        ON CONFLICT (ref, comment_id) DO UPDATE SET call = excluded.call, note = excluded.note, draft = excluded.draft,
            called_at = excluded.called_at, called_by = excluded.called_by,
            called_conversation = excluded.called_conversation, item_id = excluded.item_id, reply_error = NULL
        """,
        (ref, comment_id, DRAFT, plan, draft, now, by.handle, by.conversation_id, item_id),
    )
    return item_id


def load_draft_entry(conn: sqlite3.Connection, config: Config, item_id: int) -> dict[str, object] | None:
    row = conn.execute(_FEED_SQL + " WHERE k.item_id = ?", (item_id,)).fetchone()
    return map_rows_to_feed_entries(conn, config, [row])[0] if row is not None else None


def claim_choice(conn: sqlite3.Connection, item_id: int, choice: str, edited: str, now: int) -> ChosenReply:
    """Taken before anything is posted, so a second click cannot post twice."""
    if choice not in (SENT, EDITED, HIMSELF):
        raise UsageError(f"choice is one of {SENT}, {EDITED}, {HIMSELF}")
    row = conn.execute(_FEED_SQL + " WHERE k.item_id = ?", (item_id,)).fetchone()
    if row is None:
        raise UsageError(f"#{item_id} is not a draft reply to a colleague")
    if row["choice"] is not None:
        raise UsageError(f"#{item_id} is already chosen: {row['choice']}")
    reply: str | None = None
    if choice == SENT:
        reply = str(row["draft"])
    elif choice == EDITED:
        reply = _strip_prefix(edited)
        if not reply:
            raise UsageError("the edited reply is empty")
        if len(reply) > DRAFT_MAX_CHARS:
            raise UsageError(f"the reply is {len(reply)} characters; keep it under {DRAFT_MAX_CHARS}")
    conn.execute(
        "UPDATE colleague_calls SET choice = ?, reply = ?, chosen_at = ?, reply_error = NULL WHERE item_id = ?",
        (choice, reply, now, item_id),
    )
    return replace(map_row_to_chosen(row, choice, reply), tried=row["reply"])


def map_row_to_chosen(row: sqlite3.Row, choice: str | None = None, reply: str | None = None) -> ChosenReply:
    return ChosenReply(
        str(row["ref"]), str(row["comment_id"]), str(row["repo"]), int(row["number"]), str(row["author"]),
        str(row["url"]), str(row["body"]), choice or str(row["choice"]), reply if choice else row["reply"],
        int(row["item_id"]), str(row["parent_id"] or row["comment_id"]),
    )  # fmt: skip


def map_chosen_to_github_request(chosen: ChosenReply) -> tuple[str, str]:
    """(API path, body): an inline comment is answered in its thread, through the thread's first comment as GitHub
    requires; a review or conversation comment has no thread, so the answer is a PR comment that quotes it."""
    reply = str(chosen.reply)
    if chosen.comment_id.startswith("p"):
        return (
            f"repos/{chosen.repo}/pulls/{chosen.number}/comments/{chosen.thread_id[1:]}/replies",
            f"{AGENT_PREFIX} {reply}",
        )
    kind = "review" if chosen.comment_id.startswith("r") else "comment"
    lines = [line for line in chosen.body.strip().splitlines() if line.strip()][:QUOTED_LINES]
    quote = "\n".join("> " + line for line in lines)
    if len(quote) > COMMENT_MAX_CHARS:
        quote = quote[: COMMENT_MAX_CHARS - 1].rstrip() + "…"
    head = f"{AGENT_PREFIX} @{chosen.author}, on [your {kind}]({chosen.url}):"
    return f"repos/{chosen.repo}/issues/{chosen.number}/comments", "\n\n".join(p for p in (head, quote, reply) if p)


def find_posted_reply(chosen: ChosenReply, run: Callable[[list[str]], str]) -> str | None:
    """The link of this very reply if GitHub already has it: a timed-out post may have gone through."""
    body = map_chosen_to_github_request(chosen)[1]
    if not body.strip():
        return None
    kind = "pulls" if chosen.comment_id.startswith("p") else "issues"
    listing = f"repos/{chosen.repo}/{kind}/{chosen.number}/comments?per_page=100"
    try:
        pages: object = json.loads(run(["api", "--paginate", "--slurp", listing]))
    except ValueError as exc:
        raise GitHubError("unexpected answer from GitHub while looking for the reply") from exc
    if not isinstance(pages, list):
        raise GitHubError("unexpected answer from GitHub while looking for the reply")
    for page in cast(list[object], pages):
        for comment in cast(list[dict[str, object]], page if isinstance(page, list) else []):
            # Not by thread: GitHub files a reply to a reply under the thread's first comment.
            if str(comment.get("body") or "").strip() != body.strip():
                continue
            url = comment.get("html_url")
            if isinstance(url, str) and url:
                return url
    return None


def post_reply(chosen: ChosenReply, run: Callable[[list[str]], str]) -> tuple[str, str]:
    """Posts on GitHub; returns the comment's link and the text it carries. A try after a failure first looks for
    the failed one on GitHub, so it never posts a second reply: if that one went through, it is the answer."""
    for text in dict.fromkeys(t for t in (chosen.tried, chosen.reply) if t):
        if (found := find_posted_reply(replace(chosen, reply=text), run)) is not None:
            return found, text
    path, body = map_chosen_to_github_request(chosen)
    answer = run(["api", "-X", "POST", path, "-f", f"body={body}"])
    try:
        url = json.loads(answer).get("html_url")
    except (ValueError, AttributeError) as exc:
        raise GitHubError("GitHub's answer has no link to the comment") from exc
    if not isinstance(url, str) or not url:
        raise GitHubError("GitHub's answer has no link to the comment")
    return url, str(chosen.reply)


def release_failed_choice(conn: sqlite3.Connection, item_id: int, error: str) -> None:
    """The card comes back with the error. If GitHub took the post anyway, sending again finds it and posts nothing."""
    conn.execute(
        "UPDATE colleague_calls SET choice = NULL, chosen_at = NULL, reply_error = ? WHERE item_id = ?",
        (error[:ERROR_MAX_CHARS], item_id),
    )


def finish_choice(
    conn: sqlite3.Connection, config: Config, chosen: ChosenReply, reply_url: str | None, user: str, now: int
) -> SendResult:
    """Closes the owner's item and tells the lead that drafted it what was chosen and the final text."""
    if reply_url is not None:
        conn.execute(
            "UPDATE colleague_calls SET reply_url = ?, reply = ? WHERE item_id = ?",
            (reply_url, chosen.reply, chosen.item_id),
        )
    row = conn.execute(
        "SELECT called_by, called_conversation, note FROM colleague_calls WHERE item_id = ?", (chosen.item_id,)
    ).fetchone()
    owner = config.owner_name_at_start
    if chosen.choice == HIMSELF:
        answer = "I'll answer myself"
        body = (
            f"{owner} answers {chosen.author}'s comment {chosen.url} on GitHub themselves. Do not reply to it; that "
            "reply reaches you like any of their comments and says what to do next."
        )
    else:
        answer = ("Send as is" if chosen.choice == SENT else "Edited and sent") + f": {reply_url}"
        how = "chose to send your draft as is" if chosen.choice == SENT else "edited your draft before sending"
        head = (
            f"{owner} {how}: bridge posted the reply to {chosen.author}'s comment {chosen.url} as {reply_url}. The "
            f"hold on it is over; go on with your plan ({row['note']})"
            + (", adjusted to the edited text." if chosen.choice == EDITED else ".")
            + "\n\nFinal text:\n"
        )
        room = BODY_MAX_CHARS - len(head)
        text = str(chosen.reply)
        body = head + (text if len(text) <= room else text[: room - 1] + "…")
    conn.execute(
        """
        UPDATE user_items SET answer = ?, answered_at = ?, resolved_at = coalesce(resolved_at, ?) WHERE id = ?
        """,
        (answer, now, now, chosen.item_id),
    )
    recipient = resolve_tab(conn, str(row["called_conversation"]), str(row["called_by"]))
    sent = send_message(conn, Party(USER_CONVERSATION, user), [recipient], "info", body, now)
    conn.execute("UPDATE user_items SET answer_message_id = ? WHERE id = ?", (sent.message_id, chosen.item_id))
    return sent


def _strip_prefix(text: str) -> str:
    text = text.strip()
    return text.removeprefix(AGENT_PREFIX).strip() if text.startswith(AGENT_PREFIX) else text


def list_unfinished_choices(conn: sqlite3.Connection, now: int) -> list[ChosenReply]:
    """Chosen but never closed: serve stopped between the click and the end, or the write after the post failed."""
    rows = conn.execute(
        _FEED_SQL
        + """
        JOIN user_items u ON u.id = k.item_id
        WHERE k.choice IS NOT NULL AND u.answered_at IS NULL AND k.chosen_at < ?
        """,
        (now - UNFINISHED_AFTER_MS,),
    ).fetchall()
    return [map_row_to_chosen(row) for row in rows]


def settle_unfinished_choices(
    conn: sqlite3.Connection, config: Config, run: Callable[[list[str]], str], user: str, now: int
) -> list[SendResult]:
    """GitHub is only read here: a reply it has is finished with its link, one it lacks goes back to the owner."""
    sent: list[SendResult] = []
    for chosen in list_unfinished_choices(conn, now):
        url = None
        if chosen.choice != HIMSELF:
            try:
                url = find_posted_reply(chosen, run)
            except GitHubError as exc:
                print(f"checking the reply to {chosen.url} failed: {exc}", flush=True)
                continue
        with transaction(conn):
            if url is None and chosen.choice != HIMSELF:
                release_failed_choice(conn, chosen.item_id, "bridge found no such reply on GitHub; nothing was posted")
            else:
                sent.append(finish_choice(conn, config, chosen, url, user, now))
    return sent
