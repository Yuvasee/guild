import json
import os
import re
import secrets
import sqlite3
import subprocess
from dataclasses import dataclass
from typing import cast

from bridge.board import Party

DEFAULT_TTL_S = 7200
GH_TIMEOUT_S = 8
CLAIM_REMINDER_EVERY_MS = 6 * 3_600_000
CLAIM_REMINDERS_AT_ONCE = 3

_KINDS = ("pr", "branch", "env", "path", "issue")
_REPO_RE = r"[\w.-]+/[\w.-]+"


class ClaimError(Exception):
    pass


@dataclass(frozen=True)
class ResourceKey:
    canonical: str
    alias: str | None
    note: str = ""


@dataclass(frozen=True)
class Holder:
    resource: str
    alias: str | None
    holder_handle: str
    holder_conversation: str
    reason: str | None
    acquired_at: int
    expires_at: int
    live: bool


@dataclass(frozen=True)
class ClaimOutcome:
    status: str
    """"acquired", "already-yours", "taken-over", "held" or "held-suspect"."""
    token: str | None
    holder: Holder | None
    key: ResourceKey


def parse_resource(text: str, default_repo: str | None) -> tuple[str, str]:
    """Splits and normalises everything except PR/branch aliasing, which needs GitHub."""
    kind, sep, value = text.partition(":")
    if not sep or kind not in _KINDS or not value:
        raise ClaimError(f"resource must be one of {', '.join(k + ':…' for k in _KINDS)}; got {text!r}")
    if kind == "pr":
        match = re.fullmatch(rf"(?:({_REPO_RE}))?#?(\d+)", value)
        if match is None:
            raise ClaimError("pr resource is pr:<owner>/<repo>#<n> or pr:<n> inside a repo")
        repo = match.group(1) or default_repo
        if repo is None:
            raise ClaimError("pr:<n> needs a GitHub repo in the current directory; use pr:<owner>/<repo>#<n>")
        return kind, f"{repo.lower()}#{match.group(2)}"
    if kind == "branch":
        match = re.fullmatch(rf"(?:({_REPO_RE}):)?(.+)", value)
        assert match is not None
        repo = match.group(1) or default_repo
        if repo is None:
            raise ClaimError("branch:<name> needs a GitHub repo in the current directory")
        return kind, f"{repo.lower()}:{match.group(2)}"
    if kind == "env":
        return kind, value.lower()
    if kind == "path":
        return kind, os.path.realpath(os.path.expanduser(value))
    return kind, value.upper()


def resolve_resource(text: str, default_repo: str | None, offline: bool) -> ResourceKey:
    """A PR and its head branch are one resource; GitHub decides which PR a branch belongs to."""
    kind, value = parse_resource(text, default_repo)
    if kind not in ("pr", "branch"):
        return ResourceKey(canonical=f"{kind}:{value}", alias=None)
    if offline:
        return ResourceKey(canonical=f"{kind}:{value}", alias=None, note="offline: PR/branch aliasing skipped")
    if kind == "pr":
        repo, number = value.split("#")
        data = _gh_json(["pr", "view", number, "-R", repo, "--json", "headRefName,headRepository,headRepositoryOwner"])
        fields = cast(dict[str, object], data) if isinstance(data, dict) else {}
        head = fields.get("headRefName")
        head_repo = _head_repo(fields) or repo
        if not isinstance(head, str):
            raise ClaimError(f"GitHub returned no head branch for {repo}#{number}")
        return ResourceKey(canonical=f"pr:{value}", alias=f"branch:{head_repo.lower()}:{head}")
    repo, branch = value.split(":", 1)
    data = _gh_json(["pr", "list", "-R", repo, "--head", branch, "--state", "open", "--json", "number"])
    items = cast(list[object], data) if isinstance(data, list) else []
    numbers = [cast(dict[str, object], item).get("number") for item in items if isinstance(item, dict)]
    if len(numbers) == 1 and isinstance(numbers[0], int):
        return ResourceKey(canonical=f"pr:{repo}#{numbers[0]}", alias=f"branch:{value}")
    return ResourceKey(canonical=f"branch:{value}", alias=None)


