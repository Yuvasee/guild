import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal, TextIO, cast

from bridge.activity import SessionActivity, read_session_activity
from bridge.asks import (
    ASK_AUTOCLOSE_HOURS,
    AskError,
    close_ask,
    close_settled_asks,
    map_db_to_asks_page,
    reopen_ask,
    send_ask_nags,
    send_from_bridge,
    tell_owner_of_reopen,
)
from bridge.attention import load_projects, map_page_to_ranked_page
from bridge.auto_merge import run_auto_merge
from bridge.board import (
    HINT_SENT,
    SYSTEM_CONVERSATION,
    USER_CONVERSATION,
    AddressError,
    MessageStateError,
    Party,
    Recipient,
    SendResult,
    check_user_words,
    flag_overdue,
    resolve_handle,
    send_message,
    write_hint,
)
from bridge.capacity import (
    assess_leads,
    read_owner_presence,
    send_capacity_signals,
    send_idle_work_nudges,
    send_stale_focus_nudges,
)
from bridge.claims import list_claims
from bridge.colleague_comments import (
    HIMSELF,
    claim_choice,
    finish_choice,
    latch_held_breaches,
    post_reply,
    release_failed_choice,
    settle_unfinished_choices,
)
from bridge.compaction import give_up_driving, map_screen_to_draft
from bridge.config import Config, db_path, state_dir
from bridge.context_watch import CONTEXT_EVERY_S, list_context_tabs, read_pane_screen, record_context_screens
from bridge.db import connect, now_ms, transaction
from bridge.disk import DISK_EVERY_S, DiskReading, map_reading_to_dict, measure_disk, read_disk, send_disk_notice
from bridge.doc_nav import THEMES, convert_page_to_themed, convert_served_page_to_styled
from bridge.dream import ALL, map_dreamers_to_page, resolve_dreamers
from bridge.drivers import Link, load_drivers, scan_transcripts
from bridge.enrich import collect_watched_refs, enrich_due, load_enrichment, provider_freshness
from bridge.errlog import StampedLines, log_error
from bridge.errors import UsageError
from bridge.feed import (
    AGTERM_SILENT,
    SCREEN_UNREAD,
    TAB_NOT_FOUND,
    StuckDelivery,
    UndeliveredTab,
    batch_grace_ms,
    dismiss_undelivered_answer,
    find_stuck_deliveries,
    mark_answer_opened,
    post_from_user_to_room,
    prune_typer_locks,
    release_dead_typing_claims,
    reply_on_answer,
    reply_on_card,
    send_from_user,
    spawn_queued_retries,
    spawn_typed_delivery,
    take_delivery_retries,
    take_new_undelivered_tabs,
    take_ripe_batches,
    tell_assistant_of_stuck,
    tell_of_undelivered_tab,
)
from bridge.guard import sync_claims_flag
from bridge.guildview import TABS_STALE_MS, map_db_to_guild, post_signals_to_leads
from bridge.health import check_hooks
from bridge.hooks import spawn_compact_drive
from bridge.issue_sync import LinearError, run_issue_sync
from bridge.leads import is_conversation_locked
from bridge.notify import release_urgent_notice, show_notification, take_next_urgent_notice
from bridge.outage import FailureSpell, LoopStalls, OutageNotices
from bridge.pages import (
    Page,
    find_html_file,
    find_page,
    find_page_file,
    map_missing_page_to_html,
    map_request_path_to_page_file,
    map_request_path_to_slug,
    map_sourceless_page_to_html,
)
from bridge.pinned import (
    PinnedRequest,
    list_pinned_items,
    map_query_string_to_request,
    map_slice_to_json,
    map_slice_to_page,
)
from bridge.project_owner import PROJECTS_RETRY_MS, refresh_open_pr_projects
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
from bridge.remote import collect, list_remote_sessions
from bridge.remote import store as store_remote
from bridge.review_github import GitHubError, run_gh
from bridge.review_post import (
    ReviewError,
    advance_posts,
    ask_for_doc_updates,
    ask_for_docs,
    ask_for_usage_sections,
    ask_to_clean_up,
    ask_to_mark_drafts_ready,
    ask_to_resolve_conflicts,
    map_typed_comment_to_slack,
    record_post_choice,
    record_publish,
    refuse_backport_post,
    reopen_unsent_post,
    retry_failed_post,
    spawn_review_post,
    tell_assistant_of_stale_docs,
)
from bridge.review_slack import TOKEN_EVERY_S, apply_seen_posts, map_db_to_slack_freshness, read_slack_once
from bridge.review_worktree import (
    fill_doc_heads,
    fill_doc_reviews,
    is_worktree_job_running,
    link_worktrees_to_prs,
    map_doc_to_served,
    pick_worktree_job,
    spawn_worktree_job,
)
from bridge.reviews import (
    GITHUB_EVERY_S,
    MergeOkRefused,
    add_owner_label,
    find_issue_lead,
    find_owner_of_ref,
    map_db_to_reviews,
    poll_review_queue,
    read_github_failure,
    record_merge_ok,
    send_to_handle,
)
from bridge.rewake_marks import prune_rewake_marks
from bridge.shadow_filter import EVERY_S as SHADOW_EVERY_S
from bridge.shadow_filter import ask_model as ask_shadow_model
from bridge.shadow_filter import find_start_id as find_shadow_start_id
from bridge.shadow_filter import run_shadow_tick
from bridge.signals import SessionRefs, find_signals, map_enrichment_to_status, map_signal_to_dict
from bridge.spend import map_file_to_spend
from bridge.stall_guard import tell_of_stalls
from bridge.status_watchers import alert_watchers, read_watchers
from bridge.tabinput import map_tree_to_pane_session, run_agterm
from bridge.turncut import SWEEP_EVERY_S as TURN_CUT_SWEEP_EVERY_S
from bridge.turncut import Inspected, sweep_turn_cuts
from bridge.wake import WakeWatch
from bridge.watcher import (
    map_tree_to_blocked_tabs,
    notify_blocked_tabs,
    notify_stray_workers,
    read_screen,
    read_tree,
)
from bridge.weekly_task import send_weekly_task
from bridge.worker_api import start_worker_api
from bridge.workers import (
    WORKER_CAP,
    count_busy_agents,
    count_busy_host_agents,
    find_conversation_runs,
    find_reopened_tabs,
    hold_reopen_lock,
    list_live_workers,
    list_queued_launches,
    map_tree_to_tab,
    mark_reopened,
    start_worker_tab,
)

GUILD_PAGE_VERSION = hashlib.sha256(files("bridge").joinpath("web/guild.html").read_bytes()).hexdigest()[:12]
"""Of the page this service serves: an open page that loaded another one reloads itself."""
RECONCILE_EVERY_S = 10
MAINTAIN_EVERY_S = 30
ASKS_EVERY_S = 60
QUEUE_EVERY_S = 20
CAPACITY_EVERY_S = 300
ENRICH_EVERY_S = 60
REMOTE_EVERY_S = 60
SCAN_EVERY_S = 30
SIGNALS_EVERY_S = 60
TERMINAL_APP = "agterm"
ISSUE_SYNC_EVERY_S = 900
WATCH_EVERY_S = 30
TREE_EVERY_S = 10
WORKTREE_EVERY_S = 30
ACTIVITY_EVERY_S = 10
NOTIFY_EVERY_S = 10
RESOLVED_SHOWN_FOR_MS = 7 * 24 * 3_600_000
DRIVER_WINDOW_MS = 12 * 3_600_000
MAX_POST_BYTES = 65_536
"""Room for the owner's longest words in any script, as JSON."""
# Every SQLite call gives up the GIL, and taking it back waits out a busy sibling thread's slice: at the default 5 ms
# serve's transactions held the write lock about 9x longer, and every hook and CLI waited on them.
GIL_SWITCH_INTERVAL_S = 0.0005
PAGES_ON_PINNED = "/pinned?kind=page"
UNPUBLISHED_TOLD_EVERY_MS = 30 * 60_000
_HTML = "text/html; charset=utf-8"


