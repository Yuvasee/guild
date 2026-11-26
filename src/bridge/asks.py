import json
import random
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal, cast

from bridge.board import (
    BODY_MAX_CHARS,
    SYSTEM_CONVERSATION,
    USER_CONVERSATION,
    AddressError,
    MessageStateError,
    Party,
    Recipient,
    resolve_handle,
    send_message,
)
from bridge.config import Config
from bridge.feed import TO_ASSISTANT
from bridge.leads import find_lead_of, is_conversation_locked, is_surface_locked
from bridge.pages import find_unready_citations

STATES = ("open", "in_progress", "done_claimed", "verified", "closed")
CLOSED_HOW = ("auto", "owner", "assistant")
"""Who closed it; NULL is a drop by the ask's own session (`guild ask drop`), whose history row names it."""
WAIT_ON = ("owner", "approval", "release", "verifier", "colleague")
WAITABLE = ("open", "in_progress", "done_claimed")
ASK_STALE_HOURS = 4
"""An open or in-progress ask with no update this long nags its owner, then the assistant too; while nothing changes,
again after 1, 2, 4… periods, a day at most. Held while the owner is off, sent once the owner is back."""
ASK_VERIFY_HOURS = 1
"""A claimed-done ask unverified this long nags the same way."""
# Verify closes an ask at once; one verified before that rule closes at the next sweep.
ASK_AUTOCLOSE_HOURS = 0
"""A verified ask from before verification closed it at once closes by itself this long after."""
ASK_CHECK_DAYS = 3
"""An open or in-progress ask with no update this long, waiting on someone or not, asks its owner once a day whether it
is still current, done or obsolete."""
ASK_TO_VERIFY_HOURS = 24
"""A claim unverified this long reminds its verifier once, or the assistant when no live lead can verify it."""
TOLD_TO_VERIFY = "sent to the assistant to verify"
SPOT_CHECK_SAMPLE = 3
SPOT_CHECKED = "sampled for the assistant's spot-check"
ASK_CARD_GRACE_MS = 15 * 60_000
"""A lead marks an ask waiting on the owner, then posts its card: the check waits this long before telling."""
NO_CARD = "told the assistant: no open card on the owner's feed names it"
VERIFIER_GONE_MS = 10 * 60_000
"""A verifier's session that just ended may be a restart: its claims move only once it stayed gone this long."""
HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
Role = Literal["assistant", "lead", "other"]

WORDS_MAX_CHARS = 4000
CLAIM_MIN_CHARS = 15
EVIDENCE_MIN_CHARS = 40
HOW_MIN_CHARS = 20
WHY_MIN_CHARS = 5
REFS_MAX = 10
EVIDENCE_HINT = (
    "Evidence says what the owner would see, and where (the page, the PR, the channel), as it is now. "
    "How says how it was checked, so another session could repeat it: a URL you opened, a screenshot path, or a "
    "command and its output. 'Deployed' or 'tests pass' is not the user-visible result."
)


class AskError(Exception):
    pass


@dataclass(frozen=True)
class Ask:
    id: int
    owner: str
    recorded_by: str
    words: str
    source: str
    refs: tuple[str, ...]
    state: str
    claim: str | None
    claim_by: str | None
    evidence: str | None
    evidence_how: str | None
    evidence_by: str | None
    closed_how: str | None
    reopen_why: str | None
    created_at: int
    started_at: int | None
    done_at: int | None
    verified_at: int | None
    closed_at: int | None
    reopened_at: int | None
    updated_at: int
    last_nag_at: int | None
    nag_count: int
    wait_on: str | None
    wait_why: str | None
    wait_by: str | None
    wait_at: int | None
    wait_until: int | None
    question: bool = False
    """The owner asked it as a question; kept in the ledger."""
    told_at: int | None = None
    """When its verifier's `guild ask summary` told the assistant of its verified result."""
    verifier: str | None = None


@dataclass(frozen=True)
class Actor:
    handle: str
    conversation_id: str
    role: Role


def map_row_to_ask(row: sqlite3.Row) -> Ask:
    return Ask(
        id=int(row["id"]),
        owner=str(row["owner"]),
        recorded_by=str(row["recorded_by"]),
        words=str(row["words"]),
        source=str(row["source"]),
        refs=tuple(json.loads(row["refs"])),
        state=str(row["state"]),
        claim=row["claim"],
        claim_by=row["claim_by"],
        evidence=row["evidence"],
        evidence_how=row["evidence_how"],
        evidence_by=row["evidence_by"],
        closed_how=row["closed_how"],
        reopen_why=row["reopen_why"],
        created_at=int(row["created_at"]),
        started_at=row["started_at"],
        done_at=row["done_at"],
        verified_at=row["verified_at"],
        closed_at=row["closed_at"],
        reopened_at=row["reopened_at"],
        updated_at=int(row["updated_at"]),
        last_nag_at=row["last_nag_at"],
        nag_count=int(row["nag_count"]),
        wait_on=row["wait_on"],
        wait_why=row["wait_why"],
        wait_by=row["wait_by"],
        wait_at=row["wait_at"],
        wait_until=row["wait_until"],
        question=bool(row["question"]),
        told_at=row["told_at"],
        verifier=row["verifier"],
    )


def get_ask(conn: sqlite3.Connection, ask_id: int) -> Ask:
    row = conn.execute("SELECT * FROM asks WHERE id = ?", (ask_id,)).fetchone()
    if row is None:
        raise AskError(f"no ask #{ask_id}")
    return map_row_to_ask(row)


def list_asks(conn: sqlite3.Connection, owner: str | None = None, include_closed: bool = False) -> list[Ask]:
    where = ["1"]
    params: list[object] = []
    if owner is not None:
        where.append("owner = ?")
        params.append(owner)
    if not include_closed:
        where.append("state != 'closed'")
    rows = conn.execute(f"SELECT * FROM asks WHERE {' AND '.join(where)} ORDER BY created_at, id", params)
    return [map_row_to_ask(r) for r in rows]


def add_ask(
    conn: sqlite3.Connection,
    owner: str,
    recorded_by: str,
    words: str,
    source: str,
    refs: list[str],
    at_ms: int,
    question: bool = False,
) -> Ask:
    words = words.strip()
    if not words:
        raise AskError("an ask needs the owner's words")
    if len(words) > WORDS_MAX_CHARS:
        raise AskError(f"the words are over {WORDS_MAX_CHARS} characters; quote the ask, not the whole thread")
    if len(refs) > REFS_MAX:
        raise AskError(f"at most {REFS_MAX} refs")
    _check_not_recorded(conn, source, words)
    cursor = conn.execute(
        """
        INSERT INTO asks (owner, recorded_by, words, source, refs, state, created_at, updated_at, question)
        VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?)
        """,
        (owner, recorded_by, words, source, json.dumps(list(dict.fromkeys(refs))), at_ms, at_ms, int(question)),
    )
    ask_id = int(cursor.lastrowid or 0)
    _log(conn, ask_id, at_ms, recorded_by, "open", None)
    return get_ask(conn, ask_id)


def _check_not_recorded(conn: sqlite3.Connection, source: str, words: str) -> None:
    """The same owner's words twice make two asks that drift apart: one message, or one card's answer, is one ask."""
    if source.startswith("#"):
        row = conn.execute(
            "SELECT id FROM asks WHERE source = ? AND state != 'closed' ORDER BY id LIMIT 1", (source,)
        ).fetchone()
        if row is not None:
            raise AskError(f"message {source} is already ask #{row['id']}: add to it with `guild ask note {row['id']}`")
    cards = sorted({int(n) for n in _WAIT_CARD.findall(words)})
    if not cards:
        return
    row = conn.execute(
        f"SELECT u.id, a.id AS ask_id FROM user_items u JOIN asks a ON a.id = u.ask_id "
        f"WHERE u.id IN ({','.join('?' * len(cards))}) AND a.state != 'closed' ORDER BY u.id LIMIT 1",
        cards,
    ).fetchone()
    if row is not None:
        raise AskError(
            f"Q#{row['id']} is already ask #{row['ask_id']}: add to it with `guild ask note {row['ask_id']}`"
        )


