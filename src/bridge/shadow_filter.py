"""A wake-up filter for messages to team leads: decides whether a message must reach its lead now or can wait for a
batch, and logs each decision. In shadow mode that is all it does, after the message went out as usual. Live
(shadow_filter_live), a message a rule does not send at once may not wake its lead's tab until the filter lets it
through; one it batches waits for the lead's next message that goes now, or for its hold to run out. A lead's own
tool hooks still hand it over: only the wake-up waits."""

import json
import re
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import cast

from bridge.board import SYSTEM_CONVERSATION, USER_CONVERSATION
from bridge.classifier import call_model, quotes_customer, scrub
from bridge.config import Config, state_dir
from bridge.db import transaction

EVERY_S = 10
LIVE_EVERY_S = 2
"""Live, a message the model lets through waits for this tick, so it is short."""
LIVE_MAX_PER_TICK = 10
"""Live, a tick judges at most this many messages, so a backlog of model calls does not hold up expired holds."""
REWAKE_AFTER_MS = 60_000
CALL_TIMEOUT_S = 3.0
CATCH_UP_MS = 3_600_000
"""After a restart, messages older than this are left unjudged, so a long stop does not end in a burst of calls."""
LOG_NAME = "shadow-filter.jsonl"
LOG_MAX_BYTES = 4_000_000
"""Past this the log moves to .1, replacing the one before: a week of a busy team is about 1 MB."""
BODY_MAX_CHARS = 1500
NOW = "now"
BATCH = "batch"
NOW_KINDS = frozenset({"request", "reply"})
NOW_POST_TYPES = frozenset({"question", "blocked-tab", "turn-cut"})
_URGENT_RE = re.compile(r"\b(URGENT|STOP)\b")
TASK = (
    "You triage messages delivered to an AI team lead: a Claude Code session that coordinates worker sessions on a "
    "software team. Each message would wake the lead and cost a full turn. Decide whether the lead must react NOW, "
    "or whether the message can WAIT and be delivered together with the lead's next turn. "
    "NOW: it asks the lead a question or for a decision, gives the lead an instruction or the owner's words, reports "
    "a finished task the lead must review, merge or pass on, reports a failure, an alert or a blocker, or asks the "
    "lead to do something (fix review comments, update a doc, clean up). "
    "WAIT: information only (deploy notices, status updates, CI results with nothing to do, acknowledgements like "
    "'ok' or 'thanks'), notices about work another session already handles, or news meant for someone else."
)
QUESTIONS = {
    "reaction": {
        "type": "choice",
        "instructions": TASK + " Does this message need the lead to react now?",
        "criteria": {
            "now": "The lead must react now: a question, decision, instruction, finished work to handle, failure, "
            "alert, blocker or a request to do something.",
            "wait": "It can wait for the next turn: information only, acknowledgement, deploy or status notice, work "
            "another session handles, or news for someone else.",
        },
    }
}


@dataclass(frozen=True)
class LeadMessage:
    id: int
    from_conversation: str
    from_handle: str
    kind: str
    post_type: str | None
    board: bool
    body: str
    leads: tuple[tuple[str, str], ...]
    """(lead handle, conversation) of each team lead it went to."""


@dataclass(frozen=True)
class Verdict:
    p_now: float | None
    error: str | None
    latency_ms: int


@dataclass(frozen=True)
class Decision:
    source: str
    """rule, model or skipped (it quotes a customer's words, which never leave this machine)."""
    decision: str
    rule: str | None = None
    p_now: float | None = None
    latency_ms: int | None = None
    error: str | None = None


Ask = Callable[[str], Verdict]


def find_now_rule(message: LeadMessage, config: Config) -> str | None:
    """What makes a message important enough to go at once without asking a model, if anything."""
    if message.from_conversation == USER_CONVERSATION:
        return "owner"
    if message.from_handle == config.assistant_handle:
        return "assistant"
    if message.kind in NOW_KINDS:
        return f"kind:{message.kind}"
    if message.post_type in NOW_POST_TYPES:
        return f"type:{message.post_type}"
    if _URGENT_RE.search(message.body):
        return "urgent"
    body = " ".join(message.body.split()).casefold()
    owner = config.owner_name.casefold()
    if any(p in body for p in (f"{owner} chose", f"relayed from {owner}", f"{owner} (verbatim")):
        return "owner-relay"
    if any(p.casefold() in body for p in config.shadow_filter_refusal_phrases):
        return "new-refusal"
    return None


def find_quick_decision(message: LeadMessage, config: Config) -> Decision | None:
    """A decision that needs no model call, so live it holds nothing back."""
    rule = find_now_rule(message, config)
    if rule is not None:
        return Decision(source="rule", decision=NOW, rule=rule)
    if quotes_customer(message.body):
        return Decision(source="skipped", decision=NOW)
    return None