def _head_repo(fields: dict[str, object]) -> str | None:
    """A PR from a fork lives on the fork's branch, not on a same-named branch of the base repo."""
    owner = fields.get("headRepositoryOwner")
    repository = fields.get("headRepository")
    login = cast(dict[str, object], owner).get("login") if isinstance(owner, dict) else None
    name = cast(dict[str, object], repository).get("name") if isinstance(repository, dict) else None
    return f"{login}/{name}" if isinstance(login, str) and isinstance(name, str) else None


def acquire(
    conn: sqlite3.Connection,
    me: Party,
    key: ResourceKey,
    reason: str | None,
    ttl_s: int,
    at_ms: int,
    force: bool = False,
) -> ClaimOutcome:
    holder = find_holder(conn, key, at_ms)
    if holder is None:
        token = _insert(conn, me, key, reason, ttl_s, at_ms)
        return ClaimOutcome("acquired", token, None, key)
    if holder.holder_conversation == me.conversation_id:
        if key.alias and holder.alias is None and holder.resource != key.canonical:
            # The branch got a PR since it was claimed: the claim now covers both names.
            conn.execute(
                "UPDATE claims SET resource = ?, alias = ? WHERE resource = ?",
                (key.canonical, key.alias, holder.resource),
            )
            conn.execute(
                "UPDATE claim_log SET resource = ? WHERE resource = ? AND ended_at IS NULL",
                (key.canonical, holder.resource),
            )
        row = conn.execute(
            "SELECT token, resource, alias FROM claims WHERE resource IN (?, ?)", (key.canonical, holder.resource)
        ).fetchone()
        stored = ResourceKey(canonical=str(row["resource"]), alias=row["alias"], note=key.note)
        return ClaimOutcome("already-yours", str(row["token"]), holder, stored)
    if holder.live and not force:
        return ClaimOutcome("held-suspect" if holder.expires_at < at_ms else "held", None, holder, key)
    # The holder is gone (or the user forces it): take over, comparing against the row we saw.
    deleted = conn.execute(
        "DELETE FROM claims WHERE resource = ? AND holder_conversation = ? AND acquired_at = ?",
        (holder.resource, holder.holder_conversation, holder.acquired_at),
    )
    if deleted.rowcount == 0:
        raise ClaimError("the claim changed while taking it over; retry")
    _end_log(conn, holder.resource, "taken over", at_ms)
    token = _insert(conn, me, key, reason, ttl_s, at_ms)
    return ClaimOutcome("taken-over", token, holder, key)


def renew(conn: sqlite3.Connection, resource: str, token: str, ttl_s: int, at_ms: int) -> int:
    expires = at_ms + ttl_s * 1000
    cursor = conn.execute(
        "UPDATE claims SET renewed_at = ?, expires_at = ? WHERE (resource = ? OR alias = ?) AND token = ?",
        (at_ms, expires, resource, resource, token),
    )
    if cursor.rowcount == 0:
        raise ClaimError("no claim with that resource and token; see `bridge claims --mine`")
    return expires


def release(conn: sqlite3.Connection, resource: str, token: str, at_ms: int) -> None:
    row = conn.execute(
        "DELETE FROM claims WHERE (resource = ? OR alias = ?) AND token = ? RETURNING resource",
        (resource, resource, token),
    ).fetchone()
    if row is None:
        raise ClaimError("no claim with that resource and token; see `bridge claims --mine`")
    _end_log(conn, str(row[0]), "released", at_ms)


