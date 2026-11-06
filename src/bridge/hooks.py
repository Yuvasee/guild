import json
import os
import sqlite3
import subprocess
import sys
import time
import traceback
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from bridge.board import (
    INJECT_BUDGET_BYTES,
    is_delivery_owner,
    next_hint_epoch,
    retarget_open_messages,
    take_delivery,
    write_hint,
)
from bridge.claims import take_expired_claim_reminder
from bridge.claude import read_session_file
from bridge.compaction import (
    PHASE_COMPACTING,
    PHASE_CONTINUE,
    PHASE_REQUESTED,
    estimate_pending_tokens,
    give_up_driving,
    is_near_threshold,
    load_keep,
    map_env_to_can_drive,
    read_context_tokens,
    read_phase,
    record_context,
    reset_after_compaction,
    take_phase,
)
from bridge.config import Config, db_path, load_config
from bridge.db import HOOK_BUSY_TIMEOUT_MS, HOOK_RETRY_BUSY_TIMEOUT_MS, connect, transaction
from bridge.errlog import log_error
from bridge.feed import release_dead_typing_claims
from bridge.gitinfo import read_git_info
from bridge.guard import (
    add_merge_base_targets,
    find_conflicts,
    find_merge_blocks,
    map_command_to_targets,
    map_merge_blocks_to_lines,
)
from bridge.inject import Packed, Part, add_part, cap_context, pack_parts, save_later_parts, take_later_parts
from bridge.leads import (
    forget_assistant_role,
    forget_lead_role,
    is_conversation_locked,
    take_assistant_role,
    take_lead_role,
)
from bridge.procinfo import process_args, process_start_ms
from bridge.refs import extract_refs
from bridge.registry import (
    Observation,
    ObserveResult,
    add_branch_refs,
    add_refs,
    is_nested_agent,
    is_seen_prompt,
    make_instance_id,
    map_env_to_surface,
    mark_ended,
    observe_instance,
    record_activity,
    record_prompt,
    record_reply,
    set_attention,
    set_start_source,
    take_english_name_nudge,
    take_focus_nudge,
)
from bridge.start_context import (
    KEEP_COMMAND,
    UNREADABLE_START_LINE,
    PendingStart,
    TakenStart,
    clear_pending_start,
    convert_assistant_to_parts,
    convert_keep_to_part,
    convert_lead_to_parts,
    finish_taken_start,
    has_pending_start,
    map_packed_to_keep_status,
    put_back_taken_start,
    read_start_parts,
    take_pending_start,
    write_pending_start,
)

HOOK_EVENTS = (
    "SessionStart", "UserPromptSubmit", "Stop", "SessionEnd", "PostToolUse", "PostToolUseFailure", "Notification",
    "PreToolUse",
)  # fmt: skip
_DELIVERY_EVENTS = frozenset({"SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure"})
_CONTEXT_EVENTS = frozenset({"UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop"})
_PIN_SOURCES = frozenset({"startup", "resume", "clear", "fork"})
_NON_TERMINAL_END_REASONS = frozenset({"clear", "resume", "compact"})
_HOOK_RUNS_KEPT = 5000
_MAIL_MIN_ROOM = 300
_HARNESS_PROMPT_PREFIXES = (
    "<task-notification>", "<system-reminder>", "<local-command", "<command-name>", "[bridge]",
)  # fmt: skip


@dataclass(frozen=True)
class HookResult:
    instance_id: str | None
    output: str
    outcome: str
    pin_conversation: str | None = None


def main() -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    if event not in HOOK_EVENTS:
        log_error("hook", f"unknown hook event {event!r}")
        return 0
    return run_hook(event)