def map_message_to_state(message: LeadMessage, lead_handles: frozenset[str]) -> str:
    if message.from_conversation == SYSTEM_CONVERSATION:
        sender = "the coordination system (automatic notice)"
    elif message.from_handle in lead_handles:
        sender = "another team lead"
    else:
        sender = "a worker session (an agent doing one task, usually for this lead)"
    where = "board post to all leads" if message.board else "direct to this lead"
    return (
        f"Sender: {sender}\nMessage kind set by the sender: {message.kind} (post type: {message.post_type or 'none'}; "
        f"{where})\n\nMessage:\n{scrub(message.body)[:BODY_MAX_CHARS]}"
    )


def decide(message: LeadMessage, config: Config, lead_handles: frozenset[str], ask: Ask) -> Decision:
    quick = find_quick_decision(message, config)
    if quick is not None:
        return quick
    verdict = ask(map_message_to_state(message, lead_handles))
    if verdict.p_now is None:
        return Decision(source="model", decision=NOW, latency_ms=verdict.latency_ms, error=verdict.error)
    decision = NOW if verdict.p_now >= config.shadow_filter_threshold else BATCH
    return Decision(source="model", decision=decision, p_now=verdict.p_now, latency_ms=verdict.latency_ms)


def ask_model(url: str, key_file: str, state: str, timeout_s: float = CALL_TIMEOUT_S) -> Verdict:
    reply = call_model(url, key_file, state, cast(dict[str, object], QUESTIONS), timeout_s)
    if reply.answers is None:
        return Verdict(p_now=None, error=reply.error, latency_ms=reply.latency_ms)
    try:
        p_now = float(reply.answers["reaction"]["probabilities"]["now"])  # type: ignore[index]
    except (KeyError, TypeError, ValueError):
        return Verdict(p_now=None, error="no probability for 'now' in the answer", latency_ms=reply.latency_ms)
    if not 0 <= p_now <= 1:
        return Verdict(p_now=None, error="probability for 'now' out of range", latency_ms=reply.latency_ms)
    return Verdict(p_now=p_now, error=None, latency_ms=reply.latency_ms)


def list_new_lead_messages(
    conn: sqlite3.Connection, after_id: int, up_to_id: int, now: int, catch_up_ms: int = CATCH_UP_MS
) -> list[LeadMessage]:
    rows = conn.execute(
        """
        SELECT m.id, m.from_conversation, m.from_handle, m.kind, m.post_type, m.board, m.body,
               r.handle AS lead, r.conversation_id
        FROM messages m
        JOIN message_recipients r ON r.message_id = m.id
        JOIN leads l ON l.name = r.handle AND l.retired_at IS NULL
        WHERE m.id > ? AND m.id <= ? AND m.created_at >= ? AND m.cancelled_at IS NULL
        ORDER BY m.id, r.handle
        """,
        (after_id, up_to_id, now - catch_up_ms),
    ).fetchall()
    messages: dict[int, LeadMessage] = {}
    for row in rows:
        lead = (str(row["lead"]), str(row["conversation_id"]))
        found = messages.get(int(row["id"]))
        if found is not None:
            messages[found.id] = replace(found, leads=(*found.leads, lead))
            continue
        messages[int(row["id"])] = _map_row_to_message(row, (lead,))
    return list(messages.values())


def _map_row_to_message(row: sqlite3.Row, leads: tuple[tuple[str, str], ...]) -> LeadMessage:
    return LeadMessage(
        id=int(row["id"]),
        from_conversation=str(row["from_conversation"]),
        from_handle=str(row["from_handle"]),
        kind=str(row["kind"]),
        post_type=row["post_type"],
        board=bool(row["board"]),
        body=str(row["body"]),
        leads=leads,
    )


_LEAD_DELIVERY_SQL = """
    SELECT m.id, m.from_conversation, m.from_handle, m.kind, m.post_type, m.board, m.body, m.created_at,
           r.conversation_id, r.handle, r.filter_passed_at
    FROM message_recipients r JOIN messages m ON m.id = r.message_id
    JOIN leads l ON l.name = r.handle AND l.retired_at IS NULL
    WHERE r.handle != :assistant
"""
"""Deliveries the live filter may hold: one to a lead it has not let through is held unless a quick decision sends it
now."""