def start_ask(conn: sqlite3.Connection, ask_id: int, by: Actor, at_ms: int) -> Ask:
    ask = get_ask(conn, ask_id)
    _require(ask, ("open",), "start", hint={"in_progress": "it is already in progress; `guild ask note` records news"})
    conn.execute(
        f"UPDATE asks SET state = 'in_progress', started_at = ?, updated_at = ?, {_RESET_NAGS}, {_CLEAR_WAIT} "
        "WHERE id = ?",
        (at_ms, at_ms, ask_id),
    )
    _log(conn, ask_id, at_ms, by.handle, "in_progress", None)
    return get_ask(conn, ask_id)


def note_ask(conn: sqlite3.Connection, ask_id: int, by: Actor, text: str, at_ms: int) -> Ask:
    ask = get_ask(conn, ask_id)
    _require(ask, WAITABLE, "note")
    if len(text.strip()) < WHY_MIN_CHARS:
        raise AskError("a note says what changed")
    conn.execute(f"UPDATE asks SET updated_at = ?, {_RESET_NAGS}, {_CLEAR_WAIT} WHERE id = ?", (at_ms, ask_id))
    _log(conn, ask_id, at_ms, by.handle, None, text.strip())
    return get_ask(conn, ask_id)


def change_ask_refs(
    conn: sqlite3.Connection, ask_id: int, by: Actor, added: list[str], dropped: list[str], at_ms: int
) -> Ask:
    """Only the refs change: a wait stays, since the review-queue rule reads the refs of an ask that waits."""
    ask = get_ask(conn, ask_id)
    _require(ask, WAITABLE, "change the refs of")
    for ref in added:
        if not ref.startswith(("https://", "http://")):
            raise AskError(f"a ref is a URL: {ref!r}")
    missing = [ref for ref in dropped if ref not in ask.refs]
    if missing:
        raise AskError(f"#{ask_id} has no ref {missing[0]}; its refs: {' '.join(ask.refs) or 'none'}")
    refs = list(dict.fromkeys(ref for ref in [*ask.refs, *added] if ref not in dropped))
    if len(refs) > REFS_MAX:
        raise AskError(f"at most {REFS_MAX} refs")
    if refs == list(ask.refs):
        raise AskError(f"#{ask_id} already has these refs")
    conn.execute("UPDATE asks SET refs = ? WHERE id = ?", (json.dumps(refs), ask_id))
    change = [*(f"+{ref}" for ref in refs if ref not in ask.refs), *(f"-{ref}" for ref in dropped)]
    _log(conn, ask_id, at_ms, by.handle, None, "refs: " + " ".join(change))
    return get_ask(conn, ask_id)


def claim_ask_done(conn: sqlite3.Connection, ask_id: int, by: Actor, claim: str, at_ms: int) -> Ask:
    ask = get_ask(conn, ask_id)
    _require(ask, ("open", "in_progress"), "claim done")
    claim = claim.strip()
    if len(claim) < CLAIM_MIN_CHARS:
        raise AskError(f"the claim says what was done, where it lives (PR, URL, deploy): {CLAIM_MIN_CHARS}+ characters")
    if ask.reopened_at is not None and _is_claimed_before(conn, ask_id, claim):
        raise AskError(
            f"#{ask_id} was claimed done with these very words and reopened since: {ask.reopen_why}. "
            "Claim what changed for that reason."
        )
    conn.execute(
        f"""
        UPDATE asks SET state = 'done_claimed', claim = ?, claim_by = ?, claim_conversation = ?, claim_role = ?,
            done_at = ?, updated_at = ?, {_RESET_NAGS}, {_CLEAR_WAIT}
        WHERE id = ?
        """,
        (claim, by.handle, by.conversation_id, by.role, at_ms, at_ms, ask_id),
    )
    _log(conn, ask_id, at_ms, by.handle, "done_claimed", claim)
    return get_ask(conn, ask_id)


def _is_claimed_before(conn: sqlite3.Connection, ask_id: int, claim: str) -> bool:
    words = " ".join(claim.split())
    earlier = conn.execute("SELECT note FROM ask_events WHERE ask_id = ? AND state = 'done_claimed'", (ask_id,))
    return any(" ".join(str(row["note"] or "").split()) == words for row in earlier)


def verify_ask(
    conn: sqlite3.Connection, ask_id: int, by: Actor, evidence: str, how: str, at_ms: int, decision: str = ""
) -> Ask:
    """Verified closes it at once, by the owner's standing rule, cited from the config when it names it; a reopen
    brings it back."""
    ask = get_ask(conn, ask_id)
    _require(ask, ("done_claimed",), "verify")
    row = conn.execute("SELECT claim_conversation FROM asks WHERE id = ?", (ask_id,)).fetchone()
    if by.conversation_id == row["claim_conversation"] or by.handle == ask.claim_by:
        raise AskError(
            f"#{ask_id} was claimed done by {ask.claim_by}, and the session that claimed it cannot verify it: "
            "its verifier (`guild ask show` names it), another lead or the assistant verifies it"
        )
    if by.role == "other":
        raise AskError("only a lead or the assistant verifies; ask the lead that started you to check the claim")
    evidence, how = evidence.strip(), how.strip()
    problems: list[str] = []
    if len(evidence) < EVIDENCE_MIN_CHARS:
        problems.append(f"--evidence is {len(evidence)} characters, under {EVIDENCE_MIN_CHARS}")
    if len(how) < HOW_MIN_CHARS:
        problems.append(f"--how is {len(how)} characters, under {HOW_MIN_CHARS}")
    if evidence and evidence == ask.claim:
        problems.append("--evidence repeats the claim; say what you saw")
    if evidence and evidence == how:
        problems.append("--evidence and --how are the same text")
    if problems:
        raise AskError("; ".join(problems) + f". {EVIDENCE_HINT}")
    conn.execute(
        f"""
        UPDATE asks SET state = 'verified', evidence = ?, evidence_how = ?, evidence_by = ?,
            evidence_conversation = ?, verified_at = ?, updated_at = ?, {_RESET_NAGS}, {_CLEAR_WAIT}
        WHERE id = ?
        """,
        (evidence, how, by.handle, by.conversation_id, at_ms, at_ms, ask_id),
    )
    _log(conn, ask_id, at_ms, by.handle, "verified", f"{evidence}\nhow: {how}")
    cited = f" ({decision})" if decision else ""
    return close_ask(
        conn,
        ask_id,
        "auto",
        "bridge",
        at_ms,
        note=f"closed at once on verification, by the owner's standing rule{cited}",
    )


BATCH_WORDS_CHARS = 90
BATCH_LINK_MAX_CHARS = 300
BATCH_TITLE_CHARS = 20


def mark_ask_told(conn: sqlite3.Connection, ask_id: int, at_ms: int) -> None:
    conn.execute("UPDATE asks SET told_at = coalesce(told_at, ?) WHERE id = ?", (at_ms, ask_id))


def post_verified_batch(conn: sqlite3.Connection, config: Config, by: Party, at_ms: int) -> tuple[int, int, int]:
    """One message to the assistant listing the asks `by` verified that were not told yet, oldest first, as many as
    fit; the rest wait for the next batch. The assistant picks what reaches the owner. Returns the message's number,
    how many it lists and how many wait."""
    rows = conn.execute(
        """
        SELECT * FROM asks WHERE evidence_by = ? AND verified_at IS NOT NULL AND told_at IS NULL
        ORDER BY verified_at, id
        """,
        (by.handle,),
    ).fetchall()
    if not rows:
        raise AskError("no ask you verified is waiting for a batch summary")
    lines: list[str] = []
    listed: list[int] = []
    for ask in map(map_row_to_ask, rows):
        link = next((ref for ref in ask.refs if ref.startswith(("https://", "http://"))), None)
        line = f"- ask #{ask.id}: {_cut(ask.words, BATCH_WORDS_CHARS)}"
        # An overlong link would crowd out every line, and a summary that lists nothing never ends the batch.
        if link and len(link) <= BATCH_LINK_MAX_CHARS:
            line += f" {link}"
        if BATCH_TITLE_CHARS + len("\n".join([*lines, line])) + 1 > BODY_MAX_CHARS:
            break
        lines.append(line)
        listed.append(ask.id)
    count = len(listed)
    title = f"{count} ask{'' if count == 1 else 's'} verified"
    try:
        assistant = resolve_handle(conn, config.assistant_handle)
        if is_conversation_locked(conn, config, assistant.conversation_id):
            raise AskError(f"{assistant.handle}'s tab is locked; the summary waits until it is not")
        sent = send_message(conn, by, [assistant], "info", "\n".join([title, *lines]), at_ms)
        conn.execute("UPDATE messages SET post_type = ? WHERE id = ?", (TO_ASSISTANT, sent.message_id))
    except (MessageStateError, AddressError) as exc:
        raise AskError(str(exc)) from exc
    for ask_id in listed:
        mark_ask_told(conn, ask_id, at_ms)
    return sent.message_id, count, len(rows) - count


