import re
from dataclasses import dataclass
from pathlib import Path

_GITHUB_RE = re.compile(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


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


def read_remote_repo(git: GitInfo, remote: str) -> str | None:
    """owner/repo of a named remote; None when the remote is unknown or not on GitHub."""
    return _read_github_repo(Path(git.git_dir), remote) if git.git_dir else None


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
    common = git_dir
    commondir_file = git_dir / "commondir"
    if commondir_file.is_file():
        common = (git_dir / commondir_file.read_text().strip()).resolve()
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
