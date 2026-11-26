import fnmatch
import json
import os
import re
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

from bridge.agent_prefix import is_artifact_publish
from bridge.board import (
    HINT_SENT,
    INJECT_BUDGET_BYTES,
    Party,
    Recipient,
    is_delivery_owner,
    mark_user_parts_shown,
    merge_user_messages,
    next_hint_epoch,
    retarget_open_messages,
    split_user_parts,
    take_delivery,
    take_user_messages,
    write_hint,
)
from bridge.claims import record_nudges, take_expired_claim_reminder
from bridge.claude import read_session_file
from bridge.compaction import (
    COMPACT_AT_WINDOW_SHARE,
    PHASE_COMPACTING,
    PHASE_CONTINUE,
    PHASE_REQUESTED,
    WORKER_WINDOW_SHARE,
    estimate_pending_tokens,
    give_up_driving,
    is_context_full,
    is_near_threshold,
    is_request_delivered,
    load_keep,
    map_env_to_can_drive,
    read_phase,
    read_transcript_tail,
    record_context,
    record_failed_turns,
    reopen_before_clear,
    reset_after_compaction,
    take_phase,
)
from bridge.config import Config, db_path, load_config
from bridge.db import HOOK_BUSY_TIMEOUT_MS, HOOK_RETRY_BUSY_TIMEOUT_MS, connect, transaction
from bridge.entry import import_modules
from bridge.errlog import log_error, log_slow
from bridge.feed import find_cut_user_parts, release_dead_typing_claims, spawn_typed_delivery
from bridge.gitinfo import GitInfo, read_git_info, read_repo_facts
from bridge.guard import (
    CLOSE,
    MERGE,
    MERGE_CHECK_BUDGET_S,
    PUSH,
    SHELL_KEYWORDS,
    SSH_VALUE_OPTIONS,
    WRAPPED,
    HostRepoReader,
    SandboxMerge,
    Unreadable,
    add_merge_base_targets,
    find_conflicts,
    find_held_writes,
    find_merge_blocks,
    find_sandbox_merges,
    find_substitution_end,
    map_command_to_targets,
    map_held_writes_to_lines,
    map_merge_blocks_to_lines,
    map_sandbox_merges_to_lines,
    parse_gh_api_args,
    read_short_cluster,
    segment_command,
    strip_prefixes,
)
from bridge.inject import (
    Packed,
    Part,
    add_part,
    add_parts,
    cap_context,
    pack_parts,
    save_later_parts,
    take_later_parts,
)
from bridge.leads import (
    find_lead_conversations,
    find_lead_of,
    find_lead_on_surface,
    forget_assistant_role,
    forget_lead_role,
    is_assistant_surface,
    is_conversation_locked,
    is_surface_locked,
    list_tab_lineage,
    take_assistant_role,
    take_lead_role,
)
from bridge.procinfo import process_age_us, process_args, process_start_ms
from bridge.refs import extract_refs
from bridge.registry import (
    Observation,
    ObserveResult,
    Surface,
    add_branch_refs,
    add_refs,
    is_nested_agent,
    is_seen_prompt,
    make_instance_id,
    map_env_to_surface,
    mark_ended,
    mark_prompt_seen,
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
    is_nested_tab,
    map_packed_to_keep_status,
    put_back_taken_start,
    read_constitution_part,
    read_start_parts,
    take_pending_start,
    write_pending_start,
)
from bridge.turncut import take_turn_cut

# Loaded late to keep other hooks fast, but with load_main's wait: a guard check must not pass because a reinstall
# hid a module for a moment.
GUARD_LATE_MODULES = (
    "bridge.title_guard",
    "bridge.restricted",
    "bridge.review_deps",
    "bridge.review_github",
    "bridge.backport",
)

HOOK_EVENTS = (
    "SessionStart", "UserPromptSubmit", "Stop", "SessionEnd", "PostToolUse", "PostToolUseFailure", "Notification",
    "PreToolUse", "StopFailure",
)  # fmt: skip
"""StopFailure: Claude Code runs it instead of Stop when an API error ends the turn, as the Mac's sleep does."""
ARTIFACT_HOOK = "ArtifactPublish"
"""A PostToolUse hook of its own for the Artifact tool, outside the hint gate, so every publish reaches /pinned."""
_DELIVERY_EVENTS = frozenset({"SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure"})
_CONTEXT_EVENTS = frozenset({"UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop"})
_PIN_SOURCES = frozenset({"startup", "resume", "clear", "fork"})
_NON_TERMINAL_END_REASONS = frozenset({"clear", "resume", "compact"})
_HOOK_RUNS_KEPT = 5000
_MAIL_MIN_ROOM = 300
TASK_NOTIFICATION = "<task-notification>"
_RESULT_LINK_RE = re.compile(r"https?://\S|(?<![\w/])/p/[a-z0-9]")
_HARNESS_PROMPT_PREFIXES = (
    TASK_NOTIFICATION, "<system-reminder>", "<local-command", "<command-name>", "[bridge]",
)  # fmt: skip


@dataclass(frozen=True)
class HookResult:
    instance_id: str | None
    output: str
    outcome: str
    pin_conversation: str | None = None
    hint_due: int | None = None


@dataclass(frozen=True)
class HookCaller:
    """A Claude that is no process here, a host worker: it stands as its tab's local process, which lives as long."""

    pid: int
    proc_start_ms: int
    surface: Surface
    kind: str
    git: GitInfo | None


def main() -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    if event == "rewake":
        try:
            from bridge.rewake import run_rewake  # it loads the HTTP client, which hooks never use
        except ImportError:
            log_error("hook rewake", traceback.format_exc())
            return 0
        return run_rewake()
    if event == ARTIFACT_HOOK:
        return run_artifact_hook()
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
                import_modules(GUARD_LATE_MODULES)
                result = HookResult(None, handle_pre_tool_use(conn, payload, config, env=os.environ), "ok")
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