def close_ask(
    conn: sqlite3.Connection,
    ask_id: int,
    how: str | None,
    by: str,
    at_ms: int,
    dropped: str | None = None,
    note: str | None = None,
) -> Ask:
    """Verified is the normal way in; a drop closes an unfinished ask only with the reason it was let go. A drop by the
    ask's own session has no `how`: the history row names who dropped it."""
    if how not in CLOSED_HOW and not (how is None and dropped is not None):
        raise ValueError(how)
    ask = get_ask(conn, ask_id)
    if dropped is None:
        _require(
            ask,
            ("verified",),
            "close",
            hint={s: "it is not verified yet; --dropped '<why>' closes it unfinished" for s in STATES[:3]},
        )
    else:
        _require(ask, STATES[:4], "close")
        if len(dropped.strip()) < WHY_MIN_CHARS:
            raise AskError("--dropped says why the ask is let go")
    conn.execute(
        f"UPDATE asks SET state = 'closed', closed_how = ?, closed_at = ?, updated_at = ?, {_RESET_NAGS}, "
        f"{_CLEAR_WAIT} WHERE id = ?",
        (how, at_ms, at_ms, ask_id),
    )
    _log(conn, ask_id, at_ms, by, "closed", f"dropped: {dropped.strip()}" if dropped else note or how)
    return get_ask(conn, ask_id)


def drop_ask(conn: sqlite3.Connection, ask_id: int, by: Actor, why: str, at_ms: int) -> Ask:
    """Its owner session or the assistant lets an ask not yet claimed go as obsolete, with the reason in its history.
    A claim is not dropped: that would skip its verification; a reopen comes first."""
    ask = get_ask(conn, ask_id)
    if by.role != "assistant" and by.handle != ask.owner:
        raise AskError(f"#{ask_id} is {ask.owner}'s: only {ask.owner} or the assistant drops it")
    _require(
        ask,
        ("open", "in_progress"),
        "drop",
        hint={
            "done_claimed": f"it is claimed done; it is verified, or `guild ask reopen {ask_id} --why '…'` first",
            "verified": "it is verified and closes by itself",
        },
    )
    return close_ask(conn, ask_id, "assistant" if by.role == "assistant" else None, by.handle, at_ms, dropped=why)


def tell_assistant_of_drop(
    conn: sqlite3.Connection, config: Config, ask: Ask, why: str, now: int
) -> list[tuple[int, Recipient]]:
    """A lead closing the owner's words never goes unseen: the assistant can reopen it."""
    if is_recipient_locked(conn, config, config.assistant_handle):
        return []
    text = (
        f"{ask.owner} dropped ask #{ask.id} as obsolete ({_cut(map_words_to_sendable(conn, why), 600)}): "
        f'"{_cut(map_words_to_sendable(conn, ask.words), 600)}". If {config.owner_name} still wants it: '
        f"`guild ask reopen {ask.id} --why '…'`."
    )
    return send_from_bridge(conn, config, config.assistant_handle, text, now)


def reopen_ask(conn: sqlite3.Connection, ask_id: int, by: str, why: str, at_ms: int) -> Ask:
    ask = get_ask(conn, ask_id)
    _require(ask, ("done_claimed", "verified", "closed"), "reopen")
    why = why.strip()
    if len(why) < WHY_MIN_CHARS:
        raise AskError("--why says what is still wrong")
    conn.execute(
        f"""
        UPDATE asks SET state = 'open', reopen_why = ?, reopened_at = ?, updated_at = ?, {_RESET_NAGS},
            claim = NULL, claim_by = NULL, claim_conversation = NULL, claim_role = NULL, done_at = NULL,
            evidence = NULL, evidence_how = NULL, evidence_by = NULL, verified_at = NULL, told_at = NULL,
            verifier = NULL, evidence_conversation = NULL,
            closed_how = NULL, closed_at = NULL, {_CLEAR_WAIT}
        WHERE id = ?
        """,
        (why, at_ms, at_ms, ask_id),
    )
    _log(conn, ask_id, at_ms, by, "open", f"reopened: {why}")
    return get_ask(conn, ask_id)


def wait_ask(
    conn: sqlite3.Connection, ask_id: int, by: Actor, on: str | None, why: str, at_ms: int, until: int | None = None
) -> Ask:
    """Its reminders pause until the ask's next start, note, claim, verify, close or reopen, or until `until` passes."""
    if (on is None) == (until is None):
        raise AskError("an ask waits on someone (--on) or pauses until a time (--until)")
    if on is not None and on not in WAIT_ON:
        raise AskError(f"--on is one of {', '.join(WAIT_ON)}")
    if until is not None and until <= at_ms:
        raise AskError("--until is a time still ahead")
    ask = get_ask(conn, ask_id)
    _require(ask, WAITABLE, "wait")
    why = why.strip()
    if len(why) < WHY_MIN_CHARS:
        raise AskError("the why says who or what it waits on, and for what")
    conn.execute(
        f"""
        UPDATE asks SET wait_on = ?, wait_until = ?, wait_why = ?, wait_by = ?, wait_conversation = ?, wait_at = ?,
            updated_at = ?, {_RESET_NAGS}
        WHERE id = ?
        """,
        (on, until, why, by.handle, by.conversation_id, at_ms, at_ms, ask_id),
    )
    what = f"waiting on {on}" if until is None else f"paused until {convert_ms_to_day(until)}"
    _log(conn, ask_id, at_ms, by.handle, None, f"{what}: {why}")
    return get_ask(conn, ask_id)


def map_owner_name_to_wait_on(owner_name: str) -> str | None:
    """The `--on` word for the Guild's owner: their name, lowercased, when it is one word."""
    word = owner_name.strip().lower()
    return word if word and " " not in word and word not in WAIT_ON else None


def map_wait_on_arg_to_stored(value: str, owner_name: str) -> str:
    word = value.strip().lower()
    return "owner" if word == map_owner_name_to_wait_on(owner_name) else word


def is_ask_paused(ask: Ask, now: int) -> bool:
    return ask.wait_until is not None and ask.wait_until > now


def convert_wait_to_text(ask: Ask, owner_name: str, now: int) -> str | None:
    if is_ask_paused(ask, now):
        return f"paused until {convert_ms_to_day(cast(int, ask.wait_until))}"
    if ask.wait_on is None:
        return None
    on = owner_name if ask.wait_on == "owner" else f"{'an' if ask.wait_on == 'approval' else 'a'} {ask.wait_on}"
    return f"waiting on {on}"


def convert_ms_to_day(ms: int) -> str:
    when = datetime.fromtimestamp(ms / 1000)
    day = f"{when:%a} {when.day} {when:%b}"
    return day if (when.hour, when.minute) == (0, 0) else f"{day} {when:%H:%M}"


