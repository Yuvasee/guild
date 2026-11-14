import fnmatch
import json
import re
import subprocess
import time
import traceback
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bridge.errlog import log_error
from bridge.review_github import GitHubError, run_gh

if TYPE_CHECKING:
    from concurrent.futures import Future

_URL_REF_RE = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d{1,9})\b", re.IGNORECASE)
_REPO_REF_RE = re.compile(r"(?<![\w./-])([\w.-]+/[\w.-]+)#(\d{1,9})\b")
_BARE_REF_RE = re.compile(r"(?<![\w/&#])#(\d{1,9})\b")
# "proj-42-backport-1.13" names a release, not PR 1.
_BRANCH_REF_RE = re.compile(r"backport[-_/]?(?:of[-_/]?)?(?:pr[-_/]?)?(\d{1,9})(?![\d.])", re.IGNORECASE)
_HUNK_HEADER_RE = re.compile(rb"@@ -\d{1,9}(?:,(\d{1,9}))? \+\d{1,9}(?:,(\d{1,9}))? @@(.*)")
_NAMED_MAX = 6
_BACKPORT_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name }
    pullRequest(number: $number) { title body headRefName baseRefName }
  }
}"""


class BackportError(Exception):
    pass


@dataclass(frozen=True)
class BackportVerdict:
    waived: bool
    reason: str


def is_backport_base(bases: tuple[str, ...], base: str) -> bool:
    return any(fnmatch.fnmatchcase(base, glob) for glob in bases)


def find_named_prs(repo: str, number: int, title: str, body: str, head: str) -> tuple[int, ...]:
    """PR numbers of this repo that a backport names: #N, owner/repo#N or a PR URL in its title or body, or
    backport-N in its branch."""
    found: set[int] = set()
    for text in (title, body):
        for match in (*_URL_REF_RE.finditer(text), *_REPO_REF_RE.finditer(text)):
            if match.group(1).casefold() == repo.casefold():
                found.add(int(match.group(2)))
        found.update(int(m.group(1)) for m in _BARE_REF_RE.finditer(text))
    found.update(int(m.group(1)) for m in _BRANCH_REF_RE.finditer(head))
    found.discard(number)
    return tuple(sorted(found))


def map_diff_to_hunks(diff: bytes) -> list[tuple[bytes, bytes]]:
    """(patch, function heading) for each hunk, the patch under its file's header, so a backport combining several PRs
    that touch the same file still splits into their hunks. Lines split on LF alone, as git does, and each hunk is
    read by its declared line counts: content that looks like a header can't start a hunk of its own."""
    lines = diff.split(b"\n")
    if lines and not lines[-1]:
        lines.pop()
    hunks: list[tuple[bytes, bytes]] = []
    i = 0
    while i < len(lines):
        if not lines[i].startswith(b"diff --git "):
            raise BackportError("the diff has a line outside any file or hunk")
        first = i
        i += 1
        while i < len(lines) and not lines[i].startswith((b"@@ ", b"diff --git ")):
            if lines[i].startswith((b"Binary files ", b"GIT binary patch")):
                # GitHub's diff shows only that a binary file changed, so two different binary changes would match.
                name = lines[first][len(b"diff --git ") :].decode("utf-8", "backslashreplace")
                raise BackportError(f"it changes a binary file ({name})")
            i += 1
        header = lines[first:i]
        if i == len(lines) or lines[i].startswith(b"diff --git "):
            hunks.append((b"\n".join(header) + b"\n", b""))
            continue
        while i < len(lines) and lines[i].startswith(b"@@ "):
            match = _HUNK_HEADER_RE.match(lines[i])
            if not match:
                raise BackportError("the diff has a malformed hunk header")
            old, new = int(match.group(1) or 1), int(match.group(2) or 1)
            hunk_start = i
            i += 1
            while old or new or (i < len(lines) and lines[i].startswith(b"\\")):
                tag = lines[i][:1] if i < len(lines) else b""
                old -= tag in (b" ", b"-")
                new -= tag in (b" ", b"+")
                if tag not in (b" ", b"-", b"+", b"\\") or old < 0 or new < 0:
                    raise BackportError("a hunk's lines do not match its header")
                i += 1
            hunks.append((b"\n".join(header + lines[hunk_start:i]) + b"\n", match.group(3).strip()))
    return hunks


