import argparse
import fcntl
import json
import os
import sqlite3
import statistics
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict
from pathlib import Path

from bridge.board import Party, Recipient, add_user_item
from bridge.claude import read_session_file
from bridge.cli_board import add_board_commands
from bridge.cli_claims import add_claim_commands
from bridge.compaction import (
    CONTINUE_PROMPTS,
    KEEP_MAX_CHARS,
    PHASE_COMPACTING,
    PHASE_CONTINUE,
    give_up_driving,
    list_dropped_lines,
    load_keep,
    read_phase,
    save_keep,
    take_phase,
)
from bridge.config import Config, db_path, load_config, state_dir
from bridge.db import connect, now_ms, transaction
from bridge.dream import resolve_dreamers, run_dream, run_night, stop_claude_on_term
from bridge.enrich import enrich_due
from bridge.errlog import log_error
from bridge.errors import UsageError
from bridge.feed import (
    LOCKED_PATIENT_DELAYS_S,
    StillLockedError,
    deliver_typed,
    hold_typer_lock,
    queue_retry_without_db,
    record_failed_delivery,
    record_stuck_delivery,
    retry_when_locked,
    spawn_typed_delivery,
    tell_assistant_of_draft,
)
from bridge.guard import DEFAULT_TRIGGERS
from bridge.identity import ensure_current_instance
from bridge.inject import measure_chars
from bridge.install import find_hook_executable, install_hooks, uninstall_hooks
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
from bridge.remote import collect
from bridge.remote import store as store_remote
from bridge.restore import build_restore_command, pin_restore_command
from bridge.review_post import run_review_post
from bridge.review_slack import run_connector_read
from bridge.serve import DEFAULT_PORT, serve
from bridge.service import install_service, restart_service, uninstall_service
from bridge.tabinput import is_tab_selected, type_when_idle
from bridge.trackers import load_tracker

DEFAULT_SETTINGS = Path.home() / ".claude/settings.json"
PIN_SESSION_FILE_RETRIES = 10
COMPACT_UNPROVEN_WAITS = 6
COMPACT_CLAIM_TRIES = 6
"""Each waits the connection's busy timeout: half a minute in all."""


