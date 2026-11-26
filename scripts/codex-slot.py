#!/usr/bin/env python3
# At most SLOTS Codex reviews at a time on this Mac (laptop heat). `codex exec|e|review` takes one of SLOTS OS file
# locks and stays as codex's parent holding it: codex's launcher replaces itself with the native binary through
# execve, which drops an inherited lock. Codex runs in its own process group; a stop signal is passed to the whole
# group, and whatever codex leaves behind in it (helper processes) is ended when it exits. Interactive codex runs
# unlocked and directly. Called by codex-slot.mjs, which /opt/homebrew/bin/codex links to; installed as
# ~/.guild/bin/codex-slot.py. bridge's guard reads its slot records (bridge/codex_reads.py).
import contextlib
import fcntl
import os
import signal
import subprocess
import sys
import time

REAL = os.environ.get("CODEX_SLOT_REAL", "/opt/homebrew/lib/node_modules/@openai/codex/bin/codex.js")
SLOTS = 3
LOCKS = os.path.expanduser("~/.local/state/guild/codex-slot")
LOCKED = {"exec", "e", "review"}
LOG = os.path.expanduser("~/.local/state/guild/codex-slot.log")


def log(text: str) -> None:
    with open(LOG, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} pid {os.getpid()} {text}\n")


def take_slot() -> int:
    os.makedirs(LOCKS, exist_ok=True)
    fds = [os.open(f"{LOCKS}/{n}.lock", os.O_CREAT | os.O_RDWR, 0o600) for n in range(1, SLOTS + 1)]
    started, told = time.time(), False
    while True:
        for fd in fds:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            for other in fds:
                if other != fd:
                    os.close(other)
            if told:
                sys.stderr.write(f"codex: got a slot after {int(time.time() - started)} s\n")
            return fd
        if not told:
            holders = "; ".join(os.pread(fd, 200, 0).decode(errors="replace").strip() for fd in fds)
            sys.stderr.write(f"codex: waiting for one of the Mac's {SLOTS} Codex slots (held by: {holders})\n")
            told = True
        time.sleep(2)


def end_group(pgid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


def read_dir(args: list[str]) -> str:
    """The directory Codex reads, -C/--cd or this one: bridge's guard refuses git rebase/reset/checkout/switch/pull
    in that worktree while the slot is held."""
    for at, arg in enumerate(args):
        if arg in ("-C", "--cd") and at + 1 < len(args):
            return os.path.abspath(os.path.expanduser(args[at + 1]))
        if arg.startswith("--cd="):
            return os.path.abspath(os.path.expanduser(arg[len("--cd="):]))
    return os.getcwd()


args = sys.argv[1:]
if next((a for a in args if not a.startswith("-")), "") not in LOCKED:
    os.execv(REAL, [REAL, *args])

asked = time.time()
fd = take_slot()
reads = read_dir(args)
log(f"start after {int(time.time() - asked)} s wait, ppid {os.getppid()}, in {reads}")
os.ftruncate(fd, 0)
os.pwrite(fd, f"pid {os.getpid()} since {time.strftime('%H:%M:%S')} in {reads}".encode(), 0)
# Low CPU priority: a review must not slow the owner's own work on the laptop.
child = subprocess.Popen([REAL, *args], start_new_session=True, preexec_fn=lambda: os.nice(15))
for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(sig, lambda s, _f: end_group(child.pid, s))
code = child.wait()
log(f"end, exit {code}, ran {int(time.time() - asked)} s")
end_group(child.pid, signal.SIGTERM)
sys.exit(code if code >= 0 else 128 - code)