def serve(config: Config, port: int) -> None:
    sys.setswitchinterval(GIL_SWITCH_INTERVAL_S)
    sys.stdout = cast(TextIO, StampedLines(sys.stdout))
    sys.stderr = cast(TextIO, StampedLines(sys.stderr))
    state = DashboardState(config)
    handler = type("BridgeHandler", (DashboardHandler,), {"state": state})
    state.start_loop(state.maintain_forever)
    state.start_loop(state.enrich_forever)
    state.start_loop(state.post_signals_forever)
    state.start_loop(state.watch_blocked_tabs_forever)
    state.start_loop(state.watch_context_forever)
    state.start_loop(state.sweep_turn_cuts_forever)
    state.start_loop(state.read_tree_forever)
    state.start_loop(state.read_activity_forever)
    state.start_loop(state.scan_transcripts_forever)
    state.start_loop(state.notify_urgent_forever)
    state.start_loop(state.nag_asks_forever)
    state.start_loop(state.start_queued_workers_forever)
    state.start_loop(state.signal_capacity_forever)
    state.start_loop(state.check_disk_forever)
    if config.review_org and config.review_author:
        state.start_loop(state.poll_reviews_forever)
        if config.review_channel_id:
            state.start_loop(state.read_slack_forever)
        if config.review_worktrees_dir:
            state.start_loop(state.prepare_worktrees_forever)
    state.start_loop(state.sync_issues_forever)
    if config.remotes:
        state.start_loop(state.collect_remotes_forever)
    if config.shadow_filter:
        state.start_loop(state.shadow_filter_forever)
    if config.worker_host:
        start_worker_api(config)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"bridge dashboard on http://127.0.0.1:{port}/", flush=True)
    server.serve_forever()


def spawn_typers(typers: list[tuple[int, Recipient]], hint: bool = False) -> None:
    """One tab's failed spawn, as when no process can be spawned, must not end maintenance: what it was for comes due
    again on a later round."""
    for message_id, recipient in typers:
        try:
            if hint and recipient.pid:
                write_hint(recipient.pid, HINT_SENT)
            spawn_typed_delivery(message_id, [recipient])
        except OSError:
            log_error("typer spawn", traceback.format_exc())


POLL_FAILING = (
    "bridge: the review-queue poll has failed for {minutes} min, so new PR comments, review notices and auto-merges "
    "reach no one. Last error: {error}. bridge says once when it works again.",
    "bridge: the review-queue poll works again after {minutes} min.",
)
GITHUB_FAILING = (
    "GitHub is failing: the review poll has not read it for {minutes} min, so new PR comments, review notices and "
    "auto-merges wait. Last error: {error}. bridge says once when GitHub answers again.",
    "GitHub answers the review poll again after {minutes} min.",
)

LOOP_STALL_AFTER_MIN = {
    # Three missed rounds, and never under 10 min: a round can wait out a few 5 s lock or agterm timeouts in a row.
    "maintain_forever": 10,
    "post_signals_forever": 10,
    "watch_blocked_tabs_forever": 10,
    "sweep_turn_cuts_forever": 10,
    "read_tree_forever": 10,
    "read_activity_forever": 10,
    "notify_urgent_forever": 10,
    "nag_asks_forever": 10,
    "start_queued_workers_forever": 10,
    "read_slack_forever": 10,
    "prepare_worktrees_forever": 10,
    # Every 30 s, a round of seconds; PR comments wait on it, and a waiting comment is told at 10 min as well.
    "scan_transcripts_forever": 10,
    # Every 90 s, a screen read per tab, and agterm is slow under load.
    "watch_context_forever": 15,
    "shadow_filter_forever": 15,
    "signal_capacity_forever": 20,
    # Every 90 s; its GitHub reads stop at 5 min a round (ROUND_LIMIT_S).
    "poll_reviews_forever": 15,
    # A round reads GitHub, Linear or a host per item, so one round itself can take minutes.
    "enrich_forever": 30,
    "collect_remotes_forever": 30,
    "sync_issues_forever": 45,
    "check_disk_forever": 180,
}
"""Minutes a loop may go without finishing a round before its stall is told, by its cadence (*_EVERY_S)."""
LOOP_STALLED = (
    "bridge: serve's loop {loop} has not finished a round for {minutes} min (last: {last}), so what it does waits. "
    "serve.log has its stack as it was then. bridge says once when it finishes again."
)
LOOP_LOOK_EVERY_MS = 60_000
"""Loops rest every few seconds; a notice nobody could take is tried again at this pace, not at each rest."""
LOOP_BACK = "bridge: serve's loop {loop} finishes rounds again, after {minutes} min without one."


