import contextlib
import fnmatch
import json
import subprocess
import time
from collections.abc import Generator, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, cast

from bridge.agent_prefix import strip_agent_prefix
from bridge.config import Config
from bridge.refs import names_issue, needs_issue
from bridge.review_deps import (
    map_markdown_to_dependencies,
    map_markdown_to_goes_with,
    map_markdown_to_preconditions,
    map_markdown_to_related_prs,
)
from bridge.review_marks import find_missing_usage_section, map_markdown_to_reviews

_PAGES_MAX = 20
"""A page of 25: with each PR's commits, checks and reviews, 50 took ~11 s, past which GitHub answers 502."""
_GH_TIMEOUT_S = 60
_SEARCH_QUERY = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 25, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest {
      number title body url isDraft updatedAt mergeStateStatus isInMergeQueue
      headRefName headRefOid baseRefName additions deletions
      repository { nameWithOwner }
      baseRef { associatedPullRequests(states: OPEN, first: 1) { nodes { number } } }
      commits(last: 100) { nodes { commit { oid committedDate statusCheckRollup { state } } } }
      headStatuses: commits(last: 1) { nodes { commit { statusCheckRollup {
        contexts(first: 100) { nodes { ... on StatusContext { context state } } } } } } }
      files(first: 100) { pageInfo { hasNextPage } nodes { path additions deletions } }
      reviewRequests(first: 20) { nodes { requestedReviewer {
        __typename ... on User { login } ... on Team { slug } ... on Bot { login } } } }
      reviews(last: 100) { nodes { state commit { oid } author { __typename login } } }
      latestReviews(first: 100) { nodes { author { __typename login } } }
      reviewThreads(first: 100) { nodes { isResolved } }
      labels(first: 100) { nodes { name } }
    } }
  }
}
"""
# Apart from the search: comments under a hundred threads cost about 50 points per 50 PRs, so the 90 s search
# carries only the thread flags, and a PR's threads are read when they change.
_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) {
    reviewThreads(first: 100) { nodes { isResolved
      comments(first: 1) { nodes { author { __typename login } url body createdAt pullRequestReview { state } } }
      lastComment: comments(last: 1) { totalCount nodes {
        databaseId author { login } body createdAt pullRequestReview { state } } } } }
  } }
}
"""
_MERGE_FACTS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { defaultBranchRef { name } pullRequest(number: $number) {
    isDraft mergeStateStatus baseRefName headRefName title body
    commits(last: 1) { nodes { commit { oid statusCheckRollup { state contexts(first: 100) { nodes {
      ... on StatusContext { context state } } } } } } }
    reviews(last: 100) { nodes { state body submittedAt commit { oid } author { __typename login } } }
    comments(last: 100) { nodes { body createdAt author { __typename login } } }
    reviewThreads(first: 100) { nodes { isResolved comments(first: 1) { nodes { author { __typename login } } }
      lastComment: comments(last: 1) { nodes { author { login } pullRequestReview { state } } } } }
  } }
}
"""
_HEAD_CHECKS_QUERY = """
query($owner: String!, $name: String!, $oid: GitObjectID!) {
  repository(owner: $owner, name: $name) { object(oid: $oid) { ... on Commit {
    statusCheckRollup { contexts(first: 100) { pageInfo { hasNextPage }
    nodes { __typename ... on CheckRun { conclusion annotations(first: 5) { nodes { message } } }
      ... on StatusContext { state } }
  } } } } }
}
"""
_CHECK_FAILED = frozenset({"FAILURE", "TIMED_OUT", "CANCELLED", "STARTUP_FAILURE", "ACTION_REQUIRED", "ERROR"})
_STATUS_RUNNING = frozenset({"PENDING", "EXPECTED"})
# GitHub Actions' own annotation on a job it refused to start, e.g. when the account's payments failed.
_NOT_STARTED = "the job was not started because"
_BILLING = ("account payments have failed", "spending limit")
CI_BLOCKED_BY_BILLING = "CI didn't run: GitHub billing"
CI_BLOCKED_BY_GITHUB = "CI didn't run: GitHub didn't start the jobs"
# The merge guard runs inside a 5 s hook: a read cut short must still end in its refusal.
_MERGE_FACTS_TIMEOUT_S = 3
PENDING = "PENDING"
_UNSENT = (PENDING, "DISMISSED")
REVIEW_LOCKFILES = ("uv.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock")
APPROVED_STATE = "APPROVED"
PERSIST_SANDBOX = "PERSIST_SANDBOX"
NO_SUCH_NUMBER = "NONE"
_FINAL_STATES: dict[str, str] = {}
OWNER_LABEL_COLOR = "0e8a16"
OWNER_LABEL_ABOUT = "The owner reviewed this PR"


def list_loc_excluded(config: Config) -> tuple[str, ...]:
    """File names left out of a PR's line count (fnmatch on the base name)."""
    return (*REVIEW_LOCKFILES, *config.review_generated_files)


DOCS_AND_ASSETS = (
    "*.md", "*.mdx", "*.markdown", "*.rst",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.svg", "*.ico", "*.pdf",
)  # fmt: skip


BOT_MUST_FIX_MARKS = ("🚫 **BLOCKING", "⚠️ **IMPORTANT")
"""How the review bot opens a finding that must be fixed before it approves."""


class GitHubError(Exception):
    pass


@dataclass(frozen=True)
class HisThread:
    url: str
    replies: int
    last_id: str | None
    last_author: str
    last_body: str


@dataclass(frozen=True)
class BotThread:
    """An unresolved thread the review bot opened with a must-fix finding."""

    url: str
    waiting_since: int | None
    """When the bot opened it, while nobody has replied or our reply is still an unsent draft; None once it has a
    sent reply. The bot speaks again only after a reply, and a real objection comes back as a changes-requested review,
    so a later bot comment does not make it wait again."""


