import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass, replace
from typing import cast

from bridge.asks import is_ask_paused, is_owner_locked, list_asks, send_from_bridge
from bridge.attention import Project, load_projects
from bridge.board import AddressError, Recipient, convert_text_to_preview, resolve_handle
from bridge.compaction import read_context_full
from bridge.config import Config
from bridge.enrich import load_enrichment
from bridge.leads import Lead, LeadWork, is_surface_locked, lead_dir, list_lead_work, list_leads, read_notes
from bridge.procinfo import read_process_table
from bridge.refs import map_ref_to_label, map_text_to_ref
from bridge.review_post import is_working_time
from bridge.reviews import (
    COMMENTS_OPEN,
    DRAFTS,
    FIXED,
    PUBLISHED,
    READY,
    WAITING_FOR_OWNER,
    WAITING_FOR_YOU,
    map_db_to_reviews,
    record_signal_post,
)
from bridge.workers import (
    WORKER_CAP,
    LiveWorker,
    list_live_workers,
    list_queued_launches,
    map_processes_to_live_work,
)

IDLE_FOR_MS = 20 * 60_000
FOCUS_STALE_MS = 6 * 3_600_000
PRESENCE_FRESH_MS = 24 * 3_600_000
SIGNAL_REPEAT_MS = 2 * 3_600_000
SUGGESTIONS_MAX = 3
CHUNKS_NAMED_MAX = 3
LINEAR_PROJECT_RE = re.compile(r"P-[A-Z]+-\d+")
BACKLOG_MAX_CHARS = 300
_BACKLOG_RE = re.compile(r"^\s*(?:[-*]\s*)?QUEUE\b")
_FINISHED_ISSUE = ("completed", "canceled")
BUSY, WAITS, FINISHED = "busy", "waits", "finished"


@dataclass(frozen=True)
class Presence:
    off: bool
    why: str


@dataclass(frozen=True)
class ChunkVerdict:
    name: str
    verdict: str
    """busy, waits (on someone else) or finished (every ref ended, the chunk not yet closed)."""
    why: str


@dataclass(frozen=True)
class Suggestion:
    key: str
    title: str
    project: str
    state: str
    assignee: str | None
    shared_with: tuple[str, ...]


@dataclass(frozen=True)
class LeadLoad:
    lead: Lead
    load: str
    """idle, working or waiting."""
    workers: int | None
    """None: agterm did not answer, so the count is unknown."""
    idle_for_ms: int | None
    chunks: tuple[ChunkVerdict, ...]
    reasons: tuple[str, ...]
    reason_key: str | None
    """Set only when the lead has capacity for planned work."""
    not_flagged_why: str | None
    suggestions: tuple[Suggestion, ...]
    notes: tuple[str, ...]
    streams: tuple[str, ...] = ()
    """Its domains outside the cycle's priority projects: parallel streams it loads from its own backlog."""
    backlog: str | None = None
    """Its backlog line from its notes, if it keeps one."""


def record_owner_off(conn: sqlite3.Connection, by: str, at_ms: int) -> None:
    conn.execute(
        "INSERT INTO owner_presence (id, owner_off_at, owner_off_by) VALUES (1, ?, ?) "
        "ON CONFLICT (id) DO UPDATE SET owner_off_at = excluded.owner_off_at, owner_off_by = excluded.owner_off_by",
        (at_ms, by),
    )


def record_owner_on(conn: sqlite3.Connection, by: str, at_ms: int) -> None:
    conn.execute(
        "INSERT INTO owner_presence (id, owner_on_at, owner_on_by) VALUES (1, ?, ?) "
        "ON CONFLICT (id) DO UPDATE SET owner_on_at = excluded.owner_on_at, owner_on_by = excluded.owner_on_by",
        (at_ms, by),
    )


