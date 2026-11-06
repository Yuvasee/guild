import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import cast

from bridge.activity import SessionActivity, read_session_activity
from bridge.asks import (
    AskError,
    close_ask,
    close_settled_asks,
    map_db_to_asks_page,
    reopen_ask,
    send_ask_nags,
    tell_owner_of_reopen,
)
from bridge.attention import load_projects, map_page_to_ranked_page
from bridge.board import (
    USER_CONVERSATION,
    AddressError,
    MessageStateError,
    Recipient,
    SendResult,
    flag_overdue,
    write_hint,
)
from bridge.claims import list_claims
from bridge.config import Config, db_path
from bridge.db import connect, now_ms, transaction
from bridge.drivers import Link, load_drivers, scan_transcripts
from bridge.enrich import collect_watched_refs, enrich_due, load_enrichment, provider_freshness
from bridge.feed import (
    AGTERM_SILENT,
    SCREEN_UNREAD,
    TAB_NOT_FOUND,
    UndeliveredTab,
    answer_post,
    dismiss_undelivered_answer,
    post_from_user_to_room,
    prune_typer_locks,
    release_dead_typing_claims,
    send_from_user,
    spawn_queued_retries,
    spawn_typed_delivery,
    take_delivery_retries,
    take_new_undelivered_tabs,
    tell_of_undelivered_tab,
)
from bridge.guard import sync_claims_flag
from bridge.guildview import map_db_to_guild, post_signals_to_leads
from bridge.health import check_hooks
from bridge.notify import release_urgent_notice, show_notification, take_next_urgent_notice
from bridge.refs import map_ref_to_label
from bridge.registry import (
    RECENT_REF_TURNS,
    SessionView,
    current_ref_sql,
    end_instances,
    find_dead_instances,
    list_sessions,
    reconcile,
)
from bridge.remote import attach_command, collect, list_remote_sessions
from bridge.remote import store as store_remote
from bridge.review_post import (
    ReviewError,
    advance_posts,
    ask_for_doc_updates,
    ask_for_docs,
    ask_to_clean_up,
    ask_to_mark_drafts_ready,
    ask_to_resolve_conflicts,
    map_typed_comment_to_slack,
    record_post_choice,
    record_publish,
    reopen_unsent_post,
    retry_failed_post,
    spawn_review_post,
)
from bridge.review_slack import TOKEN_EVERY_S, apply_seen_posts, map_db_to_slack_freshness, read_slack_once
from bridge.review_worktree import (
    fill_doc_heads,
    fill_doc_reviews,
    link_worktrees_to_prs,
    map_doc_to_served,
    pick_worktree_job,
    run_worktree_job,
)
from bridge.reviews import GITHUB_EVERY_S, map_db_to_reviews, poll_review_queue
from bridge.signals import SessionRefs, find_signals, map_enrichment_to_status, map_signal_to_dict
from bridge.status_watchers import read_watchers
from bridge.tabinput import map_tree_to_pane_session, run_agterm
from bridge.wake import WakeWatch
from bridge.watcher import map_tree_to_blocked_tabs, notify_blocked_tabs, read_screen_tail, read_tree

DEFAULT_PORT = 8780
RECONCILE_EVERY_S = 10
MAINTAIN_EVERY_S = 30
ASKS_EVERY_S = 60
ASK_WHY_MAX_CHARS = 2000
ENRICH_EVERY_S = 60
REMOTE_EVERY_S = 60
SCAN_EVERY_S = 30
SIGNALS_EVERY_S = 60
WATCH_EVERY_S = 30
TREE_EVERY_S = 10
WORKTREE_EVERY_S = 30
ACTIVITY_EVERY_S = 10
NOTIFY_EVERY_S = 10
RESOLVED_SHOWN_FOR_MS = 7 * 24 * 3_600_000
DRIVER_WINDOW_MS = 12 * 3_600_000
MAX_POST_BYTES = 16_384
# Every SQLite call gives up the GIL, and taking it back waits out a busy sibling thread's slice: at the default 5 ms
# serve's transactions held the write lock about 9x longer, and every hook and CLI waited on them.
GIL_SWITCH_INTERVAL_S = 0.0005


def serve(config: Config, port: int) -> None:
    sys.setswitchinterval(GIL_SWITCH_INTERVAL_S)
    state = DashboardState(config)
    handler = type("BridgeHandler", (DashboardHandler,), {"state": state})
    threading.Thread(target=state.maintain_forever, daemon=True).start()
    threading.Thread(target=state.enrich_forever, daemon=True).start()
    threading.Thread(target=state.post_signals_forever, daemon=True).start()
    threading.Thread(target=state.watch_blocked_tabs_forever, daemon=True).start()
    threading.Thread(target=state.read_tree_forever, daemon=True).start()
    threading.Thread(target=state.read_activity_forever, daemon=True).start()
    threading.Thread(target=state.scan_transcripts_forever, daemon=True).start()
    threading.Thread(target=state.notify_urgent_forever, daemon=True).start()
    threading.Thread(target=state.nag_asks_forever, daemon=True).start()
    if config.review_org and config.review_author:
        threading.Thread(target=state.poll_reviews_forever, daemon=True).start()
        if config.chat_adapter == "slack" and config.review_channel_id:
            threading.Thread(target=state.read_slack_forever, daemon=True).start()
        if config.review_worktrees_dir:
            threading.Thread(target=state.prepare_worktrees_forever, daemon=True).start()
    if config.remotes:
        threading.Thread(target=state.collect_remotes_forever, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"bridge dashboard on http://127.0.0.1:{port}/", flush=True)
    server.serve_forever()


