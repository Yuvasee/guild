import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from bridge.activity import RunningShell, SessionActivity
from bridge.board import ACK_POST, SYSTEM_CONVERSATION, Party, Recipient, send_message
from bridge.claims import list_claims
from bridge.config import Config
from bridge.enrich import load_enrichment
from bridge.feed import DELIVERY_MARKS, OPEN_KINDS, RELAYED_ANSWER, USER_CONVERSATION, resolve_tab
from bridge.leads import TINTS, Lead, find_lead_on_surface, is_conversation_locked, list_lead_work, list_leads
from bridge.refs import map_ref_to_label
from bridge.registry import list_sessions
from bridge.remote import REMOTE_STALE_AFTER_MS
from bridge.signals import map_enrichment_to_status
from bridge.watcher import BLOCKED_AFTER_S

ROOM_POSTS = 80
CLAIM_LINES_FOR_MS = 24 * 3_600_000
STALE_QUESTION_MS = 3 * 24 * 3_600_000
WORKER_GONE_AFTER_MS = 2 * 3_600_000
TABS_STALE_MS = 45_000
ACTIVITY_STALE_MS = 45_000
SILENT_TURN_MS = 30 * 60_000
REMOTE_LINK_MS = 7 * 24 * 3_600_000
# The command that starts a run is logged just before it starts, and the two hosts' clocks differ a little.
REMOTE_LINK_BEFORE_START_MS = 10 * 60_000
_MESSAGE_POST_TYPES = (None, "report", "question", "handoff")
_SIGNATURE_RE = re.compile(r"\s*([^:()\n]{1,60}?)\s*(?:\(([^:()\n]{1,60})\))?\s*:\s*")
# A worker's tab keeps the brief's first words until its lead names it; those say nothing about the task.
_UNNAMED_TASK_RE = re.compile(r"worker brief|your brief", re.I)
NOTHING_FOR_ME = "nothing for me"
_SECTION_LABELS = r"What|Why now|Cost|Cost of [\w' -]{1,40}|[A-Z][\w'-]*(?: [\w'-]+){0,2} cost"
_ROLLUP_ORDER = ("stuck", "working", "waiting", "unknown", "rest")


@dataclass(frozen=True)
class Evidence:
    """Everything known about one session; None means that source has nothing to say, not that the answer is no."""

    tab_status: str | None
    tab_fresh: bool
    blocked_for_ms: int | None
    activity: SessionActivity | None
    silent_for_ms: int | None
    remote_running: tuple[str, ...]
    remote_unsure: tuple[str, ...]
    waits: tuple[str, ...]


