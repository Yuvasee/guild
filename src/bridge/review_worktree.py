import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from bridge.backport import is_backport_base
from bridge.config import Config, state_dir
from bridge.db import now_ms, transaction
from bridge.doc_nav import ADDED, BEHIND_BANNER_STYLE, convert_doc_to_viewable, remove_injected_parts
from bridge.leads import REVIEW_DOC_SPEC
from bridge.review_doc_check import (
    PlainLimits,
    find_doc_problems,
    find_screenshot_problems,
    read_doc_head,
    read_reader_rounds,
)
from bridge.review_marks import convert_reviews_to_text, map_html_to_reviews

FRESH_FOR_PREPARING_MS = 14 * 24 * 3_600_000
REVIEW_LARGE_LOC = 100
"""At this many lines a PR gets a review document and a worktree."""
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
_PYRIGHT_FILE = "pyrightconfig.json"
_PYRIGHT_STAMP = "review-pyright.sha256"
_STAMP_DONE = "indexed"
_NOTHING_WRITTEN = "none"
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
    python_paths: tuple[str, ...] = ()
    """review_python_paths: shared source roots added to the editor's and each service's import paths."""
    base: str = ""
    """The branch the PR merges into, which its review window diffs against."""

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
    """Old stacks must not eat the disk, and what the owner has finished with needs no worktree. A backport is its
    lead's to merge, so it gets no worktree and no doc."""
    if is_backport_base(config.backport_bases, str(row["base"])):
        return False
    if row["draft"] or row["closed_at"] is not None or int(row["loc"]) < REVIEW_LARGE_LOC:
        return False
    if has_owner_reviewed(row) or row["post_state"] == "external":
        return False
    return now - _map_iso_to_ms(str(row["updated_at"])) <= FRESH_FOR_PREPARING_MS


def has_owner_reviewed(row: sqlite3.Row) -> bool:
    return bool(row["reviewed_at"] or row["owner_reviewed_at"])