def read_owner_presence(conn: sqlite3.Connection, config: Config, now: int) -> Presence:
    """The night and morning words decide while either is under 24 h old; past that, the work clock does."""
    row = conn.execute("SELECT owner_off_at, owner_on_at, owner_off_by, owner_on_by FROM owner_presence").fetchone()
    off_at: int | None = row["owner_off_at"] if row else None
    on_at: int | None = row["owner_on_at"] if row else None
    latest = max((at for at in (off_at, on_at) if at is not None), default=None)
    if row is not None and latest is not None and now - latest <= PRESENCE_FRESH_MS:
        if off_at is not None and (on_at is None or off_at > on_at):
            # The owner may start the day by writing, with no morning word: a message of theirs after the night word.
            wrote = conn.execute(
                "SELECT max(created_at) FROM messages WHERE from_handle = ?", (config.user_alias or "user",)
            ).fetchone()[0]
            if wrote is not None and wrote > off_at:
                return Presence(False, f"on: wrote at {_stamp(int(wrote))}, after the night word")
            return Presence(True, f"off since {_stamp(off_at)} ({row['owner_off_by']})")
        return Presence(False, f"on since {_stamp(cast(int, on_at))} ({row['owner_on_by']})")
    if is_working_time(now, config):
        return Presence(False, "working hours, and no night or morning word in 24 h")
    return Presence(True, "outside working hours, and no night or morning word in 24 h")


def assess_leads(conn: sqlite3.Connection, config: Config, now: int, tree: dict[str, object] | None) -> list[LeadLoad]:
    leads = [lead for lead in list_leads(conn) if not is_surface_locked(conn, config, lead.surface) and lead.surface]
    enrichment = load_enrichment(conn, now)
    queue = {str(pr["ref"]): pr for pr in cast(list[dict[str, object]], map_db_to_reviews(conn, config, now)["prs"])}
    claimed = {
        str(r[0]) for r in conn.execute("SELECT j.value FROM lead_work w, json_each(w.refs) j WHERE w.done_at IS NULL")
    }
    projects = load_projects(conn)
    loads: list[LeadLoad] = []
    for lead in leads:
        waited = _map_asks_to_waited_refs(conn, config, lead.name, now)
        chunks = tuple(
            map_chunk_to_verdict(chunk, queue, enrichment, waited, config) for chunk in list_lead_work(conn, lead.name)
        )
        named = frozenset(s.lower() for s in config.parallel_streams)
        suggestions, notes = find_suggestions(lead, leads, projects, enrichment, claimed, named)
        load = _assess_lead(conn, now, tree, lead, chunks, suggestions, notes)
        streams = find_parallel_streams(lead, projects, config.parallel_streams)
        backlog = find_backlog_line(read_notes(lead_dir(lead.name))) if streams else None
        loads.append(replace(load, streams=streams, backlog=backlog))
    return loads


def find_parallel_streams(lead: Lead, projects: list[Project], streams: tuple[str, ...]) -> tuple[str, ...]:
    """Only a domain the config names a stream: one merely outside the priority projects may have been dropped."""
    names = {p.name.lower() for p in projects}
    named = {s.lower() for s in streams}
    return tuple(d for d in lead.domains if d.lower() in named and d.lower() not in names)


def find_backlog_line(notes: str) -> str | None:
    """The line of a lead's notes that starts with QUEUE, cut to BACKLOG_MAX_CHARS."""
    for line in notes.splitlines():
        if _BACKLOG_RE.match(line):
            return convert_text_to_preview(line.strip(), BACKLOG_MAX_CHARS)
    return None