def run_hook(event: str) -> int:
    """Entry point for every hook: fail open, never block the session, always exit 0."""
    event_ns = time.time_ns()
    output = ""
    try:
        payload = map_stdin_to_payload(sys.stdin.read())
        config = load_config()
        conn = connect(db_path(), HOOK_BUSY_TIMEOUT_MS)
        try:
            if event == "PreToolUse":
                result = HookResult(None, handle_pre_tool_use(conn, payload, config), "ok")
            elif event == "SessionStart":
                try:
                    result = handle_hook(conn, event, payload, os.environ, config, event_ns, measure=True)
                except Exception:
                    log_error(f"hook {event}", traceback.format_exc())
                    result = handle_failed_start(conn, payload, os.environ, config, event_ns)
            else:
                result = handle_hook(conn, event, payload, os.environ, config, event_ns, measure=True)
        finally:
            conn.close()
        output = result.output
        if result.pin_conversation and result.instance_id:
            spawn_pin_restore(result.instance_id, result.pin_conversation)
    except Exception:
        log_error(f"hook {event}", traceback.format_exc())
    if output:
        sys.stdout.write(output)
    return 0


def handle_pre_tool_use(conn: sqlite3.Connection, payload: dict[str, object], config: Config) -> str:
    """Denies a shell command that would mutate a resource another live session has claimed, or merge a PR the owner's
    rule holds."""
    tool_input = payload.get("tool_input")
    command = cast(dict[str, object], tool_input).get("command") if isinstance(tool_input, dict) else None
    if payload.get("tool_name") != "Bash" or not isinstance(command, str):
        return ""
    unreadable: list[str] = []
    targets = map_command_to_targets(command, _str(payload, "cwd") or os.getcwd(), config.guards, unreadable)
    all_targets = add_merge_base_targets(conn, targets)
    conflicts = find_conflicts(conn, all_targets, _str(payload, "session_id"))
    blocks = find_merge_blocks(conn, config, targets, time.time_ns() // 1_000_000)
    if not conflicts and not blocks:
        return ""
    lines = map_merge_blocks_to_lines(blocks, config.owner_name, config.owner_name_at_start)
    if not conflicts:
        return _deny("\n".join(lines))
    lines += [
        f"bridge: {h.resource} is reserved by session {h.holder_handle}"
        + (f" ({h.reason})" if h.reason else "")
        + "; this command would change it."
        for h in conflicts
    ]
    holder = conflicts[0]
    lines.append(
        f'Ask the holder first: bridge send {holder.holder_handle} "need {holder.resource} for …, can you release it?" '
        "--request — then retry once they release. If the user explicitly told you to go ahead anyway, tell the "
        "user it is blocked by this reservation; only the user may take it over."
    )
    return _deny("\n".join(lines))


def _deny(reason: str) -> str:
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    )