@dataclass(frozen=True)
class ReviewThreads:
    open: int
    his: tuple[HisThread, ...]
    colleagues: tuple[str, ...] = ()
    """Who started each colleague's thread still waiting on our side: see map_thread_to_waiting_colleague."""
    bot_must_fix: tuple[BotThread, ...] = ()


@dataclass(frozen=True)
class OpenPr:
    ref: str
    repo: str
    number: int
    title: str
    url: str
    draft: bool
    head: str
    head_oid: str
    base: str
    stack_parent: str | None
    loc: int
    checks: str | None
    requested: int
    approved_by: tuple[str, ...] = ()
    bot_approved: bool = False
    bot_cleared: bool = False
    """The review bot's latest review is of the head commit and does not request changes: reviewed, not blocked."""
    bot_rounds: int = 0
    """How many commits the review bot reviewed: one round each."""
    body_reviews: tuple[str, ...] | None = None
    """The reviews its body's Reviews section names; None without one."""
    usage_missing: str | None = None
    """The words by which it says it only enables something while its body lacks How this will be used."""
    """The review bot's latest verdict is APPROVED: GitHub-approved, though no person has seen the PR."""
    others_reviewed: bool = False
    """A person other than the author reviewed, in any state. GitHub drops a request once its reviewer reviews."""
    merge_state: str | None = None
    in_merge_queue: bool = False
    commits: tuple[tuple[str, int], ...] = ()
    """(oid, committer time), oldest first."""
    updated_at: str = ""
    open_threads: int = 0
    """Unresolved threads, a thread of the owner's pending review among them: GitHub lists it before it is sent."""
    thread_flags: str = ""
    """One letter per thread, r resolved and o open: a thread resolved and another reopened keep the count."""
    depends_on: str | None = None
    """owner/repo#N of the PR its body says it merges only after, when it names one."""
    dependencies: tuple[tuple[str, str], ...] = ()
    """(owner/repo#N, sentence cut to show) for each PR its body says it merges after: a wait on one quotes its
    sentence, so the owner knows what to edit when it is wrong."""
    goes_with: tuple[tuple[str, str], ...] = ()
    """(owner/repo#N, sentence cut to show) for each PR its body says it merges together with: one side saying so
    makes the two siblings, and a sibling's wait quotes the sentence."""
    must_merge: tuple[str, ...] = ()
    """owner/repo#N of the PRs that must be merged first: its dependencies, and a backport's default-branch PRs."""
    preconditions: tuple[str, ...] = ()
    """A blocker for each sentence of its body that holds its merge without naming a PR ('Merge after X is out')."""
    issue_missing: bool = False
    """Its branch, title and body name no issue of a configured team."""
    persist_sandbox: bool = False
    """Its body asks to keep its sandbox (PERSIST_SANDBOX): it never merges by itself."""
    labels: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    """The paths it changes."""
    related_prs: tuple[str, ...] = ()
    """owner/repo#N of the PRs its body's Related lines name."""

    @property
    def commit_oids(self) -> tuple[str, ...]:
        return tuple(oid for oid, _ in self.commits)


@dataclass(frozen=True)
class FetchedComment:
    comment_id: str
    author: str
    created_at: int
    body: str
    url: str
    parent_id: str | None = None
    parent_author: str | None = None
    state: str | None = None
    """A review's verdict: APPROVED, CHANGES_REQUESTED or COMMENTED; None on a comment."""
    commit_id: str | None = None
    by_app: bool = False
    """GitHub calls the author a Bot: an app such as CI, never a person."""
    review_id: str | None = None
    """The review an inline comment was submitted in."""


def map_iso_to_ms(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value).timestamp() * 1000)
    except ValueError:
        return None


class RoundOver(Exception):
    """The bounded round ran out of time. Not a GitHubError, so the loops that skip one PR's failed read stop too."""


@dataclass
class GhRound:
    deadline: float
    """time.monotonic() past which no gh call starts."""
    calls: int = 0
    failed: int = 0
    """Calls that exited non-zero, timed out or could not start."""


_gh_round: ContextVar[GhRound | None] = ContextVar("gh_round", default=None)


@contextlib.contextmanager
def bound_gh_round(seconds: float) -> Generator[GhRound]:
    """Every gh call in this thread inside it is counted, cut at the deadline, and refused with RoundOver past it."""
    gh_round = GhRound(time.monotonic() + seconds)
    token = _gh_round.set(gh_round)
    try:
        yield gh_round
    finally:
        _gh_round.reset(token)


