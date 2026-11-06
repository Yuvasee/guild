import sqlite3
import time
import traceback
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from bridge.errlog import log_error

MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE surfaces (
        surface TEXT PRIMARY KEY,
        agterm_session TEXT,
        agterm_workspace TEXT,
        tab_name TEXT,
        workspace_name TEXT,
        handle TEXT UNIQUE,
        handle_explicit INTEGER NOT NULL DEFAULT 0,
        updated_at INTEGER NOT NULL
    );
    CREATE TABLE instances (
        instance_id TEXT PRIMARY KEY,
        agent TEXT NOT NULL DEFAULT 'claude',
        pid INTEGER NOT NULL,
        proc_start_ms INTEGER NOT NULL,
        conversation_id TEXT,
        surface TEXT REFERENCES surfaces(surface),
        kind TEXT,
        peer_name TEXT,
        cwd TEXT,
        repo_root TEXT,
        repo TEXT,
        branch TEXT,
        status TEXT NOT NULL DEFAULT 'idle' CHECK (status IN ('active', 'idle', 'ended')),
        first_seen_at INTEGER NOT NULL,
        last_observed_at INTEGER NOT NULL,
        last_activity_at INTEGER,
        last_event_ns INTEGER NOT NULL DEFAULT 0,
        ended_at INTEGER,
        restore_command TEXT,
        restore_pinned_at INTEGER
    );
    CREATE INDEX instances_live ON instances(ended_at, pid);
    CREATE TABLE conversations (
        conversation_id TEXT PRIMARY KEY,
        instance_id TEXT REFERENCES instances(instance_id),
        start_source TEXT,
        transcript_path TEXT,
        focus TEXT,
        focus_at INTEGER,
        focus_nudged_at INTEGER,
        last_prompt TEXT,
        last_reply TEXT,
        turns INTEGER NOT NULL DEFAULT 0,
        first_seen_at INTEGER NOT NULL,
        last_seen_at INTEGER NOT NULL
    );
    CREATE TABLE refs (
        conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
        ref TEXT NOT NULL,
        source TEXT NOT NULL,
        first_seen_at INTEGER NOT NULL,
        last_seen_at INTEGER NOT NULL,
        last_turn INTEGER NOT NULL,
        hits INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (conversation_id, ref)
    );
    CREATE TABLE hook_runs (
        id INTEGER PRIMARY KEY,
        at INTEGER NOT NULL,
        event TEXT NOT NULL,
        instance_id TEXT,
        duration_us INTEGER NOT NULL,
        outcome TEXT NOT NULL
    );
    """,
    """
    ALTER TABLE instances ADD COLUMN version TEXT;
    ALTER TABLE conversations ADD COLUMN last_prompt_id TEXT;
    """,
    """
    ALTER TABLE instances ADD COLUMN peer_messaging INTEGER;
    ALTER TABLE conversations ADD COLUMN obligations_reminded_at INTEGER;
    CREATE TABLE messages (
        id INTEGER PRIMARY KEY,
        created_at INTEGER NOT NULL,
        from_conversation TEXT NOT NULL,
        from_handle TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('info', 'request', 'reply')),
        thread_id INTEGER,
        reply_to INTEGER REFERENCES messages(id),
        body TEXT NOT NULL,
        respond_by INTEGER,
        cancelled_at INTEGER
    );
    CREATE INDEX messages_thread ON messages(thread_id);
    CREATE TABLE message_recipients (
        message_id INTEGER NOT NULL REFERENCES messages(id),
        conversation_id TEXT NOT NULL,
        handle TEXT NOT NULL,
        offered_at INTEGER,
        offer_count INTEGER NOT NULL DEFAULT 0,
        acked_at INTEGER,
        done_at INTEGER,
        declined_at INTEGER,
        PRIMARY KEY (message_id, conversation_id)
    );
    CREATE INDEX message_recipients_open ON message_recipients(conversation_id, done_at, declined_at);
    CREATE TABLE claims (
        resource TEXT PRIMARY KEY,
        alias TEXT UNIQUE,
        token TEXT NOT NULL,
        holder_conversation TEXT NOT NULL,
        holder_handle TEXT NOT NULL,
        reason TEXT,
        acquired_at INTEGER NOT NULL,
        renewed_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL
    );
    CREATE TABLE user_items (
        id INTEGER PRIMARY KEY,
        created_at INTEGER NOT NULL,
        from_conversation TEXT,
        from_handle TEXT,
        severity TEXT NOT NULL CHECK (severity IN ('info', 'action', 'blocker')),
        text TEXT NOT NULL,
        source TEXT NOT NULL CHECK (source IN ('agent', 'overdue', 'claim')),
        message_id INTEGER REFERENCES messages(id),
        resolved_at INTEGER
    );
    """,
    """
    CREATE TABLE seen_prompts (
        conversation_id TEXT NOT NULL,
        prompt_id TEXT NOT NULL,
        at INTEGER NOT NULL,
        PRIMARY KEY (conversation_id, prompt_id)
    );
    """,
    """
    ALTER TABLE instances ADD COLUMN attention TEXT;
    ALTER TABLE instances ADD COLUMN attention_at INTEGER;
    """,
    """
    ALTER TABLE conversations ADD COLUMN backfilled_at INTEGER;
    """,
    """
    CREATE TABLE enrichment (
        ref TEXT PRIMARY KEY,
        data TEXT,
        fetched_at INTEGER NOT NULL,
        error TEXT
    );
    """,
    """
    ALTER TABLE enrichment ADD COLUMN ok_at INTEGER;
    UPDATE enrichment SET ok_at = fetched_at WHERE error IS NULL;
    """,
    """
    CREATE TABLE remote_hosts (
        host TEXT PRIMARY KEY,
        ok_at INTEGER,
        error TEXT,
        error_since INTEGER
    );
    CREATE TABLE remote_sessions (
        host TEXT NOT NULL,
        key TEXT NOT NULL,
        data TEXT NOT NULL,
        observed_at INTEGER NOT NULL,
        activity_at INTEGER,
        PRIMARY KEY (host, key)
    );
    CREATE TABLE remote_refs (
        host TEXT NOT NULL,
        key TEXT NOT NULL,
        ref TEXT NOT NULL,
        PRIMARY KEY (host, key, ref)
    );
    """,
    """
    CREATE TABLE transcript_scans (
        conversation_id TEXT PRIMARY KEY,
        offset INTEGER NOT NULL
    );
    CREATE TABLE remote_drivers (
        conversation_id TEXT NOT NULL,
        host TEXT NOT NULL,
        session TEXT NOT NULL,
        last_seen_at INTEGER NOT NULL,
        PRIMARY KEY (conversation_id, host, session)
    );
    """,
    # Links were stamped with scan time; rescanning stamps them with when each command actually ran.
    """
    DELETE FROM transcript_scans;
    DELETE FROM remote_drivers;
    """,
    # The ssh/tmux parser learned more forms and dropped false ones; rescan.
    """
    DELETE FROM transcript_scans;
    DELETE FROM remote_drivers;
    """,
    """
    DELETE FROM transcript_scans;
    DELETE FROM remote_drivers;
    """,
    # First scans now read PR links from whole transcripts.
    """
    DELETE FROM transcript_scans;
    """,
    # PR links show while the PR is open; their turn must not make old merged ones look recent.
    """
    UPDATE refs SET last_turn = -1 WHERE source = 'pr-link';
    """,
    """
    UPDATE refs SET last_turn = -1000000000 WHERE source = 'pr-link' AND last_turn = -1;
    """,
    """
    ALTER TABLE conversations ADD COLUMN context_tokens INTEGER;
    ALTER TABLE conversations ADD COLUMN compact_warned_at INTEGER;
    CREATE TABLE keep_notes (
        conversation_id TEXT PRIMARY KEY,
        text TEXT NOT NULL,
        saved_at INTEGER NOT NULL
    );
    """,
    """
    ALTER TABLE conversations ADD COLUMN compact_phase TEXT;
    """,
    """
    ALTER TABLE user_items ADD COLUMN kind TEXT;
    ALTER TABLE user_items ADD COLUMN body TEXT;
    ALTER TABLE user_items ADD COLUMN links TEXT;
    ALTER TABLE user_items ADD COLUMN options TEXT;
    ALTER TABLE user_items ADD COLUMN answer TEXT;
    ALTER TABLE user_items ADD COLUMN answered_at INTEGER;
    ALTER TABLE messages ADD COLUMN board INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE messages ADD COLUMN topic TEXT;
    UPDATE user_items
    SET kind = CASE severity WHEN 'blocker' THEN 'blocked' WHEN 'action' THEN 'decision' ELSE 'insight' END;
    """,
    """
    CREATE TABLE handle_aliases (
        handle TEXT PRIMARY KEY,
        surface TEXT NOT NULL,
        retired_at INTEGER NOT NULL
    );
    """,
    """
    ALTER TABLE surfaces ADD COLUMN named_for TEXT;
    UPDATE surfaces SET named_for = (
        SELECT i.conversation_id FROM instances i WHERE i.surface = surfaces.surface AND i.ended_at IS NULL
        ORDER BY i.proc_start_ms DESC LIMIT 1
    );
    """,
    """
    CREATE TABLE leads (
        name TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        scope TEXT NOT NULL,
        linear_refs TEXT NOT NULL,
        tint TEXT NOT NULL,
        alias TEXT,
        surface TEXT UNIQUE,
        workspace_id TEXT,
        previous_handle TEXT,
        previous_handle_explicit INTEGER NOT NULL DEFAULT 0,
        previous_tab_name TEXT,
        previous_workspace TEXT,
        previous_context TEXT,
        role_seen_conversation TEXT,
        created_at INTEGER NOT NULL,
        retired_at INTEGER
    );
    """,
    """
    ALTER TABLE leads ADD COLUMN label TEXT;
    ALTER TABLE surfaces ADD COLUMN needs_english_name INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE user_items ADD COLUMN answered_by TEXT;
    ALTER TABLE message_recipients ADD COLUMN delivery_failed_at INTEGER;
    ALTER TABLE message_recipients ADD COLUMN delivery_retried_at INTEGER;
    ALTER TABLE messages ADD COLUMN post_type TEXT;
    ALTER TABLE messages ADD COLUMN thread_key TEXT;
    ALTER TABLE messages ADD COLUMN link TEXT;
    ALTER TABLE messages ADD COLUMN agent_hops INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE messages ADD COLUMN paused INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE claims ADD COLUMN reminded_at INTEGER;
    CREATE INDEX messages_thread_key ON messages (thread_key) WHERE thread_key IS NOT NULL;
    CREATE TABLE claim_log (
        id INTEGER PRIMARY KEY,
        resource TEXT NOT NULL,
        holder_conversation TEXT NOT NULL,
        holder_handle TEXT,
        reason TEXT,
        taken_at INTEGER NOT NULL,
        ended_at INTEGER,
        end_kind TEXT
    );
    CREATE INDEX claim_log_open ON claim_log (resource) WHERE ended_at IS NULL;
    INSERT INTO claim_log (resource, holder_conversation, holder_handle, reason, taken_at)
    SELECT resource, holder_conversation, holder_handle, reason, acquired_at FROM claims;
    CREATE TABLE signal_posts (
        key TEXT PRIMARY KEY,
        message_id INTEGER NOT NULL,
        posted_at INTEGER NOT NULL
    );
    """,
    """
    CREATE TABLE blocked_notices (
        agterm_session TEXT PRIMARY KEY,
        first_seen_at INTEGER NOT NULL,
        notified_at INTEGER
    );
    """,
    """
    CREATE TABLE tab_parents (
        agterm_session TEXT PRIMARY KEY,
        parent_session TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE closed_workers (
        id INTEGER PRIMARY KEY,
        conversation_id TEXT NOT NULL,
        tab_name TEXT NOT NULL,
        parent_handle TEXT NOT NULL,
        resume_command TEXT NOT NULL,
        closed_at INTEGER NOT NULL
    );
    """,
    """
    CREATE TABLE lead_work (
        lead TEXT NOT NULL REFERENCES leads(name),
        name TEXT NOT NULL COLLATE NOCASE,
        refs TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        done_at INTEGER,
        PRIMARY KEY (lead, name)
    );
    CREATE TABLE remote_runs (
        host TEXT NOT NULL,
        tmux TEXT NOT NULL,
        tmux_id TEXT NOT NULL,
        pane TEXT NOT NULL,
        label TEXT NOT NULL,
        started_at INTEGER,
        PRIMARY KEY (host, tmux_id, pane)
    );
    """,
    """
    CREATE TABLE review_prs (
        ref TEXT PRIMARY KEY,
        repo TEXT NOT NULL,
        number INTEGER NOT NULL,
        title TEXT NOT NULL,
        url TEXT NOT NULL,
        draft INTEGER NOT NULL,
        head TEXT NOT NULL,
        head_oid TEXT NOT NULL,
        base TEXT NOT NULL,
        stack_parent TEXT,
        loc INTEGER NOT NULL,
        checks TEXT,
        requested INTEGER NOT NULL DEFAULT 0,
        approved_by TEXT NOT NULL DEFAULT '[]',
        merge_state TEXT,
        in_merge_queue INTEGER NOT NULL DEFAULT 0,
        commit_oids TEXT NOT NULL DEFAULT '[]',
        updated_at TEXT NOT NULL,
        first_seen_at INTEGER NOT NULL,
        fetched_at INTEGER NOT NULL,
        closed_at INTEGER,
        comments_read_for TEXT,
        reviewed_at INTEGER,
        reviewed_by TEXT,
        reviewed_oid TEXT,
        sentence TEXT,
        sentence_by TEXT,
        sentence_at INTEGER,
        sentence_asked_at INTEGER,
        post_state TEXT,
        post_detail TEXT,
        post_link TEXT,
        post_at INTEGER,
        post_started_at INTEGER,
        reviewers_told_at INTEGER,
        doc_asked_at INTEGER,
        doc_file TEXT,
        doc_oid TEXT,
        doc_at INTEGER,
        wt_path TEXT,
        wt_oid TEXT,
        wt_state TEXT,
        wt_detail TEXT,
        wt_at INTEGER
    );
    CREATE TABLE review_heads (
        ref TEXT NOT NULL,
        oid TEXT NOT NULL,
        committed_at INTEGER NOT NULL,
        PRIMARY KEY (ref, oid)
    );
    CREATE TABLE review_comments (
        ref TEXT NOT NULL,
        comment_id TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        body TEXT NOT NULL,
        url TEXT NOT NULL,
        parent_id TEXT,
        parent_author TEXT,
        verdict TEXT,
        reason TEXT,
        told_at INTEGER,
        PRIMARY KEY (ref, comment_id)
    );
    CREATE TABLE review_agent_marks (
        id INTEGER PRIMARY KEY,
        at INTEGER NOT NULL,
        conversation_id TEXT NOT NULL,
        repo TEXT,
        number INTEGER,
        command TEXT NOT NULL
    );
    CREATE INDEX review_agent_marks_at ON review_agent_marks (at);
    CREATE TABLE review_poll (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        ok_at INTEGER,
        error TEXT,
        error_at INTEGER
    );
    """,
    """
    ALTER TABLE review_prs ADD COLUMN ready_at INTEGER;
    ALTER TABLE review_prs ADD COLUMN ready_by TEXT;
    """,
    """
    ALTER TABLE user_items ADD COLUMN urgent INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE user_items ADD COLUMN notified_at INTEGER;
    ALTER TABLE user_items ADD COLUMN answer_message_id INTEGER;
    ALTER TABLE user_items ADD COLUMN answer_dismissed_at INTEGER;
    ALTER TABLE message_recipients ADD COLUMN typing_started_at INTEGER;
    CREATE TABLE project_priority (
        position INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        issue_keys TEXT NOT NULL,
        set_at INTEGER NOT NULL,
        set_by TEXT NOT NULL
    );
    """,
    # Unused steps stay as no-ops, so every later step keeps its number.
    "SELECT 1;",
    """
    ALTER TABLE review_prs ADD COLUMN others_reviewed INTEGER;
    """,
    """
    CREATE TABLE assistant_role (
        surface TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL
    );
    """,
    "SELECT 1;",
    """
    ALTER TABLE conversations ADD COLUMN compacted_at INTEGER;
    """,
    """
    ALTER TABLE review_prs ADD COLUMN post_seen_at INTEGER;
    CREATE TABLE slack_read (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        mode TEXT,
        last_ts TEXT,
        walk_latest TEXT,
        walk_newest TEXT,
        search_after INTEGER,
        search_cursor TEXT,
        started_at INTEGER,
        finished_at INTEGER,
        ok_at INTEGER,
        error TEXT,
        error_at INTEGER
    );
    CREATE TABLE review_slack_posts (
        repo TEXT NOT NULL,
        number INTEGER NOT NULL,
        ts TEXT NOT NULL,
        link TEXT,
        seen_at INTEGER NOT NULL,
        PRIMARY KEY (repo, number)
    );
    """,
    """
    CREATE TABLE review_branch_writes (
        conversation_id TEXT NOT NULL,
        branch TEXT NOT NULL,
        at INTEGER NOT NULL,
        PRIMARY KEY (conversation_id, branch)
    );
    """,
    """
    ALTER TABLE review_comments ADD COLUMN author TEXT;
    ALTER TABLE review_comments ADD COLUMN state TEXT;
    ALTER TABLE review_comments ADD COLUMN commit_id TEXT;
    ALTER TABLE review_prs ADD COLUMN open_threads INTEGER;
    ALTER TABLE review_prs ADD COLUMN others_from INTEGER;
    UPDATE review_prs SET others_from = CAST(strftime('%s', 'now') AS INTEGER) * 1000;
    UPDATE review_prs SET comments_read_for = NULL WHERE closed_at IS NULL;
    ALTER TABLE review_comments ADD COLUMN review_id TEXT;
    ALTER TABLE review_comments ADD COLUMN quiet INTEGER;
    UPDATE review_comments SET verdict = NULL, reason = NULL, quiet = 1
    WHERE verdict = 'unclear' AND told_at IS NULL AND ref IN (SELECT ref FROM review_prs WHERE closed_at IS NULL);
    UPDATE review_prs SET wt_state = 'detached' WHERE wt_path IS NOT NULL AND closed_at IS NULL;
    ALTER TABLE review_prs ADD COLUMN doc_head TEXT;
    ALTER TABLE review_prs ADD COLUMN doc_update_for TEXT;
    ALTER TABLE review_prs ADD COLUMN doc_update_at INTEGER;
    """,
    """
    ALTER TABLE review_prs ADD COLUMN post_direct INTEGER;
    ALTER TABLE review_prs ADD COLUMN post_mentions TEXT;
    UPDATE review_prs SET sentence_asked_at = NULL WHERE sentence IS NOT NULL;
    UPDATE review_comments SET verdict = NULL, reason = NULL, quiet = 1
    WHERE verdict = 'unclear' AND told_at IS NULL AND ref IN (SELECT ref FROM review_prs WHERE closed_at IS NULL);
    ALTER TABLE review_comments ADD COLUMN owner_asked_at INTEGER;
    ALTER TABLE review_prs ADD COLUMN cleanup_told_at INTEGER;
    UPDATE review_prs SET cleanup_told_at = closed_at WHERE closed_at IS NOT NULL AND wt_path IS NULL;
    """,
    """
    ALTER TABLE review_prs ADD COLUMN draft_asked_at INTEGER;
    """,
    """
    ALTER TABLE review_prs ADD COLUMN conflict_asked_oid TEXT;
    ALTER TABLE review_prs ADD COLUMN conflict_asked_at INTEGER;
    ALTER TABLE review_poll ADD COLUMN conflicts_checked_at INTEGER;
    """,
    """
    ALTER TABLE slack_read ADD COLUMN failures INTEGER;
    """,
    """
    ALTER TABLE review_prs ADD COLUMN owner_open_threads INTEGER;
    ALTER TABLE review_prs ADD COLUMN merge_block TEXT;
    ALTER TABLE message_recipients ADD COLUMN draft_told_at INTEGER;
    """,
    """
    ALTER TABLE message_recipients ADD COLUMN stuck_at INTEGER;
    CREATE TABLE undelivered_tabs (
        conversation_id TEXT PRIMARY KEY,
        handle TEXT NOT NULL,
        surface TEXT,
        oldest_message_id INTEGER NOT NULL,
        started_at INTEGER NOT NULL,
        told_at INTEGER,
        detail TEXT
    );
    """,
    """
    ALTER TABLE review_prs ADD COLUMN owner_threads TEXT;
    ALTER TABLE review_prs ADD COLUMN threads_key TEXT;
    ALTER TABLE review_prs ADD COLUMN threads_read_for TEXT;
    """,
    """
    ALTER TABLE message_recipients ADD COLUMN typing_wanted_at INTEGER;
    UPDATE message_recipients
    SET typing_wanted_at = coalesce(typing_started_at, delivery_failed_at, stuck_at, draft_told_at)
    WHERE coalesce(typing_started_at, delivery_failed_at, stuck_at, draft_told_at) IS NOT NULL;
    """,
    """
    ALTER TABLE conversations ADD COLUMN later_context TEXT;
    """,
    """
    ALTER TABLE message_recipients ADD COLUMN unproven_at INTEGER;
    """,
    """
    ALTER TABLE review_prs ADD COLUMN bot_approved INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE review_prs ADD COLUMN owner_reviewed_at INTEGER;
    ALTER TABLE review_prs ADD COLUMN owner_reviewed_by TEXT;
    ALTER TABLE review_prs ADD COLUMN merge_path TEXT;
    ALTER TABLE review_prs ADD COLUMN merge_told_at INTEGER;
    ALTER TABLE review_prs ADD COLUMN loc_rule INTEGER;
    UPDATE review_prs SET merge_path = 'published' WHERE reviewed_at IS NOT NULL OR post_state = 'external';
    """,
    """
    ALTER TABLE review_prs ADD COLUMN doc_reviews TEXT;
    ALTER TABLE review_prs ADD COLUMN body_reviews TEXT;
    """,
    """
    ALTER TABLE message_recipients ADD COLUMN typing_pid INTEGER;
    ALTER TABLE message_recipients ADD COLUMN typing_pid_start_ms INTEGER;
    """,
    """
    CREATE TABLE asks (
        id INTEGER PRIMARY KEY,
        owner TEXT NOT NULL,
        recorded_by TEXT NOT NULL,
        words TEXT NOT NULL,
        source TEXT NOT NULL,
        refs TEXT NOT NULL DEFAULT '[]',
        state TEXT NOT NULL CHECK (state IN ('open', 'in_progress', 'done_claimed', 'verified', 'closed')),
        claim TEXT,
        claim_by TEXT,
        claim_conversation TEXT,
        claim_role TEXT,
        evidence TEXT,
        evidence_how TEXT,
        evidence_by TEXT,
        closed_how TEXT CHECK (closed_how IN ('auto', 'owner', 'assistant')),
        reopen_why TEXT,
        created_at INTEGER NOT NULL,
        started_at INTEGER,
        done_at INTEGER,
        verified_at INTEGER,
        closed_at INTEGER,
        reopened_at INTEGER,
        updated_at INTEGER NOT NULL,
        last_nag_at INTEGER
    );
    CREATE INDEX asks_live ON asks (state, owner) WHERE state != 'closed';
    CREATE TABLE ask_events (
        id INTEGER PRIMARY KEY,
        ask_id INTEGER NOT NULL REFERENCES asks(id),
        at INTEGER NOT NULL,
        by TEXT NOT NULL,
        state TEXT,
        note TEXT
    );
    CREATE INDEX ask_events_ask ON ask_events (ask_id, at);
    """,
    """
    ALTER TABLE review_prs ADD COLUMN post_comment TEXT;
    """,
    """
    ALTER TABLE review_prs ADD COLUMN depends_on TEXT;
    """,
    """
    ALTER TABLE asks ADD COLUMN wait_on TEXT CHECK (wait_on IN ('owner', 'approval', 'release', 'verifier'));
    ALTER TABLE asks ADD COLUMN wait_why TEXT;
    ALTER TABLE asks ADD COLUMN wait_by TEXT;
    ALTER TABLE asks ADD COLUMN wait_at INTEGER;
    """,
    """
    ALTER TABLE asks ADD COLUMN wait_until INTEGER;
    """,
)

HOOK_BUSY_TIMEOUT_MS = 200
HOOK_RETRY_BUSY_TIMEOUT_MS = 1000
CLI_BUSY_TIMEOUT_MS = 5000
SLOW_HOLD_MS = 1000


def connect(path: Path, busy_timeout_ms: int = CLI_BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=busy_timeout_ms / 1000, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version >= len(MIGRATIONS):
        return
    conn.execute("PRAGMA journal_mode = WAL")
    with transaction(conn):
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        for script in MIGRATIONS[version:]:
            for statement in _split_statements(script):
                conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {len(MIGRATIONS)}")


@contextmanager
def transaction(conn: sqlite3.Connection, retry_busy_timeout_ms: int = 0) -> Generator[sqlite3.Connection]:
    """A write transaction; with retry_busy_timeout_ms, a lock still held after the connection's own wait gets one
    more wait that long. A lock held past SLOW_HOLD_MS is logged with its caller, as every other writer waits on it."""
    asked_at = time.monotonic()
    retried = _begin_immediate(conn, retry_busy_timeout_ms)
    held_from = time.monotonic()
    ending_at = held_from
    try:
        yield conn
    except BaseException:
        ending_at = time.monotonic()
        conn.execute("ROLLBACK")
        raise
    else:
        ending_at = time.monotonic()
        conn.execute("COMMIT")
    finally:
        held_ms = (ending_at - held_from) * 1000
        # COMMIT also runs any WAL auto-checkpoint, after the lock is let go; it is shown apart from the hold.
        ending_ms = (time.monotonic() - ending_at) * 1000
        if retried or held_ms >= SLOW_HOLD_MS or ending_ms >= SLOW_HOLD_MS:
            waited_ms = (held_from - asked_at) * 1000
            log_error(
                "db lock" + (" retried" if retried else ""),
                f"waited {waited_ms:.0f} ms, held {held_ms:.0f} ms, commit {ending_ms:.0f} ms, at {_find_caller()}",
            )


def _begin_immediate(conn: sqlite3.Connection, retry_busy_timeout_ms: int) -> bool:
    # IMMEDIATE takes the write lock up front so concurrent hooks queue on busy_timeout instead of
    # failing mid-transaction with SQLITE_BUSY on lock upgrade.
    try:
        conn.execute("BEGIN IMMEDIATE")
        return False
    except sqlite3.OperationalError as exc:
        if not retry_busy_timeout_ms or "locked" not in str(exc):
            raise
    own = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
    conn.execute(f"PRAGMA busy_timeout = {int(retry_busy_timeout_ms)}")
    try:
        conn.execute("BEGIN IMMEDIATE")
    finally:
        conn.execute(f"PRAGMA busy_timeout = {own}")
    return True


def _find_caller() -> str:
    for frame in reversed(traceback.extract_stack()[:-2]):
        if not frame.filename.endswith(("/db.py", "/contextlib.py")):
            return f"{frame.filename.rsplit('/', 1)[-1]}:{frame.lineno} {frame.name}"
    return "unknown"


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def _split_statements(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]