def list_ask_events(conn: sqlite3.Connection, ask_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM ask_events WHERE ask_id = ? ORDER BY at, id", (ask_id,)).fetchall()


_RESET_NAGS = "last_nag_at = NULL, nag_count = 0"
_CLEAR_WAIT = (
    "wait_on = NULL, wait_until = NULL, wait_why = NULL, wait_by = NULL, wait_conversation = NULL, wait_at = NULL"
)


def _require(ask: Ask, allowed: tuple[str, ...], action: str, hint: dict[str, str] | None = None) -> None:
    if ask.state in allowed:
        return
    reason = (hint or {}).get(ask.state) or f"that needs {' or '.join(allowed)}"
    raise AskError(f"cannot {action} #{ask.id}: it is {ask.state}; {reason}")


def _log(conn: sqlite3.Connection, ask_id: int, at_ms: int, by: str, state: str | None, note: str | None) -> None:
    conn.execute(
        "INSERT INTO ask_events (ask_id, at, by, state, note) VALUES (?, ?, ?, ?, ?)", (ask_id, at_ms, by, state, note)
    )


NAG_GAP_MAX_MS = DAY_MS
_ASK_LINE = re.compile(r"^#(\d+) ", re.MULTILINE)


@dataclass(frozen=True)
class Nag:
    to: str
    text: str
    ask_ids: tuple[int, ...]


def find_overdue_asks(conn: sqlite3.Connection, now: int, stale_ms: int, verify_ms: int) -> list[Ask]:
    """Not paused; not waiting, and open or in progress with no update, or claimed, not verified and with no verifier
    picked; reminded again only when nothing changed for one stale period after the first reminder, two after the
    second, and so on. An open or in-progress ask quiet for days, waiting or not, is due once a day."""
    rows = conn.execute(
        """
        SELECT * FROM asks
        WHERE (wait_until IS NULL OR wait_until <= ?3)
          AND ((wait_on IS NULL AND ((state IN ('open', 'in_progress') AND updated_at <= ?1)
                                     OR (state = 'done_claimed' AND verifier IS NULL AND updated_at <= ?2)))
               OR (state IN ('open', 'in_progress') AND updated_at <= ?4))
        ORDER BY owner, id
        """,
        (now - stale_ms, now - verify_ms, now, now - ASK_CHECK_DAYS * DAY_MS),
    )
    asks = [map_row_to_ask(r) for r in rows]
    return [a for a in asks if a.last_nag_at is None or now - a.last_nag_at >= _compute_ask_nag_gap(a, now, stale_ms)]


def _compute_ask_nag_gap(ask: Ask, now: int, stale_ms: int) -> int:
    return max(stale_ms, NAG_GAP_MAX_MS) if is_ask_due_check(ask, now) else compute_nag_gap(ask.nag_count, stale_ms)


def is_ask_due_check(ask: Ask, now: int) -> bool:
    return (
        ask.state in ("open", "in_progress")
        and not is_ask_paused(ask, now)
        and now - ask.updated_at >= ASK_CHECK_DAYS * DAY_MS
    )


def find_asks_to_verify(conn: sqlite3.Connection, now: int, after_ms: int) -> list[Ask]:
    """Claimed, or given its verifier, this long ago; not verified, waiting on someone or not; and not sent since to
    its verifier, or with none to the assistant: a new verifier, a reopen and a new claim each start the clock again,
    and a verifier dropped for none leaves the claim due to the assistant."""
    rows = conn.execute(
        """
        SELECT * FROM (
          SELECT a.*, coalesce((
            SELECT max(e.at) FROM ask_events e
            WHERE e.ask_id = a.id AND e.by = 'bridge' AND e.state IS NULL AND e.note LIKE 'verifier:%'
              AND e.at >= a.done_at
          ), a.done_at) AS clock
          FROM asks a
          WHERE state = 'done_claimed' AND (wait_until IS NULL OR wait_until <= ?2)
        ) a
        WHERE clock <= ?1
          AND NOT EXISTS (
            SELECT 1 FROM ask_events e
            WHERE e.ask_id = a.id AND e.by = 'bridge' AND e.state IS NULL AND e.at >= a.clock
              AND e.note = 'sent to ' || coalesce(a.verifier, 'the assistant') || ' to verify'
          )
        ORDER BY id
        """,
        (now - after_ms, now),
    )
    return [map_row_to_ask(r) for r in rows]


def compute_nag_gap(nag_count: int, stale_ms: int) -> int:
    return min(stale_ms << min(max(nag_count - 1, 0), 16), max(stale_ms, NAG_GAP_MAX_MS))


def map_overdue_to_assistant_nag(
    asks: list[Ask], to_verify: list[Ask], assistant: str, owner_name: str, now: int, unreached: list[str]
) -> Nag | None:
    """Its own reminders, claims to verify under their own heading, and an ask overdue with a lead only once a reminder
    to that lead went unanswered or reached no one: a lead reminded of an ask that waits on the owner marks it so.
    Claims come only from `to_verify`, so each reaches the assistant once; they are not counted as reminders."""
    own = [ask for ask in asks if ask.owner == assistant and ask.state != "done_claimed"]
    # A waiting ask's daily check is a question to its owner; only an owner nobody could tell hands it on.
    overdue = [
        ask
        for ask in asks
        if ask.owner != assistant
        and ask.state != "done_claimed"
        and (ask.owner in unreached or (ask.nag_count > 0 and ask.wait_on is None))
    ]
    lines = [f"Not told, no live session: {', '.join(unreached)}"] if unreached else []
    for nag in map_asks_to_owner_nags(own, owner_name, now):
        lines.append(nag.text)
    if to_verify:
        lines.append(
            f"To verify, claimed done over {ASK_TO_VERIFY_HOURS}h ago and still not verified, and no live lead can "
            f"verify them; each is sent once ({owner_name}'s words):"
        )
        lines.extend(convert_overdue_to_assistant_line(a, now, assistant) for a in to_verify)
    if overdue:
        lines.append(
            f"Asks overdue: the owner's move, no update for {ASK_STALE_HOURS}h even after a reminder "
            f"({owner_name}'s words; details sent to each owner with a live session):"
        )
        lines.extend(convert_overdue_to_assistant_line(a, now, assistant) for a in overdue)
    if not own and not to_verify and not overdue:
        return None
    return Nag(assistant, "\n".join(lines), tuple(ask.id for ask in own + overdue))


def map_asks_to_owner_nags(asks: list[Ask], owner_name: str, now: int) -> list[Nag]:
    by_owner: dict[str, list[Ask]] = {}
    for ask in asks:
        by_owner.setdefault(ask.owner, []).append(ask)
    nags: list[Nag] = []
    for owner, owned in by_owner.items():
        lines = [f"Asks from {owner_name} waiting on you (`guild ask show N` for the rest):"]
        lines.extend(convert_overdue_to_owner_line(ask, owner, owner_name, now) for ask in owned)
        nags.append(Nag(owner, "\n".join(lines), tuple(ask.id for ask in owned)))
    return nags


def is_claimed_by_owner(ask: Ask) -> bool:
    return ask.state == "done_claimed" and ask.claim_by == ask.owner


def convert_overdue_to_assistant_line(ask: Ask, now: int, assistant: str) -> str:
    line = f"#{ask.id} {ask.owner} {ask.state}, {_convert_wait(ask, now)}: {_cut(ask.words, 80)}"
    if not is_claimed_by_owner(ask):
        return line
    if ask.owner == assistant:
        return (
            f"{line} → you claimed it, so a lead verifies it: ask one, then `guild ask wait {ask.id} --on verifier '…'`"
        )
    verify = f"`guild ask verify {ask.id} --evidence '…' --how '…'`"
    return f"{line} → its owner claimed it, so you or another lead verify: {verify}"


def convert_overdue_to_owner_line(ask: Ask, owner: str, owner_name: str, now: int) -> str:
    head = f'#{ask.id} {ask.state}, {_convert_wait(ask, now)}: "{_cut(ask.words, 160)}"'
    on_owner = map_owner_name_to_wait_on(owner_name) or "owner"
    if is_ask_due_check(ask, now):
        waits = convert_wait_to_text(ask, owner_name, now)
        waits = f" ({waits}: {_cut(ask.wait_why or '', 160)})" if waits else ""
        return (
            f"{head}{waits} → no update for {ASK_CHECK_DAYS} days: is it still current, done or obsolete? "
            f"Current: `guild ask note {ask.id} '<what is left, who waits on whom>'`, or "
            f"`guild ask wait {ask.id} --on … '…'` if it still waits; done: `guild ask done {ask.id} --claim "
            f"'<what was done, where it shows>'`; obsolete: `guild ask drop {ask.id} '<why>'` closes it as dropped"
        )
    if ask.state != "done_claimed":
        return (
            f"{head} → waiting on {owner_name} (review, decision, OK)? `guild ask wait {ask.id} --on {on_owner} '…'` "
            f"stops these reminders; else `guild ask done {ask.id} --claim '…'` when it is done, "
            f"`guild ask note {ask.id} '…'` with news, `guild ask wait {ask.id} --on approval|release '…'`, or "
            f"`guild ask wait {ask.id} --until YYYY-MM-DD[THH:MM] '…'` to pause it"
        )
    if ask.claim_by == owner:
        return (
            f"{head} → you claimed it, so another lead or the assistant verifies it: ask one, then "
            f"`guild ask wait {ask.id} --on verifier '…'`"
        )
    return (
        f"{head} → check what {owner_name} would see, where they would see it, and "
        f"`guild ask verify {ask.id} --evidence '…' --how '…'`, or `guild ask reopen {ask.id} --why '…'`"
    )


def send_ask_nags(conn: sqlite3.Connection, config: Config, now: int, owner_off: bool) -> list[tuple[int, Recipient]]:
    """Held while the owner is off (night word, dreams, or off hours); the morning gets one batch."""
    if owner_off:
        return []
    sent = _send_reminders(conn, config, now)
    sent += send_card_notices(conn, config, now)
    sent += send_spot_check(conn, config, now)
    return sent


def _send_reminders(conn: sqlite3.Connection, config: Config, now: int) -> list[tuple[int, Recipient]]:
    hour = HOUR_MS
    assistant = config.assistant_handle
    replaced = replace_gone_verifiers(conn, config, now)

    def sendable(asks: list[Ask]) -> list[Ask]:
        return [
            replace(
                ask,
                words=map_words_to_sendable(conn, ask.words),
                wait_why=ask.wait_why and map_words_to_sendable(conn, ask.wait_why),
            )
            for ask in asks
            if not is_owner_locked(conn, config, ask.owner)
        ]

    found = find_overdue_asks(conn, now, ASK_STALE_HOURS * hour, ASK_VERIFY_HOURS * hour)
    overdue = sendable([a for a in found if not (a.owner == assistant and a.state == "done_claimed")])
    due_claims = sendable(find_asks_to_verify(conn, now, ASK_TO_VERIFY_HOURS * hour))
    if not overdue and not due_claims:
        return replaced
    sent, to_verify = remind_verifiers(conn, config, due_claims, now)
    sent = replaced + sent
    unreached: list[str] = []
    told: set[int] = set()

    def send(nag: Nag) -> set[int] | None:
        """The asks whose lines were delivered; None when nothing reached the recipient."""
        delivered = send_bodies_from_bridge(conn, config, nag.to, nag.text, now)
        sent.extend((message_id, recipient) for _, message_id, recipient in delivered)
        if not delivered:
            return None
        return {int(i) for body, _, _ in delivered for i in _ASK_LINE.findall(body)}

    for nag in map_asks_to_owner_nags([a for a in overdue if a.owner != assistant], config.owner_name, now):
        lines = send(nag)
        if lines is None:
            unreached.append(nag.to)
        else:
            told.update(lines & set(nag.ask_ids))
    # Last, so it can name every owner that could not be told.
    to_assistant = map_overdue_to_assistant_nag(overdue, to_verify, assistant, config.owner_name, now, unreached)
    if to_assistant is not None and not is_recipient_locked(conn, config, assistant):
        lines = send(to_assistant) or set()
        told.update(lines & set(to_assistant.ask_ids))
        for ask in to_verify:
            if ask.id in lines:
                _log(conn, ask.id, now, "bridge", None, TOLD_TO_VERIFY)
    # An ask nobody was told about stays due, so its back-off does not grow on reminders nobody got.
    mark_nagged(conn, sorted(told), now)
    return sent


@dataclass(frozen=True)
class LiveLead:
    name: str
    handle: str
    surface: str
    conversation_id: str
    agterm_session: str | None


def list_live_leads(conn: sqlite3.Connection, config: Config) -> list[LiveLead]:
    """Not retired, a session running in its tab now, and the tab not locked."""
    rows = conn.execute(
        """
        SELECT l.name, l.surface, s.handle, s.agterm_session, i.conversation_id FROM leads l
        JOIN surfaces s ON s.surface = l.surface
        JOIN instances i ON i.surface = l.surface AND i.ended_at IS NULL AND i.conversation_id IS NOT NULL
        WHERE l.retired_at IS NULL AND s.handle IS NOT NULL
        ORDER BY l.name, i.proc_start_ms DESC
        """
    )
    leads: dict[str, LiveLead] = {}
    for r in rows:
        leads.setdefault(
            str(r["name"]),
            LiveLead(
                str(r["name"]), str(r["handle"]), str(r["surface"]), str(r["conversation_id"]), r["agterm_session"]
            ),
        )
    return [lead for lead in leads.values() if not is_surface_locked(conn, config, lead.surface)]


def pick_verifier(conn: sqlite3.Connection, config: Config, ask: Ask) -> LiveLead | None:
    """A live lead that is neither the ask's owner, nor its claimer, nor the lead above a worker that claimed it: the
    configured verifier_lead when it fits, else fewest open verifications first, then fewest live workers, then by
    name."""
    row = conn.execute("SELECT claim_conversation FROM asks WHERE id = ?", (ask.id,)).fetchone()
    claim_conversation: str | None = row["claim_conversation"] if row else None
    claim_lead = _find_claim_lead_surface(conn, claim_conversation)
    fit = [
        lead
        for lead in list_live_leads(conn, config)
        if ask.owner not in (lead.name, lead.handle)
        and ask.claim_by not in (lead.name, lead.handle)
        and lead.conversation_id != claim_conversation
        and lead.surface != claim_lead
    ]
    if not fit:
        return None
    if chosen := next((lead for lead in fit if lead.name == config.verifier_lead), None):
        return chosen
    open_counts: dict[str, int] = {
        str(r[0]): int(r[1])
        for r in conn.execute(
            "SELECT verifier, count(*) FROM asks WHERE state = 'done_claimed' AND verifier IS NOT NULL "
            "GROUP BY verifier"
        )
    }
    return min(fit, key=lambda lead: (open_counts.get(lead.name, 0), _count_live_workers(conn, lead), lead.name))


def _find_claim_lead_surface(conn: sqlite3.Connection, conversation_id: str | None) -> str | None:
    """The tab of the lead the claiming session is, or works under."""
    if conversation_id is None:
        return None
    row = conn.execute(
        "SELECT surface FROM instances WHERE conversation_id = ? AND surface IS NOT NULL "
        "ORDER BY proc_start_ms DESC LIMIT 1",
        (conversation_id,),
    ).fetchone()
    return find_lead_of(conn, row["surface"])[1] if row else None


def _count_live_workers(conn: sqlite3.Connection, lead: LiveLead) -> int:
    """From the stored tab links, not agterm's tree: a claim must not wait on agterm."""
    if lead.agterm_session is None:
        return 0
    row = conn.execute(
        """
        SELECT count(DISTINCT t.agterm_session) FROM tab_parents t
        JOIN surfaces s ON s.agterm_session = t.agterm_session
        JOIN instances i ON i.surface = s.surface AND i.ended_at IS NULL
        WHERE t.parent_session = ?
        """,
        (lead.agterm_session,),
    ).fetchone()
    return int(row[0])


def set_verifier(conn: sqlite3.Connection, ask_id: int, lead: str | None, note: str, at_ms: int) -> None:
    conn.execute("UPDATE asks SET verifier = ? WHERE id = ?", (lead, ask_id))
    _log(conn, ask_id, at_ms, "bridge", None, note)


VERIFY_RULE = (
    "Check the real artifact (OBSERVED: the PR's state, the page, the Linear issue, an AWS read), never only re-read "
    "the claim, and say in --evidence what you could not check."
)


def convert_ask_to_verifier_lines(ask: Ask, owner_name: str) -> list[str]:
    lines = [
        f"#{ask.id} ({ask.owner}'s ask), {owner_name}'s words: \"{_cut(ask.words, 500)}\"",
        f"  claimed done by {ask.claim_by}: {_cut(ask.claim or '', 400)}",
    ]
    if ask.refs:
        lines.append("  refs: " + " ".join(ask.refs))
    lines.append(
        f"  `guild ask verify {ask.id} --evidence '<what {owner_name} would see, and where>' --how '<the URL, "
        f"command or screenshot you checked it with>'`, or `guild ask reopen {ask.id} --why '<what is still wrong>'`"
    )
    return lines


def send_to_verifier(
    conn: sqlite3.Connection, config: Config, lead: LiveLead, asks: list[Ask], head: str, now: int
) -> tuple[list[tuple[int, Recipient]], set[int]]:
    """One message listing every ask, split only past the body limit; returns what was sent and the asks whose lines
    were delivered."""
    lines = [head, VERIFY_RULE]
    for ask in asks:
        lines.extend(convert_ask_to_verifier_lines(ask, config.owner_name))
    text = map_words_to_sendable(conn, "\n".join(lines))
    delivered = send_bodies_from_bridge(conn, config, lead.handle, text, now)
    told = {int(i) for body, _, _ in delivered for i in _ASK_LINE.findall(body)}
    return [(message_id, recipient) for _, message_id, recipient in delivered], told


def assign_verifiers(
    conn: sqlite3.Connection, config: Config, asks: list[Ask], now: int
) -> tuple[list[tuple[Ask, LiveLead | None]], list[tuple[int, Recipient]]]:
    """Each ask gets a verifier by pick_verifier, picked one after another so the counts include this batch; each
    verifier gets one message. An ask whose verifier could not be told, or that no lead fits, keeps none: the
    assistant has it after a day, as before verifiers."""
    picked: list[tuple[Ask, LiveLead | None]] = []
    by_lead: dict[str, tuple[LiveLead, list[Ask]]] = {}
    for ask in asks:
        if is_owner_hidden(conn, config, ask.owner) or is_claim_locked(conn, config, ask.id):
            continue
        lead = pick_verifier(conn, config, ask)
        if lead is not None:
            set_verifier(conn, ask.id, lead.name, f"verifier: {lead.name}", now)
            by_lead.setdefault(lead.name, (lead, []))[1].append(ask)
        picked.append((ask, lead))
    sent: list[tuple[int, Recipient]] = []
    untold: set[int] = set()
    for lead, owned in by_lead.values():
        count = "a claim" if len(owned) == 1 else f"{len(owned)} claims"
        why = (
            "the configured verifier"
            if lead.name == config.verifier_lead
            else "a live lead other than each ask's owner, its claimer and the claimer's lead, with the fewest open "
            "verifications (the configured verifier is not live)"
            if config.verifier_lead
            else "a live lead other than each ask's owner, its claimer and the claimer's lead, with the fewest open "
            "verifications"
        )
        head = f"You verify {count} on {config.owner_name}'s asks: bridge picked you as {why}."
        out, told = send_to_verifier(conn, config, lead, owned, head, now)
        sent += out
        for ask in owned:
            if ask.id not in told:
                untold.add(ask.id)
                set_verifier(conn, ask.id, None, f"verifier {lead.name} could not be told; none now", now)
    return [(ask, None if ask.id in untold else lead) for ask, lead in picked], sent


def replace_gone_verifiers(conn: sqlite3.Connection, config: Config, now: int) -> list[tuple[int, Recipient]]:
    """A claim not paused whose verifier's tab is locked, or gone for VERIFIER_GONE_MS, gets a new one by the same
    rule, told as on a claim; with none, it has no verifier again, so its owner's reminders and, a day later, the
    assistant's notice come back."""
    live = {lead.name for lead in list_live_leads(conn, config)}
    rows = conn.execute(
        """
        SELECT a.* FROM asks a
        WHERE state = 'done_claimed' AND verifier IS NOT NULL AND (wait_until IS NULL OR wait_until <= ?2)
          AND NOT EXISTS (
            SELECT 1 FROM leads l JOIN instances i ON i.surface = l.surface
            WHERE l.name = a.verifier AND i.ended_at > ?1
          )
        ORDER BY id
        """,
        (now - VERIFIER_GONE_MS, now),
    )
    gone = [ask for ask in map(map_row_to_ask, rows) if ask.verifier not in live]
    for ask in gone:
        set_verifier(conn, ask.id, None, f"verifier {ask.verifier}'s tab is gone or locked", now)
    return assign_verifiers(conn, config, [replace(ask, verifier=None) for ask in gone], now)[1]


def remind_verifiers(
    conn: sqlite3.Connection, config: Config, asks: list[Ask], now: int
) -> tuple[list[tuple[int, Recipient]], list[Ask]]:
    """A claim unverified for a day reminds its verifier once; one whose tab is gone or locked, or a claim with none,
    gets a verifier by the same rule. Returns what was sent and the asks no lead was told of, for the assistant."""
    live = {lead.name: lead for lead in list_live_leads(conn, config)}
    by_lead: dict[str, tuple[LiveLead, list[Ask]]] = {}
    left: list[Ask] = []
    for ask in asks:
        if is_claim_locked(conn, config, ask.id) or is_owner_hidden(conn, config, ask.owner):
            left.append(ask)
            continue
        lead = live.get(ask.verifier or "")
        if lead is None and ask.verifier and _ended_lately(conn, ask.verifier, now):
            continue
        if lead is None:
            lead = pick_verifier(conn, config, ask)
            if lead is not None:
                set_verifier(conn, ask.id, lead.name, f"verifier: {lead.name}", now)
        if lead is None:
            left.append(ask)
        else:
            by_lead.setdefault(lead.name, (lead, []))[1].append(ask)
    sent: list[tuple[int, Recipient]] = []
    for lead, owned in by_lead.values():
        head = (
            f"To verify, claimed done over {ASK_TO_VERIFY_HOURS}h ago and still not verified; you are their verifier "
            "and hear of each once:"
        )
        out, told = send_to_verifier(conn, config, lead, owned, head, now)
        sent += out
        for ask in owned:
            if ask.id in told:
                _log(conn, ask.id, now, "bridge", None, f"sent to {lead.name} to verify")
            else:
                set_verifier(conn, ask.id, None, f"verifier {lead.name} could not be told; none now", now)
                left.append(ask)
    return sent, sorted(left, key=lambda a: a.id)


def is_claim_locked(conn: sqlite3.Connection, config: Config, ask_id: int) -> bool:
    """The claim's words are the claiming session's: a locked one's never reach a verifier."""
    row = conn.execute("SELECT claim_conversation FROM asks WHERE id = ?", (ask_id,)).fetchone()
    return bool(row and row[0]) and is_conversation_locked(conn, config, str(row[0]))


def _is_evidence_locked(conn: sqlite3.Connection, config: Config, ask: Ask) -> bool:
    """By the verifying conversation; one not stored (verified before it was) counts as locked, since a retired
    verifier's handle no longer leads to its tab."""
    return _is_conversation_column_locked(conn, config, ask.id, "evidence_conversation")


def _is_conversation_column_locked(conn: sqlite3.Connection, config: Config, ask_id: int, column: str) -> bool:
    row = conn.execute(f"SELECT {column} FROM asks WHERE id = ?", (ask_id,)).fetchone()
    return not (row and row[0]) or is_conversation_locked(conn, config, str(row[0]))


def list_unassigned_claims(conn: sqlite3.Connection) -> list[Ask]:
    rows = conn.execute("SELECT * FROM asks WHERE state = 'done_claimed' AND verifier IS NULL ORDER BY id")
    return [map_row_to_ask(r) for r in rows]


_CARD_ASK = re.compile(r"\bask #(\d+)\b", re.IGNORECASE)
_WAIT_CARD = re.compile(r"\bQ#(\d+)\b", re.IGNORECASE)
_PR_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
_SHORT_PR = re.compile(r"(?<![\w./#-])(?:([\w.-]+/[\w.-]+)|([\w.-]+))?#(\d+)\b")


def is_waiting_on_owner(ask: Ask, owner_name: str) -> bool:
    """On the owner, or on an approval whose why names the owner: `--on approval` does not say whose."""
    if ask.state not in WAITABLE or ask.wait_on not in ("owner", "approval"):
        return False
    name = owner_name.strip()
    return ask.wait_on == "owner" or bool(
        name and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", ask.wait_why or "", re.IGNORECASE)
    )


def find_asks_without_card(conn: sqlite3.Connection, config: Config, now: int) -> list[Ask]:
    """Waiting on the owner for over ASK_CARD_GRACE_MS, no open blocked or decision card naming "ask #N" in its title
    or body that the Questions column still shows, and not yet told since this wait began."""
    rows = conn.execute(
        """
        SELECT * FROM asks a
        WHERE wait_on IN ('owner', 'approval') AND state IN ('open', 'in_progress', 'done_claimed') AND wait_at <= ?
          AND NOT EXISTS (
            SELECT 1 FROM ask_events e
            WHERE e.ask_id = a.id AND e.by = 'bridge' AND e.state IS NULL AND e.note = ? AND e.at >= a.wait_at
          )
        ORDER BY id
        """,
        (now - ASK_CARD_GRACE_MS, NO_CARD),
    )
    asks = [
        ask
        for ask in map(map_row_to_ask, rows)
        if is_waiting_on_owner(ask, config.owner_name)
        and not is_owner_hidden(conn, config, ask.owner)
        and not is_review_queued(conn, ask)
    ]
    if not asks:
        return []
    from bridge.guildview import STALE_QUESTION_MS  # the page's module; hooks never run this check

    carded: set[int] = set()
    open_cards: set[int] = set()
    for card in conn.execute(
        "SELECT id, from_conversation, text, body, ask_id FROM user_items WHERE kind IN ('blocked', 'decision') "
        "AND resolved_at IS NULL AND (created_at > ? OR id IN (SELECT item_id FROM colleague_calls))",
        (now - STALE_QUESTION_MS,),
    ):
        if card["from_conversation"] and is_conversation_locked(conn, config, str(card["from_conversation"])):
            continue
        carded.update(int(n) for n in _CARD_ASK.findall(f"{card['text']}\n{card['body'] or ''}"))
        if card["ask_id"] is not None:
            carded.add(int(card["ask_id"]))
        open_cards.add(int(card["id"]))
    # A wait reason naming Q#N of such a card stands for it, as `--card Q#N` would.
    named = {ask.id for ask in asks if open_cards & {int(n) for n in _WAIT_CARD.findall(ask.wait_why or "")}}
    return [ask for ask in asks if ask.id not in carded | named]


def map_asks_to_waited_prs(conn: sqlite3.Connection, config: Config) -> dict[str, list[int]]:
    """Per PR ref in lower case, the asks still open that wait on the owner or an approval and name that PR in their
    refs or wait reason: by URL, as owner/repo#N, repo#N (repo in the review org) or a bare #N that only one open
    review PR carries. A short ref counts only when it is an open review PR; `ask #N` and `Q#N` never do."""
    open_prs = [str(r[0]).lower() for r in conn.execute("SELECT ref FROM review_prs WHERE closed_at IS NULL")]
    by_number: dict[str, list[str]] = {}
    for ref in open_prs:
        by_number.setdefault(ref.rpartition("#")[2], []).append(ref)
    org = config.review_org.lower()
    waited: dict[str, list[int]] = {}
    for row in conn.execute(
        f"SELECT id, refs, wait_why FROM asks WHERE wait_on IN ('owner', 'approval') "
        f"AND state IN ({','.join('?' * len(WAITABLE))}) ORDER BY id",
        WAITABLE,
    ):
        text = " ".join([*json.loads(row["refs"]), row["wait_why"] or ""])
        found = {f"pr:{repo.lower()}#{number}" for repo, number in _PR_URL.findall(text)}
        for owner_repo, name, number in _SHORT_PR.findall(_CARD_ASK.sub("", _WAIT_CARD.sub("", text))):
            if name or owner_repo:
                ref = f"pr:{owner_repo or f'{org}/{name}'}#{number}".lower()
                found.update(r for r in open_prs if r == ref)
            elif len(by_number.get(number, [])) == 1:
                found.add(by_number[number][0])
        for ref in sorted(found):
            waited.setdefault(ref, []).append(int(row["id"]))
    return waited


def is_review_queued(conn: sqlite3.Connection, ask: Ask) -> bool:
    """It waits on the owner's review of PRs (named in its refs or wait reason) that are all open in the review
    queue, the owner's other stream: no card needed."""
    text = " ".join([*ask.refs, ask.wait_why or ""])
    prs = {f"pr:{repo.lower()}#{number}" for repo, number in _PR_URL.findall(text)}
    if not prs:
        return False
    queued = {
        str(r[0]).lower()
        for r in conn.execute(
            f"SELECT ref FROM review_prs WHERE closed_at IS NULL AND lower(ref) IN ({','.join('?' * len(prs))})",
            sorted(prs),
        )
    }
    return prs <= queued


def link_card_to_ask(conn: sqlite3.Connection, ask_id: int, item_id: int) -> None:
    """An open question or blocker card already on the owner's feed stands for the ask."""
    cursor = conn.execute(
        "UPDATE user_items SET ask_id = ? WHERE id = ? AND kind IN ('blocked', 'decision') AND resolved_at IS NULL",
        (ask_id, item_id),
    )
    if cursor.rowcount == 0:
        raise AskError(f"Q#{item_id} is no open question or blocker card")


def send_card_notices(conn: sqlite3.Connection, config: Config, now: int) -> list[tuple[int, Recipient]]:
    """Told once per wait; a new wait makes it due again. The card is never posted for anyone."""
    assistant = config.assistant_handle
    if is_recipient_locked(conn, config, assistant):
        return []
    asks = find_asks_without_card(conn, config, now)
    if not asks:
        return []
    owner = config.owner_name
    lines = [
        f"Asks waiting on {owner} with no open card on {owner}'s feed: post one (`bridge post decision` or "
        f'`blocked`) whose title or body names "ask #N", or have its lead post it:'
    ]
    for a in asks:
        # The why is the waiting session's words: left out when its tab is locked or not known.
        locked = _is_conversation_column_locked(conn, config, a.id, "wait_conversation")
        why = "" if locked else f" — {_cut(a.wait_why or '', 200)}"
        lines.append(
            f'#{a.id} {a.owner}, waiting since {convert_ms_to_day(a.wait_at or now)}: "{_cut(a.words, 160)}"{why}'
        )
    text = map_words_to_sendable(conn, "\n".join(lines))
    delivered = send_bodies_from_bridge(conn, config, assistant, text, now)
    told = {int(i) for body, _, _ in delivered for i in _ASK_LINE.findall(body)}
    for ask in asks:
        if ask.id in told:
            _log(conn, ask.id, now, "bridge", None, NO_CARD)
    return [(message_id, recipient) for _, message_id, recipient in delivered]


def send_spot_check(conn: sqlite3.Connection, config: Config, now: int) -> list[tuple[int, Recipient]]:
    """Once a day, a random few of the asks leads verified since the last sample, for the assistant to check again."""
    assistant = config.assistant_handle
    last = conn.execute("SELECT max(at) FROM ask_events WHERE by = 'bridge' AND note = ?", (SPOT_CHECKED,)).fetchone()
    if (last[0] is not None and now - int(last[0]) < DAY_MS) or is_recipient_locked(conn, config, assistant):
        return []
    rows = conn.execute(
        "SELECT * FROM asks WHERE verified_at > ? AND verified_at <= ? AND evidence_by != ? ORDER BY id",
        (now - DAY_MS, now, assistant),
    )
    verified = [
        a
        for a in map(map_row_to_ask, rows)
        if not is_owner_hidden(conn, config, a.owner) and not _is_evidence_locked(conn, config, a)
    ]
    if not verified:
        return []
    sample = sorted(random.sample(verified, min(SPOT_CHECK_SAMPLE, len(verified))), key=lambda a: a.id)
    lines = [
        f"Spot-check: {len(sample)} of the {len(verified)} asks leads verified in the last day, picked at random. Open "
        f"what each evidence names and see whether {config.owner_name} would see it; one that does not hold: "
        "`guild ask reopen N --why '…'`."
    ]
    for ask in sample:
        lines.append(f'#{ask.id} {ask.owner}, verified by {ask.evidence_by}: "{_cut(ask.words, 160)}"')
        lines.append(f"  evidence: {_cut(ask.evidence or '', 300)}")
        lines.append(f"  how: {_cut(ask.evidence_how or '', 200)}")
    text = map_words_to_sendable(conn, "\n".join(lines))
    delivered = send_bodies_from_bridge(conn, config, assistant, text, now)
    told = {int(i) for body, _, _ in delivered for i in _ASK_LINE.findall(body)}
    for ask in sample:
        if ask.id in told:
            _log(conn, ask.id, now, "bridge", None, SPOT_CHECKED)
    return [(message_id, recipient) for _, message_id, recipient in delivered]


def tell_owner_of_reopen(conn: sqlite3.Connection, config: Config, ask: Ask, now: int) -> list[tuple[int, Recipient]]:
    """The owner reopened it on the page: sent as the owner's message, so their reason reaches the session whole."""
    if is_owner_locked(conn, config, ask.owner):
        return []
    try:
        recipient = resolve_handle(conn, ask.owner)
    except AddressError:
        return []
    text = (
        f"{config.owner_name_at_start} reopened ask #{ask.id} ({map_words_to_sendable(conn, ask.reopen_why or '')}): "
        f'"{_cut(map_words_to_sendable(conn, ask.words), 600)}". It is open again; '
        f"`guild ask show {ask.id}` has the earlier claim and evidence."
    )
    owner = Party(USER_CONVERSATION, config.user_alias or "user")
    return [(send_message(conn, owner, [recipient], "info", text, now).message_id, recipient)]


def is_recipient_locked(conn: sqlite3.Connection, config: Config, to: str) -> bool:
    try:
        return is_owner_locked(conn, config, resolve_handle(conn, to).handle)
    except AddressError:
        return False


def is_owner_hidden(conn: sqlite3.Connection, config: Config, owner: str) -> bool:
    """Locked, or its handle no longer names a tab (a retired lead's), so whether it is locked is not known."""
    row = conn.execute("SELECT surface FROM surfaces WHERE handle = ?", (owner,)).fetchone()
    return row is None or is_surface_locked(conn, config, row["surface"])


def _ended_lately(conn: sqlite3.Connection, lead: str, now: int) -> bool:
    """A session of the lead's tab ended under VERIFIER_GONE_MS ago: maybe a restart, so its claims stay."""
    row = conn.execute(
        "SELECT 1 FROM leads l JOIN instances i ON i.surface = l.surface WHERE l.name = ? AND i.ended_at > ?",
        (lead, now - VERIFIER_GONE_MS),
    ).fetchone()
    return row is not None


def is_owner_locked(conn: sqlite3.Connection, config: Config, owner: str) -> bool:
    row = conn.execute("SELECT surface FROM surfaces WHERE handle = ?", (owner,)).fetchone()
    return row is not None and is_surface_locked(conn, config, row["surface"])


def send_from_bridge(
    conn: sqlite3.Connection, config: Config, to: str, text: str, now: int
) -> list[tuple[int, Recipient]]:
    return [
        (message_id, recipient) for _, message_id, recipient in send_bodies_from_bridge(conn, config, to, text, now)
    ]


def send_bodies_from_bridge(
    conn: sqlite3.Connection, config: Config, to: str, text: str, now: int, refused_raises: bool = False
) -> list[tuple[str, int, Recipient]]:
    """Split to fit a message; a message to the assistant is marked so that it is typed into its tab. An unknown
    recipient or a refused body is skipped, or with refused_raises raised."""
    try:
        recipient = resolve_handle(conn, to)
    except AddressError:
        if refused_raises:
            raise
        return []
    sent: list[tuple[str, int, Recipient]] = []
    for body in split_text_to_bodies(text, BODY_MAX_CHARS):
        try:
            result = send_message(conn, Party(SYSTEM_CONVERSATION, "bridge"), [recipient], "info", body, now)
        except MessageStateError:
            if refused_raises:
                raise
            continue
        if to == config.assistant_handle:
            conn.execute("UPDATE messages SET post_type = ? WHERE id = ?", (TO_ASSISTANT, result.message_id))
        sent.append((body, result.message_id, recipient))
    return sent


def map_words_to_sendable(conn: sqlite3.Connection, words: str) -> str:
    """A message citing a /p/ page that opens nothing is refused, so the owner's words name it without the link."""
    for slug in find_unready_citations(conn, words):
        words = re.sub(rf"/p/{re.escape(slug)}(?![a-z0-9-])", f"(page {slug}, no local file)", words)
    return words


def split_text_to_bodies(text: str, limit: int) -> list[str]:
    bodies: list[str] = []
    current = ""
    for line in text.split("\n"):
        line = _cut(line, limit) if len(line) > limit else line
        if current and len(current) + 1 + len(line) > limit:
            bodies.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        bodies.append(current)
    return bodies


def mark_nagged(conn: sqlite3.Connection, ask_ids: list[int], now: int) -> None:
    conn.executemany(
        "UPDATE asks SET last_nag_at = ?, nag_count = nag_count + 1 WHERE id = ?", [(now, i) for i in ask_ids]
    )


def close_settled_asks(conn: sqlite3.Connection, now: int, autoclose_ms: int, decision: str = "") -> list[int]:
    """An ask verified before verify closed it at once closes by itself after the window. The history row cites the
    owner's standing decision when the config names it."""
    ids = [
        int(r[0])
        for r in conn.execute(
            "SELECT id FROM asks WHERE state = 'verified' AND verified_at <= ?", (now - autoclose_ms,)
        )
    ]
    cited = f" ({decision})" if decision else ""
    after = f" {autoclose_ms // 3_600_000} h" if autoclose_ms >= 3_600_000 else ""
    note = f"closed automatically{after} after verification, by the owner's standing rule{cited}"
    for ask_id in ids:
        close_ask(conn, ask_id, "auto", "bridge", now, note=note)
    return ids


def _convert_wait(ask: Ask, now: int) -> str:
    if ask.state == "done_claimed" and ask.done_at is not None:
        return f"claimed by {ask.claim_by} {_convert_ms_to_hours(now - ask.done_at)} ago, not verified"
    return f"no update for {_convert_ms_to_hours(now - ask.updated_at)}"


def _convert_ms_to_hours(ms: int) -> str:
    minutes = max(0, ms) // 60_000
    return f"{minutes}m" if minutes < 60 else f"{minutes // 60}h"


def _cut(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def map_db_to_asks_page(conn: sqlite3.Connection, config: Config, now: int) -> dict[str, object]:
    """Per owner, what is not verified yet; for the owner, only the asks that need attention (waiting on them, a claim
    unverified for a day, overdue), oldest first and at most `asks_panel_max`, and the verified ones they may reopen."""
    hour = HOUR_MS
    stale_ms = ASK_STALE_HOURS * hour
    by_owner: dict[str, dict[str, object]] = {}
    attention: list[dict[str, object]] = []
    verified: list[dict[str, object]] = []
    asks = list_asks(conn)
    locked = {owner for owner in {a.owner for a in asks} if is_owner_locked(conn, config, owner)}
    for ask in asks:
        if ask.owner in locked:
            continue
        overdue = is_ask_overdue(ask, now, stale_ms)
        if ask.state == "verified":
            verified.append(
                {
                    "id": ask.id, "owner": ask.owner, "words": ask.words, "claim": ask.claim,
                    "evidence": ask.evidence, "how": ask.evidence_how, "by": ask.evidence_by,
                    "at": ask.verified_at, "closes_at": (ask.verified_at or now) + ASK_AUTOCLOSE_HOURS * hour,
                }
            )  # fmt: skip
            continue
        wait = _map_ask_to_page_wait(ask, now)
        unverified = (
            ask.state == "done_claimed" and ask.done_at is not None and now - ask.done_at >= ASK_TO_VERIFY_HOURS * hour
        )
        why = "on_you" if ask.wait_on == "owner" else "to_verify" if unverified else "overdue" if overdue else None
        if why is not None:
            attention.append(
                {
                    "id": ask.id, "owner": ask.owner, "words": ask.words, "state": ask.state, "why": why,
                    "claim": ask.claim, "by": ask.claim_by, "at": ask.created_at, "done_at": ask.done_at,
                    "updated_at": ask.updated_at, "wait": wait,
                }
            )  # fmt: skip
        entry = by_owner.setdefault(ask.owner, {"open": 0, "oldest_at": ask.created_at, "overdue": False, "items": []})
        entry["open"] = cast(int, entry["open"]) + 1
        entry["oldest_at"] = min(cast(int, entry["oldest_at"]), ask.created_at)
        entry["overdue"] = bool(entry["overdue"]) or overdue
        item = {"id": ask.id, "state": ask.state, "words": ask.words, "at": ask.created_at, "overdue": overdue}
        cast(list[dict[str, object]], entry["items"]).append({**item, "wait": wait})
    shown = config.asks_panel_max
    return {
        "by_owner": by_owner,
        "attention": attention[:shown],
        "more": max(len(attention) - shown, 0),
        "verified": verified,
    }


def _map_ask_to_page_wait(ask: Ask, now: int) -> dict[str, object] | None:
    if is_ask_paused(ask, now):
        return {"on": None, "until": ask.wait_until, "why": ask.wait_why, "by": ask.wait_by, "at": ask.wait_at}
    if ask.wait_on is None:
        return None
    return {"on": ask.wait_on, "why": ask.wait_why, "by": ask.wait_by, "at": ask.wait_at}


def is_ask_overdue(ask: Ask, now: int, stale_ms: int) -> bool:
    """A claim is never overdue: it waits for a verifier, not for its owner. A wait holds it off for days, not for
    good."""
    if ask.state not in ("open", "in_progress") or is_ask_paused(ask, now):
        return False
    return is_ask_due_check(ask, now) or (ask.wait_on is None and now - ask.updated_at >= stale_ms)