def map_diff_to_patch_ids(diff: bytes, timeout_s: float) -> Counter[str]:
    """The multiset of the diff's per-hunk patch ids, each with its hunk's function heading, which patch-id ignores
    along with line numbers. --verbatim keeps whitespace: --stable alone ignores it, and an indentation change is a
    real change in Python or YAML."""
    hunks = map_diff_to_hunks(diff)
    stream = b"".join(b"commit %040x\n%s" % (i, patch) for i, (patch, _) in enumerate(hunks, 1))
    try:
        done = subprocess.run(
            ["git", "patch-id", "--verbatim"], input=stream, capture_output=True, timeout=timeout_s, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackportError(f"git patch-id failed: {type(exc).__name__}") from exc
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip().splitlines() or ["no output"]
        raise BackportError(f"git patch-id failed: {detail[-1][:200]}")
    ids: dict[int, str] = {}
    for line in done.stdout.decode("ascii", "replace").splitlines():
        patch_id, _, tag = line.partition(" ")
        ids[int(tag, 16)] = patch_id
    if sorted(ids) != list(range(1, len(hunks) + 1)):
        raise BackportError(f"git patch-id read {len(ids)} of {len(hunks)} hunks")
    return Counter(f"{ids[i]} {heading.decode('utf-8', 'backslashreplace')}" for i, (_, heading) in enumerate(hunks, 1))


def check_backport(repo: str, number: int, bases: tuple[str, ...], deadline: float) -> BackportVerdict | None:
    """Whether a backport may merge without an approval: its diff equals that of the merged default-branch PRs it
    names, together. None when its base is not a backport branch. Fails closed: anything unread keeps the rule."""
    try:
        return _check_backport(repo, number, bases, deadline)
    except (GitHubError, BackportError) as exc:
        return BackportVerdict(False, f"the backport check failed: {exc}")
    except Exception:
        # The hook lets a command through when it raises, so a bug here must still refuse.
        log_error("backport check", traceback.format_exc())
        return BackportVerdict(False, "the backport check failed: internal error, logged")


def _check_backport(repo: str, number: int, bases: tuple[str, ...], deadline: float) -> BackportVerdict | None:
    owner, _, name = repo.partition("/")
    args = ["api", "graphql", "-f", f"query={_BACKPORT_QUERY}", "-f", f"owner={owner}", "-f", f"name={name}"]
    raw: Any = _load_json(run_gh([*args, "-F", f"number={number}"], _time_left(deadline)))
    try:
        default = str(raw["data"]["repository"]["defaultBranchRef"]["name"])
        pr: Any = raw["data"]["repository"]["pullRequest"]
        base = str(pr["baseRefName"])
        named = find_named_prs(repo, number, str(pr["title"] or ""), str(pr["body"] or ""), str(pr["headRefName"]))
    except (KeyError, TypeError) as exc:
        raise BackportError(f"unexpected answer from GitHub: {type(exc).__name__}") from exc
    if not is_backport_base(bases, base):
        return None
    if not named:
        return BackportVerdict(False, f"no merged {default} PR named")
    if len(named) > _NAMED_MAX:
        return BackportVerdict(False, f"it names {len(named)} PRs, more than {_NAMED_MAX} to compare")
    # Every hook loads this module; only a backport check needs threads.
    from concurrent.futures import ThreadPoolExecutor

    # A named number may be an issue, whose diff gh cannot read; which ones are PRs is known only at the end.
    with ThreadPoolExecutor(max_workers=len(named) + 2) as pool:
        states = pool.submit(_fetch_named_states, repo, named, deadline)
        diffs = {n: pool.submit(_fetch_diff, repo, n, deadline) for n in (number, *named)}
        kinds = states.result()
        mains: list[int] = []
        for n in named:
            kind, merged, into = kinds[n]
            if kind != "PullRequest":
                continue
            if not merged or into != default:
                return BackportVerdict(False, f"#{n} is not merged into {default}")
            mains.append(n)
        if not mains:
            return BackportVerdict(False, f"no merged {default} PR named")
        ours = _read_patch_ids(diffs[number], deadline)
        if not ours:
            return BackportVerdict(False, "the backport's diff is empty")
        theirs: Counter[str] = Counter()
        for n in mains:
            theirs.update(_read_patch_ids(diffs[n], deadline))
    names = " + ".join(f"#{n}" for n in mains)
    if ours != theirs:
        return BackportVerdict(False, f"backport diff differs from {names}")
    combined = "identical diff" if len(mains) == 1 else "identical combined diff"
    return BackportVerdict(True, f"backport of {names}, {combined}: no approval needed")


def _fetch_named_states(repo: str, numbers: tuple[int, ...], deadline: float) -> dict[int, tuple[str, bool, str]]:
    owner, _, name = repo.partition("/")
    fields = " ".join(
        f"n{n}: issueOrPullRequest(number: {n}) {{ __typename ... on PullRequest {{ merged baseRefName }} }}"
        for n in numbers
    )
    query = f"query($owner: String!, $name: String!) {{ repository(owner: $owner, name: $name) {{ {fields} }} }}"
    raw: Any = _load_json(
        run_gh(["api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}", "-f", f"name={name}"],
               _time_left(deadline))
    )  # fmt: skip
    try:
        nodes: Any = raw["data"]["repository"]
        return {
            n: (str(nodes[f"n{n}"]["__typename"]), bool(nodes[f"n{n}"].get("merged")),
                str(nodes[f"n{n}"].get("baseRefName") or ""))
            for n in numbers
        }  # fmt: skip
    except (KeyError, TypeError, AttributeError) as exc:
        raise BackportError(f"unexpected answer from GitHub: {type(exc).__name__}") from exc


def _fetch_diff(repo: str, number: int, deadline: float) -> bytes:
    """Raw bytes: decoding would fold a CR into a line break and distinct invalid bytes into one replacement."""
    try:
        done = subprocess.run(
            ["gh", "pr", "diff", str(number), "--repo", repo],
            capture_output=True,
            timeout=_time_left(deadline),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitHubError(f"gh failed: {type(exc).__name__}") from exc
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip().splitlines() or ["no output"]
        raise GitHubError(f"gh failed: {detail[-1][:200]}")
    return done.stdout


def _read_patch_ids(diff: "Future[bytes]", deadline: float) -> Counter[str]:
    return map_diff_to_patch_ids(diff.result(), _time_left(deadline))


def _time_left(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0.1:
        raise BackportError("it ran out of time")
    return left


def _load_json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError as exc:
        raise BackportError("unexpected answer from GitHub: not JSON") from exc