def map_db_to_guild(
    conn: sqlite3.Connection,
    config: Config,
    now: int,
    url_of: Callable[[str], str],
    tree: dict[str, object] | None = None,
    tree_at: int | None = None,
    activity: dict[int, SessionActivity] | None = None,
    activity_at: int | None = None,
) -> dict[str, object]:
    sessions = list_sessions(conn, config, now)
    tabs = _map_tree_to_tabs(tree, config)
    tabs_fresh = tree is not None and (tree_at is None or now - tree_at <= TABS_STALE_MS)
    activity_fresh = activity is not None and (activity_at is None or now - activity_at <= ACTIVITY_STALE_MS)
    by_surface = {s.surface: s for s in sessions}
    enrichment = load_enrichment(conn, now)
    leads: list[dict[str, object]] = []
    needs: list[dict[str, object]] = _map_open_posts_to_needs(conn, config, now)
    authors: dict[str, str] = {}
    worker_tasks: dict[str, str] = {}
    active = list_leads(conn)
    for lead in active:
        if lead.surface and config.hides(*_tab_names(conn, lead.surface)):
            continue
        session = by_surface.get(lead.surface)
        conversation = session.conversation_id if session else None
        if conversation:
            authors[conversation] = lead.name
        tab_id = _agterm_session_of(conn, lead.surface)
        chunks = list_lead_work(conn, lead.name)
        chunk_refs = [ref for chunk in chunks for ref in chunk.refs]

        def evidence_for(pid: int | None, conversation_id: str | None, tab: dict[str, object] | None, refs: list[str]):
            return _gather_evidence(
                conn, config, now, tab, tabs_fresh, pid, conversation_id, activity if activity_fresh else None,
                enrichment, refs,
            )  # fmt: skip

        lead_tab = next((t for t in tabs if t.get("id") == tab_id), None)
        own: dict[str, object] = map_evidence_to_state(
            evidence_for(session.pid if session else None, conversation, lead_tab, chunk_refs)
        )
        workers: list[dict[str, object]] = []
        # The cap counts every nested tab, as `guild worker` does; the dots leave out the long idle ones.
        worker_count = sum(1 for tab in tabs if tab_id and tab.get("parent") == tab_id)
        for tab in tabs:
            if not tab_id or tab.get("parent") != tab_id:
                continue
            instance = _load_tab_instance(conn, str(tab.get("id")))
            worker_conversation = str(instance["conversation_id"]) if instance and instance["conversation_id"] else None
            if worker_conversation:
                authors[worker_conversation] = lead.name
                worker_tasks[worker_conversation] = str(tab.get("name") or "")
            state = map_evidence_to_state(
                evidence_for(int(instance["pid"]) if instance else None, worker_conversation, tab, [])
            )
            if tabs_fresh and _worker_is_gone(str(state["state"]), tab, instance is not None, now):
                continue
            workers.append({"task": tab.get("name"), "surface": instance["surface"] if instance else None, **state})
        if session is None or session.status in ("offline", "ended"):
            own = {"state": "stuck", "why": "no live session"}
            needs.append(
                {
                    "type": "lead-down",
                    "lead": lead.display_name,
                    "surface": lead.surface,
                    "text": f"{lead.display_name} has no live session",
                }
            )
        tab_name, tint = _find_tab_of_lead(conn, lead, tree, tab_id)
        leads.append(
            {
                "name": lead.name,
                "display_name": lead.display_name,
                "tab_name": tab_name,
                "tint": tint,
                "surface": lead.surface,
                **map_states_to_rollup(own, workers),
                "workers": workers,
                "worker_count": worker_count,
                "worker_cap": config.worker_cap,
                "asks_you": _has_open_post(conn, conversation, now),
                "work": [
                    {"name": c.name, "links": [_map_ref_to_link(r, config, enrichment, url_of) for r in c.refs]}
                    for c in chunks
                ],
            }
        )
    needs.extend(_load_undelivered_to_needs(conn, config))
    needs.extend(_load_assistant_undelivered_to_needs(conn, config))
    return {
        "now": now,
        "tabs_at": tree_at,
        "activity_at": activity_at if activity_fresh else None,
        "user": config.user_alias or "user",
        "needs": needs,
        "leads": leads,
        "room": _load_room(conn, config, now, url_of, active, authors, worker_tasks),
        "claims": _load_active_claims(conn, config, now, url_of),
    }


def map_evidence_to_state(evidence: Evidence) -> dict[str, object]:
    """Claude's session file and process tree beat agterm's status, which a crash or an Esc can leave behind."""
    if evidence.tab_fresh and evidence.tab_status == "blocked":
        blocked = evidence.blocked_for_ms
        if blocked is not None and blocked >= BLOCKED_AFTER_S * 1000:
            return {"state": "stuck", "why": f"a dialog has been open in its tab for {_convert_ms_to_age(blocked)}"}
        return {"state": "waiting", "why": "waiting on a dialog in its tab"}
    activity = evidence.activity
    if activity is not None:
        if activity.file_status == "busy":
            if activity.shells:
                return {"state": "working", "why": _describe_shell(activity.shells)}
            if evidence.silent_for_ms is not None and evidence.silent_for_ms > SILENT_TURN_MS:
                silence = _convert_ms_to_age(evidence.silent_for_ms)
                return {"state": "unknown", "why": f"says it is busy, but wrote nothing for {silence}"}
            return {"state": "working", "why": "in a turn"}
        if activity.shells:
            return {"state": "working", "why": _describe_shell(activity.shells)}
        if activity.file_status == "shell":
            return {"state": "working", "why": "a shell is running"}
    if evidence.remote_running:
        return {"state": "working", "why": "running on " + evidence.remote_running[0]}
    if evidence.remote_unsure:
        return {"state": "unknown", "why": f"cannot tell whether {evidence.remote_unsure[0]} is running"}
    if activity is None:
        if not evidence.tab_fresh:
            return {"state": "unknown", "why": "agterm is not answering"}
        if evidence.tab_status in ("active", "completed"):
            return {"state": "unknown", "why": "no process found for this tab"}
        return {"state": "rest", "why": "no agent in this tab"}
    if evidence.waits:
        return {"state": "waiting", "why": evidence.waits[0]}
    if activity.file_status == "idle":
        return {"state": "rest", "why": "turn ended, nothing running"}
    return {"state": "unknown", "why": "no session file for this process"}