def handle_hook(
    conn: sqlite3.Connection,
    event: str,
    payload: dict[str, object],
    env: Mapping[str, str],
    config: Config,
    event_ns: int,
    measure: bool = False,
) -> HookResult:
    pid_text = env.get("CLAUDE_PID", "")
    if not pid_text.isdigit():
        return HookResult(None, "", "no-pid")
    pid = int(pid_text)
    proc_start = process_start_ms(pid)
    if proc_start is None:
        return HookResult(None, "", "no-process")
    conversation_id = _str(payload, "session_id")
    cwd = _str(payload, "cwd")
    at_ms = event_ns // 1_000_000
    # Everything that touches the filesystem happens before the write lock is taken.
    session_file = read_session_file(pid)
    proc = process_args(pid)
    nested = proc is not None and is_nested_agent(pid, proc.env)
    git = read_git_info(cwd) if cwd else None
    pr_repo = config.pr_repo_for(git.repo if git else None)
    transcript_path = _str(payload, "transcript_path")
    # A subagent's hooks carry the parent's session and transcript, but its tool results never reach the main
    # context and nothing injected there reaches the main conversation: no context reading, mail or roles.
    in_subagent = bool(_str(payload, "agent_id"))
    reading = (
        read_context_tokens(Path(transcript_path))
        if config.compact_at_tokens and transcript_path and event in _CONTEXT_EVENTS and not in_subagent
        else None
    )
    text = _str(payload, "prompt") if event == "UserPromptSubmit" else _str(payload, "last_assistant_message")
    refs = extract_refs(text, issue_prefixes=config.issue_prefixes, default_repo=pr_repo) if text else []
    observation = Observation(
        pid=pid,
        proc_start_ms=proc_start,
        conversation_id=conversation_id,
        cwd=cwd,
        surface=None if nested else map_env_to_surface(env),
        kind=session_file.kind if session_file else None,
        peer_name=session_file.name if session_file else None,
        version=session_file.version if session_file else None,
        peer_messaging=session_file.peer_messaging if session_file else None,
        transcript_path=_str(payload, "transcript_path"),
        git=git,
        may_switch_conversation=event in ("SessionStart", "UserPromptSubmit"),
    )
    # A start whose own hook failed is made up by the conversation's next hook that can reach it.
    may_make_up = bool(
        conversation_id
        and event in _DELIVERY_EVENTS
        and event != "SessionStart"
        and not in_subagent
        and has_pending_start(conversation_id)
    )
    taken: TakenStart | None = None
    pending: PendingStart | None = None
    starting = event == "SessionStart"
    start_recorded = False
    contexts: list[str] = []
    restored: Part | None = None
    packed: Packed | None = None
    surface = observation.surface.surface if observation.surface else None
    drive: str | None = None
    can_drive = not nested and map_env_to_can_drive(env)
    pin_conversation: str | None = None
    hint_due: int | None = None
    # Under heavy load a holder's few milliseconds stretch past the first wait; one longer try keeps this turn's mail.
    with (
        _settle_taken_start(conversation_id, lambda: taken),
        transaction(conn, retry_busy_timeout_ms=HOOK_RETRY_BUSY_TIMEOUT_MS),
    ):
        prompt_id = _str(payload, "prompt_id")
        replayed = bool(conversation_id and prompt_id and is_seen_prompt(conn, conversation_id, prompt_id))
        if event == "UserPromptSubmit" and (replayed or is_harness_prompt(text or "")):
            # Replayed or harness prompts must not move the tab back to the conversation they came from.
            observation = replace(observation, may_switch_conversation=False)
        observed = observe_instance(conn, observation, at_ms, event_ns)
        instance_id = observed.instance_id
        if may_make_up and conversation_id and observed.current:
            # Taken under the write lock, so of two hooks racing for it only one makes the start up.
            taken = take_pending_start(conversation_id)
            if taken is not None:
                pending = taken.pending
                starting = True
                restored = record_made_up_start(conn, conversation_id, pending, observed, surface)
        if event == "SessionStart":
            start_recorded = observed.current
            forget_lead_role(conn, surface)
            forget_assistant_role(conn, surface)
            source = _str(payload, "source") or "startup"
            if conversation_id:
                set_start_source(conn, conversation_id, source)
            # Compaction happens mid-turn; it must not flip a working session to idle.
            if source != "compact" and observed.current:
                conn.execute("UPDATE instances SET ended_at = NULL WHERE instance_id = ?", (instance_id,))
                record_activity(conn, instance_id, "idle", event_ns)
            previous = observed.previous_conversation_id
            if source == "clear" and observed.current and conversation_id and previous and previous != conversation_id:
                retarget_open_messages(conn, previous, conversation_id, instance_id)
            if source == "compact" and conversation_id and observed.current:
                reset_after_compaction(conn, conversation_id, at_ms)
                # Only a compaction bridge started is followed by its "continue"; any other one ends the cycle.
                if take_phase(conn, conversation_id, PHASE_COMPACTING, PHASE_CONTINUE):
                    drive = "continue"
                take_phase(conn, conversation_id, PHASE_REQUESTED, None)
                kept = load_keep(conn, conversation_id)
                if kept:
                    restored = convert_keep_to_part(kept[0], kept[1], at_ms)
            if config.pin_restore and not nested and source in _PIN_SOURCES and env.get("AGTERM_SESSION_ID"):
                pin_conversation = conversation_id
        elif event == "UserPromptSubmit" and conversation_id and is_harness_prompt(text or ""):
            # Task notifications and similar harness messages arrive as prompts but are not the user's words.
            if observed.current:
                record_activity(conn, instance_id, "active", event_ns)
        elif event == "UserPromptSubmit" and conversation_id:
            fresh = record_prompt(conn, conversation_id, text or "", prompt_id, at_ms)
            # The user picked the conversation up again; a typed "continue" would now land on top of their work.
            take_phase(conn, conversation_id, PHASE_CONTINUE, None)
            if fresh and observed.current:
                record_activity(conn, instance_id, "active", event_ns)
            if fresh:
                add_refs(conn, conversation_id, refs, "prompt", at_ms)
                add_branch_refs(conn, conversation_id, git.branch if git else None, config, at_ms)
                nudge = take_focus_nudge(conn, conversation_id, config, at_ms) if observed.current else None
                if nudge:
                    contexts.append(nudge)
        elif event == "Stop":
            if observed.current:
                record_activity(conn, instance_id, "idle", event_ns)
            if (
                conversation_id
                and observed.current
                and take_phase(conn, conversation_id, PHASE_REQUESTED, PHASE_COMPACTING)
            ):
                drive = "compact"
            if conversation_id and text:
                record_reply(conn, conversation_id, text, at_ms)
                add_refs(conn, conversation_id, refs, "reply", at_ms)
        elif event == "Notification":
            if observed.current:
                set_attention(conn, instance_id, "permission", event_ns)
            # The next tool call must reach Python so the attention flag clears promptly.
            hint_due = 0
        elif event == "SessionEnd":
            if observed.current and _str(payload, "reason") not in _NON_TERMINAL_END_REASONS:
                mark_ended(conn, instance_id, at_ms, event_ns)
        if reading is not None and conversation_id and observed.current:
            warning = record_context(
                conn,
                conversation_id,
                reading,
                # The usage predates this prompt or tool result; a large one can jump straight to compaction.
                estimate_pending_tokens(event, payload),
                config.compact_at_tokens,
                can_deliver=event in _DELIVERY_EVENTS,
                driven=can_drive,
            )
            if warning:
                contexts.append(warning)
        if event in _DELIVERY_EVENTS and not in_subagent:
            leftover = False
            # A locked tab is left out: its mail waits, unacknowledged, and it gets no reminders.
            owner = bool(
                conversation_id
                and observed.current
                and is_delivery_owner(conn, conversation_id, instance_id)
                and not is_conversation_locked(conn, config, conversation_id)
            )
            # What waited from the last message goes first; a new start rebuilds roles and notes, so those copies go.
            parts = take_later_parts(conn, conversation_id) if owner and conversation_id else []
            if starting:
                parts = [p for p in parts if not p.rebuilt]
            if pending is not None:
                parts = [*pending.later, *parts]
            if restored:
                # First of all, even what waited: alone it always fits, so the list is never left waiting.
                parts.insert(0, restored)
            # The start's fallback already gave the role and notes; taking them here only records that.
            give_roles = not (pending is not None and pending.shown)
            if owner and conversation_id:
                for note in (
                    take_expired_claim_reminder(conn, conversation_id, at_ms),
                    take_english_name_nudge(conn, surface),
                ):
                    if note:
                        contexts.append(note)
                lead = take_lead_role(conn, config, surface, conversation_id)
                if lead and give_roles:
                    parts += convert_lead_to_parts(lead, config)
                if take_assistant_role(conn, config, surface, conversation_id) and give_roles:
                    parts += convert_assistant_to_parts(conn, config)
            packed = pack_parts([*parts, *(Part(c) for c in contexts)])
            if owner and conversation_id and packed.room < _MAIL_MIN_ROOM:
                # Taking mail marks it offered; with no room it waits for the next event instead.
                leftover = True
                save_later_parts(conn, conversation_id, packed.later)
            elif owner and conversation_id:
                # Notices and mail share the inbox budget; roles and notes stay outside it, as their own memory.
                spent = sum(len(c.encode()) + 2 for c in contexts)
                budget = min(INJECT_BUDGET_BYTES - spent, packed.room)
                release_dead_typing_claims(conn, at_ms, conversation_id)
                delivery = take_delivery(conn, conversation_id, at_ms, starting, budget_bytes=budget)
                if delivery.context:
                    packed = add_part(packed, Part(delivery.context, "your bridge messages", "bridge inbox"))
                leftover = delivery.leftover
                save_later_parts(conn, conversation_id, packed.later)
            elif conversation_id and packed.later:
                save_later_parts(conn, conversation_id, take_later_parts(conn, conversation_id) + packed.later)
            # Waiting parts must reach the very next tool result, not one 30 s later.
            hint_due = next_hint_epoch(leftover or bool(packed.later))
            if conversation_id and is_near_threshold(conn, conversation_id, config.compact_at_tokens):
                # Close to the warning, one long tool run could reach compaction between 30 s checks.
                hint_due = int(time.time())
        if measure:
            record_hook_run(conn, event, instance_id, at_ms)
    if start_recorded and conversation_id:
        try:
            clear_pending_start(conversation_id)
        except OSError:
            log_error("start-pending", traceback.format_exc())
    if hint_due is not None:
        try:
            write_hint(pid, hint_due)
        except OSError:
            log_error("hint", traceback.format_exc())
    keep = map_packed_to_keep_status(restored, packed)
    if drive and conversation_id and instance_id and not spawn_compact_drive(drive, instance_id, conversation_id, keep):
        try:
            with transaction(conn):
                give_up_driving(conn, conversation_id)
        except sqlite3.Error:
            log_error("compact-drive", traceback.format_exc())
    text = packed.text if packed else "\n\n".join(contexts)
    output = convert_context_to_hook_output(event, cap_context(text)) if text else ""
    return HookResult(instance_id, output, "ok", pin_conversation)