class DashboardState:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._reconciled_at = 0.0
        self._tree: tuple[dict[str, object] | None, int | None] = (None, None)
        self._scanned_from: int | None = None
        self._activity: tuple[dict[int, SessionActivity] | None, int | None] = (None, None)
        self._wake = WakeWatch()

    def snapshot(self) -> dict[str, object]:
        with self._connection() as conn:
            self._maybe_reconcile(conn)
            return map_db_to_snapshot(conn, self.config, now_ms())

    def guild_snapshot(self) -> dict[str, object]:
        # No reconcile here: it asks agterm, and maintain_forever keeps the registry current anyway.
        with self._connection() as conn:
            tree, tree_at = self._tree
            activity, activity_at = self._activity
            guild = map_db_to_guild(
                conn,
                self.config,
                now_ms(),
                lambda ref: map_ref_to_url(ref, self.config) or map_enrichment_to_url(conn, ref),
                tree,
                tree_at,
                activity,
                activity_at,
            )
            reviews = {
                **map_db_to_reviews(conn, self.config, now_ms()),
                "slack": map_db_to_slack_freshness(conn, self.config),
            }
            guild["watchers"] = read_watchers(self.config.watchers, now_ms())
            asks = map_db_to_asks_page(conn, self.config, now_ms())
            by_owner = cast(dict[str, object], asks.pop("by_owner"))
            for lead in cast(list[dict[str, object]], guild["leads"]):
                lead["asks"] = by_owner.get(str(lead["name"]))
            guild["asks"] = asks
            return map_page_to_ranked_page(guild, reviews, load_projects(conn))

    def read_tree_forever(self) -> None:
        """A page poll must not wait on agterm; when it stops answering the last tree stays, with its age."""
        while True:
            try:
                tree = read_tree(self.config)
                self._wake.note_agterm(tree is not None)
                if tree is not None:
                    self._tree = (tree, now_ms())
            except OSError as exc:
                print(f"agterm tree failed: {exc}", flush=True)
            time.sleep(TREE_EVERY_S)

    def read_activity_forever(self) -> None:
        """The page keeps the last reading with its age, and past that says unknown instead of trusting it."""
        while True:
            try:
                with self._connection() as conn:
                    activity = read_session_activity(conn, now_ms())
                if activity is not None:
                    self._activity = (activity, now_ms())
            except (sqlite3.Error, OSError) as exc:
                print(f"session activity failed: {exc}", flush=True)
            time.sleep(ACTIVITY_EVERY_S)

    def post_to_room(self, text: str, reply_to: int | None) -> tuple[bool, str]:
        try:
            with self._connection() as conn, transaction(conn):
                result, typed = post_from_user_to_room(
                    conn, self.config, self.config.user_alias or "user", text, now_ms(), reply_to
                )
        except (MessageStateError, AddressError) as exc:
            return False, str(exc)
        self._deliver(result, typed)
        return True, ", ".join(r.handle for r in result.recipients) or "the room"

    def post_signals_forever(self) -> None:
        """With every page closed too: a routine PR state change reaches the lead that owns it, once."""
        while True:
            try:
                with self._connection() as conn:
                    now = now_ms()
                    signals = cast(list[dict[str, object]], map_db_to_snapshot(conn, self.config, now)["signals"])
                    with transaction(conn):
                        posted = post_signals_to_leads(conn, self.config, signals, now)
                    nudge = [r for r in posted if r.pid]
                for recipient in nudge:
                    write_hint(recipient.pid, 0)
            except (sqlite3.Error, OSError) as exc:
                print(f"signal posts failed: {exc}", flush=True)
            time.sleep(SIGNALS_EVERY_S)

    def watch_blocked_tabs_forever(self) -> None:
        """A worker stuck on a dialog tells its owner, with nobody needing to look at it."""
        while True:
            try:
                tree = read_tree(self.config)
                if tree is not None:
                    blocked = map_tree_to_blocked_tabs(tree, self.config)
                    screens = {tab.session_id: read_screen_tail(self.config, tab.session_id) for tab in blocked}
                    with self._connection() as conn, transaction(conn):
                        sent = notify_blocked_tabs(conn, self.config, blocked, screens, now_ms())
                    for message_id, recipient in sent:
                        if recipient.pid:
                            write_hint(recipient.pid, 0)
                        spawn_typed_delivery(message_id, [recipient])
            except (sqlite3.Error, OSError, TypeError, ValueError, KeyError) as exc:
                print(f"blocked tab watch failed: {exc}", flush=True)
            time.sleep(WATCH_EVERY_S)

    def notify_urgent_forever(self) -> None:
        """Wherever they are looking: no typing, and no tab is selected or marked."""
        while True:
            try:
                with self._connection() as conn:
                    failed: frozenset[int] = frozenset()
                    while True:
                        with transaction(conn):
                            notice = take_next_urgent_notice(conn, self.config, now_ms(), failed)
                        if notice is None:
                            break
                        if not show_notification(self.config, notice.text):
                            failed |= {notice.item_id}
                            with transaction(conn):
                                release_urgent_notice(conn, notice.item_id)
            except sqlite3.Error as exc:
                print(f"urgent notification failed: {exc}", flush=True)
            time.sleep(NOTIFY_EVERY_S)

    def nag_asks_forever(self) -> None:
        """With every page closed too: an ask nobody moved, or claimed and never checked, reaches its owner."""
        while True:
            try:
                self.nag_asks_once(now_ms())
            except sqlite3.Error as exc:
                print(f"asks nag failed: {exc}", flush=True)
            time.sleep(ASKS_EVERY_S)

    def nag_asks_once(self, now: int) -> None:
        with self._connection() as conn, transaction(conn):
            close_settled_asks(conn, now, self.config.ask_autoclose_hours * 3_600_000)
            sent = send_ask_nags(conn, self.config, now)
        for message_id, recipient in sent:
            if recipient.pid:
                write_hint(recipient.pid, 0)
            spawn_typed_delivery(message_id, [recipient])

    def close_ask_by_owner(self, ask_id: int) -> tuple[bool, str]:
        try:
            with self._connection() as conn, transaction(conn):
                close_ask(conn, ask_id, "owner", self.config.user_alias or "user", now_ms())
        except AskError as exc:
            return False, str(exc)
        return True, "closed"

    def reopen_ask_by_owner(self, ask_id: int, why: str) -> tuple[bool, str]:
        try:
            with self._connection() as conn, transaction(conn):
                reason = f"on the page: {why.strip()}" if why.strip() else "on the page, no reason given"
                ask = reopen_ask(conn, ask_id, self.config.user_alias or "user", reason, now_ms())
                told = tell_owner_of_reopen(conn, self.config, ask, now_ms())
        except AskError as exc:
            return False, str(exc)
        for message_id, recipient in told:
            if recipient.pid:
                write_hint(recipient.pid, 0)
            spawn_typed_delivery(message_id, [recipient])
        return True, "open again"

    def maintain_forever(self) -> None:
        """Escalates overdue requests even when no session is active and nobody has the page open."""
        while True:
            try:
                with self._connection() as conn:
                    # Locks and tab names must stay current with every page closed: hooks trust them.
                    self._maybe_reconcile(conn)
                    dead = find_dead_instances(conn)
                    with transaction(conn):
                        end_instances(conn, dead, now_ms())
                        flag_overdue(conn, now_ms())
                        release_dead_typing_claims(conn, now_ms())
                        retries = take_delivery_retries(conn, now_ms())
                        undelivered = take_new_undelivered_tabs(conn, self.config, now_ms(), self._wake.counted_from())
                    sync_claims_flag(conn)
                    for tab in undelivered:
                        screen, unread_reason = self._read_undelivered_tab(tab)
                        # A sleep or agterm going silent or coming back during the read moves the count on.
                        recounted = replace(tab, counted_from=self._wake.counted_from())
                        with transaction(conn):
                            told = tell_of_undelivered_tab(
                                conn, self.config, recounted, screen, now_ms(), unread_reason
                            )
                        if told is not None:
                            retries.append(told)
                for message_id, recipient in retries:
                    spawn_typed_delivery(message_id, [recipient])
                spawn_queued_retries()
                prune_typer_locks()
            except sqlite3.Error as exc:
                print(f"maintenance failed: {exc}", flush=True)
            time.sleep(MAINTAIN_EVERY_S)

    def _read_undelivered_tab(self, tab: UndeliveredTab) -> tuple[str | None, str]:
        """The tab's screen, or why there is none; a tab not found by its id is looked up by its pane."""
        pane = tab.surface or ""

        def read(session: str) -> str | None:
            return run_agterm(self.config, ["session", "text", "--target", session, "--pane-id", pane, "--lines", "40"])

        if tab.agterm_session and (screen := read(tab.agterm_session)) is not None:
            self._wake.note_agterm(True)
            return screen, TAB_NOT_FOUND
        output = run_agterm(self.config, ["tree", "--json"])
        self._wake.note_agterm(output is not None)
        if output is None:
            return None, AGTERM_SILENT
        found = map_tree_to_pane_session(output, pane)
        if found is None:
            return None, TAB_NOT_FOUND
        return (read(found) if found != tab.agterm_session else None), SCREEN_UNREAD

    def enrich_forever(self) -> None:
        """GitHub and the tracker are polled here, never from hooks."""
        while True:
            try:
                with self._connection() as conn:
                    enrich_due(conn, self.config, now_ms())
            except (sqlite3.Error, OSError, ValueError) as exc:
                print(f"enrichment failed: {exc}", flush=True)
            time.sleep(ENRICH_EVERY_S)

    def collect_remotes_forever(self) -> None:
        """One collection per host at a time; ssh runs outside any database transaction."""
        while True:
            for remote in self.config.remotes:
                result = collect(remote)
                try:
                    with self._connection() as conn:
                        store_remote(conn, remote, result, self.config, now_ms())
                except sqlite3.Error as exc:
                    print(f"remote {remote.name} store failed: {exc}", flush=True)
            time.sleep(REMOTE_EVERY_S)

    def poll_reviews_forever(self) -> None:
        """With every page closed too: the queue stays current and their comments reach their owners."""
        while True:
            try:
                with self._connection() as conn:
                    sent = poll_review_queue(conn, self.config, now_ms(), self._tree[0], self._scanned_from)
                    self._advance_reviews(conn, sent)
            except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, MessageStateError) as exc:
                print(f"review queue poll failed: {exc}", flush=True)
            time.sleep(GITHUB_EVERY_S)

    def read_slack_forever(self) -> None:
        team_url: str | None = None
        while True:
            try:
                with self._connection() as conn:
                    team_url = read_slack_once(conn, self.config, team_url, now_ms())
            except (sqlite3.Error, OSError, ValueError) as exc:
                print(f"review channel read failed: {type(exc).__name__}", flush=True)
            time.sleep(TOKEN_EVERY_S)

    def _advance_reviews(self, conn: sqlite3.Connection, sent: list[tuple[int, Recipient]]) -> None:
        claim = now_ms()
        with transaction(conn):
            apply_seen_posts(conn, claim)
            to_send, told = advance_posts(conn, self.config, claim)
            fill_doc_heads(conn)
            fill_doc_reviews(conn)
            told = [*told, *ask_for_docs(conn, self.config, now_ms())]
            told = [*told, *ask_for_doc_updates(conn, self.config, now_ms())]
            told = [*told, *ask_to_clean_up(conn, self.config, now_ms())]
            told = [*told, *ask_to_mark_drafts_ready(conn, self.config, now_ms())]
            told = [*told, *ask_to_resolve_conflicts(conn, self.config, now_ms())]
        for ref in to_send:
            spawn_review_post(ref, claim)
        for message_id, recipient in [*sent, *told]:
            if recipient.pid:
                write_hint(recipient.pid, 0)
            spawn_typed_delivery(message_id, [recipient])

    def prepare_worktrees_forever(self) -> None:
        """One at a time, in this one thread, so a batch of large PRs cannot load the laptop."""
        while True:
            try:
                with self._connection() as conn:
                    link_worktrees_to_prs(conn)
                    job = pick_worktree_job(conn, self.config, now_ms())
                    if job is not None:
                        run_worktree_job(conn, self.config, job, now_ms())
            except (sqlite3.Error, OSError, ValueError) as exc:
                print(f"review worktree failed: {exc}", flush=True)
            time.sleep(WORKTREE_EVERY_S)

    def read_review_doc(self, repo: str, number: int) -> bytes | None:
        with self._connection() as conn:
            return map_doc_to_served(conn, repo, number)

    def publish_review(
        self, ref: str, action: str, direct: bool = False, mention: tuple[str, ...] = (), comment: str = ""
    ) -> tuple[bool, str]:
        people = dict(self.config.review_mention_people)
        if unknown := [name for name in mention if name not in people]:
            return False, f"not in review_mention_people: {', '.join(unknown)}"
        try:
            slack = self.config.chat_adapter not in ("file", "webhook")
            post_comment = map_typed_comment_to_slack(comment, people, slack) if mention else ""
            with self._connection() as conn:
                with transaction(conn):
                    if action == "retry":
                        retry_failed_post(conn, ref)
                    elif action == "repost":
                        reopen_unsent_post(conn, ref)
                        record_post_choice(conn, ref, direct, tuple(people[name] for name in mention), post_comment)
                    else:
                        by = self.config.user_alias or "user"
                        ids = tuple(people[name] for name in mention)
                        record_publish(conn, ref, by, now_ms(), direct, ids, post_comment)
                self._advance_reviews(conn, [])
        except ReviewError as exc:
            return False, str(exc)
        return True, "ok"

    def scan_transcripts_forever(self) -> None:
        while True:
            try:
                with self._connection() as conn:
                    started = now_ms()
                    scan_transcripts(conn, self.config.remotes, started)
                    self._scanned_from = started
            except (sqlite3.Error, OSError) as exc:
                print(f"transcript scan failed: {exc}", flush=True)
            time.sleep(SCAN_EVERY_S)

    def attach_remote(self, host: str, tmux_id: str) -> tuple[bool, str]:
        remote = next((r for r in self.config.remotes if r.name == host), None)
        if remote is None:
            return False, "unknown remote"
        with self._connection() as conn:
            known = conn.execute(
                "SELECT 1 FROM remote_sessions WHERE host = ? AND json_extract(data, '$.tmux_id') = ?", (host, tmux_id)
            ).fetchone()
        if known is None:
            return False, "unknown remote session"
        try:
            line = attach_command(remote, tmux_id)
        except ValueError as exc:
            return False, str(exc)
        command = [self.config.agtermctl, "session", "new", "--name", f"{host} {tmux_id}", "--command", line]
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
            if self.config.terminal_app:
                subprocess.run(["open", "-a", self.config.terminal_app], capture_output=True, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)
        return done.returncode == 0, (done.stdout + done.stderr).strip() or "ok"

    def set_item_resolved(self, item_id: int, resolved: bool) -> bool:
        with self._connection() as conn, transaction(conn):
            if resolved and dismiss_undelivered_answer(conn, item_id, now_ms()):
                return True
            cursor = conn.execute(
                "UPDATE user_items SET resolved_at = ? WHERE id = ? AND (resolved_at IS NULL) = ?",
                (now_ms() if resolved else None, item_id, resolved),
            )
            return cursor.rowcount > 0

    def answer_item(self, item_id: int, answer: str) -> tuple[bool, str]:
        try:
            with self._connection() as conn, transaction(conn):
                result = answer_post(conn, item_id, answer, self.config.user_alias or "user", now_ms())
        except MessageStateError as exc:
            return False, str(exc)
        self._deliver(result, result.recipients)
        return True, "sent"

    def send_from_user(self, to: list[str], text: str, reply_to: int | None) -> tuple[bool, str]:
        try:
            with self._connection() as conn, transaction(conn):
                result, typed = send_from_user(
                    conn, self.config, self.config.user_alias or "user", to, text, now_ms(), reply_to
                )
        except (MessageStateError, AddressError) as exc:
            return False, str(exc)
        self._deliver(result, typed)
        if not to and reply_to is None:
            return True, f"team:{len(result.recipients)}:{len(typed)}"
        return True, ", ".join(r.handle for r in result.recipients) or "nobody"

    def mark_read(self, message_id: int) -> bool:
        with self._connection() as conn, transaction(conn):
            cursor = conn.execute(
                """
                UPDATE message_recipients SET acked_at = ?
                WHERE message_id = ? AND conversation_id = ? AND acked_at IS NULL
                """,
                (now_ms(), message_id, USER_CONVERSATION),
            )
            return cursor.rowcount > 0

    def _deliver(self, result: SendResult, typed: list[Recipient]) -> None:
        """Every recipient picks it up at its next tool call; the typed ones also get it typed into an idle tab."""
        for recipient in result.recipients:
            if recipient.pid:
                try:
                    write_hint(recipient.pid, 0)
                except OSError as exc:
                    print(f"hint for {recipient.handle} failed: {exc}", flush=True)
        try:
            spawn_typed_delivery(result.message_id, typed)
        except OSError as exc:
            print(f"typed delivery failed to start: {exc}", flush=True)

    def jump(self, surface: str) -> tuple[bool, str]:
        with self._connection() as conn:
            row = conn.execute("SELECT agterm_session FROM surfaces WHERE surface = ?", (surface,)).fetchone()
        if row is None or row["agterm_session"] is None:
            return False, "unknown session"
        select = [self.config.agtermctl, "session", "select", "--target", str(row["agterm_session"])]
        try:
            done = subprocess.run(select, capture_output=True, text=True, timeout=5, check=False)
            # From a browser tab the terminal is behind; inside an agterm overlay this is a no-op.
            if self.config.terminal_app:
                subprocess.run(["open", "-a", self.config.terminal_app], capture_output=True, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)
        if done.returncode != 0:
            return False, (done.stdout + done.stderr).strip()
        return True, "ok"

    def _connection(self) -> "_Closing":
        return _Closing(connect(db_path()))

    def _maybe_reconcile(self, conn: sqlite3.Connection) -> None:
        with self._lock:
            if time.monotonic() - self._reconciled_at < RECONCILE_EVERY_S:
                return
            self._reconciled_at = time.monotonic()
        reconcile(conn, self.config, now_ms())