def run_gh_process(args: list[str], timeout_s: float) -> "subprocess.CompletedProcess[str]":
    gh_round = _gh_round.get()
    left = timeout_s if gh_round is None else gh_round.deadline - time.monotonic()
    if gh_round is not None:
        if left <= 0:
            raise RoundOver
        gh_round.calls += 1
    try:
        done = subprocess.run(
            ["gh", *args],
            capture_output=True, encoding="utf-8", errors="replace", timeout=min(timeout_s, left), check=False,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        if gh_round is not None:
            gh_round.failed += 1
            if left < timeout_s:
                raise RoundOver from None
        raise
    except OSError:
        if gh_round is not None:
            gh_round.failed += 1
        raise
    if gh_round is not None and done.returncode != 0:
        gh_round.failed += 1
    return done


def run_gh(args: list[str], timeout_s: float = _GH_TIMEOUT_S) -> str:
    try:
        done = run_gh_process(args, timeout_s)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitHubError(f"gh failed: {type(exc).__name__}") from exc
    if done.returncode != 0:
        raise GitHubError(f"gh failed: {(done.stderr.strip().splitlines() or ['no output'])[-1][:200]}")
    return done.stdout


def fetch_pr_body(repo: str, number: int, timeout_s: float) -> str:
    return run_gh(["api", f"repos/{repo}/pulls/{number}", "--jq", '.body // ""'], timeout_s)


def add_label(repo: str, number: int, label: str) -> None:
    """Creates the label in the repo first when adding it fails, then adds it once more."""
    add = ["api", "-X", "POST", f"repos/{repo}/issues/{number}/labels", "-f", f"labels[]={label}"]
    try:
        run_gh(add)
        return
    except GitHubError:
        pass
    with contextlib.suppress(GitHubError):
        run_gh(["label", "create", label, "-R", repo, "--color", OWNER_LABEL_COLOR, "--description", OWNER_LABEL_ABOUT])
    run_gh(add)


KnownHeads = dict[tuple[str, str, str], tuple[int, tuple[str, ...]]]
"""(ref, head oid, base) to the line count and changed paths already read for that head on that base."""


def fetch_open_prs(config: Config, known: KnownHeads | None = None) -> list[OpenPr]:
    """Raises rather than returning part of the list: a PR missing from it would look closed."""
    query = f"is:pr is:open archived:false author:{config.review_author} org:{config.review_org}"
    prs: list[OpenPr] = []
    after: str | None = None
    for _ in range(_PAGES_MAX):
        args = ["api", "graphql", "-f", f"query={_SEARCH_QUERY}", "-f", f"q={query}"]
        if after:
            args += ["-f", f"after={after}"]
        try:
            raw: Any = json.loads(run_gh(args))
            search: Any = raw["data"]["search"]
            prs.extend(map_node_to_open_pr(node, config, known) for node in search["nodes"] if node)
        except (ValueError, KeyError, TypeError) as exc:
            raise GitHubError(f"unexpected answer from GitHub: {type(exc).__name__}") from exc
        if not search["pageInfo"]["hasNextPage"]:
            return prs
        after = str(search["pageInfo"]["endCursor"])
    raise GitHubError(f"more than {_PAGES_MAX} pages of open PRs")


def map_node_to_open_pr(node: Any, config: Config, known: KnownHeads | None = None) -> OpenPr:
    repo = str(node["repository"]["nameWithOwner"])
    number = int(node["number"])
    ref = f"pr:{repo}#{number}"
    head_oid = str(node["headRefOid"])
    files: Any = node["files"]
    # Listing a large PR's files costs REST calls, and one head on one base has one line count and one list of files.
    head_key = (ref, head_oid, str(node["baseRefName"]))
    known_head = (known or {}).get(head_key) if files["pageInfo"]["hasNextPage"] else None
    if known_head is None:
        rows = fetch_all_files(repo, number) if files["pageInfo"]["hasNextPage"] else list(files["nodes"])
        loc = map_files_to_loc(int(node["additions"]), int(node["deletions"]), rows, list_loc_excluded(config))
        paths = tuple(str(row["path"]) for row in rows)
    else:
        loc, paths = known_head
    commits = [n["commit"] for n in node["commits"]["nodes"]]
    last: Any = commits[-1] if commits else {}
    base_ref: Any = node.get("baseRef") or {}
    parents: Any = base_ref.get("associatedPullRequests", {}).get("nodes") or []
    rollup: Any = last.get("statusCheckRollup") or {}
    review_threads: Any = node.get("reviewThreads") or {}
    threads: list[Any] = review_threads.get("nodes") or []
    body = str(node.get("body") or "")
    dependencies = map_markdown_to_dependencies(body, repo, number)
    label_nodes: Any = node.get("labels") or {}
    labels: list[Any] = label_nodes.get("nodes") or []
    return OpenPr(
        ref=ref,
        repo=repo,
        number=number,
        title=strip_agent_prefix(str(node["title"])),
        url=str(node["url"]),
        draft=bool(node["isDraft"]),
        head=str(node["headRefName"]),
        head_oid=head_oid,
        base=str(node["baseRefName"]),
        stack_parent=f"pr:{repo}#{parents[0]['number']}" if parents else None,
        loc=loc,
        files=paths,
        checks=rollup.get("state"),
        requested=count_real_review_requests(node["reviewRequests"]["nodes"], config),
        approved_by=map_reviews_to_approvers(node["reviews"]["nodes"], config),
        bot_approved=is_approved_by_review_bot(node["reviews"]["nodes"], config),
        bot_cleared=is_cleared_by_review_bot(
            node["reviews"]["nodes"], head_oid, config, map_commits_to_status_contexts(node.get("headStatuses"))
        ),
        bot_rounds=count_review_bot_rounds(node["reviews"]["nodes"], config),
        body_reviews=map_markdown_to_reviews(str(node.get("body") or ""), config.review_names),
        usage_missing=find_missing_usage_section(strip_agent_prefix(str(node["title"])), str(node.get("body") or "")),
        depends_on=dependencies[0][0] if len(dependencies) == 1 else None,
        dependencies=dependencies,
        goes_with=map_markdown_to_goes_with(body, repo, number),
        related_prs=map_markdown_to_related_prs(body, repo, number),
        must_merge=map_pr_to_must_merge(config, repo, number, node, tuple(ref for ref, _ in dependencies)),
        preconditions=map_markdown_to_preconditions(body, repo, number),
        persist_sandbox=PERSIST_SANDBOX in body,
        issue_missing=needs_issue(config, repo)
        and not names_issue(config.issue_prefixes, str(node["headRefName"]), str(node["title"]), body),
        others_reviewed=has_review_by_others(node["latestReviews"]["nodes"], config),
        merge_state=node.get("mergeStateStatus"),
        in_merge_queue=bool(node.get("isInMergeQueue")),
        commits=tuple((str(c["oid"]), map_iso_to_ms(c.get("committedDate")) or 0) for c in commits),
        updated_at=str(node["updatedAt"]),
        open_threads=sum(1 for thread in threads if not thread.get("isResolved")),
        thread_flags="".join("r" if thread.get("isResolved") else "o" for thread in threads),
        labels=tuple(str(n["name"]) for n in labels),
    )


def map_pr_to_must_merge(
    config: Config, repo: str, number: int, node: Any, merges_after: tuple[str, ...]
) -> tuple[str, ...]:
    # Loaded here: the backport module reads GitHub through this one.
    from bridge.backport import find_named_prs, is_backport_base

    must = set(merges_after)
    if is_backport_base(config.backport_bases, str(node["baseRefName"])):
        named = find_named_prs(repo, number, str(node["title"]), str(node.get("body") or ""), str(node["headRefName"]))
        must.update(f"{repo}#{n}" for n in named)
    return tuple(sorted(must))


def fetch_pr_states(refs: list[str]) -> dict[str, str | None]:
    """owner/repo#N to OPEN, MERGED or CLOSED, ISSUE or NONE when the number is an issue or nothing at all, or None
    when it could not be read. MERGED and ISSUE are kept for the process's life: a body that names many merged PRs
    costs one read each."""
    states: dict[str, str | None] = {r: _FINAL_STATES[r.casefold()] for r in refs if r.casefold() in _FINAL_STATES}
    by_repo: dict[str, list[tuple[str, int]]] = {}
    for ref in refs:
        repo, _, number = ref.partition("#")
        if ref not in states and "/" in repo and number.isdigit():
            by_repo.setdefault(repo.casefold(), []).append((ref, int(number)))
    if not by_repo:
        return {**dict.fromkeys(refs), **states}
    parts: list[str] = []
    aliases: dict[tuple[str, str], str] = {}
    for r, (repo, items) in enumerate(by_repo.items()):
        owner, name = repo.split("/", 1)
        fields: list[str] = []
        for p, (ref, number) in enumerate(items):
            aliases[(f"r{r}", f"p{p}")] = ref
            fields.append(f"p{p}: issueOrPullRequest(number: {number}) {{ __typename ... on PullRequest {{ state }} }}")
        parts.append(f"r{r}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{ {' '.join(fields)} }}")
    try:
        # A number that is neither fails its own field only, and gh then exits 1 with the rest of the answer.
        done = run_gh_process(["api", "graphql", "-f", f"query=query {{ {' '.join(parts)} }}"], _GH_TIMEOUT_S)
        answer: Any = json.loads(done.stdout)
        data: Any = answer.get("data") or {}
        errors: list[Any] = answer.get("errors") or []
        # No such number there, or no repository GitHub shows us (a word before '#', like a decision 'Q#12', or a repo
        # we cannot see, whose merge could never be confirmed): either way the text names no PR to wait for.
        missing: set[str] = {".".join(map(str, e["path"])) for e in errors if e.get("type") == "NOT_FOUND"}
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError, KeyError, TypeError):
        data, missing = {}, set()
    for (repo_alias, pr_alias), ref in aliases.items():
        state: object = None
        try:
            repository: Any = data[repo_alias]
            node: Any = None if repository is None else repository[pr_alias]
            if node is None and {f"{repo_alias}.{pr_alias}", repo_alias} & missing:
                state = NO_SUCH_NUMBER
            elif node is not None:
                state = "ISSUE" if node["__typename"] == "Issue" else node["state"]
        except (KeyError, TypeError):
            state = None
        # A closed PR can be reopened and a missing number is the next one opened; merged and an issue stay so.
        if state in ("MERGED", "ISSUE"):
            _FINAL_STATES[ref.casefold()] = str(state)
        states[ref] = str(state) if state in ("OPEN", "MERGED", "CLOSED", "ISSUE", NO_SUCH_NUMBER) else None
    return {**dict.fromkeys(refs), **states}


