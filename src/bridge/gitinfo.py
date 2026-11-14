import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

_GITHUB_RE = re.compile(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
_SECTION_RE = re.compile(r'^\[\s*([A-Za-z0-9.-]+)\s*(?:"((?:[^"\\]|\\.)*)")?\s*\]')
_KEY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9-]*)\s*(?:=\s*(.*))?$")
SYSTEM_CONFIG = "/etc/gitconfig"


@dataclass(frozen=True)
class GitInfo:
    repo_root: str
    branch: str | None
    repo: str | None
    git_dir: str = ""


def read_git_info(cwd: str) -> GitInfo | None:
    """Reads git state from files only; hooks cannot afford spawning git."""
    found = _find_git_dir(Path(cwd))
    if found is None:
        return None
    root, git_dir = found
    return GitInfo(
        repo_root=str(root), branch=_read_branch(git_dir), repo=_read_github_repo(git_dir), git_dir=str(git_dir)
    )


@dataclass(frozen=True)
class RepoFacts:
    """What the push and merge guard reads of a checkout, on this Mac or reported by a host worker."""

    repo_root: str
    branch: str | None
    remotes: Mapping[str, str | None] = field(default_factory=dict[str, str | None])
    """Remote name to its GitHub owner/repo; None for a remote not on GitHub."""
    tracking: Mapping[str, str] = field(default_factory=dict[str, str])
    """Branch to the remote it tracks."""
    push_default: str | None = None
    custom_push: bool = False
    """pushDefault, pushRemote, push refspecs or a branch's remote set outside the repo: where a push naming no remote
    or no refspec goes is not obvious."""
    custom_remote: bool = False
    """pushurl, insteadOf, pushInsteadOf, an include, a remote with several urls or one set outside the repo: even
    `git push <remote> <branch>` may go somewhere other than the remote's url."""

    @property
    def repo(self) -> str | None:
        return self.remotes.get("origin")


def read_repo_facts(cwd: str) -> RepoFacts | None:
    found = _find_git_dir(Path(cwd))
    if found is None:
        return None
    root, git_dir = found
    local = (_read_text(_common_dir(git_dir) / "config"), _read_text(git_dir / "config.worktree"))
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "git" / "config"
    outer = (_read_text(Path(SYSTEM_CONFIG)), _read_text(xdg), _read_text(Path.home() / ".gitconfig"))
    return map_git_config_to_facts(str(root), _read_branch(git_dir), local, outer)


def map_git_config_to_facts(
    repo_root: str, branch: str | None, local: Sequence[str], outer: Sequence[str]
) -> RepoFacts:
    """local: the repo's config files; outer: system, XDG and user config, in git's reading order.

    gbridge's read_config_facts parses the same way on a host; the two must agree.
    """
    remotes: dict[str, str | None] = {}
    tracking: dict[str, str] = {}
    push_default: str | None = None
    custom_push = custom_remote = False
    for is_local, text in [*((False, t) for t in outer), *((True, t) for t in local)]:
        for name, sub, key, value in scan_git_config(text):
            if name in ("include", "includeif") or (name == "url" and key in ("insteadof", "pushinsteadof")):
                custom_remote = True
            elif name == "remote" and sub is not None and key == "url":
                if not is_local or sub in remotes:
                    custom_remote = True
                else:
                    match = _GITHUB_RE.search(value)
                    remotes[sub] = f"{match.group(1)}/{match.group(2)}" if match else None
            elif name == "remote" and key == "pushurl":
                custom_remote = True
            elif (name, key) in (("remote", "push"), ("remote", "pushdefault"), ("branch", "pushremote")):
                custom_push = True
            elif name == "branch" and sub is not None and key == "remote":
                if is_local:
                    tracking[sub] = value
                else:
                    custom_push = True
            elif name == "push" and sub is None and key == "default":
                push_default = value.lower()
    return RepoFacts(repo_root, branch, remotes, tracking, push_default, custom_push, custom_remote)


def scan_git_config(text: str) -> list[tuple[str, str | None, str, str]]:
    """(section, subsection, key, value) of each entry; a key with no value is a true boolean."""
    entries: list[tuple[str, str | None, str, str]] = []
    section: tuple[str, str | None] = ("", None)
    for line in text.splitlines():
        rest = line.strip()
        if match := _SECTION_RE.match(rest):
            name, sub = match.group(1).lower(), match.group(2)
            if sub is None and "." in name:
                name, _, sub = name.partition(".")
            section = (name, sub)
            # git reads an entry after the header on the same line.
            rest = rest[match.end() :].strip()
        if match := _KEY_RE.match(rest):
            value = re.split(r"\s[#;]", match.group(2) or "")[0].strip().strip('"')
            entries.append((section[0], section[1], match.group(1).lower(), value))
    return entries


def _common_dir(git_dir: Path) -> Path:
    commondir_file = git_dir / "commondir"
    return (git_dir / commondir_file.read_text().strip()).resolve() if commondir_file.is_file() else git_dir


def _read_text(path: Path) -> str:
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return ""


def _find_git_dir(start: Path) -> tuple[Path, Path] | None:
    for directory in (start, *start.parents):
        dot_git = directory / ".git"
        if dot_git.is_dir():
            return directory, dot_git
        if dot_git.is_file():
            content = dot_git.read_text().strip()
            if content.startswith("gitdir:"):
                git_dir = Path(content.removeprefix("gitdir:").strip())
                return directory, git_dir if git_dir.is_absolute() else (directory / git_dir).resolve()
    return None


def _read_branch(git_dir: Path) -> str | None:
    try:
        head = (git_dir / "HEAD").read_text().strip()
    except OSError:
        return None
    if head.startswith("ref: refs/heads/"):
        return head.removeprefix("ref: refs/heads/")
    return head[:12] or None


def _read_github_repo(git_dir: Path, remote: str = "origin") -> str | None:
    common = _common_dir(git_dir)
    try:
        lines = (common / "config").read_text().splitlines()
    except OSError:
        return None
    in_origin = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            in_origin = stripped.replace(" ", "") == f'[remote"{remote}"]'
            continue
        if in_origin and stripped.startswith("url"):
            match = _GITHUB_RE.search(stripped.partition("=")[2].strip())
            return f"{match.group(1)}/{match.group(2)}" if match else None
    return None
