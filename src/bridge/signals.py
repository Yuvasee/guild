import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import cast

from bridge.refs import map_ref_to_label

_DONE_ISSUE_TYPES = frozenset({"completed", "canceled"})
_READY_MERGE_STATES = frozenset({"CLEAN", "HAS_HOOKS"})
_RED = frozenset({"FAILURE", "ERROR"})
_FINISHED_PR_STATUSES = frozenset({"merged", "closed"})
ISSUE_QUIET_MS = 2 * 3_600_000
DEFAULT_REVIEW_STATES = ("review", "qa", "test", "staging", "verif")


@dataclass(frozen=True)
class RefStatus:
    label: str
    tone: str
    """good, bad, warn, busy, done or neutral; the page maps tones to colours."""
    title: str


@dataclass(frozen=True)
class Signal:
    key: str
    """Stable while the underlying state is unchanged, so a dismissal sticks until something changes."""
    severity: str
    kind: str
    text: str
    url: str
    surface: str | None
    handle: str | None


@dataclass(frozen=True)
class SessionRefs:
    surface: str | None
    handle: str | None
    branch: str | None
    refs: list[str]
    first_seen: dict[str, int] = field(default_factory=dict[str, int])
    """When this session first touched each ref; the earliest toucher is the likely worker."""


def map_enrichment_to_status(ref: str, data: dict[str, object] | None) -> RefStatus | None:
    if not data:
        return None
    if "state" not in data:
        return RefStatus("unavailable", "neutral", str(data.get("error", ""))) if "error" in data else None
    status = map_pr_to_status(data) if ref.startswith("pr:") else map_issue_to_status(data)
    if ref.startswith("pr:") and data.get("mine") is not True and data.get("state") == "OPEN":
        # Someone else's PR: its state is for them to act on, so no colour pulls the user's eye.
        status = RefStatus(status.label, "neutral", f"{status.title}\nby {data.get('author') or 'someone else'}")
    if data.get("stale"):
        detail = f"\nlast refresh failed: {data['error']}" if "error" in data else "\nnot refreshed recently"
        return RefStatus(status.label + " (stale)", "neutral", status.title + detail)
    return status


def map_issue_to_status(data: dict[str, object]) -> RefStatus:
    state_type = str(data.get("state_type") or "")
    tone = {"started": "busy", "completed": "done", "canceled": "neutral"}.get(state_type, "neutral")
    assignee = f" · {data['assignee']}" if data.get("assignee") else ""
    return RefStatus(label=str(data.get("state") or ""), tone=tone, title=f"{data.get('title', '')}{assignee}")


def map_pr_to_status(data: dict[str, object]) -> RefStatus:
    title = str(data.get("title") or "")
    state = data.get("state")
    if state == "MERGED":
        return RefStatus("merged", "done", title)
    if state == "CLOSED":
        return RefStatus("closed", "neutral", title)
    checks, review, merge_state = data.get("checks"), data.get("review"), data.get("merge_state")
    draft = "draft · " if data.get("draft") else ""
    if checks in _RED:
        return RefStatus(f"{draft}CI failing", "bad", title)
    if merge_state == "DIRTY" or data.get("mergeable") == "CONFLICTING":
        return RefStatus(f"{draft}conflicts", "bad", title)
    if draft:
        return RefStatus("draft", "neutral", title)
    if data.get("in_merge_queue"):
        return RefStatus("in merge queue", "busy", title)
    if review == "CHANGES_REQUESTED":
        return RefStatus("changes requested", "warn", title)
    if checks in ("PENDING", "EXPECTED"):
        return RefStatus("CI running", "busy", title)
    if data.get("stacked"):
        return RefStatus("stacked", "neutral", f"{title}\nbase: {data.get('base')} (another PR)")
    if merge_state in _READY_MERGE_STATES:
        return RefStatus("ready", "good", title)
    if merge_state == "BEHIND":
        return RefStatus("behind base", "warn", title)
    if review == "APPROVED":
        return RefStatus("approved, blocked", "neutral", f"{title}\nGitHub reports {merge_state}")
    return RefStatus("in review", "neutral", title)


def find_signals(
    sessions: list[SessionRefs],
    enrichment: dict[str, dict[str, object]],
    repo_labels: Mapping[str, str] | None = None,
    at_ms: int | None = None,
    review_states: tuple[str, ...] = DEFAULT_REVIEW_STATES,
) -> list[Signal]:
    """PR states that ask for the user's hand (ready to merge, red CI, changes), then issues that look finished."""
    signals: dict[str, Signal] = {}
    pr_refs = sorted({r for s in sessions for r in s.refs if r.startswith("pr:")})
    for ref in pr_refs:
        data = enrichment.get(ref)
        if not data or "state" not in data:
            continue
        session = pick_owner(ref, data, sessions)
        for signal in _pr_signals(ref, data, session, map_ref_to_label(ref, repo_labels or {})):
            signals.setdefault(signal.key, signal)
    for ref in sorted({r for s in sessions for r in s.refs if r.startswith("issue:")}):
        signal = _issue_done_signal(ref, enrichment.get(ref), sessions, at_ms, enrichment, review_states)
        if signal:
            signals.setdefault(signal.key, signal)
    return list(signals.values())