def take_expired_claim_reminder(conn: sqlite3.Connection, conversation_id: str, at_ms: int) -> str | None:
    """Expiry does not free a live holder's claim, so the holder has to decide; a few at a time, at most every 6 h."""
    rows = conn.execute(
        """
        SELECT resource FROM claims
        WHERE holder_conversation = ? AND expires_at < ? AND coalesce(reminded_at, 0) < ?
        ORDER BY expires_at LIMIT ?
        """,
        (conversation_id, at_ms, at_ms - CLAIM_REMINDER_EVERY_MS, CLAIM_REMINDERS_AT_ONCE),
    ).fetchall()
    if not rows:
        return None
    resources = [str(r["resource"]) for r in rows]
    marks = ",".join("?" * len(resources))
    conn.execute(f"UPDATE claims SET reminded_at = ? WHERE resource IN ({marks})", (at_ms, *resources))
    return (
        f"[bridge] Your claim on {', '.join(resources)} is past its expiry and still blocks others. "
        "Release what you are done with (`bridge release <resource> --token T`; tokens: `bridge claims --mine`), "
        "renew the rest."
    )


def find_holder(conn: sqlite3.Connection, key: ResourceKey, at_ms: int) -> Holder | None:
    names = [key.canonical] + ([key.alias] if key.alias else [])
    marks = ",".join("?" * len(names))
    row = conn.execute(
        f"""
        SELECT c.*, EXISTS (
            SELECT 1 FROM instances i WHERE i.conversation_id = c.holder_conversation AND i.ended_at IS NULL
        ) AS live
        FROM claims c WHERE c.resource IN ({marks}) OR c.alias IN ({marks})
        """,
        [*names, *names],
    ).fetchone()
    return map_row_to_holder(row) if row else None


def list_claims(conn: sqlite3.Connection, conversation_id: str | None = None) -> list[sqlite3.Row]:
    where = "WHERE c.holder_conversation = ?" if conversation_id else ""
    return conn.execute(
        f"""
        SELECT c.*, EXISTS (
            SELECT 1 FROM instances i WHERE i.conversation_id = c.holder_conversation AND i.ended_at IS NULL
        ) AS live
        FROM claims c {where} ORDER BY c.acquired_at
        """,
        (conversation_id,) if conversation_id else (),
    ).fetchall()


def map_row_to_holder(row: sqlite3.Row) -> Holder:
    return Holder(
        resource=str(row["resource"]),
        alias=row["alias"],
        holder_handle=str(row["holder_handle"]),
        holder_conversation=str(row["holder_conversation"]),
        reason=row["reason"],
        acquired_at=int(row["acquired_at"]),
        expires_at=int(row["expires_at"]),
        live=bool(row["live"]),
    )


def _end_log(conn: sqlite3.Connection, resource: str, kind: str, at_ms: int) -> None:
    conn.execute(
        "UPDATE claim_log SET ended_at = ?, end_kind = ? WHERE resource = ? AND ended_at IS NULL",
        (at_ms, kind, resource),
    )


def _insert(conn: sqlite3.Connection, me: Party, key: ResourceKey, reason: str | None, ttl_s: int, at_ms: int) -> str:
    token = secrets.token_hex(6)
    try:
        conn.execute(
            """
            INSERT INTO claims (resource, alias, token, holder_conversation, holder_handle, reason,
                                acquired_at, renewed_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key.canonical,
                key.alias,
                token,
                me.conversation_id,
                me.handle,
                reason,
                at_ms,
                at_ms,
                at_ms + ttl_s * 1000,
            ),
        )
        conn.execute(
            "INSERT INTO claim_log (resource, holder_conversation, holder_handle, reason, taken_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (key.canonical, me.conversation_id, me.handle, reason, at_ms),
        )
    except sqlite3.IntegrityError as exc:
        raise ClaimError("the resource was claimed concurrently; retry") from exc
    return token


def _gh_json(args: list[str]) -> object:
    try:
        completed = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=GH_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClaimError(f"cannot reach GitHub to resolve PR/branch aliases ({exc}); retry or pass --offline") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "unknown error"
        raise ClaimError(f"gh failed to resolve the PR/branch alias: {detail}; retry or pass --offline")
    result: object = json.loads(completed.stdout or "null")
    return result
