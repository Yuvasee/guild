import fcntl
import glob
import os
import re
from dataclasses import dataclass

from bridge.gitinfo import read_git_info
from bridge.guard import GIT_VALUE_OPTIONS, SHELL_KEYWORDS, segment_command

# Written by the owner's codex-slot.py, which every `codex exec|e|review` on the Mac goes through: one file per slot,
# "pid <pid> since <HH:MM:SS> in <dir>", locked with flock for as long as that run lives.
CODEX_SLOT_DIR = "~/.local/state/guild/codex-slot"
_RECORD_RE = re.compile(r"pid (\d+) since (\S+) in (/.*)")
_TREE_MOVES = frozenset({"rebase", "reset", "checkout", "switch", "pull"})
_CODEX_READING = (
    "bridge: Codex (pid {pid}, since {since}) is reading {root}; git {verb} there would change its files under the "
    "review. Wait until that run ends (ps -p {pid}), or do this in another worktree."
)


@dataclass(frozen=True)
class CodexRead:
    pid: str
    since: str
    directory: str


def find_codex_read_refusal(command: str, cwd: str) -> str | None:
    """A git command that moves a worktree's files while a Codex run reads that worktree."""
    moves = _list_tree_moves(command, cwd)
    reads = list_codex_reads() if moves else []
    for verb, directory in moves:
        root = _find_worktree_root(directory)
        for read in reads:
            if root is not None and root == _find_worktree_root(read.directory):
                return _CODEX_READING.format(pid=read.pid, since=read.since, root=root, verb=verb)
    return None


def list_codex_reads() -> list[CodexRead]:
    """Live runs only: a record whose run has died has no lock left on its file, as the OS drops it with the
    process."""
    reads: list[CodexRead] = []
    for path in sorted(glob.glob(os.path.join(os.path.expanduser(CODEX_SLOT_DIR), "*.lock"))):
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            if match := _RECORD_RE.fullmatch(os.read(fd, 4096).decode(errors="replace").strip()):
                reads.append(CodexRead(*match.groups()))
        finally:
            os.close(fd)
    return reads


def _list_tree_moves(command: str, cwd: str) -> list[tuple[str, str]]:
    """Each git rebase, reset, checkout, switch or pull with the directory it runs in, following cd and git -C; none
    after a cd to a path the shell builds."""
    moves: list[tuple[str, str]] = []
    directory: str | None = cwd
    for tokens, _ in segment_command(command):
        while tokens and tokens[0] in SHELL_KEYWORDS:
            tokens = tokens[1:]
        if tokens[:1] == ["cd"]:
            destination = tokens[1] if len(tokens) > 1 else "~"
            hidden = destination == "-" or any(c in destination for c in "$`")
            directory = None if directory is None or hidden else _join(directory, destination)
            continue
        if not tokens or os.path.basename(tokens[0]) != "git" or directory is None:
            continue
        here, at = directory, 1
        while at < len(tokens) and tokens[at].startswith("-"):
            if tokens[at] == "-C" and at + 1 < len(tokens):
                here = _join(here, tokens[at + 1])
            elif tokens[at].startswith("-C"):
                here = _join(here, tokens[at][2:])
            at += 2 if tokens[at] in GIT_VALUE_OPTIONS else 1
        verb = tokens[at] if at < len(tokens) else ""
        if verb in _TREE_MOVES and not {"-h", "--help"} & set(tokens[at + 1 :]):
            moves.append((verb, here))
    return moves


def _join(base: str, path: str) -> str:
    return os.path.normpath(os.path.join(base, os.path.expanduser(path)))


def _find_worktree_root(directory: str) -> str | None:
    info = read_git_info(os.path.realpath(directory))
    return info.repo_root if info else None