def record_made_up_start(
    conn: sqlite3.Connection, conversation_id: str, pending: PendingStart, observed: ObserveResult, surface: str | None
) -> Part | None:
    """Records a start its own hook could not; returns the saved list when it has not reached the session yet."""
    forget_lead_role(conn, surface)
    forget_assistant_role(conn, surface)
    set_start_source(conn, conversation_id, pending.source)
    previous = observed.previous_conversation_id
    if pending.source == "clear" and previous and previous != conversation_id:
        retarget_open_messages(conn, previous, conversation_id, observed.instance_id)
    if pending.source != "compact":
        return None
    reset_after_compaction(conn, conversation_id, pending.at_ms)
    # The session moves again by itself, so a compaction bridge drove ends here, a "continue" not yet typed with it.
    for phase in (PHASE_COMPACTING, PHASE_CONTINUE, PHASE_REQUESTED):
        take_phase(conn, conversation_id, phase, None)
    kept = None if pending.shown else load_keep(conn, conversation_id)
    return convert_keep_to_part(kept[0], kept[1], pending.at_ms) if kept else None


@contextmanager
def _settle_taken_start(conversation_id: str | None, taken: Callable[[], TakenStart | None]) -> Generator[None]:
    """A start taken for making up is done once its transaction commits; otherwise it goes back for the next hook."""
    try:
        yield
    except BaseException:
        if (start := taken()) is not None and conversation_id:
            try:
                put_back_taken_start(conversation_id, start)
            except OSError:
                log_error("start-pending", traceback.format_exc())
        raise
    if (start := taken()) is not None:
        try:
            finish_taken_start(start)
        except OSError:
            log_error("start-pending", traceback.format_exc())