def is_wake_held(conn: sqlite3.Connection, config: Config, message_id: int, conversation_id: str, at_ms: int) -> bool:
    """Live, a delivery to a lead may not wake its tab, by typing or by its waiter, until the filter lets it through
    or its hold runs out; the assistant and whatever a rule sends now are never held."""
    if not config.shadow_filter_live:
        return False
    row = conn.execute(
        _LEAD_DELIVERY_SQL
        + " AND r.filter_passed_at IS NULL AND r.message_id = :message AND r.conversation_id = :conversation",
        {"assistant": config.assistant_handle, "message": message_id, "conversation": conversation_id},
    ).fetchone()
    if row is None or int(row["created_at"]) + hold_ms(config) <= at_ms:
        return False
    return find_quick_decision(_map_row_to_message(row, ()), config) is None


def hold_ms(config: Config) -> int:
    return config.shadow_filter_hold_min * 60_000


def take_due_wakes(conn: sqlite3.Connection, config: Config, at_ms: int) -> list[tuple[int, str, str]]:
    """Lead tabs to wake now, each as (message, conversation, lead) of its oldest delivery let through here; the rest
    come with the turn it starts. A tab's held deliveries are let through together once the oldest waited out its hold.
    One let through that no typer took up within REWAKE_AFTER_MS is woken again: its wake can be lost to the sender's
    typer still holding the typer lock, or to a stop between the verdict and the wake."""
    rows = conn.execute(
        _LEAD_DELIVERY_SQL
        + """
          AND (r.filter_passed_at IS NULL OR r.filter_passed_at <= :stale) AND r.typing_wanted_at IS NULL
          AND r.acked_at IS NULL AND r.offered_at IS NULL AND r.done_at IS NULL AND r.declined_at IS NULL
          AND m.cancelled_at IS NULL
          -- A conversation with no live process gets its mail from the start hook when it resumes.
          AND EXISTS (SELECT 1 FROM instances i WHERE i.conversation_id = r.conversation_id AND i.ended_at IS NULL)
        ORDER BY m.id
        """,
        {"assistant": config.assistant_handle, "stale": at_ms - REWAKE_AFTER_MS},
    ).fetchall()
    held: dict[tuple[str, str], list[sqlite3.Row]] = {}
    stale: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        tab = (str(row["conversation_id"]), str(row["handle"]))
        if row["filter_passed_at"] is not None:
            stale.setdefault(tab, []).append(row)
        elif find_quick_decision(_map_row_to_message(row, ()), config) is None:
            held.setdefault(tab, []).append(row)
    wakes: list[tuple[int, str, str]] = []
    for tab in held.keys() | stale.keys():
        due = stale.get(tab, [])
        waiting = held.get(tab, [])
        if waiting and int(waiting[0]["created_at"]) + hold_ms(config) <= at_ms:
            due = due + waiting
        if not due:
            continue
        with transaction(conn):
            # Checked again here: a hook may have handed some over since they were read.
            passed = [
                int(row["id"])
                for row in due
                if conn.execute(
                    """
                    UPDATE message_recipients SET filter_passed_at = ?
                    WHERE message_id = ? AND conversation_id = ? AND filter_passed_at IS ?
                      AND typing_wanted_at IS NULL AND acked_at IS NULL AND offered_at IS NULL
                    """,
                    (at_ms, int(row["id"]), tab[0], row["filter_passed_at"]),
                ).rowcount
            ]
        typing = conn.execute(
            """
            SELECT 1 FROM message_recipients
            WHERE conversation_id = ? AND typing_wanted_at IS NOT NULL AND acked_at IS NULL AND offered_at IS NULL
              AND done_at IS NULL AND declined_at IS NULL
            """,
            (tab[0],),
        ).fetchone()
        # A typer or the retry pass already works on this tab; the turn it starts brings these too.
        if passed and typing is None:
            wakes.append((min(passed), *tab))
    return sorted(wakes)


def _record_live_decision(
    conn: sqlite3.Connection, message_id: int, conversation_id: str, decision: Decision, at_ms: int
) -> bool:
    """True when the model let through a delivery that was held for its verdict and still waits to be delivered."""
    column = "filter_passed_at" if decision.decision == NOW else "filter_held_at"
    with transaction(conn):
        cursor = conn.execute(
            f"""
            UPDATE message_recipients SET {column} = ?
            WHERE message_id = ? AND conversation_id = ? AND filter_passed_at IS NULL
              AND acked_at IS NULL AND offered_at IS NULL
            """,
            (at_ms, message_id, conversation_id),
        )
    return cursor.rowcount == 1 and decision.decision == NOW


