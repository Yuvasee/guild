import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from bridge.config import Config, state_dir
from bridge.db import now_ms, transaction
from bridge.review_post import EXTERNAL, POSTED, POSTED_NO_LINK, SENDING, build_send_env, read_event_blocks
from bridge.reviews import store_merge_blocks

TOKEN = "token"
CONNECTOR = "connector"
TOKEN_EVERY_S = 60
CONNECTOR_EVERY_S = 300
FIRST_LOOKBACK_S = 7 * 86_400
HISTORY_PAGE = 100
HISTORY_PAGES_MAX = 10
SEARCH_PAGE = 20
SEARCH_OVERLAP_S = 600
SEARCH_TIMEOUT_S = 180
SEARCH_ATTEMPTS = 2
CONNECTOR_RETRY_S = 2
CONNECTOR_RUN_MAX_MS = (SEARCH_ATTEMPTS * SEARCH_TIMEOUT_S + CONNECTOR_RETRY_S + 60) * 1000
CLOCK_SKEW_S = 60
HTTP_TIMEOUT_S = 30
READ_TOOL = "mcp__claude_ai_Slack__slack_search_public"
NEVER_CALLED = "the read tool was never called"

_PR_URL_RE = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
_RESULT_HEAD_RE = re.compile(
    r"\A\d+ of \d+\nChannel: [^\n]*\(ID: (\w+)\)\n(?:[^\n]*\n){0,3}?Message_ts: (\d+\.\d+)\n(?:[^\n]*\n){0,3}?"
    r"Permalink: \[link\]\((https://[^)\s]+)\)\nText:"
)
_CURSOR_RE = re.compile(r"cursor: `([^`]+)`")

Fetch = Callable[[str, dict[str, str]], dict[str, Any]]


class SlackError(Exception):
    pass


@dataclass(frozen=True)
class SeenPost:
    ts: str
    text: str
    link: str | None


@dataclass(frozen=True)
class SearchPage:
    posts: list[SeenPost]
    cursor: str | None


@dataclass(frozen=True)
class ReadState:
    mode: str | None = None
    last_ts: str | None = None
    """Every channel post up to here is read."""
    walk_latest: str | None = None
    """A token read cut short: posts older than this one, and newer than last_ts, are still unread."""
    walk_newest: str | None = None
    search_after: int | None = None
    """The `after` of a search not yet read to its last page; a failed page restarts it from here."""
    search_cursor: str | None = None
    started_at: int | None = None
    """A connector search's claim; a token read clears it, so a search still running then writes nothing."""
    finished_at: int | None = None


def token_path() -> Path:
    return Path.home() / ".secrets" / "slack"


def read_token(path: Path) -> str | None:
    """None without the file. The token never leaves this process except in Slack's Authorization header."""
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return None
    if mode & 0o077:
        raise SlackError("~/.secrets/slack must be mode 600")
    token = path.read_text().strip()
    if not token:
        raise SlackError("~/.secrets/slack is empty")
    return token


def map_ts_to_key(ts: str) -> tuple[int, int]:
    whole, _, fraction = ts.partition(".")
    return int(whole), int(fraction or 0)


def map_ts_to_permalink(team_url: str, channel: str, ts: str) -> str:
    return f"{team_url.rstrip('/')}/archives/{channel}/p{ts.replace('.', '')}"


