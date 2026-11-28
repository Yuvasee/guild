import itertools
import json
import os
import re
import sqlite3
import statistics
import threading
import traceback
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

from bridge import review_github
from bridge.board import USER_CONVERSATION
from bridge.config import Config, claude_projects_dir
from bridge.errlog import log_error
from bridge.feed import ANSWER_KINDS, OPEN_KINDS
from bridge.gh_quota import CoreQuota, find_quota_refusal, read_quota
from bridge.guildview import map_body_to_sections, map_sections_to_card
from bridge.leads import find_lead_of, find_lead_on_surface, is_conversation_locked, list_leads, list_tab_lineage
from bridge.review_github import (
    GitHubError,
    RoundOver,
    bound_gh_round,
    list_loc_excluded,
    map_files_to_loc,
    map_iso_to_ms,
)
from bridge.reviews import HIS
from bridge.transcript import map_message_to_text

PR_CACHE_FILE = "owner-stats-prs.json"
PR_REFRESH_MS = 3_600_000
REFRESH_BUDGET_S = 300
"""A GitHub read's whole time: past it the read stops, keeps the cache and says why."""
FIRST_WAIT_S = 5
"""How long a page with no cached PRs waits for the first read before it shows the rest."""
SEARCH_CAP = 1000
WINDOW_DAYS = 30
READ_CHUNK = 4 << 20
MAX_RECORD = 64 << 20
"""A transcript line longer than this is skipped unread, so one runaway record cannot fill memory."""
OTHER = "other tabs"
# Text bridge or Claude Code types into a tab: mail (and its older form), worker briefs, a lead's wake-up, another
# session's message, an Esc note.
_NOT_OWNER_PREFIXES = (
    "[bridge]", "You have bridge messages", "Your brief is in ", "Guild: you are ", "Caveat:", "[Request interrupted",
    "Another Claude session sent a message",
)  # fmt: skip
_PASTE_RE = re.compile(r"<pasted_content[^>]*>\s*")
_HARNESS_TAG_RE = re.compile(
    r"<(?:task-notification|system-reminder|local-command-[\w-]+|command-[\w-]+|bash-[\w-]+|user-prompt-submit-hook)\b"
)
_SLASH_COMMAND_RE = re.compile(r"/[A-Za-z][\w:-]*(?:\s|$)")
_PRS_QUERY = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 50, after: $after) {
    issueCount pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest {
      number repository { nameWithOwner } createdAt mergedAt closedAt additions deletions
      files(first: 100) { pageInfo { hasNextPage } nodes { path additions deletions } }
    } }
  }
}
"""
_PAGES_MAX = 25


@dataclass(frozen=True)
class OwnerPr:
    created_at: int
    merged_at: int | None
    closed_at: int | None
    loc: int


@dataclass
class PrCache:
    prs: dict[str, OwnerPr] = field(default_factory=lambda: {})
    fetched_at: int | None = None
    tried_at: int | None = None
    since_ms: int | None = None
    """The start of the last complete full read; None until one finished uncapped."""
    identity: list[str] = field(default_factory=lambda: [])
    """The account and LOC exclusions the PRs were read with: another pair means a full read anew."""
    error: str | None = None
    incomplete: str | None = None
    write_error: str | None = None
    loading: bool = False


def convert_ms_to_day(at: int, zone: ZoneInfo | None) -> str:
    return datetime.fromtimestamp(at / 1000, zone).strftime("%Y-%m-%d")


def count_per_day(stamps: Iterable[int], zone: ZoneInfo | None) -> Counter[str]:
    return Counter(convert_ms_to_day(at, zone) for at in stamps)


def is_owner_prompt(entry: dict[str, object], text: str) -> bool:
    """What the owner typed into a tab: not a headless `claude -p` run, a tool result, the harness's own entries or
    tags, a slash command, or text bridge typed for him; a paste counts, unless it is bridge's long mail, which lands
    as one."""
    if entry.get("isMeta") or entry.get("isSidechain") or entry.get("isCompactSummary"):
        return False
    if entry.get("promptSource") not in (None, "typed") or entry.get("entrypoint") == "sdk-cli":
        return False
    stripped = text.lstrip()
    paste = _PASTE_RE.match(stripped)
    if paste:
        stripped = stripped[paste.end() :]
    elif _HARNESS_TAG_RE.match(stripped):
        return False
    return not (stripped.startswith(_NOT_OWNER_PREFIXES) or _SLASH_COMMAND_RE.match(stripped))


def list_owner_prompts(lines: Iterable[bytes]) -> list[tuple[str, int]]:
    """(entry uuid, epoch ms) of each prompt the owner typed; a resumed transcript repeats earlier entries with the
    same uuid, so callers dedupe by it."""
    found: list[tuple[str, int]] = []
    for line in lines:
        # Most of a transcript is tool output: skip it unparsed.
        if b'"toolUseResult"' in line or not (b'"type":"user"' in line or b'"type": "user"' in line):
            continue
        try:
            entry: object = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        data = cast(dict[str, object], entry)
        if data.get("type") != "user":
            continue
        text = map_message_to_text(data.get("message"))
        at = map_iso_to_ms(str(data.get("timestamp") or "").replace("Z", "+00:00"))
        if text and at is not None and is_owner_prompt(data, text):
            found.append((str(data.get("uuid") or f"{at}:{text[:80]}"), at))
    return found


class PromptReader:
    """Transcripts only grow: each read parses the bytes added since the last one, so the page stays quick after
    its first load, and reads in bounded blocks, a half-written last line left for the next read. A line over
    MAX_RECORD is skipped to its end and counted in `skipped`."""

    def __init__(self) -> None:
        self._seen: dict[Path, tuple[int, bool, list[tuple[str, int]]]] = {}
        """Per file: the offset read to, whether it stopped inside a skipped line, the prompts so far."""
        self._lock = threading.Lock()
        self.skipped = 0

    def read(self, path: Path) -> list[tuple[str, int]]:
        with self._lock:
            offset, dropping, prompts = self._seen.get(path, (0, False, []))
            try:
                if path.stat().st_size < offset:
                    offset, dropping, prompts = 0, False, []
                prompts = list(prompts)
                with path.open("rb") as f:
                    f.seek(offset)
                    pending = bytearray()
                    while block := f.read(READ_CHUNK):
                        if dropping:
                            end = block.find(b"\n")
                            offset += len(block) if end < 0 else end + 1
                            if end < 0:
                                continue
                            block, dropping = block[end + 1 :], False
                        pending += block
                        cut = pending.rfind(b"\n") + 1
                        if cut:
                            prompts += list_owner_prompts(bytes(pending[:cut]).splitlines())
                            offset += cut
                            del pending[:cut]
                        if len(pending) > MAX_RECORD:
                            offset += len(pending)
                            pending.clear()
                            dropping = True
                            self.skipped += 1
            except OSError:
                return self._seen.get(path, (0, False, []))[2]
            self._seen[path] = (offset, dropping, prompts)
            return prompts


def map_conversations_to_agents(
    conn: sqlite3.Connection, config: Config, conversation_ids: Iterable[str]
) -> dict[str, str]:
    """The agent the owner spoke to in each conversation: the assistant, a lead (a worker counts under its lead), or
    OTHER; a locked tab's conversation is left out."""
    leads = {lead.name for lead in list_leads(conn)}
    named = leads | {config.assistant_handle}
    closed = {
        str(r[0]): str(r[1] or r[2])
        for r in conn.execute("SELECT conversation_id, parent_lead, parent_handle FROM closed_workers")
    }
    agents: dict[str, str] = {}
    for conversation_id in conversation_ids:
        if is_conversation_locked(conn, config, conversation_id):
            continue
        parent = closed.get(conversation_id)
        if parent in named:
            agents[conversation_id] = str(parent)
            continue
        row = conn.execute(
            "SELECT i.surface FROM conversations c JOIN instances i ON i.instance_id = c.instance_id "
            "WHERE c.conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        surface = row[0] if row else None
        lead = find_lead_on_surface(conn, find_lead_of(conn, surface)[1])
        if lead is not None:
            agents[conversation_id] = lead.name
            continue
        tabs = list_tab_lineage(conn, surface)
        marks = ",".join("?" * len(tabs))
        under_assistant = conn.execute(
            f"SELECT 1 FROM surfaces WHERE handle = ? AND (surface = ? OR agterm_session IN ({marks}))",
            (config.assistant_handle, surface, *tabs),
        ).fetchone()
        agents[conversation_id] = config.assistant_handle if under_assistant else OTHER
    return agents


def find_transcripts(conn: sqlite3.Connection, since: int) -> dict[str, Path]:
    known: dict[str, Path] = {}
    try:
        index = {path.stem: path for path in claude_projects_dir().glob("*/*.jsonl")}
    except OSError:
        index = {}
    for row in conn.execute(
        "SELECT conversation_id, transcript_path FROM conversations WHERE last_seen_at >= ?", (since,)
    ):
        stored = row[1]
        path = Path(stored) if stored and os.path.isfile(stored) else index.get(str(row[0]))
        if path is not None:
            known[str(row[0])] = path
    return known


def count_tab_prompts(
    transcripts: dict[str, Path],
    agents: dict[str, str],
    read: Callable[[Path], list[tuple[str, int]]],
    since: int,
    zone: ZoneInfo | None,
) -> dict[str, Counter[str]]:
    seen: set[str] = set()
    per_agent: dict[str, Counter[str]] = {}
    for conversation_id, path in transcripts.items():
        agent = agents.get(conversation_id)
        if agent is None:
            continue
        for key, at in read(path):
            if key in seen or at < since:
                continue
            seen.add(key)
            per_agent.setdefault(agent, Counter())[convert_ms_to_day(at, zone)] += 1
    return per_agent


def count_dashboard_messages(
    conn: sqlite3.Connection, config: Config, agents: dict[str, str], since: int, zone: ZoneInfo | None
) -> tuple[dict[str, Counter[str]], Counter[str]]:
    """The owner's dashboard messages, once under each agent they went to, and once in all; an answer to a post is a
    question answered, not a message. A locked tab's recipient counts nowhere."""
    named = {lead.name for lead in list_leads(conn)} | {config.assistant_handle}
    rows = conn.execute(
        """
        SELECT DISTINCT m.id, m.created_at, r.conversation_id, r.handle FROM messages m
        JOIN message_recipients r ON r.message_id = m.id
        WHERE m.from_conversation = ? AND m.created_at >= ?
          AND m.id NOT IN (SELECT answer_message_id FROM user_items WHERE answer_message_id IS NOT NULL)
        """,
        (USER_CONVERSATION, since),
    )
    pairs: set[tuple[int, str, int]] = set()
    for message_id, at, conversation_id, handle in rows:
        agent = agents.get(str(conversation_id))
        if agent is None:
            if is_conversation_locked(conn, config, str(conversation_id)):
                continue
            agent = str(handle) if handle in named else OTHER
        pairs.add((int(message_id), agent, int(at)))
    per_agent: dict[str, Counter[str]] = {}
    for _, agent, at in pairs:
        per_agent.setdefault(agent, Counter())[convert_ms_to_day(at, zone)] += 1
    total = count_per_day((at for _, at in {(message_id, at) for message_id, _, at in pairs}), zone)
    return per_agent, total


def find_unlocked(conn: sqlite3.Connection, config: Config) -> Callable[[object], bool]:
    """Whether a post's conversation is not a locked tab's, memoized: a feed has many posts from few tabs."""
    seen: dict[str, bool] = {}

    def is_unlocked(conversation_id: object) -> bool:
        if conversation_id is None:
            return True
        key = str(conversation_id)
        if key not in seen:
            seen[key] = not is_conversation_locked(conn, config, key)
        return seen[key]

    return is_unlocked


def count_answers(
    conn: sqlite3.Connection, config: Config, since: int, zone: ZoneInfo | None
) -> dict[str, Counter[str]]:
    """button: an option picked; pick: of those, the one the post recommended; typed: his own words; chat: the
    assistant relayed what he said in chat."""
    kinds = sorted(OPEN_KINDS)
    rows = conn.execute(
        f"""
        SELECT answered_at, answer, answered_by, options, body, from_conversation FROM user_items
        WHERE kind IN ({",".join("?" * len(kinds))}) AND answer IS NOT NULL AND answered_at >= ?
        """,
        (*kinds, since),
    )
    found: dict[str, Counter[str]] = {key: Counter() for key in ("button", "pick", "typed", "chat")}
    is_unlocked = find_unlocked(conn, config)
    for at, answer, answered_by, options_json, body, conversation_id in rows:
        if not is_unlocked(conversation_id):
            continue
        day = convert_ms_to_day(int(at), zone)
        if answered_by:
            found["chat"][day] += 1
            continue
        options = [str(o) for o in json.loads(options_json or "[]")]
        if answer not in options:
            found["typed"][day] += 1
            continue
        found["button"][day] += 1
        card = map_sections_to_card(map_body_to_sections(str(body or ""), options), options)
        if card and any(o["pick"] and o["label"] == answer for o in card["options"]):
            found["pick"][day] += 1
    return found


def map_answers_to_median_hours(
    conn: sqlite3.Connection, config: Config, since: int, zone: ZoneInfo | None
) -> dict[str, float]:
    kinds = sorted(OPEN_KINDS)
    waits: dict[str, list[float]] = {}
    is_unlocked = find_unlocked(conn, config)
    for created, answered, conversation_id in conn.execute(
        f"""
        SELECT created_at, answered_at, from_conversation FROM user_items
        WHERE kind IN ({",".join("?" * len(kinds))}) AND answered_at >= ?
        """,
        (*kinds, since),
    ):
        if not is_unlocked(conversation_id):
            continue
        waits.setdefault(convert_ms_to_day(int(answered), zone), []).append((int(answered) - int(created)) / 3_600_000)
    return {day: round(statistics.median(hours), 1) for day, hours in waits.items()}


def count_withdrawn_questions(
    conn: sqlite3.Connection, config: Config, since: int, zone: ZoneInfo | None
) -> Counter[str]:
    """Closed with no answer: its author withdrew it, or he dismissed it on the page."""
    kinds = sorted(OPEN_KINDS)
    rows = conn.execute(
        f"""
        SELECT resolved_at, from_conversation FROM user_items
        WHERE kind IN ({",".join("?" * len(kinds))}) AND answer IS NULL AND resolved_at >= ?
        """,
        (*kinds, since),
    )
    is_unlocked = find_unlocked(conn, config)
    return count_per_day((int(at) for at, conversation_id in rows if is_unlocked(conversation_id)), zone)


def count_answers_opened(conn: sqlite3.Connection, config: Config, since: int, zone: ZoneInfo | None) -> Counter[str]:
    kinds = sorted(ANSWER_KINDS)
    rows = conn.execute(
        f"""
        SELECT opened_at, from_conversation FROM user_items
        WHERE kind IN ({",".join("?" * len(kinds))}) AND opened_at >= ?
        """,
        (*kinds, since),
    )
    is_unlocked = find_unlocked(conn, config)
    return count_per_day((int(at) for at, conversation_id in rows if is_unlocked(conversation_id)), zone)


def count_published_prs(
    conn: sqlite3.Connection, config: Config, owner_prs: Iterable[str], since: int, zone: ZoneInfo | None
) -> Counter[str]:
    """Posts of the owner's own PRs (owner/repo#number, as GitHub names his), by the post's Slack ts: seen_at is when
    bridge first read it, and its first read took in the older posts; the channel carries colleagues' PRs too."""
    ignored = set(config.review_ignored_posts)
    his = {key.casefold() for key in owner_prs}
    stamps: list[int] = []
    for ts, repo, number in conn.execute("SELECT ts, repo, number FROM review_slack_posts"):
        if f"{repo}#{number}".casefold() not in his:
            continue
        try:
            at = int(float(ts) * 1000)
        except (TypeError, ValueError):
            continue
        if at >= since and ts not in ignored:
            stamps.append(at)
    return count_per_day(stamps, zone)


def count_owner_comments(conn: sqlite3.Connection, since: int, zone: ZoneInfo | None) -> Counter[str]:
    """The review queue's own verdict on who wrote each comment under the shared account."""
    rows = conn.execute("SELECT created_at FROM review_comments WHERE verdict = ? AND created_at >= ?", (HIS, since))
    return count_per_day((int(r[0]) for r in rows), zone)


def count_asks(conn: sqlite3.Connection, since: int, zone: ZoneInfo | None) -> dict[str, Counter[str]]:
    recorded = conn.execute("SELECT created_at FROM asks WHERE created_at >= ?", (since,))
    verified = conn.execute("SELECT at FROM ask_events WHERE state = 'verified' AND at >= ?", (since,))
    reopened = conn.execute(
        "SELECT at FROM ask_events WHERE state = 'open' AND note LIKE 'reopened:%' AND at >= ?", (since,)
    )
    return {
        "recorded": count_per_day((int(r[0]) for r in recorded), zone),
        "verified": count_per_day((int(r[0]) for r in verified), zone),
        "reopened": count_per_day((int(r[0]) for r in reopened), zone),
    }


def count_prs(prs: Iterable[OwnerPr], since: int, zone: ZoneInfo | None) -> dict[str, Counter[str]]:
    """loc_written: the size of each PR opened that day, as it is now; loc_merged: of each PR merged that day."""
    found: dict[str, Counter[str]] = {k: Counter() for k in ("opened", "merged", "closed", "loc_written", "loc_merged")}
    for pr in prs:
        if pr.created_at >= since:
            day = convert_ms_to_day(pr.created_at, zone)
            found["opened"][day] += 1
            found["loc_written"][day] += pr.loc
        if pr.merged_at is not None and pr.merged_at >= since:
            day = convert_ms_to_day(pr.merged_at, zone)
            found["merged"][day] += 1
            found["loc_merged"][day] += pr.loc
        elif pr.merged_at is None and pr.closed_at is not None and pr.closed_at >= since:
            found["closed"][convert_ms_to_day(pr.closed_at, zone)] += 1
    return found


def map_node_to_owner_pr(
    node: Any, excluded: tuple[str, ...], core_refusal: Callable[[], str | None]
) -> tuple[str, OwnerPr]:
    """A PR with over 100 files gets them all, as the review queue reads them, or a lockfile past the first 100 would
    count as code. Those pages spend the REST quota, so the gh guard's check comes first."""
    repo, number = str(node["repository"]["nameWithOwner"]), int(node["number"])
    key = f"{repo}#{number}"
    block: Any = node.get("files") or {}
    files: list[Any] = block.get("nodes") or []
    page: Any = block.get("pageInfo") or {}
    if page.get("hasNextPage"):
        if (refusal := core_refusal()) is not None:
            raise GitHubError(refusal)
        files = review_github.fetch_all_files(repo, number)
    loc = map_files_to_loc(int(node["additions"]), int(node["deletions"]), files, excluded)
    created = map_iso_to_ms(node["createdAt"])
    if created is None:
        raise GitHubError(f"no createdAt on {key}")
    return key, OwnerPr(created, map_iso_to_ms(node.get("mergedAt")), map_iso_to_ms(node.get("closedAt")), loc)


def convert_ms_to_search_time(at: int) -> str:
    return datetime.fromtimestamp(at / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def search_owner_prs(
    config: Config, qualifier: str, core_refusal: Callable[[], str | None]
) -> tuple[dict[str, OwnerPr], bool]:
    """PRs by review_author in any repo matching the qualifier, and whether GitHub's 1000-result cap cut them."""
    query = f"is:pr author:{config.review_author} {qualifier}"
    excluded = list_loc_excluded(config)
    prs: dict[str, OwnerPr] = {}
    after: str | None = None
    total = 0
    while True:
        args = ["api", "graphql", "-f", f"query={_PRS_QUERY}", "-f", f"q={query}"]
        if after:
            args += ["-f", f"after={after}"]
        try:
            search: Any = json.loads(review_github.run_gh(args))["data"]["search"]
            prs.update(map_node_to_owner_pr(n, excluded, core_refusal) for n in search["nodes"] if n)
            total = int(search["issueCount"])
            more = bool(search["pageInfo"]["hasNextPage"])
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise GitHubError(f"unexpected answer from GitHub: {type(exc).__name__}") from exc
        if not more:
            break
        after = str(search["pageInfo"]["endCursor"])
    return prs, total > SEARCH_CAP


def fetch_owner_prs(
    config: Config, since: int, now: int, core_refusal: Callable[[], str | None]
) -> tuple[dict[str, OwnerPr], list[str]]:
    """Every PR updated since `since`: those created since then, a window at a time so no search reaches the cap,
    and older ones updated since. Returns the PRs and the searches the cap cut."""
    bounds = [*range(since, now, WINDOW_DAYS * 24 * 3_600_000), now + 3_600_000]
    qualifiers = [
        f"created:{convert_ms_to_search_time(start)}..{convert_ms_to_search_time(end - 1000)}"
        for start, end in itertools.pairwise(bounds)
    ]
    qualifiers.append(f"created:<{convert_ms_to_search_time(since)} updated:>={convert_ms_to_search_time(since)}")
    prs: dict[str, OwnerPr] = {}
    capped: list[str] = []
    for qualifier in qualifiers:
        found, cut = search_owner_prs(config, qualifier, core_refusal)
        prs.update(found)
        if cut:
            capped.append(qualifier)
    return prs, capped


def map_config_to_identity(config: Config) -> list[str]:
    return [config.review_author.casefold(), *sorted(list_loc_excluded(config))]


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise TypeError("not an int")


def read_pr_cache(path: Path) -> PrCache:
    """A file that is missing, not JSON or not in this shape reads as no cache."""
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
        prs: dict[str, OwnerPr] = {}
        for key, row in raw["prs"].items():
            created, merged, closed, loc = (_int_or_none(value) for value in row)
            if created is None or loc is None:
                raise TypeError("no created_at or loc")
            prs[str(key)] = OwnerPr(created, merged, closed, loc)
        parts: list[Any] = raw.get("identity") or []
        identity = [str(part) for part in parts]
        return PrCache(
            prs,
            _int_or_none(raw.get("fetched_at")),
            _int_or_none(raw.get("tried_at")),
            _int_or_none(raw.get("since_ms")),
            identity,
            None if raw.get("error") is None else str(raw["error"]),
            None if raw.get("incomplete") is None else str(raw["incomplete"]),
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return PrCache()


def write_pr_cache(path: Path, cache: PrCache) -> None:
    raw = {
        "fetched_at": cache.fetched_at,
        "tried_at": cache.tried_at,
        "since_ms": cache.since_ms,
        "identity": cache.identity,
        "error": cache.error,
        "incomplete": cache.incomplete,
        "prs": {k: [p.created_at, p.merged_at, p.closed_at, p.loc] for k, p in cache.prs.items()},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(raw), encoding="utf-8")
    os.replace(tmp, path)


def find_graphql_refusal(
    reserve: int, fetch: Callable[[], CoreQuota | None] = lambda: read_quota("graphql")
) -> str | None:
    """The search spends GraphQL points, a bucket apart from the REST quota the gh guard keeps."""
    if reserve <= 0:
        return None
    quota = fetch()
    if quota is None or quota.remaining >= reserve:
        return None
    when = datetime.fromtimestamp(quota.reset_at, UTC).strftime("%H:%M")
    return f"GraphQL quota {quota.remaining}/{quota.limit} < {reserve} reserve, resets {when} UTC"


class PrStore:
    """The owner's PRs, from a cache file and kept in memory: a page reads what is there, and a read of GitHub runs
    in the background at most once an hour, failed or not, even when the file cannot be written; only a bug in
    the read lets the next page try again."""

    def __init__(
        self,
        path: Path,
        quota_refusal: Callable[[int], str | None] = find_graphql_refusal,
        core_refusal: Callable[[int], str | None] = find_quota_refusal,
    ) -> None:
        self.path = path
        self._quota_refusal = quota_refusal
        self._core_refusal = core_refusal
        self._cache: PrCache | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def _load(self, config: Config) -> PrCache:
        if self._cache is None:
            self._cache = read_pr_cache(self.path)
        identity = map_config_to_identity(config)
        if self._cache.identity != identity:
            self._cache = PrCache(identity=identity)
        return self._cache

    def view(self, config: Config, since: int, now: int) -> PrCache:
        if not config.review_author:
            return PrCache(error="review_author is not set")
        with self._lock:
            cache = self._load(config)
            due = cache.tried_at is None or now - cache.tried_at >= PR_REFRESH_MS
            if due and (self._thread is None or not self._thread.is_alive()):
                cache.tried_at = now
                self._thread = threading.Thread(target=self.refresh_or_log, args=(config, since, now), daemon=True)
                self._thread.start()
            thread = self._thread
        if thread is not None and cache.fetched_at is None:
            thread.join(FIRST_WAIT_S)
        with self._lock:
            cache = self._load(config)
            return PrCache(
                dict(cache.prs),
                cache.fetched_at,
                cache.tried_at,
                cache.since_ms,
                cache.identity,
                cache.error,
                cache.incomplete,
                cache.write_error,
                loading=thread is not None and thread.is_alive(),
            )

    def refresh_or_log(self, config: Config, since: int, now: int) -> None:
        """A bug in a read shows in the page header and errors.log, and the next page load tries again."""
        try:
            self.refresh(config, since, now)
        except Exception as exc:
            log_error("owner stats", traceback.format_exc())
            with self._lock:
                cache = self._load(config)
                cache.error = f"read failed: {type(exc).__name__}: {exc}"[:300]
                cache.tried_at = None

    def refresh(self, config: Config, since: int, now: int) -> None:
        """The first read, or one after an incomplete or differently configured one, covers everything since
        `since`; later ones only PRs updated since a day before the last good read."""
        with self._lock:
            cache = self._load(config)
            cache.tried_at = now
            full = cache.fetched_at is None or cache.since_ms != since
            last = cache.fetched_at
        error: str | None = self._quota_refusal(config.gh_quota_reserve)

        def core_refusal() -> str | None:
            return self._core_refusal(config.gh_quota_reserve)

        fetched: dict[str, OwnerPr] = {}
        capped: list[str] = []
        if error is None:
            try:
                with bound_gh_round(REFRESH_BUDGET_S):
                    if full or last is None:
                        fetched, capped = fetch_owner_prs(config, since, now, core_refusal)
                    else:
                        found, cut = search_owner_prs(
                            config, f"updated:>={convert_ms_to_search_time(last - 24 * 3_600_000)}", core_refusal
                        )
                        fetched, capped = found, ["recent updates"] if cut else []
            except GitHubError as exc:
                error = str(exc)
            except RoundOver:
                error = f"GitHub read took over {REFRESH_BUDGET_S} s and stopped"
        with self._lock:
            cache = self._load(config)
            cache.tried_at = now
            if error is not None:
                cache.error = error
            else:
                cache.prs = fetched if full else {**cache.prs, **fetched}
                cache.fetched_at, cache.error = now, None
                # A cut search, full or not, means the next read is full; only an uncut full one clears the warning.
                if capped:
                    cache.since_ms = None
                    cache.incomplete = f"GitHub's {SEARCH_CAP}-result cap cut {', '.join(capped)}"
                elif full:
                    cache.since_ms, cache.incomplete = since, None
            try:
                write_pr_cache(self.path, cache)
                cache.write_error = None
            except OSError as exc:
                cache.write_error = f"cache not saved: {exc.strerror or type(exc).__name__}"


def list_days(first_day: str, last_day: str) -> list[str]:
    day = datetime.strptime(first_day, "%Y-%m-%d")
    end = datetime.strptime(last_day, "%Y-%m-%d")
    days: list[str] = []
    while day <= end:
        days.append(day.strftime("%Y-%m-%d"))
        day += timedelta(days=1)
    return days


def map_counts_to_values(counts: dict[str, float], days: list[str], scale: float = 1) -> list[float | None]:
    """None before the source's first day: no record yet, which is not a zero."""
    first = min((d for d, v in counts.items() if v), default=None)
    return [None if first is None or day < first else round(counts.get(day, 0) / scale, 2) for day in days]


def map_measures_to_values(measures: dict[str, float], days: list[str]) -> list[float | None]:
    """A measure, not a count: a day without one is blank and a measured 0 stays 0."""
    return [measures.get(day) for day in days]


def map_db_to_stats(
    conn: sqlite3.Connection,
    config: Config,
    now: int,
    read: Callable[[Path], list[tuple[str, int]]],
    load_prs: Callable[[int], PrCache],
) -> dict[str, object]:
    zone = ZoneInfo(config.work_timezone) if config.work_timezone else None
    row = conn.execute("SELECT min(created_at) FROM messages").fetchone()
    first_day = convert_ms_to_day(int(row[0]) if row and row[0] is not None else now, zone)
    since = int(datetime.strptime(first_day, "%Y-%m-%d").replace(tzinfo=zone).timestamp() * 1000)
    days = list_days(first_day, convert_ms_to_day(now, zone))
    transcripts = find_transcripts(conn, since)
    recipients = {str(r[0]) for r in conn.execute("SELECT DISTINCT conversation_id FROM message_recipients")}
    agents = map_conversations_to_agents(conn, config, set(transcripts) | recipients)
    typed = count_tab_prompts(transcripts, agents, read, since, zone)
    posted, posted_total = count_dashboard_messages(conn, config, agents, since, zone)
    lines: list[dict[str, object]] = []

    def add(group: str, key: str, label: str, counts: dict[str, float], unit: str = "count", scale: float = 1) -> None:
        values = map_counts_to_values(counts, days, scale)
        lines.append({"key": key, "group": group, "label": label, "unit": unit, "values": values})

    names = [(config.assistant_handle, config.assistant_handle.capitalize())]
    names += [(lead.name, lead.display_name) for lead in list_leads(conn)]
    names.append((OTHER, "Other tabs"))
    total: Counter[str] = Counter(posted_total)
    for counts in typed.values():
        total.update(counts)
    add("Messages", "msg_all", "All agents", dict(total))
    for name, label in names:
        add("Messages", f"msg_{name}", label, dict(typed.get(name, Counter()) + posted.get(name, Counter())))
    answers = count_answers(conn, config, since, zone)
    add("Questions", "q_button", "Answered by a button", dict(answers["button"]))
    add("Questions", "q_pick", "…the recommended answer", dict(answers["pick"]))
    add("Questions", "q_typed", "Answered in your words", dict(answers["typed"]))
    add("Questions", "q_chat", f"Settled in chat with {names[0][1]}", dict(answers["chat"]))
    add(
        "Questions", "q_withdrawn", "Withdrawn or dismissed", dict(count_withdrawn_questions(conn, config, since, zone))
    )
    hours = map_measures_to_values(map_answers_to_median_hours(conn, config, since, zone), days)
    lines.append(
        {"key": "q_hours", "group": "Questions", "label": "Median hours to answer", "unit": "hours", "values": hours}
    )
    add("Questions", "a_opened", "Answers opened", dict(count_answers_opened(conn, config, since, zone)))
    asks = count_asks(conn, since, zone)
    add("Asks", "asks_recorded", "Recorded", dict(asks["recorded"]))
    add("Asks", "asks_verified", "Verified", dict(asks["verified"]))
    add("Asks", "asks_reopened", "Reopened", dict(asks["reopened"]))
    cache = load_prs(since)
    prs = count_prs(cache.prs.values(), since, zone)
    published = count_published_prs(conn, config, cache.prs, since, zone) if cache.fetched_at else Counter[str]()
    add("PRs", "pr_published", "Published for review", dict(published))
    add("PRs", "pr_opened", "Opened", dict(prs["opened"]))
    add("PRs", "pr_merged", "Merged", dict(prs["merged"]))
    add("PRs", "pr_closed", "Closed unmerged", dict(prs["closed"]))
    add("Lines of code", "loc_written", "Written (PRs opened)", dict(prs["loc_written"]), "kloc", 1000)
    add("Lines of code", "loc_merged", "Merged", dict(prs["loc_merged"]), "kloc", 1000)
    add("Comments", "comments", "Your PR comments", dict(count_owner_comments(conn, since, zone)))
    return {
        "days": days,
        "lines": lines,
        "author": config.review_author,
        "github": {
            "fetched_at": cache.fetched_at,
            "error": cache.error,
            "incomplete": cache.incomplete,
            "write_error": cache.write_error,
            "loading": cache.loading,
            "prs": len(cache.prs),
        },
        "transcripts": len(transcripts),
    }