def run_shadow_tick(
    conn: sqlite3.Connection,
    config: Config,
    after_id: int,
    now: int,
    ask: Ask,
    wake: Callable[[int, str, str], None] | None = None,
) -> int:
    """Judges the messages to leads past after_id and logs one row per lead; returns the new after_id.

    Live, it records each verdict first, and calls wake(message, conversation, lead) for a delivery the model let
    through, as nothing else will start it."""
    top = conn.execute("SELECT max(id) FROM messages").fetchone()[0]
    up_to = int(top) if top is not None else after_id
    if up_to <= after_id:
        return after_id
    # Live, a message still within its hold waits for its verdict, however long the hold.
    catch_up = max(CATCH_UP_MS, hold_ms(config)) if config.shadow_filter_live else CATCH_UP_MS
    messages = list_new_lead_messages(conn, after_id, up_to, now, catch_up)
    if config.shadow_filter_live and len(messages) > LIVE_MAX_PER_TICK:
        messages = messages[:LIVE_MAX_PER_TICK]
        up_to = messages[-1].id
    lead_handles = frozenset(str(r[0]) for r in conn.execute("SELECT name FROM leads"))
    for message in messages:
        decision = decide(message, config, lead_handles, ask)
        if config.shadow_filter_live and decision.source == "model":
            for lead, conversation in message.leads:
                if lead == config.assistant_handle:
                    continue
                if _record_live_decision(conn, message.id, conversation, decision, now) and wake is not None:
                    wake(message.id, conversation, lead)
        append_rows(
            [
                {"at": now, "message": message.id, "lead": lead, "conversation": conversation, **asdict(decision)}
                for lead, conversation in message.leads
            ]
        )
    return up_to


def log_path() -> Path:
    return state_dir() / LOG_NAME


def append_rows(rows: list[dict[str, object]]) -> None:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size >= LOG_MAX_BYTES:
        path.replace(path.with_name(LOG_NAME + ".1"))
    with path.open("ab") as f:
        if f.tell() > 0 and not _ends_with_newline(path):
            # A write cut short by a crash: the next row must not join its fragment.
            f.write(b"\n")
        f.write("".join(json.dumps(row) + "\n" for row in rows).encode())


def _ends_with_newline(path: Path) -> bool:
    with path.open("rb") as f:
        f.seek(-1, 2)
        return f.read(1) == b"\n"


def find_start_id(conn: sqlite3.Connection, config: Config, now: int) -> int:
    """Where the log left off, so a restart misses nothing; with no log, from now on, or live from the messages still
    within their hold, which wait for a verdict."""
    for row in reversed(read_rows()):
        message = row.get("message")
        if isinstance(message, int):
            return message
    if config.shadow_filter_live:
        top = conn.execute("SELECT max(id) FROM messages WHERE created_at < ?", (now - hold_ms(config),)).fetchone()[0]
    else:
        top = conn.execute("SELECT max(id) FROM messages").fetchone()[0]
    return int(top) if top is not None else 0


def read_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    path = log_path()
    for part in (path.with_name(LOG_NAME + ".1"), path):
        if not part.exists():
            continue
        for line in part.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(cast(dict[str, object], row))
    return rows


def map_rows_to_report(rows: list[dict[str, object]], since_ms: int) -> list[str]:
    # A tick that failed halfway judges its messages again: the last verdict per delivery counts.
    recent = {
        (r.get("message"), r.get("lead")): r for r in rows if isinstance(at := r.get("at"), int) and at >= since_ms
    }
    rows = list(recent.values())
    if not rows:
        return ["no decisions logged in this period"]
    messages = {r["message"] for r in rows}
    sources = Counter(str(r.get("source")) for r in rows)
    rules = Counter(str(r.get("rule")) for r in rows if r.get("source") == "rule")
    model = [r for r in rows if r.get("source") == "model"]
    batched = [r for r in rows if r.get("decision") == BATCH]
    errors = [r for r in model if r.get("error")]
    # One call per message, though a board post logs a row for each lead.
    calls = {r.get("message"): ms for r in model if isinstance(ms := r.get("latency_ms"), int)}
    latencies = sorted(calls.values())
    lines = [
        f"messages to leads: {len(messages)} ({len(rows)} lead deliveries)",
        f"by rule: {sources['rule']} ({', '.join(f'{k} {v}' for k, v in rules.most_common())})",
        f"by model: {len(model)}; skipped (customer text): {sources['skipped']}",
        f"would batch: {len(batched)} = {_share(len(batched), len(rows))} of lead deliveries, "
        f"{_share(len(batched), len(model))} of model decisions",
    ]
    if latencies:
        lines.append(f"model latency ms: p50 {_percentile(latencies, 0.5)}, p95 {_percentile(latencies, 0.95)}")
    lines.append(f"model errors (logged as now): {len(errors)}")
    lines += [f"  {n} x {text}" for text, n in Counter(str(r["error"]) for r in errors).most_common(5)]
    return lines


def _share(part: int, whole: int) -> str:
    return f"{part / whole:.0%}" if whole else "-"


def _percentile(sorted_values: list[int], q: float) -> int:
    return sorted_values[min(len(sorted_values) - 1, int(q * len(sorted_values)))]