def build_token_fetch(token: str) -> Fetch:
    def fetch(method: str, params: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"https://slack.com/api/{method}?{urllib.parse.urlencode(params)}",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
                body: Any = json.load(response)
        except urllib.error.HTTPError as exc:
            raise SlackError("rate limited" if exc.code == 429 else f"HTTP {exc.code}") from None
        except (OSError, ValueError) as exc:
            raise SlackError(f"Slack did not answer: {type(exc).__name__}") from None
        if not isinstance(body, dict) or not cast(dict[str, Any], body).get("ok"):
            raise SlackError(str(cast(Any, body).get("error") if isinstance(body, dict) else "bad answer"))
        return cast(dict[str, Any], body)

    return fetch


def read_history(
    fetch: Fetch, channel: str, team_url: str, state: ReadState, now: int
) -> tuple[list[SeenPost], ReadState, str | None]:
    """Newest first, a page at a time, walking back with `latest`; a walk cut short resumes on the next read. The
    first walk keeps its week-old floor in last_ts, so a resume does not move it."""
    oldest = state.last_ts or f"{now // 1000 - FIRST_LOOKBACK_S}.000000"
    latest, newest = state.walk_latest, state.walk_newest
    posts: list[SeenPost] = []
    cursor: str | None = None
    for _ in range(HISTORY_PAGES_MAX):
        params = {"channel": channel, "oldest": oldest, "limit": str(HISTORY_PAGE)}
        if latest:
            params["latest"] = latest
        if cursor:
            params["cursor"] = cursor
        try:
            body = fetch("conversations.history", params)
        except SlackError as exc:
            return posts, ReadState(TOKEN, oldest, latest, newest), str(exc)
        raw: list[Any] = body.get("messages") or []
        messages: list[dict[str, Any]] = [
            cast(dict[str, Any], m) for m in raw if isinstance(m, dict) and cast(dict[str, Any], m).get("ts")
        ]
        for message in messages:
            ts = str(message["ts"])
            posts.append(SeenPost(ts, str(message.get("text") or ""), map_ts_to_permalink(team_url, channel, ts)))
            if newest is None or map_ts_to_key(ts) > map_ts_to_key(newest):
                newest = ts
        if not body.get("has_more"):
            return posts, ReadState(TOKEN, newest or oldest), None
        if messages:
            latest, cursor = min((str(m["ts"]) for m in messages), key=map_ts_to_key), None
        else:
            # Slack may answer a page with nothing but a cursor.
            metadata: Any = body.get("response_metadata") or {}
            cursor = str(metadata.get("next_cursor") or "") or None
            if cursor is None:
                return posts, ReadState(TOKEN, oldest, latest, newest), "Slack gave an empty page with no cursor"
    return posts, ReadState(TOKEN, oldest, latest, newest), None


def build_search_request(channel: str, state: ReadState, now: int) -> dict[str, Any]:
    """Ascending from a little before the last post read, as the search index can lag behind the channel; the rest of a
    longer answer comes by its cursor."""
    if state.search_after is not None:
        after = state.search_after
    elif state.last_ts is None:
        after = now // 1000 - FIRST_LOOKBACK_S
    else:
        after = map_ts_to_key(state.last_ts)[0] - SEARCH_OVERLAP_S
    request: dict[str, Any] = {
        "filters": f"in:<#{channel}>",
        "after": str(after),
        "sort": "timestamp",
        "sort_dir": "asc",
        "limit": SEARCH_PAGE,
        "include_bots": True,
        "include_context": False,
    }
    if state.search_cursor:
        request["cursor"] = state.search_cursor
    return request


def build_search_command(config: Config, request: dict[str, Any]) -> list[str]:
    """One turn: the tool runs and its result is read from the events, and the model never reads what people wrote.
    Other tools stay unapproved under dontAsk, but a turn after the result could still try one."""
    prompt = (
        f"Call {READ_TOOL} exactly once with exactly this JSON as its input, changing nothing, then stop:\n"
        + json.dumps(request, ensure_ascii=False)
    )
    return [
        shutil.which("claude") or "claude", "-p", prompt,
        "--model", config.review_post_model,
        "--setting-sources", "user",
        "--settings", '{"disableAllHooks":true}',
        "--tools", "",
        "--allowedTools", READ_TOOL,
        "--permission-mode", "dontAsk",
        "--max-turns", "1",
        "--no-session-persistence",
        "--output-format", "stream-json",
        "--verbose",
    ]  # fmt: skip


def map_events_to_search(stdout: str, channel: str, request: dict[str, Any], now: int) -> SearchPage:
    """Only the read tool's own result counts, for the call made exactly as asked, never the model's text."""
    calls: set[object] = set()
    answers: list[str] = []
    for block in read_event_blocks(stdout):
        if block.get("type") == "tool_use" and block.get("name") == READ_TOOL and block.get("input") == request:
            calls.add(block.get("id"))
        elif block.get("type") == "tool_result" and block.get("tool_use_id") in calls:
            if block.get("is_error"):
                raise SlackError(f"the read tool returned an error: {str(block.get('content'))[:200]}")
            answers.append(_map_result_to_text(block.get("content")))
    if not answers:
        raise SlackError(NEVER_CALLED)
    floor = int(request["after"])
    pages = [map_search_answer_to_page(answer, channel, floor, now) for answer in answers]
    return SearchPage([post for page in pages for post in page.posts], pages[-1].cursor)


def _map_result_to_text(content: object) -> str:
    if isinstance(content, str):
        return content
    parts = [cast(dict[str, Any], b).get("text") for b in cast(list[Any], content or []) if isinstance(b, dict)]
    return "".join(str(p) for p in parts if p)


def map_search_answer_to_page(answer: str, channel: str, floor_s: int, now: int) -> SearchPage:
    """A message's own text can imitate a result's head, so a result counts only with a ts inside the window asked for
    and a permalink to that very message in this channel."""
    try:
        raw: Any = json.loads(answer)
    except ValueError as exc:
        raise SlackError("the read tool's answer is not JSON") from exc
    if not isinstance(raw, dict):
        raise SlackError("the read tool's answer is not JSON")
    answer_fields = cast(dict[str, Any], raw)
    cursor = _CURSOR_RE.search(str(answer_fields.get("pagination_info") or ""))
    posts: list[SeenPost] = []
    for chunk in re.split(r"^### Result ", str(answer_fields.get("results") or ""), flags=re.M)[1:]:
        head = _RESULT_HEAD_RE.match(chunk)
        if head is None or head.group(1) != channel:
            continue
        ts, link = head.group(2), head.group(3)
        if not floor_s <= map_ts_to_key(ts)[0] <= now // 1000 + CLOCK_SKEW_S:
            continue
        expected = re.compile(rf"https://[\w-]+\.slack\.com/archives/{channel}/p{ts.replace('.', '')}(?:\?\S*)?")
        posts.append(SeenPost(ts, chunk[head.end() :], link if expected.fullmatch(link) else None))
    return SearchPage(posts, cursor.group(1) if cursor else None)


def map_search_to_state(page: SearchPage, request: dict[str, Any], state: ReadState) -> ReadState:
    """Only a post with its own permalink moves the mark: a message can imitate a result, though not its link."""
    keys = [post.ts for post in page.posts if post.link] + ([state.last_ts] if state.last_ts else [])
    newest = max(keys, key=map_ts_to_key) if keys else None
    return replace(
        state,
        mode=CONNECTOR,
        last_ts=newest,
        walk_latest=None,
        walk_newest=None,
        search_after=int(request["after"]) if page.cursor else None,
        search_cursor=page.cursor,
    )


def record_seen_posts(conn: sqlite3.Connection, posts: list[SeenPost], now: int) -> list[str]:
    """Kept per PR link, the earliest post first, whether or not the PR is in the queue yet: GitHub may list it after
    Slack does. Refs whose record changed."""
    for post in posts:
        for repo, number in _PR_URL_RE.findall(post.text):
            known = conn.execute(
                "SELECT ts, link FROM review_slack_posts WHERE repo = ? AND number = ?", (repo.lower(), int(number))
            ).fetchone()
            if known is not None:
                earlier = map_ts_to_key(str(known["ts"])) < map_ts_to_key(post.ts)
                if earlier or (known["ts"] == post.ts and (known["link"] or not post.link)):
                    continue
            conn.execute(
                """
                INSERT INTO review_slack_posts (repo, number, ts, link, seen_at) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (repo, number) DO UPDATE SET ts = excluded.ts, link = excluded.link
                """,
                (repo.lower(), int(number), post.ts, post.link, now),
            )
    return apply_seen_posts(conn, now)


def apply_seen_posts(conn: sqlite3.Connection, now: int) -> list[str]:
    """Before any send is claimed, so a post seen in the channel is never posted again."""
    rows = conn.execute(
        """
        SELECT p.ref, s.link FROM review_prs p
        JOIN review_slack_posts s ON s.repo = lower(p.repo) AND s.number = p.number
        WHERE p.closed_at IS NULL
        """
    ).fetchall()
    changed = [str(r["ref"]) for r in rows if record_seen_post(conn, str(r["ref"]), r["link"], now)]
    if changed:
        # The merge guard reads the stored path, which a post has just changed.
        store_merge_blocks(conn)
    return changed


def record_seen_post(conn: sqlite3.Connection, ref: str, link: str | None, now: int) -> bool:
    """A post found in the channel, whoever made it, publishes the PR, and its owner is not told to request reviewers.
    A send in flight is left to its sender, whose own post this may be, and its own post keeps bridge's usual steps;
    a sent post with no link gets this one. Any send still to come ends."""
    row = conn.execute("SELECT post_state, post_link FROM review_prs WHERE ref = ?", (ref,)).fetchone()
    state = row["post_state"]
    if state in (SENDING, POSTED) or (state in (EXTERNAL, POSTED_NO_LINK) and row["post_link"]):
        return False
    if state in (EXTERNAL, POSTED_NO_LINK):
        if link is None:
            return False
        conn.execute("UPDATE review_prs SET post_link = ? WHERE ref = ?", (link, ref))
        return True
    conn.execute(
        """
        UPDATE review_prs SET post_state = ?, post_link = ?, post_at = ?, post_detail = NULL, post_seen_at = ?
        WHERE ref = ?
        """,
        (EXTERNAL, link, now, now, ref),
    )
    return True


def load_read_state(conn: sqlite3.Connection) -> ReadState:
    row = conn.execute("SELECT * FROM slack_read WHERE id = 1").fetchone()
    if row is None:
        return ReadState()
    return ReadState(
        row["mode"], row["last_ts"], row["walk_latest"], row["walk_newest"], row["search_after"],
        row["search_cursor"], row["started_at"], row["finished_at"],
    )  # fmt: skip


def store_read_state(conn: sqlite3.Connection, state: ReadState) -> None:
    conn.execute(
        """
        INSERT INTO slack_read (id, mode, last_ts, walk_latest, walk_newest, search_after, search_cursor, started_at,
                                finished_at)
        VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            mode = excluded.mode, last_ts = excluded.last_ts, walk_latest = excluded.walk_latest,
            walk_newest = excluded.walk_newest, search_after = excluded.search_after,
            search_cursor = excluded.search_cursor, started_at = excluded.started_at,
            finished_at = excluded.finished_at
        """,
        (
            state.mode, state.last_ts, state.walk_latest, state.walk_newest, state.search_after, state.search_cursor,
            state.started_at, state.finished_at,
        ),
    )  # fmt: skip


def record_read_outcome(conn: sqlite3.Connection, error: str | None, now: int) -> None:
    if error is None:
        conn.execute(
            "UPDATE slack_read SET ok_at = ?, error = NULL, error_at = NULL, failures = 0 WHERE id = 1", (now,)
        )
    else:
        conn.execute(
            "UPDATE slack_read SET error = ?, error_at = ?, failures = coalesce(failures, 0) + 1 WHERE id = 1",
            (error, now),
        )


def read_with_token(conn: sqlite3.Connection, config: Config, token: str, team_url: str | None, now: int) -> str:
    """One read of the channel. Returns the workspace URL, read once per process."""
    fetch = build_token_fetch(token)
    if team_url is None:
        team_url = str(fetch("auth.test", {}).get("url") or "")
        if not team_url:
            raise SlackError("auth.test gave no workspace URL")
    posts, state, error = read_history(fetch, config.review_channel_id, team_url, load_read_state(conn), now)
    with transaction(conn):
        record_seen_posts(conn, posts, now)
        store_read_state(conn, state)
        record_read_outcome(conn, error, now)
    return team_url


def claim_connector_read(conn: sqlite3.Connection, now: int) -> int | None:
    """One search at a time, at most one per CONNECTOR_EVERY_S: each starts a model session. A run that never said it
    finished is taken as gone after the longest a run can take."""
    with transaction(conn):
        state = load_read_state(conn)
        started = state.started_at or 0
        running = state.started_at is not None and (state.finished_at or 0) < started
        if now - started < CONNECTOR_EVERY_S * 1000 or (running and now - started < CONNECTOR_RUN_MAX_MS):
            return None
        store_read_state(conn, replace(state, mode=CONNECTOR, started_at=now))
    return now


def run_connector_read(conn: sqlite3.Connection, config: Config, claim: int) -> None:
    """The detached reader. It writes only while its claim stands, so one back from a stop, or overtaken by a token
    read, changes nothing."""
    state = load_read_state(conn)
    if state.started_at != claim:
        return
    channel = config.review_channel_id
    request = build_search_request(channel, state, now_ms())
    page: SearchPage | None = None
    error: str | None = None
    for attempt in range(SEARCH_ATTEMPTS):
        if attempt:
            # A model can still answer in text without calling the tool; one more try costs a few seconds.
            time.sleep(CONNECTOR_RETRY_S)
            if load_read_state(conn).started_at != claim:
                return
        try:
            done = subprocess.run(
                build_search_command(config, request),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                env=build_send_env(),
                cwd=state_dir(),
                stdin=subprocess.DEVNULL,
                timeout=SEARCH_TIMEOUT_S,
                check=False,
            )
            page, error = map_events_to_search(done.stdout, channel, request, now_ms()), None
            break
        except subprocess.TimeoutExpired:
            error = "the search timed out"
            break
        except OSError as exc:
            error = f"claude did not start: {type(exc).__name__}"
            break
        except SlackError as exc:
            error = str(exc)
            if error != NEVER_CALLED:
                break
    now = now_ms()
    with transaction(conn):
        current = load_read_state(conn)
        if current.started_at != claim:
            return
        if page is None:
            # A cursor that failed is not tried again; the search starts over from its own floor.
            store_read_state(conn, replace(current, search_cursor=None, finished_at=now))
            record_read_outcome(conn, error, now)
            return
        record_seen_posts(conn, page.posts, now)
        store_read_state(conn, replace(map_search_to_state(page, request, current), finished_at=now))
        record_read_outcome(conn, None, now)


def spawn_connector_read(claim: int) -> None:
    subprocess.Popen(
        [sys.executable, "-m", "bridge", "_review-slack-read", str(claim)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def read_slack_once(
    conn: sqlite3.Connection,
    config: Config,
    team_url: str | None,
    now: int,
    spawn: Callable[[int], None] = spawn_connector_read,
) -> str | None:
    """A token read every call when ~/.secrets/slack holds one; else a detached connector search when one is due.
    Returns the workspace URL to keep for the next call."""
    try:
        token = read_token(token_path())
        if token is not None:
            return read_with_token(conn, config, token, team_url, now)
    except SlackError as exc:
        with transaction(conn):
            store_read_state(conn, replace(load_read_state(conn), mode=TOKEN, started_at=None))
            record_read_outcome(conn, str(exc), now)
        return team_url
    claim = claim_connector_read(conn, now)
    if claim is not None:
        spawn(claim)
    return team_url


def map_db_to_slack_freshness(conn: sqlite3.Connection, config: Config) -> dict[str, object]:
    row = conn.execute("SELECT mode, ok_at, error, error_at, failures FROM slack_read WHERE id = 1").fetchone()
    mode = row["mode"] if row else None
    return {
        "enabled": config.chat_adapter == "slack" and bool(config.review_channel_id),
        "mode": mode,
        "every_s": CONNECTOR_EVERY_S if mode == CONNECTOR else TOKEN_EVERY_S,
        "ok_at": row["ok_at"] if row else None,
        "error": row["error"] if row else None,
        "error_at": row["error_at"] if row else None,
        "failures": int(row["failures"] or 0) if row else 0,
    }