class _Closing:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        return self.conn

    def __exit__(self, *_: object) -> None:
        self.conn.close()


def map_db_to_snapshot(conn: sqlite3.Connection, config: Config, now: int) -> dict[str, object]:
    open_counts = {
        str(r["conversation_id"]): {"unread": int(r["unread"]), "open_requests": int(r["obligations"])}
        for r in conn.execute(
            """
            SELECT r.conversation_id,
                   sum(r.acked_at IS NULL) AS unread,
                   sum(r.acked_at IS NOT NULL AND m.kind = 'request') AS obligations
            FROM message_recipients r JOIN messages m ON m.id = r.message_id
            WHERE r.done_at IS NULL AND r.declined_at IS NULL AND m.cancelled_at IS NULL
            GROUP BY r.conversation_id
            """
        )
    }
    claim_rows = list_claims(conn)
    claims_by_conversation: dict[str, list[str]] = {}
    for row in claim_rows:
        claims_by_conversation.setdefault(str(row["holder_conversation"]), []).append(str(row["resource"]))
    enrichment = load_enrichment(conn, now)
    sessions: list[dict[str, object]] = []
    # Rows name the conversation and handle a session had when it wrote them. Show the tab that now holds that
    # conversation (also after /clear or a resume in another tab), by its current handle.
    surface_by_conversation = map_conversations_to_surfaces(conn)
    handle_by_surface: dict[str, str] = {}
    # By the tab that last held a conversation, so a locked session's rows stay hidden after /clear or exit.
    hidden_conversations = {
        str(r["conversation_id"])
        for r in conn.execute(
            """
            SELECT c.conversation_id, s.tab_name, s.workspace_name FROM conversations c
            JOIN instances i ON i.instance_id = c.instance_id JOIN surfaces s ON s.surface = i.surface
            """
        )
        if config.hides(r["tab_name"], r["workspace_name"])
    }
    views: list[SessionView] = []
    for view in list_sessions(conn, config, now):
        if config.hides(view.tab_name, view.workspace_name):
            hidden_conversations.add(view.conversation_id or "")
        else:
            views.append(view)
    for view in views:
        item = asdict(view)
        item.update(open_counts.get(view.conversation_id or "", {"unread": 0, "open_requests": 0}))
        item["claims"] = claims_by_conversation.get(view.conversation_id or "", [])
        item["links"] = map_refs_to_links(view.repo, view.branch, view.refs, config, enrichment)
        sessions.append(item)
        if view.surface and view.handle:
            handle_by_surface[view.surface] = view.handle
    handle_by_conversation = {
        conversation: handle_by_surface[surface]
        for conversation, surface in surface_by_conversation.items()
        if surface in handle_by_surface
    }
    items = [
        {
            **dict(r),
            "links": _json_list(r["links"]),
            "options": _json_list(r["options"]),
            "surface": surface_by_conversation.get(str(r["from_conversation"])),
            "from_handle": handle_by_conversation.get(str(r["from_conversation"]), r["from_handle"]),
        }
        for r in conn.execute(
            """
            SELECT id, created_at, from_conversation, from_handle, severity, text, source, message_id, resolved_at,
                   kind, body, links, options, answer, answered_at
            FROM user_items
            WHERE created_at > ? OR resolved_at > ? OR (resolved_at IS NULL AND severity != 'info')
            ORDER BY severity = 'blocker' DESC, severity = 'action' DESC, created_at DESC
            LIMIT 300
            """,
            (now - RESOLVED_SHOWN_FOR_MS, now - RESOLVED_SHOWN_FOR_MS),
        )
        if str(r["from_conversation"]) not in hidden_conversations
    ]
    messages = [
        {
            **dict(r),
            "from_handle": handle_by_conversation.get(str(r["from_conversation"]), r["from_handle"]),
            "surface": surface_by_conversation.get(str(r["from_conversation"])),
            "recipients": convert_recipients_to_current_handles(r["recipients"], handle_by_conversation),
        }
        for r in conn.execute(
            """
            SELECT m.id, m.created_at, m.from_conversation, m.from_handle, m.kind, m.thread_id, m.reply_to, m.body,
                   m.respond_by, m.cancelled_at, m.board, m.topic, m.from_conversation = 'user' AS from_user,
                   max(r.conversation_id = 'user' AND r.acked_at IS NULL) AS unread_for_user,
                   group_concat(r.conversation_id || char(9) || r.handle || ':' || CASE
                       WHEN r.done_at IS NOT NULL THEN 'done'
                       WHEN r.declined_at IS NOT NULL THEN 'declined'
                       WHEN r.acked_at IS NOT NULL THEN 'acked'
                       WHEN r.offered_at IS NOT NULL THEN 'delivered'
                       ELSE 'queued' END, char(10)) AS recipients
            FROM messages m LEFT JOIN message_recipients r ON r.message_id = m.id
            WHERE m.thread_id IN (SELECT thread_id FROM messages GROUP BY thread_id ORDER BY max(id) DESC LIMIT 30)
            GROUP BY m.id
            ORDER BY (SELECT max(id) FROM messages t WHERE t.thread_id = m.thread_id) DESC, m.id
            """
        )
        if str(r["from_conversation"]) not in hidden_conversations
    ]
    claims = [
        {
            "resource": r["resource"],
            "alias": r["alias"],
            "holder_handle": r["holder_handle"],
            "reason": r["reason"],
            "acquired_at": r["acquired_at"],
            "expires_at": r["expires_at"],
            "live": bool(r["live"]),
            "url": map_ref_to_url(str(r["resource"]), config),
        }
        for r in claim_rows
    ]
    watched_by_conversation: dict[str, dict[str, int]] = {}
    for r in conn.execute(
        f"""
        SELECT r.conversation_id, r.ref, r.first_seen_at FROM refs r
        JOIN conversations c ON c.conversation_id = r.conversation_id
        LEFT JOIN enrichment e ON e.ref = r.ref
        WHERE {current_ref_sql(f"c.turns - {RECENT_REF_TURNS}")}
        UNION SELECT holder_conversation, resource, acquired_at FROM claims
        """
    ):
        watched_by_conversation.setdefault(str(r[0]), {})[str(r[1])] = int(r[2])
    remote_sessions = [r for r in list_remote_sessions(conn, now) if not config.hides(cast(str, r["tab_name"]))]
    attach_drivers(sessions, remote_sessions, load_drivers(conn, now - DRIVER_WINDOW_MS))
    for remote_session in remote_sessions:
        remote_refs = cast(list[str], remote_session["refs"])
        remote_session.update(
            unread=0,
            open_requests=0,
            claims=[],
            links=map_refs_to_links(
                cast(str | None, remote_session["repo"]),
                cast(str | None, remote_session["branch"]),
                remote_refs,
                config,
                enrichment,
            ),
        )
    sessions.extend(remote_sessions)
    signals = find_signals(
        [
            SessionRefs(
                v.surface,
                v.handle,
                v.branch,
                list(watched_by_conversation.get(v.conversation_id or "", {})),
                watched_by_conversation.get(v.conversation_id or "", {}),
            )
            for v in views
            if v.status != "ended"
        ]
        + [map_remote_to_session_refs(r, sessions) for r in remote_sessions],
        enrichment,
        config.repo_labels,
        now,
    )
    return {
        "now": now,
        "sessions": sessions,
        "items": items,
        "messages": messages,
        "claims": claims,
        "signals": [map_signal_to_dict(s) for s in signals],
        "freshness": provider_freshness(enrichment, collect_watched_refs(conn)),
        "hooks": asdict(check_hooks(conn, now)),
        "user": config.user_alias or "user",
        "remotes": [dict(r) for r in conn.execute("SELECT * FROM remote_hosts ORDER BY host")],
    }


