import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from bridge.config import Config, state_dir
from bridge.db import transaction
from bridge.doc_nav import convert_doc_to_viewable
from bridge.review_marks import convert_reviews_to_text, map_html_to_reviews

FRESH_FOR_PREPARING_MS = 14 * 24 * 3_600_000
FAILED_RETRY_MS = 3_600_000
GIT_TIMEOUT_S = 600
ENV_TIMEOUT_S = 1800
DOC_MAX_BYTES = 20_000_000

READY = "ready"
PREPARING = "preparing"
DIRTY = "dirty"
WT_FAILED = "failed"
NO_CHECKOUT = "no-checkout"
DETACHED = "detached"
"""Made before worktrees stood on a branch; the next refresh puts it on one."""

_LOOKED_AT_HOURLY = frozenset({DIRTY, WT_FAILED, NO_CHECKOUT})
_REBUILT_DIRS = frozenset({".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", "node_modules"})
_SETTINGS_FILE = ".vscode/settings.json"
_SETTINGS_STAMP = "review-settings.sha256"
_REQUIREMENTS_STAMP = "review-requirements.sha256"
_SETUP_STAMP = "review-setup.sha256"
_SETUP_INPUTS = frozenset({
    "setup.py", "setup.cfg", "pyproject.toml", ".python-version", "uv.lock", "poetry.lock", "pdm.lock", "Pipfile",
    "Pipfile.lock",
})  # fmt: skip
_PYTHON_PIN_RE = re.compile(r"3\.\d+(?:\.\d+)?")
_VENV_VERSION_RE = re.compile(r"^version(?:_info)?\s*=\s*(\S+)", re.M)
_NOT_SOURCE_DIRS = frozenset({".git", ".venv", "node_modules", "__pycache__"})
_OURS_DIRS = (".venv/", ".vscode/")
_SAFE_NAME_RE = re.compile(r"[\w.-]+")
_VERSION_PART_RE = re.compile(r"\s*[<>=!~].*$")
_REQUIREMENT_NAME_RE = re.compile(r"[A-Za-z0-9][\w.-]*")
_META_RE = re.compile(rb"<meta\b[^>]*>", re.I)
_PR_HEAD_NAME_RE = re.compile(rb"""\bname\s*=\s*["']?pr-head\b""", re.I)
_CONTENT_SHA_RE = re.compile(rb"""\bcontent\s*=\s*["']?([0-9a-fA-F]{7,40})\b""")
_BODY_TAG_RE = re.compile(rb"<body\b[^>]*>", re.I)
_SSH_AUTH_FAILED_RE = re.compile(r"Permission denied \(publickey|sign_and_send_pubkey")
_SSH_REMOTE_RE = re.compile(r"(?:ssh://git@([\w.-]+)(?::\d+)?/|git@([\w.-]+):/*)(.+?)(?:\.git)?/?")
SSH_KEY_NOT_LOADED = "GitHub SSH key not loaded: run ssh-add"
_WORKSPACE_SUFFIX = ".code-workspace"
DOC_SHA_CHARS = 9


class DocError(Exception):
    pass


class StepFailed(Exception):
    def __init__(self, message: str, stderr: str = "") -> None:
        super().__init__(message)
        self.stderr = stderr


@dataclass(frozen=True)
class WorktreeJob:
    ref: str
    repo: str
    number: int
    head_oid: str
    head: str
    """The PR's branch on origin, which the worktree's own branch tracks."""
    action: str
    """create, refresh or remove."""
    path: Path
    recorded_oid: str | None = None
    """Where bridge last left the worktree; a HEAD elsewhere holds commits made by hand."""
    setup_script: str = ""
    """The repo's own env setup, relative to its root; empty for bridge's generic env."""

    @property
    def branch(self) -> str:
        """Its own, as the PR's branch is usually checked out in its author's worktree, and git allows one."""
        return map_number_to_branch(self.number)

    @property
    def upstream(self) -> str:
        return f"origin/{self.head}"


def map_number_to_branch(number: int) -> str:
    return f"review/{number}"


def is_worth_preparing(row: sqlite3.Row, config: Config, now: int) -> bool:
    """Old stacks must not eat the disk, and what they have finished with needs no worktree."""
    if row["draft"] or row["closed_at"] is not None or int(row["loc"]) < config.review_large_loc:
        return False
    if row["reviewed_at"] or row["owner_reviewed_at"] or row["post_state"] == "external":
        return False
    return now - _map_iso_to_ms(str(row["updated_at"])) <= FRESH_FOR_PREPARING_MS