def _issue_done_signal(
    ref: str,
    data: dict[str, object] | None,
    sessions: list[SessionRefs],
    at_ms: int | None,
    enrichment: dict[str, dict[str, object]],
    review_states: tuple[str, ...],
) -> Signal | None:
    """An issue still open although every PR its tracker links to it is finished and at least one was meant to close it.

    One merged PR proves little: issues often take several. A quiet period leaves room for the next PR.
    """
    if not data or data.get("stale") or "error" in data or not data.get("linked_complete"):
        return None
    if data.get("state_type") in _DONE_ISSUE_TYPES:
        return None
    state = str(data.get("state") or "")
    # States like In Review or QA are where a merged PR is expected to wait; not bookkeeping debt.
    if any(word.lower() in state.lower() for word in review_states):
        return None
    linked = [
        cast(dict[str, object], p) for p in cast(list[object], data.get("linked_prs") or []) if isinstance(p, dict)
    ]
    if not linked or any(p.get("status") not in _FINISHED_PR_STATUSES for p in linked):
        return None
    # The tracker's copy of a PR can lag GitHub: our own fresh GitHub data wins when it says a PR is open.
    for p in linked:
        github = enrichment.get(str(p.get("ref")))
        if github and not github.get("stale") and github.get("state") == "OPEN":
            return None
    if any(p.get("status") == "merged" and not isinstance(p.get("merged_at"), int) for p in linked):
        return None
    closing = [p for p in linked if p.get("closes") and p.get("status") == "merged"]
    if not closing:
        return None
    last_merge = max((int(str(p["merged_at"])) for p in linked if isinstance(p.get("merged_at"), int)), default=0)
    if at_ms is not None and at_ms - last_merge < ISSUE_QUIET_MS:
        return None
    key = ref.removeprefix("issue:")
    merged = sum(1 for p in linked if p.get("status") == "merged")
    text = f"{key} {data.get('title', '')}: {merged} of {len(linked)} linked PRs merged, none open; still {state}"
    owner = pick_issue_owner(ref, sessions)
    # Keyed by issue and state only: a dismissal holds until the issue moves, whatever PRs merge meanwhile.
    return Signal(
        f"{ref}:done?:{state}", "info", "drift", text, str(data.get("url") or ""), owner.surface, owner.handle
    )


def pick_issue_owner(ref: str, sessions: list[SessionRefs]) -> SessionRefs:
    """The session on a branch named after the issue, else the one that mentioned it first."""
    key = re.escape(ref.removeprefix("issue:").lower())
    touching = [s for s in sessions if ref in s.refs]
    on_branch = [s for s in touching if s.branch and re.search(rf"(?<![a-z0-9]){key}(?![0-9])", s.branch.lower())]
    if on_branch:
        return on_branch[0]
    return min(touching, key=lambda s: s.first_seen.get(ref, 1 << 62))


def pick_owner(ref: str, data: dict[str, object], sessions: list[SessionRefs]) -> SessionRefs:
    """The session on the PR's head branch, else the one that touched the PR first (not whoever mentioned it last)."""
    touching = [s for s in sessions if ref in s.refs]
    head = str(data.get("head") or "")
    on_branch = [s for s in touching if head and s.branch == head]
    if on_branch:
        return on_branch[0]
    return min(touching, key=lambda s: s.first_seen.get(ref, 1 << 62))


def map_signal_to_dict(signal: Signal) -> dict[str, object]:
    return asdict(signal)


def _pr_signals(ref: str, data: dict[str, object], session: SessionRefs, number: str) -> list[Signal]:
    title = str(data.get("title") or "")
    url = str(data.get("url") or "")
    # Keys follow the head commit, so a dismissed alert comes back for a new push.
    oid = str(data.get("head_oid") or data.get("updated_at") or "")
    found: list[Signal] = []

    def add(kind: str, severity: str, text: str, key: str, link: str = url) -> None:
        found.append(Signal(key, severity, kind, text, link, session.surface, session.handle))

    if data.get("state") == "OPEN" and data.get("mine") is True:
        if data.get("checks") in _RED:
            add("ci", "info" if data.get("draft") else "action", f"{number} {title}", f"{ref}:ci:{oid}")
        elif (
            not data.get("stale")
            and not data.get("draft")
            and not data.get("stacked")
            and not data.get("in_merge_queue")
            and data.get("merge_state") in _READY_MERGE_STATES
            and data.get("checks") in ("SUCCESS", None)
        ):
            add("ready", "action", f"{number} {title}", f"{ref}:ready:{oid}")
        elif data.get("review") == "CHANGES_REQUESTED" and not data.get("draft"):
            add("changes", "info", f"{number} {title}", f"{ref}:changes:{oid}")
    return found
