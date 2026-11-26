import os
import subprocess
from pathlib import PurePosixPath

from bridge.guard import HostCheckout, Target

EMPTY_BLOB = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
EMPTY_ALLOWED = frozenset({".gitkeep", "__init__.py"})
GIT_TIMEOUT_S = 5


def find_new_empty_files(targets: list[Target]) -> list[str]:
    """Paths the pushed revision holds as zero bytes that no remote commit it builds on has, moves apart: a stray
    empty file is what an unquoted heredoc or a mistyped redirection leaves."""
    found: list[str] = []
    for target in targets:
        if target.action != "git push" or target.pushed is None:
            continue
        directory, revision = target.pushed
        listed = _run_git(directory, "rev-list", "--boundary", revision, "--not", "--remotes")
        if not listed:
            continue
        # Each remote commit the pushed ones build on; none when no remote has any of its history.
        bases = [line[1:] for line in listed.splitlines() if line.startswith("-")] or [EMPTY_TREE]
        diffs: list[str] = []
        for base in bases:
            diff = _run_git(
                directory, "diff", "--raw", "-z", "--no-abbrev", "--no-renames", "--diff-filter=AD", base, revision
            )
            if diff is None:
                diffs = []
                break
            diffs.append(diff)
        new = set(map_raw_diffs_to_new_empty_files(diffs))
        found.extend(path for path in sorted(new) if path not in found)
    return found


def map_raw_diffs_to_new_empty_files(diffs: list[str]) -> list[str]:
    """The empty files every one of the diffs adds: one diff per remote commit the push builds on."""
    if not diffs:
        return []
    common = set(map_raw_diff_to_new_empty_files(diffs[0]))
    for diff in diffs[1:]:
        common &= set(map_raw_diff_to_new_empty_files(diff))
    return sorted(common)


def find_host_new_empty_files(targets: list[Target], checkouts: tuple[HostCheckout, ...]) -> list[str]:
    """The same for a host push, from the diffs gbridge sent for each checkout's current branch; a push of another
    branch, or from a gbridge that sends none, is not checked."""
    found: list[str] = []
    for target in targets:
        if target.action != "git push" or target.pushed is None:
            continue
        directory, revision = target.pushed
        for checkout in checkouts:
            if (
                os.path.normpath(checkout.directory) == os.path.normpath(directory)
                and checkout.facts.branch == revision
            ):
                found.extend(p for p in map_raw_diffs_to_new_empty_files(list(checkout.empty_diffs)) if p not in found)
    return found


def map_raw_diff_to_new_empty_files(raw: str) -> list[str]:
    """Empty files `git diff --raw -z --no-renames` adds, less those an empty file it deletes was moved to. Git pairs
    identical empty blobs at random, so a move is matched here: by name first, then any left over excuses one more."""
    fields = raw.split("\0")
    added: list[str] = []
    deleted: list[str] = []
    for meta, path in zip(fields[::2], fields[1::2], strict=False):
        parts = meta.split()
        if len(parts) < 5:
            continue
        if parts[4] == "A" and parts[3] == EMPTY_BLOB:
            added.append(path)
        elif parts[4] == "D" and parts[2] == EMPTY_BLOB:
            deleted.append(path)
    names = [PurePosixPath(path).name for path in deleted]
    moved: list[str] = []
    for path in added:
        if (name := PurePosixPath(path).name) in names:
            names.remove(name)
            moved.append(path)
    # An allowed name a file moved to takes up that move, so it excuses no stray beside it.
    left = sorted((p for p in added if p not in moved), key=lambda p: (PurePosixPath(p).name not in EMPTY_ALLOWED, p))
    return [path for path in left[len(names) :] if PurePosixPath(path).name not in EMPTY_ALLOWED]


def map_empty_files_to_denial(paths: list[str]) -> str:
    named = ", ".join(paths[:5]) + (f" and {len(paths) - 5} more" if len(paths) > 5 else "")
    return (
        f"bridge: this push adds empty files: {named}. A zero-byte file is most often a stray (an unquoted heredoc, a "
        "redirection typo) that git add -A or git add . took in. Remove it with git rm and a new commit, or give it "
        "content; stage files by path from now on."
    )


def _run_git(directory: str, *args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", directory, *args], capture_output=True, text=True, timeout=GIT_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None
