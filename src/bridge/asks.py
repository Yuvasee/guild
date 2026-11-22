import json
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal, cast

from bridge.board import (
    BODY_MAX_CHARS,
    SYSTEM_CONVERSATION,
    AddressError,
    MessageStateError,
    Party,
    Recipient,
    resolve_handle,
    send_message,
)
from bridge.config import Config
from bridge.feed import TO_ASSISTANT
from bridge.leads import is_surface_locked
from bridge.pages import find_unready_citations

STATES = ("open", "in_progress", "done_claimed", "verified", "closed")
CLOSED_HOW = ("auto", "owner", "assistant")
"""Who closed it; NULL is a drop by the ask's own session (`guild ask drop`), whose history row names it."""
WAIT_ON = ("owner", "approval", "release", "verifier")
WAITABLE = ("open", "in_progress", "done_claimed")
ASK_STALE_HOURS = 4
"""An open or in-progress ask with no update this long nags its owner, then the assistant too; while nothing changes,
again after 1, 2, 4… periods, a day at most. Held while the owner is off, sent once the owner is back."""
ASK_VERIFY_HOURS = 1
"""A claimed-done ask unverified this long nags the same way."""
ASK_AUTOCLOSE_HOURS = 24
"""A verified ask closes by itself this long after, unless the owner reopens it."""
ASK_CHECK_DAYS = 3
"""An open or in-progress ask with no update this long, waiting on someone or not, asks its owner once a day whether it
is still current, done or obsolete."""
ASK_TO_VERIFY_HOURS = 24
"""A claim unverified this long reaches the assistant once, under "To verify"."""
TOLD_TO_VERIFY = "sent to the assistant to verify"
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
    conn: sqlite3.Connection, owner: str, recorded_by: str, words: str, source: str, refs: list[str], at_ms: int
) -> Ask:
    words = words.strip()
    if not words:
        raise AskError("an ask needs the owner's words")
    if len(words) > WORDS_MAX_CHARS:
        raise AskError(f"the words are over {WORDS_MAX_CHARS} characters; quote the ask, not the whole thread")
    if len(refs) > REFS_MAX:
        raise AskError(f"at most {REFS_MAX} refs")
    cursor = conn.execute(
        """
        INSERT INTO asks (owner, recorded_by, words, source, refs, state, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'open', ?, ?)
        """,
        (owner, recorded_by, words, source, json.dumps(list(dict.fromkeys(refs))), at_ms, at_ms),
    )
    ask_id = int(cursor.lastrowid or 0)
    _log(conn, ask_id, at_ms, recorded_by, "open", None)
    return get_ask(conn, ask_id)


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


def claim_ask_done(conn: sqlite3.Connection, ask_id: int, by: Actor, claim: str, at_ms: int) -> Ask:
    ask = get_ask(conn, ask_id)
    _require(ask, ("open", "in_progress"), "claim done")
    claim = claim.strip()
    if len(claim) < CLAIM_MIN_CHARS:
        raise AskError(f"the claim says what was done, where it lives (PR, URL, deploy): {CLAIM_MIN_CHARS}+ characters")
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