def _assess_lead(
    conn: sqlite3.Connection,
    now: int,
    tree: dict[str, object] | None,
    lead: Lead,
    chunks: tuple[ChunkVerdict, ...],
    suggestions: tuple[Suggestion, ...],
    notes: tuple[str, ...],
) -> LeadLoad:
    instance = conn.execute(
        """
        SELECT status, conversation_id, coalesce(last_activity_at, first_seen_at) AS since FROM instances
        WHERE surface = ? AND ended_at IS NULL ORDER BY proc_start_ms DESC LIMIT 1
        """,
        (lead.surface,),
    ).fetchone()
    session = conn.execute("SELECT agterm_session FROM surfaces WHERE surface = ?", (lead.surface,)).fetchone()
    workers: list[LiveWorker] | None = None
    queued = 0
    if tree is not None and session is not None and session["agterm_session"]:
        workers = list_live_workers(conn, tree, str(session["agterm_session"]))
        queued = count_queued_launches(str(session["agterm_session"]))
    idle_for = now - int(instance["since"]) if instance is not None and instance["status"] == "idle" else None
    # A finished chunk blocks nothing, but alone it says nothing of waiting: only an idle tab shows room then.
    all_wait = any(c.verdict == WAITS for c in chunks) and all(c.verdict != BUSY for c in chunks)
    working = (instance is not None and instance["status"] == "active") or any(
        w.status == "active" for w in workers or []
    )
    load = "working" if working else "waiting" if all_wait else "idle"
    reasons: list[str] = []
    keys: list[str] = []
    if idle_for is not None and idle_for >= IDLE_FOR_MS:
        reasons.append(f"idle {_convert_ms_to_age(idle_for)}")
        keys.append("idle")
    if all_wait:
        reasons.append(convert_chunks_to_reason(chunks))
        causes = "\n".join(sorted(c.why for c in chunks if c.verdict == WAITS))
        keys.append("waiting:" + hashlib.sha256(causes.encode()).hexdigest()[:12])
    planned_max = WORKER_CAP - 1
    # A queued launch takes one of its slots as soon as the laptop has room.
    count = len(workers) + queued if workers is not None else None
    not_flagged: str | None = None
    if instance is None:
        not_flagged = "no live session"
    elif full := read_context_full(conn, instance["conversation_id"]):
        not_flagged = full
    elif count is None:
        not_flagged = "agterm did not answer, so its workers are unknown"
    elif count >= planned_max:
        not_flagged = f"{count} workers, at or over the planned {planned_max}; the last slot stays for urgent work"
    elif not reasons:
        not_flagged = "busy: " + next((c.why for c in chunks if c.verdict == BUSY), "in a turn, or idle under 20m")
    return LeadLoad(
        lead=lead,
        load=load,
        workers=count,
        idle_for_ms=idle_for,
        chunks=chunks,
        reasons=tuple(reasons),
        reason_key=",".join(keys) if not_flagged is None else None,
        not_flagged_why=not_flagged,
        suggestions=suggestions,
        notes=notes,
    )


def count_queued_launches(parent: str) -> int:
    count = 0
    for path in list_queued_launches():
        try:
            launch: object = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        count += isinstance(launch, dict) and cast(dict[str, object], launch).get("parent") == parent
    return count


def map_chunk_to_verdict(
    chunk: LeadWork,
    queue: dict[str, dict[str, object]],
    enrichment: dict[str, dict[str, object]],
    waited: dict[str, str],
    config: Config,
) -> ChunkVerdict:
    """Busy while one ref is open and not waiting; nothing can be read from a chunk without refs, so it is busy."""
    if not chunk.refs:
        return ChunkVerdict(chunk.name, BUSY, f"{chunk.name}: no refs to judge it by")
    verdicts: list[tuple[str, str]] = []
    for ref in chunk.refs:
        label = map_ref_to_label(ref)
        if ref in waited:
            verdicts.append((WAITS, f"{label} {waited[ref]}"))
        elif ref.startswith("pr:"):
            verdict, why = map_pr_to_verdict(ref, queue, enrichment, config)
            verdicts.append((verdict, f"{label} {why}"))
        elif ref.startswith("issue:"):
            verdicts.append(map_issue_to_verdict(ref, label, queue, enrichment, config))
        else:
            verdicts.append((BUSY, f"{label} cannot be judged"))
    for wanted in (BUSY, WAITS):
        found = [why for verdict, why in verdicts if verdict == wanted]
        if found:
            return ChunkVerdict(chunk.name, wanted, f"{chunk.name}: {found[0]}")
    return ChunkVerdict(chunk.name, FINISHED, f"{chunk.name}: every ref ended, not closed")


def map_pr_to_verdict(
    ref: str,
    queue: dict[str, dict[str, object]],
    enrichment: dict[str, dict[str, object]],
    config: Config,
    linear_status: str | None = None,
) -> tuple[str, str]:
    """The review queue first, then the PR cache, then what Linear says of a PR attached to an issue."""
    pr = queue.get(ref)
    if pr is not None:
        group, state = pr.get("group"), pr.get("state")
        if group == PUBLISHED:
            return WAITS, "published, waiting on reviewers"
        if group == WAITING_FOR_YOU and state in (WAITING_FOR_OWNER, FIXED) and not pr.get("reviews_gap"):
            return WAITS, f"waiting for {config.owner_name}'s review"
        if pr.get("reviews_gap"):
            return BUSY, f"open: {pr['reviews_gap']}"
        if state == COMMENTS_OPEN:
            return BUSY, f"open: {config.owner_name}'s comments to answer"
        if group == READY:
            return BUSY, "ready for its lead to merge"
        if group == DRAFTS:
            return BUSY, "a draft"
        return BUSY, f"open: {state}"
    data = enrichment.get(ref) or {}
    state = data.get("state")
    if state == "MERGED":
        return WAITS, "merged, awaiting release"
    if state == "CLOSED":
        return FINISHED, "closed"
    if state == "OPEN":
        return BUSY, "open, not in the review queue"
    if linear_status == "merged":
        return WAITS, "merged, awaiting release (per Linear)"
    if linear_status == "closed":
        return FINISHED, "closed (per Linear)"
    return BUSY, "state not cached"


