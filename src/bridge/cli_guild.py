import argparse
import json
import os
import re
import shlex
import sqlite3
import sys
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, cast

from bridge.asks import AskError
from bridge.attention import PriorityError, load_projects, map_arg_to_project, store_projects
from bridge.board import (
    AddressError,
    MessageStateError,
    add_ack,
    resolve_handle,
    send_message,
)
from bridge.capacity import (
    assess_leads,
    convert_load_to_day_line,
    convert_loads_to_dry_run,
    convert_loads_to_team_line,
    map_domains_to_warnings,
    park_room,
    read_owner_presence,
    record_owner_off,
    record_owner_on,
)
from bridge.cli_asks import add_ask_commands, map_local_time_arg_to_ms
from bridge.cli_board import map_wake_to_lines, nudge_recipients
from bridge.colleague_comments import (
    list_open_calls,
    map_arg_to_comment_key,
    map_entry_to_line,
    record_draft,
    record_small_call,
)
from bridge.config import Config, Remote, db_path, load_config, state_dir
from bridge.context_watch import map_screen_to_model_pct
from bridge.db import connect, now_ms, transaction
from bridge.doc_screen import screen_doc_for_warnings
from bridge.dream import (
    FAILED,
    OK,
    NightRun,
    new_night_id,
    read_night_run,
    resolve_dreamers,
    run_dream,
    runs_path,
    spawn_night,
)
from bridge.errors import UsageError
from bridge.feed import answer_post, spawn_typed_delivery
from bridge.host_workers import (
    HOST_KIND_PREFIX,
    HostWorker,
    append_conversation,
    build_host_brief_note,
    build_host_cleanup_command,
    build_tab_command,
    delete_record,
    find_mapped_root,
    find_record_by_tab,
    find_remote,
    hash_token,
    is_tab_loop_running,
    list_records,
    make_worker_name,
    map_cwd_to_host_path,
    new_token,
    read_gbridge_source,
    read_local_model_flags,
    read_record,
    read_session_state,
    run_on_host,
    write_record,
)
from bridge.identity import current_party, ensure_current_instance
from bridge.inject import measure_chars
from bridge.install import list_guard_globs
from bridge.leads import (
    NOTES_INJECT_MAX_CHARS,
    REVIEW_DOC_SPEC,
    Lead,
    LeadError,
    append_handover,
    assistant_dir,
    convert_size_to_notes_warning,
    enroll_lead,
    find_lead_on_surface,
    finish_lead_work,
    get_lead,
    is_assistant_surface,
    is_conversation_locked,
    is_surface_locked,
    lead_dir,
    list_internal_names,
    list_lead_work,
    list_leads,
    map_names_to_brief_rules,
    read_handovers,
    read_notes,
    read_tab_context,
    restore_tab_after_retirement,
    retire_lead,
    set_lead_domains,
    set_lead_label,
    set_lead_work,
    sync_lead_terminal,
    write_notes,
)
from bridge.linear_cli import add_linear_commands
from bridge.refs import map_text_to_ref
from bridge.registry import is_english_name, list_sessions, reconcile
from bridge.review_doc_check import find_doc_warnings, read_visible_top
from bridge.review_post import (
    ReviewError,
    map_arg_to_ref,
    record_hold,
    record_published_elsewhere,
    record_sentence,
    record_unhold,
    reopen_unsent_post,
)
from bridge.review_worktree import DOC_MIN_TERMS, REVIEW_LARGE_LOC, DocError, count_doc_terms, register_doc
from bridge.reviews import clear_approval_waiver, find_owner, map_db_to_reviews, record_approval_waiver
from bridge.start_context import CONSTITUTION_PATH
from bridge.tabinput import is_tab_selected, run_agterm, type_when_idle
from bridge.watcher import find_conversation_tab, map_tree_to_parent, read_screen_tail, read_tree
from bridge.workers import (
    WORKER_CAP,
    ChildTab,
    ClosedWorker,
    HostResume,
    check_background_work,
    check_host_work,
    count_busy_agents,
    find_closed_worker,
    find_conversation_runs,
    find_mid_turn,
    find_reopened_tabs,
    find_tab_sessions,
    find_undelivered_reports,
    forget_closed_workers,
    hold_reopen_lock,
    is_host_resume,
    is_lead_of_closed,
    is_reopen_queued,
    list_closed_workers,
    list_live_workers,
    map_host_tab_to_closed_records,
    map_resume_command_to_cwd,
    map_resume_command_to_host_resume,
    map_tab_to_closed_records,
    map_tree_to_child_tab,
    map_tree_to_tab,
    map_workers_to_cap_refusal,
    mark_reopened,
    queue_worker_launch,
    record_closed_workers,
    start_worker_tab,
    unmark_reopened,
)

WAKE_WAIT_S = 120
MODEL_SWITCH_WAIT_S = 20
WORKER_START_WAIT_S = 45
BY_HAND = "by hand: creates a tab with no lead:"
HOST_WORKER_START_WAIT_S = 120
# No Claude in Chrome: it drives the owner's own browser, and their screen is often shared.
WORKER_CLAUDE_FLAGS = "--dangerously-skip-permissions --no-chrome"
# Playwright's MCP opens a visible browser window, so workers go without it.
WORKER_DISABLED_PLUGINS = ("playwright@claude-plugins-official",)
WORKER_BROWSER_RULE = (
    "No browser use (Claude in Chrome, Playwright) unless the task says so, and then only in a hidden tab group: "
    "{owner}'s screen is often shared. Linear and the other claude.ai connectors are your tools."
)
# Without a terminal on stdin, codex exec waits for more input forever: two reviews hung for hours at 0% CPU.
WORKER_CODEX_RULE = (
    "Run codex exec with </dev/null and under a timeout (timeout 900 codex exec ... </dev/null); "
    "without them it waits on stdin forever."
)


def main(argv: list[str] | None = None) -> int:
    config = load_config()
    args = parse_args(sys.argv[1:] if argv is None else argv, config)
    conn = connect(db_path())
    try:
        return int(args.func(args, conn, config))
    except (UsageError, LeadError, ReviewError, DocError, PriorityError, AskError) as exc:
        print(f"guild: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()


def parse_args(argv: list[str], config: Config | None = None) -> argparse.Namespace:
    # `guild worker` takes a free task name, so these two words cannot be argparse subcommands of it.
    if argv[:1] == ["worker"] and argv[1:2] in (["close"], ["closed"], ["reopen"], ["attach"]):
        return build_worker_lifecycle_parser().parse_args(argv[1:])
    return build_parser(config or Config()).parse_args(argv)


def build_worker_lifecycle_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="guild worker")
    sub = parser.add_subparsers(dest="worker_command", required=True)
    p = sub.add_parser("close", help="close your own nested tab and keep what resumes it")
    p.add_argument("tab", help="the tab's name or agterm id")
    p.add_argument("--force", action="store_true", help="close even if it is mid-turn or has a live background run")
    p.set_defaults(func=cmd_worker_close)
    p = sub.add_parser("closed", help="closed workers, newest first, with the command that reopens each")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_worker_closed)
    p = sub.add_parser("reopen", help="resume a closed worker in a tab nested under yours (its lead or the assistant)")
    p.add_argument("worker", help="its id from guild worker closed, or its tab name")
    p.set_defaults(func=cmd_worker_reopen)
    p = sub.add_parser("attach", help="a host worker whose tab was closed: open it again under yours")
    p.add_argument("worker", nargs="?", help="its g-... name; none lists host workers without a tab")
    p.set_defaults(func=cmd_worker_attach)
    return parser