def handle_failed_start(
    conn: sqlite3.Connection, payload: dict[str, object], env: Mapping[str, str], config: Config, event_ns: int
) -> HookResult:
    """A start the database could not record: its parts are read without the write lock and given now, and the
    conversation's next hook records it. If even reading fails, the session is told where its context is."""
    conversation_id = _str(payload, "session_id")
    pid_text = env.get("CLAUDE_PID", "")
    proc_start = process_start_ms(int(pid_text)) if pid_text.isdigit() else None
    if not conversation_id or proc_start is None:
        return HookResult(None, "", "error")
    pid = int(pid_text)
    instance_id = make_instance_id(pid, proc_start)
    proc = process_args(pid)
    nested = proc is not None and is_nested_agent(pid, proc.env)
    observed_surface = None if nested else map_env_to_surface(env)
    surface = observed_surface.surface if observed_surface else None
    source = _str(payload, "source") or "startup"
    at_ms = event_ns // 1_000_000
    pending = PendingStart(source, at_ms, False, [])
    text = UNREADABLE_START_LINE
    drive_keep: str | None = None
    try:
        parts = read_start_parts(conn, config, surface, conversation_id, source, at_ms)
        packed = pack_parts(parts)
        pending = PendingStart(source, at_ms, True, packed.later)
        text = packed.text
        if source == "compact" and read_phase(conn, conversation_id) == PHASE_COMPACTING:
            kept = next((p for p in parts if p.command == KEEP_COMMAND), None)
            drive_keep = map_packed_to_keep_status(kept, packed)
    except Exception:
        log_error("hook SessionStart fallback", traceback.format_exc())
        # The drive types only into a compaction it started, so with the phase unread it may still be ours.
        drive_keep = "unread" if source == "compact" else None
    try:
        write_pending_start(conversation_id, pending)
        # The next tool result must reach Python to record the start, not one 30 s later.
        write_hint(pid, int(time.time()))
    except OSError:
        log_error("start-pending", traceback.format_exc())
    if drive_keep and not nested and map_env_to_can_drive(env):
        spawn_compact_drive("continue", instance_id, conversation_id, drive_keep, claim=True)
    pin = config.pin_restore and not nested and source in _PIN_SOURCES and bool(env.get("AGTERM_SESSION_ID"))
    output = convert_context_to_hook_output("SessionStart", cap_context(text)) if text else ""
    return HookResult(instance_id, output, "fallback", conversation_id if pin else None)