class DashboardState:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._reconciled_at = 0.0
        self._tree: tuple[dict[str, object] | None, int | None] = (None, None)
        self._scanned_from: int | None = None
        self._activity: tuple[dict[int, SessionActivity] | None, int | None] = (None, None)
        self._disk: DiskReading | None = None
        self._wake = WakeWatch()
        self._outages = OutageNotices()
        self._poll_spell = FailureSpell()
        self._loops = LoopStalls(now_ms())
        self._loops_lock = threading.Lock()
        self._loops_logged: dict[str, int | None] = {}
        """Each stalled loop whose stack is in serve.log, by the last round it had finished: a new stall logs anew."""
        self._loops_looked_at = 0
        self._github_spell = FailureSpell()
        self._projects_retry_at = 0
        self._unpublished_told: dict[str, int] = {}
        self._batch_spawned_at: dict[str, int] = {}
        """When maintenance last woke each tab for its batch; lost on restart, which costs one extra spawn."""
        self._inspected_transcripts: Inspected = {}
        self._weekly_failure: str | None = None
        self._github: Callable[[list[str]], str] = run_gh

    def snapshot(self) -> dict[str, object]:
        with self._connection() as conn:
            self._maybe_reconcile(conn)
            return map_db_to_snapshot(conn, self.config, now_ms())

    def pinned_page(self, request: PinnedRequest) -> str:
        with self._connection() as conn:
            return map_slice_to_page(list_pinned_items(conn, request), request)

    def pinned_items(self, request: PinnedRequest) -> dict[str, object]:
        with self._connection() as conn:
            return map_slice_to_json(list_pinned_items(conn, request))

    def dreams_page(self) -> str:
        with self._connection() as conn:
            return map_dreamers_to_page(resolve_dreamers(conn, self.config, [ALL]))

    def page_response(self, path: str) -> tuple[int, str | None, str, bytes]:
        """Status, redirect target, content type and body for /p/<slug>: the page's own local files, never a hosted
        copy. /p/<slug> moves to /p/<slug>/ so the page's relative links reach its folder; /p/ sends to Pinned."""
        slug = map_request_path_to_slug(path)
        inner = None if slug is not None else map_request_path_to_page_file(path)
        if slug == "":
            return 302, PAGES_ON_PINNED, "", b""
        with self._connection() as conn:
            name = slug if slug is not None else inner[0] if inner is not None else None
            page = find_page(conn, name) if name is not None else None
        if page is None or page.archived_at is not None:
            archived = page is not None
            return (410 if archived else 404), None, _HTML, map_missing_page_to_html(name, archived).encode()
        if inner is not None:
            found = find_page_file(page, inner[1])
            if found is None:
                return 404, None, _HTML, map_missing_page_to_html(name, False).encode()
            return 200, None, mimetypes.guess_type(found.name)[0] or "application/octet-stream", found.read_bytes()
        if not urllib.parse.urlsplit(path).path.endswith("/"):
            query = urllib.parse.urlsplit(path).query
            return 302, f"/p/{page.slug}/" + (f"?{query}" if query else ""), "", b""
        html_file = find_html_file(Path(page.source_dir).expanduser())
        if html_file is None:
            self._tell_owner_of_sourceless_page(page)
            return 404, None, _HTML, map_sourceless_page_to_html(page).encode()
        return (
            200,
            None,
            _HTML,
            convert_page_to_themed(convert_served_page_to_styled(html_file.read_bytes()), self.read_theme()),
        )

    def _tell_owner_of_sourceless_page(self, page: Page) -> None:
        """Whoever opened it saw nothing, so its owner hears so, once per page per half hour."""
        now = now_ms()
        with self._lock:
            if now - self._unpublished_told.get(page.slug, 0) < UNPUBLISHED_TOLD_EVERY_MS:
                return
            self._unpublished_told[page.slug] = now
        text = (
            f"Your page '{page.slug}' was just opened from its stable link, but its source {page.source_dir} holds "
            "no index.html (or single .html file), so the reader saw nothing. Put the page there."
        )
        with self._connection() as conn, transaction(conn):
            send_from_bridge(conn, self.config, page.created_by, text, now)

    def guild_snapshot(self) -> dict[str, object]:
        # No reconcile here: it asks agterm, and maintain_forever keeps the registry current anyway.
        with self._connection() as conn:
            tree, tree_at = self._tree
            activity, activity_at = self._activity
            guild = map_db_to_guild(
                conn,
                self.config,
                now_ms(),
                lambda ref: map_ref_to_url(ref, self.config),
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
            guild["page_version"] = GUILD_PAGE_VERSION
            guild["disk"] = map_reading_to_dict(self._disk) if self._disk else None
            guild["spend"] = map_file_to_spend(self.config.spend_file, now_ms())
            guild["links"] = [{"name": link.name, "url": link.url} for link in self.config.links]
            guild["agents"] = {
                "busy": count_busy_agents(conn),
                "hosts": count_busy_host_agents(conn),
                "cap": self.config.laptop_agent_cap,
                "queued": len(list_queued_launches()),
            }
            asks = map_db_to_asks_page(conn, self.config, now_ms())
            by_owner = cast(dict[str, object], asks.pop("by_owner"))
            for lead in cast(list[dict[str, object]], guild["leads"]):
                lead["asks"] = by_owner.get(str(lead["name"]))
            guild["asks"] = asks
            return map_page_to_ranked_page(guild, reviews, load_projects(conn))

    def start_loop(self, loop: Callable[[], None]) -> None:
        self._loops.limits[loop.__name__] = LOOP_STALL_AFTER_MIN[loop.__name__] * 60_000
        threading.Thread(target=loop, daemon=True).start()

    def _rest(self, loop: str, seconds: float, finished: bool = True) -> None:
        """The end of a round: it counts as finished, every watched loop is looked at, then the loop sleeps."""
        now = now_ms()
        if finished:
            self._loops.finished[loop] = now
        try:
            self._tell_of_stalled_loops(now)
        except Exception:
            # The loop resting here must go on whatever this look hits.
            log_error("loop stall watch", traceback.format_exc())
        time.sleep(seconds)

    def _scan_finished_at(self) -> int:
        """0: none since serve started."""
        return self._loops.finished.get("scan_transcripts_forever", 0)

    def _tell_of_stalled_loops(self, now: int) -> None:
        """Told to whoever hears bridge's failures; one not told is tried at the next rest of any loop."""
        if not self._loops_lock.acquire(blocking=False):
            return
        try:
            # A wall clock set back would otherwise hold every look until it caught up.
            if 0 <= now - self._loops_looked_at < LOOP_LOOK_EVERY_MS:
                return
            self._loops_looked_at = now
            stalled, back = self._loops.take(now, self._wake.counted_from())
            to = (self.config.auto_merge_failures_to or self.config.assistant_handle,)
            for loop, last in stalled:
                minutes = (now - (self._loops.started_at if last is None else last)) // 60_000
                at = "none since serve started" if last is None else _format_utc(last)
                text = LOOP_STALLED.format(loop=loop, minutes=minutes, last=at)
                if loop not in self._loops_logged or self._loops_logged[loop] != last:
                    self._loops_logged[loop] = last
                    print(
                        f"{text}\n{_format_loop_stack(loop)}\n", end="", flush=True
                    )  # One write: no other stdout line lands inside the stack.
                if self._tell_of_failure(text, to):
                    self._loops.mark_told(loop, last)
            for loop, minutes in back:
                text = LOOP_BACK.format(loop=loop, minutes=minutes)
                print(text, flush=True)
                if self._tell_of_failure(text, to):
                    self._loops.mark_back(loop)
        finally:
            self._loops_lock.release()

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
            self._rest("read_tree_forever", TREE_EVERY_S)

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
            self._rest("read_activity_forever", ACTIVITY_EVERY_S)

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
                    write_hint(recipient.pid, HINT_SENT)
            except Exception as exc:
                # Any failure, not just the expected ones: an escaped exception would end signal posts until restart.
                print(f"signal posts failed: {exc}", flush=True)
                log_error("signal posts", traceback.format_exc())
            self.send_weekly_task_once(now_ms())
            self._rest("post_signals_forever", SIGNALS_EVERY_S)

    def send_weekly_task_once(self, now: int) -> None:
        """On its day with nobody running `guild day`; signal_posts keeps it to one send per week. A failure is told
        once per reason, not every minute it repeats."""
        try:
            with self._connection() as conn, transaction(conn):
                weekly = send_weekly_task(conn, self.config, now)
        except Exception as exc:
            # Apart from signal posts: a missing or refused task file must not stop them.
            if str(exc) != self._weekly_failure:
                print(f"weekly task failed: {exc}", flush=True)
                log_error("weekly task", traceback.format_exc())
            self._weekly_failure = str(exc)
            return
        self._weekly_failure = None
        if weekly is None:
            return
        line, sent = weekly
        print(line, flush=True)
        for message_id, recipient in sent:
            if recipient.pid:
                write_hint(recipient.pid, HINT_SENT)
            spawn_typed_delivery(message_id, [recipient])

    def watch_blocked_tabs_forever(self) -> None:
        """A worker stuck on a dialog tells its owner, and a closed one resumed outside its lead's tree its lead."""
        while True:
            try:
                tree = read_tree(self.config)
                if tree is not None:
                    blocked = map_tree_to_blocked_tabs(tree, self.config)
                    screens = {tab.session_id: read_screen(self.config, tab.session_id) for tab in blocked}
                    with self._connection() as conn, transaction(conn):
                        sent = notify_blocked_tabs(conn, self.config, blocked, screens, now_ms())
                        sent += notify_stray_workers(conn, self.config, tree, now_ms())
                    for message_id, recipient in sent:
                        if recipient.pid:
                            write_hint(recipient.pid, HINT_SENT)
                        spawn_typed_delivery(message_id, [recipient])
            except (sqlite3.Error, OSError, TypeError, ValueError, KeyError) as exc:
                print(f"blocked tab watch failed: {exc}", flush=True)
            self._rest("watch_blocked_tabs_forever", WATCH_EVERY_S)

    def sweep_turn_cuts_forever(self) -> None:
        """A cut whose StopFailure hook never ran, or failed, is told to the assistant."""
        while True:
            try:
                with self._connection() as conn:
                    sent = sweep_turn_cuts(conn, self.config, now_ms(), self._inspected_transcripts)
                for message_id, recipient in sent:
                    if recipient.pid:
                        write_hint(recipient.pid, HINT_SENT)
                    spawn_typed_delivery(message_id, [recipient])
            except Exception:
                # Any failure: an escaped exception would end the sweep until restart.
                log_error("turn cut sweep", traceback.format_exc())
            self._rest("sweep_turn_cuts_forever", TURN_CUT_SWEEP_EVERY_S)

    def watch_context_forever(self) -> None:
        """Slower than the blocked watch: a screen read per tab, and agterm is slow under load."""
        while True:
            try:
                self.watch_context_once()
            except Exception:
                # Any failure: an escaped exception would end the watch until restart.
                log_error("context watch", traceback.format_exc())
            self._rest("watch_context_forever", CONTEXT_EVERY_S)

    def watch_context_once(self) -> None:
        tree, tree_at = self._tree
        if tree is None or tree_at is None or now_ms() - tree_at > TABS_STALE_MS:
            return
        with self._connection() as conn:
            tabs = list_context_tabs(conn, self.config, tree)
        screens: dict[str, tuple[str | None, int]] = {}
        for tab in tabs:
            read_at = now_ms()
            screens[tab.conversation_id] = (read_pane_screen(self.config, tab), read_at)
        with self._connection() as conn, transaction(conn):
            actions = record_context_screens(conn, self.config, tabs, screens, now_ms())
        for instance_id, conversation_id, generic_keep in actions.drives:
            if not spawn_compact_drive(
                "compact", instance_id, conversation_id, "none", generic_keep=generic_keep, by_screen=True
            ):
                with self._connection() as conn, transaction(conn):
                    give_up_driving(conn, conversation_id)
        for message_id, recipient in actions.messages:
            if recipient.pid:
                write_hint(recipient.pid, HINT_SENT)
            spawn_typed_delivery(message_id, [recipient])

    def notify_urgent_forever(self) -> None:
        """Wherever the owner is looking: no typing, and no tab is selected or marked."""
        while True:
            try:
                with self._connection() as conn:
                    failed: frozenset[int] = frozenset()
                    while True:
                        with transaction(conn):
                            notice = take_next_urgent_notice(conn, self.config, now_ms(), failed)
                        if notice is None:
                            break
                        if not show_notification(notice.text):
                            failed |= {notice.item_id}
                            with transaction(conn):
                                release_urgent_notice(conn, notice.item_id)
            except sqlite3.Error as exc:
                print(f"urgent notification failed: {exc}", flush=True)
            self._rest("notify_urgent_forever", NOTIFY_EVERY_S)

    def shadow_filter_forever(self) -> None:
        """Its own thread: a slow or failing model call holds up nothing else. It only logs: batches hold wakes now."""
        url, key_file = self.config.shadow_filter_url, self.config.shadow_filter_key_file
        logging_only = replace(self.config, shadow_filter_live=False)
        after: int | None = None
        while True:
            try:
                with self._connection() as conn:
                    if after is None:
                        after = find_shadow_start_id(conn, logging_only, now_ms())
                    after = run_shadow_tick(
                        conn, logging_only, after, now_ms(), lambda state: ask_shadow_model(url, key_file, state)
                    )
            except Exception as exc:
                # Any failure: an escaped exception would end the filter until restart. The log says how far it got.
                after = None
                print(f"shadow filter failed: {exc}", flush=True)
                log_error("shadow filter", traceback.format_exc())
            self._rest("shadow_filter_forever", SHADOW_EVERY_S)

    def start_queued_workers_forever(self) -> None:
        """Workers queued by the laptop cap start, oldest first, as slots free; the lead that asked is told."""
        while True:
            try:
                self.start_queued_workers_once()
            except Exception:
                log_error("start queued workers", traceback.format_exc())
            self._rest("start_queued_workers_forever", QUEUE_EVERY_S)

    def start_queued_workers_once(self) -> None:
        for path in list_queued_launches():
            try:
                launch = json.loads(path.read_text())
            except FileNotFoundError:
                continue
            reopens = launch.get("reopens")
            # A reopen's checks and start run under the CLI's lock, taken before any write transaction.
            with self._connection() as conn, hold_reopen_lock() if reopens else nullcontext():
                if not path.exists():
                    continue
                started = self._start_queued_launch(conn, path, launch)
                if started is None:
                    return
            if started:
                spawn_typed_delivery(started.message_id, started.recipients)

    def _start_queued_launch(
        self, conn: sqlite3.Connection, path: Path, launch: dict[str, Any]
    ) -> SendResult | Literal[False] | None:
        """None: no slot, stop for now; False: this one waits or was dropped quietly."""
        cap = self.config.laptop_agent_cap
        if cap and count_busy_agents(conn) >= cap:
            return None
        tree = read_tree(self.config)
        if tree is None:
            return None
        parent = str(launch["parent"])
        if map_tree_to_tab(tree, parent) is None:
            path.unlink(missing_ok=True)
            return False
        if len(list_live_workers(conn, tree, parent)) >= WORKER_CAP:
            return False
        reopens = launch.get("reopens")
        if reopens:
            dead = find_dead_instances(conn)
            with transaction(conn):
                end_instances(conn, dead, now_ms())
        path.unlink(missing_ok=True)
        with transaction(conn):
            if reopens and (
                find_conversation_runs(conn, str(reopens))
                or any(map_tree_to_tab(tree, tab) for tab in find_reopened_tabs(conn, str(reopens)))
            ):
                text = f"Your queued reopen of {launch['name']!r} is dropped: it already runs."
            else:
                session_id = start_worker_tab(
                    conn, self.config, parent, str(launch["name"]), Path(str(launch["cwd"])), str(launch["command"])
                )
                if reopens:
                    mark_reopened(conn, str(reopens), session_id, now_ms())
                text = f"Your queued worker {launch['name']!r} has started (tab {session_id}): a laptop slot freed."
            return send_message(
                conn,
                Party(SYSTEM_CONVERSATION, "bridge"),
                [resolve_handle(conn, str(launch["requested_by"]))],
                "info",
                text,
                now_ms(),
            )

    def nag_asks_forever(self) -> None:
        """With every page closed too: an ask nobody moved, or claimed and never checked, reaches its owner."""
        while True:
            try:
                self.nag_asks_once(now_ms())
            except sqlite3.Error as exc:
                print(f"asks nag failed: {exc}", flush=True)
            self._rest("nag_asks_forever", ASKS_EVERY_S)

    def nag_asks_once(self, now: int) -> None:
        with self._connection() as conn, transaction(conn):
            close_settled_asks(conn, now, ASK_AUTOCLOSE_HOURS * 3_600_000, self.config.ask_autoclose_decision)
            sent = send_ask_nags(conn, self.config, now, read_owner_presence(conn, self.config, now).off)
        for message_id, recipient in sent:
            if recipient.pid:
                write_hint(recipient.pid, HINT_SENT)
            spawn_typed_delivery(message_id, [recipient])

    def signal_capacity_forever(self) -> None:
        """With every page closed too: a lead with room for planned work is named to the assistant, with next items."""
        while True:
            try:
                self.signal_capacity_once(now_ms())
            except Exception as exc:
                # An escaped exception would end capacity signals until restart.
                print(f"capacity signals failed: {exc}", flush=True)
                log_error("capacity signals", traceback.format_exc())
            self._rest("signal_capacity_forever", CAPACITY_EVERY_S)

    def signal_capacity_once(self, now: int) -> None:
        tree = read_tree(self.config)
        with self._connection() as conn:
            # Read before the write lock: the review queue and enrichment reads take a while.
            loads = assess_leads(conn, self.config, now, tree)
            with transaction(conn):
                sent = send_capacity_signals(conn, self.config, now, loads)
                sent += send_idle_work_nudges(conn, self.config, now, loads)
                sent += send_stale_focus_nudges(conn, self.config, now)
        for message_id, recipient in sent:
            if recipient.pid:
                write_hint(recipient.pid, HINT_SENT)
            spawn_typed_delivery(message_id, [recipient])

    def check_disk_forever(self) -> None:
        """Hourly, with every page closed too: free space for the page, and the assistant hears when it runs low."""
        while True:
            try:
                self.check_disk_once(now_ms())
            except Exception as exc:
                # An escaped exception would end disk checks until restart.
                print(f"disk check failed: {exc}", flush=True)
                log_error("disk check", traceback.format_exc())
            self._rest("check_disk_forever", DISK_EVERY_S)

    def check_disk_once(self, now: int) -> None:
        reading = read_disk(now)
        if self._disk is None:
            self._disk = reading
        # du takes tens of seconds: outside any transaction, and the page shows the last reading meanwhile.
        self._disk = reading = measure_disk(reading, self.config.disk_folders)
        with self._connection() as conn, transaction(conn):
            sent = send_disk_notice(conn, self.config, reading)
        for message_id, recipient in sent:
            if recipient.pid:
                write_hint(recipient.pid, HINT_SENT)
            spawn_typed_delivery(message_id, [recipient])

    def sync_issues_forever(self) -> None:
        while True:
            try:
                with self._connection() as conn:
                    report = run_issue_sync(conn, self.config, now_ms(), dry_run=False)
                for _, recipient in report.sent:
                    if recipient.pid:
                        write_hint(recipient.pid, HINT_SENT)
            except Exception:
                log_error("issue sync", traceback.format_exc())
            self._rest("sync_issues_forever", ISSUE_SYNC_EVERY_S)

    def close_ask_by_owner(self, ask_id: int) -> tuple[bool, str]:
        try:
            with self._connection() as conn, transaction(conn):
                close_ask(conn, ask_id, "owner", self.config.user_alias or "user", now_ms())
        except AskError as exc:
            return False, str(exc)
        return True, "closed"

    def reopen_ask_by_owner(self, ask_id: int, why: str) -> tuple[bool, str]:
        try:
            why = check_user_words(why) if why.strip() else ""
        except MessageStateError as exc:
            return False, str(exc)
        try:
            with self._connection() as conn, transaction(conn):
                reason = f"on the page: {why}" if why else "on the page, no reason given"
                ask = reopen_ask(conn, ask_id, self.config.user_alias or "user", reason, now_ms())
                told = tell_owner_of_reopen(conn, self.config, ask, now_ms())
        except (AskError, MessageStateError) as exc:
            return False, str(exc)
        for message_id, recipient in told:
            if recipient.pid:
                write_hint(recipient.pid, HINT_SENT)
            spawn_typed_delivery(message_id, [recipient])
        if not told:
            return True, f"open again, but {ask.owner} was not told: its tab is locked or has no live session"
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
                        flag_overdue(conn, now_ms(), batch_grace_ms(self.config))
                        release_dead_typing_claims(conn, now_ms())
                        retries = take_delivery_retries(conn, now_ms())
                        ripe = take_ripe_batches(conn, self.config, now_ms(), self._batch_spawned_at)
                        undelivered = take_new_undelivered_tabs(conn, self.config, now_ms(), self._wake.counted_from())
                        stuck = find_stuck_deliveries(conn, now_ms())
                    sync_claims_flag(conn)
                    for found in stuck:
                        box = self._read_stuck_box(found)
                        with transaction(conn):
                            told = tell_assistant_of_stuck(conn, self.config, found, box, now_ms())
                        if told is not None:
                            retries.append(told)
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
                    overdue = [str(r[0]) for r in conn.execute("SELECT handle FROM undelivered_tabs")]
                    for cause, text in self._outages.take(
                        overdue, self.config.assistant_handle, self._wake.silent_since(), now_ms()
                    ):
                        if not show_notification(text):
                            self._outages.release(cause)
                    self._alert_watchers(conn)
                spawn_typers(retries)
                spawn_typers(ripe, hint=True)
                spawn_queued_retries()
                prune_typer_locks()
                prune_rewake_marks()
            except sqlite3.Error as exc:
                print(f"maintenance failed: {exc}", flush=True)
            self._rest("maintain_forever", MAINTAIN_EVERY_S)

    def _alert_watchers(self, conn: sqlite3.Connection) -> None:
        try:
            with transaction(conn):
                sent = alert_watchers(conn, self.config, now_ms())
            for message_id, recipient in sent:
                if recipient.pid:
                    write_hint(recipient.pid, HINT_SENT)
                spawn_typed_delivery(message_id, [recipient])
        except Exception:
            # Kept apart from the loop's own handling: an escaped error would end maintenance until restart.
            log_error("watcher alerts", traceback.format_exc())

    def _read_stuck_box(self, stuck: StuckDelivery) -> str | None:
        if not stuck.agterm_session or not stuck.surface:
            return None
        target = ["--target", stuck.agterm_session, "--pane-id", stuck.surface]
        screen = run_agterm(self.config, ["session", "text", *target, "--lines", "40"])
        return map_screen_to_draft(screen) if screen is not None else None

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
        """GitHub and Linear are polled here, never from hooks."""
        while True:
            try:
                with self._connection() as conn:
                    enrich_due(conn, self.config, now_ms())
            except (sqlite3.Error, OSError, ValueError) as exc:
                print(f"enrichment failed: {exc}", flush=True)
            self._rest("enrich_forever", ENRICH_EVERY_S)

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
            self._rest("collect_remotes_forever", REMOTE_EVERY_S)

    def poll_reviews_forever(self) -> None:
        """With every page closed too: the queue stays current and the owner's comments reach their owners. A GitHub
        read that fails ends the round as usual, kept as the poll's error, so GitHub failing is told apart from bridge
        failing."""
        while True:
            try:
                with self._connection() as conn:
                    sent = poll_review_queue(
                        conn, self.config, now_ms(), self._tree[0], self._scanned_from, self._refresh_pr_projects
                    )
                    try:
                        sent = [*sent, *run_auto_merge(conn, self.config, now_ms())]
                    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, MessageStateError) as exc:
                        print(f"auto-merge failed: {exc}", flush=True)
                    with transaction(conn):
                        latch_held_breaches(conn, now_ms())
                    user = self.config.user_alias or "user"
                    for result in settle_unfinished_choices(conn, self.config, self._github, user, now_ms()):
                        self._deliver(result, result.recipients)
                    self._advance_reviews(conn, sent)
            except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, MessageStateError) as exc:
                print(f"review queue poll failed: {exc}", flush=True)
                self._tell_of_stalls_alone()
                self._follow_spell(self._poll_spell, str(exc).replace("/p/", "p/"), POLL_FAILING)
            else:
                self._follow_spell(self._poll_spell, None, POLL_FAILING)
            self._follow_github_spell()
            self._rest("poll_reviews_forever", GITHUB_EVERY_S)

    def _follow_github_spell(self) -> None:
        """After every round, failed or not: its GitHub read is committed before the steps that may raise."""
        try:
            with self._connection() as conn:
                github = read_github_failure(conn)
        except (sqlite3.Error, OSError) as exc:
            print(f"reading the review poll's GitHub state failed: {exc}", flush=True)
            return
        self._follow_spell(self._github_spell, github[0] if github else None, GITHUB_FAILING)

    def _follow_spell(self, spell: FailureSpell, error: str | None, notices: tuple[str, str]) -> None:
        """error: this round failed with it, None: it worked. notices: the failing and the back line, formatted with
        minutes and error."""
        failing, back = notices
        if error is not None:
            minutes = spell.fail(now_ms(), self._wake.counted_from())
            if minutes is not None:
                spell.told = self._tell_of_failure(failing.format(minutes=minutes, error=error))
            return
        minutes = spell.recovered(now_ms())
        if minutes is None or self._tell_of_failure(back.format(minutes=minutes)):
            spell.reset()

    def _tell_of_stalls_alone(self) -> None:
        """A failed round never reached the guard in _advance_reviews, or rolled it back with it."""
        try:
            with self._connection() as conn, transaction(conn):
                told = tell_of_stalls(conn, self.config, now_ms(), self._scan_finished_at())
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, MessageStateError) as exc:
            print(f"review stall guard failed: {exc}", flush=True)
            return
        for message_id, recipient in told:
            try:
                if recipient.pid:
                    write_hint(recipient.pid, HINT_SENT)
                spawn_typed_delivery(message_id, [recipient])
            except OSError as exc:
                print(f"delivering a review stall notice to {recipient.handle} failed: {exc}", flush=True)

    def _refresh_pr_projects(self, conn: sqlite3.Connection) -> None:
        """A failure only leaves routing on the cache it has, so it is logged and tried again later, not raised."""
        if now_ms() < self._projects_retry_at:
            return
        try:
            refresh_open_pr_projects(conn, self.config, now_ms())
        except Exception as exc:
            self._projects_retry_at = now_ms() + PROJECTS_RETRY_MS
            print(f"reading the PRs' issue projects failed, retry in 10 min: {exc}", flush=True)
            if not isinstance(exc, LinearError):
                log_error("issue projects", traceback.format_exc())

    def _tell_of_failure(self, text: str, to: tuple[str, ...] = ()) -> bool:
        """to, or else the assistant and whoever hears bridge's failures (auto_merge_failures_to); whether anyone was
        told."""
        wanted = to or (self.config.assistant_handle, self.config.auto_merge_failures_to)
        handles = list(dict.fromkeys(h for h in wanted if h))
        told = False
        for handle in handles:
            try:
                with self._connection() as conn, transaction(conn):
                    message_id, recipient = send_to_handle(conn, handle, text, now_ms())
            except (sqlite3.Error, AddressError, MessageStateError) as exc:
                print(f"telling {handle} of a bridge failure failed: {exc}", flush=True)
                continue
            told = True
            try:
                if recipient.pid:
                    write_hint(recipient.pid, HINT_SENT)
                spawn_typed_delivery(message_id, [recipient])
            except OSError as exc:
                print(f"delivering the bridge-failure notice to {handle} failed: {exc}", flush=True)
        return told

    def read_slack_forever(self) -> None:
        team_url: str | None = None
        while True:
            try:
                with self._connection() as conn:
                    team_url = read_slack_once(conn, self.config, team_url, now_ms())
            except (sqlite3.Error, OSError, ValueError) as exc:
                print(f"review channel read failed: {type(exc).__name__}", flush=True)
            self._rest("read_slack_forever", TOKEN_EVERY_S)

    def _advance_reviews(self, conn: sqlite3.Connection, sent: list[tuple[int, Recipient]]) -> None:
        claim = now_ms()
        with transaction(conn):
            apply_seen_posts(conn, claim)
            to_send, told = advance_posts(conn, self.config, claim)
            fill_doc_heads(conn)
            fill_doc_reviews(conn, self.config)
            told = [*told, *ask_for_docs(conn, self.config, now_ms())]
            told = [*told, *ask_for_usage_sections(conn, self.config, now_ms())]
            told = [*told, *ask_for_doc_updates(conn, self.config, now_ms())]
            told = [*told, *tell_assistant_of_stale_docs(conn, self.config, now_ms())]
            told = [*told, *tell_of_stalls(conn, self.config, now_ms(), self._scan_finished_at())]
            told = [*told, *ask_to_clean_up(conn, self.config, now_ms())]
            told = [*told, *ask_to_mark_drafts_ready(conn, self.config, now_ms())]
            told = [*told, *ask_to_resolve_conflicts(conn, self.config, now_ms())]
        for ref in to_send:
            spawn_review_post(ref, claim)
        for message_id, recipient in [*sent, *told]:
            if recipient.pid:
                write_hint(recipient.pid, HINT_SENT)
            spawn_typed_delivery(message_id, [recipient])

    def prepare_worktrees_forever(self) -> None:
        """One job at a time, in a runner of its own that outlives a restart, so a batch of large PRs cannot load the
        laptop and a deploy does not cut an install short."""
        while True:
            try:
                with self._connection() as conn:
                    link_worktrees_to_prs(conn, self.config.review_python_paths)
                    # The runner picks again under its lock; this look only spares a start when nothing is due.
                    due = not is_worktree_job_running() and pick_worktree_job(conn, self.config, now_ms()) is not None
                if due:
                    spawn_worktree_job()
            except (sqlite3.Error, OSError, ValueError) as exc:
                print(f"review worktree failed: {exc}", flush=True)
            self._rest("prepare_worktrees_forever", WORKTREE_EVERY_S)

    def read_review_doc(self, repo: str, number: int) -> bytes | None:
        with self._connection() as conn:
            doc = map_doc_to_served(conn, repo, number)
        return None if doc is None else convert_page_to_themed(doc, self.read_theme())

    def read_theme(self) -> str | None:
        try:
            theme = (state_dir() / "theme").read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return theme if theme in THEMES else None

    def write_theme(self, theme: str) -> None:
        (state_dir() / "theme").write_text(theme + "\n", encoding="utf-8")

    def publish_review(
        self, ref: str, action: str, direct: bool = False, mention: tuple[str, ...] = (), comment: str = ""
    ) -> tuple[bool, str]:
        people = dict(self.config.review_mention_people)
        if unknown := [name for name in mention if name not in people]:
            return False, f"not in review_mention_people: {', '.join(unknown)}"
        published: sqlite3.Row | None = None
        try:
            post_comment = map_typed_comment_to_slack(comment, people) if mention else ""
            with self._connection() as conn:
                with transaction(conn):
                    refuse_backport_post(conn, self.config, ref)
                    if action == "retry":
                        retry_failed_post(conn, ref)
                    elif action == "repost":
                        reopen_unsent_post(conn, ref)
                        record_post_choice(conn, ref, direct, tuple(people[name] for name in mention), post_comment)
                    else:
                        by = self.config.user_alias or "user"
                        ids = tuple(people[name] for name in mention)
                        record_publish(conn, ref, by, now_ms(), direct, ids, post_comment)
                        published = conn.execute("SELECT repo, number FROM review_prs WHERE ref = ?", (ref,)).fetchone()
                self._advance_reviews(conn, [])
        except ReviewError as exc:
            return False, str(exc)
        if published is not None and self.config.review_owner_label:
            # Off the request: GitHub may take seconds, and the poll labels it anyway if this fails.
            args = (self.config, str(published["repo"]), int(published["number"]))
            threading.Thread(target=add_owner_label, args=args, daemon=True).start()
        return True, "ok"

    def ok_merge(self, ref: str, head_oid: str) -> tuple[bool, str]:
        try:
            with self._connection() as conn, transaction(conn):
                by = self.config.user_alias or "user"
                message_id, recipient = record_merge_ok(conn, self.config, ref, head_oid, by, now_ms())
        except (MergeOkRefused, MessageStateError) as exc:
            return False, str(exc)
        try:
            if recipient.pid:
                write_hint(recipient.pid, HINT_SENT)
            spawn_typed_delivery(message_id, [recipient])
        except OSError as exc:
            print(f"merge OK delivery to {recipient.handle} failed to start: {exc}", flush=True)
        return True, recipient.handle

    def scan_transcripts_forever(self) -> None:
        """Only a scan that ends counts as a finished round: PR comments wait on one."""
        while True:
            finished = False
            try:
                with self._connection() as conn:
                    started = now_ms()
                    scan_transcripts(conn, self.config.remotes, started)
                    self._scanned_from = started
                finished = True
            except Exception as exc:
                # Any failure: an escaped exception would end the scan, and every comment's verdict, until restart.
                print(f"transcript scan failed: {exc}", flush=True)
                if not isinstance(exc, (sqlite3.Error, OSError)):
                    log_error("transcript scan", traceback.format_exc())
            self._rest("scan_transcripts_forever", SCAN_EVERY_S, finished)

    def set_item_resolved(self, item_id: int, resolved: bool) -> bool:
        with self._connection() as conn, transaction(conn):
            if resolved and dismiss_undelivered_answer(conn, item_id, now_ms()):
                return True
            cursor = conn.execute(
                "UPDATE user_items SET resolved_at = ?1 WHERE id = ?2 AND (resolved_at IS NULL) = ?3",
                (now_ms() if resolved else None, item_id, resolved),
            )
            return cursor.rowcount > 0

    def answer_item(self, item_id: int, answer: str, ask: bool = False) -> tuple[bool, str]:
        try:
            with self._connection() as conn, transaction(conn):
                result, asked = reply_on_card(conn, item_id, answer, self.config.user_alias or "user", now_ms(), ask)
        except MessageStateError as exc:
            return False, str(exc)
        self._deliver(result, result.recipients)
        return True, "asked" if asked else "sent"

    def reply_on_answer(self, item_id: int, text: str) -> tuple[bool, str]:
        try:
            with self._connection() as conn, transaction(conn):
                result = reply_on_answer(
                    conn, item_id, text, self.config.user_alias or "user", self.config.assistant_handle, now_ms(),
                    lambda conversation: is_conversation_locked(conn, self.config, conversation),
                )  # fmt: skip
        except (MessageStateError, AddressError) as exc:
            return False, str(exc)
        self._deliver(result, result.recipients)
        return True, "sent"

    def open_answer(self, item_id: int) -> bool:
        with self._connection() as conn, transaction(conn):
            return mark_answer_opened(conn, item_id, now_ms())

    def choose_colleague_reply(self, item_id: int, choice: str, text: str) -> tuple[bool, str]:
        """GitHub is written only here, on the owner's click, and with no transaction open."""
        user = self.config.user_alias or "user"
        try:
            with self._connection() as conn, transaction(conn):
                chosen = claim_choice(conn, item_id, choice, text, now_ms())
            url = None
            if chosen.choice != HIMSELF:
                try:
                    url, text = post_reply(chosen, self._github)
                    chosen = replace(chosen, reply=text)
                except GitHubError as exc:
                    with self._connection() as conn, transaction(conn):
                        release_failed_choice(conn, item_id, str(exc))
                    return False, f"not posted: {exc}"
            try:
                with self._connection() as conn, transaction(conn):
                    result = finish_choice(conn, self.config, chosen, url, user, now_ms())
            except sqlite3.Error as exc:
                posted = f"posted as {url}" if url else "chosen"
                return False, f"{posted}, but not recorded ({exc}); bridge records it within minutes and never reposts"
        except (UsageError, MessageStateError) as exc:
            return False, str(exc)
        self._deliver(result, result.recipients)
        return True, "sent"

    def send_from_user(self, to: list[str], text: str, reply_to: int | None) -> tuple[bool, str]:
        try:
            with self._connection() as conn, transaction(conn):
                result, typed = send_from_user(
                    conn, self.config, self.config.user_alias or "user", to, check_user_words(text), now_ms(), reply_to
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
                    write_hint(recipient.pid, HINT_SENT)
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
            subprocess.run(["open", "-a", TERMINAL_APP], capture_output=True, timeout=5, check=False)
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


def _format_loop_stack(loop: str) -> str:
    """Where the loop's thread is now: what a restart would erase. A thread is named 'Thread-N (<its target>)'."""
    frames = sys._current_frames()  # pyright: ignore[reportPrivateUsage]
    for thread in threading.enumerate():
        if thread.name.endswith(f"({loop})") and thread.ident in frames:
            return f"{thread.name} is at:\n" + "".join(traceback.format_stack(frames[thread.ident])).rstrip()
    return f"no thread runs {loop}: it ended"


def _format_utc(at: int) -> str:
    return time.strftime("%m-%d %H:%M UTC", time.gmtime(at / 1000))


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
    for view in list_sessions(conn, now):
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

    def route_issue(issue_ref: str, linked: list[str], closing: list[str]) -> tuple[str | None, str | None] | None:
        owner = find_issue_lead(conn, config, issue_ref, linked, closing, enrichment)
        return (owner.surface, owner.handle) if owner else None

    def route_pr(ref: str, data: dict[str, object]) -> tuple[str | None, str | None] | None:
        owner = find_owner_of_ref(conn, config, ref, str(data.get("head") or ""))
        return (owner.surface, owner.handle) if owner else None

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
        + [map_remote_to_session_refs(r, sessions) for r in remote_sessions]
        # A lead's declared PR still has a lead after the worker that made it has ended.
        + [SessionRefs(None, None, None, list_lead_work_prs(conn))],
        enrichment,
        now,
        route_issue,
        route_pr,
        config.review_stall_status,
        config.review_bot_name,
        {
            str(r["ref"]).lower(): (str(r["ci_blocked_for"]), str(r["ci_blocked_by"]))
            for r in conn.execute(
                "SELECT ref, ci_blocked_for, ci_blocked_by FROM review_prs "
                "WHERE closed_at IS NULL AND ci_blocked_by IS NOT NULL"
            )
        },
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


def list_lead_work_prs(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT r.value FROM lead_work w, json_each(w.refs) r WHERE w.done_at IS NULL AND r.value LIKE 'pr:%'"
    )
    return sorted(str(r[0]) for r in rows)


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
        label = map_ref_to_label(ref)
        data = enrichment.get(ref)
        if data and "state" not in data and data.get("error") == "not found or no access":
            # A bare number that is not a PR (or a repo we cannot see) is noise, not a link worth showing.
            continue
        status = map_enrichment_to_status(ref, data)
        links.append(
            {
                "kind": kind,
                "label": label,
                "url": map_ref_to_url(ref, config),
                "status": asdict(status) if status else None,
                "done": bool(data)
                and (data.get("state") in ("MERGED", "CLOSED") or data.get("state_type") in ("completed", "canceled")),
            }
        )
    return links


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
        elif match := re.fullmatch(r"/reviews/([\w.-]+/[\w.-]+)/(\d{1,9})/doc", self.path):
            doc = self.state.read_review_doc(match.group(1), int(match.group(2)))
            if doc is None:
                self._send(404, "text/plain", b"no document registered for this PR")
            else:
                self._send_sandboxed_html(doc)
        elif match := re.fullmatch(r"/(api/)?pinned(?:\?(.*))?", self.path):
            try:
                request = map_query_string_to_request(match.group(2) or "")
            except ValueError as error:
                self._send(400, "text/plain", str(error).encode())
            else:
                if match.group(1):
                    self._json(200, self.state.pinned_items(request))
                else:
                    self._send(200, "text/html; charset=utf-8", self.state.pinned_page(request).encode())
        elif self.path == "/dreams":
            self._send(200, "text/html; charset=utf-8", self.state.dreams_page().encode())
        elif self.path == "/p" or self.path.startswith(("/p/", "/p?")):
            status, target, content_type, body = self.state.page_response(self.path)
            if target is not None:
                self._redirect(target)
            else:
                self._send(status, content_type, body)
        elif self.path == "/api/guild":
            self._json(200, self.state.guild_snapshot())
        elif self.path == "/api/state":
            self._json(200, self.state.snapshot())
        else:
            self._send(404, "text/plain", b"not found")

    def do_POST(self) -> None:
        if self.path == "/theme":
            self._set_theme()
            return
        # The custom header forces a CORS preflight, so other web pages cannot trigger actions.
        if not self._trusted_request() or self.headers.get("X-Bridge") != "1":
            self._send(403, "text/plain", b"forbidden")
            return
        length = self.headers.get("Content-Length") or ""
        if length.isdigit() and int(length) > MAX_POST_BYTES:
            detail = f"too long to send: {int(length):,} bytes, the most is {MAX_POST_BYTES:,}"
            self._json(413, {"ok": False, "detail": detail})
            return
        if match := re.fullmatch(r"/api/items/(\d+)/(resolve|unresolve)", self.path):
            ok = self.state.set_item_resolved(int(match.group(1)), match.group(2) == "resolve")
            self._json(200 if ok else 409, {"ok": ok})
        elif match := re.fullmatch(r"/api/items/(\d{1,12})/answer", self.path):
            data = self._read_json()
            if data is None or not isinstance(data.get("answer"), str):
                self._json(400, {"ok": False, "detail": "expected {answer: string}"})
                return
            ok, detail = self.state.answer_item(int(match.group(1)), str(data["answer"]), data.get("ask") is True)
            self._json(200 if ok else 409, {"ok": ok, "detail": detail})
        elif match := re.fullmatch(r"/api/items/(\d{1,12})/reply", self.path):
            data = self._read_json()
            if data is None or not isinstance(data.get("text"), str):
                self._json(400, {"ok": False, "detail": "expected {text: string}"})
                return
            ok, detail = self.state.reply_on_answer(int(match.group(1)), str(data["text"]))
            self._json(200 if ok else 409, {"ok": ok, "detail": detail})
        elif match := re.fullmatch(r"/api/items/(\d{1,12})/open", self.path):
            ok = self.state.open_answer(int(match.group(1)))
            self._json(200, {"ok": ok})
        elif match := re.fullmatch(r"/api/colleague-replies/(\d{1,12})", self.path):
            data = self._read_json()
            choice, text = (data or {}).get("choice"), (data or {}).get("text", "")
            if not isinstance(choice, str) or not isinstance(text, str):
                self._json(400, {"ok": False, "detail": "expected {choice: string, text: string}"})
                return
            ok, detail = self.state.choose_colleague_reply(int(match.group(1)), choice, text)
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
        elif match := re.fullmatch(r"/api/reviews/([\w.-]+/[\w.-]+)/(\d{1,9})/merge-ok", self.path):
            head_oid = (self._read_json() or {}).get("head_oid")
            if not isinstance(head_oid, str) or not re.fullmatch(r"[0-9a-f]{1,64}", head_oid):
                self._json(400, {"ok": False, "detail": "expected {head_oid: hex string}"})
                return
            ok, detail = self.state.ok_merge(f"pr:{match.group(1)}#{match.group(2)}", head_oid)
            self._json(200 if ok else 409, {"ok": ok, "detail": detail})
        elif match := re.fullmatch(r"/api/asks/(\d{1,9})/(close|reopen)", self.path):
            data = self._read_json() or {}
            why = data.get("why", "")
            if not isinstance(why, str):
                self._json(400, {"ok": False, "detail": "expected {why: string}"})
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

    def _set_theme(self) -> None:
        """The reader's light / dark / auto choice. A review doc sends it from its sandbox, whose origin is "null", as a
        plain no-cors POST; the worst another page can do with it is change the theme."""
        port = int(self.server.server_address[1])  # pyright: ignore[reportUnknownArgumentType, reportIndexIssue]
        ours = {f"127.0.0.1:{port}", f"localhost:{port}"}
        origin = self.headers.get("Origin")
        length = self.headers.get("Content-Length") or ""
        if self.headers.get("Host") not in ours or not (
            origin in (None, "null") or origin.removeprefix("http://") in ours
        ):
            self._send(403, "text/plain", b"forbidden")
            return
        theme = (
            self.rfile.read(int(length)).decode("utf-8", "replace").strip()
            if re.fullmatch(r"[0-9]{1,2}", length)
            else ""
        )
        if theme not in THEMES:
            self._send(400, "text/plain", b"expected light, dark or auto")
            return
        self.state.write_theme(theme)
        self._send(204, "text/plain", b"")

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

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

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
