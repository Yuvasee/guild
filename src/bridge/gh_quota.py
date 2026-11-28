import json
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from bridge.config import state_dir
from bridge.errlog import log_error

CACHE_TTL_S = 30
FETCH_TIMEOUT_S = 3
SPENDING_SUBCOMMANDS = frozenset({"api", "run", "pr", "issue", "repo", "release", "search"})
_CACHE_FILE = "gh-quota.json"
_VALUE_OPTIONS = frozenset({"-R", "--repo", "-H", "--header"})


@dataclass(frozen=True)
class CoreQuota:
    remaining: int
    limit: int
    reset_at: int


def is_quota_spending(words: list[str]) -> bool:
    """words: one command, program first. `gh api rate_limit` is the free read the guard itself makes."""
    if not words or words[0] != "gh":
        return False
    positional: list[str] = []
    skip = False
    for word in words[1:]:
        if skip:
            skip = False
        elif word in _VALUE_OPTIONS:
            skip = True
        elif not word.startswith("-"):
            positional.append(word)
    if not positional or positional[0] not in SPENDING_SUBCOMMANDS:
        return False
    return positional[:2] not in (["api", "rate_limit"], ["api", "/rate_limit"])


def read_core_quota() -> CoreQuota | None:
    return read_quota("core")


def read_quota(bucket: str) -> CoreQuota | None:
    """bucket: a key of rate_limit's resources, "core" for REST, "graphql" for GraphQL points."""
    try:
        done = subprocess.run(
            ["gh", "api", "rate_limit"], capture_output=True, text=True, timeout=FETCH_TIMEOUT_S, check=False
        )
        found = json.loads(done.stdout)["resources"][bucket]
        return CoreQuota(int(found["remaining"]), int(found["limit"]), int(found["reset"]))
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None


def find_quota_refusal(
    reserve: int, fetch: Callable[[], CoreQuota | None] = read_core_quota, now: float | None = None
) -> str | None:
    """A failed read is cached like a good one, so a broken gh costs one timeout per CACHE_TTL_S, and logged once per
    run of failures; the command is then allowed. A cached result whose reset has passed is stale."""
    if reserve <= 0:
        return None
    now = time.time() if now is None else now
    path = state_dir() / _CACHE_FILE
    previous = _read_cache(path)
    at, reset = previous.get("at"), previous.get("reset")
    fresh = isinstance(at, (int, float)) and 0 <= now - at < CACHE_TTL_S
    if fresh and (previous.get("failed") or (isinstance(reset, int) and reset > now)):
        return _map_cache_to_refusal(previous, reserve)
    quota = fetch()
    entry: dict[str, object] = (
        {"at": now, "failed": True}
        if quota is None
        else {"at": now, "remaining": quota.remaining, "limit": quota.limit, "reset": quota.reset_at}
    )
    if quota is None and not previous.get("failed"):
        log_error("gh guard", "GET /rate_limit failed; gh commands are allowed until it answers")
    entry = _keep_lower_in_window(entry, _read_cache(path))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{_CACHE_FILE}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(entry), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass
    return _map_cache_to_refusal(entry, reserve)


def _read_cache(path: Path) -> dict[str, object]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return cast(dict[str, object], loaded) if isinstance(loaded, dict) else {}


def _keep_lower_in_window(entry: dict[str, object], other: dict[str, object]) -> dict[str, object]:
    """A slower read may finish after a newer one: within one reset window the quota only falls, so the lower
    remaining is the later truth; a later window wins outright."""
    remaining, reset = entry.get("remaining"), entry.get("reset")
    o_remaining, o_reset = other.get("remaining"), other.get("reset")
    if not all(isinstance(v, int) for v in (remaining, reset, o_remaining, o_reset)):
        return entry
    assert isinstance(remaining, int) and isinstance(reset, int)
    assert isinstance(o_remaining, int) and isinstance(o_reset, int)
    if o_reset > reset or (o_reset == reset and o_remaining < remaining):
        return {**other, "at": entry["at"]}
    return entry


def _map_cache_to_refusal(entry: dict[str, object], reserve: int) -> str | None:
    remaining, limit, reset = entry.get("remaining"), entry.get("limit"), entry.get("reset")
    if not (isinstance(remaining, int) and isinstance(limit, int) and isinstance(reset, int)):
        return None
    if remaining >= reserve:
        return None
    when = time.strftime("%H:%M", time.gmtime(reset))
    return f"gh guard: core quota {remaining}/{limit} < {reserve} reserve, resets {when} UTC; retry after reset"