def attach_drivers(local: list[dict[str, object]], remote: list[dict[str, object]], links: list[Link]) -> None:
    """Marks which local session recently drove which live remote tmux session (gone sessions are ignored)."""
    by_name: dict[tuple[str, str], dict[str, object]] = {}
    for r in remote:
        host = str(cast(dict[str, object], r["remote"])["host"])
        by_name[(host, str(r["tab_name"]))] = r
        by_name[(host, str(cast(dict[str, object], r["remote"])["tmux_id"]))] = r
    by_conversation = {str(s.get("conversation_id")): s for s in local}
    for session in local:
        session.setdefault("drives", [])
    for r in remote:
        r["driven_by"] = []
    seen: set[tuple[str, object]] = set()
    # Most recent first, so a remote row's first driver is the session using it now.
    for link in sorted(links, key=lambda x: -x.used_at):
        target = by_name.get((link.host, link.session))
        driver = by_conversation.get(link.conversation_id)
        if target is None or driver is None or (link.conversation_id, target["handle"]) in seen:
            continue
        seen.add((link.conversation_id, target["handle"]))
        cast(list[object], driver["drives"]).append(
            {
                "handle": target["handle"],
                "host": link.host,
                "tmux_id": cast(dict[str, object], target["remote"])["tmux_id"],
                "status": target["status"],
                "agent": target["agent"],
                "used_at": link.used_at,
            }
        )
        cast(list[object], target["driven_by"]).append(driver["handle"])


