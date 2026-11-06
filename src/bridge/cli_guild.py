import argparse
import json
import os
import shlex
import sqlite3
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

from bridge.asks import AskError
from bridge.attention import PriorityError, load_projects, map_arg_to_project, store_projects
from bridge.board import (
    ACK_CALL_TOKENS,
    CHARS_PER_TOKEN,
    AddressError,
    MessageStateError,
    add_ack,
    measure_ack_cost,
    resolve_handle,
    send_message,
)
from bridge.cli_asks import add_ask_commands
from bridge.cli_board import map_wake_to_lines, nudge_recipients
from bridge.config import Config, db_path, load_config, state_dir
from bridge.db import connect, now_ms, transaction
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
from bridge.identity import current_party, ensure_current_instance
from bridge.inject import measure_chars
from bridge.leads import (
    NOTES_INJECT_MAX_CHARS,
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
    set_lead_label,
    set_lead_work,
    sync_lead_terminal,
    write_notes,
)
from bridge.refs import map_text_to_ref
from bridge.registry import is_english_name, list_sessions, reconcile, record_tab_parent
from bridge.review_post import (
    ReviewError,
    map_arg_to_ref,
    record_published_elsewhere,
    record_sentence,
    reopen_unsent_post,
)
from bridge.review_worktree import DocError, register_doc
from bridge.reviews import map_db_to_reviews
from bridge.tabinput import is_tab_selected, run_agterm, type_when_idle
from bridge.watcher import map_tree_to_parent, read_screen_tail, read_tree
from bridge.workers import (
    ChildTab,
    check_background_work,
    find_mid_turn,
    find_tab_sessions,
    find_undelivered_reports,
    forget_closed_workers,
    list_closed_workers,
    list_live_workers,
    map_tab_to_closed_records,
    map_tree_to_child_tab,
    map_workers_to_cap_refusal,
    record_closed_workers,
)

WAKE_WAIT_S = 120
WORKER_START_WAIT_S = 45
# No Claude in Chrome: it drives the owner's own browser, and their screen is often shared.
WORKER_CLAUDE_FLAGS = "--dangerously-skip-permissions --no-chrome"
# Playwright's MCP opens a visible browser window, so workers go without it.
WORKER_DISABLED_PLUGINS = ("playwright@claude-plugins-official",)
WORKER_BROWSER_RULE = (
    "No browser use (Claude in Chrome, Playwright) unless the task says so, and then only in a hidden tab group: "
    "{owner}'s screen is often shared. The tracker and the claude.ai connectors are your tools."
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
    if argv[:1] == ["worker"] and argv[1:2] in (["close"], ["closed"]):
        return build_worker_lifecycle_parser().parse_args(argv[1:])
    return build_parser(config or Config()).parse_args(argv)


def build_worker_lifecycle_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="guild worker")
    sub = parser.add_subparsers(dest="worker_command", required=True)
    p = sub.add_parser("close", help="close your own nested tab and keep what resumes it")
    p.add_argument("tab", help="the tab's name or agterm id")
    p.add_argument("--force", action="store_true", help="close even if it is mid-turn or has a live background run")
    p.set_defaults(func=cmd_worker_close)
    p = sub.add_parser("closed", help="closed workers, newest first, with the command that resumes each")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_worker_closed)
    return parser


