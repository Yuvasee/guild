import fnmatch
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, cast

from bridge.agent_prefix import strip_agent_prefix
from bridge.config import Config
from bridge.review_deps import map_markdown_to_dependency
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
      files(first: 100) { pageInfo { hasNextPage } nodes { path additions deletions } }
      reviewRequests(first: 20) { nodes { requestedReviewer {
        __typename ... on User { login } ... on Team { slug } ... on Bot { login } } } }
      reviews(last: 100) { nodes { state commit { oid } author { __typename login } } }
      latestReviews(first: 100) { nodes { author { __typename login } } }
      reviewThreads(first: 100) { nodes { isResolved } }
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
      comments(first: 1) { nodes { author { login } url pullRequestReview { state } } }
      lastComment: comments(last: 1) { totalCount nodes {
        databaseId author { login } body pullRequestReview { state } } } } }
  } }
}
"""
_MERGE_FACTS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) {
    isDraft mergeStateStatus
    commits(last: 1) { nodes { commit { oid statusCheckRollup { state } } } }
    reviews(last: 100) { nodes { state commit { oid } author { __typename login } } }
    reviewThreads(first: 100) { nodes { isResolved comments(first: 1) { nodes { author { login } } } } }
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
REVIEW_LOCKFILES = ("uv.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock")
APPROVED_STATE = "APPROVED"


def list_loc_excluded(config: Config) -> tuple[str, ...]:
    """File names left out of a PR's line count (fnmatch on the base name)."""
    return (*REVIEW_LOCKFILES, *config.review_generated_files)


DOCS_AND_ASSETS = (
    "*.md", "*.mdx", "*.markdown", "*.rst",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.svg", "*.ico", "*.pdf",
)  # fmt: skip


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
class ReviewThreads:
    open: int
    his: tuple[HisThread, ...]


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
    """owner/repo#N of the PR its body says it merges only after."""

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


def run_gh(args: list[str], timeout_s: float = _GH_TIMEOUT_S) -> str:
    try:
        done = subprocess.run(
            ["gh", *args], capture_output=True, encoding="utf-8", errors="replace", timeout=timeout_s, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitHubError(f"gh failed: {type(exc).__name__}") from exc
    if done.returncode != 0:
        raise GitHubError(f"gh failed: {(done.stderr.strip().splitlines() or ['no output'])[-1][:200]}")
    return done.stdout


def fetch_open_prs(config: Config, known_loc: dict[tuple[str, str], int] | None = None) -> list[OpenPr]:
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
            prs.extend(map_node_to_open_pr(node, config, known_loc) for node in search["nodes"] if node)
        except (ValueError, KeyError, TypeError) as exc:
            raise GitHubError(f"unexpected answer from GitHub: {type(exc).__name__}") from exc
        if not search["pageInfo"]["hasNextPage"]:
            return prs
        after = str(search["pageInfo"]["endCursor"])
    raise GitHubError(f"more than {_PAGES_MAX} pages of open PRs")


def map_node_to_open_pr(node: Any, config: Config, known_loc: dict[tuple[str, str], int] | None = None) -> OpenPr:
    repo = str(node["repository"]["nameWithOwner"])
    number = int(node["number"])
    ref = f"pr:{repo}#{number}"
    head_oid = str(node["headRefOid"])
    files: Any = node["files"]
    # Listing a large PR's files costs REST calls, and one head has one line count.
    loc = (known_loc or {}).get((ref, head_oid)) if files["pageInfo"]["hasNextPage"] else None
    if loc is None:
        rows = fetch_all_files(repo, number) if files["pageInfo"]["hasNextPage"] else list(files["nodes"])
        loc = map_files_to_loc(int(node["additions"]), int(node["deletions"]), rows, list_loc_excluded(config))
    commits = [n["commit"] for n in node["commits"]["nodes"]]
    last: Any = commits[-1] if commits else {}
    base_ref: Any = node.get("baseRef") or {}
    parents: Any = base_ref.get("associatedPullRequests", {}).get("nodes") or []
    rollup: Any = last.get("statusCheckRollup") or {}
    review_threads: Any = node.get("reviewThreads") or {}
    threads: list[Any] = review_threads.get("nodes") or []
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
        checks=rollup.get("state"),
        requested=count_real_review_requests(node["reviewRequests"]["nodes"], config),
        approved_by=map_reviews_to_approvers(node["reviews"]["nodes"], config),
        bot_approved=is_approved_by_review_bot(node["reviews"]["nodes"], config),
        bot_cleared=is_cleared_by_review_bot(node["reviews"]["nodes"], head_oid, config),
        body_reviews=map_markdown_to_reviews(str(node.get("body") or ""), config.review_names),
        usage_missing=find_missing_usage_section(strip_agent_prefix(str(node["title"])), str(node.get("body") or "")),
        depends_on=map_markdown_to_dependency(str(node.get("body") or ""), repo, number),
        others_reviewed=has_review_by_others(node["latestReviews"]["nodes"], config),
        merge_state=node.get("mergeStateStatus"),
        in_merge_queue=bool(node.get("isInMergeQueue")),
        commits=tuple((str(c["oid"]), map_iso_to_ms(c.get("committedDate")) or 0) for c in commits),
        updated_at=str(node["updatedAt"]),
        open_threads=sum(1 for thread in threads if not thread.get("isResolved")),
        thread_flags="".join("r" if thread.get("isResolved") else "o" for thread in threads),
    )