def fetch_review_threads(config: Config, repo: str, number: int) -> ReviewThreads:
    owner, _, name = repo.partition("/")
    args = ["api", "graphql", "-f", f"query={_THREADS_QUERY}", "-f", f"owner={owner}", "-f", f"name={name}"]
    try:
        raw: Any = json.loads(run_gh([*args, "-F", f"number={number}"]))
        nodes: list[Any] = raw["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    except (ValueError, KeyError, TypeError) as exc:
        raise GitHubError(f"unexpected answer from GitHub: {type(exc).__name__}") from exc
    return map_threads_to_review_threads(nodes, config.review_author, config.review_bot_logins)


class ChecksRunning(Exception):
    """Every failed check so far is a job GitHub refused to start, but another check still runs."""


@dataclass(frozen=True)
class LiveMergeFacts:
    draft: bool
    approvers: tuple[str, ...]
    """People whose latest verdict is APPROVED; never the review bot."""
    bot_approved: bool
    """The review bot approved, and its latest review is of the head."""
    bot_cleared: bool
    checks: str | None
    merge_state: str | None
    his_threads: int
    """The author's unresolved threads."""
    head_oid: str | None
    base: str | None
    default_branch: str | None
    colleague_threads: tuple[str, ...] = ()
    """Who started each colleague's thread still waiting on our side."""
    colleague_comments: tuple[str, ...] = ()
    """Who wrote each colleague's comment still waiting on our side."""
    head: str = ""
    title: str = ""
    body: str = ""


def fetch_merge_facts(
    config: Config, repo: str, number: int, timeout_s: float = _MERGE_FACTS_TIMEOUT_S
) -> tuple[bool, bool, str | None, str | None, int, str | None, tuple[str, ...], tuple[str, ...]]:
    """(draft, approved, CI rollup, merge state, the author's unresolved threads, head commit, colleagues' threads
    and comments waiting on our side) of one PR, read live for the merge guard."""
    live = read_live_merge_facts(config, repo, number, timeout_s)
    approved = bool(live.approvers) or live.bot_approved
    return (
        live.draft,
        approved,
        live.checks,
        live.merge_state,
        live.his_threads,
        live.head_oid,
        live.colleague_threads,
        live.colleague_comments,
    )


def read_live_merge_facts(
    config: Config, repo: str, number: int, timeout_s: float = _MERGE_FACTS_TIMEOUT_S
) -> LiveMergeFacts:
    owner, _, name = repo.partition("/")
    args = ["api", "graphql", "-f", f"query={_MERGE_FACTS_QUERY}", "-f", f"owner={owner}", "-f", f"name={name}"]
    try:
        raw: Any = json.loads(run_gh([*args, "-F", f"number={number}"], timeout_s))
        repository: Any = raw["data"]["repository"]
        pr: Any = repository["pullRequest"]
        commits: list[Any] = pr["commits"]["nodes"]
        head: Any = commits[-1]["commit"] if commits else {}
        rollup: Any = head.get("statusCheckRollup") or {}
        reviews: list[Any] = pr["reviews"]["nodes"]
        author = config.review_author.casefold()
        head_oid = str(head.get("oid") or "")
        bot_cleared = is_cleared_by_review_bot(reviews, head_oid, config, map_rollup_to_status_contexts(rollup))
        threads: list[Any] = pr["reviewThreads"]["nodes"]
        default: Any = repository.get("defaultBranchRef") or {}
        return LiveMergeFacts(
            draft=bool(pr["isDraft"]),
            approvers=map_reviews_to_approvers(reviews, config),
            bot_approved=is_approved_by_review_bot(reviews, config) and bot_cleared,
            bot_cleared=bot_cleared,
            checks=rollup.get("state"),
            merge_state=pr.get("mergeStateStatus"),
            his_threads=sum(1 for t in threads if not t.get("isResolved") and _map_thread_to_starter(t) == author),
            head_oid=head_oid or None,
            colleague_threads=list_waiting_colleagues(threads, config.review_author, config.review_bot_logins),
            colleague_comments=list_unanswered_colleague_comments(
                [*map(_map_node_to_said, reviews), *map(_map_node_to_said, pr["comments"]["nodes"])],
                config.review_author,
                config.review_bot_logins,
            ),
            base=pr.get("baseRefName"),
            default_branch=default.get("name"),
            head=str(pr.get("headRefName") or ""),
            title=str(pr.get("title") or ""),
            body=str(pr.get("body") or ""),
        )
    except (ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
        raise GitHubError(f"unexpected answer from GitHub: {type(exc).__name__}") from exc


def fetch_ci_blocked_by(repo: str, oid: str) -> str | None:
    """Why GitHub itself kept CI from running at this commit, or None. Raises ChecksRunning."""
    owner, _, name = repo.partition("/")
    args = ["api", "graphql", "-f", f"query={_HEAD_CHECKS_QUERY}", "-f", f"owner={owner}", "-f", f"name={name}"]
    try:
        raw: Any = json.loads(run_gh([*args, "-f", f"oid={oid}"]))
        commit: Any = raw["data"]["repository"]["object"] or {}
        rollup: Any = commit.get("statusCheckRollup") or {}
        contexts: Any = rollup.get("contexts") or {}
        nodes: list[Any] = contexts.get("nodes") or []
        page: Any = contexts.get("pageInfo") or {}
        more = bool(page.get("hasNextPage"))
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise GitHubError(f"unexpected answer from GitHub: {type(exc).__name__}") from exc
    # A real failure may sit past the first page; the button then stays hidden as for any red CI.
    return None if more else map_checks_to_ci_blocked_by(nodes)


def map_checks_to_ci_blocked_by(nodes: list[Any]) -> str | None:
    """A reason only when every failed check is a job GitHub refused to start: one real failure means CI is red. While
    another check runs it raises ChecksRunning, as that check may still fail for real."""
    failed: list[Any] = [n for n in nodes if n and (n.get("conclusion") or n.get("state")) in _CHECK_FAILED]
    refusals: list[str] = []
    for node in failed:
        block: Any = node.get("annotations") or {}
        annotations: list[Any] = block.get("nodes") or []
        found = [str(a.get("message") or "").lower() for a in annotations if a]
        refusal = next((m for m in found if m.startswith(_NOT_STARTED)), None)
        if refusal is None:
            return None
        refusals.append(refusal)
    if not refusals:
        return None
    if any(_is_check_running(n) for n in nodes if n):
        raise ChecksRunning
    billing = all(any(words in m for words in _BILLING) for m in refusals)
    return CI_BLOCKED_BY_BILLING if billing else CI_BLOCKED_BY_GITHUB


def _is_check_running(node: Any) -> bool:
    if node.get("__typename") == "StatusContext":
        return node.get("state") in _STATUS_RUNNING
    return node.get("conclusion") is None


def map_threads_to_review_threads(nodes: list[Any], author: str, bots: Sequence[str] = ()) -> ReviewThreads:
    """A thread the owner's pending review opened is a draft only they see: it counts as neither open nor theirs."""
    sent = [t for t in nodes if t and not t.get("isResolved") and _map_thread_to_first_review_state(t) != PENDING]
    his = [t for t in sent if _map_thread_to_starter(t) == author.casefold()]
    return ReviewThreads(
        len(sent),
        tuple(map_thread_to_his_thread(t) for t in his),
        list_waiting_colleagues(sent, author, bots),
        tuple(map_thread_to_bot_thread(t, author) for t in sent if is_bot_must_fix_thread(t, bots)),
    )


def is_bot_must_fix_thread(thread: Any, bots: Sequence[str]) -> bool:
    connection: Any = thread.get("comments") or {}
    comments: list[Any] = connection.get("nodes") or []
    body = str((comments[0].get("body") if comments else None) or "").lstrip()
    return _map_thread_to_starter(thread) in {b.casefold() for b in bots} and body.startswith(BOT_MUST_FIX_MARKS)


def map_thread_to_bot_thread(thread: Any, author: str) -> BotThread:
    first_connection: Any = thread.get("comments") or {}
    first: list[Any] = first_connection.get("nodes") or [{}]
    last_connection: Any = thread.get("lastComment") or {}
    last: list[Any] = last_connection.get("nodes") or []
    node: Any = last[0] if last else {}
    review: Any = node.get("pullRequestReview") or {}
    last_author: Any = node.get("author") or {}
    opened = map_iso_to_ms(first[0].get("createdAt"))
    url = str(first[0].get("url") or "")
    if str(last_author.get("login") or "").casefold() != author.casefold():
        return BotThread(url, opened if int(last_connection.get("totalCount") or 1) <= 1 else None)
    return BotThread(url, opened if review.get("state") == PENDING else None)


@dataclass(frozen=True)
class Said:
    """A conversation comment or a review: what the colleague-comment rule weighs besides threads."""

    author: str
    at: int
    body: str
    state: str | None = None
    """A review's verdict; None on a comment."""
    by_app: bool = False


def list_unanswered_colleague_comments(said: Sequence[Said], author: str, bots: Sequence[str]) -> tuple[str, ...]:
    """The author of each conversation comment or review text by a person other than the owner's account and the
    review bot with no word from that account after it: a comment or a submitted review, which the owner and the
    agents share. The account cannot approve its own PR, so its review with no text carries inline replies. An
    approval's text is the colleague's verdict, not a request; inline comments count by their threads."""
    ours = author.casefold()
    ignored = {ours, *(b.casefold() for b in bots)}
    answered = [
        s.at
        for s in said
        if s.author.casefold() == ours and (s.body.strip() or s.state is not None) and s.state not in _UNSENT
    ]
    answered_at = max(answered, default=None)
    return tuple(
        s.author
        for s in sorted(said, key=lambda s: s.at)
        if s.author
        and s.author.casefold() not in ignored
        and not s.by_app
        and s.body.strip()
        and s.state not in (APPROVED_STATE, *_UNSENT)
        and (answered_at is None or answered_at <= s.at)
    )


def _map_node_to_said(node: Any) -> Said:
    author: Any = node.get("author") or {}
    return Said(
        author=str(author.get("login") or ""),
        at=map_iso_to_ms(node.get("submittedAt") or node.get("createdAt")) or 0,
        body=str(node.get("body") or ""),
        state=node.get("state"),
        by_app=author.get("__typename") == "Bot",
    )


def list_waiting_colleagues(nodes: list[Any], author: str, bots: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        login
        for t in nodes
        if t and not t.get("isResolved") and (login := map_thread_to_waiting_colleague(t, author, bots))
    )


def map_thread_to_waiting_colleague(thread: Any, author: str, bots: Sequence[str]) -> str | None:
    """The login of the person who started an unresolved thread whose last sent comment is not from the owner's
    account, which the owner and the agents share: their reply is our side's answer. None for a thread of the owner's
    account, the review bot's or an app's, which their own rules cover, and for one answered."""
    connection: Any = thread.get("comments") or {}
    comments: list[Any] = connection.get("nodes") or []
    starter: Any = (comments[0].get("author") if comments else None) or {}
    login = str(starter.get("login") or "")
    ours = author.casefold()
    if not login or starter.get("__typename") == "Bot" or login.casefold() in {ours, *(b.casefold() for b in bots)}:
        return None
    last_connection: Any = thread.get("lastComment") or {}
    last: list[Any] = last_connection.get("nodes") or []
    node: Any = last[0] if last else {}
    review: Any = node.get("pullRequestReview") or {}
    last_author: Any = node.get("author") or {}
    answered = str(last_author.get("login") or "").casefold() == ours and review.get("state") != PENDING
    return None if answered else login


def _map_thread_to_first_review_state(thread: Any) -> str | None:
    connection: Any = thread.get("comments") or {}
    comments: list[Any] = connection.get("nodes") or []
    review: Any = (comments[0].get("pullRequestReview") if comments else None) or {}
    return review.get("state")


def map_thread_to_his_thread(thread: Any) -> HisThread:
    first_connection: Any = thread.get("comments") or {}
    first: list[Any] = first_connection.get("nodes") or []
    last_connection: Any = thread.get("lastComment") or {}
    last: list[Any] = last_connection.get("nodes") or []
    node: Any = last[0] if last else {}
    review: Any = node.get("pullRequestReview") or {}
    # The owner's unsent reply is no answer, and its words stay theirs until they submit them.
    if review.get("state") == PENDING:
        node = {"author": node.get("author")}
    author: Any = node.get("author") or {}
    replies = max(int(last_connection.get("totalCount") or 0) - 1, 0)
    return HisThread(
        url=str(first[0].get("url") or "") if first else "",
        replies=replies,
        last_id=f"p{node['databaseId']}" if replies and node.get("databaseId") else None,
        last_author=str(author.get("login") or ""),
        last_body=str(node.get("body") or ""),
    )


def _map_thread_to_starter(thread: Any) -> str:
    connection: Any = thread.get("comments") or {}
    comments: list[Any] = connection.get("nodes") or []
    author: Any = (comments[0].get("author") if comments else None) or {}
    return str(author.get("login") or "").casefold()


def map_files_to_loc(additions: int, deletions: int, files: list[Any], excluded: tuple[str, ...]) -> int:
    """Lines of code: docs and assets are left out with the configured files, so a PR with no code counts 0 and gets
    no document or review worktree."""
    loc = additions + deletions
    for row in files:
        if is_loc_excluded(str(row["path"]), excluded):
            loc -= int(row["additions"]) + int(row["deletions"])
    return max(loc, 0)


def is_loc_excluded(path: str, excluded: tuple[str, ...]) -> bool:
    name = PurePosixPath(path).name.lower()
    return any(fnmatch.fnmatch(name, pattern.lower()) for pattern in (*excluded, *DOCS_AND_ASSETS))


COMPARE_FILES_MAX = 300
"""GitHub's compare lists at most this many files: one more may be missing, so such a diff is never only comments."""
_HASH_COMMENTED = frozenset({
    ".py", ".pyi", ".sh", ".bash", ".zsh", ".rb", ".pl", ".r", ".yaml", ".yml", ".toml", ".tf", ".tfvars", ".hcl",
    ".cfg", ".conf", ".mk", ".dockerfile", "dockerfile", "makefile",
})  # fmt: skip
_SLASH_COMMENTED = frozenset({
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".java", ".kt", ".kts", ".scala", ".c", ".h", ".cc", ".cpp",
    ".hpp", ".cs", ".rs", ".swift", ".php", ".dart", ".css", ".scss", ".less",
})  # fmt: skip
_DASH_COMMENTED = frozenset({".sql", ".lua", ".hs"})


@dataclass(frozen=True)
class DocDiff:
    """What changed between the commit a review doc describes and the PR's head."""

    commits: int | None
    """Commits since the doc's; None when the head does not descend from it (a rebase or force-push) or GitHub
    could not say."""
    lines: int | None
    """Changed lines other than comments, blank lines and docs; None as commits."""
    cosmetic: bool
    """Every change is a comment, a blank line or a docs file: the doc still describes the code."""


UNKNOWN_DIFF = DocDiff(None, None, False)


def is_docs_file(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return any(fnmatch.fnmatch(name, pattern) for pattern in DOCS_AND_ASSETS)


def is_comment_line(path: str, line: str) -> bool:
    """A whole line that is blank or a comment in its file's language; a file of an unknown language has none, so a
    change there makes the doc stale. A line alone cannot tell a docstring, or a # inside a multi-line string or a
    heredoc, from its surroundings: a docstring counts as code, those two as comments. A block comment's middle line
    (" * text") counts as code, as in C it can be a dereference."""
    text = line.strip()
    if not text:
        return True
    name = PurePosixPath(path).name.lower()
    kind = PurePosixPath(name).suffix or name
    if kind in _HASH_COMMENTED:
        return text.startswith("#")
    if kind in _SLASH_COMMENTED:
        if text.startswith("/*"):
            return "*/" not in text or (text.endswith("*/") and text.count("*/") == 1)
        return text.startswith("//") or text in ("*", "*/")
    if kind in _DASH_COMMENTED:
        return text.startswith("--")
    return False


def count_code_lines_in_patch(path: str, patch: str) -> int:
    """Added and removed lines of a GitHub file patch, hunks only with no file header, that are not comments or
    blank."""
    return sum(1 for line in patch.splitlines() if line[:1] in ("+", "-") and not is_comment_line(path, line[1:]))


def map_compare_to_doc_diff(raw: Any, own_files: set[str] | None = None, own_commits: int | None = None) -> DocDiff:
    """From GitHub's compare of the doc's commit with the head. A file GitHub gives no patch for (binary, too large)
    counts in full and is never only comments; a file renamed from or to code is never only comments; a list GitHub
    cut short has no line count. With the PR's own files and its own commits since the doc, a merge of the base
    counts for nothing: only what the PR itself changes is the doc's business."""
    status = raw.get("status")
    if status == "identical":
        return DocDiff(0, 0, True)
    if status != "ahead":
        return UNKNOWN_DIFF
    files: list[Any] = raw.get("files") or []
    lines = 0
    complete = len(files) < COMPARE_FILES_MAX
    for row in files:
        path = str(row.get("filename") or "")
        previous = str(row.get("previous_filename") or path)
        if own_files is not None and path not in own_files and previous not in own_files:
            continue
        if row.get("status") == "renamed" and not (is_docs_file(path) and is_docs_file(previous)):
            complete = False
        if is_docs_file(path) and is_docs_file(previous):
            continue
        patch = row.get("patch")
        if isinstance(patch, str):
            lines += count_code_lines_in_patch(path, patch)
        else:
            complete = False
            lines += int(row.get("additions") or 0) + int(row.get("deletions") or 0)
    cut = len(files) >= COMPARE_FILES_MAX
    commits = own_commits if own_commits is not None else int(raw.get("ahead_by") or 0)
    return DocDiff(commits, None if cut else lines, complete and lines == 0)


def count_own_commits_since(commits: list[Any], doc_commit: str) -> int | None:
    """The PR's own commits after the doc's, merges left out; None when the doc's commit is not among them."""
    shas = [str(c.get("sha") or "") for c in commits]
    at = next((i for i, sha in enumerate(shas) if sha.startswith(doc_commit)), None)
    if at is None:
        return None
    return sum(1 for c in commits[at + 1 :] if len(c.get("parents") or []) < 2)


def fetch_doc_diff(repo: str, doc_commit: str, head: str, number: int | None = None) -> DocDiff:
    try:
        raw: Any = json.loads(run_gh(["api", f"repos/{repo}/compare/{doc_commit}...{head}"]))
        if number is None:
            return map_compare_to_doc_diff(raw)
        pages: Any = json.loads(
            run_gh(["api", "--paginate", "--slurp", f"repos/{repo}/pulls/{number}/commits?per_page=100"])
        )
        own = count_own_commits_since([c for page in pages for c in page], doc_commit)
        files = {str(f["path"]) for f in fetch_all_files(repo, number)}
        return map_compare_to_doc_diff(raw, files, own)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise GitHubError(f"unexpected answer from GitHub: {type(exc).__name__}") from exc


def fetch_all_files(repo: str, number: int) -> list[Any]:
    pages: Any = json.loads(run_gh(["api", "--paginate", "--slurp", f"repos/{repo}/pulls/{number}/files?per_page=100"]))
    return [
        {"path": f["filename"], "additions": f["additions"], "deletions": f["deletions"]}
        for page in pages
        for f in page
    ]


def count_real_review_requests(nodes: list[Any], config: Config) -> int:
    count = 0
    for node in nodes:
        reviewer: Any = node.get("requestedReviewer") or {}
        if reviewer.get("__typename") != "Bot" and reviewer.get("login") not in config.review_bot_logins:
            count += 1
    return count


def has_review_by_others(nodes: list[Any], config: Config) -> bool:
    """From latestReviews, one per reviewer: `reviews` is cut at 100, and bots can fill that. Logins ignore case."""
    ignored = {login.casefold() for login in (config.review_author, *config.review_bot_logins)}
    for node in nodes:
        author: Any = node.get("author") or {}
        login = str(author.get("login") or "")
        if login and author.get("__typename") != "Bot" and login.casefold() not in ignored:
            return True
    return False


def is_approved_by_review_bot(nodes: list[Any], config: Config) -> bool:
    """Whether a review bot's latest verdict is APPROVED; a comment changes no verdict, as for people."""
    bots = {login.casefold() for login in config.review_bot_logins}
    verdicts: dict[str, str] = {}
    for node in nodes:
        author: Any = node.get("author") or {}
        login = str(author.get("login") or "").casefold()
        if login in bots and node.get("state") != "COMMENTED":
            verdicts[login] = str(node.get("state"))
    return APPROVED_STATE in verdicts.values()


def map_rollup_to_status_contexts(rollup: Any) -> list[Any]:
    nodes: list[Any] = []
    if rollup and rollup.get("contexts"):
        nodes = rollup["contexts"].get("nodes") or []
    return [n for n in nodes if n and n.get("context")]


def map_commits_to_status_contexts(commits: Any) -> list[Any]:
    nodes: list[Any] = []
    if commits:
        nodes = commits.get("nodes") or []
    if not nodes or not nodes[-1].get("commit"):
        return []
    return map_rollup_to_status_contexts(nodes[-1]["commit"].get("statusCheckRollup"))


def is_cleared_by_review_bot(nodes: list[Any], head_oid: str, config: Config, statuses: Sequence[Any] = ()) -> bool:
    """Whether the review bot cleared the head: its latest review, of any kind, is of the head and does not request
    changes; or, with no review of the head, the head's status named like the bot is success."""
    bots = {login.casefold() for login in config.review_bot_logins}
    latest: Any = None
    for node in nodes:
        author: Any = node.get("author") or {}
        if str(author.get("login") or "").casefold() in bots:
            latest = node
    if latest is not None:
        commit: Any = latest.get("commit") or {}
        if commit.get("oid") == head_oid:
            return latest.get("state") != "CHANGES_REQUESTED"
    name = config.review_bot_name.casefold()
    return any(str(s.get("context") or "").casefold() == name and s.get("state") == "SUCCESS" for s in statuses)


def count_review_bot_rounds(nodes: list[Any], config: Config) -> int:
    bots = {login.casefold() for login in config.review_bot_logins}
    commits: set[str] = set()
    for node in nodes:
        author: Any = node.get("author") or {}
        commit: Any = node.get("commit") or {}
        if str(author.get("login") or "").casefold() in bots and node.get("state") != PENDING:
            commits.add(str(commit.get("oid")))
    return len(commits)


def map_reviews_to_approvers(nodes: list[Any], config: Config) -> tuple[str, ...]:
    """People whose latest verdict is APPROVED. Not GitHub's reviewDecision, which a bot can set by itself.
    A comment changes nobody's verdict; a dismissal or a request for changes replaces it."""
    ignored = {config.review_author, *config.review_bot_logins}
    verdicts: dict[str, str] = {}
    for node in nodes:
        author: Any = node.get("author") or {}
        login = author.get("login")
        if login and author.get("__typename") != "Bot" and login not in ignored and node.get("state") != "COMMENTED":
            verdicts[str(login)] = str(node.get("state"))
    return tuple(sorted(login for login, verdict in verdicts.items() if verdict == "APPROVED"))


def fetch_comments(repo: str, number: int) -> list[FetchedComment]:
    """Everything that can carry anyone's words or verdict: inline comments, conversation comments and reviews."""
    inline = _fetch_pages(f"repos/{repo}/pulls/{number}/comments?per_page=100")
    authors = {int(c["id"]): _map_row_to_author(c) for c in inline}
    comments: list[FetchedComment] = []
    for row in inline:
        parent = row.get("in_reply_to_id")
        comments.append(
            _map_row_to_comment(
                row, f"p{row['id']}", f"p{parent}" if parent else None, authors.get(int(parent)) if parent else None
            )
        )
    for row in _fetch_pages(f"repos/{repo}/issues/{number}/comments?per_page=100"):
        comments.append(_map_row_to_comment(row, f"i{row['id']}"))
    for row in _fetch_pages(f"repos/{repo}/pulls/{number}/reviews?per_page=100"):
        if row.get("submitted_at"):
            comments.append(
                _map_row_to_comment({**row, "created_at": row["submitted_at"]}, f"r{row['id']}", state=row.get("state"))
            )
    return comments


def _map_row_to_author(row: Any) -> str:
    user: Any = row.get("user") or {}
    return str(user.get("login") or "")


def _fetch_pages(path: str) -> list[Any]:
    try:
        pages: object = json.loads(run_gh(["api", "--paginate", "--slurp", path]))
    except ValueError as exc:
        raise GitHubError(f"unexpected answer from GitHub for {path}") from exc
    if not isinstance(pages, list):
        raise GitHubError(f"unexpected answer from GitHub for {path}")
    return [row for page in cast(list[Any], pages) for row in page]


def _map_row_to_comment(
    row: Any,
    comment_id: str,
    parent_id: str | None = None,
    parent_author: str | None = None,
    state: str | None = None,
) -> FetchedComment:
    user: Any = row.get("user") or {}
    review = row.get("pull_request_review_id")
    return FetchedComment(
        comment_id=comment_id,
        author=_map_row_to_author(row),
        created_at=map_iso_to_ms(row.get("created_at")) or 0,
        body=str(row.get("body") or ""),
        url=str(row.get("html_url") or ""),
        parent_id=parent_id,
        parent_author=parent_author,
        state=state,
        commit_id=row.get("commit_id"),
        by_app=user.get("type") == "Bot",
        review_id=f"r{review}" if review else None,
    )