def map_issue_to_verdict(
    ref: str,
    label: str,
    queue: dict[str, dict[str, object]],
    enrichment: dict[str, dict[str, object]],
    config: Config,
) -> tuple[str, str]:
    """An open issue waits only through a PR Linear attached to it that waits, and none attached that is busy."""
    data = enrichment.get(ref) or {}
    if "state_type" not in data:
        return BUSY, f"{label} state not cached"
    if data["state_type"] in _FINISHED_ISSUE:
        return FINISHED, f"{label} {data.get('state')}"
    linked = cast(list[dict[str, object]], data.get("linked_prs") or [])
    if not data.get("linked_complete"):
        return BUSY, f"{label} {data.get('state')}, Linear's PR list for it is incomplete"
    verdicts = [
        (str(pr["ref"]), *map_pr_to_verdict(str(pr["ref"]), queue, enrichment, config, cast(str, pr.get("status"))))
        for pr in linked
    ]
    busy = [(pr, why) for pr, verdict, why in verdicts if verdict == BUSY]
    waits = [(pr, why) for pr, verdict, why in verdicts if verdict == WAITS]
    if busy:
        pr, why = busy[0]
        return BUSY, f"{label} {data.get('state')}, {map_ref_to_label(pr)} {why}"
    if waits:
        pr, why = waits[0]
        return WAITS, f"{label} via {map_ref_to_label(pr)} {why}"
    return BUSY, f"{label} {data.get('state')}, no waiting PR"


def _map_asks_to_waited_refs(conn: sqlite3.Connection, config: Config, lead: str, now: int) -> dict[str, str]:
    waited: dict[str, str] = {}
    for ask in list_asks(conn, owner=lead):
        if ask.wait_on is None and not is_ask_paused(ask, now):
            continue
        on = "paused" if ask.wait_on is None else f"on {config.owner_name if ask.wait_on == 'owner' else ask.wait_on}"
        for text in ask.refs:
            ref = map_text_to_ref(text, issue_prefixes=config.issue_prefixes, default_repo=None)
            if ref is not None:
                waited.setdefault(ref, f"ask #{ask.id} waits {on}")
    return waited


def find_suggestions(
    lead: Lead,
    leads: list[Lead],
    projects: list[Project],
    enrichment: dict[str, dict[str, object]],
    claimed: set[str],
    streams: frozenset[str] = frozenset(),
) -> tuple[tuple[Suggestion, ...], tuple[str, ...]]:
    """Open issues of its priority projects that no open chunk lists, from the cache only: unassigned first, then by
    priority. A Linear project ref is kept but cannot be searched: the cache holds no issue's project."""
    notes: list[str] = []
    names = {p.name.lower(): p.name for p in projects}
    mine: list[str] = []
    for domain in lead.domains:
        if LINEAR_PROJECT_RE.fullmatch(domain):
            notes.append(f"{domain}: the issue cache holds no project, so it is not searched")
        elif domain.lower() in names:
            mine.append(domain.lower())
        elif domain.lower() not in streams:
            notes.append(f"{domain!r} is not a priority project")
    ranked: list[tuple[bool, int, int, Suggestion]] = []
    seen: set[str] = set()
    for position, project in enumerate(projects):
        if project.name.lower() not in mine:
            continue
        shared = tuple(
            other.name
            for other in leads
            if other.name != lead.name and project.name.lower() in {d.lower() for d in other.domains}
        )
        for index, key in enumerate(project.issue_keys):
            ref = f"issue:{key}"
            data = enrichment.get(ref) or {}
            if ref in claimed or ref in seen or "state_type" not in data or data["state_type"] in _FINISHED_ISSUE:
                continue
            seen.add(ref)
            assignee = cast(str | None, data.get("assignee"))
            suggestion = Suggestion(key, str(data.get("title") or ""), project.name, str(data.get("state")), assignee,
                                    shared)  # fmt: skip
            ranked.append((assignee is not None, position, index, suggestion))
    ranked.sort(key=lambda item: item[:3])
    return tuple(item[3] for item in ranked[:SUGGESTIONS_MAX]), tuple(notes)


