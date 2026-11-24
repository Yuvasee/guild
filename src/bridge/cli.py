import argparse
import fcntl
import json
import os
import signal
import sqlite3
import sys
import time
import urllib.request
from dataclasses import asdict
from pathlib import Path

from bridge.auto_merge import map_db_to_dry_run
from bridge.board import (
    HINT_SENT,
    SYSTEM_CONVERSATION,
    USER_ITEM_MAX_CHARS,
    Party,
    Recipient,
    add_user_item,
    send_message,
    write_hint,
)
from bridge.claude import read_session_file
from bridge.cli_board import add_board_commands
from bridge.cli_claims import add_claim_commands
from bridge.cli_pages import add_page_commands
from bridge.cli_pins import add_pin_commands
from bridge.compaction import (
    COMPACT_WAIT_S,
    CONTINUE_PROMPTS,
    GENERIC_KEEP,
    KEEP_MAX_CHARS,
    PHASE_COMPACTING,
    PHASE_CONTINUE,
    RETRY_AFTER_MS,
    TOLD_HELD,
    UNREAD_NOTE,
    end_driving,
    list_dropped_lines,
    load_keep,
    read_phase,
    save_keep,
    take_phase,
    take_told_once,
)
from bridge.config import Config, db_path, load_config, state_dir
from bridge.context_watch import ContextTab, find_alert_recipients, list_context_tabs
from bridge.db import connect, now_ms, run_typed_command, transaction
from bridge.deploycheck import check_long_message, check_rewake, check_typing, close_stale_check_tabs
from bridge.dream import resolve_dreamers, run_dream, run_night, stop_claude_on_term
from bridge.errlog import log_error, log_slow
from bridge.errors import UsageError
from bridge.feed import (
    BLOCKED_TAB,
    LOCKED_PATIENT_DELAYS_S,
    StillLockedError,
    claim_failure_log,
    deliver_typed,
    hold_typer_lock,
    queue_retry_without_db,
    record_failed_delivery,
    record_stuck_delivery,
    retry_when_locked,
    spawn_typed_delivery,
    tell_assistant_of_draft,
)
from bridge.host_workers import HOST_KIND_PREFIX, run_tab_loop
from bridge.identity import ensure_current_instance
from bridge.inject import measure_chars
from bridge.install import find_hook_executable, install_hooks, uninstall_hooks
from bridge.issue_sync import map_report_to_lines, run_issue_sync
from bridge.procinfo import process_args, process_start_ms
from bridge.registry import (
    FOCUS_MAX_CHARS,
    HandleTakenError,
    SessionView,
    format_age,
    is_nested_agent,
    list_sessions,
    make_instance_id,
    reconcile,
    set_focus,
    set_handle,
)
from bridge.restore import build_restore_command, pin_restore_command
from bridge.review_post import run_review_post
from bridge.review_slack import run_connector_read
from bridge.review_worktree import run_next_worktree_job
from bridge.rewake_marks import install_waiter_script
from bridge.serve import DEFAULT_PORT, serve
from bridge.service import install_service, restart_service, uninstall_service
from bridge.shadow_filter import map_rows_to_report
from bridge.shadow_filter import read_rows as read_shadow_rows
from bridge.tabinput import is_tab_selected, map_box_to_quote, type_when_idle
from bridge.watcher import read_tree
from bridge.worker_api import WORKER_API_PORT, convert_bridge_commands_to_gbridge

DEFAULT_SETTINGS = Path.home() / ".claude/settings.json"
PIN_SESSION_FILE_RETRIES = 10
_DRIVE_HELD = {
    "draft": "its input box holds text that is not stray keys, which may be the owner's draft",
    "unproven": "its input box holds text, and the tab stayed selected, so bridge could not tell a draft from Claude "
    "Code's grey suggestion",
    "timeout": "it stayed busy with a turn or a dialog, its vim input stayed out of INSERT mode, its screen showed no "
    "readable input box, or the Mac slept",
}
"""Waits that ran out: nothing went wrong, so they are no error."""
_DRIVE_FAILED = {
    "failed": "a key bridge typed did not show as expected, so it typed no more; look for a stray character in the box",
    "stuck": "bridge's text did not leave the box after Enter",
}


