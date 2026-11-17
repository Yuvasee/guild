import json
import re
import sqlite3
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

from bridge.board import USER_CONVERSATION
from bridge.config import Config
from bridge.review_github import GitHubError, map_iso_to_ms, run_gh
from bridge.transcript import find_transcript, map_message_to_text

DAY_MS = 24 * 3_600_000
MANY_COMMITS = 20
_PAGES_MAX = 10
_MERGED_QUERY = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 100, after: $after) {
    issueCount pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest { commits { totalCount } } }
  }
}
"""
_NOT_TYPED = ("<", "[bridge]", "Caveat:")


@dataclass(frozen=True)
class MergedPrs:
    count: int
    median_commits: float | None
    many_commits: int
    read: int
    """PRs whose commits were counted: fewer than count past GitHub's search limit."""


def fetch_merged_prs(config: Config, since_ms: int) -> MergedPrs:
    day = datetime.fromtimestamp(since_ms / 1000, UTC).strftime("%Y-%m-%d")
    query = f"is:pr is:merged author:{config.review_author} org:{config.review_org} merged:>={day}"
    commits: list[int] = []
    total = 0
    after: str | None = None
    for _ in range(_PAGES_MAX):
        args = ["api", "graphql", "-f", f"query={_MERGED_QUERY}", "-f", f"q={query}"]
        if after:
            args += ["-f", f"after={after}"]
        try:
            search: Any = json.loads(run_gh(args))["data"]["search"]
            commits += [int(n["commits"]["totalCount"]) for n in search["nodes"] if n]
            total = int(search["issueCount"])
        except (ValueError, KeyError, TypeError) as exc:
            raise GitHubError(f"unexpected answer from GitHub: {type(exc).__name__}") from exc
        if not search["pageInfo"]["hasNextPage"]:
            break
        after = str(search["pageInfo"]["endCursor"])
    median = statistics.median(commits) if commits else None
    return MergedPrs(max(total, len(commits)), median, sum(1 for c in commits if c >= MANY_COMMITS), len(commits))


def map_phrases_to_pattern(phrases: tuple[str, ...]) -> re.Pattern[str]:
    """… (or ...) in a phrase stands for up to five words."""
    if not phrases:
        return re.compile(r"(?!)")
    parts = [r"\W+(?:\w+\W+){0,4}?".join(re.escape(p) for p in re.split(r"\s*(?:…|\.\.\.)\s*", ph)) for ph in phrases]
    return re.compile("|".join(f"(?:{p})" for p in parts), re.IGNORECASE)


def list_owner_dashboard_words(conn: sqlite3.Connection, since: int, until: int) -> list[tuple[int, str]]:
    """The owner's dashboard messages and their own answers to posts; an answer's message repeats the post, so the
    answer alone is read."""
    rows = conn.execute(
        """
        SELECT created_at, body FROM messages
        WHERE from_conversation = ?1 AND created_at >= ?2 AND created_at < ?3
          AND id NOT IN (SELECT answer_message_id FROM user_items WHERE answer_message_id IS NOT NULL)
        UNION ALL
        SELECT answered_at, answer FROM user_items
        WHERE answer IS NOT NULL AND answered_by IS NULL AND answered_at >= ?2 AND answered_at < ?3
        """,
        (USER_CONVERSATION, since, until),
    )
    return [(int(r[0]), str(r[1])) for r in rows]


def list_assistant_transcripts(conn: sqlite3.Connection, config: Config, since: int) -> list[Path]:
    rows = conn.execute(
        """
        SELECT DISTINCT c.conversation_id, c.transcript_path FROM conversations c
        JOIN instances i ON i.conversation_id = c.conversation_id OR i.instance_id = c.instance_id
        JOIN surfaces s ON s.surface = i.surface
        WHERE s.handle = ? AND c.last_seen_at >= ?
        """,
        (config.assistant_handle, since),
    )
    found = (find_transcript(str(r["conversation_id"]), r["transcript_path"]) for r in rows)
    return sorted({path for path in found if path is not None})


def list_typed_prompts(path: Path, since: int, until: int) -> list[tuple[int, str]]:
    """What a person typed into the tab: bridge's typed messages and the harness's own prompts are left out."""
    prompts: list[tuple[int, str]] = []
    with path.open(encoding="utf-8", errors="replace") as lines:
        for line in lines:
            if '"type":"user"' not in line.replace(" ", ""):
                continue
            try:
                entry: object = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            data = cast(dict[str, object], entry)
            if (
                data.get("type") != "user"
                or data.get("isMeta")
                or data.get("isSidechain")
                or data.get("isCompactSummary")
            ):
                continue
            at = map_iso_to_ms(str(data.get("timestamp") or "").replace("Z", "+00:00"))
            text = map_message_to_text(data.get("message"))
            if at is None or not text or not since <= at < until or text.lstrip().startswith(_NOT_TYPED):
                continue
            prompts.append((at, text))
    return prompts


def count_per_day(
    words: list[tuple[int, str]], pattern: re.Pattern[str], zone: ZoneInfo | None
) -> tuple[int, Counter[str]]:
    days: Counter[str] = Counter()
    for at, text in words:
        if pattern.search(text):
            days[datetime.fromtimestamp(at / 1000, zone).strftime("%a %Y-%m-%d")] += 1
    return len(words), days


def convert_week_to_lines(conn: sqlite3.Connection, config: Config, now: int, days: int) -> list[str]:
    since = now - days * DAY_MS
    lines: list[str] = []
    if config.review_org and config.review_author:
        try:
            merged = fetch_merged_prs(config, since)
            median = "none" if merged.median_commits is None else f"{merged.median_commits:g}"
            partial = f" (commits of the first {merged.read} only)" if merged.read < merged.count else ""
            lines.append(
                f"PRs by {config.review_author} merged in {config.review_org}, last {days} days: {merged.count}; "
                f"median commits per PR: {median}; with {MANY_COMMITS}+ commits: {merged.many_commits}{partial}"
            )
        except GitHubError as exc:
            lines.append(f"PRs merged: not read ({exc})")
    else:
        lines.append("PRs merged: not read (review_org or review_author is not set)")
    pattern = map_phrases_to_pattern(config.confusion_phrases)
    zone = ZoneInfo(config.work_timezone) if config.work_timezone else None
    dashboard = list_owner_dashboard_words(conn, since, now)
    transcripts = list_assistant_transcripts(conn, config, since)
    typed = [p for path in transcripts for p in list_typed_prompts(path, since, now)]
    read, per_day = count_per_day(dashboard + typed, pattern, zone)
    tab = f"{config.assistant_handle}'s tab"
    where = f"{len(dashboard)} dashboard messages and answers, {len(typed)} prompts typed in {tab}"
    if not transcripts:
        where += f" (no transcript of {tab} found: dashboard only)"
    lines.append(
        f"{config.owner_name_at_start} not understanding, rough count by phrase match, last {days} days: "
        f"{sum(per_day.values())} of {read} ({where})"
    )
    lines += [f"  {day}: {count}" for day, count in sorted(per_day.items(), key=lambda item: item[0][4:])]
    return lines