def _map_iso_to_ms(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def map_ref_to_worktree_path(config: Config, repo: str, number: int) -> Path | None:
    name = repo.rpartition("/")[2]
    if not config.review_worktrees_dir or not _SAFE_NAME_RE.fullmatch(name):
        return None
    return Path(config.review_worktrees_dir).expanduser() / f"review-{name}-{number}"


def map_worktree_to_workspace(path: Path) -> Path:
    """Beside the worktree, not in it, where an untracked file would read as their change."""
    return path.parent / f"{path.name}{_WORKSPACE_SUFFIX}"


def map_repo_to_checkout(config: Config, repo: str) -> Path | None:
    name = repo.rpartition("/")[2]
    if not config.review_checkouts_dir or not _SAFE_NAME_RE.fullmatch(name):
        return None
    checkout = Path(config.review_checkouts_dir).expanduser() / name
    return checkout if (checkout / ".git").exists() else None


def map_repo_to_setup_script(config: Config, repo: str) -> str:
    return next((script for name, script in config.review_setup_scripts if name.lower() == repo.lower()), "")


def pick_worktree_job(conn: sqlite3.Connection, config: Config, now: int) -> WorktreeJob | None:
    """One job at a time: removals first, then new worktrees, then refreshes."""
    rows = conn.execute("SELECT * FROM review_prs ORDER BY updated_at DESC").fetchall()

    def cooling(row: sqlite3.Row) -> bool:
        # A checkout cloned since is only a directory lookup away, so it ends the wait.
        if row["wt_state"] == NO_CHECKOUT and map_repo_to_checkout(config, str(row["repo"])) is not None:
            return False
        return row["wt_state"] in _LOOKED_AT_HOURLY and now - int(row["wt_at"] or 0) < FAILED_RETRY_MS

    for row in rows:
        if row["closed_at"] is not None and row["wt_path"] and not cooling(row):
            return _map_row_to_job(row, "remove", Path(str(row["wt_path"])), config)
    for row in rows:
        if row["wt_path"] or cooling(row) or not is_worth_preparing(row, config, now):
            continue
        path = map_ref_to_worktree_path(config, str(row["repo"]), int(row["number"]))
        if path is not None:
            return _map_row_to_job(row, "create", path, config)
    for row in rows:
        if row["closed_at"] is not None or not row["wt_path"] or cooling(row):
            continue
        # Also with the head unchanged: a failed install or a cleaned-up worktree has to settle.
        stale = row["wt_oid"] != row["head_oid"] or row["wt_state"] in (*_LOOKED_AT_HOURLY, DETACHED)
        if stale or _has_outdated_env(row, config):
            return _map_row_to_job(row, "refresh", Path(str(row["wt_path"])), config)
    return None


def _has_outdated_env(row: sqlite3.Row, config: Config) -> bool:
    """Prepared before its repo's setup script was configured, or with another Python than the repo pins."""
    if row["wt_state"] != READY:
        return False
    path = Path(str(row["wt_path"]))
    venv = path / ".venv"
    script = map_repo_to_setup_script(config, str(row["repo"]))
    if script and (path / script).is_file():
        return not (venv / _SETUP_STAMP).exists()
    pinned = read_pinned_python(path)
    return bool(pinned) and (venv / _REQUIREMENTS_STAMP).exists() and not _venv_runs(venv, pinned)


def _map_row_to_job(row: sqlite3.Row, action: str, path: Path, config: Config) -> WorktreeJob:
    return WorktreeJob(
        str(row["ref"]),
        str(row["repo"]),
        int(row["number"]),
        str(row["head_oid"]),
        str(row["head"]),
        action,
        path,
        row["wt_oid"],
        map_repo_to_setup_script(config, str(row["repo"])),
    )


def record_worktree(
    conn: sqlite3.Connection, ref: str, path: Path | None, oid: str | None, state: str, detail: str | None, now: int
) -> None:
    conn.execute(
        "UPDATE review_prs SET wt_path = ?, wt_oid = ?, wt_state = ?, wt_detail = ?, wt_at = ? WHERE ref = ?",
        (str(path) if path else None, oid, state, detail, now, ref),
    )


def run_worktree_job(conn: sqlite3.Connection, config: Config, job: WorktreeJob, now: int) -> None:
    """Git and the installer run with no transaction open; the row is written when they are done."""
    row = conn.execute("SELECT wt_path, wt_oid FROM review_prs WHERE ref = ?", (job.ref,)).fetchone()
    kept_path = Path(str(row["wt_path"])) if row["wt_path"] else None
    try:
        if job.action == "remove":
            result = _remove_worktree(job, map_repo_to_checkout(config, job.repo))
        elif job.action == "refresh":
            result = _refresh_worktree(job, map_repo_to_checkout(config, job.repo))
        else:
            result = _create_worktree(job, map_repo_to_checkout(config, job.repo))
    except (StepFailed, OSError) as exc:
        result = (kept_path, row["wt_oid"], WT_FAILED, str(exc)[:300])
    if job.action == "remove" and result[0] is None:
        map_worktree_to_workspace(job.path).unlink(missing_ok=True)
    elif result[0] is not None:
        _link_worktree_to_pr(job.repo, job.number, result[0])
    with transaction(conn):
        record_worktree(conn, job.ref, result[0], result[1], result[2], result[3], now)


def link_worktrees_to_prs(conn: sqlite3.Connection) -> None:
    """Worktrees made before they had a workspace, or before its current settings, get it here, without waiting for
    their head to move."""
    for row in conn.execute("SELECT repo, number, wt_path FROM review_prs WHERE wt_path IS NOT NULL"):
        path = Path(str(row["wt_path"]))
        if not map_worktree_to_workspace(path).exists():
            _link_worktree_to_pr(str(row["repo"]), int(row["number"]), path)
        elif (path / ".git").exists():
            try:
                write_workspace(path)
            except OSError as exc:
                print(f"review workspace for {row['repo']}#{row['number']} failed: {exc}", flush=True)


def _link_worktree_to_pr(repo: str, number: int, path: Path) -> None:
    """The workspace the Review link opens, and the branch key the GitHub Pull Requests extension reads to show this
    PR there. Neither stops the worktree from being used, so a failure is only printed."""
    if not (path / ".git").exists():
        return
    owner, _, name = repo.partition("/")
    try:
        key = f"branch.{map_number_to_branch(number)}.github-pr-owner-number"
        _run(["git", "-C", str(path), "config", "--local", key, f"{owner}#{name}#{number}"], GIT_TIMEOUT_S)
        write_workspace(path)
    except (StepFailed, OSError) as exc:
        print(f"review workspace for {repo}#{number} failed: {exc}", flush=True)


def write_workspace(path: Path) -> None:
    """Bridge's settings go in where missing; their folders, settings and anything else they added stay. A file that is
    not plain JSON is left alone, as rewriting it would drop their comments."""
    ours: dict[str, object] = {
        "workbench.secondarySideBar.defaultVisibility": "hidden",
        "githubPullRequests.focusedMode": "overview",
    }
    python = path / ".venv" / "bin" / "python"
    if python.exists():
        ours["python.defaultInterpreterPath"] = str(python)
        # The Python Environments extension resolves the global scope after the folder's, and the Python extension's
        # interpreter check in between finds none and asks for one; the stable path reads the setting directly.
        ours["python.useEnvironmentsExtension"] = False
    workspace = map_worktree_to_workspace(path)
    try:
        current: object = json.loads(workspace.read_text())
    except FileNotFoundError:
        current = {}
    except (OSError, ValueError):
        return
    if not isinstance(current, dict):
        return
    current = cast(dict[str, object], current)
    settings = current.get("settings")
    theirs = cast(dict[str, object], settings) if isinstance(settings, dict) else {}
    merged = {
        "folders": current.get("folders") or [{"path": str(path)}],
        **{key: value for key, value in current.items() if key not in ("folders", "settings")},
        "settings": {**ours, **theirs},
    }
    if merged == current:
        return
    temporary = workspace.with_suffix(".tmp")
    temporary.write_text(json.dumps(merged, indent=2) + "\n")
    temporary.replace(workspace)


Result = tuple[Path | None, str | None, str, str | None]


def _create_worktree(job: WorktreeJob, checkout: Path | None) -> Result:
    if checkout is None:
        return None, None, NO_CHECKOUT, f"no main checkout of {job.repo} on this machine"
    if (job.path / ".git").exists():
        # Left by a run that stopped before recording it; it may hold work by now.
        return _refresh_worktree(job, checkout)
    if _branch_holds_owner_commits(checkout, job):
        return None, None, DIRTY, f"local commits on {job.branch}, no worktree made"
    _fetch_head_branch(checkout, job)
    job.path.parent.mkdir(parents=True, exist_ok=True)
    _run(
        ["git", "-C", str(checkout), "worktree", "add", "-q", "-B", job.branch, str(job.path), job.upstream],
        GIT_TIMEOUT_S,
    )
    _run(["git", "-C", str(job.path), "branch", "-q", f"--set-upstream-to={job.upstream}"], GIT_TIMEOUT_S)
    return _finish_with_env(job)


def _refresh_worktree(job: WorktreeJob, checkout: Path | None) -> Result:
    """Fetch, then fast-forward its branch, so a plain git pull there works too. After a force-push only a worktree
    with nothing of theirs is reset to the new head."""
    problem = _find_owner_work_in_the_way(job, fetched=False)
    if problem:
        return job.path, job.recorded_oid, DIRTY, problem
    if checkout is None:
        return job.path, job.recorded_oid, NO_CHECKOUT, f"no main checkout of {job.repo} on this machine"
    _fetch_head_branch(checkout, job)
    # A fetch can take minutes, and they may have committed, edited or switched branch during it.
    problem = _find_owner_work_in_the_way(job, fetched=True)
    if problem:
        return job.path, job.recorded_oid, DIRTY, problem
    if _read_branch(job.path) is None:
        _run(["git", "-C", str(job.path), "checkout", "-q", "-B", job.branch], GIT_TIMEOUT_S)
    _run(["git", "-C", str(job.path), "branch", "-q", f"--set-upstream-to={job.upstream}"], GIT_TIMEOUT_S)
    if _is_ancestor(job.path, "HEAD", job.upstream):
        _run(["git", "-C", str(job.path), "merge", "-q", "--ff-only", job.upstream], GIT_TIMEOUT_S)
    else:
        _run(["git", "-C", str(job.path), "reset", "-q", "--hard", job.upstream], GIT_TIMEOUT_S)
    return _finish_with_env(job)


def _find_owner_work_in_the_way(job: WorktreeJob, fetched: bool) -> str | None:
    """What a refresh could lose; None when nothing. Once fetched, also a file of theirs that the new head would write
    over: env, editor and ignored files pass the status check but must not be clobbered."""
    if _has_local_commits(job):
        return "local commits, not refreshed"
    branch = _read_branch(job.path)
    if branch not in (None, job.branch):
        return f"on another branch, {branch}, not refreshed"
    if branch is None and _branch_holds_owner_commits(job.path, job):
        return f"local commits on {job.branch}, not refreshed"
    changes = _find_changes_in_the_way(job.path)
    if changes:
        return f"not refreshed, it has {changes}"
    if fetched:
        added = _run(
            ["git", "-C", str(job.path), "diff", "--name-only", "-z", "--no-renames", "--diff-filter=A", "HEAD",
             job.upstream],
            GIT_TIMEOUT_S,
        ).split("\0")  # fmt: skip
        clobbered = [name for name in added if name and os.path.lexists(job.path / name)]
        if clobbered:
            return f"not refreshed, the new head would overwrite local {', '.join(clobbered[:5])}"
    return None


def _branch_holds_owner_commits(repo: Path, job: WorktreeJob) -> bool:
    """Its own branch, kept when the worktree went detached or was removed, with a tip outside the PR."""
    try:
        tip = _run(
            ["git", "-C", str(repo), "rev-parse", "-q", "--verify", f"refs/heads/{job.branch}^{{commit}}"],
            GIT_TIMEOUT_S,
        ).strip()
    except StepFailed:
        return False
    if tip in (job.recorded_oid, job.head_oid, _read_head(job.path) if (job.path / ".git").exists() else None):
        return False
    return not _is_ancestor(repo, tip, job.head_oid)


def _finish_with_env(job: WorktreeJob) -> Result:
    """The worktree stands at the head by now, so a failed install must not lose track of it. Origin may be ahead of
    the last poll; what is recorded is where it stands."""
    oid = _read_head(job.path)
    try:
        prepare_python_env(job.path, job.setup_script)
    except (StepFailed, OSError) as exc:
        return job.path, oid, WT_FAILED, f"Python env: {str(exc)[:280]}"
    return job.path, oid, READY, None


def _fetch_head_branch(checkout: Path, job: WorktreeJob) -> None:
    """Into remote-tracking refs, not FETCH_HEAD: the main checkout is shared, and a session may be about to merge what
    it just fetched. The PR's own ref comes too, so a branch of the same name that is not the PR's, as with a fork's
    PR, is never checked out as if it were."""
    pull_ref = f"refs/remotes/origin/pr/{job.number}"
    refspecs = [f"+refs/heads/{job.head}:refs/remotes/origin/{job.head}", f"+refs/pull/{job.number}/head:{pull_ref}"]
    try:
        _run(["git", "-C", str(checkout), "fetch", "-q", "--no-write-fetch-head", "origin", *refspecs], GIT_TIMEOUT_S)
    except StepFailed as exc:
        if not _SSH_AUTH_FAILED_RE.search(exc.stderr):
            raise
        _fetch_over_https(checkout, refspecs)
    tips = _run(["git", "-C", str(checkout), "rev-parse", job.upstream, pull_ref], GIT_TIMEOUT_S).split()
    if len(set(tips)) != 1:
        raise StepFailed(f"{job.upstream} is not this PR's head; a fork's PR cannot stand on that branch")


def _fetch_over_https(checkout: Path, refspecs: list[str]) -> None:
    """When the SSH agent holds no key, once more with gh's token. Only for this fetch: the remote's URL and their git
    config stay as they are, and other credential helpers are off so none can show a dialog."""
    remote = _run(["git", "-C", str(checkout), "remote", "get-url", "origin"], GIT_TIMEOUT_S).strip()
    url = map_ssh_remote_to_https(remote)
    if url is None:
        raise StepFailed(SSH_KEY_NOT_LOADED)
    command = [
        "git", "-C", str(checkout), "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
        "fetch", "-q", "--no-write-fetch-head", url, *refspecs,
    ]  # fmt: skip
    try:
        _run(command, GIT_TIMEOUT_S, {"GIT_TERMINAL_PROMPT": "0"})
    except StepFailed as exc:
        raise StepFailed(SSH_KEY_NOT_LOADED) from exc


def map_ssh_remote_to_https(remote: str) -> str | None:
    match = _SSH_REMOTE_RE.fullmatch(remote)
    return f"https://{match.group(1) or match.group(2)}/{match.group(3)}.git" if match else None


def _read_branch(path: Path) -> str | None:
    try:
        return _run(["git", "-C", str(path), "symbolic-ref", "-q", "--short", "HEAD"], GIT_TIMEOUT_S).strip() or None
    except StepFailed:
        return None


def _is_ancestor(path: Path, older: str, newer: str) -> bool:
    try:
        _run(["git", "-C", str(path), "merge-base", "--is-ancestor", older, newer], GIT_TIMEOUT_S)
    except StepFailed:
        return False
    return True


def _remove_worktree(job: WorktreeJob, checkout: Path | None) -> Result:
    """Only through git, and only what holds nothing of theirs; its branch goes with it on the same terms."""
    if not job.path.exists():
        if checkout is not None and not _branch_holds_owner_commits(checkout, job):
            with contextlib.suppress(StepFailed):
                # A folder deleted by hand stays registered, and a registered worktree keeps its branch.
                _run(["git", "-C", str(checkout), "worktree", "prune"], GIT_TIMEOUT_S)
                _run(["git", "-C", str(checkout), "branch", "-q", "-D", job.branch], GIT_TIMEOUT_S)
        return None, None, "removed", None
    problem = _find_local_work(job)
    if problem:
        return job.path, job.recorded_oid, DIRTY, f"PR closed; kept because it has {problem}"
    if checkout is None:
        return job.path, job.recorded_oid, NO_CHECKOUT, f"no main checkout of {job.repo} on this machine"
    keep_branch = _branch_holds_owner_commits(checkout, job)
    _run(["git", "-C", str(checkout), "worktree", "remove", "--force", str(job.path)], GIT_TIMEOUT_S)
    if keep_branch:
        return None, None, "removed", f"kept the branch {job.branch}: it has local commits"
    with contextlib.suppress(StepFailed):
        _run(["git", "-C", str(checkout), "branch", "-q", "-D", job.branch], GIT_TIMEOUT_S)
    return None, None, "removed", None


def _find_local_work(job: WorktreeJob) -> str | None:
    """What removing or moving this worktree would lose; None when nothing."""
    if _has_local_commits(job):
        return "local commits"
    dirty = _list_dirty_files(job.path)
    return f"local changes: {', '.join(dirty[:5])}" if dirty else None


def _has_local_commits(job: WorktreeJob) -> bool:
    """A HEAD neither where bridge left it nor part of the PR. A force-push can make this a false yes, which only
    keeps a worktree around."""
    head = _read_head(job.path)
    if head in (job.recorded_oid, job.head_oid):
        return False
    return not _is_ancestor(job.path, head, job.head_oid)


def _find_changes_in_the_way(path: Path) -> str | None:
    """What a fast-forward could clash with or a reset lose. Ignored files and the env and editor folders bridge made
    stay where they are, so only their tracked edits and their other new files hold a refresh back."""
    dirty = [
        name for code, name in _read_status(path) if code != "!!" and not (code == "??" and name.startswith(_OURS_DIRS))
    ]
    return f"local changes: {', '.join(dirty[:5])}" if dirty else None


def _list_dirty_files(path: Path) -> list[str]:
    # An ignored file can be theirs too (an ignored .vscode/launch.json), and removing the worktree deletes it, so of
    # those only what tools rebuild is left out; a tracked one never.
    dirty: list[str] = []
    for code, name in _read_status(path):
        if name.startswith(".venv/") or (code == "!!" and is_rebuilt_by_tools(name)):
            continue
        if not (name == _SETTINGS_FILE and _is_our_settings(path)):
            dirty.append(name)
    return dirty


def _read_status(path: Path) -> list[tuple[str, str]]:
    """(code, path) per file. The user's git config must not hide a file, nor a folder hide its files; -z keeps a name
    with a space unquoted."""
    command = ["git", "-C", str(path), "status", "--porcelain", "-z", "--untracked-files=all", "--ignored"]
    entries: list[tuple[str, str]] = []
    fields = iter(_run(command, GIT_TIMEOUT_S).split("\0"))
    for field in fields:
        if not field:
            continue
        code = field[:2]
        entries.append((code, field[3:]))
        if code[0] in "RC":
            next(fields, None)
    return entries


def is_rebuilt_by_tools(name: str) -> bool:
    parts = Path(name).parts
    return (
        bool(_REBUILT_DIRS.intersection(parts)) or name.endswith(".pyc") or any(p.endswith(".egg-info") for p in parts)
    )


def _is_our_settings(root: Path) -> bool:
    try:
        written = (root / ".venv" / _SETTINGS_STAMP).read_text().strip()
        return written == hashlib.sha256((root / _SETTINGS_FILE).read_bytes()).hexdigest()
    except OSError:
        return False


def _read_head(path: Path) -> str:
    return _run(["git", "-C", str(path), "rev-parse", "HEAD"], GIT_TIMEOUT_S).strip()


def _run(command: list[str], timeout_s: int, env: dict[str, str] | None = None, cwd: Path | None = None) -> str:
    """In its own process group, so a timeout also stops what it started and the next job never runs beside it."""
    try:
        process = subprocess.Popen(
            ["nice", "-n", "10", *command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, **env} if env else None,
            cwd=cwd,
            start_new_session=True,
        )
    except OSError as exc:
        raise StepFailed(f"{command[0]} failed: {type(exc).__name__}") from exc
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        with contextlib.suppress(OSError):
            os.killpg(process.pid, signal.SIGKILL)
        process.kill()
        process.wait()
        raise StepFailed(f"{command[0]} failed: TimeoutExpired") from exc
    if process.returncode != 0:
        tail = (stderr.strip() or stdout.strip() or "no output").splitlines()[-1]
        raise StepFailed(f"{command[0]} {command[3] if len(command) > 3 else ''} failed: {tail}", stderr)
    return stdout


def find_requirement_files(root: Path) -> list[Path]:
    """Services nest them at any depth, so every level is read, as one env for the whole repo needs."""
    found: list[Path] = []
    for directory, names, files in os.walk(root):
        names[:] = [n for n in names if n not in _NOT_SOURCE_DIRS]
        if "requirements.txt" in files:
            found.append(Path(directory) / "requirements.txt")
    return sorted(found)


def map_requirements_to_consolidated(files: list[Path]) -> list[str]:
    """One line per package, the first pin winning. Local paths and nested includes are left out: extraPaths covers
    the in-repo packages."""
    chosen: dict[str, str] = {}
    for path in files:
        for raw in path.read_text(errors="replace").splitlines():
            line = raw.split(" #")[0].strip()
            match = _REQUIREMENT_NAME_RE.match(line)
            if line and not line.startswith(("#", "-", ".", "/")) and match:
                chosen.setdefault(match.group(0).lower().replace("_", "-"), line)
    return [chosen[name] for name in sorted(chosen)]


def prepare_python_env(root: Path, setup_script: str = "") -> None:
    """The repo's own setup when it has one in this worktree, else a generic env; a repo with no Python gets neither
    env nor editor settings. Either is skipped when what it is built from did not change."""
    if setup_script and (root / setup_script).is_file():
        _run_setup_script(root, root / setup_script)
        return
    files = find_requirement_files(root)
    if not files:
        return
    venv = root / ".venv"
    python = venv / "bin" / "python"
    pinned = read_pinned_python(root)
    if python.exists() and pinned and _is_bridge_venv(venv) and not _venv_runs(venv, pinned):
        # The settings stamp lives in the env; without it the settings would pass for theirs.
        if _is_our_settings(root):
            (root / _SETTINGS_FILE).unlink()
        shutil.rmtree(venv)
    if not python.exists():
        _run(["uv", "venv", "-q", "--python", pinned or ">=3.11", str(venv)], ENV_TIMEOUT_S)
    lines = map_requirements_to_consolidated(files)
    wanted = hashlib.sha256("\n".join(lines).encode()).hexdigest()
    stamp = venv / _REQUIREMENTS_STAMP
    if not stamp.exists() or stamp.read_text().strip() != wanted:
        _install_requirements(venv, python, lines)
        stamp.write_text(wanted)
    write_editor_settings(root, python, files)


def _run_setup_script(root: Path, script: Path) -> None:
    """Its env replaces bridge's generic one, which has another Python and lacks the repo's own packages, and its
    editor settings replace bridge's, which it would otherwise keep; theirs stay as they were, whatever it writes. The
    settings it writes are stamped as bridge's, so removing the worktree does not take them for theirs."""
    venv = root / ".venv"
    stamp = venv / _SETUP_STAMP
    wanted = _hash_setup_inputs(root, script)
    with contextlib.suppress(OSError):
        if stamp.read_text().strip() == wanted:
            return
    settings = root / _SETTINGS_FILE
    if _is_our_settings(root):
        settings.unlink()
    if (venv / _REQUIREMENTS_STAMP).exists():
        shutil.rmtree(venv)
    theirs = settings.read_bytes() if settings.exists() else None
    try:
        _run([str(script)], ENV_TIMEOUT_S, cwd=root)
    finally:
        if theirs is not None and (not settings.is_file() or settings.read_bytes() != theirs):
            settings.parent.mkdir(exist_ok=True)
            settings.write_bytes(theirs)
    if theirs is None and settings.exists():
        (venv / _SETTINGS_STAMP).write_text(hashlib.sha256(settings.read_bytes()).hexdigest())
    stamp.write_text(wanted)


def _hash_setup_inputs(root: Path, script: Path) -> str:
    digest = hashlib.sha256(script.read_bytes())
    for directory, names, files in os.walk(root):
        names[:] = sorted(n for n in names if n not in _NOT_SOURCE_DIRS)
        for name in sorted(n for n in files if n in _SETUP_INPUTS or _is_requirements_file(n)):
            path = Path(directory) / name
            digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def _is_requirements_file(name: str) -> bool:
    return name.startswith("requirements") and name.endswith((".txt", ".in"))


def read_pinned_python(root: Path) -> str:
    """.python-version, else the pythonVersion its pyright config checks against; empty when neither says."""
    with contextlib.suppress(OSError, IndexError):
        line = (root / ".python-version").read_text().splitlines()[0].strip()
        if _PYTHON_PIN_RE.fullmatch(line):
            return line
    with contextlib.suppress(OSError, ValueError):
        config: object = json.loads((root / "pyrightconfig.json").read_text())
        value = cast(dict[str, object], config).get("pythonVersion") if isinstance(config, dict) else None
        if isinstance(value, str) and _PYTHON_PIN_RE.fullmatch(value):
            return value
    return ""


def _is_bridge_venv(venv: Path) -> bool:
    return (venv / _REQUIREMENTS_STAMP).exists() or (venv / _SETUP_STAMP).exists()


def _venv_runs(venv: Path, pinned: str) -> bool:
    try:
        match = _VENV_VERSION_RE.search((venv / "pyvenv.cfg").read_text())
    except OSError:
        return False
    return bool(match) and (match.group(1) == pinned or match.group(1).startswith(f"{pinned}."))


def _install_requirements(venv: Path, python: Path, lines: list[str]) -> None:
    """Services pin versions that cannot all hold at once. This env is for reading code in an editor, so when the pins
    conflict, having every package matters more than which version of it."""
    requirements = venv / "review-requirements.txt"
    requirements.write_text("\n".join(lines) + "\n")
    install = ["uv", "pip", "install", "-q", "--python", str(python), "-r", str(requirements)]
    try:
        _run(install, ENV_TIMEOUT_S)
    except StepFailed:
        requirements.write_text("\n".join(map_requirement_to_name(line) for line in lines) + "\n")
        _run(install, ENV_TIMEOUT_S)


def map_requirement_to_name(line: str) -> str:
    """The version goes; a platform condition stays, or a Windows-only package would break the install here."""
    requirement, separator, condition = line.partition(";")
    name = _VERSION_PART_RE.sub("", requirement).strip()
    return f"{name}; {condition.strip()}" if separator else name


def write_editor_settings(root: Path, python: Path, requirement_files: list[Path]) -> None:
    top_level = dict.fromkeys(f.relative_to(root).parts[0] for f in requirement_files if f.parent != root)
    extra = [str(root), *(str(root / name) for name in top_level)]
    settings_path = root / _SETTINGS_FILE
    if settings_path.exists():
        return
    settings_path.parent.mkdir(exist_ok=True)
    settings = {"python.defaultInterpreterPath": str(python), "python.analysis.extraPaths": extra}
    text = json.dumps(settings, indent=2) + "\n"
    settings_path.write_text(text)
    (root / ".venv" / _SETTINGS_STAMP).write_text(hashlib.sha256(text.encode()).hexdigest())


def review_docs_dir() -> Path:
    return state_dir() / "review-docs"


def register_doc(conn: sqlite3.Connection, ref: str, source: Path, now: int) -> None:
    """Copied in, so the page serves what was registered and never follows a path it was handed later."""
    row = conn.execute("SELECT repo, number, head_oid FROM review_prs WHERE ref = ?", (ref,)).fetchone()
    if row is None:
        raise DocError(f"{ref} is not in the queue")
    if source.suffix.lower() not in (".html", ".htm") or not source.is_file():
        raise DocError(f"{source} is not an HTML file")
    if source.stat().st_size > DOC_MAX_BYTES:
        raise DocError(f"{source} is over {DOC_MAX_BYTES // 1_000_000} MB")
    name = f"{str(row['repo']).replace('/', '-')}-{row['number']}.html"
    target = review_docs_dir() / name
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    shutil.copyfile(source, temporary)
    temporary.replace(target)
    html = target.read_bytes()
    conn.execute(
        "UPDATE review_prs SET doc_file = ?, doc_oid = ?, doc_at = ?, doc_head = ?, doc_reviews = ? WHERE ref = ?",
        (name, row["head_oid"], now, read_doc_head(html), convert_reviews_to_text(map_html_to_reviews(html)), ref),
    )


def read_doc_head(html: bytes) -> str:
    """The commit the doc says it was checked against, from <meta name="pr-head" content="<sha>">; empty without one."""
    for tag in _META_RE.findall(html):
        sha = _CONTENT_SHA_RE.search(tag)
        if _PR_HEAD_NAME_RE.search(tag) and sha:
            return sha.group(1).decode().lower()
    return ""


def fill_doc_heads(conn: sqlite3.Connection) -> None:
    """Docs registered before bridge read the marker: read once, and an empty one means there is none."""
    for row in conn.execute("SELECT ref, doc_file FROM review_prs WHERE doc_file IS NOT NULL AND doc_head IS NULL"):
        try:
            html = (review_docs_dir() / Path(str(row["doc_file"])).name).read_bytes()
        except OSError:
            html = b""
        conn.execute("UPDATE review_prs SET doc_head = ? WHERE ref = ?", (read_doc_head(html), row["ref"]))


def fill_doc_reviews(conn: sqlite3.Connection) -> None:
    """Docs registered before bridge read their Reviews section: read once."""
    for row in conn.execute("SELECT ref, doc_file FROM review_prs WHERE doc_file IS NOT NULL AND doc_reviews IS NULL"):
        try:
            html = (review_docs_dir() / Path(str(row["doc_file"])).name).read_bytes()
        except OSError:
            html = b""
        reviews = convert_reviews_to_text(map_html_to_reviews(html))
        conn.execute("UPDATE review_prs SET doc_reviews = ? WHERE ref = ?", (reviews, row["ref"]))


def map_row_to_doc_heads(row: sqlite3.Row) -> tuple[str, bool]:
    """(the commit the doc is at, whether that is the PR's head). Its marker wins over the head it was registered at,
    which can lag when the doc was registered before bridge saw the push."""
    head = str(row["head_oid"])
    marker = str(row["doc_head"] or "")
    if marker:
        return marker, head.lower().startswith(marker)
    return str(row["doc_oid"] or ""), row["doc_oid"] == head


def map_doc_to_served(conn: sqlite3.Connection, repo: str, number: int) -> bytes | None:
    """The registered doc with a contents bar from its headings, and a line on top when it is behind the PR."""
    doc = read_registered_doc(conn, repo, number)
    if doc is not None:
        doc = convert_doc_to_viewable(doc)
    row = conn.execute("SELECT * FROM review_prs WHERE repo = ? AND number = ?", (repo, number)).fetchone()
    if doc is None or row is None:
        return doc
    at, current = map_row_to_doc_heads(row)
    if current:
        return doc
    updating = row["doc_update_for"] == row["head_oid"]
    words = f"doc at {at[:DOC_SHA_CHARS] or 'unknown'}, PR at {str(row['head_oid'])[:DOC_SHA_CHARS]}"
    words += "; its owner is updating it" if updating else ""
    banner = (
        '<div style="font:14px system-ui,sans-serif;padding:8px 12px;background:#fff3cd;color:#5c4400;'
        f'border-bottom:1px solid #e0c36a">{words}</div>'
    ).encode()
    body = _BODY_TAG_RE.search(doc)
    return doc[: body.end()] + banner + doc[body.end() :] if body else banner + doc


def read_registered_doc(conn: sqlite3.Connection, repo: str, number: int) -> bytes | None:
    row = conn.execute("SELECT doc_file FROM review_prs WHERE repo = ? AND number = ?", (repo, number)).fetchone()
    if row is None or not row["doc_file"]:
        return None
    try:
        return (review_docs_dir() / Path(str(row["doc_file"])).name).read_bytes()
    except OSError:
        return None