def verify_ask(conn: sqlite3.Connection, ask_id: int, by: Actor, evidence: str, how: str, at_ms: int) -> Ask:
    ask = get_ask(conn, ask_id)
    _require(ask, ("done_claimed",), "verify")
    row = conn.execute("SELECT claim_conversation FROM asks WHERE id = ?", (ask_id,)).fetchone()
    if by.conversation_id == row["claim_conversation"] or by.handle == ask.claim_by:
        raise AskError(
            f"#{ask_id} was claimed done by {ask.claim_by}, and the session that claimed it cannot verify it: "
            "a lead verifies its worker's claim; the assistant or another lead verifies a lead's"
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
        UPDATE asks SET state = 'verified', evidence = ?, evidence_how = ?, evidence_by = ?, verified_at = ?,
            updated_at = ?, {_RESET_NAGS}, {_CLEAR_WAIT}
        WHERE id = ?
        """,
        (evidence, how, by.handle, at_ms, at_ms, ask_id),
    )
    _log(conn, ask_id, at_ms, by.handle, "verified", f"{evidence}\nhow: {how}")
    return get_ask(conn, ask_id)


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
            evidence = NULL, evidence_how = NULL, evidence_by = NULL, verified_at = NULL,
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
        UPDATE asks SET wait_on = ?, wait_until = ?, wait_why = ?, wait_by = ?, wait_at = ?, updated_at = ?,
            {_RESET_NAGS}
        WHERE id = ?
        """,
        (on, until, why, by.handle, at_ms, at_ms, ask_id),
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
_CLEAR_WAIT = "wait_on = NULL, wait_until = NULL, wait_why = NULL, wait_by = NULL, wait_at = NULL"


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
    """Not paused; not waiting, and open or in progress with no update, or claimed and not verified; reminded again
    only when nothing changed for one stale period after the first reminder, two after the second, and so on. An open
    or in-progress ask quiet for days, waiting or not, is due once a day."""
    rows = conn.execute(
        """
        SELECT * FROM asks
        WHERE (wait_until IS NULL OR wait_until <= ?3)
          AND ((wait_on IS NULL AND ((state IN ('open', 'in_progress') AND updated_at <= ?1)
                                     OR (state = 'done_claimed' AND updated_at <= ?2)))
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
    """Claimed this long ago and not verified, waiting on someone or not, and not yet sent to the assistant since that
    claim: a reopen and a new claim make it due again."""
    rows = conn.execute(
        """
        SELECT * FROM asks a
        WHERE state = 'done_claimed' AND done_at <= ? AND (wait_until IS NULL OR wait_until <= ?)
          AND NOT EXISTS (
            SELECT 1 FROM ask_events e
            WHERE e.ask_id = a.id AND e.by = 'bridge' AND e.state IS NULL AND e.note = ? AND e.at >= a.done_at
          )
        ORDER BY id
        """,
        (now - after_ms, now, TOLD_TO_VERIFY),
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
            f"To verify, claimed done over {ASK_TO_VERIFY_HOURS}h ago and still not verified; each is sent once "
            f"({owner_name}'s words):"
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
    hour = HOUR_MS
    assistant = config.assistant_handle

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
    to_verify = sendable(find_asks_to_verify(conn, now, ASK_TO_VERIFY_HOURS * hour))
    if not overdue and not to_verify:
        return []
    sent: list[tuple[int, Recipient]] = []
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


def tell_owner_of_reopen(conn: sqlite3.Connection, config: Config, ask: Ask, now: int) -> list[tuple[int, Recipient]]:
    if is_owner_locked(conn, config, ask.owner):
        return []
    text = (
        f"{config.owner_name_at_start} reopened ask #{ask.id} ({_cut(ask.reopen_why or '', 600)}): "
        f'"{_cut(map_words_to_sendable(conn, ask.words), 600)}". It is open again; '
        f"`guild ask show {ask.id}` has the earlier claim and evidence."
    )
    return send_from_bridge(conn, config, ask.owner, text, now)


def is_recipient_locked(conn: sqlite3.Connection, config: Config, to: str) -> bool:
    try:
        return is_owner_locked(conn, config, resolve_handle(conn, to).handle)
    except AddressError:
        return False


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
    """Verified is the normal end; it closes by itself unless reopened within the window. The history row cites the
    owner's standing decision when the config names it."""
    ids = [
        int(r[0])
        for r in conn.execute(
            "SELECT id FROM asks WHERE state = 'verified' AND verified_at <= ?", (now - autoclose_ms,)
        )
    ]
    cited = f" ({decision})" if decision else ""
    note = f"closed automatically {autoclose_ms // 3_600_000} h after verification, by the owner's standing rule{cited}"
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