def build_parser(config: Config) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="guild", description="Team leads on top of bridge")
    sub = parser.add_subparsers(dest="command", required=True)
    add_ask_commands(sub, config.owner_name)

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
    p.add_argument("--ref", action="append", default=[], help="tracker project or issue; repeat")
    p.add_argument("--label", required=True, help="short project label; the tab is named '<Name> · <label>'")
    p.add_argument("--alias", help="spelling for dictation, e.g. Arlow")
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
    p = lead_sub.add_parser("wake", help="type a first prompt into a lead's idle tab, so its role arrives")
    p.add_argument("name")
    p.set_defaults(func=cmd_lead_wake)

    p = sub.add_parser(
        "worker",
        help="start a task in its own tab nested under yours, and check that it started; "
        "`guild worker close <tab>` closes one, `guild worker closed` lists them",
    )
    p.add_argument("name", help="the task, in English; the tab's name")
    p.add_argument("--prompt", required=True, help="the brief; - reads stdin")
    p.add_argument("--cwd", help="default: this directory")
    p.set_defaults(func=cmd_worker)

    p = sub.add_parser("answer", help="relay the user's answer to a post in the feed (the assistant only)")
    p.add_argument("id", type=int)
    p.add_argument("text")
    p.set_defaults(func=cmd_answer)

    review = sub.add_parser("review", help="the PR review queue on the page")
    review_sub = review.add_subparsers(dest="review_command", required=True)
    p = review_sub.add_parser("list", help="what the queue shows")
    p.set_defaults(func=cmd_review_list)
    p = review_sub.add_parser("sentence", help="the short description of what a PR does, for its review post")
    p.add_argument("pr", help="URL, repo#n or a number")
    p.add_argument("text")
    p.set_defaults(func=cmd_review_sentence)
    p = review_sub.add_parser("doc", help="register a large PR's HTML document; the page serves this copy")
    p.add_argument("pr")
    p.add_argument("file")
    p.set_defaults(func=cmd_review_doc)
    p = review_sub.add_parser("published", help="record a post made by hand, so it is never posted again")
    p.add_argument("pr")
    p.add_argument("link")
    p.set_defaults(func=cmd_review_published)
    p = review_sub.add_parser(
        "repost",
        help="send again a post whose sender stopped (look in the channel first) or that a dry run only logged",
    )
    p.add_argument("pr")
    p.set_defaults(func=cmd_review_repost)

    p = sub.add_parser("dream", help="the night's dreams, in parallel and detached; default: config dream_leads")
    p.add_argument("leads", nargs="*", help="lead names, the assistant's handle, or all; none means config dream_leads")
    waiting = p.add_mutually_exclusive_group()
    waiting.add_argument("--wait", action="store_true", help="dream here, one after another, and print each outcome")
    waiting.add_argument(
        "--wait-all", action="store_true", help="dream in parallel and return when all are done or cut at the limit"
    )
    p.set_defaults(func=cmd_dream)

    p = sub.add_parser("notes", help="a lead's or the assistant's notes that outlive conversations")
    p.add_argument("lead", nargs="?", help="default: your own")
    p.add_argument("--write", action="store_true", help="replace your notes from stdin")
    p.add_argument("--handovers", action="store_true", help="show handovers from other tabs")
    p.set_defaults(func=cmd_notes)

    work = sub.add_parser("work", help="the main chunks of work a lead declares; the page shows these")
    work_sub = work.add_subparsers(dest="work_command", required=True)
    p = work_sub.add_parser("set", help="add a chunk, or update it by name; given refs replace its refs")
    p.add_argument("name", help="a few plain words, e.g. 'Nightly export worker'")
    p.add_argument("refs", nargs="*", help="PROJ-12, acme/web#34, #34 (your repo), or a URL")
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

    p = sub.add_parser("ack-cost", help="what board posts reaching every lead cost; tokens are an estimate")
    p.add_argument("--hours", type=int, default=24)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_ack_cost)

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
    sessions = {s.surface: s for s in list_sessions(conn, config, now)}
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
    if len(live) >= config.worker_cap:
        raise UsageError(map_workers_to_cap_refusal(live, config.worker_cap, now_ms()))
    report = f'Report to the lead that started you when done or stuck: bridge send {me.handle} "…"'
    brief = state_dir() / "worker-briefs" / f"{uuid.uuid4().hex}.md"
    brief.parent.mkdir(parents=True, exist_ok=True)
    rules = map_names_to_brief_rules(list_internal_names(conn, config), config.assistant_handle, config.owner_name)
    brief.write_text(
        f"{prompt.strip()}\n\n{rules}\n{WORKER_BROWSER_RULE.format(owner=config.owner_name)}\n\n{report}\n"
    )
    first_prompt = f"Your brief is in {brief}. Read it and do it."
    cwd = Path(os.path.abspath(os.path.expanduser(args.cwd or os.getcwd())))
    command = "zsh -lc " + shlex.quote(build_worker_claude_command(cwd, first_prompt))
    started = run_agterm(
        config,
        ["session", "new", "--parent", parent, "--no-select", "--name", name, "--cwd", str(cwd),
         "--command", command, "--json"],
    )  # fmt: skip
    if started is None:
        raise UsageError("agterm could not create the tab")
    session_id = str(json.loads(started)["result"]["id"])
    record_tab_parent(conn, session_id, parent)
    print(f"tab {session_id}: {name}")
    return _await_worker_start(config, session_id)


def build_worker_claude_command(cwd: Path, first_prompt: str) -> str:
    settings = map_cwd_to_worker_settings(cwd)
    return f"claude {WORKER_CLAUDE_FLAGS} --settings {shlex.quote(settings)} {shlex.quote(first_prompt)}"


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
    work = check_background_work(conn, config, tab, sessions)
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
    records = map_tab_to_closed_records(tab, sessions)
    with transaction(conn):
        ids = record_closed_workers(conn, records, tab.name, me.handle, now_ms())
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
    for record in records:
        print(f"resume: {record.resume_command}")
    if not records:
        print("no conversation was known in that tab, so there is nothing to resume")
    return 0


def _read_child_tab(conn: sqlite3.Connection, config: Config, parent: str, wanted: str) -> ChildTab:
    reconcile(conn, config, now_ms())
    tree = read_tree(config)
    if tree is None:
        raise UsageError("agterm is not reachable, so the tab cannot be checked")
    return map_tree_to_child_tab(tree, parent, wanted)


def cmd_worker_closed(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    rows = list_closed_workers(conn, int(args.limit))
    if not rows:
        print("no closed workers")
    for row in rows:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(row["closed_at"]) / 1000))
        print(f"{stamp}  {row['tab_name']}  (lead {row['parent_handle']})\n  {row['resume_command']}")
    return 0


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
        register_doc(conn, ref, Path(str(args.file)).expanduser(), now_ms())
    print(f"{ref}: document registered")
    return 0


def cmd_review_published(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    if not str(args.link).startswith("https://"):
        raise UsageError("the link is the https:// permalink of the post")
    with transaction(conn):
        ref = map_arg_to_ref(conn, str(args.pr))
        record_published_elsewhere(conn, ref, str(args.link), now_ms())
    print(f"{ref}: published, and it will not be posted again")
    return 0


def cmd_review_repost(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    with transaction(conn):
        ref = map_arg_to_ref(conn, str(args.pr))
        reopen_unsent_post(conn, ref)
    print(f"{ref}: the post goes out again at the next round")
    return 0


def cmd_dream(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
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


def cmd_ack_cost(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    cost = measure_ack_cost(conn, now_ms() - int(args.hours) * 3_600_000)
    if args.json:
        print(json.dumps(cost))
        return 0
    print(
        f"last {args.hours}h: {cost['posts']} board posts, {cost['addressed']} deliveries "
        f"({cost['not_yet_seen']} not yet seen), {cost['acks']} acks"
    )
    print(
        f"about {cost['estimated_tokens']} tokens: post text and ack text at {CHARS_PER_TOKEN} characters a token, "
        f"plus an assumed {ACK_CALL_TOKENS} per ack call. Each line also stays in the lead's context and is "
        "re-read at cached price every later turn; that part is not counted."
    )
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