def _map_iso_to_ms(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def map_ref_to_worktree_path(config: Config, repo: str, number: int) -> Path | None:
    name = repo.rpartition("/")[2]
    if not config.review_worktrees_dir or not _SAFE_NAME_RE.fullmatch(name):
        return None
    return Path(config.review_worktrees_dir).expanduser() / f"review-{name}-{number}"


def map_worktree_to_workspace(path: Path) -> Path:
    """Beside the worktree, not in it, where an untracked file would read as the owner's change."""
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
        # A backport is its lead's to merge, so the owner's review checkout of it goes like a closed PR's.
        done = row["closed_at"] is not None or is_backport_base(config.backport_bases, str(row["base"]))
        if done and row["wt_path"] and not cooling(row):
            return _map_row_to_job(row, "remove", Path(str(row["wt_path"])), config)
    for row in rows:
        if row["wt_path"] or cooling(row) or not is_worth_preparing(row, config, now):
            continue
        path = map_ref_to_worktree_path(config, str(row["repo"]), int(row["number"]))
        if path is not None:
            return _map_row_to_job(row, "create", path, config)
    # The longest-waiting refresh goes first: newest-PR-first let one slow install, cut by each serve restart, starve
    # every other checkout for a day.
    for row in sorted(rows, key=lambda r: int(r["wt_at"] or 0)):
        if row["closed_at"] is not None or not row["wt_path"] or cooling(row):
            continue
        if is_backport_base(config.backport_bases, str(row["base"])):
            continue
        # Also with the head unchanged: a failed install or a cleaned-up worktree has to settle.
        stale = row["wt_oid"] != row["head_oid"] or row["wt_state"] in (*_LOOKED_AT_HOURLY, DETACHED, PREPARING)
        if stale or _has_outdated_env(row, config):
            return _map_row_to_job(row, "refresh", Path(str(row["wt_path"])), config)
    return None


def run_next_worktree_job(conn: sqlite3.Connection, config: Config) -> bool:
    """The one job runner: a process of its own, apart from serve, so a restart neither kills its install nor starts
    it again; a second runner leaves at once. Every row still preparing was cut off by a runner that died."""
    lock_path = worktree_job_lock()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        job = pick_worktree_job(conn, config, now_ms())
        if job is not None:
            run_worktree_job(conn, config, job, now_ms())
        return job is not None


def is_worktree_job_running() -> bool:
    try:
        with worktree_job_lock().open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    return False


def spawn_worktree_job() -> None:
    """Detached, with its output in a file: a pipe to a restarted serve would end the install at its next line."""
    log = worktree_job_lock().with_name("worktree-job.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as out:
        subprocess.Popen(
            [sys.executable, "-m", "bridge", "_worktree-job"],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
            start_new_session=True,
        )


def worktree_job_lock() -> Path:
    return state_dir() / "locks" / "worktree-job.lock"


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
        config.review_python_paths,
        str(row["base"] or ""),
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
    if job.action in ("refresh", "create"):
        # Preparing until its result is written: one killed midway, its install half done, never reads as ready, and
        # it goes to the back of the line.
        with transaction(conn):
            record_worktree(conn, job.ref, kept_path, row["wt_oid"], PREPARING, f"{job.action} started", now)
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
        _link_worktree_to_pr(job.repo, job.number, result[0], job.base)
    with transaction(conn):
        record_worktree(conn, job.ref, result[0], result[1], result[2], result[3], now)


def link_worktrees_to_prs(conn: sqlite3.Connection, python_paths: tuple[str, ...] = ()) -> None:
    """Worktrees made before they had a workspace, or before its current settings, get it here, without waiting for
    their head to move."""
    for row in conn.execute("SELECT repo, number, base, wt_path FROM review_prs WHERE wt_path IS NOT NULL"):
        path = Path(str(row["wt_path"]))
        if not map_worktree_to_workspace(path).exists():
            _link_worktree_to_pr(str(row["repo"]), int(row["number"]), path, str(row["base"] or ""))
        elif (path / ".git").exists():
            try:
                write_workspace(path, str(row["repo"]), int(row["number"]), str(row["base"] or ""))
            except OSError as exc:
                print(f"review workspace for {row['repo']}#{row['number']} failed: {exc}", flush=True)
        stamp = _map_worktree_to_pyright_stamp(path)
        if stamp is not None and not _read_pyright_stamp(path)[1]:
            try:
                write_pyright_config(path, python_paths)
            except (StepFailed, OSError) as exc:
                print(f"review pyright config for {row['repo']}#{row['number']} failed: {exc}", flush=True)


def _link_worktree_to_pr(repo: str, number: int, path: Path, base: str) -> None:
    """The workspace the Review link opens, and the branch key the GitHub Pull Requests extension reads to show this
    PR there. Neither stops the worktree from being used, so a failure is only printed."""
    if not (path / ".git").exists():
        return
    owner, _, name = repo.partition("/")
    try:
        key = f"branch.{map_number_to_branch(number)}.github-pr-owner-number"
        _run(["git", "-C", str(path), "config", "--local", key, f"{owner}#{name}#{number}"], GIT_TIMEOUT_S)
        write_workspace(path, repo, number, base)
    except (StepFailed, OSError) as exc:
        print(f"review workspace for {repo}#{number} failed: {exc}", flush=True)


def write_workspace(path: Path, repo: str, number: int, base: str) -> None:
    """Bridge's settings go in where missing; the owner's folders, settings and anything else they added stay. A file
    that is not plain JSON is left alone, as rewriting it would drop their comments."""
    ours: dict[str, object] = {
        "workbench.secondarySideBar.defaultVisibility": "hidden",
        "githubPullRequests.focusedMode": "overview",
    }
    # The local extension in vscode/guild-review-pr-tree diffs the worktree's HEAD against this base, again each time
    # bridge moves the head: the GitHub Pull Requests extension's own changes view keeps the file list it first fetched.
    guild: dict[str, object] = {"guild.reviewPullRequest": f"https://github.com/{repo}/pull/{number}"}
    if base:
        guild["guild.reviewBase"] = base
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
    his = cast(dict[str, object], settings) if isinstance(settings, dict) else {}
    merged = {
        "folders": current.get("folders") or [{"path": str(path)}],
        **{key: value for key, value in current.items() if key not in ("folders", "settings")},
        "settings": {**ours, **his, **guild},
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
    if _branch_holds_his_commits(checkout, job):
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
    with nothing of the owner's is reset to the new head."""
    problem = _find_his_work_in_the_way(job, fetched=False)
    if problem:
        return job.path, job.recorded_oid, DIRTY, problem
    if checkout is None:
        return job.path, job.recorded_oid, NO_CHECKOUT, f"no main checkout of {job.repo} on this machine"
    _fetch_head_branch(checkout, job)
    # A fetch can take minutes, and the owner may have committed, edited or switched branch during it.
    problem = _find_his_work_in_the_way(job, fetched=True)
    if problem:
        return job.path, job.recorded_oid, DIRTY, problem
    if _read_branch(job.path) is None:
        _run(["git", "-C", str(job.path), "checkout", "-q", "-B", job.branch], GIT_TIMEOUT_S)
    _run(["git", "-C", str(job.path), "branch", "-q", f"--set-upstream-to={job.upstream}"], GIT_TIMEOUT_S)
    restore_pyright_config(job.path)
    if _is_ancestor(job.path, "HEAD", job.upstream):
        _run(["git", "-C", str(job.path), "merge", "-q", "--ff-only", job.upstream], GIT_TIMEOUT_S)
    else:
        _run(["git", "-C", str(job.path), "reset", "-q", "--hard", job.upstream], GIT_TIMEOUT_S)
    return _finish_with_env(job)


def _find_his_work_in_the_way(job: WorktreeJob, fetched: bool) -> str | None:
    """What a refresh could lose; None when nothing. Once fetched, also a file of the owner's that the new head would
    write over: env, editor and ignored files pass the status check but must not be clobbered."""
    if _has_local_commits(job):
        return "local commits, not refreshed"
    branch = _read_branch(job.path)
    if branch not in (None, job.branch):
        return f"on another branch, {branch}, not refreshed"
    if branch is None and _branch_holds_his_commits(job.path, job):
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


def _branch_holds_his_commits(repo: Path, job: WorktreeJob) -> bool:
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
        prepare_python_env(job.path, job.setup_script, job.python_paths)
        write_pyright_config(job.path, job.python_paths)
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
    """When the SSH agent holds no key, once more with gh's token. Only for this fetch: the remote's URL and the owner's
    git config stay as they are, and other credential helpers are off so none can show a dialog."""
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
    """Only through git, and only what holds nothing of the owner's; its branch goes with it on the same terms."""
    if not job.path.exists():
        if checkout is not None and not _branch_holds_his_commits(checkout, job):
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
    keep_branch = _branch_holds_his_commits(checkout, job)
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
    stay where they are, so only the owner's tracked edits and other new files hold a refresh back."""
    entries = [
        (code, name)
        for code, name in _read_status(path)
        if code != "!!" and not (code == "??" and name.startswith(_OURS_DIRS))
    ]
    dirty = _map_status_to_his_files(path, entries)
    return f"local changes: {', '.join(dirty[:5])}" if dirty else None


def _list_dirty_files(path: Path) -> list[str]:
    # An ignored file can be the owner's too (an ignored .vscode/launch.json), and removing the worktree deletes it,
    # so of those only what tools rebuild is left out; a tracked one never.
    entries: list[tuple[str, str]] = []
    for code, name in _read_status(path):
        if name.startswith(".venv/") or (code == "!!" and is_rebuilt_by_tools(name)):
            continue
        if not (name == _SETTINGS_FILE and _is_our_settings(path)):
            entries.append((code, name))
    return _map_status_to_his_files(path, entries)


def _map_status_to_his_files(root: Path, entries: list[tuple[str, str]]) -> list[str]:
    """Our pyright config never counts; the owner's edit of it always does, staged too, though skip-worktree hides an
    unstaged one from git status."""
    rest = [name for _, name in entries if name != _PYRIGHT_FILE]
    codes = [code for code, name in entries if name == _PYRIGHT_FILE]
    if any(code[0] not in " ?" for code in codes) or (codes and codes[0] == "??"):
        return [*rest, _PYRIGHT_FILE]
    if not codes and not _read_pyright_stamp(root)[0]:
        return rest
    return [*rest, _PYRIGHT_FILE] if _is_his_pyright_edit(root, _read_tracked_pyright(root)) else rest


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
    """Services nest them at any depth, so every level is read, as the workspace's own consolidated env does."""
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


def prepare_python_env(root: Path, setup_script: str = "", python_paths: tuple[str, ...] = ()) -> None:
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
        # The settings stamp lives in the env; without it the settings would pass for the owner's.
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
    write_editor_settings(root, python, files, python_paths)


def _run_setup_script(root: Path, script: Path) -> None:
    """Its env replaces bridge's generic one, which has another Python and lacks the repo's own packages, and its
    editor settings replace bridge's, which it would otherwise keep; the owner's stay as they were, whatever it writes.
    The settings it writes are stamped as bridge's, so removing the worktree does not take them for the owner's."""
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
    his = settings.read_bytes() if settings.exists() else None
    try:
        _run([str(script)], ENV_TIMEOUT_S, cwd=root)
    finally:
        if his is not None and (not settings.is_file() or settings.read_bytes() != his):
            settings.parent.mkdir(exist_ok=True)
            settings.write_bytes(his)
    if his is None and settings.exists():
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


def write_editor_settings(
    root: Path, python: Path, requirement_files: list[Path], python_paths: tuple[str, ...] = ()
) -> None:
    top_level = dict.fromkeys(f.relative_to(root).parts[0] for f in requirement_files if f.parent != root)
    extra = [str(root), *(str(root / name) for name in top_level)]
    extra += [str(root / p) for p in python_paths if (root / p).is_dir()]
    settings_path = root / _SETTINGS_FILE
    if settings_path.exists():
        return
    settings_path.parent.mkdir(exist_ok=True)
    settings = {"python.defaultInterpreterPath": str(python), "python.analysis.extraPaths": extra}
    text = json.dumps(settings, indent=2) + "\n"
    settings_path.write_text(text)
    (root / ".venv" / _SETTINGS_STAMP).write_text(hashlib.sha256(text.encode()).hexdigest())


def write_pyright_config(root: Path, python_paths: tuple[str, ...] = ()) -> None:
    """The repo's own pyrightconfig.json plus one execution environment per service directory, as each service runs
    from its own directory and several have a top-level package of the same name (say, utils) that a flat
    extraPaths list resolves to the wrong one. Only over a tracked config, so editor settings never lose to a config
    of ours; marked skip-worktree so the review's diff stays the PR's. Each step leaves a state the next pass reads
    right: the stamp names the new config before the file changes, and says done only once git skips it; both are
    replaced whole, never left half-written."""
    stamp = _map_worktree_to_pyright_stamp(root)
    if stamp is None:
        return
    tracked = _read_tracked_pyright(root)
    if _is_his_pyright_edit(root, tracked) or _has_staged_pyright(root):
        return
    wanted = (
        map_pyright_config_to_service_envs(tracked, find_service_roots(root), python_paths)
        if tracked is not None
        else None
    )
    # Again: the owner may have saved an edit while the services were looked for.
    if _is_his_pyright_edit(root, tracked):
        return
    if wanted is None:
        restore_pyright_config(root)
        _write_whole(stamp, f"{_NOTHING_WRITTEN}\n{_STAMP_DONE}\n", stamp.parent)
        return
    digest = hashlib.sha256(wanted.encode()).hexdigest()
    config_path = root / _PYRIGHT_FILE
    if _read_text(config_path) != wanted:
        _write_whole(stamp, "\n".join([digest, *_read_pyright_stamp(root)[0]]) + "\n", stamp.parent)
        _write_whole(config_path, wanted, stamp.parent)
    _run(["git", "-C", str(root), "update-index", "--skip-worktree", _PYRIGHT_FILE], GIT_TIMEOUT_S)
    _write_whole(stamp, f"{digest}\n{_STAMP_DONE}\n", stamp.parent)


def _write_whole(path: Path, text: str, scratch: Path) -> None:
    """Through a file in the worktree's git dir, where one left behind is no new file of the owner's."""
    temporary = scratch / f"{path.name}.tmp"
    temporary.write_text(text)
    temporary.replace(path)


def restore_pyright_config(root: Path) -> None:
    """The tracked one back, before git moves the branch, so a new head's own change to it applies. The caller has
    made sure no edit of the owner's is there."""
    stamp = _map_worktree_to_pyright_stamp(root)
    if stamp is None or not stamp.exists():
        return
    if _read_pyright_stamp(root)[0]:
        _run(["git", "-C", str(root), "update-index", "--no-skip-worktree", _PYRIGHT_FILE], GIT_TIMEOUT_S)
        tracked = _read_tracked_pyright(root)
        if tracked is not None and _read_text(root / _PYRIGHT_FILE) != tracked:
            _run(["git", "-C", str(root), "checkout", "-q", "--", _PYRIGHT_FILE], GIT_TIMEOUT_S)
    stamp.unlink()


def map_pyright_config_to_service_envs(
    text: str, service_roots: list[str], python_paths: tuple[str, ...] = ()
) -> str | None:
    """None when there is nothing to add: no services, a config that is not plain JSON, extends another, or has its
    own environments. An environment's extraPaths replace the top-level ones and its root replaces the repo's, so each
    repeats them after its own dir and the shared source roots the config already lists, and ends with the repo's
    root."""
    try:
        config: object = json.loads(text)
    except ValueError:
        return None
    if not service_roots or not isinstance(config, dict):
        return None
    config = cast(dict[str, object], config)
    if "executionEnvironments" in config or "extends" in config:
        return None
    extra = config.get("extraPaths")
    shared = [str(p) for p in cast(list[object], extra)] if isinstance(extra, list) else []
    envs: list[dict[str, object]] = []
    # Pyright takes the first environment whose root holds the file, so a nested service comes before its parent.
    for name in sorted(service_roots, key=lambda n: (-n.count("/"), n)):
        listed = [p for p in python_paths if p in shared]
        envs.append({"root": name, "extraPaths": list(dict.fromkeys([name, *listed, *shared, "."]))})
    return json.dumps({**config, "executionEnvironments": envs}, indent=2) + "\n"


def find_service_roots(root: Path) -> list[str]:
    """Each directory with its own requirements.txt runs as a service from that directory; relative to the repo."""
    return sorted({f.parent.relative_to(root).as_posix() for f in find_requirement_files(root) if f.parent != root})


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def _read_tracked_pyright(root: Path) -> str | None:
    """None when HEAD has none; a failing git raises, so a passing error is never taken for no config."""
    listed = _run(["git", "-C", str(root), "ls-tree", "--name-only", "HEAD", "--", _PYRIGHT_FILE], GIT_TIMEOUT_S)
    if not listed.strip():
        return None
    return _run(["git", "-C", str(root), "show", f"HEAD:{_PYRIGHT_FILE}"], GIT_TIMEOUT_S)


def _has_staged_pyright(root: Path) -> bool:
    try:
        _run(["git", "-C", str(root), "diff", "--cached", "--quiet", "HEAD", "--", _PYRIGHT_FILE], GIT_TIMEOUT_S)
    except StepFailed:
        return True
    return False


def _map_worktree_to_pyright_stamp(root: Path) -> Path | None:
    """In the worktree's own git dir, which outlives a rebuilt .venv; read from .git, not git: it runs each pass."""
    dot_git = root / ".git"
    if dot_git.is_dir():
        return dot_git / _PYRIGHT_STAMP
    text = _read_text(dot_git)
    if text is None or not text.startswith("gitdir:"):
        return None
    git_dir = Path(text.partition(":")[2].strip())
    return (git_dir if git_dir.is_absolute() else root / git_dir) / _PYRIGHT_STAMP


def _read_pyright_stamp(root: Path) -> tuple[list[str], bool]:
    """(hashes of the configs bridge may have left there, the newest first; whether its last write finished)."""
    stamp = _map_worktree_to_pyright_stamp(root)
    lines = (_read_text(stamp) or "").split() if stamp is not None else []
    return [line for line in lines if line not in (_NOTHING_WRITTEN, _STAMP_DONE)], _STAMP_DONE in lines


def _is_his_pyright_edit(root: Path, tracked: str | None) -> bool:
    """Neither the tracked config nor the one bridge stamped; an untracked config is never bridge's, so git status
    alone tells about it."""
    if tracked is None:
        return False
    try:
        current = (root / _PYRIGHT_FILE).read_bytes()
    except OSError:
        return True
    # git show hands the config over with LF line ends, whatever the file on disk has.
    if current.replace(b"\r\n", b"\n") == tracked.encode():
        return False
    return hashlib.sha256(current).hexdigest() not in _read_pyright_stamp(root)[0]


def review_docs_dir() -> Path:
    return state_dir() / "review-docs"


def register_doc(conn: sqlite3.Connection, config: Config, ref: str, source: Path, now: int) -> bytes:
    """Copied in, so the page serves what was registered and never follows a path it was handed later. A new doc starts
    the update requests and the assistant's notice over. Returns the page as registered."""
    row = conn.execute("SELECT * FROM review_prs WHERE ref = ?", (ref,)).fetchone()
    if row is None:
        raise DocError(f"{ref} is not in the queue")
    if source.suffix.lower() not in (".html", ".htm") or not source.is_file():
        raise DocError(f"{source} is not an HTML file")
    if source.stat().st_size > DOC_MAX_BYTES:
        raise DocError(f"{source} is over {DOC_MAX_BYTES // 1_000_000} MB")
    text, _ = remove_injected_parts(source.read_bytes().decode("utf-8", "surrogateescape"))
    page = text.encode("utf-8", "surrogateescape")
    commit = read_doc_head(page) or str(row["head_oid"])
    files = list_files_at(Path(str(row["wt_path"])), commit) if row["wt_path"] else None
    limits = PlainLimits(
        config.doc_in_short_max_bullets,
        config.doc_bullet_max_words,
        config.doc_sentence_max_words,
        config.doc_visible_max_words,
    )
    problems = find_doc_problems(
        page,
        has_owner_reviewed(row),
        config.owner_name,
        files,
        limits,
        str(row["base"]),
        read_reader_rounds(str(source)),
    )
    wt = Path(str(row["wt_path"])) if row["wt_path"] else None
    changed = list_changed_files(wt, str(row["base"]), commit) if wt else None
    problems.extend(find_screenshot_problems(text, changed, config.review_ui_paths))
    if problems:
        listed = "".join(f"\n- {problem}" for problem in problems)
        raise DocError(f"{ref}: the doc was not registered, as {REVIEW_DOC_SPEC} asks:{listed}")
    name = f"{str(row['repo']).replace('/', '-')}-{row['number']}.html"
    target = review_docs_dir() / name
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_bytes(page)
    temporary.replace(target)
    conn.execute(
        """
        UPDATE review_prs SET doc_file = ?, doc_oid = ?, doc_at = ?, doc_head = ?, doc_reviews = ?,
            doc_update_for = NULL, doc_update_first_at = NULL, doc_update_to = NULL, doc_escalated_at = NULL
        WHERE ref = ?
        """,
        (
            name,
            row["head_oid"],
            now,
            read_doc_head(page),
            convert_reviews_to_text(map_html_to_reviews(page, config.review_names)),
            ref,
        ),
    )
    return page


def list_files_at(worktree: Path, commit: str) -> frozenset[str] | None:
    """Every file and directory of the commit, read from the review worktree's repo rather than its checkout, which
    may still stand at an earlier push; None when there is no worktree or it does not have that commit."""
    if not worktree.is_dir():
        return None
    try:
        listed = subprocess.run(
            ["git", "-C", str(worktree), "ls-tree", "-r", "-t", "--name-only", "-z", commit],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=60,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return frozenset(name.decode("utf-8", "replace") for name in listed.split(b"\0") if name)


def list_changed_files(worktree: Path, base: str, commit: str) -> list[str] | None:
    """The files the PR changes against its base, from the review worktree's repo; None when they cannot be read."""
    if not worktree.is_dir():
        return None
    for ref in (f"origin/{base}", base):
        try:
            listed = subprocess.run(
                ["git", "-C", str(worktree), "diff", "--name-only", "-z", f"{ref}...{commit}"],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=60,
                check=True,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        return [name.decode("utf-8", "replace") for name in listed.split(b"\0") if name]
    return None


def fill_doc_heads(conn: sqlite3.Connection) -> None:
    """Docs registered before bridge read the marker: read once, and an empty one means there is none."""
    for row in conn.execute("SELECT ref, doc_file FROM review_prs WHERE doc_file IS NOT NULL AND doc_head IS NULL"):
        try:
            html = (review_docs_dir() / Path(str(row["doc_file"])).name).read_bytes()
        except OSError:
            html = b""
        conn.execute("UPDATE review_prs SET doc_head = ? WHERE ref = ?", (read_doc_head(html), row["ref"]))


def fill_doc_reviews(conn: sqlite3.Connection, config: Config) -> None:
    """Docs registered before bridge read their Reviews section: read once."""
    for row in conn.execute("SELECT ref, doc_file FROM review_prs WHERE doc_file IS NOT NULL AND doc_reviews IS NULL"):
        try:
            html = (review_docs_dir() / Path(str(row["doc_file"])).name).read_bytes()
        except OSError:
            html = b""
        reviews = convert_reviews_to_text(map_html_to_reviews(html, config.review_names))
        conn.execute("UPDATE review_prs SET doc_reviews = ? WHERE ref = ?", (reviews, row["ref"]))


def map_row_to_doc_heads(row: sqlite3.Row) -> tuple[str, bool]:
    """(the commit the doc is at, whether it describes the PR's head: it is that commit, or every change since is a
    comment or docs). Its marker wins over the head it was registered at, which can lag when the doc was registered
    before bridge saw the push."""
    head = str(row["head_oid"])
    marker = str(row["doc_head"] or "")
    if marker:
        at, same = marker, head.lower().startswith(marker)
    else:
        at, same = str(row["doc_oid"] or ""), row["doc_oid"] == head
    return at, same or (row["doc_diff_for"] == map_doc_to_diff_key(at, head) and bool(row["doc_diff_cosmetic"]))


def map_doc_to_diff_key(doc_commit: str, head: str) -> str:
    return f"{doc_commit} {head}"


def map_row_to_doc_behind(row: sqlite3.Row) -> str:
    """How far the code moved past a doc that is behind: commits and changed lines of code once GitHub's compare for
    this doc and head is in."""
    at, _ = map_row_to_doc_heads(row)
    head = str(row["head_oid"])
    shas = f"doc at {at[:DOC_SHA_CHARS] or 'unknown'}, PR at {head[:DOC_SHA_CHARS]}"
    commits, lines = row["doc_diff_commits"], row["doc_diff_lines"]
    if row["doc_diff_for"] != map_doc_to_diff_key(at, head) or commits is None or lines is None:
        return f"doc older than code ({shas})"
    counted = f"{commits} commit{'' if commits == 1 else 's'}, {lines} line{'' if lines == 1 else 's'}"
    return f"doc older than code: {counted} ({shas})"


def map_doc_to_served(conn: sqlite3.Connection, repo: str, number: int) -> bytes | None:
    """The registered doc with a contents bar from its headings, and a line on top when it is behind the PR."""
    doc = read_registered_doc(conn, repo, number)
    if doc is not None:
        doc = convert_doc_to_viewable(doc)
    row = conn.execute("SELECT * FROM review_prs WHERE repo = ? AND number = ?", (repo, number)).fetchone()
    if doc is None or row is None:
        return doc
    _, current = map_row_to_doc_heads(row)
    if current:
        return doc
    words = map_row_to_doc_behind(row)
    banner = f'<div {ADDED}="behind" style="{BEHIND_BANNER_STYLE}">{words}</div>'.encode()
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