def run_artifact_hook() -> int:
    """Fails open like every hook: a publish that cannot be pinned leaves a line in errors.log, never blocks."""
    try:
        from bridge.pins import PinError  # only this hook needs it
    except ImportError:
        log_error(f"hook {ARTIFACT_HOOK}", traceback.format_exc())
        return 0
    try:
        payload = map_stdin_to_payload(sys.stdin.read())
        config = load_config()
        conn = connect(db_path(), HOOK_BUSY_TIMEOUT_MS)
        try:
            from bridge.artifacts import capture_artifact_publish  # only this hook needs it

            with transaction(conn, retry_busy_timeout_ms=HOOK_RETRY_BUSY_TIMEOUT_MS):
                capture_artifact_publish(conn, config, payload, os.environ, time.time_ns() // 1_000_000)
        finally:
            conn.close()
    except PinError as error:
        log_error(f"hook {ARTIFACT_HOOK}", str(error))
    except Exception:
        log_error(f"hook {ARTIFACT_HOOK}", traceback.format_exc())
    return 0


_KILL_BY_NAME = frozenset({"pkill", "killall"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash"})
_SHELL_VALUE_OPTIONS = frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"})
_SUDO_VALUE_OPTIONS = frozenset({"-u", "-g", "-U", "-C", "-D", "-h", "-p", "-r", "-t"})
_XARGS_VALUE_OPTIONS = frozenset({"-n", "-L", "-I", "-P", "-s", "-d", "-E", "-a"})


def find_kill_by_name_refusal(command: str, depth: int = 0) -> str | None:
    """A name or pattern also matches other sessions' processes, the owner's own and shared ones: kill by PID. Read
    with the guard's lexer, so quoted text, heredoc bodies and comments are data; a command run through ssh or a
    shell's -c is read too."""
    segments = segment_command(command)
    for index, (tokens, joined_by) in enumerate(segments):
        words = _drop_command_prefix(tokens)
        program = os.path.basename(words[0]) if words else ""
        after = segments[index + 1] if index + 1 < len(segments) else None
        before = segments[index - 1] if index else None
        if (
            program in _KILL_BY_NAME
            or (program == "kill" and any(w.startswith("`pgrep") for w in words))
            or (program == "kill" and words[-1] == "$" and after is not None and after[1] == "("
                and _program_of(after[0]) == "pgrep")
            or (program == "xargs" and joined_by in ("|", "|&") and before is not None
                and _program_of(before[0]) == "pgrep"
                and _xargs_program(words) == "kill")
        ):  # fmt: skip
            return (
                "killing by name or pattern (pkill, killall, pgrep | xargs kill, kill $(pgrep …)) can hit other "
                "sessions' and the owner's processes. Find the PID first (pgrep -fl …, ps), check it is yours, then "
                "kill <PID>."
            )
        if depth < 2:
            for text in _list_inner_commands(program, words):
                if refusal := find_kill_by_name_refusal(text, depth + 1):
                    return refusal
    return None


def _list_inner_commands(program: str, words: list[str]) -> list[str]:
    """The command lines an ssh or a shell's -c runs."""
    if program == "ssh":
        remote = _map_ssh_to_remote_command(words)
        return [remote] if remote else []
    if program in _SHELLS:
        return [
            words[at + 1]
            for at, w in enumerate(words[:-1])
            if (w[:1] == "-" and w[1:2] != "-" and "c" in w and w[1:].isalpha() and len(w) < 8)
        ]
    return []


def _map_ssh_to_remote_command(words: list[str]) -> str:
    """ssh reads options before and after the destination (-ti key, -ikey, box -t) and joins the remaining words with
    spaces, as the remote shell then reads them."""
    at, seen_host = 1, False
    while at < len(words):
        word = words[at]
        if word == "--":
            at += 1
            if seen_host:
                break
        elif word.startswith("-") and len(word) > 1:
            at += 1
            for end, letter in enumerate(word[1:], 2):
                if "-" + letter in SSH_VALUE_OPTIONS:
                    at += 1 if end == len(word) else 0
                    break
        elif not seen_host:
            seen_host = True
            at += 1
        else:
            break
    return " ".join(words[at:])


def _reads_script_on_stdin(program: str, words: list[str]) -> bool:
    """A shell with no -c and no script file, or ssh with no remote command or one such shell, runs a heredoc as
    commands."""
    words = _drop_redirections(words)
    if program == "ssh":
        remote = _map_ssh_to_remote_command(words)
        first = segment_command(remote)[:1] if remote else []
        inner = _drop_command_prefix(first[0][0]) if first else []
        shell = os.path.basename(inner[0]) if inner else ""
        return not remote or (shell in _SHELLS and _reads_script_on_stdin(shell, inner))
    if program not in _SHELLS or _list_inner_commands(program, words):
        return False
    at = 1
    while at < len(words) and words[at][:1] in "-+":
        letters = words[at][1:] if words[at][:1] == "-" and words[at][1:].isalpha() else ""
        if "n" in letters:
            return False
        if "s" in letters:
            return True
        at += 2 if words[at] in _SHELL_VALUE_OPTIONS else 1
    return at >= len(words)


def _drop_redirections(words: list[str]) -> list[str]:
    """<<EOF, << EOF, 2>&1, > out: redirections, not the program's arguments."""
    kept: list[str] = []
    skip = False
    for word in words:
        if skip:
            skip = False
        elif _REDIRECT_RE.match(word):
            skip = bool(_BARE_REDIRECT_RE.fullmatch(word))
        else:
            kept.append(word)
    return kept


_UNNAMED_CLUSTER = (
    "bridge: this command names no cluster, so it runs against this machine's default context, which differs per "
    "machine and may be production. Add --context <cluster> (helm: --kube-context <cluster>); "
    "kubectl config get-contexts lists them."
)
_DEFAULT_SWITCH = (
    "bridge: kubectl config use-context, set-context --current, kubectx and kubens change the default cluster or "
    "namespace for every session on this machine. Leave the default alone and name the cluster on each command: "
    "--context <cluster> (helm: --kube-context <cluster>)."
)
# Global options of kubectl, helm and k9s that take their value as the next word, so it is not read as a subcommand.
_CLUSTER_VALUE_OPTIONS = frozenset({
    "-n", "--namespace", "--context", "--kube-context", "--cluster", "--user", "--kubeconfig", "-s", "--server",
    "--token", "--as", "--as-group", "--as-uid", "--request-timeout", "-v", "--v", "--cache-dir",
    "--certificate-authority", "--client-certificate", "--client-key", "--tls-server-name", "--username",
    "--password", "--profile", "--profile-output", "--log-file", "--log-dir", "--kube-apiserver", "--kube-token",
    "--kube-as-user", "--kube-as-group", "--kube-ca-file", "--kube-tls-server-name", "--registry-config",
    "--repository-config", "--repository-cache", "--burst-limit", "--qps", "-c", "--command", "-l", "--logLevel",
    "--logFile", "-r", "--refresh", "-o", "--output", "--template", "--sort-by", "--field-selector", "--selector",
    "-L", "--label-columns", "--chunk-size", "--timeout", "--set", "--set-string", "--set-file", "--set-json",
    "--set-literal", "--values", "--raw",
})  # fmt: skip
_CLUSTER_VALUE_LETTERS = frozenset("nsvclroL")
# Root booleans of kubectl, helm and k9s: before the verb, any other flag written without = takes the next word, as
# cobra reads it (kubectl -f web.yaml apply).
_CLUSTER_ROOT_BOOLEANS = frozenset({
    "-h", "--help", "--insecure-skip-tls-verify", "--match-server-version", "--warnings-as-errors",
    "--disable-compression", "--debug", "-A", "--all-namespaces", "--headless", "--logoless", "--crumbsless",
    "--splashless", "--readonly", "--write",
})  # fmt: skip
# kubectl explain is not here: it reads the resource schema from the cluster's API server.
_KUBECTL_LOCAL = frozenset({"config", "completion", "kustomize", "help", "options", "plugin"})
_HELM_LOCAL = frozenset({
    "template", "lint", "package", "repo", "dependency", "dep", "show", "inspect", "pull", "fetch", "push",
    "registry", "create", "version", "completion", "env", "plugin", "search", "verify", "help",
})  # fmt: skip
_K9S_LOCAL = frozenset({"version", "help", "info", "completion"})


def find_unnamed_cluster_refusal(command: str) -> str | None:
    """A kubectl, helm or k9s command that names no context runs against the machine's default one, which differs per
    machine and changes; a command that changes that default changes it for every session."""
    for line, _ in _list_command_lines(command):
        for words, _ in line:
            program = os.path.basename(words[0]) if words else ""
            if refusal := _map_cluster_command_to_refusal(program, words[1:]):
                return refusal
    return None


def _list_command_lines(
    command: str, depth: int = 0, chained_outside: bool = False
) -> Generator[tuple[list[tuple[list[str], str]], bool]]:
    """The command line, then each one it runs through ssh, a shell's -c, a heredoc fed to a shell and a command
    substitution in double quotes or backticks: segments of words with the operator before each, and whether the line
    or one around it joins more than one step by ;, &&, ||, & or a newline. Read like the kill guard: quoted text,
    heredoc bodies and comments are data; redirections (2>/dev/null, > out) are not words of the command."""
    bodies: list[tuple[int, str]] = []
    expanding: list[tuple[int, str]] = []
    substitutions: list[str] = []
    segments = [
        (_drop_command_prefix(tokens), joined_by)
        for tokens, joined_by in segment_command(
            command, heredoc_bodies=bodies, substitutions=substitutions, redirections=[], expanding_bodies=expanding
        )
    ]
    steps = sum(1 for words, joined_by in segments if words and joined_by not in ("|", "|&"))
    chained = chained_outside or steps > 1
    yield segments, chained
    if depth >= 2:
        return
    bodies_by_owner: dict[int, list[str]] = {}
    for owner, body in bodies:
        bodies_by_owner.setdefault(owner, []).append(body)
    inner: list[str] = [*substitutions, *(s for _, body in expanding for s in _list_substitutions(body))]
    for index, (words, _) in enumerate(segments):
        program = os.path.basename(words[0]) if words else ""
        inner += _list_inner_commands(program, words)
        if index in bodies_by_owner and _reads_script_on_stdin(program, words):
            inner += bodies_by_owner[index]
    for text in inner:
        yield from _list_command_lines(text, depth + 1, chained)


def _list_substitutions(body: str) -> list[str]:
    """The $( ) and backtick commands the shell runs while expanding an unquoted heredoc's body."""
    found: list[str] = []
    at = 0
    while (start := body.find("$(", at)) >= 0:
        if start and body[start - 1] == "\\":
            at = start + 2
            continue
        end = find_substitution_end(body, start + 2)
        if end < 0:
            break
        found.append(body[start + 2 : end])
        at = end + 1
    return [*found, *body.split("`")[1:-1:2]]


def _map_cluster_command_to_refusal(program: str, all_args: list[str]) -> str | None:
    if program in ("kubectx", "kubens"):
        return _DEFAULT_SWITCH
    if program not in ("kubectl", "helm", "k9s"):
        return None
    # After --, words belong to the command kubectl exec or helm's post-renderer runs.
    args = all_args[: all_args.index("--")] if "--" in all_args else all_args
    positional = _list_positionals(args)
    if program == "kubectl" and (
        positional[:2] in (["config", "use-context"], ["config", "use"])
        or (positional[:2] == ["config", "set-context"] and _is_on(args, "--current"))
        or (positional[:3] in (["config", "set", "current-context"], ["config", "unset", "current-context"]))
    ):
        return _DEFAULT_SWITCH
    if _option_value(args, "--kube-context" if program == "helm" else "--context"):
        return None
    if "-h" in args or "--help" in args:
        return None
    if not positional:
        # Bare kubectl and helm print their help; bare k9s opens the default cluster.
        return _UNNAMED_CLUSTER if program == "k9s" else None
    first = positional[0]
    if program == "kubectl" and (
        first in _KUBECTL_LOCAL or (first == "version" and _is_on(args, "--client")) or _is_on(args, "--local")
    ):
        return None
    validated = first == "template" and (_is_on(args, "--validate") or "--dry-run=server" in args)
    if program == "helm" and ((first in _HELM_LOCAL and not validated) or "--dry-run=client" in args):
        return None
    if program == "k9s" and first in _K9S_LOCAL:
        return None
    return _UNNAMED_CLUSTER


_SECRET_VALUES = (
    "bridge: this prints secret values (kubectl get secret with -o json, yaml, jsonpath, go-template or "
    "custom-columns). Never read secret values, even filtered. Key names only: kubectl describe secret <name>."
)
_CLAUDE_LOGIN = (
    "bridge: never log out of Claude or touch its keychain entry: one login serves every session on this Mac. "
    "This command would {what}."
)
_PERMISSION_PROBE = (
    "bridge: read-only AWS excludes permission probes (aws iam simulate-*): they raise security alerts. Stop at the "
    "first AccessDenied and report it."
)
_CHAINED_APPLY = (
    "bridge: {what} runs alone, not chained with other steps by ;, &&, || or a newline: a step that fails or does "
    "something else must not run with it unseen. Run it as its own command and read its result first."
)
_PROD_VERB = (
    "bridge: {context} is a production context, and {program} {verb} there is refused for every session. Read it "
    "with get, describe, logs or top. Anything else only the owner runs, by hand: send {assistant} the exact command "
    "for a card. This guard has no waiver for agents, and typing the command into a tab is refused too."
)
_TYPED_SHELL = (
    "bridge: an agent may not type a '!' shell command into a tab. Run it with the Bash tool; what this guard refuses "
    "there only the owner runs."
)
_UNREAD_CONTEXT = (
    "bridge: the context of this {program} {verb} is {context}, which the shell fills in, so the guard cannot tell "
    "whether it is production. Name the context literally."
)
_SAFE_OUTPUTS = frozenset({"", "name", "wide"})
# What a production context allows; every other verb is refused, and with no verb kubectl and helm print help.
_PROD_KUBECTL_READS = frozenset({
    "", "get", "describe", "logs", "top", "explain", "api-resources", "api-versions", "version", "cluster-info",
    "diff", "events", "wait", *_KUBECTL_LOCAL,
})  # fmt: skip
_PROD_KUBECTL_SUBCOMMAND_READS = (["auth", "can-i"], ["auth", "whoami"], ["rollout", "status"], ["rollout", "history"])
_PROD_HELM_READS = frozenset({
    "", "list", "ls", "status", "history", "hist", "get", "show", "template", "search", "version", "diff",
    *_HELM_LOCAL,
})  # fmt: skip
# kubectl verbs whose words after -- are a command run in a container: read as a command line of their own.
_KUBECTL_CONTAINER_RUNS = frozenset({"exec", "debug", "run"})
_CLAUDE_VALUE_OPTIONS = frozenset({
    "--add-dir", "--agent", "--agents", "--allowedTools", "--allowed-tools", "--append-system-prompt",
    "--append-system-prompt-file", "--autocompact", "--betas", "--debug-file", "--disallowedTools",
    "--disallowed-tools", "--effort", "--environment", "--fallback-model", "--file", "--input-format", "--json-schema",
    "--max-budget-usd", "--max-turns", "--mcp-config", "--model", "-n", "--name", "--output-format",
    "--permission-mode", "--permission-prompts", "--plugin-dir", "--plugin-url", "--session-id", "--setting-sources",
    "--settings", "--system-prompt", "--system-prompt-file", "--system-prompt-snapshot", "--tools",
})  # fmt: skip
# Options whose value is optional: commander takes the next word as it unless that starts with -.
_CLAUDE_OPTIONAL_VALUE_OPTIONS = frozenset({
    "-d", "--debug", "-r", "--resume", "-w", "--worktree", "--cloud", "--from-pr", "--prompt-suggestions",
    "--remote-control", "--teleport",
})  # fmt: skip
_AWS_BOOLEANS = frozenset({
    "--debug", "--no-verify-ssl", "--no-paginate", "--no-sign-request", "--no-cli-pager", "--cli-auto-prompt",
    "--no-cli-auto-prompt",
})  # fmt: skip
_TMUX_SEND_VALUE_OPTIONS = frozenset({"-t", "-N", "-c"})
_AGTERM_VALUE_OPTIONS = frozenset({"--pane", "--target", "--socket", "--window", "--pane-id"})


def find_rule_refusal(command: str, config: Config, depth: int = 0, chained_outside: bool = False) -> str | None:
    """Prod and secrets rules, said in the refusal rather than in every session's context: no secret values read, no
    Claude logout, no AWS permission probe, no admin rights on GitHub, a secret or cluster apply run alone, and on a
    production context only reads. Read through ssh, a shell's -c and the rest as the cluster check reads them, and a
    command kubectl exec runs in a container too. An alias defined in the command itself (k=kubectl) is not followed:
    that is evasion."""
    for line, chained in _list_command_lines(command, chained_outside=chained_outside):
        for words, _ in line:
            program = os.path.basename(words[0]) if words else ""
            if _asks_help(program, words[1:]):
                continue
            if refusal := _map_command_to_rule_refusal(program, words[1:], config, depth):
                return refusal
            if chained and (applied := _name_apply(program, words[1:])):
                return _CHAINED_APPLY.format(what=applied)
            inside = _map_container_run_to_command(program, words[1:]) if depth < 2 else None
            if inside and (refusal := find_rule_refusal(inside, config, depth + 1, chained)):
                return refusal
    return None


def _asks_help(program: str, all_args: list[str]) -> bool:
    """--help or -h before any --, or aws's own help word: the command prints help and runs nothing."""
    args = all_args[: all_args.index("--")] if "--" in all_args else all_args
    return "--help" in args or "-h" in args or (program == "aws" and "help" in args)


def _map_command_to_rule_refusal(program: str, all_args: list[str], config: Config, depth: int = 0) -> str | None:
    if typed := _map_typed_to_refusal(program, all_args, config, depth):
        return typed
    if program in ("kubectl", "helm"):
        split = all_args.index("--") if "--" in all_args else len(all_args)
        args = all_args[:split]
        positional = _list_positionals(args, limit=None)
        first = positional[0] if positional else ""
        if program == "kubectl" and first == "get":
            # After --, kubectl get reads resources, not a command.
            resources = [*positional[1:], *all_args[split + 1 :]]
            values = _read_output_format(args).split("=", 1)[0] not in _SAFE_OUTPUTS
            if (values and _names_secret(resources)) or _is_secrets_path(_option_value(args, "--raw") or ""):
                return _SECRET_VALUES
        reads = _PROD_KUBECTL_READS if program == "kubectl" else _PROD_HELM_READS
        is_read = first in reads or (program == "kubectl" and positional[:2] in _PROD_KUBECTL_SUBCOMMAND_READS)
        if is_read or not config.prod_kube_contexts:
            return None
        context = _option_value(args, "--kube-context" if program == "helm" else "--context") or ""
        if "$" in context or "`" in context:
            return _UNREAD_CONTEXT.format(program=program, verb=first, context=context)
        if any(fnmatch.fnmatchcase(context, pattern) for pattern in config.prod_kube_contexts):
            return _PROD_VERB.format(context=context, program=program, verb=first, assistant=config.assistant_handle)
        return None
    if program == "claude":
        positional = _list_claude_positionals(all_args)
        if positional[:1] == ["logout"] or positional[:2] == ["auth", "logout"]:
            return _CLAUDE_LOGIN.format(what="log Claude out")
    if program == "security":
        subcommand = next((a for a in all_args if not a.startswith("-")), "")
        if "password" in subcommand and any("claude" in a.lower() for a in all_args):
            return _CLAUDE_LOGIN.format(what="read or change Claude's keychain entry")
    if program == "aws":
        operation = _list_aws_positionals(all_args)
        if operation[:1] == ["iam"] and operation[1:2] and operation[1].startswith("simulate-"):
            return _PERMISSION_PROBE
    if program == "gh":
        return _map_gh_to_admin_refusal(all_args, config)
    return None


_ADMIN_RIGHTS = (
    "bridge: {what} can use the owner's admin rights to get past the repository's merge rules, and the owner's "
    "rule is that no one uses them until the owner asks. Merge with a plain `gh pr merge <number>` once the PR meets "
    "the rules; if it cannot, send {assistant} the exact command for a card, and the owner runs it by hand."
)
_API_MERGE_RE = re.compile(r"(?:^|/)repos/[^/\s]+/[^/\s]+/pulls/[^/\s]+/merge")
_API_RULES_RE = re.compile(
    r"(?:^|/)(?:repos/[^/\s]+/[^/\s]+|orgs/[^/\s]+)/(?:rulesets|branches/\S+/protection)(?:[/?]|$)"
)
_GRAPHQL_ADMIN_RE = re.compile(r"mergePullRequest|(?:create|update|delete)\w*(?:Ruleset|BranchProtectionRule)", re.I)


def _map_gh_to_admin_refusal(args: list[str], config: Config) -> str | None:
    """gh pr merge --admin, a merge through the API (which an admin token gets past the rules with, as --admin does)
    and a ruleset or branch protection write."""
    positional: list[tuple[int, str]] = []
    at = 0
    while at < len(args) and len(positional) < 2:
        if args[at] in ("-R", "--repo"):
            at += 1
        elif not args[at].startswith("-"):
            positional.append((at, args[at]))
        at += 1
    words = [word for _, word in positional]
    what = None
    if words == ["pr", "merge"] and any(_is_admin_flag(a) for a in args[positional[1][0] + 1 :]):
        what = "gh pr merge --admin"
    elif words[:1] == ["api"]:
        method, fields, path, _ = parse_gh_api_args(args[positional[0][0] + 1 :])
        if path.strip("/") == "graphql" and _GRAPHQL_ADMIN_RE.search(" ".join(value for _, value in fields)):
            what = "this GraphQL mutation"
        elif method not in ("GET", "HEAD") and _API_MERGE_RE.search(path):
            what = "a merge through gh api"
        elif method not in ("GET", "HEAD") and _API_RULES_RE.search(path):
            what = "a ruleset or branch protection change"
    return _ADMIN_RIGHTS.format(what=what, assistant=config.assistant_handle) if what else None


def _is_admin_flag(arg: str) -> bool:
    name, eq, value = arg.partition("=")
    return name == "--admin" and (not eq or value.lower() not in ("false", "f", "0"))


def _list_aws_positionals(args: list[str]) -> list[str]:
    """aws's service and operation: its global options take a value, but for these flags."""
    found: list[str] = []
    at = 0
    while at < len(args) and len(found) < 2:
        if args[at].startswith("-"):
            at += 1 if args[at] in _AWS_BOOLEANS or "=" in args[at] else 2
        else:
            found.append(args[at])
            at += 1
    return found


def _is_secrets_path(path: str) -> bool:
    """An API path that lists or reads Secrets: a segment secrets that is a resource, not a namespace's name."""
    segments = path.split("?", 1)[0].strip("/").split("/")
    return any(seg == "secrets" and (at == 0 or segments[at - 1] != "namespaces") for at, seg in enumerate(segments))


def _list_claude_positionals(args: list[str]) -> list[str]:
    """Claude's subcommand words; none with -p, where the word is a prompt."""
    found: list[str] = []
    at = 0
    while at < len(args):
        arg = args[at]
        if arg in ("-p", "--print"):
            return []
        if arg.startswith("-"):
            optional = arg in _CLAUDE_OPTIONAL_VALUE_OPTIONS and at + 1 < len(args) and not args[at + 1].startswith("-")
            at += 2 if arg in _CLAUDE_VALUE_OPTIONS or optional else 1
        else:
            found.append(arg)
            at += 1
    return found


def _list_typed_words(program: str, args: list[str]) -> list[str]:
    """The words agtermctl or tmux send-keys types into a tab; the tab gets them joined with nothing between."""
    if program == "agtermctl":
        value_options = _AGTERM_VALUE_OPTIONS
    elif program == "tmux" and {"send-keys", "send"} & set(args):
        value_options = _TMUX_SEND_VALUE_OPTIONS
    else:
        return []
    typed: list[str] = []
    at = 0
    while at < len(args):
        if args[at].startswith("-") and args[at] != "-":
            at += 2 if args[at] in value_options else 1
        else:
            typed.append(args[at])
            at += 1
    return typed


def _map_typed_to_refusal(program: str, args: list[str], config: Config, depth: int) -> str | None:
    typed = _list_typed_words(program, args)
    starts = ["".join(typed[start:]).lstrip() for start in range(len(typed))]
    if any(start.startswith("/logout") for start in starts):
        return _CLAUDE_LOGIN.format(what="type /logout into a tab")
    if any(start.startswith("!") for start in starts):
        return _TYPED_SHELL
    if depth >= 2:
        return None
    # A shell tab runs what is typed, so it is read as a command from each word on; key names read as extra words.
    words = [word.strip() for word in typed]
    return next(
        (r for start in range(len(words)) if (r := find_rule_refusal(" ".join(words[start:]), config, depth + 1))), None
    )


def _map_container_run_to_command(program: str, all_args: list[str]) -> str | None:
    """The command line kubectl exec, debug or run starts in a container after --, quoted back for the shell."""
    if program != "kubectl" or "--" not in all_args:
        return None
    split = all_args.index("--")
    positional = _list_positionals(all_args[:split], limit=1)
    if positional[:1] != [] and positional[0] in _KUBECTL_CONTAINER_RUNS and all_args[split + 1 :]:
        return " ".join("'" + word.replace("'", "'\\''") + "'" for word in all_args[split + 1 :])
    return None


def _name_apply(program: str, all_args: list[str]) -> str | None:
    """A command that creates a secret or applies to a cluster, as the refusal names it; None for any other."""
    if program not in ("kubectl", "helm"):
        return None
    args = all_args[: all_args.index("--")] if "--" in all_args else all_args
    positional = _list_positionals(args)
    if program == "kubectl" and (positional[:1] == ["apply"] or positional[:2] == ["create", "secret"]):
        return f"kubectl {' '.join(positional[:2]) if positional[0] == 'create' else 'apply'}"
    if program == "helm" and positional[:1] in (["upgrade"], ["install"]):
        return f"helm {positional[0]}"
    return None


def _names_secret(positional: list[str]) -> bool:
    """The resource type kubectl get reads: secret, secrets.v1, configmap,secret, or a type/name such as secret/web
    anywhere; a resource merely named secret is not one."""
    if not positional:
        return False
    items = [*positional[0].split(","), *(word for word in positional[1:] if "/" in word)]
    return any(item.split("/", 1)[0].split(".", 1)[0].lower() in ("secret", "secrets") for item in items)


def _read_output_format(args: list[str]) -> str:
    """The last -o/--output value in any of its spellings (-o yaml, -oyaml, -o=yaml, -Aoyaml, --output=yaml); a
    --template alone prints as go-template."""
    found = ""
    at = 0
    while at < len(args):
        arg = args[at]
        letter, attached = read_short_cluster(arg, _CLUSTER_VALUE_LETTERS)
        if arg == "--output" or (letter == "o" and attached is None):
            found = args[at + 1] if at + 1 < len(args) else ""
        elif letter == "o" and attached is not None:
            found = attached
        elif arg.startswith("--output="):
            found = arg.removeprefix("--output=")
        elif (arg == "--template" or arg.startswith("--template=")) and not found:
            found = "go-template"
        at += 2 if arg in _CLUSTER_VALUE_OPTIONS or (letter is not None and attached is None) else 1
    return found


def _list_positionals(args: list[str], limit: int | None = 3) -> list[str]:
    found: list[str] = []
    at = 0
    while at < len(args) and (limit is None or len(found) < limit):
        arg = args[at]
        if arg.startswith("-"):
            before_verb = (
                not found
                and "=" not in arg
                and arg not in _CLUSTER_ROOT_BOOLEANS
                and (arg.startswith("--") or len(arg) == 2)
            )
            letter, attached = read_short_cluster(arg, _CLUSTER_VALUE_LETTERS)
            takes = arg in _CLUSTER_VALUE_OPTIONS or before_verb or (letter is not None and attached is None)
            at += 2 if takes else 1
        else:
            found.append(args[at])
            at += 1
    return found


def _option_value(args: list[str], option: str) -> str | None:
    """The value the last --option or --option=value gives, as the tool reads it; None when absent."""
    value: str | None = None
    at = 0
    while at < len(args):
        arg = args[at]
        if arg == option:
            value = args[at + 1] if at + 1 < len(args) else ""
            at += 2
            continue
        if arg.startswith(option + "="):
            value = arg[len(option) + 1 :]
        at += 1 + (arg in _CLUSTER_VALUE_OPTIONS)
    return value


def _is_on(args: list[str], flag: str) -> bool:
    """A boolean flag's last setting: --flag or --flag=true and the like turn it on."""
    on = False
    at = 0
    while at < len(args):
        arg = args[at]
        if arg == flag:
            on = True
        elif arg.startswith(flag + "="):
            on = arg[len(flag) + 1 :].lower() in ("true", "1", "t")
        at += 1 + (arg in _CLUSTER_VALUE_OPTIONS)
    return on


def _drop_command_prefix(tokens: list[str]) -> list[str]:
    at = 0
    while at < len(tokens) and tokens[at] in SHELL_KEYWORDS:
        at += 1
    if at < len(tokens) and tokens[at] == "sudo":
        at += 1
        while at < len(tokens) and tokens[at].startswith("-"):
            at += 2 if tokens[at] in _SUDO_VALUE_OPTIONS else 1
    return strip_prefixes(tokens[at:]) if at else tokens


def _program_of(tokens: list[str]) -> str:
    words = _drop_command_prefix(tokens)
    return os.path.basename(words[0]) if words else ""


def _xargs_program(words: list[str]) -> str:
    at = 1
    while at < len(words) and words[at].startswith("-"):
        at += 2 if words[at] in _XARGS_VALUE_OPTIONS else 1
    return os.path.basename(words[at]) if at < len(words) else ""


_CHECKERS = frozenset({
    "pre-commit", "pytest", "ruff", "mypy", "pyright", "tox", "nox", "jest", "vitest", "eslint", "tsc", "make",
})  # fmt: skip
_RUNNERS = frozenset({"npm", "pnpm", "yarn", "go", "cargo"})
_RUNNER_CHECKS = frozenset({"test", "lint", "check", "typecheck", "vet", "clippy"})
_FILTERS = frozenset({"grep", "egrep", "rg", "tail", "head", "sed", "awk", "cut", "sort", "uniq", "wc", "tee", "less"})


def find_hidden_exit_code_note(command: str) -> str | None:
    """A test, lint, pre-commit or commit run piped into a filter reports the filter's exit code: a failed check reads
    as passed. Not refused, since the output is often all that is wanted; the note says how to keep the real code."""
    if _KEEPS_EXIT_CODE_RE.search(command) and not re.search(r"set\s+\+o\s+pipefail", command):
        return None
    segments = segment_command(command)
    for index in range(1, len(segments)):
        tokens, joined_by = segments[index]
        if joined_by not in ("|", "|&") or _program_of(tokens) not in _FILTERS:
            continue
        # A subshell's ) leaves a segment of nothing or only redirections between the checker and the pipe.
        before: list[str] = next(
            (
                t
                for t, j in reversed(segments[:index])
                if t and not (j == ")" and all(_REDIRECT_RE.match(w) for w in t))
            ),
            list[str](),
        )
        checker = _map_tokens_to_checker(_drop_command_prefix(before))
        if checker:
            return (
                f"bridge: {checker}'s exit code is lost in this pipe: the shell reports the last command's, so a "
                f"failed {checker} reads as passed. Add 'set -o pipefail; ' at the start, or check "
                "${PIPESTATUS[0]} (bash) / ${pipestatus[1]} (zsh), before calling it passed."
            )
    return None


_REDIRECT_RE = re.compile(r"^\d*(?:[<>]|&>)")
_BARE_REDIRECT_RE = re.compile(r"\d*(?:<<-?|<<<|>>|>\||<>|[<>]&?|&>>?)")
_KEEPS_EXIT_CODE_RE = re.compile(
    r"(?:set\s+-o\s+pipefail|setopt\s+pipefail|\bset\s+-\w*e\w*o\s+pipefail|pipestatus)", re.I
)
_RUNNER_VALUE_OPTIONS = frozenset({
    "--project", "--python", "-p", "--with", "--directory", "--package", "--group", "--extra", "--env-file", "--index",
    "--from",
})  # fmt: skip


def _map_tokens_to_checker(words: list[str]) -> str | None:
    words = _strip_runner(words)
    program = os.path.basename(words[0]) if words else ""
    if program in _CHECKERS:
        return program
    if program == "git" and "commit" in words[1:6]:
        return "git commit"
    if program in _RUNNERS and len(words) > 1:
        task = words[2] if words[1] == "run" and len(words) > 2 else words[1]
        if task in _RUNNER_CHECKS:
            return f"{program} {task}"
    return None


def handle_pre_tool_use(
    conn: sqlite3.Connection,
    payload: dict[str, object],
    config: Config,
    host_checkout: HostRepoReader | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    decision = _check_pre_tool_use(conn, payload, config, host_checkout, env)
    raw_input = payload.get("tool_input")
    command = cast(dict[str, object], raw_input).get("command") if isinstance(raw_input, dict) else None
    if decision or payload.get("tool_name") != "Bash" or not isinstance(command, str):
        return decision
    notes = [find_hidden_exit_code_note(command)]
    if host_checkout is None and config.heavy_job_load_per_core and (job := find_heavy_job(command)):
        notes.append(find_load_note(conn, config, job, _str(payload, "session_id")))
    text = "\n".join(n for n in notes if n)
    return _note(text) if text else ""


_TEST_PATH_RE = re.compile(r"(?:\.py|::.*)$")
_SOURCE_RE = re.compile(r"\.(?:py|pyi|[cm]?[jt]sx?)$")
_INFO_FLAGS = frozenset({"--version", "-V", "--help", "-h"})
_MAKE_VALUE_OPTIONS = frozenset({"-C", "-f", "-I", "-o", "-W", "--directory", "--file", "--makefile"})
_PYTEST_VALUE_OPTIONS = frozenset({
    "-p", "-c", "-m", "-n", "-o", "--ignore", "--ignore-glob", "--deselect", "--rootdir", "--confcutdir", "--maxfail",
    "--durations", "--tb", "--cov", "--junitxml", "--basetemp",
})  # fmt: skip
_CARGO_VALUE_OPTIONS = frozenset({
    "-p", "--package", "-j", "--jobs", "-F", "--features", "--target", "--manifest-path", "--profile", "--exclude",
})  # fmt: skip


def find_heavy_job(command: str) -> str | None:
    """A check or build over the whole project, run on this Mac: a single test file, node id or -k filter is not one,
    nor a command handed to ssh, whose host runs it."""
    for tokens, _ in segment_command(command):
        words = _strip_runner(_drop_timeout(_drop_redirections(_drop_command_prefix(tokens))))
        if not words or _INFO_FLAGS.intersection(words):
            continue
        program, args = os.path.basename(words[0]), words[1:]
        if program == "pytest":
            positional = _list_positional(args, _PYTEST_VALUE_OPTIONS)
            if not any(_TEST_PATH_RE.search(a) for a in positional) and not any(a.startswith("-k") for a in args):
                return "pytest"
        elif program in ("pyright", "mypy", "tsc"):
            if not any(_SOURCE_RE.search(a) for a in args if not a.startswith("-")):
                return program
        elif program in ("npm", "pnpm", "yarn") and args:
            task = args[1] if args[0] == "run" and len(args) > 1 else args[0]
            passed = args[args.index("--") + 1 :] if "--" in args else []
            if task.split(":")[0] in ("test", "build") and not _list_positional(passed, frozenset()):
                return f"{program} {task}"
        elif program == "uv" and args[:1] == ["sync"]:
            return "uv sync"
        elif program == "docker" and "build" in args[:2]:
            return "docker build"
        elif program == "make":
            targets = _list_make_targets(args)
            if not targets or {"test", "build"} & set(targets):
                return " ".join(["make", *targets])
        elif program == "cargo" and args[:1] == ["test"] and not _list_positional(args[1:], _CARGO_VALUE_OPTIONS):
            return "cargo test"
        elif (
            program == "go"
            and args[:1] == ["test"]
            and not any(a.split("=")[0] == "-run" or a.endswith(".go") for a in args)
        ):
            return "go test"
    return None


def _strip_runner(words: list[str]) -> list[str]:
    while words[:2] in (["uv", "run"], ["poetry", "run"]) or words[:1] in (["npx"], ["uvx"]):
        words = words[2:] if words[0] in ("uv", "poetry") else words[1:]
        while words and words[0].startswith("-"):
            words = words[2:] if words[0] in _RUNNER_VALUE_OPTIONS else words[1:]
    if words[:2] in (["python", "-m"], ["python3", "-m"]):
        words = words[2:]
    return words


def _drop_timeout(words: list[str]) -> list[str]:
    if words[:1] != ["timeout"]:
        return words
    words = words[1:]
    while words and words[0].startswith("-"):
        words = words[2:] if words[0] in ("-s", "-k") else words[1:]
    return words[1:]


def _list_positional(args: list[str], value_options: frozenset[str]) -> list[str]:
    positional: list[str] = []
    at = 0
    while at < len(args):
        if args[at] == "--":
            break
        if args[at] in value_options:
            at += 2
            continue
        if not args[at].startswith("-"):
            positional.append(args[at])
        at += 1
    return positional


def _list_make_targets(args: list[str]) -> list[str]:
    targets: list[str] = []
    at = 0
    while at < len(args):
        arg = args[at]
        if arg in _MAKE_VALUE_OPTIONS or (arg == "-j" and at + 1 < len(args) and args[at + 1].isdigit()):
            at += 2
            continue
        if not arg.startswith("-") and "=" not in arg:
            targets.append(arg)
        at += 1
    return targets


def find_load_note(conn: sqlite3.Connection, config: Config, job: str, conversation_id: str | None) -> str | None:
    """Over the load threshold, says to run the job on worker_host, and logs it to slow.log for the leads."""
    cores = os.cpu_count() or 1
    load = os.getloadavg()[0]
    if load <= config.heavy_job_load_per_core * cores:
        return None
    remote = next((r for r in config.remotes if r.name == config.worker_host), None)
    host = remote.ssh if remote else config.worker_host
    who = _find_handle(conn, conversation_id) or (conversation_id or "?")[:8]
    log_slow("load note", f"load note: {who} {job} at load {load:.0f}")
    head = f"bridge: this Mac's load is {load:.0f} on {cores} cores, and {job} would add to it."
    if not host:
        return f"{head} Run a targeted subset (one test file, -k) or wait for the load to fall; no worker_host is set."
    run = f"job-{re.sub(r'[^A-Za-z0-9_-]', '', who) or 'x'}"
    return (
        f"{head} Run it on {host} if you can, detached, then poll: d={run}-$(date +%s); t=$(git stash create); "
        f'git -C "$(git rev-parse --show-toplevel)" archive ${{t:-HEAD}} | ssh {host} "mkdir ~/$d && tar -x -C '
        f'~/$d" && ssh {host} "cd ~/$d && nohup bash -lc \'<command>\' > ~/$d.log 2>&1 &", then poll '
        f'ssh {host} "tail -n 20 ~/$d.log". The whole checkout is sent with its uncommitted changes (git stash '
        "create leaves the stash list alone), not untracked files; run <command> from the same subfolder."
    )


def _find_handle(conn: sqlite3.Connection, conversation_id: str | None) -> str | None:
    if not conversation_id:
        return None
    row = conn.execute(
        "SELECT s.handle FROM instances i JOIN surfaces s ON s.surface = i.surface "
        "WHERE i.conversation_id = ? AND s.handle IS NOT NULL ORDER BY i.last_observed_at DESC LIMIT 1",
        (conversation_id,),
    ).fetchone()
    return str(row[0]) if row else None


ARTIFACT_REFUSAL = (
    "Pages are local: write _pages/<slug>/index.html, `bridge page add <slug>`, and link "
    "http://127.0.0.1:8765/p/<slug>. The team never publishes to claude.ai."
)


HOST_ARTIFACT_REFUSAL = (
    "The team never publishes to claude.ai: its pages are local, on the Mac. Push the page's HTML or tell your lead "
    "its path, and the lead adds it with `bridge page add`."
)


SKILL_TOOL = "Skill"


def find_lead_skill_refusal(conn: sqlite3.Connection, config: Config, payload: dict[str, object]) -> str | None:
    """A lead or the assistant routes and reviews, and a skill's body would ride in its long-lived context; a locked
    tab or the owner typing there changes nothing. A plugin or directory skill (a:b) matches by its last part."""
    tool_input = payload.get("tool_input")
    skill = cast(dict[str, object], tool_input).get("skill") if isinstance(tool_input, dict) else None
    if not isinstance(skill, str) or skill.rsplit(":", 1)[-1] in config.lead_skills:
        return None
    row = conn.execute(
        "SELECT s.surface, s.handle FROM instances i JOIN surfaces s ON s.surface = i.surface "
        "WHERE i.conversation_id = ? AND i.ended_at IS NULL ORDER BY i.first_seen_at DESC LIMIT 1",
        (str(payload.get("session_id") or ""),),
    ).fetchone()
    if row is None or (row["handle"] != config.assistant_handle and find_lead_on_surface(conn, row["surface"]) is None):
        return None
    return (
        "Leads and the assistant route and review: give this to a worker (guild worker). "
        f"Allowed here: {', '.join(config.lead_skills)}."
    )


def check_host_artifact(conn: sqlite3.Connection, config: Config, surface: str) -> str:
    """Every host worker has a lead, so only a locked tab makes it personal."""
    return "" if is_surface_locked(conn, config, surface) else _deny(HOST_ARTIFACT_REFUSAL)


def is_team_conversation(conn: sqlite3.Connection, config: Config, conversation_id: str, pane: str | None) -> bool:
    """A lead, the assistant, or a session in or under the tab of either, outside a locked tab: the owner's other
    sessions are not. pane, the hook's own, stands in for a nested claude, whose instance has no surface."""
    row = conn.execute(
        "SELECT surface FROM instances WHERE conversation_id = ? AND ended_at IS NULL ORDER BY first_seen_at DESC",
        (conversation_id,),
    ).fetchone()
    surface = (row["surface"] if row else None) or pane
    if surface is None or is_surface_locked(conn, config, surface):
        return False
    own = conn.execute("SELECT handle FROM surfaces WHERE surface = ?", (surface,)).fetchone()
    if (own is not None and own["handle"] == config.assistant_handle) or find_lead_of(conn, surface)[1] is not None:
        return True
    return any(
        conn.execute(
            "SELECT 1 FROM surfaces WHERE agterm_session = ? AND handle = ?", (tab, config.assistant_handle)
        ).fetchone()
        for tab in list_tab_lineage(conn, surface)
    )


def _check_pre_tool_use(
    conn: sqlite3.Connection,
    payload: dict[str, object],
    config: Config,
    host_checkout: HostRepoReader | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Denies a robot-prefixed title, a change to the owner's label, a PR that would name no issue or change another
    team's restricted path without a why and the assistant's OK, an answer to a colleague's comment no call handles
    yet, a shell command a prod or secrets rule refuses, or one that would mutate a resource another live session has
    claimed.

    host_checkout is set for a host worker: git facts come from its report, and a merge it names no PR for is refused.
    """
    if is_artifact_publish(payload) and is_team_conversation(
        conn, config, str(payload.get("session_id") or ""), (env or {}).get("AGTERM_PANE_ID")
    ):
        return _deny(ARTIFACT_REFUSAL)
    if payload.get("tool_name") == SKILL_TOOL:
        refusal = find_lead_skill_refusal(conn, config, payload)
        return _deny(refusal) if refusal else ""
    from bridge.title_guard import (  # it loads the review modules, which other hooks never use
        find_colleague_reply_refusal,
        find_missing_issue_refusal,
        find_owner_label_writes,
        find_prefixed_linear_title,
        find_prefixed_titles,
        find_restricted_path_refusal,
        find_unclaimed_prs,
        map_label_write_to_denial,
        map_titles_to_denial,
    )

    raw_input = payload.get("tool_input")
    tool_input = cast(dict[str, object], raw_input) if isinstance(raw_input, dict) else {}
    tool = str(payload.get("tool_name") or "")
    linear_title = find_prefixed_linear_title(tool, tool_input)
    if linear_title is not None:
        return _deny(map_titles_to_denial([linear_title]))
    command = tool_input.get("command")
    if tool != "Bash" or not isinstance(command, str):
        return ""
    if kill := find_kill_by_name_refusal(command):
        return _deny(kill)
    if rule := find_rule_refusal(command, config):
        return _deny(rule)
    if unnamed_cluster := find_unnamed_cluster_refusal(command):
        return _deny(unnamed_cluster)
    titles = find_prefixed_titles(command)
    if titles:
        return _deny(map_titles_to_denial(titles))
    if label_writes := find_owner_label_writes(command, config.review_owner_label):
        return _deny(map_label_write_to_denial(config.review_owner_label, config.owner_name, label_writes[0]))
    cwd = _str(payload, "cwd")
    if host_checkout is None:
        missing_issue = find_missing_issue_refusal(conn, config, command, cwd or os.getcwd())
    else:
        missing_issue = find_missing_issue_refusal(conn, config, command, cwd, host_checkout, read_files=False)
    if missing_issue:
        return _deny(missing_issue)
    session = _str(payload, "session_id")
    if host_checkout is None:
        restricted = find_restricted_path_refusal(conn, config, command, cwd or os.getcwd(), session)
    else:
        restricted = find_restricted_path_refusal(conn, config, command, cwd, session, host_checkout, read_files=False)
    if restricted:
        return _deny(restricted)
    where = cwd if host_checkout is not None else cwd or os.getcwd()
    if reply := find_colleague_reply_refusal(conn, config, command, where, host_checkout):
        return _deny(reply)
    unreadable: list[Unreadable] = []
    if host_checkout is None:
        targets = map_command_to_targets(command, _str(payload, "cwd") or os.getcwd(), unreadable)
    else:
        targets = map_command_to_targets(
            command, _str(payload, "cwd") or "", unreadable, host_checkout, strict=True,
            home=host_checkout.home,
        )  # fmt: skip
    if host_checkout is None and any(t.pushed is not None for t in targets):
        from bridge.empty_files import find_new_empty_files, map_empty_files_to_denial  # git runs only for a push

        if empty := find_new_empty_files(targets):
            return _deny(map_empty_files_to_denial(empty))
    all_targets = add_merge_base_targets(conn, targets)
    conflicts = find_conflicts(conn, all_targets, _str(payload, "session_id"))
    held = find_held_writes(conn, all_targets)
    waived: list[str] = []
    heads: dict[str, str] = {}
    now = time.time_ns() // 1_000_000
    merge_deadline = time.monotonic() + MERGE_CHECK_BUDGET_S
    blocks = find_merge_blocks(conn, config, targets, now, waived, heads)
    # The Mac sees a host only through the facts of the checkouts its gbridge reported: a push or merge it cannot
    # read could be to any branch or PR. On the Mac only a merge or close whose text hides its PR is refused, and a
    # push or merge written so the guard cannot read it (bash -c or -s, eval, xargs, sudo, find -exec, a url, -c).
    unnamed = unreadable if host_checkout is not None else [u for u in unreadable if u.refused_on_mac]
    if not conflicts and not blocks and not unnamed and not held:
        if sandboxes := find_sandbox_merges(conn, targets, now, _str(payload, "session_id") or "", heads):
            _record_sandbox_warnings(conn, sandboxes, now, merge_deadline)
            return _deny("\n".join(map_sandbox_merges_to_lines(sandboxes)))
        conversation_id = _str(payload, "session_id")
        pushed = [
            (parts[1], parts[2])
            for t in targets
            if t.action == "git push" and len(parts := t.key.split(":", 2)) == 3 and parts[0] == "branch"
        ]
        if host_checkout is not None and conversation_id and pushed:
            from bridge.reviews import record_host_pushes  # a third of every hook's start-up otherwise

            with transaction(conn):
                record_host_pushes(conn, config, conversation_id, pushed, time.time_ns() // 1_000_000)
        notes = waived
        if conversation_id:
            read_repo = host_checkout if host_checkout is not None else read_repo_facts
            unclaimed = find_unclaimed_prs(conn, command, where, targets, conversation_id, read_repo)
            if unclaimed and (told := _record_nudges(conn, conversation_id, unclaimed)):
                notes = [*waived, *(map_unclaimed_pr_to_note(ref, host_checkout is not None) for ref in told)]
        return _note("\n".join(notes)) if notes else ""
    lines = map_held_writes_to_lines(held, config.owner_name, now)
    lines += map_merge_blocks_to_lines(blocks, config.owner_name, config.owner_name_at_start)
    readable = host_checkout.roots if host_checkout is not None else None
    lines += [map_unreadable_to_line(u, readable) for u in unnamed[:1]]
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


def map_unclaimed_pr_to_note(ref: str, on_host: bool) -> str:
    if on_host:
        return f"bridge: no claim held on {ref}: ask your lead to claim it first; this note comes once per PR."
    return f"bridge: no claim held on {ref}: bridge claim it first (`bridge claim {ref}`); this note comes once per PR."


def _record_sandbox_warnings(conn: sqlite3.Connection, merges: list[SandboxMerge], now: int, deadline: float) -> None:
    """Waits on a busy database only until the merge check's deadline, as a hook that times out lets the merge
    through; unrecorded, the next merge is refused with the warning again, never let through."""
    wait_ms = min(HOOK_BUSY_TIMEOUT_MS, int((deadline - time.monotonic()) * 1000))
    if wait_ms <= 0:
        log_error("hook PreToolUse", "sandbox warning not recorded: no time left")
        return
    own = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
    conn.execute(f"PRAGMA busy_timeout = {min(own, wait_ms)}")
    try:
        with transaction(conn):
            conn.executemany(
                "UPDATE review_prs SET sandbox_warned_at = ?, sandbox_warned_for = ? WHERE ref = ?",
                [(now, m.warned_for, m.row["ref"]) for m in merges],
            )
    except sqlite3.OperationalError:
        log_error("hook PreToolUse", f"sandbox warning not recorded: {traceback.format_exc(limit=1)}")
    finally:
        conn.execute(f"PRAGMA busy_timeout = {own}")


def _record_nudges(conn: sqlite3.Connection, conversation_id: str, refs: list[str]) -> list[str]:
    """The refs to tell about. None while the database is busy: the note comes on a later change, not a slow hook."""
    me = Party(conversation_id, _find_handle(conn, conversation_id) or conversation_id[:8])
    try:
        with transaction(conn, retry_busy_timeout_ms=HOOK_RETRY_BUSY_TIMEOUT_MS):
            return record_nudges(conn, me, refs, time.time_ns() // 1_000_000)
    except sqlite3.OperationalError:
        log_error("hook PreToolUse", f"claim nudge not recorded: {traceback.format_exc(limit=1)}")
        return []


def map_unreadable_to_line(unreadable: Unreadable, readable: tuple[str, ...] | None) -> str:
    """readable: on a worker host, the directories whose checkouts the guard reads; None on the Mac."""
    text = unreadable.text[:120]
    where = ""
    if readable is not None:
        places = " or ".join(readable) if readable else "no directory (bridge does not know this worker's)"
        where = (
            f" On a worker host bridge reads the plain git settings only of a checkout under {places}: run it "
            "there, as `cd <checkout> && <command>` or from that directory."
        )
    if unreadable.kind == WRAPPED:
        return (
            f"bridge: the guard cannot read this push or merge ({text}); write it as a plain `git push <remote> "
            "<branch>` or `gh pr merge <number>`, not inside bash -c or -s, a heredoc or pipe into a shell, eval, "
            "xargs, sudo, find -exec or env -S, to a remote name rather than a url and with no -c setting but user.*, "
            "color.* or advice.*, so the guard can read it."
        )
    if unreadable.kind == MERGE:
        return (
            f"bridge: this merge names no PR the guard can identify ({text}).{where} Name the PR plainly, with no "
            "variable or repo-picking environment: `gh pr merge <number> -R <owner>/<repo>` or its URL."
        )
    if unreadable.kind == CLOSE:
        return (
            f"bridge: this close names no PR the guard can identify ({text}).{where} Name the PR plainly, with no "
            "variable or repo-picking environment: `gh pr close <number> -R <owner>/<repo>` or its URL."
        )
    if unreadable.kind == PUSH:
        return (
            f"bridge: this push names no branch the guard can read ({text}).{where} Name it plainly: "
            "`git push origin <branch>`."
        )
    return (
        f"bridge: this command mentions a push, merge or close the guard cannot read ({text}). On a worker host "
        "bridge passes only a push, merge or close it reads: run it as a plain command of its own, not inside "
        "if/then, a subshell, bash -c, eval, xargs, a pipe into another program or a substitution, and with no "
        "repo-picking environment such as GH_REPO or GIT_DIR. E.g. `git push origin <branch>` or "
        "`gh pr merge <number> -R <owner>/<repo>`." + (_BODY_FILE_HINT if _is_text_with_code(unreadable.text) else "")
    )


# A backtick or $( in a comment's text reads as a command the shell may run; from a file it is only text.
_BODY_FILE_HINT = (
    " A comment or reply whose text shows code (backticks, $(…)): write the text to a file and pass it as "
    "`gh pr comment <number> --body-file <file>` or `gh api <path> -F body=@<file>`."
)


def _is_text_with_code(text: str) -> bool:
    return re.search(r"(?:^|\s)gh\s", text) is not None and ("`" in text or "$(" in text)


def _note(context: str) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": context}})


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


def _find_window_share(conn: sqlite3.Connection, surface: str | None) -> float:
    """A tab with a lead above it, not a lead itself, is a worker."""
    lead_surface = find_lead_of(conn, surface)[1]
    return WORKER_WINDOW_SHARE if lead_surface is not None and lead_surface != surface else COMPACT_AT_WINDOW_SHARE


def handle_hook(
    conn: sqlite3.Connection,
    event: str,
    payload: dict[str, object],
    env: Mapping[str, str],
    config: Config,
    event_ns: int,
    measure: bool = False,
    caller: HookCaller | None = None,
) -> HookResult:
    conversation_id = _str(payload, "session_id")
    cwd = _str(payload, "cwd")
    at_ms = event_ns // 1_000_000
    if caller is None:
        pid_text = env.get("CLAUDE_PID", "")
        if not pid_text.isdigit():
            return HookResult(None, "", "no-pid")
        pid = int(pid_text)
        found_start = process_start_ms(pid)
        if found_start is None:
            return HookResult(None, "", "no-process")
        proc_start = found_start
        # Everything that touches the filesystem happens before the write lock is taken.
        session_file = read_session_file(pid)
        proc = process_args(pid)
        nested = proc is not None and is_nested_agent(pid, proc.env)
        git = read_git_info(cwd) if cwd else None
        surface_seen = None if nested else map_env_to_surface(env)
        transcript_path = _str(payload, "transcript_path")
    else:
        # Its files are on the host: no session file, git from the host, no transcript to read here.
        pid, proc_start, session_file, nested = caller.pid, caller.proc_start_ms, None, False
        git, surface_seen, transcript_path = caller.git, caller.surface, None
    pr_repo = config.pr_repo_for(git.repo if git else None)
    # A subagent's hooks carry the parent's session and transcript, but its tool results never reach the main
    # context and nothing injected there reaches the main conversation: no context reading, mail or roles.
    in_subagent = bool(_str(payload, "agent_id"))
    reads_context = bool(config.compact_at_tokens) and event in _CONTEXT_EVENTS
    # A cut turn's tail tells whether the model answered since bridge last resumed it.
    tail = (
        read_transcript_tail(Path(transcript_path))
        if transcript_path and not in_subagent and (reads_context or event == "StopFailure")
        else None
    )
    text = _str(payload, "prompt") if event == "UserPromptSubmit" else _str(payload, "last_assistant_message")
    refs = extract_refs(text, issue_prefixes=config.issue_prefixes, default_repo=pr_repo) if text else []
    observation = Observation(
        pid=pid,
        proc_start_ms=proc_start,
        conversation_id=conversation_id,
        cwd=cwd,
        surface=surface_seen,
        kind=caller.kind if caller else session_file.kind if session_file else None,
        peer_name=session_file.name if session_file else None,
        version=session_file.version if session_file else None,
        peer_messaging=session_file.peer_messaging if session_file else None,
        transcript_path=transcript_path,
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
    worker_compacted = False
    reopened = 0
    packed: Packed | None = None
    cut_mail: list[tuple[int, Recipient]] = []
    surface = observation.surface.surface if observation.surface else None
    drive: str | None = None
    can_drive = not nested and caller is None and map_env_to_can_drive(env)
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
                reopen_before_clear(conn, previous)
                retarget_open_messages(conn, previous, conversation_id, instance_id)
            if source == "compact" and conversation_id and observed.current:
                reopened = reset_after_compaction(conn, conversation_id, at_ms)
                # Only a compaction bridge started is followed by its "continue"; any other one ends the cycle.
                if take_phase(conn, conversation_id, PHASE_COMPACTING, PHASE_CONTINUE):
                    drive = "continue"
                take_phase(conn, conversation_id, PHASE_REQUESTED, None)
                kept = load_keep(conn, conversation_id)
                if kept:
                    restored = convert_keep_to_part(kept[0], kept[1], at_ms)
                worker_compacted = not is_surface_locked(conn, config, surface) and is_nested_tab(conn, surface)
            pinnable = not nested and caller is None and env.get("AGTERM_SESSION_ID")
            if pinnable and source in _PIN_SOURCES:
                pin_conversation = conversation_id
        elif event == "UserPromptSubmit" and conversation_id and is_harness_prompt(text or ""):
            # Task notifications and similar harness messages arrive as prompts but are not the user's words.
            if observed.current:
                record_activity(conn, instance_id, "active", event_ns)
            if (text or "").lstrip().startswith(TASK_NOTIFICATION):
                # Mail or a finished task started a turn after compaction: it goes on without a typed "continue".
                take_phase(conn, conversation_id, PHASE_CONTINUE, None)
        elif event == "UserPromptSubmit" and conversation_id:
            fresh = record_prompt(conn, conversation_id, text or "", prompt_id, at_ms)
            # The user picked the conversation up again; a typed "continue" would now land on top of their work.
            take_phase(conn, conversation_id, PHASE_CONTINUE, None)
            if fresh and observed.current:
                record_activity(conn, instance_id, "active", event_ns)
            if fresh:
                conn.execute(
                    "UPDATE conversations SET owner_prompt_at = ? WHERE conversation_id = ?", (at_ms, conversation_id)
                )
                add_refs(conn, conversation_id, refs, "prompt", at_ms)
                add_branch_refs(conn, conversation_id, git.branch if git else None, config, at_ms)
                nudge = (
                    take_focus_nudge(
                        conn,
                        conversation_id,
                        at_ms,
                        lead=conversation_id in find_lead_conversations(conn),
                    )
                    if observed.current
                    else None
                )
                if nudge:
                    contexts.append(nudge)
        elif event == "Stop":
            if observed.current:
                record_activity(conn, instance_id, "idle", event_ns)
            if (
                conversation_id
                and observed.current
                # A request bridge sent as a message may still wait for this very turn to end to be typed.
                and is_request_delivered(conn, conversation_id)
                and take_phase(conn, conversation_id, PHASE_REQUESTED, PHASE_COMPACTING)
            ):
                drive = "compact"
                # The drive starts now, however long ago the request went out: serve's watcher dates its end by this.
                conn.execute(
                    "UPDATE conversations SET compact_requested_at = ? WHERE conversation_id = ?",
                    (at_ms, conversation_id),
                )
            if conversation_id and text:
                record_reply(conn, conversation_id, text, at_ms)
                add_refs(conn, conversation_id, refs, "reply", at_ms)
            if conversation_id and observed.current and not in_subagent:
                reminder = take_answers_reminder(conn, config, conversation_id, surface, text or "")
                if reminder:
                    save_later_parts(conn, conversation_id, [*take_later_parts(conn, conversation_id), Part(reminder)])
        elif event == "StopFailure" and conversation_id and observed.current and not in_subagent:
            # No Stop follows: without this the tab stays "active", so no probe may tell its grey suggestion apart.
            record_activity(conn, instance_id, "idle", event_ns)
            # A request to save notes and end the turn died with it; the next prompt asks again.
            if read_phase(conn, conversation_id) == PHASE_REQUESTED:
                give_up_driving(conn, conversation_id)
            if caller is None and not nested:
                answered_at = tail.reading.at_ms if tail and tail.reading else None
                error = _str(payload, "error")
                cut_mail = take_turn_cut(conn, config, conversation_id, surface, error, text, answered_at, at_ms)
        elif event == "Notification":
            if observed.current:
                set_attention(conn, instance_id, "permission", event_ns)
            # The next tool call must reach Python so the attention flag clears promptly.
            hint_due = 0
        elif event == "SessionEnd":
            if observed.current and _str(payload, "reason") not in _NON_TERMINAL_END_REASONS:
                mark_ended(conn, instance_id, at_ms, event_ns)
        # A cut turn's failure counts at once: a context over its window is then compaction's to cure, not a resume's.
        failure_counts = reads_context or (event == "StopFailure" and bool(config.compact_at_tokens))
        if failure_counts and tail is not None and conversation_id and observed.current:
            record_failed_turns(conn, conversation_id, tail)
        if reads_context and tail is not None and tail.reading is not None and conversation_id and observed.current:
            warning = record_context(
                conn,
                conversation_id,
                tail.reading,
                # The usage predates this prompt or tool result; a large one can jump straight to compaction.
                estimate_pending_tokens(event, payload),
                config.compact_at_tokens,
                _find_window_share(conn, surface),
                can_deliver=event in _DELIVERY_EVENTS,
                driven=can_drive,
                at_ms=at_ms,
            )
            if warning:
                contexts.append(warning)
        if event in _DELIVERY_EVENTS and not in_subagent:
            leftover = False
            # A locked tab is left out: its mail waits, unacknowledged, and it gets no reminders. So does a tab whose
            # context is full: the request carrying it would fail, and it comes after compaction instead.
            owner = bool(
                conversation_id
                and observed.current
                and is_delivery_owner(conn, conversation_id, instance_id)
                and not is_conversation_locked(conn, config, conversation_id)
                and not is_context_full(conn, conversation_id)
            )
            # What waited from the last message goes first; a new start rebuilds roles and notes, so those copies go.
            parts = take_later_parts(conn, conversation_id) if owner and conversation_id else []
            if starting:
                parts = [p for p in parts if not p.rebuilt]
            if pending is not None:
                parts = [*pending.later, *parts]
            parts, waiting_user = split_user_parts(parts)
            typed: tuple[tuple[int, tuple[Part, ...]], ...] = ()
            # A full context would fail the request carrying them; the typed line names bridge show for that case.
            if event == "UserPromptSubmit" and conversation_id and not is_context_full(conn, conversation_id):
                typed = find_cut_user_parts(conn, conversation_id, text or "")
                # Typed prompts are harness prompts, which record no prompt_id unless marked here.
                if typed and prompt_id is not None and not mark_prompt_seen(conn, conversation_id, prompt_id, at_ms):
                    typed = ()
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
                elif worker_compacted and not lead and give_roles:
                    # A worker read the constitution in its brief; compaction drops it, so it comes back here.
                    parts.append(read_constitution_part())
            packed = pack_parts([*parts, *(Part(c) for c in contexts)])
            if owner and conversation_id and packed.room < _MAIL_MIN_ROOM:
                # Taking mail marks it offered; with no room it waits for the next event instead.
                leftover = True
                # Its older unread ones wait with it, in order; other mail stays unoffered.
                older = take_user_messages(conn, conversation_id, at_ms) if typed else ()
                packed = add_parts(packed, merge_user_messages(waiting_user, older, typed))
                save_later_parts(conn, conversation_id, packed.later)
            elif owner and conversation_id:
                # Notices and mail share the inbox budget; roles and notes stay outside it, as their own memory.
                spent = sum(len(c.encode()) + 2 for c in contexts)
                budget = min(INJECT_BUDGET_BYTES - spent, packed.room)
                release_dead_typing_claims(conn, at_ms, conversation_id)
                delivery = take_delivery(conn, conversation_id, at_ms, starting, budget_bytes=budget)
                # The user's words still waiting, unread, given again and typed, oldest message first.
                packed = add_parts(packed, merge_user_messages(waiting_user, delivery.user_messages, typed))
                if delivery.context:
                    packed = add_part(packed, Part(delivery.context, "your bridge messages", "bridge inbox"))
                leftover = delivery.leftover
                save_later_parts(conn, conversation_id, packed.later)
            else:
                packed = add_parts(packed, merge_user_messages(waiting_user, typed))
                if conversation_id and packed.later:
                    save_later_parts(conn, conversation_id, take_later_parts(conn, conversation_id) + packed.later)
            if conversation_id:
                mark_user_parts_shown(conn, conversation_id, packed.shown, at_ms)
            # Waiting parts must reach the very next tool result, not one 30 s later.
            hint_due = next_hint_epoch(leftover or bool(packed.later))
            if conversation_id and is_near_threshold(
                conn, conversation_id, config.compact_at_tokens, _find_window_share(conn, surface)
            ):
                # Close to the warning, one long tool run could reach compaction between 30 s checks.
                hint_due = int(time.time())
        if measure:
            record_hook_run(conn, event, instance_id, at_ms)
    if start_recorded and conversation_id:
        try:
            clear_pending_start(conversation_id)
        except OSError:
            log_error("start-pending", traceback.format_exc())
    if hint_due is not None and caller is None:
        try:
            write_hint(pid, hint_due)
        except OSError:
            log_error("hint", traceback.format_exc())
    for message_id, recipient in cut_mail:
        try:
            if recipient.pid:
                write_hint(recipient.pid, HINT_SENT)
            spawn_typed_delivery(message_id, [recipient])
        except OSError:
            log_error("turn cut", traceback.format_exc())
    keep = map_packed_to_keep_status(restored, packed)
    if (
        drive
        and conversation_id
        and instance_id
        and not spawn_compact_drive(drive, instance_id, conversation_id, keep, reopened=reopened > 0)
    ):
        try:
            with transaction(conn):
                give_up_driving(conn, conversation_id)
        except sqlite3.Error:
            log_error("compact-drive", traceback.format_exc())
    text = packed.text if packed else "\n\n".join(contexts)
    output = convert_context_to_hook_output(event, cap_context(text)) if text else ""
    return HookResult(instance_id, output, "ok", pin_conversation, hint_due)


def record_made_up_start(
    conn: sqlite3.Connection, conversation_id: str, pending: PendingStart, observed: ObserveResult, surface: str | None
) -> Part | None:
    """Records a start its own hook could not; returns the saved list when it has not reached the session yet."""
    forget_lead_role(conn, surface)
    forget_assistant_role(conn, surface)
    set_start_source(conn, conversation_id, pending.source)
    previous = observed.previous_conversation_id
    if pending.source == "clear" and previous and previous != conversation_id:
        reopen_before_clear(conn, previous)
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
    pin = not nested and source in _PIN_SOURCES and bool(env.get("AGTERM_SESSION_ID"))
    output = convert_context_to_hook_output("SessionStart", cap_context(text)) if text else ""
    return HookResult(instance_id, output, "fallback", conversation_id if pin else None)


def take_answers_reminder(
    conn: sqlite3.Connection, config: Config, conversation_id: str, surface: str | None, reply: str
) -> str | None:
    """The assistant's turn that answered the owner's own prompt with a link and posted nothing to the feed gets one
    line at its next prompt or tool call. Every turn's end clears the prompt's mark, so it is said once."""
    row = conn.execute(
        "SELECT owner_prompt_at FROM conversations WHERE conversation_id = ?", (conversation_id,)
    ).fetchone()
    if row is None or row["owner_prompt_at"] is None:
        return None
    asked_at = int(row["owner_prompt_at"])
    conn.execute("UPDATE conversations SET owner_prompt_at = NULL WHERE conversation_id = ?", (conversation_id,))
    if not _RESULT_LINK_RE.search(reply) or not is_assistant_surface(conn, config, surface):
        return None
    posted = conn.execute(
        "SELECT 1 FROM user_items WHERE from_conversation = ? AND created_at >= ? LIMIT 1", (conversation_id, asked_at)
    ).fetchone()
    if posted:
        return None
    return (
        f"[bridge] Your reply to {config.owner_name} carried a link and this turn posted nothing to the feed: if it is "
        "a result they will want again, `bridge post done '<title>' --body - --link URL [--ask N] [--pin]` puts it "
        "in their Answers. Not a refusal; skip it when it is not a result."
    )


def is_harness_prompt(text: str) -> bool:
    return text.lstrip().startswith(_HARNESS_PROMPT_PREFIXES)


def record_hook_run(conn: sqlite3.Connection, event: str, instance_id: str, at_ms: int) -> None:
    """Records wall time since this process started, so interpreter start-up and lock waits are included."""
    duration_us = process_age_us(os.getpid()) or 0
    cursor = conn.execute(
        "INSERT INTO hook_runs (at, event, instance_id, duration_us, outcome) VALUES (?, ?, ?, ?, 'ok')",
        (at_ms, event, instance_id, duration_us),
    )
    if cursor.lastrowid and cursor.lastrowid % 500 == 0:
        conn.execute("DELETE FROM hook_runs WHERE id <= ?", (cursor.lastrowid - _HOOK_RUNS_KEPT,))


def spawn_compact_drive(
    phase: str,
    instance_id: str,
    conversation_id: str,
    keep: str,
    claim: bool = False,
    reopened: bool = False,
    generic_keep: bool = False,
    by_screen: bool = False,
) -> bool:
    # Detached: it waits for the tab to be idle, which happens only after this hook returns.
    argv = [sys.executable, "-m", "bridge", "_compact-drive", phase, instance_id, conversation_id, "--keep", keep]
    flags = [("--claim", claim), ("--reopened", reopened), ("--generic-keep", generic_keep), ("--by-screen", by_screen)]
    try:
        subprocess.Popen(
            [*argv, *(flag for flag, on in flags if on)],
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
