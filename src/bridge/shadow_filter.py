"""Shadow of a wake-up filter for messages to team leads: decides what a filter would hold back and logs it, after the
message went out as usual. Nothing here changes or delays delivery."""

import http.client
import json
import re
import sqlite3
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import cast

from bridge.board import SYSTEM_CONVERSATION, USER_CONVERSATION
from bridge.config import Config, state_dir

EVERY_S = 10
CALL_TIMEOUT_S = 3.0
CATCH_UP_MS = 3_600_000
"""After a restart, messages older than this are left unjudged, so a long stop does not end in a burst of calls."""
LOG_NAME = "shadow-filter.jsonl"
LOG_MAX_BYTES = 4_000_000
"""Past this the log moves to .1, replacing the one before: a week of a busy team is about 1 MB."""
BODY_MAX_CHARS = 1500
MODEL = "jev-latest"
NOW = "now"
BATCH = "batch"
NOW_KINDS = frozenset({"request", "reply"})
NOW_POST_TYPES = frozenset({"question", "blocked-tab"})
_URGENT_RE = re.compile(r"\b(URGENT|STOP)\b")
_SECRET_RES: tuple[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...] = (
    (
        re.compile(
            r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{16,}"
            r"|xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+)"
        ),
        "[secret]",
    ),
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|authorization)[\"']?\s*[:=]\s*(?:bearer\s+)?\S+"
        ),
        r"\1=[secret]",
    ),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [secret]"),
    (re.compile(r"(?i)\b(mongodb(\+srv)?|postgres(ql)?|mysql|redis|amqp)://\S+"), "[connection-string]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[email]"),
    (
        re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),
        lambda m: m.group(0) if re.fullmatch(r"[0-9a-f]{40}", m.group(0)) else "[long-token]",
    ),
)
_CUSTOMER_TEXT_RE = re.compile(
    r"(?im)((customer|user|client) (said|says|wrote|writes|asked|asks)|conversation text|transcript of|\"role\"\s*:"
    r"|\buser:\s|\bassistant:\s|^\s*(customer|client):|message content:|the (bot|agent) (replied|answered):)"
)
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
    return None


def scrub(text: str) -> str:
    for pattern, replacement in _SECRET_RES:
        text = pattern.sub(replacement, text)
    return text


def quotes_customer(text: str) -> bool:
    return bool(_CUSTOMER_TEXT_RE.search(text))


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
    rule = find_now_rule(message, config)
    if rule is not None:
        return Decision(source="rule", decision=NOW, rule=rule)
    if quotes_customer(message.body):
        return Decision(source="skipped", decision=NOW)
    verdict = ask(map_message_to_state(message, lead_handles))
    if verdict.p_now is None:
        return Decision(source="model", decision=NOW, latency_ms=verdict.latency_ms, error=verdict.error)
    decision = NOW if verdict.p_now >= config.shadow_filter_threshold else BATCH
    return Decision(source="model", decision=decision, p_now=verdict.p_now, latency_ms=verdict.latency_ms)


def ask_model(url: str, key_file: str, state: str, timeout_s: float = CALL_TIMEOUT_S) -> Verdict:
    """The key is read for each call and kept nowhere; an error text never carries it."""
    started = time.monotonic()

    key = ""

    def failed(error: str) -> Verdict:
        for part in key.split():
            error = error.replace(part, "[key]")
        return Verdict(p_now=None, error=error[:200], latency_ms=round((time.monotonic() - started) * 1000))

    try:
        key = Path(key_file).expanduser().read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        return failed(f"key file unreadable: {type(exc).__name__}")
    if not key:
        return failed("key file empty")
    body = json.dumps({"state": state, "model": MODEL, "questions": QUESTIONS}).encode()
    request = urllib.request.Request(
        url, data=body, method="POST", headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            answer = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode(errors="replace")[:300]
        except (OSError, http.client.HTTPException):
            detail = ""
        return failed(f"http {exc.code}: {scrub(detail)}")
    except TimeoutError:
        return failed("timeout")
    except urllib.error.URLError as exc:
        reason = "timeout" if isinstance(exc.reason, TimeoutError) else str(exc.reason)
        return failed(reason)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        # The name only: a malformed key shows up in the text of some of these.
        return failed(type(exc).__name__)
    try:
        p_now = float(answer["answers"]["reaction"]["probabilities"]["now"])
    except (KeyError, TypeError, ValueError):
        return failed("no probability for 'now' in the answer")
    if not 0 <= p_now <= 1:
        return failed(f"probability for 'now' out of range: {p_now}")
    return Verdict(p_now=p_now, error=None, latency_ms=round((time.monotonic() - started) * 1000))


def list_new_lead_messages(conn: sqlite3.Connection, after_id: int, up_to_id: int, now: int) -> list[LeadMessage]:
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
        (after_id, up_to_id, now - CATCH_UP_MS),
    ).fetchall()
    messages: dict[int, LeadMessage] = {}
    for row in rows:
        lead = (str(row["lead"]), str(row["conversation_id"]))
        found = messages.get(int(row["id"]))
        if found is not None:
            messages[found.id] = replace(found, leads=(*found.leads, lead))
            continue
        messages[int(row["id"])] = LeadMessage(
            id=int(row["id"]),
            from_conversation=str(row["from_conversation"]),
            from_handle=str(row["from_handle"]),
            kind=str(row["kind"]),
            post_type=row["post_type"],
            board=bool(row["board"]),
            body=str(row["body"]),
            leads=(lead,),
        )
    return list(messages.values())


def run_shadow_tick(conn: sqlite3.Connection, config: Config, after_id: int, now: int, ask: Ask) -> int:
    """Judges the messages to leads past after_id and logs one row per lead; returns the new after_id."""
    top = conn.execute("SELECT max(id) FROM messages").fetchone()[0]
    up_to = int(top) if top is not None else after_id
    if up_to <= after_id:
        return after_id
    messages = list_new_lead_messages(conn, after_id, up_to, now)
    lead_handles = frozenset(str(r[0]) for r in conn.execute("SELECT name FROM leads"))
    for message in messages:
        decision = decide(message, config, lead_handles, ask)
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


def find_start_id(conn: sqlite3.Connection) -> int:
    """Where the log left off, so a restart misses nothing; with no log, from now on."""
    for row in reversed(read_rows()):
        message = row.get("message")
        if isinstance(message, int):
            return message
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