def map_states_to_rollup(own: dict[str, object], workers: list[dict[str, object]]) -> dict[str, object]:
    """A lead whose turn ended with workers running is working: the row must say what its team is doing."""
    states = [str(own["state"]), *[str(w["state"]) for w in workers]]
    top = next(state for state in _ROLLUP_ORDER if state in states)
    if top == own["state"]:
        return dict(own)
    matching = [w for w in workers if w["state"] == top]
    if top == "waiting":
        return {"state": top, "why": matching[0]["why"]}
    names = ", ".join(str(w["task"]) for w in matching[:2]) + (f" +{len(matching) - 2}" if len(matching) > 2 else "")
    return {"state": top, "why": f"{names}: {matching[0]['why']}"}


def _gather_evidence(
    conn: sqlite3.Connection,
    config: Config,
    now: int,
    tab: dict[str, object] | None,
    tabs_fresh: bool,
    pid: int | None,
    conversation_id: str | None,
    activity: dict[int, SessionActivity] | None,
    enrichment: dict[str, dict[str, object]],
    extra_refs: list[str],
) -> Evidence:
    status = tab.get("status") if tab else None
    blocked_for: int | None = None
    if status == "blocked" and tab:
        notice = conn.execute(
            "SELECT first_seen_at FROM blocked_notices WHERE agterm_session = ?", (str(tab.get("id")),)
        ).fetchone()
        blocked_for = now - int(notice[0]) if notice else None
    session_activity = activity.get(pid) if activity is not None and pid is not None else None
    last_write = session_activity.last_write_ms if session_activity else None
    running, unsure = _load_remote_work(conn, config, conversation_id, now)
    return Evidence(
        tab_status=str(status) if status else None,
        tab_fresh=tabs_fresh,
        blocked_for_ms=blocked_for,
        activity=session_activity,
        silent_for_ms=now - last_write if last_write else None,
        remote_running=running,
        remote_unsure=unsure,
        waits=tuple(_load_waits(conn, config, conversation_id, now, enrichment, extra_refs)),
    )


def _describe_shell(shells: tuple[RunningShell, ...]) -> str:
    longest = max(shells, key=lambda shell: shell.for_ms)
    more = f" (+{len(shells) - 1} more)" if len(shells) > 1 else ""
    return f"running {longest.label} for {_convert_ms_to_age(longest.for_ms)}{more}"


