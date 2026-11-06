import json
import re
import sqlite3
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, replace

from bridge.claude import ClaudeSessionFile, list_session_files
from bridge.config import Config
from bridge.db import transaction
from bridge.gitinfo import GitInfo, read_git_info
from bridge.procinfo import pid_alive, process_args, process_start_ms
from bridge.refs import extract_branch_refs, extract_refs
from bridge.transcript import find_transcript, read_last_turn, read_session_title

FOCUS_MAX_CHARS = 120
SNIPPET_MAX_CHARS = 240
PROC_START_TOLERANCE_MS = 2_000
SESSION_FILE_START_TOLERANCE_MS = 60_000
RECENT_REF_TURNS = 5
NEVER_RECENT_TURN = -1_000_000_000
"""last_turn of a PR link that was never mentioned in a turn: no session is young enough to count it as recent."""


def current_ref_sql(min_turn: str, *, for_polling: bool = False) -> str:
    """Refs a session is on now: mentioned in recent turns, or a PR it created that is still open.

    Polling also keeps closed PR links, which can be reopened; only merged ones are final.
    Expects refs as r and a LEFT JOIN of enrichment as e.
    """
    state = "coalesce(json_extract(e.data, '$.state'), 'OPEN')"
    alive = f"{state} <> 'MERGED'" if for_polling else f"{state} = 'OPEN'"
    return f"(r.last_turn >= {min_turn} OR (r.source = 'pr-link' AND {alive}))"


_FILE_STATUS = {"idle": "idle", "busy": "active"}
_PASTE_TAG_RE = re.compile(r"</?pasted_content[^>]*>")


@dataclass(frozen=True)
class Surface:
    surface: str
    agterm_session: str | None
    agterm_workspace: str | None


@dataclass(frozen=True)
class Observation:
    pid: int
    proc_start_ms: int
    conversation_id: str | None
    cwd: str | None
    surface: Surface | None
    kind: str | None = None
    peer_name: str | None = None
    version: str | None = None
    peer_messaging: bool | None = None
    transcript_path: str | None = None
    git: GitInfo | None = None
    may_switch_conversation: bool = True


@dataclass(frozen=True)
class SessionView:
    instance_id: str
    handle: str | None
    tab_name: str | None
    workspace_name: str | None
    status: str
    pid: int
    conversation_id: str | None
    peer_name: str | None
    cwd: str | None
    repo: str | None
    branch: str | None
    focus: str | None
    focus_at: int | None
    last_prompt: str | None
    last_reply: str | None
    turns: int
    last_activity_at: int | None
    last_observed_at: int
    refs: list[str]
    surface: str | None = None
    agterm_session: str | None = None
    attention: str | None = None
    attention_at: int | None = None
    restore_command: str | None = None
    context_tokens: int | None = None


def make_instance_id(pid: int, proc_start_ms: int) -> str:
    return f"{pid}@{proc_start_ms}"


def map_env_to_surface(env: Mapping[str, str]) -> Surface | None:
    pane = env.get("AGTERM_PANE_ID")
    if not pane:
        return None
    return Surface(
        surface=pane,
        agterm_session=env.get("AGTERM_SESSION_ID"),
        agterm_workspace=env.get("AGTERM_WORKSPACE_ID"),
    )


def is_nested_agent(pid: int, proc_env: Mapping[str, str]) -> bool:
    """A claude started from inside another agent's shell inherits that agent's tab ids but does not own the tab."""
    parent = proc_env.get("CLAUDE_PID")
    return (parent is not None and parent != str(pid)) or "CLAUDECODE" in proc_env


@dataclass(frozen=True)
class ObserveResult:
    instance_id: str
    current: bool
    previous_conversation_id: str | None
    """False when a newer event already updated this instance; such events only touch their own conversation."""