def main(argv: list[str] | None = None) -> int:
    config = load_config()
    args = build_parser(config).parse_args(argv)
    if args.func is cmd_deliver:
        # Detached, with nobody to see it fail: a lock that outlasts connect's busy timeout must not end it unlogged.
        try:
            conn = retry_when_locked("_deliver: open the database", lambda: connect(db_path()), LOCKED_PATIENT_DELAYS_S)
        except StillLockedError:
            queue_retry_without_db(int(args.message_id), str(args.conversation_id))
            return 1
    else:
        conn = connect(db_path())
    try:
        return int(args.func(args, conn, config))
    except UsageError as exc:
        print(f"bridge: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()


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

    p = sub.add_parser("hook-stats", help="hook latency percentiles")
    p.add_argument("--last", type=int, default=1000)
    p.set_defaults(func=cmd_hook_stats)

    p = sub.add_parser("install-hooks", help="add bridge hooks to Claude Code settings")
    p.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    p.set_defaults(func=cmd_install_hooks)

    p = sub.add_parser("uninstall-hooks", help="remove bridge hooks from Claude Code settings")
    p.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    p.set_defaults(func=cmd_uninstall_hooks)

    add_board_commands(sub, config)
    add_claim_commands(sub, config)

    p = sub.add_parser("enrich", help="refresh GitHub PR and tracker issue state for live sessions")
    p.add_argument("--force", action="store_true", help="ignore freshness and refetch everything")
    p.set_defaults(func=cmd_enrich)

    p = sub.add_parser("remote-sync", help="collect sessions from configured remote hosts now")
    p.set_defaults(func=cmd_remote_sync)

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

    p = sub.add_parser("open", help="open the dashboard as an agterm overlay (or in the browser)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--target", help="agterm session id to overlay (default: this tab)")
    p.add_argument("--browser", action="store_true")
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("_compact-drive", help=argparse.SUPPRESS)
    p.add_argument("phase", choices=("compact", "continue"))
    p.add_argument("--keep", choices=tuple(CONTINUE_PROMPTS), default="shown")
    # From a start whose hook could not write: the drive moves the cycle to "continue" itself.
    p.add_argument("--claim", action="store_true")
    p.add_argument("instance_id")
    p.add_argument("conversation_id")
    p.set_defaults(func=cmd_compact_drive)

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
        for s in list_sessions(conn, config, now, include_ended=bool(args.all))
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
    view = next((s for s in list_sessions(conn, config, now) if s.instance_id == instance_id), None)
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


def cmd_hook_stats(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    rows = conn.execute(
        "SELECT event, duration_us FROM hook_runs ORDER BY id DESC LIMIT ?", (int(args.last),)
    ).fetchall()
    by_event: dict[str, list[int]] = {}
    for row in rows:
        by_event.setdefault(str(row["event"]), []).append(int(row["duration_us"]))
    print("wall time from hook process start (includes interpreter start-up and lock waits)")
    for event, durations in sorted(by_event.items()):
        print(f"{event:18} n={len(durations):5}  {format_percentiles(durations)}")
    errors = state_dir() / "errors.log"
    text = errors.read_text() if errors.exists() else ""
    print(
        f"dropped on a locked database: {text.count('database is locked')}; other hook errors: "
        f"{text.count('Traceback') - text.count('database is locked')} (see {errors})"
    )
    return 0


def cmd_install_hooks(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    executable = find_hook_executable()
    triggers = (*DEFAULT_TRIGGERS, *(g.trigger for g in config.guards))
    events = install_hooks(Path(args.settings), executable, triggers)
    print(f"installed {', '.join(events)} hooks → {executable} in {args.settings}")
    return 0


def cmd_uninstall_hooks(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    uninstall_hooks(Path(args.settings))
    print(f"removed bridge hooks from {args.settings}")
    return 0


def cmd_enrich(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    counts = enrich_due(conn, config, now_ms(), force=bool(args.force))
    tracker = load_tracker(config)
    where = tracker.describe() if tracker else "tracker off"
    print(f"refreshed {counts['prs']} PRs and {counts['issues']} issues ({where})")
    errors = conn.execute("SELECT ref, error FROM enrichment WHERE error IS NOT NULL ORDER BY ref").fetchall()
    for row in errors:
        print(f"  {row['ref']}: {row['error']}")
    return 0


def cmd_remote_sync(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    if not config.remotes:
        raise UsageError("no [[remotes]] in config.toml")
    for remote in config.remotes:
        result = collect(remote)
        store_remote(conn, remote, result, config, now_ms())
        print(
            f"{remote.name}: {len(result.sessions)} agent sessions" if result.ok else f"{remote.name}: {result.error}"
        )
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


def cmd_open(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    url = f"http://127.0.0.1:{args.port}/"
    if not dashboard_is_up(url):
        log = (state_dir() / "serve.log").open("a")
        subprocess.Popen(
            [sys.executable, "-m", "bridge", "serve", "--port", str(args.port)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        deadline = time.monotonic() + 5
        while not dashboard_is_up(url):
            if time.monotonic() > deadline:
                raise UsageError(f"dashboard did not start; see {state_dir() / 'serve.log'}")
            time.sleep(0.1)
    target = args.target or os.environ.get("AGTERM_SESSION_ID")
    if args.browser or not target:
        subprocess.run(["open", url], check=False)
        print(url)
        return 0
    command = [config.agtermctl, "session", "overlay", "open", "--url", url, "--js", "--target", target]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise UsageError(f"agterm overlay failed: {(completed.stdout + completed.stderr).strip()}")
    print(f"dashboard {url} opened over agterm session {target}")
    return 0


def dashboard_is_up(url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "api/state", timeout=2) as response:
            return response.status == 200
    except OSError:
        return False


def claim_compaction(conn: sqlite3.Connection, conversation_id: str) -> bool:
    """Started by a start whose hook could not write, so the lock may stay held a while yet."""
    for attempt in range(COMPACT_CLAIM_TRIES):
        try:
            with transaction(conn):
                return take_phase(conn, conversation_id, PHASE_COMPACTING, PHASE_CONTINUE)
        except sqlite3.OperationalError:
            if attempt == COMPACT_CLAIM_TRIES - 1:
                # The conversation's next hook ends the cycle; until then nobody types "continue".
                log_error("compact-drive", f"continue not claimed for {conversation_id}: database stayed locked")
    return False


def cmd_compact_drive(args: argparse.Namespace, conn: sqlite3.Connection, config: Config) -> int:
    """Types /compact (or the continue prompt) into the session's agterm pane once Claude Code waits for input."""
    phase, instance_id, conversation_id = str(args.phase), str(args.instance_id), str(args.conversation_id)
    session, pane = os.environ.get("AGTERM_SESSION_ID", ""), os.environ.get("AGTERM_PANE_ID", "")
    expected = PHASE_COMPACTING if phase == "compact" else PHASE_CONTINUE
    command = "/compact" if phase == "compact" else CONTINUE_PROMPTS[str(args.keep)]
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
        status = conn.execute("SELECT status FROM instances WHERE instance_id = ?", (instance_id,)).fetchone()
        return status is not None and status["status"] == "idle" and not is_tab_selected(config, session)

    result = type_when_idle(config, session, pane, command, still_wanted, may_probe_box=may_probe_box)
    for _ in range(COMPACT_UNPROVEN_WAITS):
        # Most likely Claude Code's grey suggestion in a selected tab: wait for them to leave it, telling nobody.
        if result != "unproven" or not still_wanted():
            break
        result = type_when_idle(config, session, pane, command, still_wanted, may_probe_box=may_probe_box)
    if result == "typed":
        if phase == "continue":
            with transaction(conn):
                take_phase(conn, conversation_id, PHASE_CONTINUE, None)
        return 0
    if result == "unwanted" and read_phase(conn, conversation_id) != expected:
        return 0
    log_error("compact-drive", f"{phase} not typed for {conversation_id}: {result}")
    with transaction(conn):
        if read_phase(conn, conversation_id) == expected:
            give_up_driving(conn, conversation_id)
            handle = conn.execute(
                "SELECT s.handle FROM instances i LEFT JOIN surfaces s ON s.surface = i.surface"
                " WHERE i.instance_id = ?",
                (instance_id,),
            ).fetchone()
            name = str(handle["handle"]) if handle and handle["handle"] else conversation_id[:8]
            what = "/compact" if phase == "compact" else "continue after compaction"
            add_user_item(
                conn,
                Party(conversation_id, name),
                "action",
                f"bridge could not submit {what} in {name} (the tab stayed busy, or Enter did not take); do it by hand",
                now_ms(),
            )
    return 1


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
    log_error("deliver", f"message #{message_id} to {conversation_id} not typed: {result}")

    def record() -> tuple[int, Recipient] | None:
        with transaction(conn):
            record_failed_delivery(conn, message_id, conversation_id, now_ms(), unproven=result == "unproven")
            if result == "stuck":
                record_stuck_delivery(conn, message_id, conversation_id, now_ms())
            if result == "draft":
                return tell_assistant_of_draft(conn, config, message_id, conversation_id, now_ms())
        return None

    what = f"#{message_id} to {conversation_id}: record {result}"
    told = retry_when_locked(what, record, LOCKED_PATIENT_DELAYS_S)
    if told is not None:
        spawn_typed_delivery(told[0], [told[1]])
    return 0


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


def format_percentiles(durations_us: list[int]) -> str:
    ordered = sorted(durations_us)
    p50 = statistics.median(ordered) / 1000
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] / 1000
    return f"p50={p50:6.1f}ms  p95={p95:6.1f}ms  max={ordered[-1] / 1000:6.1f}ms"


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