def build_parser(config: Config) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="guild", description="Team leads on top of bridge")
    sub = parser.add_subparsers(dest="command", required=True)
    add_ask_commands(sub, config.owner_name)
    add_linear_commands(sub)

    p = sub.add_parser("leads", help="list team leads")
    p.add_argument("--json", action="store_true")
    p.add_argument("--all", action="store_true", help="include retired leads")
    p.set_defaults(func=cmd_leads)

    lead = sub.add_parser("lead", help="enroll, sync or retire a team lead")
    lead_sub = lead.add_subparsers(dest="lead_command", required=True)
    p = lead_sub.add_parser("add", help="make a tab a team lead: name, tint, a place in the TEAM workspace")
    p.add_argument("name", help="one capitalised word, e.g. Ada")
    p.add_argument("--tab", required=True, help="handle of the tab that becomes the lead")
    p.add_argument("--scope", required=True)
    p.add_argument("--ref", action="append", default=[], help="Linear project or issue; repeat")
    p.add_argument("--label", required=True, help="short project label; the tab is named '<Name> · <label>'")
    p.add_argument("--alias", help="spelling for dictation, e.g. Sami")
    p.add_argument("--tint")
    p.set_defaults(func=cmd_lead_add)
    p = lead_sub.add_parser("label", help="change a lead's project label and rename its tab")
    p.add_argument("name")
    p.add_argument("label")
    p.set_defaults(func=cmd_lead_label)
    p = lead_sub.add_parser("sync", help="apply a lead's tab name, tint and place in TEAM again")
    p.add_argument("name")
    p.set_defaults(func=cmd_lead_sync)
    p = lead_sub.add_parser("retire", help="undo enrollment; the tab goes back, the name is never reused")
    p.add_argument("name")
    p.add_argument("--force", action="store_true", help="retire even if the tab could not be restored")
    p.set_defaults(func=cmd_lead_retire)
    p = lead_sub.add_parser(
        "domains", help="the projects a lead takes new work from: priority project names or Linear project refs"
    )
    p.add_argument("name")
    p.add_argument("domains", nargs="*", help="none shows them; given ones replace them (the assistant or the lead)")
    p.add_argument("--clear", action="store_true", help="remove every domain")
    p.set_defaults(func=cmd_lead_domains)
    p = lead_sub.add_parser("park", help="no 'has room' notices for a lead left idle on purpose, until a date")
    p.add_argument("name")
    parked = p.add_mutually_exclusive_group(required=True)
    parked.add_argument("--until", metavar="YYYY-MM-DD[THH:MM]", help="local time")
    parked.add_argument("--clear", action="store_true", help="notices again")
    p.set_defaults(func=cmd_lead_park)
    p = lead_sub.add_parser("wake", help="type a first prompt into a lead's idle tab, so its role arrives")
    p.add_argument("name")
    p.set_defaults(func=cmd_lead_wake)
    p = lead_sub.add_parser(
        "model", help="type /model <model> into lead tabs once each is idle with an empty box; locked tabs are skipped"
    )
    p.add_argument("model", help="a model id or alias, e.g. claude-opus-5-5")
    p.add_argument("names", nargs="*", help="lead names or the assistant's handle")
    p.add_argument("--all", action="store_true", help="every active lead and the assistant")
    p.set_defaults(func=cmd_lead_model)

    p = sub.add_parser(
        "worker",
        help="start a task in its own tab nested under yours, and check that it started; "
        "`guild worker close <tab>` closes one, `guild worker closed` lists them, `guild worker reopen <id>` "
        "resumes one",
    )
    p.add_argument("name", help="the task, in English; the tab's name")
    p.add_argument("--prompt", required=True, help="the brief; - reads stdin")
    p.add_argument("--cwd", help="default: this directory")
    p.add_argument("--local", action="store_true", help="run Claude on this Mac even when a worker host is configured")
    p.set_defaults(func=cmd_worker)

    p = sub.add_parser("answer", help="relay the user's answer to a post in the feed (the assistant only)")
    p.add_argument("id", type=int)
    p.add_argument("text")
    p.set_defaults(func=cmd_answer)

    comment = sub.add_parser("comment", help="your call on a colleague's comment on our PR; the page shows it")
    comment_sub = comment.add_subparsers(dest="comment_command", required=True)
    p = comment_sub.add_parser("small", help="a small one (typo, rename, factual question) you handled")
    p.add_argument("comment", help="the comment's link or id")
    p.add_argument("did", help="what you did")
    p.set_defaults(func=cmd_comment_small)
    p = comment_sub.add_parser(
        "draft", help="a substantive one: your draft reply and plan go to the owner, who chooses; bridge posts after"
    )
    p.add_argument("comment", help="the comment's link or id")
    p.add_argument("--reply-file", required=True, help="the draft reply, as it would go on GitHub")
    p.add_argument("--plan", required=True, help="what you would do about it")
    p.set_defaults(func=cmd_comment_draft)
    p = comment_sub.add_parser("list", help="colleague comments still waiting on you or on the owner")
    p.add_argument("--all", action="store_true", help="every lead's, not only yours")
    p.set_defaults(func=cmd_comment_list)

    review = sub.add_parser("review", help="the PR review queue on the page")
    review_sub = review.add_subparsers(dest="review_command", required=True)
    p = review_sub.add_parser("list", help="what the queue shows")
    p.set_defaults(func=cmd_review_list)
    p = review_sub.add_parser("sentence", help="the one sentence of project context for a PR's Slack post")
    p.add_argument("pr", help="URL, repo#n or a number")
    p.add_argument("text")
    p.set_defaults(func=cmd_review_sentence)
    p = review_sub.add_parser("doc", help="register a large PR's HTML document; the page serves this copy")
    p.add_argument("pr")
    p.add_argument("file")
    p.set_defaults(func=cmd_review_doc)
    p = review_sub.add_parser("published", help="record a Slack post made by hand, so it is never posted again")
    p.add_argument("pr")
    p.add_argument("link")
    p.set_defaults(func=cmd_review_published)
    p = review_sub.add_parser("ready", help="retired: mark the draft ready on GitHub with gh pr ready instead")
    p.add_argument("pr")
    p.add_argument("--undo", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_review_ready)
    p = review_sub.add_parser(
        "hold", help=f"keep a PR for {config.owner_name}'s own review, never ready to merge (its lead or the assistant)"
    )
    p.add_argument("pr")
    p.add_argument("why")
    p.set_defaults(func=cmd_review_hold)
    p = review_sub.add_parser("unhold", help="lift a hold before the review ends it (its lead or the assistant)")
    p.add_argument("pr")
    p.set_defaults(func=cmd_review_unhold)
    p = review_sub.add_parser(
        "waive",
        help=f"merge without a GitHub approval because {config.owner_name} said so in an answered decision "
        "(its lead or the assistant)",
    )
    p.add_argument("pr")
    p.add_argument("--decision", type=int, required=True, help="the answered decision or blocked post on the page")
    p.set_defaults(func=cmd_review_waive)
    p = review_sub.add_parser("unwaive", help="take the waiver back: the PR needs a GitHub approval again")
    p.add_argument("pr")
    p.set_defaults(func=cmd_review_unwaive)
    p = review_sub.add_parser(
        "repost",
        help="send again a post whose sender stopped (look in the channel first) or that a dry run only logged",
    )
    p.add_argument("pr")
    p.set_defaults(func=cmd_review_repost)

    p = sub.add_parser("day", help=f"{config.owner_name} is on again; how loaded each lead is and its next work")
    p.set_defaults(func=cmd_day)

    p = sub.add_parser("dream", help="the night's dreams, in parallel and detached; default: config dream_leads")
    p.add_argument("leads", nargs="*", help="lead names, the assistant's handle, or all; none means config dream_leads")
    waiting = p.add_mutually_exclusive_group()
    waiting.add_argument("--wait", action="store_true", help="dream here, one after another, and print each outcome")
    waiting.add_argument(
        "--wait-all", action="store_true", help="dream in parallel and return when all are done or cut at the limit"
    )
    p.set_defaults(func=cmd_dream)

    p = sub.add_parser(
        "capacity", help="a dry run of the capacity signal: who has room for planned work now, why, what to suggest"
    )
    p.set_defaults(func=cmd_capacity)

    p = sub.add_parser("notes", help="a lead's or the assistant's notes that outlive conversations")
    p.add_argument("lead", nargs="?", help="default: your own")
    p.add_argument("--write", action="store_true", help="replace your notes from stdin")
    p.add_argument("--handovers", action="store_true", help="show handovers from other tabs")
    p.set_defaults(func=cmd_notes)

    work = sub.add_parser("work", help="the main chunks of work a lead declares; the page shows these")
    work_sub = work.add_subparsers(dest="work_command", required=True)
    p = work_sub.add_parser("set", help="add a chunk, or update it by name; given refs replace its refs")
    p.add_argument("name", help="a few plain words, e.g. 'Nightly export worker'")
    p.add_argument("refs", nargs="*", help="PROJ-34, Acme-Org/monorepo#45, #45 (your repo), or a URL")
    p.add_argument("--lead", help="default: your own")
    p.set_defaults(func=cmd_work_set)
    p = work_sub.add_parser("done", help="a chunk ended; it leaves the page")
    p.add_argument("name")
    p.add_argument("--lead", help="default: your own")
    p.set_defaults(func=cmd_work_done)
    p = work_sub.add_parser("list", help="the open chunks")
    p.add_argument("--lead", help="default: your own")
    p.set_defaults(func=cmd_work_list)

    p = sub.add_parser("ack", help="a lead's one line on a board post; shown faint, delivered to nobody")
    p.add_argument("id", type=int)
    p.add_argument("text")
    p.set_defaults(func=cmd_ack)

    priority = sub.add_parser("priority", help="the ordered projects behind the page's PR numbers")
    priority_sub = priority.add_subparsers(dest="priority_command", required=True)
    p = priority_sub.add_parser("set", help="replace the list, first project first (the assistant only)")
    p.add_argument("projects", nargs="+", metavar="name=PROJ-1,PROJ-2")
    p.set_defaults(func=cmd_priority_set)
    p = priority_sub.add_parser("show", help="the list as the page ranks by it")
    p.set_defaults(func=cmd_priority_show)

    p = sub.add_parser("urgent", help="questions marked urgent, newest first, to audit against the criteria")
    p.add_argument("--days", type=int, default=14)
    p.set_defaults(func=cmd_urgent)

    p = sub.add_parser("handover", help="hand your work to a lead, from stdin")
    p.add_argument("lead")
    p.set_defaults(func=cmd_handover)
    return parser