def fetch_review_threads(config: Config, repo: str, number: int) -> ReviewThreads:
    owner, _, name = repo.partition("/")
    args = ["api", "graphql", "-f", f"query={_THREADS_QUERY}", "-f", f"owner={owner}", "-f", f"name={name}"]
    try:
        raw: Any = json.loads(run_gh([*args, "-F", f"number={number}"]))
        nodes: list[Any] = raw["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    except (ValueError, KeyError, TypeError) as exc:
        raise GitHubError(f"unexpected answer from GitHub: {type(exc).__name__}") from exc
    return map_threads_to_review_threads(nodes, config.review_author)


class ChecksRunning(Exception):
    """Every failed check so far is a job GitHub refused to start, but another check still runs."""


def fetch_merge_facts(
    config: Config, repo: str, number: int, timeout_s: float = _MERGE_FACTS_TIMEOUT_S
) -> tuple[bool, bool, str | None, str | None, int, str | None]:
    """(draft, approved, CI rollup, merge state, the author's unresolved threads, head commit) of one PR, read live for
    the merge guard."""
    owner, _, name = repo.partition("/")
    args = ["api", "graphql", "-f", f"query={_MERGE_FACTS_QUERY}", "-f", f"owner={owner}", "-f", f"name={name}"]
    try:
        raw: Any = json.loads(run_gh([*args, "-F", f"number={number}"], timeout_s))
        pr: Any = raw["data"]["repository"]["pullRequest"]
        commits: list[Any] = pr["commits"]["nodes"]
        head: Any = commits[-1]["commit"] if commits else {}
        rollup: Any = head.get("statusCheckRollup") or {}
        reviews: list[Any] = pr["reviews"]["nodes"]
        author = config.review_author.casefold()
        head_oid = str(head.get("oid") or "")
        bot = is_approved_by_review_bot(reviews, config) and is_cleared_by_review_bot(reviews, head_oid, config)
        approved = bool(map_reviews_to_approvers(reviews, config)) or bot
        threads: list[Any] = pr["reviewThreads"]["nodes"]
        his = sum(1 for t in threads if not t.get("isResolved") and _map_thread_to_starter(t) == author)
        return bool(pr["isDraft"]), approved, rollup.get("state"), pr.get("mergeStateStatus"), his, head_oid or None
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


def map_threads_to_review_threads(nodes: list[Any], author: str) -> ReviewThreads:
    """A thread the owner's pending review opened is a draft only they see: it counts as neither open nor theirs."""
    sent = [t for t in nodes if t and not t.get("isResolved") and _map_thread_to_first_review_state(t) != PENDING]
    his = [t for t in sent if _map_thread_to_starter(t) == author.casefold()]
    return ReviewThreads(len(sent), tuple(map_thread_to_his_thread(t) for t in his))


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
        name = PurePosixPath(str(row["path"])).name.lower()
        if any(fnmatch.fnmatch(name, pattern.lower()) for pattern in (*excluded, *DOCS_AND_ASSETS)):
            loc -= int(row["additions"]) + int(row["deletions"])
    return max(loc, 0)


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


def map_compare_to_doc_diff(raw: Any) -> DocDiff:
    """From GitHub's compare of the doc's commit with the head. A file GitHub gives no patch for (binary, too large)
    counts in full and is never only comments; a file renamed from or to code is never only comments; a list GitHub
    cut short has no line count."""
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
    return DocDiff(int(raw.get("ahead_by") or 0), None if cut else lines, complete and lines == 0)


def fetch_doc_diff(repo: str, doc_commit: str, head: str) -> DocDiff:
    try:
        raw: Any = json.loads(run_gh(["api", f"repos/{repo}/compare/{doc_commit}...{head}"]))
        return map_compare_to_doc_diff(raw)
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


def is_cleared_by_review_bot(nodes: list[Any], head_oid: str, config: Config) -> bool:
    """Whether a review bot's latest review, of any kind, is of the head commit and does not request changes."""
    bots = {login.casefold() for login in config.review_bot_logins}
    latest: Any = None
    for node in nodes:
        author: Any = node.get("author") or {}
        if str(author.get("login") or "").casefold() in bots:
            latest = node
    if latest is None:
        return False
    commit: Any = latest.get("commit") or {}
    return commit.get("oid") == head_oid and latest.get("state") != "CHANGES_REQUESTED"


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