def convert_chunks_to_reason(chunks: tuple[ChunkVerdict, ...]) -> str:
    shown = "; ".join(c.why for c in chunks[:CHUNKS_NAMED_MAX])
    more = f"; +{len(chunks) - CHUNKS_NAMED_MAX} more" if len(chunks) > CHUNKS_NAMED_MAX else ""
    return f"every open chunk waits on someone else: {shown}{more}"


def convert_suggestion_to_text(suggestion: Suggestion) -> str:
    who = suggestion.assignee or "unassigned"
    shared = f"; shared with {', '.join(suggestion.shared_with)}" if suggestion.shared_with else ""
    return f'{suggestion.key} "{suggestion.title}" ({suggestion.project}, {suggestion.state}, {who}{shared})'


def convert_load_to_signal(load: LeadLoad) -> str:
    head = (
        f"{load.lead.display_name} has room for planned work ({load.workers}/{WORKER_CAP} workers, planned at "
        f"most {WORKER_CAP - 1}): {'; '.join(load.reasons)}."
    )
    stream = f"\n{convert_streams_to_text(load)}" if load.streams else ""
    if load.suggestions:
        items = "; ".join(f"{n}) {convert_suggestion_to_text(s)}" for n, s in enumerate(load.suggestions, start=1))
        return f"{head}\nNext in its domains: {items}.{stream}"
    if not load.lead.domains:
        return f"{head}\nNo domains set, so nothing to suggest: guild lead domains {load.lead.name} <project>…"
    if load.streams and len(load.streams) == len(load.lead.domains):
        return f"{head}{stream}"
    why = "; ".join(load.notes) or "no open issue of its priority projects is outside the work chunks"
    return f"{head}\nNothing to suggest from the priority projects: {why}.{stream}"


def convert_streams_to_text(load: LeadLoad) -> str:
    names = ", ".join(load.streams)
    kind = "is a parallel stream" if len(load.streams) == 1 else "are parallel streams"
    own = (
        f"Its own backlog (from its notes): {load.backlog}"
        if load.backlog
        else "It loads itself from its own backlog; ask it for its list."
    )
    return f"{names} {kind} outside the cycle's priority projects. {own}"


def find_due_signals(conn: sqlite3.Connection, loads: list[LeadLoad], now: int) -> list[LeadLoad]:
    due: list[LeadLoad] = []
    for load in loads:
        if load.reason_key is None or is_room_parked(conn, load.lead.name, now):
            continue
        row = conn.execute(
            "SELECT reason_key, sent_at FROM capacity_signals WHERE lead = ?", (load.lead.name,)
        ).fetchone()
        if row is None or row["reason_key"] != load.reason_key or now - int(row["sent_at"]) >= SIGNAL_REPEAT_MS:
            due.append(load)
    return due


def is_room_parked(conn: sqlite3.Connection, lead: str, now: int) -> bool:
    row = conn.execute("SELECT room_parked_until FROM leads WHERE name = ?", (lead,)).fetchone()
    return row is not None and row[0] is not None and int(row[0]) > now


def park_room(conn: sqlite3.Connection, lead: str, until_ms: int | None) -> None:
    """The lead is left idle on purpose: no 'has room' notice until then; None lifts it."""
    conn.execute("UPDATE leads SET room_parked_until = ? WHERE name = ?", (until_ms, lead))


def find_assistant_unreachable(conn: sqlite3.Connection, config: Config) -> str | None:
    if is_owner_locked(conn, config, config.assistant_handle):
        return f"{config.assistant_handle}'s tab is locked"
    try:
        resolve_handle(conn, config.assistant_handle)
    except AddressError as exc:
        return str(exc).split(";")[0]
    return None


def send_capacity_signals(
    conn: sqlite3.Connection, config: Config, now: int, loads: list[LeadLoad]
) -> list[tuple[int, Recipient]]:
    """None while the owner is off; a signal that reached nobody is not recorded, so the next round tries again."""
    if read_owner_presence(conn, config, now).off or find_assistant_unreachable(conn, config):
        return []
    sent: list[tuple[int, Recipient]] = []
    for load in find_due_signals(conn, loads, now):
        delivered = send_from_bridge(conn, config, config.assistant_handle, convert_load_to_signal(load), now)
        if not delivered:
            continue
        conn.execute(
            """
            INSERT INTO capacity_signals (lead, reason_key, sent_at, message_id) VALUES (?, ?, ?, ?)
            ON CONFLICT (lead) DO UPDATE SET reason_key = excluded.reason_key, sent_at = excluded.sent_at,
                message_id = excluded.message_id
            """,
            (load.lead.name, load.reason_key, now, delivered[0][0]),
        )
        sent.extend(delivered)
    return sent


