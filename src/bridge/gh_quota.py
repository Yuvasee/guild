import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from bridge.config import state_dir
from bridge.errlog import log_error

CACHE_TTL_S = 30
FETCH_TIMEOUT_S = 3
SPENDING_SUBCOMMANDS = frozenset({"api", "run", "pr", "issue", "repo", "release", "search"})
_CACHE_FILE = "gh-quota.json"


@dataclass(frozen=True)
class CoreQuota:
    remaining: int
    limit: int
    reset_at: int


def is_quota_spending(words: list[str]) -> bool:
    """words: one command, program first. `gh api rate_limit` is the free read the guard itself makes."""
    if not words or words[0] != "gh":
        return False
    positional = [w for w in words[1:] if not w.startswith("-")]
    if not positional or positional[0] not in SPENDING_SUBCOMMANDS:
        return False
    return positional[:2] != ["api", "rate_limit"] and positional[:2] != ["api", "/rate_limit"]


def read_core_quota() -> CoreQuota | None:
    try:
        done = subprocess.run(
            ["gh", "api", "rate_limit"], capture_output=True, text=True, timeout=FETCH_TIMEOUT_S, check=False
        )
        core = json.loads(done.stdout)["resources"]["core"]
        return CoreQuota(int(core["remaining"]), int(core["limit"]), int(core["reset"]))
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None


def find_quota_refusal(
    reserve: int, fetch: Callable[[], CoreQuota | None] = read_core_quota, now: float | None = None
) -> str | None:
    """A failed read is cached like a good one, so a broken gh costs one timeout per CACHE_TTL_S, and logged once per
    run of failures; the command is then allowed."""
    if reserve <= 0:
        return None
    now = time.time() if now is None else now
    path = state_dir() / _CACHE_FILE
    previous: dict[str, object] = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        previous = cast(dict[str, object], loaded) if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        pass
    at = previous.get("at")
    if isinstance(at, (int, float)) and 0 <= now - at < CACHE_TTL_S:
        return _map_cache_to_refusal(previous, reserve)
    quota = fetch()
    entry: dict[str, object] = (
        {"at": now, "failed": True}
        if quota is None
        else {"at": now, "remaining": quota.remaining, "limit": quota.limit, "reset": quota.reset_at}
    )
    if quota is None and not previous.get("failed"):
        log_error("gh guard", "GET /rate_limit failed; gh commands are allowed until it answers")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entry), encoding="utf-8")
    except OSError:
        pass
    return _map_cache_to_refusal(entry, reserve)


def _map_cache_to_refusal(entry: dict[str, object], reserve: int) -> str | None:
    remaining, limit, reset = entry.get("remaining"), entry.get("limit"), entry.get("reset")
    if not (isinstance(remaining, int) and isinstance(limit, int) and isinstance(reset, int)):
        return None
    if remaining >= reserve:
        return None
    when = time.strftime("%H:%M", time.gmtime(reset))
    return f"gh guard: core quota {remaining}/{limit} < {reserve} reserve, resets {when} UTC; retry after reset"