def observe_instance(
    conn: sqlite3.Connection, obs: Observation, at_ms: int, event_ns: int | None = None
) -> ObserveResult:
    """Upserts an instance; conversation and cwd change only for events newer than the last applied one."""
    instance_id = make_instance_id(obs.pid, obs.proc_start_ms)
    if obs.surface is not None:
        conn.execute(
            """
            INSERT INTO surfaces (surface, agterm_session, agterm_workspace, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (surface) DO UPDATE SET
                agterm_session = coalesce(excluded.agterm_session, agterm_session),
                agterm_workspace = coalesce(excluded.agterm_workspace, agterm_workspace),
                updated_at = excluded.updated_at
            """,
            (obs.surface.surface, obs.surface.agterm_session, obs.surface.agterm_workspace, at_ms),
        )
    git = obs.git
    row = conn.execute(
        "SELECT last_event_ns, conversation_id, cwd FROM instances WHERE instance_id = ?", (instance_id,)
    ).fetchone()
    if row is None:
        current = True
        conn.execute(
            """
            INSERT INTO instances (
                instance_id, pid, proc_start_ms, conversation_id, surface, kind, peer_name, version, peer_messaging,
                cwd, repo_root, repo, branch, first_seen_at, last_observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                instance_id,
                obs.pid,
                obs.proc_start_ms,
                obs.conversation_id,
                obs.surface.surface if obs.surface else None,
                obs.kind,
                obs.peer_name,
                obs.version,
                obs.peer_messaging,
                obs.cwd,
                git.repo_root if git else None,
                git.repo if git else None,
                git.branch if git else None,
                at_ms,
                at_ms,
            ),
        )
    else:
        newer = event_ns is not None and event_ns > int(row["last_event_ns"])
        switch = obs.may_switch_conversation and newer
        # Only prompts and session starts move a tab to another conversation; a late Stop of the old one must not.
        current = newer and (obs.conversation_id is None or obs.conversation_id == row["conversation_id"] or switch)
        conn.execute(
            """
            UPDATE instances SET
                surface = coalesce(?, surface), kind = coalesce(?, kind), peer_name = coalesce(?, peer_name),
                version = coalesce(?, version), peer_messaging = coalesce(?, peer_messaging),
                last_observed_at = max(last_observed_at, ?)
            WHERE instance_id = ?
            """,
            (
                obs.surface.surface if obs.surface else None,
                obs.kind,
                obs.peer_name,
                obs.version,
                obs.peer_messaging,
                at_ms,
                instance_id,
            ),
        )
        if obs.conversation_id and (switch or row["conversation_id"] is None):
            conn.execute(
                "UPDATE instances SET conversation_id = ? WHERE instance_id = ?", (obs.conversation_id, instance_id)
            )
        if obs.cwd and (current or row["cwd"] is None):
            conn.execute(
                "UPDATE instances SET cwd = ?, repo_root = ?, repo = ?, branch = ? WHERE instance_id = ?",
                (
                    obs.cwd,
                    git.repo_root if git else None,
                    git.repo if git else None,
                    git.branch if git else None,
                    instance_id,
                ),
            )
    if obs.conversation_id is not None:
        conn.execute(
            """
            INSERT INTO conversations (
                conversation_id, instance_id, transcript_path, first_seen_at, last_seen_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (conversation_id) DO UPDATE SET
                instance_id = excluded.instance_id,
                transcript_path = coalesce(excluded.transcript_path, transcript_path),
                last_seen_at = max(last_seen_at, excluded.last_seen_at)
            """,
            (obs.conversation_id, instance_id, obs.transcript_path, at_ms, at_ms),
        )
    previous = str(row["conversation_id"]) if row is not None and row["conversation_id"] else None
    return ObserveResult(instance_id=instance_id, current=current, previous_conversation_id=previous)


def record_activity(conn: sqlite3.Connection, instance_id: str, status: str, event_ns: int) -> bool:
    """Applies a status transition unless a later event already did; hooks can run out of order."""
    cursor = conn.execute(
        """
        UPDATE instances
        SET status = ?, last_event_ns = ?, last_activity_at = ?, attention = NULL, attention_at = NULL
        WHERE instance_id = ? AND last_event_ns < ? AND ended_at IS NULL
        """,
        (status, event_ns, event_ns // 1_000_000, instance_id, event_ns),
    )
    return cursor.rowcount > 0


def set_attention(conn: sqlite3.Connection, instance_id: str, attention: str, event_ns: int) -> None:
    """Marks a session as blocked on the user (e.g. a permission prompt) until its next activity."""
    conn.execute(
        """
        UPDATE instances SET attention = ?, attention_at = ?, last_event_ns = ?
        WHERE instance_id = ? AND last_event_ns < ? AND ended_at IS NULL
        """,
        (attention, event_ns // 1_000_000, event_ns, instance_id, event_ns),
    )


def mark_ended(conn: sqlite3.Connection, instance_id: str, at_ms: int, event_ns: int | None = None) -> None:
    conn.execute(
        """
        UPDATE instances SET status = 'ended', ended_at = ?, last_event_ns = max(last_event_ns, coalesce(?, 0))
        WHERE instance_id = ? AND ended_at IS NULL AND (? IS NULL OR last_event_ns < ?)
        """,
        (at_ms, event_ns, instance_id, event_ns, event_ns),
    )


def set_start_source(conn: sqlite3.Connection, conversation_id: str, source: str) -> None:
    conn.execute("UPDATE conversations SET start_source = ? WHERE conversation_id = ?", (source, conversation_id))


def is_seen_prompt(conn: sqlite3.Connection, conversation_id: str, prompt_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM seen_prompts WHERE conversation_id = ? AND prompt_id = ?", (conversation_id, prompt_id)
    ).fetchone()
    return row is not None


def record_prompt(
    conn: sqlite3.Connection, conversation_id: str, prompt: str, prompt_id: str | None, at_ms: int
) -> bool:
    """Counts a turn once per prompt_id; returns False for a repeated delivery of an already seen prompt."""
    if prompt_id is not None:
        seen = conn.execute(
            "INSERT OR IGNORE INTO seen_prompts (conversation_id, prompt_id, at) VALUES (?, ?, ?)",
            (conversation_id, prompt_id, at_ms),
        )
        if seen.rowcount == 0:
            return False
    conn.execute(
        """
        UPDATE conversations SET turns = turns + 1, last_prompt = ?, last_seen_at = max(last_seen_at, ?)
        WHERE conversation_id = ?
        """,
        (convert_text_to_snippet(prompt), at_ms, conversation_id),
    )
    return True


def record_reply(conn: sqlite3.Connection, conversation_id: str, reply: str, at_ms: int) -> None:
    conn.execute(
        """
        UPDATE conversations SET last_reply = ?, last_seen_at = max(last_seen_at, ?)
        WHERE conversation_id = ?
        """,
        (convert_text_to_snippet(reply), at_ms, conversation_id),
    )


def add_refs(conn: sqlite3.Connection, conversation_id: str, refs: list[str], source: str, at_ms: int) -> None:
    if not refs:
        return
    turns_row = conn.execute("SELECT turns FROM conversations WHERE conversation_id = ?", (conversation_id,)).fetchone()
    turn = int(turns_row["turns"]) if turns_row else 0
    conn.executemany(
        """
        INSERT INTO refs (conversation_id, ref, source, first_seen_at, last_seen_at, last_turn)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (conversation_id, ref) DO UPDATE SET
            last_seen_at = excluded.last_seen_at, last_turn = excluded.last_turn, hits = hits + 1
        """,
        [(conversation_id, ref, source, at_ms, at_ms, turn) for ref in refs],
    )


def add_branch_refs(
    conn: sqlite3.Connection, conversation_id: str, branch: str | None, config: Config, at_ms: int
) -> None:
    if branch:
        add_refs(conn, conversation_id, extract_branch_refs(branch, config.issue_prefixes), "branch", at_ms)


def set_focus(conn: sqlite3.Connection, conversation_id: str, focus: str | None, at_ms: int) -> None:
    conn.execute(
        "UPDATE conversations SET focus = ?, focus_at = ? WHERE conversation_id = ?",
        (focus[:FOCUS_MAX_CHARS] if focus else None, at_ms if focus else None, conversation_id),
    )


def take_focus_nudge(conn: sqlite3.Connection, conversation_id: str, config: Config, at_ms: int) -> str | None:
    """Returns a reminder to refresh focus when it is missing or old, at most once per nudge interval."""
    row = conn.execute(
        "SELECT focus, focus_at, focus_nudged_at, turns FROM conversations WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    if row is None or int(row["turns"]) < config.focus_nudge_min_turns:
        return None
    nudged_at = row["focus_nudged_at"]
    if nudged_at is not None and at_ms - int(nudged_at) < config.focus_nudge_every_s * 1000:
        return None
    focus = row["focus"]
    if focus is None:
        state = "is not set"
    elif at_ms - int(row["focus_at"]) > config.focus_stale_after_s * 1000:
        hours = (at_ms - int(row["focus_at"])) / 3_600_000
        state = f'is {hours:.1f}h old: "{focus}"'
    else:
        return None
    conn.execute("UPDATE conversations SET focus_nudged_at = ? WHERE conversation_id = ?", (at_ms, conversation_id))
    return (
        f"[bridge registry] This session's focus line {state}. If it does not describe your current task, "
        f'run: bridge focus "<what you are working on, under {FOCUS_MAX_CHARS} chars>". '
        "Other sessions and the user's dashboard read it."
    )


def find_live_instance(conn: sqlite3.Connection, pid: int, proc_start_ms: int) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM instances WHERE instance_id = ?", (make_instance_id(pid, proc_start_ms),)
    ).fetchone()
    return row


def reconcile(conn: sqlite3.Connection, config: Config, at_ms: int) -> None:
    """Brings the registry in line with live processes, Claude's session files and agterm tab names."""
    for session_file in list_session_files():
        observation = map_session_file_to_observation(session_file)
        if observation is None:
            continue
        git = read_git_info(session_file.cwd) if session_file.cwd else None
        with transaction(conn):
            instance_id = observe_instance(conn, observation, at_ms).instance_id
            apply_session_file_status(conn, instance_id, session_file, git)
    dead = find_dead_instances(conn)
    with transaction(conn):
        end_instances(conn, dead, at_ms)
    backfill_last_turns(conn, config, at_ms)
    sync_tabs(conn, config, at_ms)


def sync_tabs(conn: sqlite3.Connection, config: Config, at_ms: int) -> None:
    tabs = name_tabs_after_sessions(conn, config, read_agterm_tabs(config.agtermctl))
    with transaction(conn):
        for surface, tab in tabs.items():
            conn.execute(
                "UPDATE surfaces SET tab_name = ?, workspace_name = ?, updated_at = ? WHERE surface = ?",
                (tab.tab_name, tab.workspace_name, at_ms, surface),
            )
        _sync_tab_parents(conn, tabs)
        assign_handles(conn, at_ms)


def record_tab_parent(conn: sqlite3.Connection, session_id: str, parent_session: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO tab_parents (agterm_session, parent_session) VALUES (?, ?)",
        (session_id, parent_session),
    )


def backfill_last_turns(conn: sqlite3.Connection, config: Config, at_ms: int) -> None:
    """Sessions that ran before the hooks were installed get their last prompt and reply from the transcript."""
    rows = conn.execute(
        """
        SELECT c.conversation_id, c.transcript_path, i.repo FROM conversations c
        JOIN instances i ON i.conversation_id = c.conversation_id AND i.ended_at IS NULL
        WHERE c.last_prompt IS NULL AND c.backfilled_at IS NULL
        """
    ).fetchall()
    for row in rows:
        path = find_transcript(str(row["conversation_id"]), row["transcript_path"])
        turn = read_last_turn(path) if path else None
        with transaction(conn):
            conn.execute(
                """
                UPDATE conversations SET backfilled_at = ?,
                    last_prompt = coalesce(last_prompt, ?), last_reply = coalesce(last_reply, ?),
                    transcript_path = coalesce(transcript_path, ?)
                WHERE conversation_id = ?
                """,
                (
                    at_ms,
                    convert_text_to_snippet(turn.prompt) if turn and turn.prompt else None,
                    convert_text_to_snippet(turn.reply) if turn and turn.reply else None,
                    str(path) if path else None,
                    row["conversation_id"],
                ),
            )
            if turn:
                pr_repo = config.pr_repo_for(row["repo"])
                for text, source in ((turn.prompt, "prompt"), (turn.reply, "reply")):
                    if text:
                        refs = extract_refs(text, issue_prefixes=config.issue_prefixes, default_repo=pr_repo)
                        add_refs(conn, str(row["conversation_id"]), refs, source, at_ms)


def map_session_file_to_observation(session_file: ClaudeSessionFile) -> Observation | None:
    if not pid_alive(session_file.pid):
        return None
    proc_start = process_start_ms(session_file.pid)
    if proc_start is None:
        return None
    if (
        session_file.started_at_ms is not None
        and abs(session_file.started_at_ms - proc_start) > SESSION_FILE_START_TOLERANCE_MS
    ):
        return None
    args = process_args(session_file.pid)
    if args is not None and is_nested_agent(session_file.pid, args.env):
        args = None
    return Observation(
        pid=session_file.pid,
        proc_start_ms=proc_start,
        conversation_id=None,
        cwd=None,
        surface=map_env_to_surface(args.env) if args else None,
        kind=session_file.kind,
        peer_name=session_file.name,
        version=session_file.version,
        peer_messaging=session_file.peer_messaging,
    )


def apply_session_file_status(
    conn: sqlite3.Connection, instance_id: str, session_file: ClaudeSessionFile, git: GitInfo | None = None
) -> None:
    """Session files cover instances whose hooks never ran (started before install) and fill gaps."""
    row = conn.execute(
        "SELECT conversation_id, last_event_ns, cwd FROM instances WHERE instance_id = ?", (instance_id,)
    ).fetchone()
    if row is None or session_file.status_updated_at_ms is None:
        return
    file_event_ns = session_file.status_updated_at_ms * 1_000_000
    if file_event_ns <= int(row["last_event_ns"]):
        return
    switched = session_file.session_id != row["conversation_id"]
    cwd = session_file.cwd if switched or row["cwd"] is None else None
    observe_instance(
        conn,
        Observation(
            pid=session_file.pid,
            proc_start_ms=int(instance_id.split("@")[1]),
            conversation_id=session_file.session_id,
            cwd=cwd,
            surface=None,
            git=git if cwd else None,
        ),
        session_file.status_updated_at_ms,
        file_event_ns,
    )
    status = _FILE_STATUS.get(session_file.status or "")
    if status:
        record_activity(conn, instance_id, status, file_event_ns)


def end_dead_instances(conn: sqlite3.Connection, at_ms: int) -> None:
    end_instances(conn, find_dead_instances(conn), at_ms)


def find_dead_instances(conn: sqlite3.Connection) -> list[str]:
    """Read-only, so a threaded caller can run it before taking the write lock: each process check gives up the GIL."""
    rows = conn.execute("SELECT instance_id, pid, proc_start_ms FROM instances WHERE ended_at IS NULL").fetchall()
    dead: list[str] = []
    for row in rows:
        start = process_start_ms(int(row["pid"])) if pid_alive(int(row["pid"])) else None
        if start is None or abs(start - int(row["proc_start_ms"])) > PROC_START_TOLERANCE_MS:
            dead.append(str(row["instance_id"]))
    return dead


def end_instances(conn: sqlite3.Connection, instance_ids: list[str], at_ms: int) -> None:
    for instance_id in instance_ids:
        mark_ended(conn, instance_id, at_ms)


@dataclass(frozen=True)
class AgtermTab:
    tab_name: str
    workspace_name: str
    session_id: str = ""
    title: str = ""
    parent: str = ""


def _sync_tab_parents(conn: sqlite3.Connection, tabs: dict[str, AgtermTab]) -> None:
    # No tabs means agterm did not answer, which is the moment these links exist for.
    if not tabs:
        return
    conn.execute("DELETE FROM tab_parents")
    for tab in tabs.values():
        if tab.parent and tab.session_id:
            record_tab_parent(conn, tab.session_id, tab.parent)


def read_agterm_tabs(agtermctl: str) -> dict[str, AgtermTab]:
    try:
        completed = subprocess.run(
            [agtermctl, "tree", "--json"], capture_output=True, text=True, timeout=5, check=False
        )
        raw: object = json.loads(completed.stdout) if completed.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {}
    return map_agterm_tree_to_tabs(raw)


def name_tabs_after_sessions(
    conn: sqlite3.Connection, config: Config, tabs: dict[str, AgtermTab]
) -> dict[str, AgtermTab]:
    """Each session started in a tab renames the tab once to the session's title; later renames by hand stay."""
    rows = conn.execute(
        """
        SELECT i.surface, c.conversation_id, c.transcript_path FROM instances i
        JOIN conversations c ON c.conversation_id = i.conversation_id
        JOIN surfaces s ON s.surface = i.surface
        WHERE i.ended_at IS NULL AND s.named_for IS NOT c.conversation_id
          AND s.surface NOT IN (SELECT surface FROM leads WHERE surface IS NOT NULL)
          AND i.proc_start_ms = (
              SELECT max(n.proc_start_ms) FROM instances n WHERE n.surface = i.surface AND n.ended_at IS NULL
          )
        """
    ).fetchall()
    renamed = dict(tabs)
    for row in rows:
        surface = str(row["surface"])
        tab = renamed.get(surface)
        if tab is None or not tab.session_id or config.hides(tab.tab_name, tab.workspace_name):
            continue
        # A worker's tab is named after its task, and its session's title is only about the brief file.
        if (
            tab.parent
            and conn.execute(
                "SELECT 1 FROM surfaces s JOIN leads l ON l.surface = s.surface WHERE s.agterm_session = ?",
                (tab.parent,),
            ).fetchone()
        ):
            continue
        path = find_transcript(str(row["conversation_id"]), row["transcript_path"])
        try:
            name = read_session_title(path) if path else None
        except OSError:
            continue
        # Until Claude Code has titled the session (after the first prompt), try again on the next pass.
        if not name:
            continue
        if not is_english_name(name):
            # The session names its own tab instead; the next hook tells it to.
            with transaction(conn):
                conn.execute(
                    "UPDATE surfaces SET named_for = ?, needs_english_name = 1 WHERE surface = ?",
                    (row["conversation_id"], surface),
                )
            continue
        if name != tab.tab_name:
            if not rename_agterm_tab(config.agtermctl, tab.session_id, name):
                continue
            for pane, other in renamed.items():
                if other.session_id == tab.session_id:
                    renamed[pane] = replace(other, tab_name=name)
        with transaction(conn):
            conn.execute("UPDATE surfaces SET named_for = ? WHERE surface = ?", (row["conversation_id"], surface))
    return renamed


def is_english_name(name: str) -> bool:
    return not any(ch.isalpha() and not ch.isascii() for ch in name)


def take_english_name_nudge(conn: sqlite3.Connection, surface: str | None) -> str | None:
    if surface is None:
        return None
    updated = conn.execute(
        "UPDATE surfaces SET needs_english_name = 0 WHERE surface = ? AND needs_english_name = 1", (surface,)
    )
    if not updated.rowcount:
        return None
    return (
        "[bridge] Tab names are in English, and this session's title is not. Give this tab a short English name: "
        'agtermctl session rename "<name>" --target "$AGTERM_SESSION_ID"'
    )


def rename_agterm_tab(agtermctl: str, session_id: str, name: str) -> bool:
    try:
        completed = subprocess.run(
            [agtermctl, "session", "rename", name, "--target", session_id],
            capture_output=True, text=True, timeout=5, check=False,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def map_agterm_tree_to_tabs(raw: object) -> dict[str, AgtermTab]:
    tabs: dict[str, AgtermTab] = {}
    tree = _get(_get(raw, "result"), "tree")
    for workspace in _items(_get(tree, "workspaces")):
        workspace_name = _get(workspace, "name")
        for session in _items(_get(workspace, "sessions")):
            name = _get(session, "name")
            session_id = _get(session, "id")
            title = _get(session, "title")
            parent = _get(session, "parent")
            for surface in _items(_get(session, "surfaces")):
                pane = _get(surface, "paneID")
                if isinstance(pane, str) and isinstance(name, str):
                    tabs[pane] = AgtermTab(
                        tab_name=name,
                        workspace_name=workspace_name if isinstance(workspace_name, str) else "",
                        session_id=session_id if isinstance(session_id, str) else "",
                        title=title if isinstance(title, str) else "",
                        parent=parent if isinstance(parent, str) else "",
                    )
    return tabs


def assign_handles(conn: sqlite3.Connection, at_ms: int) -> None:
    """Keeps each live surface's automatic handle in step with its tab name; explicit handles stay."""
    live = {
        str(r["surface"])
        for r in conn.execute("SELECT DISTINCT surface FROM instances WHERE ended_at IS NULL AND surface IS NOT NULL")
    }
    rows = conn.execute("SELECT surface, tab_name, handle, handle_explicit FROM surfaces ORDER BY surface").fetchall()
    taken = {str(r["handle"]) for r in rows if r["handle"] and (r["handle_explicit"] or r["surface"] in live)}
    taken |= protected_lead_handles(conn)
    for row in rows:
        if row["handle_explicit"] or row["surface"] not in live or not row["tab_name"]:
            continue
        base = convert_tab_name_to_handle(str(row["tab_name"]))
        current = row["handle"]
        if current:
            taken.discard(str(current))
        # A numbered handle is a collision suffix only while another tab holds the plain name.
        if current == base or (current and base in taken and re.fullmatch(rf"{re.escape(base)}-\d+", str(current))):
            taken.add(str(current))
            continue
        handle = base
        suffix = 2
        while handle in taken:
            handle = f"{base}-{suffix}"
            suffix += 1
        # Only automatic handles of surfaces without a live session can be reclaimed.
        conn.execute("UPDATE surfaces SET handle = NULL WHERE handle = ? AND surface != ?", (handle, row["surface"]))
        rename_handle(conn, str(row["surface"]), current, handle, at_ms)
        taken.add(handle)
    sync_claim_handles(conn)


def rename_handle(conn: sqlite3.Connection, surface: str, old: str | None, new: str, at_ms: int) -> None:
    """Peers still name the old handle, so it stays an alias of the surface."""
    conn.execute("UPDATE surfaces SET handle = ? WHERE surface = ?", (new, surface))
    conn.execute("DELETE FROM handle_aliases WHERE handle = ?", (new,))
    if not old or old == new:
        return
    conn.execute(
        "INSERT INTO handle_aliases (handle, surface, retired_at) VALUES (?, ?, ?) "
        "ON CONFLICT (handle) DO UPDATE SET surface = excluded.surface, retired_at = excluded.retired_at",
        (old, surface, at_ms),
    )


def sync_claim_handles(conn: sqlite3.Connection) -> None:
    """A claim names its holder by the handle it had when claiming; show the holder's current one."""
    conn.execute(
        """
        UPDATE claims SET holder_handle = (
            SELECT s.handle FROM instances i JOIN surfaces s ON s.surface = i.surface
            WHERE i.conversation_id = claims.holder_conversation AND i.ended_at IS NULL AND s.handle IS NOT NULL
            ORDER BY i.proc_start_ms DESC LIMIT 1
        )
        WHERE EXISTS (
            SELECT 1 FROM instances i JOIN surfaces s ON s.surface = i.surface
            WHERE i.conversation_id = claims.holder_conversation AND i.ended_at IS NULL
              AND s.handle IS NOT NULL AND s.handle != claims.holder_handle
        )
        """
    )


class HandleTakenError(Exception):
    pass


def protected_lead_handles(conn: sqlite3.Connection) -> set[str]:
    """A lead's name and alias are its own for good, even after it retires."""
    rows = conn.execute("SELECT name FROM leads UNION SELECT alias FROM leads WHERE alias IS NOT NULL")
    return {str(r[0]) for r in rows}


def set_handle(conn: sqlite3.Connection, surface: str, handle: str, at_ms: int) -> None:
    if handle in protected_lead_handles(conn):
        raise HandleTakenError(f"handle {handle!r} belongs to a team lead")
    lead = conn.execute("SELECT display_name FROM leads WHERE surface = ?", (surface,)).fetchone()
    if lead is not None:
        raise HandleTakenError(f"this tab is the team lead {lead['display_name']}; its handle is the lead's name")
    owner = conn.execute(
        """
        SELECT s.surface, s.tab_name, s.handle_explicit,
               EXISTS (SELECT 1 FROM instances i WHERE i.surface = s.surface AND i.ended_at IS NULL) AS live
        FROM surfaces s WHERE s.handle = ? AND s.surface != ?
        """,
        (handle, surface),
    ).fetchone()
    if owner is not None and (owner["live"] or owner["handle_explicit"]):
        raise HandleTakenError(f"handle {handle!r} belongs to tab {owner['tab_name'] or owner['surface']!r}")
    current = conn.execute("SELECT handle FROM surfaces WHERE surface = ?", (surface,)).fetchone()
    conn.execute("UPDATE surfaces SET handle = NULL WHERE handle = ? AND surface != ?", (handle, surface))
    rename_handle(conn, surface, current["handle"] if current else None, handle, at_ms)
    conn.execute("UPDATE surfaces SET handle_explicit = 1 WHERE surface = ?", (surface,))
    sync_claim_handles(conn)


def list_sessions(
    conn: sqlite3.Connection, config: Config, now_ms: int, include_ended: bool = False
) -> list[SessionView]:
    rows = conn.execute(
        f"""
        SELECT i.*, s.handle, s.tab_name, s.workspace_name, s.agterm_session,
               c.focus, c.focus_at, c.last_prompt, c.last_reply, coalesce(c.turns, 0) AS turns,
               c.context_tokens
        FROM instances i
        LEFT JOIN surfaces s ON s.surface = i.surface
        LEFT JOIN conversations c ON c.conversation_id = i.conversation_id
        {"" if include_ended else "WHERE i.ended_at IS NULL"}
        ORDER BY s.workspace_name, s.tab_name, i.pid
        """
    ).fetchall()
    return [map_row_to_session_view(conn, row, config, now_ms) for row in rows]


def map_row_to_session_view(conn: sqlite3.Connection, row: sqlite3.Row, config: Config, now_ms: int) -> SessionView:
    status = str(row["status"])
    if status != "ended" and now_ms - int(row["last_observed_at"]) > config.stale_after_s * 1000:
        status = "stale"
    refs: list[str] = []
    if row["conversation_id"]:
        refs = [
            str(r["ref"])
            for r in conn.execute(
                f"""
                SELECT r.ref FROM refs r LEFT JOIN enrichment e ON e.ref = r.ref
                WHERE r.conversation_id = ?
                  AND {current_ref_sql("?")}
                ORDER BY r.source = 'branch' DESC, r.source = 'pr-link' DESC, r.last_seen_at DESC, r.hits DESC
                LIMIT 12
                """,
                (row["conversation_id"], int(row["turns"]) - RECENT_REF_TURNS),
            )
        ]
    return SessionView(
        instance_id=str(row["instance_id"]),
        handle=row["handle"],
        tab_name=row["tab_name"],
        workspace_name=row["workspace_name"],
        status=status,
        pid=int(row["pid"]),
        conversation_id=row["conversation_id"],
        peer_name=row["peer_name"],
        cwd=row["cwd"],
        repo=row["repo"],
        branch=row["branch"],
        focus=row["focus"],
        focus_at=row["focus_at"],
        last_prompt=row["last_prompt"],
        last_reply=row["last_reply"],
        turns=int(row["turns"]),
        last_activity_at=row["last_activity_at"],
        last_observed_at=int(row["last_observed_at"]),
        refs=refs,
        surface=row["surface"],
        agterm_session=row["agterm_session"],
        attention=row["attention"],
        attention_at=row["attention_at"],
        restore_command=row["restore_command"],
        context_tokens=row["context_tokens"],
    )


def convert_text_to_snippet(text: str) -> str:
    collapsed = " ".join(_PASTE_TAG_RE.sub(" ", text).split())
    return collapsed if len(collapsed) <= SNIPPET_MAX_CHARS else collapsed[: SNIPPET_MAX_CHARS - 1] + "…"


def convert_tab_name_to_handle(tab_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", tab_name.lower()).strip("-")
    return slug or "session"


def _get(obj: object, key: str) -> object:
    if isinstance(obj, dict):
        return obj.get(key)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return None


def _items(obj: object) -> list[object]:
    return list(obj) if isinstance(obj, list) else []  # pyright: ignore[reportUnknownArgumentType]


def format_age(ms: int) -> str:
    seconds = max(ms, 0) // 1000
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"
