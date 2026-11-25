"""Workers whose Claude runs on a remote host in tmux, shown in a local agterm tab that only keeps reattaching."""

import contextlib
import hashlib
import hmac
import json
import os
import posixpath
import random
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
import termios
import time
from dataclasses import asdict, dataclass, replace
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Any, cast

from bridge.config import Config, Remote, db_path, load_config, read_overlay, state_dir
from bridge.db import connect, transaction
from bridge.errors import UsageError
from bridge.gitinfo import GitInfo
from bridge.procinfo import process_start_ms
from bridge.registry import PROC_START_TOLERANCE_MS, Observation, Surface, observe_instance, record_activity
from bridge.remote import build_shared_connection_options, make_shared_control_path
from bridge.restore import pin_restore_command

HOST_KIND_PREFIX = "host:"
SETUP_TIMEOUT_S = 90
SSH_CONNECT_TIMEOUT_S = 15
TUNNEL_PORTS = (20000, 59999)
RECONNECT_BACKOFF_S = (2, 4, 8, 15, 30)
GONE_HOLD_S = 8
TMUX_SOCKET = "gbridge"
# Never the host user's tmux.conf: its restore plugin re-created all their sessions inside our server.
TMUX = f"tmux -L {TMUX_SOCKET} -f /dev/null"
HOST_WORKER_RE = re.compile(r"g-[a-z0-9][a-z0-9-]{0,48}")
_CONVERSATION_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SLUG_MAX_CHARS = 32
# Kept for the tab's processes: a test serve and its state run beside the live one.
_PASSED_ENV = ("BRIDGE_STATE_DIR", "BRIDGE_CONFIG")
CLAUDE_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
BRIDGE_SKILL_DIR = "~/.claude/skills/bridge"
# Every launch carries them over ssh's stdin; ours come to about 300 KB.
RULE_FILES_MAX_BYTES = 1_000_000
REVIEW_DOC_CHECKER = "review_doc_check.py"
SYNC_TIMEOUT_S = 60
_SKIPPED_RULE_FILE_RE = re.compile(r"^\.|\.bak|\.proposed$|\.orig$")


@dataclass(frozen=True)
class HostWorker:
    worker: str
    name: str
    host: str
    ssh: str
    cwd: str
    remote_cwd: str
    remote_dir: str
    lead: str
    parent: str
    token_sha256: str
    created_at: int
    tab: str = ""
    loop_pid: int = 0
    loop_start_ms: int = 0
    pane: str = ""
    agterm_session: str = ""
    agterm_workspace: str = ""
    remote_checkout: str = ""
    """The mapped checkout on the host, which the worker's own worktree (remote_worktree) was made from."""
    remote_worktree: str = ""
    model_flags: str = ""
    """--model and --effort it was started with; a resume gets them too."""


def host_workers_dir() -> Path:
    return state_dir() / "host-workers"


def record_path(worker: str) -> Path:
    if not HOST_WORKER_RE.fullmatch(worker):
        raise UsageError(f"bad host worker name {worker!r}")
    return host_workers_dir() / f"{worker}.json"


def write_record(record: HostWorker) -> None:
    path = record_path(record.worker)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(asdict(record), f)
    tmp.replace(path)


def read_record(worker: str) -> HostWorker | None:
    try:
        raw: Any = json.loads(record_path(worker).read_text())
    except (OSError, ValueError, UsageError):
        return None
    if not isinstance(raw, dict):
        return None
    fields = set(HostWorker.__dataclass_fields__)
    try:
        return HostWorker(**{k: v for k, v in cast(dict[str, Any], raw).items() if k in fields})
    except TypeError:
        return None


def list_records() -> list[HostWorker]:
    directory = host_workers_dir()
    names = sorted(p.stem for p in directory.glob("g-*.json")) if directory.is_dir() else []
    return [record for record in map(read_record, names) if record is not None]


def find_record_by_tab(tab: str) -> HostWorker | None:
    return next((r for r in list_records() if r.tab == tab), None)


def is_tab_loop_running(record: HostWorker) -> bool:
    """Its tab's process alone stands for the worker on this Mac: hooks and gbridge act through it."""
    if not record.loop_pid:
        return False
    start = process_start_ms(record.loop_pid)
    return start is not None and abs(start - record.loop_start_ms) <= PROC_START_TOLERANCE_MS