def map_remote_to_session_refs(remote: dict[str, object], local: list[dict[str, object]]) -> SessionRefs:
    """A remote session's signals point at the local session that drives it, when there is one."""
    by_handle = {s.get("handle"): s for s in local}
    owner = next((by_handle[h] for h in cast(list[object], remote.get("driven_by", [])) if h in by_handle), None)
    return SessionRefs(
        cast(str | None, owner["surface"]) if owner else None,
        cast(str, owner["handle"] if owner else remote["handle"]),
        cast(str | None, remote["branch"]),
        cast(list[str], remote["refs"]),
    )


def map_refs_to_links(
    repo: str | None, branch: str | None, refs: list[str], config: Config, enrichment: dict[str, dict[str, object]]
) -> list[dict[str, object]]:
    """Refs as {kind, label, url, status}; the current branch first, then refs from recent turns."""
    links: list[dict[str, object]] = []
    if repo and branch and branch not in ("main", "master"):
        links.append({"kind": "branch", "label": branch, "url": f"https://github.com/{repo}/tree/{branch}"})
    for ref in refs:
        kind = ref.partition(":")[0]
        label = map_ref_to_label(ref, config.repo_labels)
        data = enrichment.get(ref)
        if data and "state" not in data and data.get("error") == "not found or no access":
            # A bare number that is not a PR (or a repo we cannot see) is noise, not a link worth showing.
            continue
        status = map_enrichment_to_status(ref, data)
        links.append(
            {
                "kind": kind,
                "label": label,
                "url": map_ref_to_url(ref, config) or (map_data_to_web_url(data) if kind == "issue" else ""),
                "status": asdict(status) if status else None,
                "done": bool(data)
                and (data.get("state") in ("MERGED", "CLOSED") or data.get("state_type") in ("completed", "canceled")),
            }
        )
    return links


