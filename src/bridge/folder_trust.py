"""Whether Claude Code would stop at "Is this a project you trust?" in a folder, and marking one trusted.

Read from Claude Code 2.1's source: a folder counts as trusted when ~/.claude.json has
projects[<path>].hasTrustDialogAccepted for the folder's real path, a folder above it up to its git checkout's root
(outside git, any folder above it), or that checkout's main checkout, where Claude Code records an accepted dialog.
"""

import json
import os
import unicodedata
from pathlib import Path
from typing import Any, cast

from bridge.errors import UsageError
from bridge.host_side import claude_json_path, set_worktree_trust


def find_trusting_path(cwd: Path) -> str | None:
    """The path whose trust record covers cwd; None when there is none. Raises OSError or ValueError when
    ~/.claude.json cannot be read."""
    with open(claude_json_path(), encoding="utf-8") as f:
        data: Any = json.load(f)
    projects: Any = cast(dict[str, Any], data).get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        return None
    entries = cast(dict[str, Any], projects)
    for key in list_trust_keys(cwd):
        entry: Any = entries.get(key)
        if isinstance(entry, dict) and cast(dict[str, Any], entry).get("hasTrustDialogAccepted") is True:
            return key
    return None


def list_trust_keys(cwd: Path) -> list[str]:
    keys: list[str] = []
    path = map_cwd_to_claude_path(cwd)
    while True:
        keys.append(path)
        if os.path.exists(os.path.join(path, ".git")):
            keys.append(map_checkout_to_main_checkout(path))
            break
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return list(dict.fromkeys(keys))


def map_cwd_to_claude_path(cwd: Path) -> str:
    """Claude Code reads its start folder as process.cwd() made real and NFC, so a symlink's own path counts for
    nothing."""
    return unicodedata.normalize("NFC", os.path.realpath(cwd))


def map_checkout_to_main_checkout(root: str) -> str:
    """A linked worktree's .git file names its git dir, whose commondir is the main checkout's .git."""
    try:
        text = Path(root, ".git").read_text(encoding="utf-8").strip()
        if not text.startswith("gitdir:"):
            return root
        git_dir = os.path.join(root, text.removeprefix("gitdir:").strip())
        common = os.path.normpath(os.path.join(git_dir, Path(git_dir, "commondir").read_text(encoding="utf-8").strip()))
    except (OSError, ValueError):
        return root
    return os.path.dirname(common) if os.path.basename(common) == ".git" else common


def ensure_worker_folder_trusted(cwd: Path, roots: tuple[str, ...]) -> str:
    """Raises UsageError saying why a worker in cwd would wait at Claude Code's trust dialog. Under one of roots, the
    owner's own folders, cwd is first marked trusted as accepting the dialog would; returns what was marked, if any."""
    try:
        if find_trusting_path(cwd) is not None:
            return ""
    except (OSError, ValueError) as exc:
        raise UsageError(f"could not read {claude_json_path()} to see whether Claude Code trusts {cwd}: {exc}") from exc
    untrusted = f"Claude Code has not trusted {cwd} yet, so a worker there would wait at its trust dialog"
    real = Path(map_cwd_to_claude_path(cwd))
    if real == Path.home().resolve():
        # Claude Code never records the home folder's trust: its dialog's answer holds for one session.
        raise UsageError(f"{untrusted} at every start; start the worker in a project folder")
    root = next((r for r in roots if real.is_relative_to(Path(r).expanduser().resolve())), None)
    if root is None:
        raise UsageError(
            f"{untrusted}: run claude in it once and answer 'Yes, I trust this folder', or add a folder above it to "
            "worker_trust_roots in the bridge config"
        )
    if why := set_worktree_trust(str(real), True):
        raise UsageError(f"{untrusted}, and marking it trusted failed: {why}")
    return f"marked {real} trusted for Claude Code: it is under {root} in worker_trust_roots"