def main(argv: list[str] | None = None) -> int:
    config = load_config()
    args = build_parser(config).parse_args(argv)
    if args.func is not cmd_deliver:
        return run_typed_command("bridge", args.func.__name__, db_path(), lambda conn: run_command(args, conn, config))
    # Detached, with nobody to see it fail: a lock that outlasts connect's busy timeout must not end it unlogged.
    # Its retries count tries of the short wait, holding the tab's typer lock meanwhile.
    try:
        conn = retry_when_locked("_deliver: open the database", lambda: connect(db_path()), LOCKED_PATIENT_DELAYS_S)
    except StillLockedError:
        queue_retry_without_db(int(args.message_id), str(args.conversation_id))
        return 1
    try:
        return run_command(args, conn, config)
    finally:
        conn.close()


def run_command(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    try:
        return int(args.func(args, conn, config))
    except UsageError as exc:
        print(f"bridge: {exc}", file=sys.stderr)
        return 2


def build_parser(config: Config) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bridge", description="Coordination registry for local agent sessions.")
    sub = parser.add_subparsers(required=True, metavar="command")

    p = sub.add_parser("sessions", help="list live sessions")
    p.add_argument("--json", action="store_true")
    p.add_argument("--all", action="store_true", help="include ended sessions")
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("whoami", help="show this session's registry entry")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_whoami)

    p = sub.add_parser("focus", help="set what this session is working on")
    p.add_argument("text", nargs="?")
    p.add_argument("--clear", action="store_true")
    p.set_defaults(func=cmd_focus)

    p = sub.add_parser("keep", help="save notes that bridge gives back to this session after compaction")
    p.add_argument("text", nargs="?", help="the notes; read from stdin when omitted")
    p.add_argument("--show", action="store_true", help="print the saved notes")
    p.set_defaults(func=cmd_keep)

    p = sub.add_parser("handle", help="set this session's stable handle (survives restarts of the tab)")
    p.add_argument("name")
    p.set_defaults(func=cmd_handle)

    p = sub.add_parser("install-hooks", help="add bridge hooks to Claude Code settings")
    p.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    p.set_defaults(func=cmd_install_hooks)

    p = sub.add_parser("uninstall-hooks", help="remove bridge hooks from Claude Code settings")
    p.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    p.set_defaults(func=cmd_uninstall_hooks)

    add_board_commands(sub, config)
    add_claim_commands(sub, config)
    add_pin_commands(sub)
    add_page_commands(sub)

    p = sub.add_parser("issue-sync", help="move Linear issues forward to match their PRs and live work")
    p.add_argument("--dry-run", action="store_true", help="print the moves without making them")
    p.set_defaults(func=cmd_issue_sync)

    p = sub.add_parser(
        "auto-merge", help="what the auto-merger would do with each open queue PR: merge it, or what blocks it"
    )
    p.add_argument("--dry-run", action="store_true", required=True, help="read GitHub, merge and write nothing")
    p.set_defaults(func=cmd_auto_merge)

    p = sub.add_parser("shadow-report", help="what the shadow wake-up filter decided for messages to leads")
    p.add_argument("--days", type=float, default=7)
    p.set_defaults(func=cmd_shadow_report)

    p = sub.add_parser("serve", help="run the dashboard web server")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("install-service", help="run the dashboard permanently under launchd")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.set_defaults(func=cmd_install_service)

    p = sub.add_parser("uninstall-service", help="stop and remove the launchd dashboard service")
    p.set_defaults(func=cmd_uninstall_service)

    p = sub.add_parser("restart-service", help="restart the dashboard service (after reinstalling bridge)")
    p.set_defaults(func=cmd_restart_service)

    p = sub.add_parser(
        "deploy-check",
        help="type a line and its Enter into a throwaway tab, as bridge types into leads' tabs, and wake an idle "
        "throwaway Claude through the rewake waiter; run before and after a deploy, exit 1 if either does not work",
    )
    p.set_defaults(func=cmd_deploy_check)

    p = sub.add_parser("_compact-drive", help=argparse.SUPPRESS)
    p.add_argument("phase", choices=("compact", "continue"))
    p.add_argument("--keep", choices=tuple(CONTINUE_PROMPTS), default="shown")
    # From a start whose hook could not write: the drive moves the cycle to "continue" itself.
    p.add_argument("--claim", action="store_true")
    p.add_argument("--reopened", action="store_true", help="messages from while the context was full are given again")
    p.add_argument("--generic-keep", action="store_true", help="/compact says what to keep: no notes were saved")
    p.add_argument("--by-screen", action="store_true", help="probe whenever the screen shows the tab waiting")
    p.add_argument("instance_id")
    p.add_argument("conversation_id")
    p.set_defaults(func=cmd_compact_drive)

    p = sub.add_parser("_worktree-job", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_worktree_job)

    p = sub.add_parser("_deliver", help=argparse.SUPPRESS)
    p.add_argument("message_id", type=int)
    p.add_argument("conversation_id")
    p.set_defaults(func=cmd_deliver)

    p = sub.add_parser("_review-post", help=argparse.SUPPRESS)
    p.add_argument("ref")
    p.add_argument("claim", type=int)
    p.set_defaults(func=cmd_review_post)

    p = sub.add_parser("_review-slack-read", help=argparse.SUPPRESS)
    p.add_argument("claim", type=int)
    p.set_defaults(func=cmd_review_slack_read)

    p = sub.add_parser("_dream", help=argparse.SUPPRESS)
    p.add_argument("--night", required=True)
    p.add_argument("lead")
    p.set_defaults(func=cmd_dream)

    p = sub.add_parser("_dream-night", help=argparse.SUPPRESS)
    p.add_argument("--night", required=True)
    p.add_argument("handles", nargs="+")
    p.set_defaults(func=cmd_dream_night)

    p = sub.add_parser("_host-tab", help=argparse.SUPPRESS)
    p.add_argument("worker")
    p.set_defaults(func=cmd_host_tab)

    p = sub.add_parser("_pin-restore", help=argparse.SUPPRESS)
    p.add_argument("instance_id")
    p.add_argument("conversation_id")
    p.set_defaults(func=cmd_pin_restore)
    return parser