def delete_record(worker: str) -> None:
    record_path(worker).unlink(missing_ok=True)
    conversations_path(worker).unlink(missing_ok=True)


def conversations_path(worker: str) -> Path:
    return record_path(worker).with_suffix(".conversations")


def read_bound_conversations(worker: str) -> list[str]:
    try:
        return conversations_path(worker).read_text().split()
    except OSError:
        return []


def bind_conversation(conn: sqlite3.Connection, worker: str, conversation_id: str) -> bool:
    """A worker speaks only for conversations it began: a token must never act as the user or another session."""
    bound = read_bound_conversations(worker)
    if conversation_id in bound:
        if bound[-1] != conversation_id:
            append_conversation(worker, conversation_id)
        return True
    if not _CONVERSATION_RE.fullmatch(conversation_id):
        return False
    if conn.execute("SELECT 1 FROM conversations WHERE conversation_id = ?", (conversation_id,)).fetchone():
        return False
    append_conversation(worker, conversation_id)
    return True


def append_conversation(worker: str, conversation_id: str) -> None:
    # Appended, never rewritten: the tab's loop rewrites the record itself at the same time.
    fd = os.open(conversations_path(worker), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(conversation_id + "\n")


def register_tab_instance(record: HostWorker) -> None:
    """A reopened tab is a new process here, and an idle worker sends no hook to say it lives on in it."""
    bound = read_bound_conversations(record.worker)
    if not bound or not record.pane:
        return
    conversation_id = bound[-1]
    conn = connect(db_path())
    try:
        with transaction(conn):
            previous = conn.execute(
                """
                SELECT i.status, i.repo_root, i.repo, i.branch FROM instances i
                WHERE i.conversation_id = ? ORDER BY i.proc_start_ms DESC LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
            if previous is None or previous["status"] == "ended":
                return
            git = (
                GitInfo(str(previous["repo_root"]), previous["branch"], previous["repo"])
                if previous["repo_root"]
                else None
            )
            observation = Observation(
                pid=record.loop_pid,
                proc_start_ms=record.loop_start_ms,
                conversation_id=conversation_id,
                cwd=record.remote_cwd,
                surface=Surface(record.pane, record.agterm_session or None, record.agterm_workspace or None),
                kind=f"{HOST_KIND_PREFIX}{record.host}",
                git=git,
            )
            instance_id = observe_instance(conn, observation, int(time.time() * 1000)).instance_id
            record_activity(conn, instance_id, str(previous["status"]), time.time_ns())
    finally:
        conn.close()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def authenticate(worker: str, token: str) -> HostWorker | None:
    record = read_record(worker)
    if record is None or not token:
        return None
    return record if hmac.compare_digest(record.token_sha256, hash_token(token)) else None


def read_local_model_flags(claude_dir: Path | None = None, model_override: str = "") -> str:
    """--model and --effort as a Claude started on this Mac gets them from its user settings (no model there: the
    account's default model), for a host whose own Claude settings may name another; model_override wins."""
    directory = claude_dir or Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    try:
        raw: Any = json.loads((directory / "settings.json").read_text())
    except (OSError, ValueError):
        raw = {}
    settings = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
    model, effort = model_override or settings.get("model"), settings.get("effortLevel")
    flags = ["--model", model if isinstance(model, str) and model.strip() else "default"]
    if isinstance(effort, str) and effort in CLAUDE_EFFORT_LEVELS:
        flags += ["--effort", effort]
    return shlex.join(flags)


def find_remote(config: Config, name: str) -> Remote:
    remote = next((r for r in config.remotes if r.name == name), None)
    if remote is None:
        raise UsageError(f"worker_host {name!r} is not a [[remotes]] name in the bridge config")
    return remote


def map_cwd_to_host_path(cwd: Path, path_map: dict[str, str]) -> str | None:
    """The longest mapped checkout that holds cwd decides; symlinks are compared resolved too."""
    candidates = dict.fromkeys([cwd, cwd.resolve()])
    best: tuple[int, str] | None = None
    for local, remote in path_map.items():
        root = Path(os.path.expanduser(local))
        for root_form in dict.fromkeys([root, root.resolve()]):
            for path in candidates:
                if path == root_form or root_form in path.parents:
                    relative = path.relative_to(root_form)
                    target = remote.rstrip("/") + ("" if str(relative) == "." else f"/{relative.as_posix()}")
                    if best is None or len(root_form.parts) > best[0]:
                        best = (len(root_form.parts), target)
    return best[1] if best else None


def find_mapped_root(host_path: str, path_map: dict[str, str]) -> str | None:
    """The host side of the longest worker_path_map entry that holds host_path."""
    path = PurePosixPath(posixpath.normpath(host_path))
    best: PurePosixPath | None = None
    for remote in path_map.values():
        root = PurePosixPath(posixpath.normpath(remote))
        if (path == root or root in path.parents) and (best is None or len(root.parts) > len(best.parts)):
            best = root
    return str(best) if best else None


def list_readable_roots(record: HostWorker, path_map: dict[str, str]) -> tuple[str, ...]:
    """Where the guard reads a host worker's checkouts: its mapped root and its own worktree."""
    checkout = record.remote_checkout or record.remote_cwd
    mapped = (find_mapped_root(checkout, path_map) or record.remote_checkout) if checkout else ""
    worktree = record.remote_worktree or record.remote_cwd
    return tuple(dict.fromkeys(r for r in (mapped, worktree) if r.startswith("/")))


def make_worker_name(task: str) -> str:
    words = re.findall(r"[a-z0-9]+", task.lower())
    slug = "-".join(words)[:_SLUG_MAX_CHARS].strip("-") or "worker"
    return f"g-{slug}-{secrets.token_hex(2)}"


def new_token() -> str:
    return secrets.token_hex(32)


def build_host_brief_note(config: Config, host: str, worker: str, lead: str, mapped_root: str) -> str:
    codex = read_overlay(config, "host-worker-codex", lead=lead)
    codex_part = f"{codex}\n\n" if codex else ""
    return (
        f"# Where you run\n\nYou run on {host}, in tmux session {worker} (its own socket: tmux -L {TMUX_SOCKET}), "
        "reached from the owner's Mac through an ssh tunnel; the Mac shows you in an agterm tab under your lead. "
        "Talk to bridge with `gbridge`, never `bridge`: on Linux `bridge` is the network tool. gbridge takes the "
        "same arguments for send, reply, ack, done, decline, focus, keep, inbox and show; wherever a message, skill "
        "or rule says `bridge <command>`, run `gbridge <command>`. Other bridge commands (post, board, claim, guild) "
        f"do not exist here: ask your lead ({lead}) with gbridge send instead. Paths on the Mac (/Users/...) are not "
        "reachable from here. If gbridge says the Mac is unreachable, keep working and retry later: the Mac may be "
        "asleep, and nothing here stops when it is. Pushes and merges go through bridge's guard on the Mac; while "
        "it cannot be reached they are refused.\n\n"
        f"You work in a git worktree of your own, ~/.local/state/gbridge/worktrees/{worker}, made from the host's "
        "checkout at its HEAD (detached): others use that checkout, so never work in it. Create your branch in your "
        "worktree. When you are closed it is removed if it has no uncommitted changes and no commit on no branch; "
        "otherwise it stays. The guard reads a push or merge run in your worktree or in another git checkout under "
        f"{mapped_root} (e.g. a worktree you add there, with `cd <it> && git push ...`); one run anywhere else is "
        "refused.\n\n"
        f"{codex_part}"
        f"Files someone will read after you (a review doc, a report) go under {mapped_root}/_tmp or a worktree there, "
        "never in ~/.local/state/gbridge: that folder goes when your tab is closed.\n"
    )


def read_host_rule_files(config: Config) -> tuple[list[dict[str, Any]], list[str]]:
    """The bridge skill and overlay_dir as a host worker must read them, at their paths relative to this Mac's home,
    which host_side writes under the host's home; and why any folder is not sent.

    Top-level text files only. Over RULE_FILES_MAX_BYTES none is sent: half a set would mix old rules with new.
    """
    home = Path.home()
    folders = [(Path(BRIDGE_SKILL_DIR).expanduser(), 0o644, False)]
    if config.overlay_dir:
        folders.append((Path(config.overlay_dir).expanduser(), 0o600, False))
    folders += [(Path(d).expanduser(), 0o644, True) for d in config.host_sync_dirs]
    # A host has no guild to run `guild review doc`; the skill tells its workers to run this copy of its checks.
    checker = files("bridge").joinpath(REVIEW_DOC_CHECKER).read_text()
    rule_files: list[dict[str, Any]] = []
    notes: list[str] = []
    size = len(checker.encode("utf-8"))
    for folder, mode, nested in folders:
        if not folder.is_dir():
            continue
        if not folder.is_relative_to(home):
            notes.append(f"{folder} is not under {home}, so it has no path on the host")
            continue
        try:
            paths = sorted(folder.rglob("*") if nested else folder.iterdir())
        except OSError as exc:
            notes.append(f"{folder} could not be listed ({exc}); not sent")
            continue
        for path in paths:
            try:
                skipped = any(_SKIPPED_RULE_FILE_RE.search(part) for part in path.relative_to(folder).parts)
                if skipped or not path.is_file():
                    continue
                size += path.stat().st_size
                if size > RULE_FILES_MAX_BYTES:
                    notes.append(f"rule files come to over {RULE_FILES_MAX_BYTES} bytes, the cap; none was sent")
                    return [], notes
                text = path.read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                continue
            except OSError as exc:
                notes.append(f"{path} could not be read ({exc}); not sent")
                continue
            if "\0" in text:
                continue
            relative = folder.relative_to(home) / path.relative_to(folder)
            rule_files.append({"path": relative.as_posix(), "text": text, "mode": mode})
    path = PurePosixPath(BRIDGE_SKILL_DIR.removeprefix("~/"), REVIEW_DOC_CHECKER)
    rule_files.append({"path": path.as_posix(), "text": checker, "mode": 0o644})
    return rule_files, notes


def sync_rules_to_remotes(config: Config) -> tuple[list[str], bool]:
    """Brings each remote's rule files up to what a host worker started now would get, and checks each by its hash
    there. Returns lines to print and whether every remote is in step; one that cannot be reached is not. A folder
    left out (an overlay outside home) is named, as at a start, and the rest still goes."""
    rule_files, notes = read_host_rule_files(config)
    lines = [f"rule files: {note}" for note in notes]
    if not config.remotes:
        return [*lines, "no remotes configured"], True
    if not rule_files:
        return [*lines, "nothing was sent"], False
    expected = {f["path"]: hashlib.sha256(str(f["text"]).encode("utf-8")).hexdigest() for f in rule_files}
    in_step = True
    for remote in config.remotes:
        done = run_on_host(remote, {"op": "sync", "rule_files": rule_files}, SYNC_TIMEOUT_S)
        if not done.get("ok"):
            lines.append(f"{remote.name}: not synced: {done.get('error')}")
            in_step = False
            continue
        for path in cast(list[str], done.get("updated") or []):
            lines.append(f"{remote.name}: updated {path}")
        if done.get("warning"):
            lines.append(f"{remote.name}: {done['warning']}")
        hashes = cast(dict[str, Any], done.get("hashes") or {})
        stale = [path for path, digest in expected.items() if hashes.get(path) != digest]
        if stale:
            lines.append(f"{remote.name}: differs from the Mac after the sync: {', '.join(f'~/{p}' for p in stale)}")
            in_step = False
        else:
            lines.append(f"{remote.name}: {len(expected)} rule files match the Mac by sha256")
    return lines, in_step


def run_on_host(remote: Remote, payload: dict[str, Any], timeout_s: int = SETUP_TIMEOUT_S) -> dict[str, Any]:
    """The payload, token included, travels inside the script on ssh's stdin: never on a command line or in a log."""
    script = files("bridge").joinpath("host_side.py").read_text()
    program = f"{script}\nPAYLOAD = {json.dumps(payload)!r}\nPAYLOAD = __import__('json').loads(PAYLOAD)\nmain()\n"
    program = program.replace('\nif __name__ == "__main__":\n    main()\n', "\n")
    command = map_remote_to_ssh(remote, "python3 -I -S -")
    try:
        done = subprocess.run(
            command,
            input=program,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "unknown": True, "error": f"ssh to {remote.name} timed out after {timeout_s}s"}
    except OSError as exc:
        return {"ok": False, "unknown": True, "error": f"ssh to {remote.name} failed: {exc}"}
    lines = done.stdout.strip().splitlines()
    try:
        raw: Any = json.loads(lines[-1]) if lines else None
    except ValueError:
        raw = None
    if not isinstance(raw, dict):
        # No answer from the script: whatever it was asked may or may not have happened there.
        last = (done.stderr.strip().splitlines() or [f"exit {done.returncode}"])[-1][:300]
        return {"ok": False, "unknown": True, "error": f"ssh to {remote.name}: {last}"}
    return cast(dict[str, Any], raw)


def map_remote_to_ssh(remote: Remote, remote_command: str) -> list[str]:
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT_S}",
        *build_shared_connection_options(remote.ssh),
        "-o", "ForwardAgent=no",
        "-o", "ServerAliveInterval=10",
        "-o", "ServerAliveCountMax=3",
        remote.ssh,
        remote_command,
    ]  # fmt: skip


def read_gbridge_source() -> str:
    return files("bridge").joinpath("gbridge.py").read_text()


def build_tab_command(worker: str) -> str:
    env = [f"{name}={shlex.quote(value)}" for name in _PASSED_ENV if (value := os.environ.get(name))]
    # agterm keeps this as the tab's restore command: it must outlive this release, so the entry point on PATH.
    loop = shlex.join([shutil.which("bridge") or "bridge", "_host-tab", worker])
    # A login shell, as for local workers: agterm's own environment lacks what ssh's proxy commands need (aws).
    return "zsh -lc " + shlex.quote(" ".join(["exec", "env", *env, loop]) if env else f"exec {loop}")


def build_attach_command(record: HostWorker, port: int, api_port: int, shared: bool = False) -> list[str]:
    """The tunnel goes to the worker API only: the dashboard's own port takes actions from anything on localhost."""
    port_file = (
        shlex.quote(record.remote_dir + "/port")
        if record.remote_dir
        else f"~/.local/state/gbridge/{record.worker}/port"
    )
    remote = (
        f"printf %s {port} > {port_file} && exec {TMUX} attach-session -t {map_worker_to_exact_target(record.worker)}"
    )
    path = make_shared_control_path(record.ssh) if shared else None
    # Past sshd's MaxSessions (10 by default) a tab's session through the master is refused, and ssh's fallback
    # connection then fails on the forward the master already took for it: shared only where the host allows more.
    # Never the master itself: open_shared_connection starts that outside the tab.
    connection = ["-o", "ControlMaster=no", "-o", f"ControlPath={path}"] if path else ["-o", "ControlPath=none"]
    return [
        "ssh", "-t",
        "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT_S}",
        *connection,
        "-o", "ForwardAgent=no",
        "-o", "ServerAliveInterval=10",
        "-o", "ServerAliveCountMax=3",
        # A dropped link leaves sshd holding the old port for long: a refused forward means try a new one.
        "-o", "ExitOnForwardFailure=yes",
        "-R", f"127.0.0.1:{port}:127.0.0.1:{api_port}",
        record.ssh,
        remote,
    ]  # fmt: skip


def open_shared_connection(remote: Remote) -> None:
    """In a session of its own, so the master it may start outlives this tab. A failure is left to the attach,
    which then connects on its own."""
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            map_remote_to_ssh(remote, "true"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=SSH_CONNECT_TIMEOUT_S + 15,
            check=False,
            start_new_session=True,
        )


def read_tabs_share(host: str) -> bool:
    """Read at every attach: turning it off takes effect at the next reattach, without reopening tabs."""
    try:
        config = load_config()
    except (OSError, ValueError):
        return False
    return any(r.share_tabs for r in config.remotes if r.name == host)


def read_session_state(record: HostWorker) -> str:
    """present, gone, or unknown when the host cannot be asked."""
    remote = Remote(name=record.host, ssh=record.ssh)
    command = map_remote_to_ssh(remote, f"{TMUX} has-session -t {map_worker_to_exact_target(record.worker)}")
    try:
        done = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=SSH_CONNECT_TIMEOUT_S + 15,
            check=False,
            start_new_session=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if done.returncode == 0:
        return "present"
    # Only tmux's own answer: a shell or ssh error also exits 1, and "gone" removes the worker's files.
    if done.returncode == 1 and any(text in done.stderr for text in ("can't find session", "no server running")):
        return "gone"
    return "unknown"


def build_host_cleanup_command(record: HostWorker) -> str:
    target = map_worker_to_exact_target(record.worker)
    directory = shlex.quote(record.remote_dir) if record.remote_dir else f"~/.local/state/gbridge/{record.worker}"
    return f"{TMUX} kill-session -t {target}; rm -rf {directory}"


def map_worker_to_exact_target(worker: str) -> str:
    """'=' makes tmux match the whole name, not a prefix; quoted, since zsh as the remote shell expands =word."""
    if not HOST_WORKER_RE.fullmatch(worker):
        raise UsageError(f"bad host worker name {worker!r}")
    return f"'={worker}'"


def run_tab_loop(worker: str, api_port: int, agtermctl: str) -> int:
    """The tab's whole life: attach, and after any drop attach again until the host says the session is gone."""
    record = read_record(worker)
    if record is None:
        print(f"bridge: no record of host worker {worker}; nothing to attach to")
        time.sleep(GONE_HOLD_S)
        return 1
    start = process_start_ms(os.getpid()) or 0
    record = replace(
        record,
        loop_pid=os.getpid(),
        loop_start_ms=start,
        pane=os.environ.get("AGTERM_PANE_ID", ""),
        agterm_session=os.environ.get("AGTERM_SESSION_ID", ""),
        agterm_workspace=os.environ.get("AGTERM_WORKSPACE_ID", ""),
        tab=os.environ.get("AGTERM_SESSION_ID", "") or record.tab,
    )
    write_record(record)
    register_tab_instance(record)
    # agterm starts a --command tab afresh on relaunch only with a pinned restore command.
    pinned = pin_restore_command(build_tab_command(worker), os.environ, agtermctl)
    if not pinned.ok:
        print(f"bridge: agterm will not reopen this tab after a restart: {pinned.detail}")
    env = {**os.environ, "TERM": "xterm-256color"}
    remote = Remote(name=record.host, ssh=record.ssh)
    failures = 0
    port = random.randint(*TUNNEL_PORTS)
    while True:
        _flush_typed_input()
        started = time.monotonic()
        shared = read_tabs_share(record.host)
        if shared:
            open_shared_connection(remote)
        code = subprocess.run(build_attach_command(record, port, api_port, shared), env=env, check=False).returncode
        if code == 255:
            # Its forward may be refused next time. Kept otherwise: a master keeps a client's forward after it exits,
            # so a new port on each reattach would leave one more listener on the host each time.
            port = random.randint(*TUNNEL_PORTS)
        state = read_session_state(record)
        if state == "gone":
            print(f"\r\nbridge: {worker} has ended on {record.host}; this tab closes.")
            removed = run_on_host(remote, {"op": "remove", "worker": worker})
            if removed.get("kept"):
                print(f"bridge: on {record.host}, {removed['kept']}")
            if removed.get("ok"):
                delete_record(worker)
            else:
                print(f"bridge: could not remove {record.remote_dir} on {record.host}: {removed.get('error')}")
            time.sleep(GONE_HOLD_S)
            return 0
        if time.monotonic() - started > 60:
            failures = 0
        wait = RECONNECT_BACKOFF_S[min(failures, len(RECONNECT_BACKOFF_S) - 1)]
        failures += 1
        # ssh's fatal error lands in the frozen frame's input box; a cleared screen leaves no box to read as a draft.
        print(f"\033[2J\033[Hbridge: lost {record.host} (ssh exit {code}, session {state}); reattaching in {wait}s")
        time.sleep(wait)


def _flush_typed_input() -> None:
    """Keys typed while detached would otherwise reach Claude, stale, once the link is back."""
    with contextlib.suppress(OSError, termios.error, ValueError):
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