def _convert_ms_to_age(ms: int) -> str:
    minutes = max(0, ms // 60_000)
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h" if minutes < 1440 else f"{minutes // 1440}d"


def _load_remote_work(
    conn: sqlite3.Connection, config: Config, conversation_id: str | None, now: int
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Remote tmux sessions this conversation drove (by name or id) that run something, or may: (running, unsure).

    A link holds for days, since a run outlives the command that started it, but not for a later run under one name.
    """
    if conversation_id is None:
        return (), ()
    running: list[str] = []
    unsure: list[str] = []
    for link in conn.execute(
        """
        SELECT d.host, d.session, d.last_seen_at, h.ok_at FROM remote_drivers d
        LEFT JOIN remote_hosts h ON h.host = d.host
        WHERE d.conversation_id = ? AND d.last_seen_at > ?
        """,
        (conversation_id, now - REMOTE_LINK_MS),
    ):
        host, session = str(link["host"]), str(link["session"])
        fresh = link["ok_at"] is not None and now - int(link["ok_at"]) <= REMOTE_STALE_AFTER_MS
        earliest_start = int(link["last_seen_at"]) + REMOTE_LINK_BEFORE_START_MS
        for row in conn.execute(
            """
            SELECT tmux, started_at, 'working' AS state FROM remote_runs WHERE host = ?1 AND (tmux = ?2 OR tmux_id = ?2)
            UNION ALL
            SELECT json_extract(data, '$.tmux'), json_extract(data, '$.started_at'), json_extract(data, '$.state')
            FROM remote_sessions WHERE host = ?1
              AND (json_extract(data, '$.tmux') = ?2 OR json_extract(data, '$.tmux_id') = ?2)
            """,
            (host, session),
        ):
            started = row["started_at"]
            if config.hides(str(row["tmux"])) or (started is not None and int(started) > earliest_start):
                continue
            label = f"{host}/{row['tmux']}"
            if row["state"] == "unknown" or (row["state"] == "working" and not fresh):
                unsure.append(label)
            elif row["state"] == "working":
                running.append(label)
    return tuple(running), tuple(unsure)


def _worker_is_gone(state: str, tab: dict[str, object], has_process: bool, now: int) -> bool:
    """Stale means gone, but a live process whose state cannot be read stays: that is what unknown is for."""
    if state == "unknown" and has_process:
        return False
    changed = tab.get("statusChangedAt")
    if not isinstance(changed, int | float):
        return state == "rest"
    return state in ("rest", "unknown") and now - changed * 1000 > WORKER_GONE_AFTER_MS


def _agterm_session_of(conn: sqlite3.Connection, surface: str | None) -> str | None:
    row = conn.execute("SELECT agterm_session FROM surfaces WHERE surface = ?", (surface,)).fetchone()
    return str(row[0]) if row and row[0] else None


def _load_tab_instance(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT i.pid, i.conversation_id, i.surface FROM surfaces s
        JOIN instances i ON i.surface = s.surface AND i.ended_at IS NULL
        WHERE s.agterm_session = ? ORDER BY i.proc_start_ms DESC LIMIT 1
        """,
        (session_id,),
    ).fetchone()


def _map_tree_to_tabs(tree: dict[str, object] | None, config: Config) -> list[dict[str, object]]:
    tabs: list[dict[str, object]] = []
    workspaces = tree.get("workspaces") if tree else None
    for workspace in cast(list[dict[str, object]], workspaces if isinstance(workspaces, list) else []):
        sessions = workspace.get("sessions")
        for tab in cast(list[dict[str, object]], sessions if isinstance(sessions, list) else []):
            if not config.hides(str(tab.get("name")), str(workspace.get("name"))):
                tabs.append(tab)
    return tabs


def _load_waits(
    conn: sqlite3.Connection,
    config: Config,
    conversation_id: str | None,
    now: int,
    enrichment: dict[str, dict[str, object]],
    extra_refs: list[str],
) -> list[str]:
    """Named someones only, each backed by a row: an open post, an open request, or CI running on the session's PR."""
    waits: list[str] = []
    if conversation_id is not None:
        if _has_open_post(conn, conversation_id, now):
            waits.append("waiting for you")
        for row in conn.execute(
            """
            SELECT r.handle, r.conversation_id FROM messages m JOIN message_recipients r ON r.message_id = m.id
            WHERE m.from_conversation = ? AND m.kind = 'request' AND m.cancelled_at IS NULL AND m.created_at > ?
              AND r.acked_at IS NULL AND r.done_at IS NULL AND r.declined_at IS NULL
            """,
            (conversation_id, now - STALE_QUESTION_MS),
        ):
            if not is_conversation_locked(conn, config, str(row["conversation_id"])):
                waits.append(f"waiting on {row['handle']}")
    refs = list(extra_refs)
    if conversation_id is not None:
        refs += [
            str(r[0])
            for r in conn.execute(
                """
                SELECT ref FROM refs WHERE conversation_id = ?1 AND source = 'pr-link'
                UNION SELECT resource FROM claims WHERE holder_conversation = ?1
                """,
                (conversation_id,),
            )
        ]
    for ref in dict.fromkeys(refs):
        data = enrichment.get(ref)
        if (
            ref.startswith("pr:")
            and data
            and data.get("state") == "OPEN"
            and data.get("checks") in ("PENDING", "EXPECTED")
            and not data.get("stale")
        ):
            waits.append(f"waiting on CI, {map_ref_to_label(ref, config.repo_labels)}")
    return waits


def _has_open_post(conn: sqlite3.Connection, conversation_id: str | None, now: int) -> bool:
    if conversation_id is None:
        return False
    marks = ",".join("?" * len(OPEN_KINDS))
    row = conn.execute(
        f"""
        SELECT 1 FROM user_items WHERE from_conversation = ? AND resolved_at IS NULL AND source = 'agent'
          AND kind IN ({marks}) AND created_at > ? LIMIT 1
        """,
        (conversation_id, *OPEN_KINDS, now - STALE_QUESTION_MS),
    ).fetchone()
    return row is not None


def _map_ref_to_link(
    ref: str, config: Config, enrichment: dict[str, dict[str, object]], url_of: Callable[[str], str]
) -> dict[str, object]:
    data = enrichment.get(ref)
    # A state past its refresh is left out: an old "CI running" on the page would be a guess.
    status = map_enrichment_to_status(ref, data) if data and "state" in data and not data.get("stale") else None
    finished = bool(data) and (
        data.get("state") in ("MERGED", "CLOSED") or data.get("state_type") in ("completed", "canceled")
    )
    return {
        "kind": ref.partition(":")[0],
        "label": map_ref_to_label(ref, config.repo_labels),
        "url": url_of(ref),
        "state": status.label if status else None,
        "title": str(data.get("title") or "") if data else "",
        "finished": finished,
    }


def post_signals_to_leads(
    conn: sqlite3.Connection, config: Config, signals: list[dict[str, object]], now: int
) -> list[Recipient]:
    """Each signal once, by its key, which stays the same while the underlying state does."""
    posted: list[Recipient] = []
    for signal in signals:
        key = str(signal["key"])
        surface = signal.get("surface")
        lead = find_lead_on_surface(conn, surface if isinstance(surface, str) else None)
        if lead is None or conn.execute("SELECT 1 FROM signal_posts WHERE key = ?", (key,)).fetchone():
            continue
        row = conn.execute(
            "SELECT conversation_id FROM instances WHERE surface = ? AND ended_at IS NULL "
            "AND conversation_id IS NOT NULL ORDER BY proc_start_ms DESC LIMIT 1",
            (lead.surface,),
        ).fetchone()
        if row is None or is_conversation_locked(conn, config, str(row[0])):
            continue
        recipient = resolve_tab(conn, str(row[0]), lead.name)
        text = f"{signal['text']} ({signal['kind']})"
        result = send_message(conn, Party(SYSTEM_CONVERSATION, "bridge"), [recipient], "info", text, now)
        # Not on the board: routine CI and review are for the lead, and the user must not see them.
        conn.execute(
            "UPDATE messages SET post_type = 'report', link = ? WHERE id = ?",
            (signal.get("url") or None, result.message_id),
        )
        conn.execute(
            "INSERT INTO signal_posts (key, message_id, posted_at) VALUES (?, ?, ?)", (key, result.message_id, now)
        )
        posted.append(recipient)
    return posted


def _tab_names(conn: sqlite3.Connection, surface: str) -> tuple[str | None, str | None]:
    row = conn.execute("SELECT tab_name, workspace_name FROM surfaces WHERE surface = ?", (surface,)).fetchone()
    return (row["tab_name"], row["workspace_name"]) if row else (None, None)


def _find_tab_of_lead(
    conn: sqlite3.Connection, lead: Lead, tree: dict[str, object] | None, tab_id: str | None
) -> tuple[str, str | None]:
    """(name, tint) as agterm shows them now; without its tree, what bridge last saw and the tint it asked for."""
    workspaces = tree.get("workspaces") if tree else None
    for workspace in cast(list[dict[str, object]], workspaces if isinstance(workspaces, list) else []):
        sessions = workspace.get("sessions")
        for tab in cast(list[dict[str, object]], sessions if isinstance(sessions, list) else []):
            if tab_id and tab.get("id") == tab_id:
                tint = tab.get("tint")
                return str(tab.get("name") or lead.tab_title), str(tint) if tint in TINTS else None
    return _tab_names(conn, lead.surface or "")[0] or lead.tab_title, lead.tint


def map_body_to_sections(body: str, options: list[str]) -> list[dict[str, object]]:
    """A question's labels, each starting a section at a line start or after a sentence; an answer's name counts as a
    label. Anything else stays the author's text, line breaks included."""
    named = sorted((re.escape(option) for option in options if option), key=len, reverse=True)
    answers = "".join(f"|{option} cost|{option}" for option in named)
    found = list(re.finditer(rf"(?m)(?:^[ \t]*|(?<=[.!?])[ \t]+)({_SECTION_LABELS}{answers}):[ \t]*", body))
    if not found:
        return [{"label": None, "text": body}]
    before = body[: found[0].start()].rstrip()
    sections: list[dict[str, object]] = [{"label": None, "text": before}] if before else []
    ends = [match.start() for match in found[1:]] + [len(body)]
    for match, end in zip(found, ends, strict=True):
        sections.append({"label": match.group(1), "text": body[match.end() : end].strip()})
    return sections


def _map_open_posts_to_needs(conn: sqlite3.Connection, config: Config, now: int) -> list[dict[str, object]]:
    """A question leaves only when its answer has reached the session, or they dismiss it. A claim to type it counts
    as offered so a hook does not deliver it too, but it has not arrived until the typing is over."""
    marks = ",".join("?" * len(OPEN_KINDS))
    needs: list[dict[str, object]] = []
    for row in conn.execute(
        f"""
        SELECT id, created_at, from_conversation, from_handle, kind, text, body, links, options, urgent, answer,
               resolved_at, answer_message_id,
               (SELECT max(r.delivery_failed_at IS NOT NULL AND r.unproven_at IS NULL) FROM message_recipients r
                WHERE r.message_id = answer_message_id) AS delivery_failed,
               (SELECT r.handle FROM message_recipients r WHERE r.message_id = answer_message_id LIMIT 1) AS answer_to,
               (SELECT i.surface FROM message_recipients r JOIN instances i ON i.conversation_id = r.conversation_id
                WHERE r.message_id = answer_message_id AND i.ended_at IS NULL
                ORDER BY i.proc_start_ms DESC LIMIT 1) AS surface
        FROM user_items
        WHERE source = 'agent' AND kind IN ({marks})
          AND ((resolved_at IS NULL AND created_at > ?)
               OR (answer_message_id IS NOT NULL AND answer_dismissed_at IS NULL AND NOT EXISTS (
                   SELECT 1 FROM message_recipients r WHERE r.message_id = answer_message_id
                     AND (r.offered_at IS NOT NULL OR r.acked_at IS NOT NULL) AND r.typing_started_at IS NULL)))
        ORDER BY urgent DESC, created_at
        """,
        (*OPEN_KINDS, now - STALE_QUESTION_MS),
    ):
        if is_conversation_locked(conn, config, str(row["from_conversation"])):
            continue
        options: list[str] = json.loads(row["options"]) if row["options"] else []
        delivering = row["answer_message_id"] is not None and row["resolved_at"] is not None
        needs.append(
            {
                "type": "post",
                "id": row["id"],
                "at": row["created_at"],
                "kind": row["kind"],
                "from": row["from_handle"],
                "text": row["text"],
                "urgent": bool(row["urgent"]),
                "sections": map_body_to_sections(str(row["body"]), options) if row["body"] else [],
                "links": json.loads(row["links"]) if row["links"] else [],
                "options": options,
                **(
                    {
                        "delivering": True,
                        "answer": row["answer"],
                        "delivery_failed": bool(row["delivery_failed"]),
                        "answer_to": row["answer_to"],
                        "surface": row["surface"],
                    }
                    if delivering
                    else {}
                ),
            }
        )
    return needs


def _load_undelivered_to_needs(conn: sqlite3.Connection, config: Config) -> list[dict[str, object]]:
    needs: list[dict[str, object]] = []
    for row in conn.execute(
        """
        SELECT m.body, r.handle, r.conversation_id,
               (SELECT i.surface FROM instances i WHERE i.conversation_id = r.conversation_id AND i.ended_at IS NULL
                ORDER BY i.proc_start_ms DESC LIMIT 1) AS surface
        FROM message_recipients r JOIN messages m ON m.id = r.message_id
        WHERE r.delivery_failed_at IS NOT NULL AND r.acked_at IS NULL AND m.cancelled_at IS NULL
          AND r.unproven_at IS NULL AND (m.from_conversation = ? OR m.post_type = ?)
          AND m.id NOT IN (SELECT answer_message_id FROM user_items WHERE answer_message_id IS NOT NULL)
        """,
        (USER_CONVERSATION, RELAYED_ANSWER),
    ):
        if is_conversation_locked(conn, config, str(row["conversation_id"])):
            continue
        needs.append(
            {
                "type": "undelivered",
                "lead": row["handle"],
                "surface": row["surface"],
                "text": f"Your message has not reached {row['handle']}'s tab yet; it may hold a draft. Retrying.",
                "body": row["body"],
            }
        )
    return needs


def _load_assistant_undelivered_to_needs(conn: sqlite3.Connection, config: Config) -> list[dict[str, object]]:
    """Its tab cannot be told through itself."""
    return [
        {"type": "undelivered", "lead": row["handle"], "surface": row["surface"], "text": row["detail"]}
        for row in conn.execute(
            "SELECT handle, surface, detail FROM undelivered_tabs WHERE handle = ? AND detail IS NOT NULL",
            (config.assistant_handle,),
        )
    ]


def _load_room(
    conn: sqlite3.Connection,
    config: Config,
    now: int,
    url_of: Callable[[str], str],
    leads: list[Lead],
    authors: dict[str, str],
    worker_tasks: dict[str, str],
) -> list[dict[str, object]]:
    display = {lead.name: lead.display_name for lead in leads}

    def who(conversation: str, handle: str, owner: str | None) -> str:
        if owner:
            lead_name = display.get(owner, owner)
            task = worker_tasks.get(conversation, "")
            return f"{task} · {lead_name}" if task and not _UNNAMED_TASK_RE.search(task) else lead_name
        if conversation == USER_CONVERSATION or handle == config.assistant_handle:
            return handle.capitalize()
        return _load_tab_name(conn, conversation) or handle

    roots = conn.execute(
        """
        SELECT thread_id, max(id) AS last FROM messages
        WHERE thread_id IN (SELECT thread_id FROM messages WHERE board = 1) AND post_type IS NOT ?
        GROUP BY thread_id ORDER BY last DESC LIMIT ?
        """,
        (ACK_POST, ROOM_POSTS),
    ).fetchall()
    threads: dict[int, list[dict[str, object]]] = {}
    marks = ",".join("?" * len(roots)) or "NULL"
    lead_names = {lead.name for lead in leads}
    for row in conn.execute(
        f"""
        SELECT id, thread_id, reply_to, created_at, from_conversation, from_handle, body, post_type, thread_key, link,
               paused, topic
        FROM messages WHERE thread_id IN ({marks}) AND cancelled_at IS NULL ORDER BY id
        """,
        [r["thread_id"] for r in roots],
    ):
        conversation = str(row["from_conversation"])
        if is_conversation_locked(conn, config, conversation):
            continue
        handle = str(row["from_handle"])
        owner = authors.get(conversation) or (handle if handle in lead_names else None)
        post_type = None if row["post_type"] in DELIVERY_MARKS else row["post_type"]
        threads.setdefault(int(row["thread_id"]), []).append(
            {
                "id": row["id"],
                "reply_to": row["reply_to"],
                "at": row["created_at"],
                "from": row["from_handle"],
                "who": who(conversation, handle, owner),
                "lead": owner,
                "level": _map_post_to_level(conversation, handle, post_type, authors, lead_names, config),
                "body": convert_body_to_shown_text(str(row["body"]), (handle, owner)),
                "post_type": post_type,
                "thread_key": row["thread_key"],
                "topic": row["topic"],
                "link": row["link"],
                "paused": bool(row["paused"]),
            }
        )
    in_force = {str(c["resource"]) for c in _load_active_claims(conn, config, now, url_of)}

    def who_saw(conversation: str, handle: str) -> str:
        # An unnamed worker writes as its lead, but its receipt must not read as the lead having seen the post.
        if _UNNAMED_TASK_RE.search(worker_tasks.get(conversation, "")):
            return handle
        return who(conversation, handle, authors.get(conversation) or (handle if handle in lead_names else None))

    seen_by = _load_seen_by(conn, config, [str(p["id"]) for posts in threads.values() for p in posts], who_saw)
    room: list[dict[str, object]] = []
    for everything in threads.values():
        posts = [post for post in everything if post["post_type"] != ACK_POST]
        if not posts:
            continue
        acks = [post for post in everything if post["post_type"] == ACK_POST]
        for post in posts:
            names = [*seen_by.get(int(str(post["id"])), [])]
            names += [str(a["who"]) for a in acks if a.get("reply_to") == post["id"] and a["who"] not in names]
            post["seen_by"] = names
            post["acks"] = [{"who": a["who"], "body": a["body"]} for a in acks if a.get("reply_to") == post["id"]]
        levels = {post["level"] for post in posts}
        # A status line that drew a real answer is a conversation, and the owner asked to see leads talking.
        if any(" ".join(str(a["body"]).lower().split()).strip(" .") != NOTHING_FOR_ME for a in acks):
            levels.add("message")
        names = sorted({str(post["lead"]) for post in posts if post["lead"]})
        room.append(
            {
                "kind": "post",
                "at": posts[-1]["at"],
                **posts[0],
                "level": "message" if "message" in levels else "line",
                "leads": names,
                "replies": posts[1:],
            }
        )
    for row in conn.execute(
        """
        SELECT l.resource, l.holder_conversation, l.holder_handle, l.reason, l.taken_at, l.ended_at, l.end_kind,
               l.taken_at AS at
        FROM claim_log l
        WHERE l.taken_at > ? OR l.ended_at > ?
        """,
        (now - CLAIM_LINES_FOR_MS, now - CLAIM_LINES_FOR_MS),
    ):
        conversation = str(row["holder_conversation"])
        if is_conversation_locked(conn, config, conversation):
            continue
        ended = row["end_kind"]
        if not ended and row["resource"] not in in_force:
            continue
        handle = str(row["holder_handle"])
        owner = authors.get(conversation) or (handle if handle in lead_names else None)
        room.append(
            {
                "kind": "claim",
                "level": "line",
                "at": row["taken_at"],
                "from": row["holder_handle"],
                "who": who(conversation, handle, owner),
                "leads": [owner] if owner else [],
                "resource": row["resource"],
                "label": _convert_resource_to_label(str(row["resource"]), config),
                "reason": row["reason"],
                "state": ended or "held",
                "url": url_of(str(row["resource"])),
            }
        )
    return sorted(room, key=lambda item: int(str(item["at"])))[-ROOM_POSTS:]


def convert_body_to_shown_text(body: str, author_names: tuple[str | None, ...]) -> str:
    """Leads sign their posts ("Ada: Ada: …", "Access (Boris): …"), and the page names the author above the text."""
    names = {name.lower() for name in author_names if name}
    shown = body
    while match := _SIGNATURE_RE.match(shown):
        rest = shown[match.end() :]
        signer = (match.group(2) or match.group(1)).strip().lower()
        if signer not in names or not rest.strip():
            break
        shown = rest
    return shown


def _load_tab_name(conn: sqlite3.Connection, conversation: str) -> str | None:
    row = conn.execute(
        """
        SELECT s.tab_name FROM conversations c JOIN instances i ON i.instance_id = c.instance_id
        JOIN surfaces s ON s.surface = i.surface WHERE c.conversation_id = ?
        """,
        (conversation,),
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def _load_seen_by(
    conn: sqlite3.Connection,
    config: Config,
    message_ids: list[str],
    who_saw: Callable[[str, str], str],
) -> dict[int, list[str]]:
    """Only what really reached a session: a row is written at sending, and offered_at when a hook delivers it."""
    marks = ",".join("?" * len(message_ids)) or "NULL"
    seen: dict[int, list[str]] = {}
    for row in conn.execute(
        f"""
        SELECT message_id, conversation_id, handle FROM message_recipients
        WHERE message_id IN ({marks}) AND (offered_at IS NOT NULL OR acked_at IS NOT NULL) ORDER BY handle
        """,
        message_ids,
    ):
        conversation, handle = str(row["conversation_id"]), str(row["handle"])
        if is_conversation_locked(conn, config, conversation) or conversation == USER_CONVERSATION:
            continue
        names = seen.setdefault(int(row["message_id"]), [])
        name = who_saw(conversation, handle)
        if name not in names:
            names.append(name)
    return seen


def _map_post_to_level(
    conversation: str,
    handle: str,
    post_type: str | None,
    authors: dict[str, str],
    lead_names: set[str],
    config: Config,
) -> str:
    """A message is someone on the team saying something; status lines and everyone else stay barely visible."""
    if conversation == SYSTEM_CONVERSATION or post_type not in _MESSAGE_POST_TYPES:
        return "line"
    team = conversation == USER_CONVERSATION or handle == config.assistant_handle
    return "message" if team or conversation in authors or handle in lead_names else "line"


def _convert_resource_to_label(resource: str, config: Config) -> str:
    kind, _, value = resource.partition(":")
    if kind == "branch":
        return value.rpartition(":")[2]
    return map_ref_to_label(resource, config.repo_labels) if kind in ("pr", "gh-issue", "issue") else value or resource


def _load_active_claims(
    conn: sqlite3.Connection, config: Config, now: int, url_of: Callable[[str], str]
) -> list[dict[str, object]]:
    """Only claims in force: a lapsed one is for its holder to settle, and the user is not asked."""
    active: list[dict[str, object]] = []
    for row in list_claims(conn):
        if is_conversation_locked(conn, config, str(row["holder_conversation"])):
            continue
        if not row["live"] or int(row["expires_at"]) < now:
            continue
        active.append(
            {
                "resource": row["resource"],
                "kind": str(row["resource"]).partition(":")[0],
                "label": _convert_resource_to_label(str(row["resource"]), config),
                "holder": row["holder_handle"],
                "reason": row["reason"],
                "acquired_at": row["acquired_at"],
                "url": url_of(str(row["resource"])),
            }
        )
    return sorted(active, key=lambda claim: (claim["kind"] in ("pr", "branch"), str(claim["resource"])))