def list_ready_asks(conn: sqlite3.Connection, lead: str, now: int) -> list[int]:
    """The owner's asks this lead holds that wait on nobody: work it can start without anyone."""
    return [
        ask.id
        for ask in list_asks(conn, owner=lead)
        if ask.state in ("open", "in_progress") and ask.wait_on is None and not is_ask_paused(ask, now)
    ]


def convert_idle_work_to_text(load: LeadLoad, asks: list[int], queue: str | None) -> str:
    work = [f"asks {', '.join(f'#{n}' for n in asks)} wait on nobody (`guild ask list`)"] if asks else []
    if queue:
        work.append(f"your notes' {queue}")
    return (
        f"You have been idle {_convert_ms_to_age(cast(int, load.idle_for_ms))} with work waiting: {'; '.join(work)}. "
        "Start the next item, or mark what blocks it (`guild ask wait`)."
    )


def send_idle_work_nudges(
    conn: sqlite3.Connection, config: Config, now: int, loads: list[LeadLoad]
) -> list[tuple[int, Recipient]]:
    """A lead idle 20 minutes, no worker known active, with an ask that waits on nobody or a QUEUE line in its notes:
    told once per idle spell, which its last activity names. Held while the owner is off, like the assistant's
    notices."""
    if read_owner_presence(conn, config, now).off:
        return []
    sent: list[tuple[int, Recipient]] = []
    for load in loads:
        if load.idle_for_ms is None or load.idle_for_ms < IDLE_FOR_MS or load.load == "working":
            continue
        if load.workers is None or is_room_parked(conn, load.lead.name, now):
            continue
        since = now - load.idle_for_ms
        # Assessed before the write lock: a prompt since then has ended this idle spell.
        current = conn.execute(
            """
            SELECT 1 FROM instances WHERE surface = ? AND ended_at IS NULL AND status = 'idle'
              AND coalesce(last_activity_at, first_seen_at) = ?
            """,
            (load.lead.surface, since),
        ).fetchone()
        if current is None:
            continue
        key = f"idle-work:{load.lead.name}:{since}"
        if conn.execute("SELECT 1 FROM signal_posts WHERE key = ?", (key,)).fetchone():
            continue
        asks = list_ready_asks(conn, load.lead.name, now)
        queue = find_backlog_line(read_notes(lead_dir(load.lead.name)))
        if (not asks and not queue) or is_waiting_on_own_run(conn, load.lead.surface):
            continue
        delivered = send_from_bridge(conn, config, load.lead.name, convert_idle_work_to_text(load, asks, queue), now)
        if delivered:
            record_signal_post(conn, key, delivered[0][0], now)
            sent.extend(delivered)
    return sent


def is_waiting_on_own_run(conn: sqlite3.Connection, surface: str | None) -> bool:
    """A lead whose tab still runs a command it started in the background (a test suite, a deploy) waits on it: its
    Claude idles at the prompt meanwhile, which is not room for new work."""
    rows = read_process_table() if surface else None
    if rows is None:
        return False
    pids = {
        int(row["pid"])
        for row in conn.execute(
            "SELECT pid FROM instances WHERE surface = ? AND ended_at IS NULL AND pid IS NOT NULL", (surface,)
        )
    }
    session = conn.execute("SELECT agterm_session FROM surfaces WHERE surface = ?", (surface,)).fetchone()
    tab = str(session["agterm_session"]) if session is not None and session["agterm_session"] else ""
    return bool(map_processes_to_live_work(rows, pids, tab))


