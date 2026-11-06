import fnmatch
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, cast

from bridge.config import Config
from bridge.review_deps import map_markdown_to_dependency
from bridge.review_marks import map_markdown_to_reviews

_PAGES_MAX = 10
_GH_TIMEOUT_S = 60
_SEARCH_QUERY = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 50, after: $after) {
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
      reviews(last: 100) { nodes { state author { __typename login } } }
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
    commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
    reviews(last: 100) { nodes { state author { __typename login } } }
    reviewThreads(first: 100) { nodes { isResolved comments(first: 1) { nodes { author { login } } } } }
  } }
}
"""
# The merge guard runs inside a 5 s hook: a read cut short must still end in its refusal.
_MERGE_FACTS_TIMEOUT_S = 3
PENDING = "PENDING"
APPROVED_STATE = "APPROVED"
DOCS_AND_ASSETS = (
    "*.md", "*.mdx", "*.markdown", "*.rst",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.svg", "*.ico", "*.pdf",
)  # fmt: skip


class GitHubError(Exception):
    pass


@dataclass(frozen=True)
class OwnerThread:
    url: str
    replies: int
    last_id: str | None
    last_author: str
    last_body: str


@dataclass(frozen=True)
class ReviewThreads:
    open: int
    theirs: tuple[OwnerThread, ...]


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
    """The review bot's latest verdict is APPROVED: GitHub-approved, though no person has seen the PR."""
    body_reviews: tuple[str, ...] | None = None
    """The reviews its body's Reviews section names; None without one."""
    others_reviewed: bool = False
    """A person other than the author reviewed, in any state. GitHub drops a request once its reviewer reviews."""
    merge_state: str | None = None
    in_merge_queue: bool = False
    commits: tuple[tuple[str, int], ...] = ()
    """(oid, committer time), oldest first."""
    updated_at: str = ""
    open_threads: int = 0
    """Unresolved threads, a thread of their pending review among them: GitHub lists it before it is sent."""
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
        loc = map_files_to_loc(int(node["additions"]), int(node["deletions"]), rows, config.review_loc_excluded)
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
        title=str(node["title"]),
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
        body_reviews=map_markdown_to_reviews(str(node.get("body") or "")),
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


def fetch_merge_facts(config: Config, repo: str, number: int) -> tuple[bool, bool, str | None, str | None, int]:
    """(draft, approved, CI rollup, merge state, the author's unresolved threads) of one PR, read live for the merge
    guard."""
    owner, _, name = repo.partition("/")
    args = ["api", "graphql", "-f", f"query={_MERGE_FACTS_QUERY}", "-f", f"owner={owner}", "-f", f"name={name}"]
    try:
        raw: Any = json.loads(run_gh([*args, "-F", f"number={number}"], _MERGE_FACTS_TIMEOUT_S))
        pr: Any = raw["data"]["repository"]["pullRequest"]
        commits: list[Any] = pr["commits"]["nodes"]
        rollup: Any = (commits[-1]["commit"].get("statusCheckRollup") if commits else None) or {}
        reviews: list[Any] = pr["reviews"]["nodes"]
        author = config.review_author.casefold()
        approved = bool(map_reviews_to_approvers(reviews, config)) or is_approved_by_review_bot(reviews, config)
        threads: list[Any] = pr["reviewThreads"]["nodes"]
        theirs = sum(1 for t in threads if not t.get("isResolved") and _map_thread_to_starter(t) == author)
        return bool(pr["isDraft"]), approved, rollup.get("state"), pr.get("mergeStateStatus"), theirs
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        raise GitHubError(f"unexpected answer from GitHub: {type(exc).__name__}") from exc


def map_threads_to_review_threads(nodes: list[Any], author: str) -> ReviewThreads:
    """A thread their pending review opened is a draft only they see: it counts as neither open nor theirs."""
    sent = [t for t in nodes if t and not t.get("isResolved") and _map_thread_to_first_review_state(t) != PENDING]
    theirs = [t for t in sent if _map_thread_to_starter(t) == author.casefold()]
    return ReviewThreads(len(sent), tuple(map_thread_to_owner_thread(t) for t in theirs))


def _map_thread_to_first_review_state(thread: Any) -> str | None:
    connection: Any = thread.get("comments") or {}
    comments: list[Any] = connection.get("nodes") or []
    review: Any = (comments[0].get("pullRequestReview") if comments else None) or {}
    return review.get("state")


def map_thread_to_owner_thread(thread: Any) -> OwnerThread:
    first_connection: Any = thread.get("comments") or {}
    first: list[Any] = first_connection.get("nodes") or []
    last_connection: Any = thread.get("lastComment") or {}
    last: list[Any] = last_connection.get("nodes") or []
    node: Any = last[0] if last else {}
    review: Any = node.get("pullRequestReview") or {}
    # Their unsent reply is no answer, and its words stay theirs until they submit them.
    if review.get("state") == PENDING:
        node = {"author": node.get("author")}
    author: Any = node.get("author") or {}
    replies = max(int(last_connection.get("totalCount") or 0) - 1, 0)
    return OwnerThread(
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