def map_enrichment_to_url(conn: sqlite3.Connection, ref: str) -> str:
    """The tracker's own link for an issue when issue_url gives none."""
    if not ref.startswith("issue:"):
        return ""
    row = conn.execute("SELECT data FROM enrichment WHERE ref = ?", (ref,)).fetchone()
    data: object = json.loads(row["data"]) if row and row["data"] else None
    return map_data_to_web_url(cast(dict[str, object], data) if isinstance(data, dict) else None)


def map_data_to_web_url(data: dict[str, object] | None) -> str:
    """Only http(s): the URL comes from a tracker adapter and ends up in an href."""
    url = (data or {}).get("url")
    return url if isinstance(url, str) and url.startswith(("https://", "http://")) else ""


def map_ref_to_url(ref: str, config: Config) -> str:
    kind, _, value = ref.partition(":")
    if kind in ("pr", "gh-issue") and "#" in value:
        repo, number = value.split("#", 1)
        return f"https://github.com/{repo}/{'pull' if kind == 'pr' else 'issues'}/{number}"
    if kind == "branch" and value.count(":") == 1:
        repo, branch = value.split(":")
        return f"https://github.com/{repo}/tree/{branch}"
    if kind == "issue" and config.issue_url:
        return config.issue_url.format(key=value)
    return ""