def cmd_sessions(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    now = now_ms()
    reconcile(conn, config, now)
    me = _current_instance_id()
    sessions = [
        s
        for s in list_sessions(conn, now, include_ended=bool(args.all))
        if s.instance_id == me or not config.hides(s.tab_name, s.workspace_name)
    ]
    if args.json:
        print(json.dumps([asdict(s) for s in sessions], indent=2))
        return 0
    for line in map_sessions_to_lines(sessions, me, now):
        print(line)
    return 0


def cmd_whoami(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    instance_id = ensure_current_instance(conn)
    now = now_ms()
    view = next((s for s in list_sessions(conn, now) if s.instance_id == instance_id), None)
    if view is None:
        raise UsageError(f"instance {instance_id} is not live in the registry")
    if args.json:
        print(json.dumps(asdict(view), indent=2))
        return 0
    for line in map_sessions_to_lines([view], instance_id, now):
        print(line)
    print(f"instance {view.instance_id}  conversation {view.conversation_id}  peer {view.peer_name}")
    return 0


def cmd_focus(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    instance_id = ensure_current_instance(conn)
    conversation_id = _conversation_of(conn, instance_id)
    if args.clear:
        text = None
    elif args.text:
        text = " ".join(str(args.text).split())
        if len(text) > FOCUS_MAX_CHARS:
            raise UsageError(f"focus is {len(text)} chars; keep it under {FOCUS_MAX_CHARS}")
    else:
        row = conn.execute("SELECT focus FROM conversations WHERE conversation_id = ?", (conversation_id,)).fetchone()
        print(row["focus"] if row and row["focus"] else "(no focus set)")
        return 0
    with transaction(conn):
        set_focus(conn, conversation_id, text, now_ms())
    print(f"focus: {text}" if text else "focus cleared")
    return 0


def cmd_keep(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    conversation_id = _conversation_of(conn, ensure_current_instance(conn))
    if args.show:
        kept = load_keep(conn, conversation_id)
        print(kept[0] if kept else "(nothing saved)")
        return 0
    text = (str(args.text) if args.text is not None else sys.stdin.read()).strip()
    if not text:
        raise UsageError("nothing to keep: pass the notes as an argument or on stdin")
    size = measure_chars(text)
    if size > KEEP_MAX_CHARS:
        raise UsageError(f"notes are {size} chars; keep them under {KEEP_MAX_CHARS}")
    at_ms = now_ms()
    with transaction(conn):
        previous = load_keep(conn, conversation_id)
        save_keep(conn, conversation_id, text, at_ms)
    print(f"kept {size} chars; bridge gives them back after the next compaction")
    dropped = list_dropped_lines(previous[0], text) if previous else []
    if previous and dropped:
        # One list per conversation: whatever the writer leaves out of the new one is gone for good.
        print(
            f"\nThis replaced the list you saved {format_age(at_ms - previous[1])} ago. These of its lines are not "
            "in the new one, and are lost unless you keep them again (run `bridge keep` with the full list):"
        )
        print("\n".join(dropped))
    return 0


def cmd_handle(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    instance_id = ensure_current_instance(conn)
    row = conn.execute("SELECT surface FROM instances WHERE instance_id = ?", (instance_id,)).fetchone()
    if row is None or row["surface"] is None:
        raise UsageError("this session has no agterm surface, so it has no stable handle")
    name = str(args.name)
    if not name.replace("-", "").isalnum() or name != name.lower():
        raise UsageError("handle must be lowercase letters, digits and dashes")
    try:
        with transaction(conn):
            set_handle(conn, str(row["surface"]), name, now_ms())
    except HandleTakenError as exc:
        raise UsageError(str(exc)) from exc
    print(f"handle: {name}")
    return 0


def cmd_issue_sync(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    report = run_issue_sync(conn, config, now_ms(), dry_run=bool(args.dry_run))
    for line in map_report_to_lines(report, dry_run=bool(args.dry_run)):
        print(line)
    for _, recipient in report.sent:
        if recipient.pid:
            write_hint(recipient.pid, HINT_SENT)
    return 1 if report.failed or report.errors else 0


def cmd_auto_merge(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    print(f"auto_merge = {config.auto_merge}")
    for ref, block in map_db_to_dry_run(conn, config):
        print(f"{ref.removeprefix('pr:')}: {'would merge' if block is None else f'blocked by {block}'}")
    return 0


def cmd_shadow_report(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    for line in map_rows_to_report(read_shadow_rows(), now_ms() - round(float(args.days) * 86_400_000)):
        print(line)
    return 0


def cmd_install_hooks(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    executable = find_hook_executable()
    waiter = install_waiter_script()
    events = install_hooks(Path(args.settings), executable, waiter_script=waiter)
    print(f"installed {', '.join(events)} hooks → {executable} in {args.settings}; rewake waiter {waiter}")
    return 0


def cmd_uninstall_hooks(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    uninstall_hooks(Path(args.settings))
    print(f"removed bridge hooks from {args.settings}")
    return 0


def cmd_serve(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    conn.close()
    serve(config, int(args.port))
    return 0


def cmd_install_service(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    path = install_service(int(args.port))
    url = f"http://127.0.0.1:{args.port}/"
    deadline = time.monotonic() + 10
    while not dashboard_is_up(url):
        if time.monotonic() > deadline:
            raise UsageError(f"service installed at {path} but the dashboard is not answering; see serve.log")
        time.sleep(0.2)
    print(f"dashboard service running: {url} (launchd {path})")
    return 0


def cmd_uninstall_service(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    uninstall_service()
    print("dashboard service removed")
    return 0


def cmd_restart_service(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    restart_service()
    print("dashboard service restarted")
    return 0


def cmd_deploy_check(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    # A killed deploy still runs each check's finally, which closes its tab; only SIGKILL skips it.
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, exit_on_signal)
    for note in close_stale_check_tabs(config, time.time()):
        print(f"deploy check: {note}")
    results = [
        ("typing", check_typing(config)),
        ("rewake", check_rewake(config)),
        ("long message", check_long_message(config)),
    ]
    for name, result in results:
        print(f"deploy check {name} {'passed' if result.ok else 'FAILED'}: {result.detail}")
    return 0 if all(result.ok for _, result in results) else 1


def exit_on_signal(signum: int, frame: object) -> None:
    # A second signal would cut short the closing it asks for.
    for other in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(other, signal.SIG_IGN)
    raise SystemExit(128 + signum)


def dashboard_is_up(url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "api/state", timeout=2) as response:
            return response.status == 200
    except OSError:
        return False


def claim_compaction(conn: sqlite3.Connection, conversation_id: str) -> bool:
    """Started by a start whose hook could not write, so the lock may stay held a while yet: a typed command's wait."""
    try:
        with transaction(conn):
            return take_phase(conn, conversation_id, PHASE_COMPACTING, PHASE_CONTINUE)
    except sqlite3.OperationalError:
        # The conversation's next hook ends the cycle; until then nobody types "continue".
        log_error("compact-drive", f"continue not claimed for {conversation_id}: database stayed locked")
    return False


def find_instance_tab(conn: sqlite3.Connection, instance_id: str) -> tuple[str, str, bool]:
    """(session, pane, on a host). A host worker's drive starts in serve, whose environment names no tab: its
    registered tab is the one."""
    row = conn.execute(
        "SELECT s.agterm_session, s.surface, i.kind FROM instances i JOIN surfaces s ON s.surface = i.surface"
        " WHERE i.instance_id = ?",
        (instance_id,),
    ).fetchone()
    if row is not None and row["agterm_session"]:
        host = str(row["kind"] or "").startswith(HOST_KIND_PREFIX)
        return str(row["agterm_session"]), str(row["surface"]), host
    return os.environ.get("AGTERM_SESSION_ID", ""), os.environ.get("AGTERM_PANE_ID", ""), False


def cmd_worktree_job(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    run_next_worktree_job(conn, config)
    return 0


def cmd_compact_drive(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    """Types /compact (or the continue prompt) into the session's agterm pane once Claude Code waits for input."""
    phase, instance_id, conversation_id = str(args.phase), str(args.instance_id), str(args.conversation_id)
    session, pane, on_host = find_instance_tab(conn, instance_id)
    expected = PHASE_COMPACTING if phase == "compact" else PHASE_CONTINUE
    if phase == "compact":
        command = f"/compact {GENERIC_KEEP}" if args.generic_keep else "/compact"
    else:
        command = CONTINUE_PROMPTS[str(args.keep)] + (UNREAD_NOTE if args.reopened else "")
        if on_host:
            command = convert_bridge_commands_to_gbridge(command)
    if args.claim and not claim_compaction(conn, conversation_id):
        return 0

    def still_wanted() -> bool:
        row = conn.execute(
            "SELECT conversation_id, ended_at FROM instances WHERE instance_id = ?", (instance_id,)
        ).fetchone()
        alive = row is not None and row["ended_at"] is None and row["conversation_id"] == conversation_id
        # Another compaction, a newer driver or the user may have moved the cycle on while this one waited.
        return alive and read_phase(conn, conversation_id) == expected

    def may_probe_box() -> bool:
        # Claude Code's grey suggestion after a turn reads as a draft; without a probe /compact would never go in.
        # A prompt refused at the context limit leaves the status "active": a safety-net drive goes by the screen,
        # which type_when_idle reads as not held right before every probe.
        status = conn.execute("SELECT status FROM instances WHERE instance_id = ?", (instance_id,)).fetchone()
        idle = args.by_screen or (status is not None and status["status"] == "idle")
        return idle and not is_tab_selected(config, session)

    told_unsure = False

    def tell_unsure(box: str) -> None:
        nonlocal told_unsure
        tree = read_tree(config) or {}
        with transaction(conn):
            if read_phase(conn, conversation_id) != expected:
                return
            if phase == "compact" and not take_told_once(conn, TOLD_HELD, conversation_id, now_ms()):
                told_unsure = True
                return
            told = tell_of_held_drive(conn, config, tree, conversation_id, instance_id, phase, "unsure", box)
        told_unsure = True
        _spawn_told(told)

    result = type_when_idle(
        config,
        session,
        pane,
        command,
        still_wanted,
        wait_s=COMPACT_WAIT_S,
        may_probe_box=may_probe_box,
        tell_unsure=tell_unsure,
    )
    if result == "typed":
        if phase == "continue":
            with transaction(conn):
                take_phase(conn, conversation_id, PHASE_CONTINUE, None)
        return 0
    # Read before the write lock: agterm can take seconds to answer.
    tree = (read_tree(config) or {}) if result != "unwanted" else {}
    with transaction(conn):
        if read_phase(conn, conversation_id) != expected:
            return 0
        end_driving(conn, conversation_id, now_ms())
        # "unwanted" with the phase still ours: its tab ended or moved to another conversation, so nothing is owed.
        # Told of the box's text at the start, they already know the wait would end like this. A retried /compact
        # held again is told once per cycle; a "continue" is never retried.
        told = (
            []
            if result == "unwanted"
            or (result == "draft" and told_unsure)
            or (phase == "compact" and not take_told_once(conn, TOLD_HELD, conversation_id, now_ms()))
            else tell_of_held_drive(conn, config, tree, conversation_id, instance_id, phase, result)
        )
    if result in _DRIVE_FAILED:
        log_error("compact-drive", f"{phase} not typed for {conversation_id}: {result}")
    elif result in _DRIVE_HELD:
        log_slow("compact-drive", f"{phase} not typed for {conversation_id} in {COMPACT_WAIT_S // 60} min: {result}")
    _spawn_told(told)
    return 1 if result in _DRIVE_FAILED else 0


def _spawn_told(told: list[tuple[int, Recipient]]) -> None:
    for message_id, recipient in told:
        if recipient.pid:
            write_hint(recipient.pid, HINT_SENT)
        spawn_typed_delivery(message_id, [recipient])


def tell_of_held_drive(
    conn: sqlite3.Connection,
    config: Config,
    tree: dict[str, object],
    conversation_id: str,
    instance_id: str,
    phase: str,
    result: str,
    box: str = "",
) -> list[tuple[int, Recipient]]:
    """The assistant and a worker's lead hear it, as of a dead tab; with neither, the owner's page. "unsure" is told
    while the wait goes on, with the box's text."""
    row = conn.execute(
        "SELECT s.handle, s.agterm_session, s.surface FROM instances i LEFT JOIN surfaces s ON s.surface = i.surface"
        " WHERE i.instance_id = ?",
        (instance_id,),
    ).fetchone()
    name = str(row["handle"]) if row and row["handle"] else conversation_id[:8]
    shown = next((t for t in list_context_tabs(conn, config, tree) if t.conversation_id == conversation_id), None)
    # Without agterm's tree the lead is unknown, but the assistant still hears it.
    tab = shown or ContextTab(
        conversation_id, instance_id, str(row["agterm_session"] if row else "?"), str(row["surface"] if row else ""),
        name, None,
    )  # fmt: skip
    recipients = find_alert_recipients(conn, config, tab)
    what = "/compact" if phase == "compact" else "continue"
    then = "type /compact there" if phase == "compact" else "tell it to continue"
    if result == "unsure":
        body = (
            f'bridge is waiting to type {what} in tab "{tab.name}" ({tab.session}): its input box holds '
            f"{map_box_to_quote(box)}, which bridge cannot judge as stray keys or someone's words, so it leaves it. "
            f"Clear it if it is stray; bridge waits up to {COMPACT_WAIT_S // 60} min, then {then} by hand."
        )
    else:
        why = _DRIVE_HELD.get(result) or _DRIVE_FAILED[result]
        # A failed drive stopped at an unexpected screen, often seconds in, not at the end of its wait.
        within = f" within {COMPACT_WAIT_S // 60} min" if result in _DRIVE_HELD else ""
        again = (
            f"bridge tries again after {RETRY_AFTER_MS // 60_000} min and does not tell of it again; "
            if phase == "compact"
            else ""
        )
        body = (
            f'bridge could not type {what} in tab "{tab.name}" ({tab.session}){within}: '
            f"{why}. {again}once the box is clear, {then}."
        )
    if not recipients:
        text = (
            body
            if result == "unsure"
            else f"{name}: bridge could not type {what} ({result}); do it by hand once the box is clear"
        )
        add_user_item(conn, Party(conversation_id, name), "action", text[:USER_ITEM_MAX_CHARS], now_ms())
        return []
    sent = send_message(conn, Party(SYSTEM_CONVERSATION, "bridge"), recipients, "info", body, now_ms())
    conn.execute("UPDATE messages SET post_type = ? WHERE id = ?", (BLOCKED_TAB, sent.message_id))
    return [(sent.message_id, r) for r in recipients]


def cmd_review_post(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    run_review_post(conn, config, str(args.ref), int(args.claim))
    return 0


def cmd_dream(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    stop_claude_on_term()
    return 0 if run_dream(conn, config, str(args.lead), night=str(args.night)).outcome == "ok" else 1


def cmd_dream_night(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    dreamers = resolve_dreamers(conn, config, [str(h) for h in args.handles])
    runs = run_night(conn, config, str(args.night), dreamers)
    return 0 if all(r.outcome == "ok" for r in runs) else 1


def cmd_review_slack_read(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    run_connector_read(conn, config, int(args.claim))
    return 0


def cmd_deliver(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    try:
        with hold_typer_lock(int(args.message_id), str(args.conversation_id)) as only_typer:
            return _deliver_as_only_typer(args, conn, config) if only_typer else 0
    except StillLockedError:
        # Queued once the typer lock is let go: the typer it starts would otherwise find it held and leave.
        queue_retry_without_db(int(args.message_id), str(args.conversation_id))
        return 1


def _deliver_as_only_typer(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    message_id, conversation_id = int(args.message_id), str(args.conversation_id)
    result = deliver_typed(conn, config, message_id, conversation_id)
    if result in (None, "typed", "unwanted"):
        return 0

    def record() -> tuple[bool, tuple[int, Recipient] | None]:
        with transaction(conn):
            record_failed_delivery(
                conn, message_id, conversation_id, now_ms(), unproven=result == "unproven", stuck=result == "stuck"
            )
            if result == "stuck":
                record_stuck_delivery(conn, message_id, conversation_id, now_ms())
            # A busy tab, the owner at it, or their draft in the box holds the message by design, and its retry follows.
            first = result in ("failed", "stuck") and claim_failure_log(conn, message_id, conversation_id, now_ms())
            if result == "draft":
                return first, tell_assistant_of_draft(conn, config, message_id, conversation_id, now_ms())
        return first, None

    what = f"#{message_id} to {conversation_id}: record {result}"
    first, told = retry_when_locked(what, record, LOCKED_PATIENT_DELAYS_S)
    if first:
        log_error("deliver", f"message #{message_id} to {conversation_id} not typed: {result}")
    if told is not None:
        spawn_typed_delivery(told[0], [told[1]])
    return 0


def cmd_host_tab(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    conn.close()
    return run_tab_loop(str(args.worker), WORKER_API_PORT, config.agtermctl)


def cmd_pin_restore(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    instance_id, conversation_id = str(args.instance_id), str(args.conversation_id)
    pid_text, _, start_text = instance_id.partition("@")
    pid = int(pid_text)
    session_file = None
    for _ in range(PIN_SESSION_FILE_RETRIES):
        session_file = read_session_file(pid)
        if session_file is not None:
            break
        time.sleep(0.3)
    if session_file is None or session_file.kind != "interactive":
        return 0
    proc = process_args(pid)
    if proc is None:
        log_error("pin-restore", f"cannot read argv of pid {pid}")
        return 1
    if is_nested_agent(pid, proc.env) or {"-p", "--print"} & set(proc.argv):
        return 0
    # Platform binaries hide their environment; the hook that spawned us inherited the same agterm ids.
    env = {**{k: v for k, v in os.environ.items() if k.startswith("AGTERM_")}, **proc.env}
    surface = env.get("AGTERM_PANE_ID", "none")
    lock_dir = state_dir() / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / f"pin-{surface}.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # A newer start/clear/resume may have superseded this job while it waited.
        row = conn.execute(
            "SELECT conversation_id, ended_at FROM instances WHERE instance_id = ?", (instance_id,)
        ).fetchone()
        start = process_start_ms(pid)
        if (
            row is None
            or row["ended_at"] is not None
            or row["conversation_id"] != conversation_id
            or start is None
            or str(start) != start_text
        ):
            return 0
        command = build_restore_command(proc.argv, conversation_id)
        result = pin_restore_command(command, env, config.agtermctl)
        if not result.ok:
            log_error("pin-restore", f"{command}\n{result.detail}")
            return 1
        with transaction(conn):
            conn.execute(
                "UPDATE instances SET restore_command = ?, restore_pinned_at = ? WHERE instance_id = ?",
                (command, now_ms(), instance_id),
            )
    return 0


def map_sessions_to_lines(sessions: list[SessionView], me: str | None, now: int) -> list[str]:
    lines = [f"  {'HANDLE':20} {'STATE':7} {'LAST':>5}  {'BRANCH':28} FOCUS (» last prompt)"]
    for s in sessions:
        marker = "*" if s.instance_id == me else " "
        handle = s.handle or s.tab_name or f"pid:{s.pid}"
        last = format_age(now - s.last_activity_at) if s.last_activity_at else "-"
        focus = s.focus or (f"» {s.last_prompt}" if s.last_prompt else "")
        lines.append(f"{marker} {handle[:20]:20} {s.status:7} {last:>5}  {(s.branch or '-')[:28]:28} {focus[:90]}")
        if s.refs:
            lines.append(f"  {'':20} {'':7} {'':5}  refs: {' '.join(s.refs[:8])}")
    return lines


def _current_instance_id() -> str | None:
    pid_text = os.environ.get("CLAUDE_PID", "")
    if not pid_text.isdigit():
        return None
    start = process_start_ms(int(pid_text))
    return make_instance_id(int(pid_text), start) if start is not None else None


def _conversation_of(conn: sqlite3.Connection, instance_id: str) -> str:
    row = conn.execute("SELECT conversation_id FROM instances WHERE instance_id = ?", (instance_id,)).fetchone()
    if row is None or row["conversation_id"] is None:
        raise UsageError("this session has no known conversation yet; send one prompt first")
    return str(row["conversation_id"])