def is_harness_prompt(text: str) -> bool:
    return text.lstrip().startswith(_HARNESS_PROMPT_PREFIXES)


def record_hook_run(conn: sqlite3.Connection, event: str, instance_id: str, at_ms: int) -> None:
    """Records wall time since this process started, so interpreter start-up and lock waits are included."""
    own_start = process_start_ms(os.getpid())
    duration_us = (time.time_ns() // 1000 - own_start * 1000) if own_start else 0
    cursor = conn.execute(
        "INSERT INTO hook_runs (at, event, instance_id, duration_us, outcome) VALUES (?, ?, ?, ?, 'ok')",
        (at_ms, event, instance_id, duration_us),
    )
    if cursor.lastrowid and cursor.lastrowid % 500 == 0:
        conn.execute("DELETE FROM hook_runs WHERE id <= ?", (cursor.lastrowid - _HOOK_RUNS_KEPT,))


def spawn_compact_drive(phase: str, instance_id: str, conversation_id: str, keep: str, claim: bool = False) -> bool:
    # Detached: it waits for the tab to be idle, which happens only after this hook returns.
    argv = [sys.executable, "-m", "bridge", "_compact-drive", phase, instance_id, conversation_id, "--keep", keep]
    try:
        subprocess.Popen(
            [*argv, *(["--claim"] if claim else [])],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        log_error("compact-drive", traceback.format_exc())
        return False
    return True


def spawn_pin_restore(instance_id: str, conversation_id: str) -> None:
    # Detached so the agterm round-trip never counts against hook latency.
    subprocess.Popen(
        [sys.executable, "-m", "bridge", "_pin-restore", instance_id, conversation_id],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def convert_context_to_hook_output(event: str, context: str) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}})


def map_stdin_to_payload(text: str) -> dict[str, object]:
    raw: object = json.loads(text) if text.strip() else {}
    if not isinstance(raw, dict):
        raise ValueError("hook payload is not a JSON object")
    return cast(dict[str, object], raw)


def _str(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None