def send_stale_focus_nudges(conn: sqlite3.Connection, config: Config, now: int) -> list[tuple[int, Recipient]]:
    """A lead whose focus line is over six hours old and that ended a turn after that: told once per focus line.
    The per-prompt nudge misses leads, whose turns mostly start with bridge's messages. Held while the owner is off."""
    if read_owner_presence(conn, config, now).off:
        return []
    sent: list[tuple[int, Recipient]] = []
    rows = conn.execute(
        """
        SELECT l.name, l.surface, c.focus, c.focus_at FROM leads l
        JOIN instances i ON i.surface = l.surface AND i.ended_at IS NULL
        JOIN conversations c ON c.conversation_id = i.conversation_id
        WHERE l.retired_at IS NULL AND c.focus IS NOT NULL AND c.focus_at <= ?1 AND c.last_seen_at >= c.focus_at + ?2
        """,
        (now - FOCUS_STALE_MS, FOCUS_STALE_MS),
    ).fetchall()
    for row in rows:
        key = f"focus-stale:{row['name']}:{row['focus_at']}"
        if is_surface_locked(conn, config, row["surface"]):
            continue
        if conn.execute("SELECT 1 FROM signal_posts WHERE key = ?", (key,)).fetchone():
            continue
        hours = (now - int(row["focus_at"])) // 3_600_000
        text = (
            f'Your focus line is {hours} h old: "{row["focus"]}". If it is not what you do now: '
            '`bridge focus "<your current task>"`.'
        )
        delivered = send_from_bridge(conn, config, str(row["name"]), text, now)
        if delivered:
            record_signal_post(conn, key, delivered[0][0], now)
            sent.extend(delivered)
    return sent


def convert_loads_to_team_line(loads: list[LeadLoad]) -> str:
    target = round(100 * (WORKER_CAP - 1) / WORKER_CAP) if WORKER_CAP else 0
    if not loads:
        return "team load: no leads"
    if any(load.workers is None for load in loads):
        return f"team load: unknown (agterm did not answer); target {target}%"
    used = sum(cast(int, load.workers) for load in loads)
    slots = len(loads) * WORKER_CAP
    return f"team load: {used}/{slots} worker slots, {round(100 * used / slots)}%; target {target}%"


def convert_load_to_day_line(load: LeadLoad) -> str:
    workers = "?" if load.workers is None else str(load.workers)
    if load.suggestions:
        nxt = convert_suggestion_to_text(load.suggestions[0])
    else:
        nxt = "nothing to suggest" + ("" if load.lead.domains else " (no domains set)")
    return f"{load.lead.display_name}: {load.load}, {workers}/{WORKER_CAP} workers; next: {nxt}"


def convert_loads_to_dry_run(
    conn: sqlite3.Connection, config: Config, loads: list[LeadLoad], presence: Presence, now: int
) -> list[str]:
    lines = [
        f"{config.owner_name_at_start}: {'off' if presence.off else 'on'}, {presence.why}"
        + ("; no signal goes while off" if presence.off else ""),
        convert_loads_to_team_line(loads),
    ]
    due = {load.lead.name for load in find_due_signals(conn, loads, now)}
    unreachable = find_assistant_unreachable(conn, config)
    for load in loads:
        lines.append("")
        if load.reason_key is None:
            lines.append(f"{load.lead.display_name}: not flagged, {load.not_flagged_why}")
        else:
            if load.lead.name not in due:
                sends = f"held (same reason sent under {SIGNAL_REPEAT_MS // 3_600_000} h ago)"
            elif presence.off:
                sends = "held (owner off)"
            elif unreachable:
                sends = f"held ({unreachable})"
            else:
                sends = "would be sent now"
            lines.append(f"{load.lead.display_name}: FLAGGED, {sends}")
            lines.extend(f"  {line}" for line in convert_load_to_signal(load).split("\n"))
        lines.extend(f"  chunk {c.verdict}: {c.why}" for c in load.chunks)
        if load.reason_key is None and load.suggestions:
            lines.append(f"  next: {convert_suggestion_to_text(load.suggestions[0])}")
        lines.extend(f"  note: {note}" for note in load.notes)
    return lines


def map_domains_to_warnings(domains: list[str], projects: list[Project]) -> list[str]:
    names = {p.name.lower() for p in projects}
    return [
        f"{d!r} is neither a priority project ({', '.join(p.name for p in projects) or 'none set'}) nor a Linear "
        "project ref like P-PROJ-41"
        for d in domains
        if d.lower() not in names and not LINEAR_PROJECT_RE.fullmatch(d)
    ]


def _stamp(ms: int) -> str:
    return time.strftime("%a %H:%M", time.localtime(ms / 1000))


def _convert_ms_to_age(ms: int) -> str:
    minutes = max(0, ms // 60_000)
    return f"{minutes}m" if minutes < 60 else f"{minutes // 60}h{minutes % 60:02d}m"