class DashboardHandler(BaseHTTPRequestHandler):
    state: DashboardState

    def do_GET(self) -> None:
        if not self._trusted_request():
            return
        if self.path in ("/", "/guild"):
            body = files("bridge").joinpath("web/guild.html").read_bytes()
            self._send(200, "text/html; charset=utf-8", body)
        elif self.path in ("/dashboard", "/index.html"):
            body = files("bridge").joinpath("web/index.html").read_bytes()
            self._send(200, "text/html; charset=utf-8", body)
        elif match := re.fullmatch(r"/reviews/([\w.-]+/[\w.-]+)/(\d{1,9})/doc", self.path):
            doc = self.state.read_review_doc(match.group(1), int(match.group(2)))
            if doc is None:
                self._send(404, "text/plain", b"no document registered for this PR")
            else:
                self._send_sandboxed_html(doc)
        elif self.path == "/api/guild":
            self._json(200, self.state.guild_snapshot())
        elif self.path == "/api/state":
            self._json(200, self.state.snapshot())
        else:
            self._send(404, "text/plain", b"not found")

    def do_POST(self) -> None:
        # The custom header forces a CORS preflight, so other web pages cannot trigger actions.
        if not self._trusted_request() or self.headers.get("X-Bridge") != "1":
            self._send(403, "text/plain", b"forbidden")
            return
        if match := re.fullmatch(r"/api/items/(\d+)/(resolve|unresolve)", self.path):
            ok = self.state.set_item_resolved(int(match.group(1)), match.group(2) == "resolve")
            self._json(200 if ok else 409, {"ok": ok})
        elif match := re.fullmatch(r"/api/remote/([a-z0-9-]+)/((?:\$|%24)\d+)/attach", self.path):
            ok, detail = self.state.attach_remote(match.group(1), urllib.parse.unquote(match.group(2)))
            self._json(200 if ok else 409, {"ok": ok, "detail": detail})
        elif match := re.fullmatch(r"/api/items/(\d{1,12})/answer", self.path):
            data = self._read_json()
            if data is None or not isinstance(data.get("answer"), str):
                self._json(400, {"ok": False, "detail": "expected {answer: string}"})
                return
            ok, detail = self.state.answer_item(int(match.group(1)), str(data["answer"]))
            self._json(200 if ok else 409, {"ok": ok, "detail": detail})
        elif self.path == "/api/messages":
            data = self._read_json()
            request = map_json_to_user_message(data) if data is not None else None
            if request is None:
                self._json(400, {"ok": False, "detail": "expected {to: [handle], text: string, reply_to: id|null}"})
                return
            ok, detail = self.state.send_from_user(*request)
            self._json(200 if ok else 409, {"ok": ok, "detail": detail})
        elif self.path == "/api/room":
            data = self._read_json()
            text = data.get("text") if data else None
            reply_to = data.get("reply_to") if data else None
            if not isinstance(text, str) or not (reply_to is None or isinstance(reply_to, int)):
                self._json(400, {"ok": False, "detail": "expected {text: string, reply_to: id|null}"})
                return
            ok, detail = self.state.post_to_room(text, reply_to)
            self._json(200 if ok else 409, {"ok": ok, "detail": detail})
        elif match := re.fullmatch(r"/api/messages/(\d{1,12})/read", self.path):
            ok = self.state.mark_read(int(match.group(1)))
            self._json(200 if ok else 409, {"ok": ok})
        elif match := re.fullmatch(r"/api/reviews/([\w.-]+/[\w.-]+)/(\d{1,9})/(publish|retry|repost)", self.path):
            ref = f"pr:{match.group(1)}#{match.group(2)}"
            choice = map_json_to_publish_choice(self._read_json() or {})
            if choice is None:
                self._json(400, {"ok": False, "detail": "expected {direct: bool, mention: [name], comment: string}"})
                return
            ok, detail = self.state.publish_review(ref, match.group(3), *choice)
            self._json(200 if ok else 409, {"ok": ok, "detail": detail})
        elif match := re.fullmatch(r"/api/asks/(\d{1,9})/(close|reopen)", self.path):
            data = self._read_json() or {}
            why = data.get("why", "")
            if not isinstance(why, str) or len(why) > ASK_WHY_MAX_CHARS:
                self._json(400, {"ok": False, "detail": f"expected {{why: string}} under {ASK_WHY_MAX_CHARS} chars"})
                return
            if match.group(2) == "close":
                ok, detail = self.state.close_ask_by_owner(int(match.group(1)))
            else:
                ok, detail = self.state.reopen_ask_by_owner(int(match.group(1)), why)
            self._json(200 if ok else 409, {"ok": ok, "detail": detail})
        elif match := re.fullmatch(r"/api/sessions/([\w-]+)/jump", self.path):
            ok, detail = self.state.jump(match.group(1))
            self._json(200 if ok else 409, {"ok": ok, "detail": detail})
        else:
            self._send(404, "text/plain", b"not found")

    def _read_json(self) -> dict[str, object] | None:
        length_text = self.headers.get("Content-Length") or ""
        if not re.fullmatch(r"[0-9]{1,6}", length_text) or not 0 < int(length_text) <= MAX_POST_BYTES:
            return None
        try:
            data: object = json.loads(self.rfile.read(int(length_text)))
        except ValueError:
            return None
        return cast(dict[str, object], data) if isinstance(data, dict) else None

    def _trusted_request(self) -> bool:
        """Rejects DNS rebinding: only our own host names, and same-origin when a browser sends Origin."""
        port = int(self.server.server_address[1])  # pyright: ignore[reportUnknownArgumentType, reportIndexIssue]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        origin = self.headers.get("Origin")
        if self.headers.get("Host") not in allowed or (
            origin is not None and origin.removeprefix("http://") not in allowed
        ):
            self._send(403, "text/plain", b"forbidden")
            return False
        return True

    def log_message(self, format: str, *args: object) -> None:
        if os.environ.get("BRIDGE_HTTP_LOG"):
            super().log_message(format, *args)

    def _json(self, status: int, payload: object) -> None:
        self._send(status, "application/json", json.dumps(payload).encode())

    def _send_sandboxed_html(self, body: bytes) -> None:
        # An opaque origin: its scripts can draw diagrams but cannot call this server's API.
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Security-Policy", "sandbox allow-scripts")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def map_json_to_publish_choice(data: dict[str, object]) -> tuple[bool, tuple[str, ...], str] | None:
    direct = data.get("direct", False)
    raw = data.get("mention", [])
    comment = data.get("comment", "")
    mention = cast(list[object], raw) if isinstance(raw, list) else None
    if (
        not isinstance(direct, bool)
        or mention is None
        or not all(isinstance(name, str) for name in mention)
        or not isinstance(comment, str)
    ):
        return None
    return direct, tuple(str(name) for name in dict.fromkeys(mention)), comment