def cmd_leads(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    now = now_ms()
    reconcile(conn, config, now)
    sessions = {s.surface: s for s in list_sessions(conn, now)}
    rows: list[dict[str, object]] = []
    for lead in list_leads(conn, include_retired=bool(args.all)):
        session = sessions.get(lead.surface)
        locked = is_surface_locked(conn, config, lead.surface)
        rows.append(
            {
                **asdict(lead),
                "locked": locked,
                "status": "locked"
                if locked
                else ("retired" if lead.retired_at else (session.status if session else "offline")),
                "focus": None if locked or session is None else session.focus,
            }
        )
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print('no team leads yet: guild lead add <Name> --tab <handle> --scope "…"')
    for row in rows:
        print(f"{row['display_name']:<10} {row['status']:<8} {row['scope']}")
        if row["focus"]:
            print(f"{'':<10} {'':<8} {row['focus']}")
    return 0


def cmd_lead_add(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    now = now_ms()
    reconcile(conn, config, now)
    tab = conn.execute(
        """
        SELECT agterm_session FROM surfaces WHERE handle = ?1
           OR (NOT EXISTS (SELECT 1 FROM surfaces WHERE handle = ?1)
               AND surface = (SELECT surface FROM handle_aliases WHERE handle = ?1))
        """,
        (str(args.tab).lower(),),
    ).fetchone()
    previous_context = read_tab_context(config, str(tab[0])) if tab and tab[0] else None
    with transaction(conn):
        lead = enroll_lead(
            conn,
            config,
            str(args.name),
            str(args.tab),
            str(args.scope),
            list(args.ref),
            args.alias,
            args.tint,
            previous_context,
            now,
            args.label,
        )
    nudge_recipients([resolve_handle(conn, lead.name)])
    print(f"{lead.display_name} leads: {lead.scope}")
    status = _sync_terminal(conn, config, lead)
    print(f"If the tab is idle, its role arrives with its next prompt: guild lead wake {lead.name}")
    return status


def cmd_lead_label(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    lead = _active_lead(conn, config, str(args.name))
    with transaction(conn):
        lead = set_lead_label(conn, lead.name, str(args.label))
    print(f"tab: {lead.tab_title}")
    return _sync_terminal(conn, config, lead)


def cmd_lead_sync(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    lead = _active_lead(conn, config, str(args.name))
    return _sync_terminal(conn, config, lead)


def cmd_lead_retire(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    lead = get_lead(conn, str(args.name))
    if lead.retired_at is not None:
        raise LeadError(f"{lead.display_name} is already retired")
    # A locked tab is left alone; a failed restore keeps the lead, so running this again retries it.
    if lead.surface and not is_surface_locked(conn, config, lead.surface):
        problems = restore_tab_after_retirement(config, lead, _agterm_session_of(conn, lead))
        for problem in problems:
            print(f"warning: {problem}", file=sys.stderr)
        if problems and not args.force:
            print("not retired; run it again, or add --force", file=sys.stderr)
            return 1
    with transaction(conn):
        retire_lead(conn, lead.name, now_ms())
    reconcile(conn, config, now_ms())
    print(f"{lead.display_name} retired; the name will not be reused")
    return 0


def cmd_lead_domains(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    lead = _active_lead(conn, config, str(args.name))
    domains = [str(d) for d in args.domains]
    if domains or args.clear:
        if domains and args.clear:
            raise UsageError("give domains or --clear, not both")
        me = current_party(conn)
        mine = find_lead_on_surface(conn, _my_surface(conn))
        if me.handle != config.assistant_handle and (mine is None or mine.name != lead.name):
            raise UsageError(f"only {config.assistant_handle} or {lead.display_name} sets its domains")
        with transaction(conn):
            lead = set_lead_domains(conn, lead.name, domains)
        for warning in map_domains_to_warnings(list(lead.domains), load_projects(conn)):
            print(f"warning: {warning}", file=sys.stderr)
    print(f"{lead.display_name}: {', '.join(lead.domains) or 'no domains'}")
    return 0


def cmd_lead_park(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    lead = _active_lead(conn, config, str(args.name))
    me = current_party(conn)
    mine = find_lead_on_surface(conn, _my_surface(conn))
    if me.handle != config.assistant_handle and (mine is None or mine.name != lead.name):
        raise UsageError(f"only {config.assistant_handle} or {lead.display_name} parks it")
    until = None if args.clear else map_local_time_arg_to_ms(str(args.until))
    with transaction(conn):
        park_room(conn, lead.name, until)
    when = (
        "notices again" if until is None else time.strftime("parked until %Y-%m-%d %H:%M", time.localtime(until / 1000))
    )
    print(f"{lead.display_name}: 'has room' {when}")
    return 0


def cmd_capacity(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    now = now_ms()
    loads = assess_leads(conn, config, now, read_tree(config))
    print("dry run: nothing is sent; bridge serve sends these every 5 minutes")
    for line in convert_loads_to_dry_run(conn, config, loads, read_owner_presence(conn, config, now), now):
        print(line)
    return 0


def cmd_day(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    with transaction(conn):
        record_owner_on(conn, "guild day", now_ms())
    loads = assess_leads(conn, config, now_ms(), read_tree(config))
    print(convert_loads_to_team_line(loads))
    for load in loads:
        print(convert_load_to_day_line(load))
    return 0


def cmd_lead_wake(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    lead = _active_lead(conn, config, str(args.name))
    text = f"Guild: you are {lead.display_name}, a team lead now. Your role is in the context above; start with it."

    def still_a_lead() -> bool:
        current = get_lead(conn, lead.name)
        return current.retired_at is None and not is_surface_locked(conn, config, current.surface)

    session = _agterm_session_of(conn, lead)

    def may_probe_box() -> bool:
        # A fresh tab shows Claude Code's grey suggestion, which reads as a draft until one probe key tells them apart.
        status = conn.execute(
            "SELECT status FROM instances WHERE surface = ? AND ended_at IS NULL ORDER BY first_seen_at DESC LIMIT 1",
            (lead.surface,),
        ).fetchone()
        return status is not None and status["status"] == "idle" and not is_tab_selected(config, session)

    result = type_when_idle(
        config, session, str(lead.surface), text, still_a_lead, wait_s=WAKE_WAIT_S, may_probe_box=may_probe_box
    )
    print(f"{lead.display_name}: {result}")
    return 0 if result == "typed" else 1


_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\[\]-]{0,63}")


def cmd_lead_model(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    model = str(args.model)
    if not _MODEL_RE.fullmatch(model):
        raise UsageError(f"{model!r} is not a model id or alias")
    names = [str(n) for n in args.names]
    if args.all:
        names = [*dict.fromkeys([*(lead.name for lead in list_leads(conn)), config.assistant_handle, *names])]
    if not names:
        raise UsageError("name the tabs, or --all")
    failed = 0
    for name in names:
        row = conn.execute("SELECT surface, agterm_session FROM surfaces WHERE handle = ?", (name,)).fetchone()
        if row is None or not row["agterm_session"]:
            print(f"{name}: no agterm tab")
            failed += 1
            continue
        surface, session = str(row["surface"]), str(row["agterm_session"])
        if is_surface_locked(conn, config, surface):
            print(f"{name}: locked, skipped")
            continue

        def unlocked(surface: str = surface) -> bool:
            return not is_surface_locked(conn, config, surface)

        def may_probe_box(surface: str = surface, session: str = session) -> bool:
            status = conn.execute(
                "SELECT status FROM instances WHERE surface = ? AND ended_at IS NULL ORDER BY first_seen_at DESC "
                "LIMIT 1",
                (surface,),
            ).fetchone()
            return status is not None and status["status"] == "idle" and not is_tab_selected(config, session)

        if is_on_model(read_screen_tail(config, session), model):
            print(f"{name}: already on it")
            continue
        result = type_when_idle(
            config, session, surface, f"/model {model}", unlocked, wait_s=WAKE_WAIT_S, may_probe_box=may_probe_box
        )
        if result in ("typed", "stuck"):
            result = confirm_model_switch(config, session, model) or result
        if result in ("typed", "stuck"):
            # A busy tab takes /model as its next prompt: it switches when its turn ends, not within this wait.
            result = "queued: it switches when its current turn ends; its footer shows the model"
        print(f"{name}: {result}")
        failed += result != "switched" and not result.startswith("queued")
    return 0 if not failed else 1


def is_on_model(screen: str, model: str) -> bool:
    """The status line under the box names the model as Claude Code shows it: claude-opus-5-5 reads "Opus 5.5"."""
    found = map_screen_to_model_pct(screen)
    return found is not None and convert_model_to_label(model) in found[0].lower()


def convert_model_to_label(model: str) -> str:
    parts = model.lower().removeprefix("claude-").split("[")[0].split("-")
    words = [p for p in parts if not p.isdigit()]
    version = ".".join(p for p in parts if p.isdigit())
    return " ".join([*words, version]) if version else " ".join(words)


_SWITCH_DIALOG_RE = re.compile(r"\u276f\s*1\.\s*Yes, switch to ")
_SWITCHED_RE = re.compile(r"Set model to |Kept model as ")


def confirm_model_switch(config: Config, session: str, model: str = "") -> str | None:
    """A conversation cached for one model asks before re-reading it on another; the switch is what was asked for."""
    deadline = time.monotonic() + MODEL_SWITCH_WAIT_S
    confirmed = False
    while time.monotonic() < deadline:
        screen = read_screen_tail(config, session)
        if _SWITCHED_RE.search(screen) or (model and is_on_model(screen, model)):
            return "switched"
        if not confirmed and _SWITCH_DIALOG_RE.search(screen):
            run_agterm(config, ["session", "type", "--target", session, "\r"])
            confirmed = True
        time.sleep(1)
    return None


def cmd_worker(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    me = current_party(conn)
    parent = os.environ.get("AGTERM_SESSION_ID")
    if not parent:
        raise UsageError("guild worker runs from an agterm tab")
    name = " ".join(str(args.name).split())
    if not name or not is_english_name(name):
        raise UsageError("a worker's tab name is in English")
    prompt = sys.stdin.read() if args.prompt == "-" else str(args.prompt)
    if not prompt.strip():
        raise UsageError("a worker needs a brief")
    # Without a tree agterm is down, and creating the tab fails on its own.
    tree = read_tree(config)
    live = list_live_workers(conn, tree, parent) if tree else []
    if len(live) >= WORKER_CAP:
        raise UsageError(map_workers_to_cap_refusal(live, WORKER_CAP, now_ms()))
    cwd = Path(os.path.abspath(os.path.expanduser(args.cwd or os.getcwd())))
    if config.worker_host and not args.local:
        return _start_host_worker(conn, config, me.handle, parent, name, prompt, cwd)
    brief = state_dir() / "worker-briefs" / f"{uuid.uuid4().hex}.md"
    brief.parent.mkdir(parents=True, exist_ok=True)
    brief.write_text(build_worker_brief(conn, config, prompt, f"bridge send {me.handle}"))
    first_prompt = f"Your brief is in {brief}. Read it and do it."
    command = "zsh -lc " + shlex.quote(build_worker_claude_command(cwd, first_prompt, config.worker_model))
    busy = count_busy_agents(conn)
    if config.laptop_agent_cap and busy >= config.laptop_agent_cap:
        launch = {"parent": parent, "name": name, "cwd": str(cwd), "command": command, "requested_by": me.handle}
        place = queue_worker_launch(launch)
        print(
            f"queued {name!r} (place {place}): {busy} agents are working, the laptop cap is "
            f"{config.laptop_agent_cap}. bridge starts it when a slot frees and tells you."
        )
        return 0
    session_id = start_worker_tab(conn, config, parent, name, cwd, command)
    print(f"tab {session_id}: {name}")
    return _await_worker_start(config, session_id)


def build_worker_brief(
    conn: sqlite3.Connection, config: Config, prompt: str, report_command: str, where: str = ""
) -> str:
    report = f'Report to the lead that started you when done or stuck: {report_command} "…"'
    rules = map_names_to_brief_rules(list_internal_names(conn, config), config)
    constitution = CONSTITUTION_PATH.read_text(encoding="utf-8").strip()
    place = f"{where}\n" if where else ""
    return (
        f"{constitution}\n\n{place}# Your task\n\n{prompt.strip()}\n\n{rules}\n"
        f"{WORKER_BROWSER_RULE.format(owner=config.owner_name)}\n{WORKER_CODEX_RULE}\n\n{report}\n"
    )


def _start_host_worker(
    conn: sqlite3.Connection, config: Config, lead: str, parent: str, name: str, prompt: str, cwd: Path
) -> int:
    """Claude runs on the host in tmux; the tab here only reattaches, so closing it or a lost link never ends it."""
    remote = find_remote(config, config.worker_host)
    remote_cwd = map_cwd_to_host_path(cwd, dict(config.worker_path_map))
    if remote_cwd is None:
        raise UsageError(
            f"{cwd} has no worker_path_map entry for {remote.name}: add the checkout to worker_path_map in the bridge "
            "config, or run this worker on the Mac with --local"
        )
    worker = make_worker_name(name)
    token = new_token()
    mapped_root = find_mapped_root(remote_cwd, dict(config.worker_path_map)) or remote_cwd
    note = build_host_brief_note(config, remote.name, worker, lead, mapped_root)
    brief = build_worker_brief(conn, config, prompt, f"gbridge send {lead}", note)
    record = HostWorker(
        worker=worker, name=name, host=remote.name, ssh=remote.ssh, cwd=str(cwd), remote_cwd=remote_cwd, remote_dir="",
        lead=lead, parent=parent, token_sha256=hash_token(token), created_at=now_ms(),
        model_flags=read_local_model_flags(model_override=config.worker_model),
    )  # fmt: skip
    session_id = _launch_host_worker(
        conn, config, remote, record, {"token": token, "cwd": remote_cwd, "brief": brief, "mapped_root": mapped_root}
    )
    print(f"tab {session_id}: {name} (Claude on {remote.name}, tmux -L gbridge session {worker})")
    return _await_host_worker_start(conn, config, session_id)


def _reopen_host_worker(
    conn: sqlite3.Connection,
    config: Config,
    lead: str,
    parent: str,
    closed: ClosedWorker,
    resume: HostResume,
    remote: Remote,
) -> str:
    """As a fresh one, gbridge and all, under its old name: its worktree path is where Claude finds the conversation,
    and a close then removes that worktree as for any worker."""
    token = new_token()
    record = HostWorker(
        worker=resume.worker, name=closed.tab_name, host=remote.name, ssh=remote.ssh, cwd=str(Path.home()),
        remote_cwd=resume.remote_cwd, remote_dir="", lead=lead, parent=parent, token_sha256=hash_token(token),
        created_at=now_ms(),
        model_flags=resume.model_flags or read_local_model_flags(model_override=config.worker_model),
    )  # fmt: skip
    mapped_root = find_mapped_root(resume.remote_checkout, dict(config.worker_path_map)) or resume.remote_checkout
    payload = {
        "token": token, "cwd": resume.remote_cwd, "checkout": resume.remote_checkout, "mapped_root": mapped_root,
        "resume": closed.conversation_id,
    }  # fmt: skip
    return _launch_host_worker(conn, config, remote, record, payload)


def _launch_host_worker(
    conn: sqlite3.Connection, config: Config, remote: Remote, record: HostWorker, payload: dict[str, Any]
) -> str:
    # Written first: the worker's first hook may come as soon as its Claude starts.
    write_record(record)
    if payload.get("resume"):
        # bind_conversation gives a worker only conversations bridge has never seen; this one was its own.
        append_conversation(record.worker, str(payload["resume"]))
    launched = run_on_host(
        remote,
        {
            "op": "launch", "worker": record.worker, "gbridge": read_gbridge_source(),
            "claude_flags": f"{WORKER_CLAUDE_FLAGS} {record.model_flags}",
            "disabled_plugins": [*WORKER_DISABLED_PLUGINS, *config.host_worker_disabled_plugins],
            "globs": list_guard_globs(), **payload,
        },
    )  # fmt: skip
    if not launched.get("ok"):
        if launched.get("unknown"):
            _abandon_host_worker(remote, record, str(launched.get("error")))
        delete_record(record.worker)
        raise UsageError(f"could not start {record.worker} on {remote.name}: {launched.get('error')}")
    record = replace(
        record,
        remote_dir=str(launched["dir"]),
        remote_cwd=str(launched.get("cwd") or record.remote_cwd),
        remote_checkout=str(launched.get("checkout") or ""),
        remote_worktree=str(launched.get("worktree") or ""),
    )
    write_record(record)
    if launched.get("warning"):
        print(f"on {remote.name}: {launched['warning']}")
    try:
        return start_worker_tab(
            conn, config, record.parent, record.name, Path(record.cwd), build_tab_command(record.worker)
        )
    except UsageError as exc:
        _abandon_host_worker(remote, record, str(exc))
        delete_record(record.worker)
        raise


def _abandon_host_worker(remote: Remote, record: HostWorker, why: str) -> None:
    """Its record goes only once the host confirms nothing is left: it alone reaches and authenticates the worker."""
    removed = run_on_host(remote, {"op": "remove", "worker": record.worker})
    if not removed.get("ok"):
        raise UsageError(
            f"{why}; and {record.worker} may still run on {remote.name} ({removed.get('error')}). Its record is kept: "
            f"guild worker attach {record.worker} opens it, guild worker close then ends it."
        )


def _await_host_worker_start(conn: sqlite3.Connection, config: Config, session_id: str, resumed: bool = False) -> int:
    """agterm sees only ssh in the tab, so the worker's own hooks say when it works on the brief; a resumed one waits
    idle for a prompt, so its first hook is the sign."""
    deadline = time.monotonic() + HOST_WORKER_START_WAIT_S
    while time.monotonic() < deadline:
        row = conn.execute(
            """
            SELECT i.status FROM instances i JOIN surfaces s ON s.surface = i.surface
            WHERE s.agterm_session = ? AND i.ended_at IS NULL AND i.kind LIKE 'host:%'
            ORDER BY i.first_seen_at DESC LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        if row is not None and (resumed or row["status"] == "active"):
            print("resumed" if resumed else "started")
            return 0
        time.sleep(2)
    word = "resumed" if resumed else "started"
    print(f"NOT {word}. Look: agtermctl session text --target {session_id}\n{read_screen_tail(config, session_id)}")
    return 1


def build_worker_claude_command(cwd: Path, first_prompt: str, model: str = "") -> str:
    settings = map_cwd_to_worker_settings(cwd)
    flags = f"{WORKER_CLAUDE_FLAGS} --model {shlex.quote(model)}" if model else WORKER_CLAUDE_FLAGS
    return f"claude {flags} --settings {shlex.quote(settings)} {shlex.quote(first_prompt)}"


def map_cwd_to_worker_settings(cwd: Path) -> str:
    """Rejecting the project's .mcp.json servers by name skips their approval dialog and keeps claude.ai connectors."""
    settings: dict[str, Any] = {"enabledPlugins": dict.fromkeys(WORKER_DISABLED_PLUGINS, False)}
    if servers := list_project_mcp_servers(cwd):
        settings["disabledMcpjsonServers"] = servers
    return json.dumps(settings, separators=(",", ":"))


def list_project_mcp_servers(cwd: Path) -> list[str]:
    """Claude reads .mcp.json in the start directory and every directory above it, the symlink-resolved path too."""
    names: set[str] = set()
    directories = dict.fromkeys([cwd, *cwd.parents, cwd.resolve(), *cwd.resolve().parents])
    for directory in directories:
        try:
            data = json.loads((directory / ".mcp.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        servers = cast(dict[str, Any], data).get("mcpServers") if isinstance(data, dict) else None
        if isinstance(servers, dict):
            names.update(str(name) for name in cast(dict[str, Any], servers))
    return sorted(names)


def _await_worker_start(config: Config, session_id: str) -> int:
    """It has started once it works on the brief; a dialog here would otherwise stay unseen."""
    deadline = time.monotonic() + WORKER_START_WAIT_S
    while time.monotonic() < deadline:
        tree = read_tree(config)
        tabs = [
            t for w in cast(list[dict[str, Any]], (tree or {}).get("workspaces", [])) for t in w.get("sessions", [])
        ]
        status = next((t.get("status") for t in tabs if t.get("id") == session_id), None)
        if status in ("active", "completed"):
            print("started")
            return 0
        if status == "blocked":
            break
        time.sleep(2)
    print(f"NOT started. Look: agtermctl session text --target {session_id}\n{read_screen_tail(config, session_id)}")
    return 1


def cmd_worker_close(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    parent = os.environ.get("AGTERM_SESSION_ID")
    if not parent:
        raise UsageError("guild worker close runs from an agterm tab")
    me = current_party(conn)
    if is_surface_locked(conn, config, _my_surface(conn)):
        raise UsageError("this tab is locked, and Guild ignores locked tabs")
    tab = _read_child_tab(conn, config, parent, str(args.tab))
    if config.hides(tab.name, tab.workspace_name):
        raise UsageError(f"{tab.name} is locked, and Guild ignores locked tabs")
    if any(find_lead_on_surface(conn, pane) for pane in tab.panes):
        raise UsageError(f"{tab.name} is a team lead: guild lead retire")
    if tab.children:
        raise UsageError(f"{tab.name} has nested tabs that would close with it: {', '.join(tab.children)}")
    sessions = find_tab_sessions(conn, tab)
    host = find_record_by_tab(tab.id)
    work = check_host_work(conn, tab, host) if host else check_background_work(conn, config, tab, sessions)
    for line in work.unknown:
        print(f"not known: {line}", file=sys.stderr)
    # Those checks can take many seconds, and a message may have started a turn meanwhile.
    tab = _read_child_tab(conn, config, parent, tab.id)
    sessions = find_tab_sessions(conn, tab)
    for line in find_undelivered_reports(conn, tab, me.conversation_id):
        print(f"warning: not delivered to you yet: {line}", file=sys.stderr)
    blockers = [*find_mid_turn(tab, sessions), *work.live]
    if blockers and not args.force:
        for line in blockers:
            print(f"refused: {line}", file=sys.stderr)
        print(f"{tab.name} stays open; --force closes it anyway", file=sys.stderr)
        return 1
    for line in blockers:
        print(f"forced past: {line}")
    records = map_host_tab_to_closed_records(host, sessions) if host else map_tab_to_closed_records(tab, sessions)
    mine = find_lead_on_surface(conn, _my_surface(conn))
    with transaction(conn):
        ids = record_closed_workers(conn, records, tab.name, me.handle, parent, mine.name if mine else None, now_ms())
    # Recorded first: a crash between the two leaves an extra line, never a closed tab nobody can resume.
    if run_agterm(config, ["session", "close", "--target", tab.id]) is None:
        # A timeout can lose the answer to a close that happened, and then the record is all that resumes the work.
        after = read_tree(config)
        if after is None:
            print(f"agterm did not answer; {tab.name} may or may not be closed, its record is kept", file=sys.stderr)
            return 1
        if map_tree_to_parent(after, tab.id) is not None:
            with transaction(conn):
                forget_closed_workers(conn, ids)
            print(f"agterm could not close {tab.name}; it stays open", file=sys.stderr)
            return 1
    print(f"closed {tab.name}")
    if host:
        removed = run_on_host(Remote(name=host.host, ssh=host.ssh), {"op": "remove", "worker": host.worker})
        if removed.get("ok"):
            delete_record(host.worker)
            print(f"ended tmux session {host.worker} on {host.host} and removed {host.remote_dir}")
            if removed.get("warning"):
                print(f"on {host.host}, {removed['warning']}")
            if removed.get("kept"):
                print(f"on {host.host}, {removed['kept']}")
            elif host.remote_worktree:
                print(f"removed its worktree {host.remote_worktree}")
        else:
            print(
                f"NOT cleaned up on {host.host}: {removed.get('error')}. Its Claude may still run there; end it with: "
                f"ssh {host.ssh} {shlex.quote(build_host_cleanup_command(host))}",
                file=sys.stderr,
            )
    for closed_id, record in zip(ids, records, strict=True):
        print(f"reopen: guild worker reopen {closed_id}")
        print(f"{BY_HAND} {record.resume_command}")
    if not records:
        print("no conversation was known in that tab, so there is nothing to resume")
    return 0


def _read_child_tab(conn: sqlite3.Connection, config: Config, parent: str, wanted: str) -> ChildTab:
    reconcile(conn, config, now_ms())
    tree = read_tree(config)
    if tree is None:
        raise UsageError("agterm is not reachable, so the tab cannot be checked")
    return map_tree_to_child_tab(tree, parent, wanted)


def cmd_worker_attach(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    parent = os.environ.get("AGTERM_SESSION_ID")
    if not parent:
        raise UsageError("guild worker attach runs from an agterm tab")
    detached = [r for r in list_records() if not is_tab_loop_running(r)]
    if not args.worker:
        for record in detached:
            print(f"{record.worker}  {record.name}  on {record.host}  (lead {record.lead})")
        if not detached:
            print("every host worker has its tab")
        return 0
    record = read_record(str(args.worker))
    if record is None:
        raise UsageError(f"no host worker {args.worker}")
    if record not in detached:
        raise UsageError(f"{record.worker} is attached in tab {record.tab}")
    state = read_session_state(record)
    if state == "gone":
        raise UsageError(f"{record.worker} has ended on {record.host}")
    session_id = start_worker_tab(
        conn, config, parent, record.name or record.worker, Path(record.cwd), build_tab_command(record.worker)
    )
    print(f"tab {session_id}: {record.name or record.worker} (attached to {record.worker} on {record.host})")
    return 0


def cmd_worker_closed(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    rows = list_closed_workers(conn, int(args.limit))
    if not rows:
        print("no closed workers")
    for row in rows:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(row["closed_at"]) / 1000))
        print(
            f"{stamp}  {row['tab_name']}  (lead {row['parent_handle']})\n  guild worker reopen {row['id']}\n"
            f"  {BY_HAND} {row['resume_command']}"
        )
    return 0


def cmd_worker_reopen(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    parent = os.environ.get("AGTERM_SESSION_ID")
    if not parent:
        raise UsageError("guild worker reopen runs from an agterm tab")
    me = current_party(conn)
    if is_surface_locked(conn, config, _my_surface(conn)):
        raise UsageError("this tab is locked, and Guild ignores locked tabs")
    closed = find_closed_worker(conn, str(args.worker))
    if me.handle != config.assistant_handle and not is_lead_of_closed(conn, closed, parent, me.conversation_id):
        raise UsageError(f"only its lead {closed.parent_handle} or {config.assistant_handle} reopens {closed.tab_name}")
    cwd = map_resume_command_to_cwd(closed.resume_command) or Path.home()
    command = "zsh -lc " + shlex.quote(closed.resume_command)
    with hold_reopen_lock():
        # Under the lock: two closed records may name one host worker, and a reopen writes its record.
        host = _find_host_resume(conn, config, closed) if is_host_resume(closed.resume_command) else None
        if is_reopen_queued(closed.conversation_id):
            raise UsageError(f"{closed.tab_name} is already queued to reopen")
        reconcile(conn, config, now_ms())
        tree = read_tree(config)
        if tree is None:
            raise UsageError("agterm is not reachable, so the worker cannot be reopened")
        for tab_id in find_reopened_tabs(conn, closed.conversation_id):
            if (tab := map_tree_to_tab(tree, tab_id)) is not None:
                raise UsageError(f"{closed.tab_name} is already reopened in tab {tab.name} ({tab.id})")
        strays = _find_stray_tabs(conn, tree, closed)
        live = list_live_workers(conn, tree, parent)
        if len(live) >= WORKER_CAP:
            raise UsageError(map_workers_to_cap_refusal(live, WORKER_CAP, now_ms()))
        for stray in strays:
            _close_stray_tab(conn, config, stray)
        busy = count_busy_agents(conn)
        if not host and config.laptop_agent_cap and busy >= config.laptop_agent_cap:
            launch = {
                "parent": parent, "name": closed.tab_name, "cwd": str(cwd), "command": command,
                "requested_by": me.handle, "reopens": closed.conversation_id,
            }  # fmt: skip
            place = queue_worker_launch(launch)
            print(
                f"queued reopening {closed.tab_name!r} (place {place}): {busy} agents are working, the laptop cap is "
                f"{config.laptop_agent_cap}. bridge starts it when a slot frees and tells you."
            )
            return 0
        if host:
            session_id = _reopen_host_worker(conn, config, me.handle, parent, closed, *host)
        else:
            session_id = start_worker_tab(conn, config, parent, closed.tab_name, cwd, command)
        with transaction(conn):
            mark_reopened(conn, closed.conversation_id, session_id, now_ms())
    if host:
        resume, remote = host
        print(f"tab {session_id}: {closed.tab_name} (Claude on {remote.name}, tmux -L gbridge session {resume.worker})")
        return _await_host_worker_start(conn, config, session_id, resumed=True)
    print(f"tab {session_id}: {closed.tab_name}")
    return _await_reopened(conn, config, session_id, closed)


def _find_host_resume(conn: sqlite3.Connection, config: Config, closed: ClosedWorker) -> tuple[HostResume, Remote]:
    resume = map_resume_command_to_host_resume(closed.resume_command, closed.conversation_id)
    if resume is None:
        raise UsageError(
            f"{closed.tab_name} ran in its host's shared checkout, before host workers had a worktree of their own, so "
            f"it cannot come back with gbridge. Resume it by hand, with no lead and no bridge messages: "
            f"{closed.resume_command}"
        )
    remote = next((r for r in config.remotes if resume.ssh in r.host_names()), None)
    if remote is None:
        raise UsageError(f"{resume.ssh} is not a [[remotes]] host in the bridge config, so {closed.tab_name} cannot "
                         "be reopened with gbridge")  # fmt: skip
    ran = conn.execute(
        "SELECT 1 FROM instances WHERE conversation_id = ? AND kind = ? LIMIT 1",
        (closed.conversation_id, f"{HOST_KIND_PREFIX}{remote.name}"),
    ).fetchone()
    if ran is None:
        # Its new token is given the conversation: only one that ran on that host may be, never a Mac session's.
        raise UsageError(f"{closed.tab_name}'s conversation never ran on {remote.name}, so it is not reopened there")
    if read_record(resume.worker) is not None:
        raise UsageError(
            f"{resume.worker} still has its host worker record: guild worker attach {resume.worker} opens it, "
            "guild worker close then ends it"
        )
    return resume, remote


def _find_stray_tabs(conn: sqlite3.Connection, tree: dict[str, object], closed: ClosedWorker) -> list[ChildTab]:
    """Tabs outside any tree that run the worker's conversation, to close; a run anywhere else refuses."""
    strays: list[ChildTab] = []
    for run in find_conversation_runs(conn, closed.conversation_id):
        tab = map_tree_to_tab(tree, run.agterm_session) if run.agterm_session else None
        if tab is None:
            raise UsageError(f"{closed.tab_name}'s conversation already runs outside agterm (pid {run.pid})")
        if tab.parent:
            raise UsageError(f"{closed.tab_name}'s conversation already runs in tab {tab.name} ({tab.id})")
        strays.append(tab)
    return strays


def _close_stray_tab(conn: sqlite3.Connection, config: Config, tab: ChildTab) -> None:
    """Resumed by hand, it runs outside its lead's tree; it moves only while idle, as its tab closes."""
    _check_stray_tab_may_close(config, tab)
    work = check_background_work(conn, config, tab, find_tab_sessions(conn, tab))
    for line in work.unknown:
        print(f"not known: {line}", file=sys.stderr)
    # Those checks can take many seconds: a turn, a nested tab or a parent may have come meanwhile.
    reconcile(conn, config, now_ms())
    tree = read_tree(config)
    fresh = map_tree_to_tab(tree, tab.id) if tree else None
    if fresh is None:
        raise UsageError(f"{tab.name} ({tab.id}), where it runs outside your tree, could not be read again")
    _check_stray_tab_may_close(config, fresh)
    blockers = [*find_mid_turn(fresh, find_tab_sessions(conn, fresh)), *work.live]
    if blockers:
        raise UsageError(f"it runs in {tab.name} ({tab.id}), outside your tree: {'; '.join(blockers)}. "
                         "Reopen it when it is idle")  # fmt: skip
    if run_agterm(config, ["session", "close", "--target", tab.id]) is None:
        after = read_tree(config)
        if after is None or map_tree_to_tab(after, tab.id) is not None:
            raise UsageError(f"agterm could not close {tab.name} ({tab.id}), where it runs outside your tree")
    print(f"closed {tab.name} ({tab.id}), where it ran outside your tree")


def _check_stray_tab_may_close(config: Config, tab: ChildTab) -> None:
    if config.hides(tab.name, tab.workspace_name):
        raise UsageError(f"it runs in {tab.name}, which is locked, and Guild ignores locked tabs")
    if tab.children:
        raise UsageError(f"it runs in {tab.name}, which has nested tabs that would close with it")
    if tab.parent:
        raise UsageError(f"it runs in {tab.name} ({tab.id}), which is now nested under another tab")


def _await_reopened(conn: sqlite3.Connection, config: Config, session_id: str, closed: ClosedWorker) -> int:
    """A resumed worker waits idle for a prompt, so its conversation turning up in the tab is the sign."""
    deadline = time.monotonic() + WORKER_START_WAIT_S
    while time.monotonic() < deadline:
        if find_conversation_tab(conn, closed.conversation_id) == session_id:
            print("resumed")
            return 0
        tree = read_tree(config)
        tab = map_tree_to_tab(tree, session_id) if tree else None
        if tree is not None and tab is None:
            # The tab closes when its command exits, so the resume failed; the record is closed again.
            with transaction(conn):
                unmark_reopened(conn, closed.conversation_id, session_id)
            print(f"NOT resumed: the tab closed, so its resume command exited. guild worker reopen {closed.id} "
                  "tries again.")  # fmt: skip
            return 1
        if tab is not None and tab.status in ("active", "completed"):
            print("resumed")
            return 0
        if tab is not None and tab.status == "blocked":
            break
        time.sleep(2)
    print(f"NOT resumed. Look: agtermctl session text --target {session_id}\n{read_screen_tail(config, session_id)}")
    return 1


def cmd_answer(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    me = current_party(conn)
    if me.handle != config.assistant_handle:
        raise UsageError(f"only {config.assistant_handle} relays the user's answers")
    try:
        with transaction(conn):
            result = answer_post(
                conn, int(args.id), str(args.text), config.user_alias or "user", now_ms(), relayed_by=me
            )
    except MessageStateError as exc:
        raise UsageError(str(exc)) from exc
    nudge_recipients(result.recipients)
    spawn_typed_delivery(result.message_id, result.recipients)
    print(f"answered #{args.id}; typing it into {', '.join(r.handle for r in result.recipients) or 'nobody'}")
    # A native peer message wakes the tab even if typing cannot get in; the answer also waits in its inbox.
    for line in map_wake_to_lines(result.recipients):
        print(line)
    return 0


def cmd_comment_small(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    me = current_party(conn)
    with transaction(conn):
        ref, comment_id = map_arg_to_comment_key(conn, str(args.comment))
        record_small_call(conn, ref, comment_id, str(args.did), me, now_ms())
    print(f"{comment_id} on {ref}: small, handled; the page shows {config.owner_name} what you did")
    return 0


def cmd_comment_draft(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    me = current_party(conn)
    try:
        draft = Path(str(args.reply_file)).expanduser().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise UsageError(f"cannot read the reply file: {exc}") from exc
    with transaction(conn):
        ref, comment_id = map_arg_to_comment_key(conn, str(args.comment))
        item_id = record_draft(conn, config, ref, comment_id, draft, str(args.plan), me, now_ms())
    print(
        f"{comment_id} on {ref}: draft #{item_id} waits for {config.owner_name} on the page. Reply and push for it are "
        f"held until {config.owner_name} chooses; the choice and the final text come back to you."
    )
    return 0


def cmd_comment_list(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    me = current_party(conn)
    lead = find_lead_on_surface(conn, _my_surface(conn))
    entries = list_open_calls(conn, config, me, lead.display_name if lead else None, bool(args.all), now_ms())
    if not entries:
        print("no colleague comment waits on a call or on the owner")
    for entry in entries:
        print(map_entry_to_line(entry))
    return 0


def cmd_review_list(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    view = map_db_to_reviews(conn, config, now_ms())
    print(json.dumps(view, indent=2, ensure_ascii=False))
    return 0


def cmd_review_sentence(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    me = current_party(conn)
    with transaction(conn):
        ref = map_arg_to_ref(conn, str(args.pr))
        record_sentence(conn, config, ref, str(args.text), me.handle, now_ms())
    print(f"{ref}: sentence saved")
    return 0


def cmd_review_doc(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    with transaction(conn):
        ref = map_arg_to_ref(conn, str(args.pr))
        source = Path(str(args.file)).expanduser()
        page = register_doc(conn, config, ref, source, now_ms())
        loc = int(conn.execute("SELECT loc FROM review_prs WHERE ref = ?", (ref,)).fetchone()["loc"] or 0)
    print(f"{ref}: document registered")
    for warning in find_doc_warnings(read_visible_top(page)[1]) + screen_doc_for_warnings(page, config):
        print(f"warning: {warning}", file=sys.stderr)
    terms = count_doc_terms(page)
    if loc >= REVIEW_LARGE_LOC and terms < DOC_MIN_TERMS:
        print(
            f"warning: {terms} click terms for a PR of {loc} lines; {config.owner_name} reads it without knowing the "
            f"code, so every internal name or concept is a term at its first use in each section (at least "
            f"{DOC_MIN_TERMS}), and the doc opens with the plain story of what changes: {REVIEW_DOC_SPEC}",
            file=sys.stderr,
        )
    return 0


def cmd_review_published(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    if not str(args.link).startswith("https://"):
        raise UsageError("the link is the https:// permalink of the post")
    with transaction(conn):
        ref = map_arg_to_ref(conn, str(args.pr))
        record_published_elsewhere(conn, ref, str(args.link), now_ms())
    print(f"{ref}: published, and it will not be posted again")
    return 0


def cmd_review_ready(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    print(
        f"guild review ready is retired: a draft waits for {config.owner_name} only once GitHub says it is ready. "
        f"When it is, run gh pr ready {args.pr}; if it is not, say why on the board.",
        file=sys.stderr,
    )
    return 2


def cmd_review_hold(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    ref = map_arg_to_ref(conn, str(args.pr))
    me = _check_may_hold(conn, config, ref)
    with transaction(conn):
        record_hold(conn, config, ref, str(args.why), me, now_ms())
    print(
        f"{ref}: held for {config.owner_name}'s review; it waits in Open and its merge is refused until "
        f"{config.owner_name} comments or reviews on GitHub or it is published, or guild review unhold {args.pr}"
    )
    return 0


def cmd_review_unhold(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    ref = map_arg_to_ref(conn, str(args.pr))
    _check_may_hold(conn, config, ref)
    with transaction(conn):
        record_unhold(conn, ref)
    print(f"{ref}: no longer held; the usual merge rule applies")
    return 0


def cmd_review_waive(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    ref = map_arg_to_ref(conn, str(args.pr))
    me = _check_may_hold(conn, config, ref)
    try:
        with transaction(conn):
            answer = record_approval_waiver(conn, ref, int(args.decision), me, now_ms())
    except ValueError as exc:
        raise UsageError(str(exc)) from exc
    print(
        f"{ref}: approval waived by {config.owner_name}'s decision #{args.decision} (answer: {answer!r}); every other "
        "merge condition still applies. Take it back: guild review unwaive " + str(args.pr)
    )
    return 0


def cmd_review_unwaive(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    ref = map_arg_to_ref(conn, str(args.pr))
    _check_may_hold(conn, config, ref)
    with transaction(conn):
        clear_approval_waiver(conn, ref)
    print(f"{ref}: the waiver is gone; it needs a GitHub approval again")
    return 0


def _check_may_hold(conn: sqlite3.Connection, config: Config, ref: str) -> str:
    """The handle of the PR's lead or the assistant; anyone else is refused."""
    me = current_party(conn)
    if me.handle == config.assistant_handle:
        return me.handle
    surface = _my_surface(conn)
    lead = find_lead_on_surface(conn, surface)
    row = conn.execute("SELECT repo, head FROM review_prs WHERE ref = ?", (ref,)).fetchone()
    owner = find_owner(conn, config, ref, str(row["repo"]), str(row["head"]))
    whose = owner.lead if owner and owner.lead else None
    if (
        lead is None
        or is_surface_locked(conn, config, surface)
        or (whose or "").casefold() != lead.display_name.casefold()
    ):
        known = f"it is {whose}'s" if whose else "no lead owns it yet (claim it, or name it in guild work set)"
        raise UsageError(f"only the PR's lead or {config.assistant_handle} holds it; {known}")
    return me.handle


def cmd_review_repost(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    with transaction(conn):
        ref = map_arg_to_ref(conn, str(args.pr))
        reopen_unsent_post(conn, ref)
    print(f"{ref}: the post goes out again at the next round")
    return 0


def cmd_dream(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    if args.wait_all:
        # The night word runs this form; a detached or one-by-one dream can be a daytime test.
        with transaction(conn):
            record_owner_off(conn, "guild dream --wait-all", now_ms())
    dreamers = resolve_dreamers(conn, config, [str(n) for n in args.leads])
    if not dreamers:
        print("no lead to dream: name one, or set dream_leads in the config")
        return 0
    if args.wait_all:
        # The night runs detached even here: should this wait be killed, the dreams and the summary still finish.
        started, night = time.monotonic(), new_night_id()
        spawn_night(night, [d.handle for d in dreamers]).wait()
        lost = "the night ended without its log line"
        runs = [read_night_run(d, night) or NightRun(d, FAILED, lost, 0.0, None) for d in dreamers]
        for run in runs:
            tokens = f"{run.tokens:,} tokens" if run.tokens is not None else "tokens unknown"
            why = f" ({run.why})" if run.why and run.outcome != OK else ""
            print(f"{run.dreamer.display}: {run.outcome}, {run.seconds:.0f} s, {tokens}{why}")
        total = sum(r.tokens or 0 for r in runs)
        print(f"total: {time.monotonic() - started:.0f} s, {total:,} tokens")
        return 0 if all(r.outcome == OK for r in runs) else 1
    if not args.wait:
        spawn_night(new_night_id(), [d.handle for d in dreamers])
        for dreamer in dreamers:
            print(f"{dreamer.display}: dreaming, detached; outcome in {runs_path(dreamer.directory)}")
        return 0
    failed = 0
    for dreamer in dreamers:
        run = run_dream(conn, config, dreamer.handle)
        failed += run.outcome != OK
        detail = run.why or f"{run.dream_chars} chars, {run.rules} rules, {run.seconds} s"
        print(f"{dreamer.display}: {run.outcome}: {detail}")
    return 1 if failed else 0


def cmd_notes(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    if args.lead:
        if args.write:
            raise UsageError("a lead writes only its own notes")
        lead = get_lead(conn, str(args.lead))
        directory, owner = lead_dir(lead.name), lead.display_name
    elif is_assistant_surface(conn, config, _my_surface(conn)):
        directory, owner = assistant_dir(), config.assistant_handle
    else:
        lead = _my_lead(conn, config)
        directory, owner = lead_dir(lead.name), lead.display_name
    if args.write:
        text = sys.stdin.read()
        write_notes(directory, text)
        print(f"notes saved for {owner}")
        if (size := measure_chars(text.strip())) > NOTES_INJECT_MAX_CHARS:
            print(convert_size_to_notes_warning(size))
        return 0
    text = read_handovers(directory) if args.handovers else read_notes(directory)
    print(text.rstrip() if text.strip() else "(empty)")
    return 0


def cmd_work_set(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    lead = _work_lead(conn, config, args.lead)
    repo = _repo_of_lead(conn, lead)
    refs: list[str] = []
    for text in cast(list[str], args.refs):
        ref = map_text_to_ref(text, issue_prefixes=config.issue_prefixes, default_repo=config.pr_repo_for(repo))
        if ref is None:
            raise UsageError(f"not an issue or PR: {text!r}; use PROJ-12, owner/repo#12 or a URL")
        refs.append(ref)
    with transaction(conn):
        set_lead_work(conn, lead.name, str(args.name), list(dict.fromkeys(refs)) if refs else None, now_ms())
    return _print_work(conn, lead)


def cmd_work_done(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    lead = _work_lead(conn, config, args.lead)
    with transaction(conn):
        finish_lead_work(conn, lead.name, str(args.name), now_ms())
    return _print_work(conn, lead)


def cmd_work_list(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    return _print_work(conn, _work_lead(conn, config, args.lead))


def _work_lead(conn: sqlite3.Connection, config: Config, name: str | None) -> Lead:
    if name is None:
        return _my_lead(conn, config)
    if is_surface_locked(conn, config, _my_surface(conn)):
        raise UsageError("this tab is locked, and Guild ignores locked tabs")
    return _active_lead(conn, config, name)


def _repo_of_lead(conn: sqlite3.Connection, lead: Lead) -> str | None:
    row = conn.execute(
        "SELECT repo FROM instances WHERE surface = ? AND ended_at IS NULL ORDER BY proc_start_ms DESC LIMIT 1",
        (lead.surface,),
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def _print_work(conn: sqlite3.Connection, lead: Lead) -> int:
    chunks = list_lead_work(conn, lead.name)
    print(f"{lead.display_name}: {len(chunks)} open" if chunks else f"{lead.display_name}: no work declared")
    for chunk in chunks:
        print(f"  {chunk.name}" + (f"  [{' '.join(chunk.refs)}]" if chunk.refs else ""))
    return 0


def cmd_ack(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    lead = _my_lead(conn, config)
    me = current_party(conn)
    try:
        with transaction(conn):
            message_id = add_ack(conn, me, lead.name, int(args.id), str(args.text), now_ms())
    except MessageStateError as exc:
        raise UsageError(str(exc)) from exc
    print(f"#{message_id} ack on #{args.id}; delivered to nobody")
    return 0


def cmd_priority_set(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    me = current_party(conn)
    if me.handle != config.assistant_handle:
        raise UsageError(f"only {config.assistant_handle} sets the priority")
    projects = [map_arg_to_project(str(text)) for text in args.projects]
    with transaction(conn):
        store_projects(conn, projects, me.handle, now_ms())
    return cmd_priority_show(args, conn, config)


def cmd_priority_show(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    projects = load_projects(conn)
    if not projects:
        print("no priority set")
    for position, project in enumerate(projects, start=1):
        print(f"{position}. {project.name}: {', '.join(project.issue_keys)}")
    return 0


def cmd_urgent(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    rows = conn.execute(
        """
        SELECT id, created_at, from_conversation, from_handle, kind, text, body, answer, resolved_at, notified_at
        FROM user_items WHERE urgent = 1 AND created_at > ? ORDER BY created_at DESC
        """,
        (now_ms() - int(args.days) * 86_400_000,),
    ).fetchall()
    shown = [row for row in rows if not is_conversation_locked(conn, config, str(row["from_conversation"]))]
    if not shown:
        print(f"no urgent questions in the last {args.days} days")
    for row in shown:
        stamp = time.strftime("%m-%d %H:%M", time.localtime(int(row["created_at"]) / 1000))
        closed = f"answered: {row['answer']}" if row["answer"] else "closed" if row["resolved_at"] else "open"
        notified = "notified" if row["notified_at"] else "not notified"
        print(f"#{row['id']} {stamp} {row['from_handle']} ({row['kind']}, {closed}, {notified})\n  {row['text']}")
        if row["body"]:
            print("  " + str(row["body"]).replace("\n", "\n  "))
    return 0


def cmd_handover(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    lead = get_lead(conn, str(args.lead))
    if lead.retired_at is not None:
        raise LeadError(f"{lead.display_name} is retired")
    me = current_party(conn)
    if is_surface_locked(conn, config, _my_surface(conn)):
        raise UsageError("this tab is locked, and Guild ignores locked tabs")
    text = sys.stdin.read()
    if not text.strip():
        raise UsageError("nothing on stdin to hand over")
    append_handover(lead.name, me.handle, time.strftime("%Y-%m-%d %H:%M"), text)
    print(f"handed over to {lead.display_name}")
    body = f"Handover from {me.handle}: read it with `guild notes --handovers`."
    try:
        with transaction(conn):
            recipient = resolve_handle(conn, lead.name)
            result = send_message(conn, me, [recipient], "info", body, now_ms())
    except (AddressError, MessageStateError) as exc:
        print(f"warning: the note is saved, but {lead.display_name} was not told: {exc}", file=sys.stderr)
        return 0
    nudge_recipients([recipient])
    for line in map_wake_to_lines(result.wake):
        print(line)
    return 0


def _my_surface(conn: sqlite3.Connection) -> str | None:
    instance_id = ensure_current_instance(conn)
    row = conn.execute("SELECT surface FROM instances WHERE instance_id = ?", (instance_id,)).fetchone()
    return row["surface"] if row else None


def _my_lead(conn: sqlite3.Connection, config: Config) -> Lead:
    surface = _my_surface(conn)
    lead = find_lead_on_surface(conn, surface)
    if lead is None or is_surface_locked(conn, config, surface):
        raise UsageError("this tab is not a team lead")
    return lead


def _agterm_session_of(conn: sqlite3.Connection, lead: Lead) -> str:
    row = conn.execute("SELECT agterm_session FROM surfaces WHERE surface = ?", (lead.surface,)).fetchone()
    if row is None or not row["agterm_session"]:
        raise LeadError(f"{lead.display_name} has no agterm tab")
    return str(row["agterm_session"])


def _active_lead(conn: sqlite3.Connection, config: Config, name: str) -> Lead:
    lead = get_lead(conn, name)
    if lead.retired_at is not None:
        raise LeadError(f"{lead.display_name} is retired")
    if is_surface_locked(conn, config, lead.surface):
        raise LeadError(f"{lead.display_name}'s tab is locked, and Guild ignores locked tabs")
    return lead


def _sync_terminal(conn: sqlite3.Connection, config: Config, lead: Lead) -> int:
    problems = sync_lead_terminal(config, lead, _agterm_session_of(conn, lead))
    for problem in problems:
        print(f"warning: {problem}", file=sys.stderr)
    if problems:
        print(f"run `guild lead sync {lead.name}` to try again", file=sys.stderr)
        return 1
    return 0