def map_json_to_user_message(data: dict[str, object]) -> tuple[list[str], str, int | None] | None:
    """(handles, text, reply_to) from a dashboard request; None when a field has the wrong type."""
    to, text, reply_to = data.get("to", []), data.get("text"), data.get("reply_to")
    if not isinstance(to, list) or not isinstance(text, str):
        return None
    handles = cast(list[object], to)
    if not all(isinstance(h, str) for h in handles):
        return None
    if reply_to is not None and (
        isinstance(reply_to, bool) or not isinstance(reply_to, int) or not 0 < reply_to < 2**53
    ):
        return None
    return [str(h) for h in handles], text, reply_to


def _json_list(raw: object) -> list[object]:
    if not isinstance(raw, str):
        return []
    try:
        value: object = json.loads(raw)
    except ValueError:
        return []
    return cast(list[object], value) if isinstance(value, list) else []


def convert_recipients_to_current_handles(raw: str | None, handle_by_conversation: dict[str, str]) -> str:
    parts: list[str] = []
    for entry in (raw or "").split("\n"):
        conversation_id, _, handle_state = entry.partition("\t")
        handle, _, state = handle_state.rpartition(":")
        if handle:
            parts.append(f"{handle_by_conversation.get(conversation_id, handle)}:{state}")
    return " ".join(parts)


def map_conversations_to_surfaces(conn: sqlite3.Connection) -> dict[str, str]:
    """The live tab holding each conversation: the runtime that last held it, or the one now in its pane."""
    result: dict[str, str] = {}
    for row in conn.execute(
        """
        SELECT c.conversation_id, held.surface FROM conversations c
        JOIN instances held ON held.instance_id = c.instance_id
        WHERE held.surface IS NOT NULL
          AND (held.ended_at IS NULL OR EXISTS (
              SELECT 1 FROM instances i WHERE i.surface = held.surface AND i.ended_at IS NULL))
        """
    ):
        result[str(row["conversation_id"])] = str(row["surface"])
    # Delivery goes to the newest live runtime of a conversation, so the page shows that one too.
    for row in conn.execute(
        """
        SELECT conversation_id, surface FROM instances
        WHERE ended_at IS NULL AND conversation_id IS NOT NULL AND surface IS NOT NULL
        ORDER BY proc_start_ms
        """
    ):
        result[str(row["conversation_id"])] = str(row["surface"])
    return result
