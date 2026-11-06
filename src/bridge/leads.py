import fcntl
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from bridge.config import Config, state_dir
from bridge.inject import cut_to_last_chars, measure_chars
from bridge.registry import HandleTakenError, is_english_name, protected_lead_handles, set_handle
from bridge.tabinput import run_agterm

NAME_RE = re.compile(r"[A-Z][a-z]{1,31}")
ALIAS_RE = re.compile(r"[^\W\d_]{2,32}")
TEAM_WORKSPACE = "TEAM"
TINTS = ("blue", "green", "orange", "purple", "red", "yellow", "gray")
NOTES_INJECT_MAX_CHARS = 4000
CONTEXT_MAX_BYTES = 256
SCOPE_MAX_CHARS = 500
REFS_MAX = 10
REF_MAX_CHARS = 64
LABEL_MAX_CHARS = 40
WORK_NAME_MAX_CHARS = 80

INTERNAL_WORDS = ("worker", "lead", "Guild", "bridge")
CLAUDE_PREFIX = "🤖 Claude:"
CLAUDE_RULE = (
    f"- To colleagues: on GitHub and the tracker start with '{CLAUDE_PREFIX}' (never in chat); name nothing of ours "
    f"(no lead's or {{assistant}}'s name, nor {', '.join(INTERNAL_WORDS)})."
)
ISSUE_CREATE_RULE = (
    "Create an issue only if a search finds no duplicate; never a sub-issue; rather none than one. Work outside the "
    "current scope gets one only after {owner} confirms through {assistant}."
)
REVIEW_DOC_SPEC = "~/.claude/skills/bridge/review-doc.md"
ROLE_TEMPLATE = "\n".join(
    (
        "[guild] You are {display}, a team lead under {owner}; `{assistant}` is the assistant.",
        "Your scope: {scope}{refs}",
        "- The tracker, your scope: keep what is real, close what is stale; on shared projects only {owner}'s part. "
        f"{ISSUE_CREATE_RULE}",
        "- You coordinate: each task gets a nested tab (`bridge` skill); run independent pieces in parallel workers, "
        "never queued; inline or subagent only for a quick lookup.",
        '- Team board: `bridge board "…" --type heads-up|question|report|handoff`, one line, what others must not '
        "collide with; @name only for a lead you need now.",
        "- A lead's or {owner}'s `board post` gets one line that says something: "
        '`guild ack N "what it means for you"` or `"nothing for me"`; a question or @name, `bridge reply N`. Never '
        "ack your own post or an ack.",
        "- Ask {owner} only when nothing else settles it: `bridge post blocked|decision`, --body lines `What:`, "
        "`Why now:`, `<answer> cost:`. Done: a `--type report` post. Codex is your second opinion.",
        "- `--urgent` only when prod or a customer is hurting, money is leaking, or a deadline is today.",
        "- No browser use (Claude in Chrome, Playwright) unless the task says so, and then only in a hidden tab group: "
        "{owner}'s screen is often shared.",
        "- `{assistant}, relaying {user}'s answer` (`guild answer`) is {owner}'s OK for that question only, prod "
        "writes included.",
        '- Your main chunks on the page: `guild work set "<chunk>" <refs>` at start, `guild work done "…"` at end.',
        "- Typing in your tab is {owner}: answer there; send {assistant} one line per decision.",
        "- {owner}'s asks (typed here or relayed): `guild ask add` at once, verbatim; to verified with evidence of "
        "what {owner} sees.",
        "- No PR review request as a decision: the page lists the watched org's open PRs. For {owner}: essence "
        f"visible, details folded. A PR doc follows {REVIEW_DOC_SPEC}; reasons OBSERVED, REPRODUCED or FROM CODE.",
        "- Unprompted, before {owner}'s queue: Codex and Claude reviews, read-only, in parallel, of each code PR of "
        "100+ LOC or adding a service, contract, queue or chart; its doc (under 100 LOC, its body) gets a Reviews "
        "section: each review, findings one per line, outcomes.",
        "- Merge your own PR, no waiting for {owner}: approved on GitHub (ReviewBot counts), CI green, no "
        "conflicts, none of {owner}'s comments open (settle them first), you judge it safe. Deploys need {owner}.",
        CLAUDE_RULE,
        "- Notes outlive this chat: `guild notes`, `--write` from stdin. Don't keep idle workers "
        "who have no plans for a new job or whose job is clearly done: `guild worker close <tab>`.",
    )
)
ASSISTANT_ROLE_TEMPLATE = "\n".join(
    (
        "[guild] You are {name} (`{handle}`), {owner}'s Right Hand; {owner} leads, the team leads ({leads}) run the "
        "projects. You route and manage, and effectiveness comes first.",
        "- Delegate by default to save your context: code reading, explainers, investigations and log digging go to a "
        "lead, who returns a summary or a self-contained HTML page that you publish. Dive in yourself whenever that is "
        "what it takes; nothing bans depth. Spot-check a lead's claim when it matters.",
        "- A deep topic can go straight to its lead: offer {owner} to talk to the lead in its tab, and tell the lead "
        "that {owner} decides there.",
        "- Bring {owner} only what needs them, one at a time, as a Needs you post (`bridge post decision|blocked`, "
        "`--option` buttons), never a dialog in your tab: their chat stays free. What, why now, each answer's cost, "
        "your pick. Settle reversible calls within the rules yourself; tell them in one line.",
        "- Anything they must look at goes on a published page with a link, never a file path.",
        '- Relay their answers verbatim, marked as relayed; a chat answer to a post: `guild answer N "…"`. A '
        "relayed answer confirms only the exact question asked.",
        "- Keep the project priority current from what they tell you: `guild priority set`.",
        "- Brief the leads on their work. Before you report, say what you checked yourself and "
        "what is a lead's word you did not verify.",
        "- {night_words} starts the night: run "
        "`guild dream --wait-all` (Bash timeout 600000) and tell {owner} its total time in one line.",
        "- {hidden_marks} in a tab or workspace name: ignore it entirely.",
        "- No cloud changes or deploys without {owner}'s OK. Every PR mention carries its full URL.",
        "- Answer in the language they write, briefly.",
        "- The rest is in your memory files (MEMORY.md and the files it links); they still hold.",
        "- Your notes outlive this conversation: `guild notes`, `guild notes --write` (stdin).",
    )
)
FIRST_JOB = (
    "Your notes are empty, so start here: make an honest pass over your scope in the tracker "
    "(what is real, what is stale), post one summary line on the board, and write your notes."
)


class LeadError(Exception):
    pass


@dataclass(frozen=True)
class Lead:
    name: str
    display_name: str
    scope: str
    linear_refs: tuple[str, ...]
    tint: str
    alias: str | None
    surface: str | None
    previous_handle: str | None
    previous_handle_explicit: bool
    previous_tab_name: str | None
    previous_workspace: str | None
    previous_context: str | None
    created_at: int
    retired_at: int | None
    label: str | None

    @property
    def tab_title(self) -> str:
        return f"{self.display_name} · {self.label}" if self.label else self.display_name


def map_row_to_lead(row: sqlite3.Row) -> Lead:
    return Lead(
        name=str(row["name"]),
        display_name=str(row["display_name"]),
        scope=str(row["scope"]),
        linear_refs=tuple(cast(list[str], json.loads(str(row["linear_refs"])))),
        tint=str(row["tint"]),
        alias=row["alias"],
        surface=row["surface"],
        previous_handle=row["previous_handle"],
        previous_handle_explicit=bool(row["previous_handle_explicit"]),
        previous_tab_name=row["previous_tab_name"],
        previous_workspace=row["previous_workspace"],
        previous_context=row["previous_context"],
        created_at=int(row["created_at"]),
        retired_at=row["retired_at"],
        label=row["label"],
    )


def list_internal_names(conn: sqlite3.Connection, config: Config) -> list[str]:
    """Our names, which colleagues must never see: every lead's, retired ones too, the assistant's, and our words."""
    names = [lead.display_name for lead in list_leads(conn, include_retired=True)]
    return list(dict.fromkeys([*names, config.assistant_handle.capitalize(), *INTERNAL_WORDS]))


def find_internal_names(text: str, names: list[str]) -> list[str]:
    """Whole words, any case; a plural counts."""
    return [name for name in names if re.search(rf"\b{re.escape(name)}s?\b", text, re.IGNORECASE)]


EVIDENCE_RULE = (
    "Every reason a change exists says where it comes from: OBSERVED (it happened to a real user: cite the log, data "
    "or report), REPRODUCED (we made it happen: on which commit and how) or FROM CODE (read from source or docs, not "
    "seen). Never write 'users hit' unless it was observed; if nothing was observed, say who would hit it and when."
)


PR_REVIEWS_RULE = (
    "Every code PR of 100+ lines, or of any size that adds a service, contract, queue or chart, gets a Codex review "
    "and a Claude review (a fresh session that did not write it), independent, read-only and in parallel, before it "
    "goes to "
    "{owner}'s queue, without being asked. Its review doc gets a section headed Reviews: each review that ran (Codex, "
    "Claude, ReviewBot, others), its findings one line each, and the outcome (fixed in <commit> / deferred: reason + "
    "issue / rejected: reason). Under 100 lines the same short list goes in the PR body."
)


def map_names_to_brief_rules(names: list[str], assistant: str, owner: str) -> str:
    """The rules every worker starts with, its own names spelled out."""
    claude = (
        f"On GitHub and the tracker, what you write to colleagues starts with '{CLAUDE_PREFIX}'; in chat it never "
        f"does. Everywhere it names nothing of ours: not {', '.join(names)}."
    )
    linear = ISSUE_CREATE_RULE.format(assistant=assistant.capitalize(), owner=owner)
    review_doc = f"A PR's review doc follows {REVIEW_DOC_SPEC}: its sections, order and rules."
    return f"{claude}\n{linear}\n{EVIDENCE_RULE}\n{PR_REVIEWS_RULE.format(owner=owner)} {review_doc}"


def list_leads(conn: sqlite3.Connection, include_retired: bool = False) -> list[Lead]:
    where = "" if include_retired else "WHERE retired_at IS NULL"
    return [map_row_to_lead(r) for r in conn.execute(f"SELECT * FROM leads {where} ORDER BY created_at, name")]


def get_lead(conn: sqlite3.Connection, name: str) -> Lead:
    row = conn.execute("SELECT * FROM leads WHERE name = ?", (name.lower(),)).fetchone()
    if row is None:
        raise LeadError(f"no lead named {name!r}")
    return map_row_to_lead(row)


@dataclass(frozen=True)
class LeadWork:
    name: str
    refs: tuple[str, ...]


def set_lead_work(conn: sqlite3.Connection, lead: str, name: str, refs: list[str] | None, at_ms: int) -> None:
    """Refs replace the chunk's when given; a chunk finished earlier comes back when set again."""
    clean = " ".join(name.split())
    if not clean or len(clean) > WORK_NAME_MAX_CHARS:
        raise LeadError(f"a chunk's name is one line of 1 to {WORK_NAME_MAX_CHARS} characters")
    conn.execute(
        """
        INSERT INTO lead_work (lead, name, refs, created_at, updated_at) VALUES (?1, ?2, ?3, ?4, ?4)
        ON CONFLICT (lead, name) DO UPDATE SET
            name = excluded.name, updated_at = excluded.updated_at, done_at = NULL,
            refs = CASE WHEN ?5 THEN excluded.refs ELSE refs END
        """,
        (lead, clean, json.dumps(refs or []), at_ms, refs is not None),
    )


def finish_lead_work(conn: sqlite3.Connection, lead: str, name: str, at_ms: int) -> None:
    cursor = conn.execute(
        "UPDATE lead_work SET done_at = ?, updated_at = ? WHERE lead = ? AND name = ? AND done_at IS NULL",
        (at_ms, at_ms, lead, " ".join(name.split())),
    )
    if cursor.rowcount == 0:
        raise LeadError(f"{lead} has no open chunk named {name!r}")


def list_lead_work(conn: sqlite3.Connection, lead: str) -> list[LeadWork]:
    return [
        LeadWork(str(r["name"]), tuple(cast(list[str], json.loads(str(r["refs"])))))
        for r in conn.execute(
            "SELECT name, refs FROM lead_work WHERE lead = ? AND done_at IS NULL ORDER BY created_at, name", (lead,)
        )
    ]


def find_lead_on_surface(conn: sqlite3.Connection, surface: str | None) -> Lead | None:
    if surface is None:
        return None
    row = conn.execute("SELECT * FROM leads WHERE surface = ? AND retired_at IS NULL", (surface,)).fetchone()
    return map_row_to_lead(row) if row else None


def is_surface_locked(conn: sqlite3.Connection, config: Config, surface: str | None) -> bool:
    """The one check for Guild: a locked tab or workspace is outside everything."""
    if surface is None:
        return False
    row = conn.execute("SELECT tab_name, workspace_name FROM surfaces WHERE surface = ?", (surface,)).fetchone()
    return row is not None and config.hides(row["tab_name"], row["workspace_name"])


def is_conversation_locked(conn: sqlite3.Connection, config: Config, conversation_id: str) -> bool:
    """By the tab that holds or last held the conversation, so its mail and posts stay out after /clear or exit."""
    row = conn.execute(
        """
        SELECT s.tab_name, s.workspace_name FROM conversations c
        JOIN instances i ON i.instance_id = c.instance_id JOIN surfaces s ON s.surface = i.surface
        WHERE c.conversation_id = ?
        """,
        (conversation_id,),
    ).fetchone()
    return row is not None and config.hides(row["tab_name"], row["workspace_name"])


def find_lead_conversations(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """
        SELECT i.conversation_id FROM leads l JOIN instances i ON i.surface = l.surface AND i.ended_at IS NULL
        WHERE l.retired_at IS NULL AND i.conversation_id IS NOT NULL
        """
    )
    return {str(r[0]) for r in rows}


def is_lead_tab(conn: sqlite3.Connection, agterm_session: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM surfaces s JOIN leads l ON l.surface = s.surface AND l.retired_at IS NULL
        WHERE s.agterm_session = ?
        """,
        (agterm_session,),
    ).fetchone()
    return row is not None


def validate_label(label: str | None) -> str | None:
    if label is None:
        return None
    label = " ".join(label.split())
    if not label or len(label) > LABEL_MAX_CHARS or not is_english_name(label):
        raise LeadError(f"a label is 1 to {LABEL_MAX_CHARS} chars, in English")
    return label


def set_lead_label(conn: sqlite3.Connection, name: str, label: str) -> Lead:
    lead = get_lead(conn, name)
    if lead.retired_at is not None:
        raise LeadError(f"{lead.display_name} is retired")
    conn.execute("UPDATE leads SET label = ? WHERE name = ?", (validate_label(label), lead.name))
    return get_lead(conn, lead.name)


def enroll_lead(
    conn: sqlite3.Connection,
    config: Config,
    display_name: str,
    tab_handle: str,
    scope: str,
    linear_refs: list[str],
    alias: str | None,
    tint: str | None,
    previous_context: str | None,
    at_ms: int,
    label: str | None = None,
) -> Lead:
    """Runs inside the caller's transaction. A name stays with its lead for good, so retired ones are refused too."""
    if not NAME_RE.fullmatch(display_name):
        raise LeadError(f"{display_name!r}: a lead's name is one capitalised word in Latin letters")
    if not scope.strip():
        raise LeadError("a lead needs a scope")
    label = validate_label(label)
    if len(scope) > SCOPE_MAX_CHARS:
        raise LeadError(f"scope is {len(scope)} chars; keep it under {SCOPE_MAX_CHARS}, it goes into every start")
    if len(linear_refs) > REFS_MAX or any(len(r) > REF_MAX_CHARS for r in linear_refs):
        raise LeadError(f"at most {REFS_MAX} refs of {REF_MAX_CHARS} chars")
    name = display_name.lower()
    alias = alias.lower() if alias else None
    if alias is not None and not ALIAS_RE.fullmatch(alias):
        raise LeadError(f"{alias!r}: an alias is one word")
    if conn.execute("SELECT 1 FROM leads WHERE name = ?", (name,)).fetchone():
        raise LeadError(f"the name {display_name} is taken, and a retired name is never reused")
    row = conn.execute(
        """
        SELECT s.surface, s.handle, s.handle_explicit, s.tab_name, s.workspace_name, s.agterm_session FROM surfaces s
        WHERE s.handle = ?1
           OR (NOT EXISTS (SELECT 1 FROM surfaces WHERE handle = ?1)
               AND s.surface = (SELECT surface FROM handle_aliases WHERE handle = ?1))
        """,
        (tab_handle.lower(),),
    ).fetchone()
    if row is None:
        raise LeadError(f"no tab with handle {tab_handle!r}")
    surface = str(row["surface"])
    if not conn.execute("SELECT 1 FROM instances WHERE surface = ? AND ended_at IS NULL", (surface,)).fetchone():
        raise LeadError(f"tab {tab_handle!r} has no live session")
    if not row["agterm_session"]:
        raise LeadError(f"tab {tab_handle!r} is not an agterm tab")
    if is_surface_locked(conn, config, surface):
        raise LeadError(f"tab {tab_handle!r} is locked, and Guild ignores locked tabs")
    assistant_pane = conn.execute(
        "SELECT 1 FROM surfaces WHERE agterm_session = ? AND handle = ?",
        (row["agterm_session"], config.assistant_handle),
    ).fetchone()
    if assistant_pane:
        raise LeadError(f"tab {tab_handle!r} is the assistant's, not a lead's")
    held = find_lead_on_surface(conn, surface)
    if held is not None:
        raise LeadError(f"tab {tab_handle!r} already is {held.display_name}")
    if alias is not None and _is_handle_unavailable(conn, alias, surface):
        raise LeadError(f"alias {alias!r} already names another tab or lead")
    used = {str(r[0]) for r in conn.execute("SELECT tint FROM leads WHERE retired_at IS NULL")}
    chosen = tint or next((t for t in TINTS if t not in used), TINTS[0])
    if chosen not in TINTS:
        raise LeadError(f"tint must be one of {', '.join(TINTS)}")
    try:
        set_handle(conn, surface, name, at_ms)
    except HandleTakenError as exc:
        raise LeadError(str(exc)) from exc
    if alias is not None and alias != name:
        conn.execute(
            "INSERT INTO handle_aliases (handle, surface, retired_at) VALUES (?, ?, ?) "
            "ON CONFLICT (handle) DO UPDATE SET surface = excluded.surface, retired_at = excluded.retired_at",
            (alias, surface, at_ms),
        )
    conn.execute(
        """
        INSERT INTO leads (name, display_name, scope, linear_refs, tint, alias, surface, previous_handle,
                           previous_handle_explicit, previous_tab_name, previous_workspace, previous_context,
                           created_at, label)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (name, display_name, scope.strip(), json.dumps(linear_refs), chosen, alias, surface, row["handle"],
         int(row["handle_explicit"] or 0), row["tab_name"], row["workspace_name"], previous_context, at_ms, label),
    )  # fmt: skip
    return get_lead(conn, name)


def retire_lead(conn: sqlite3.Connection, name: str, at_ms: int) -> None:
    """The name and alias stop routing but stay reserved; the tab gets back the handle it had."""
    lead = get_lead(conn, name)
    if lead.retired_at is not None:
        raise LeadError(f"{lead.display_name} is already retired")
    conn.execute("UPDATE leads SET surface = NULL, retired_at = ? WHERE name = ?", (at_ms, lead.name))
    conn.execute("DELETE FROM handle_aliases WHERE handle IN (?, ?)", (lead.name, lead.alias))
    if not lead.surface:
        return
    previous = lead.previous_handle
    if previous and _is_handle_unavailable(conn, previous, lead.surface):
        previous = None
    conn.execute(
        "UPDATE surfaces SET handle = ?, handle_explicit = ? WHERE surface = ?",
        (previous, int(lead.previous_handle_explicit and previous is not None), lead.surface),
    )
    if previous:
        conn.execute("DELETE FROM handle_aliases WHERE handle = ?", (previous,))


def _is_handle_unavailable(conn: sqlite3.Connection, handle: str, surface: str) -> bool:
    if handle in protected_lead_handles(conn):
        return True
    return (
        conn.execute(
            "SELECT 1 FROM surfaces WHERE handle = ?1 AND surface != ?2 "
            "UNION SELECT 1 FROM handle_aliases WHERE handle = ?1 AND surface != ?2",
            (handle, surface),
        ).fetchone()
        is not None
    )


def forget_lead_role(conn: sqlite3.Connection, surface: str | None) -> None:
    """Any session start can lose the role, compaction and /clear included, even one this hook cannot answer."""
    if surface is not None:
        conn.execute("UPDATE leads SET role_seen_conversation = NULL WHERE surface = ?", (surface,))


def take_lead_role(conn: sqlite3.Connection, config: Config, surface: str | None, conversation_id: str) -> Lead | None:
    lead = find_lead_on_surface(conn, surface)
    if lead is None or is_surface_locked(conn, config, surface):
        return None
    updated = conn.execute(
        "UPDATE leads SET role_seen_conversation = ?1 WHERE name = ?2 AND role_seen_conversation IS NOT ?1",
        (conversation_id, lead.name),
    )
    return lead if updated.rowcount else None


def convert_lead_to_role(lead: Lead, config: Config) -> str:
    refs = f" (tracker: {', '.join(lead.linear_refs)})" if lead.linear_refs else ""
    return ROLE_TEMPLATE.format(
        display=lead.display_name,
        scope=lead.scope,
        refs=refs,
        assistant=config.assistant_handle,
        user=config.user_alias or "the user",
        owner=config.owner_name,
    )


def convert_notes_to_lead_section(notes: str) -> str:
    return convert_notes_to_section(notes) if notes.strip() else FIRST_JOB


def is_assistant_surface(conn: sqlite3.Connection, config: Config, surface: str | None) -> bool:
    if surface is None or is_surface_locked(conn, config, surface):
        return False
    row = conn.execute("SELECT handle FROM surfaces WHERE surface = ?", (surface,)).fetchone()
    return row is not None and row["handle"] == config.assistant_handle and find_lead_on_surface(conn, surface) is None


def forget_assistant_role(conn: sqlite3.Connection, surface: str | None) -> None:
    if surface is not None:
        conn.execute("DELETE FROM assistant_role WHERE surface = ?", (surface,))


def take_assistant_role(conn: sqlite3.Connection, config: Config, surface: str | None, conversation_id: str) -> bool:
    if surface is None or not is_assistant_surface(conn, config, surface):
        return False
    seen = conn.execute("SELECT conversation_id FROM assistant_role WHERE surface = ?", (surface,)).fetchone()
    if seen is not None and seen["conversation_id"] == conversation_id:
        return False
    conn.execute(
        "INSERT INTO assistant_role (surface, conversation_id) VALUES (?, ?) "
        "ON CONFLICT (surface) DO UPDATE SET conversation_id = excluded.conversation_id",
        (surface, conversation_id),
    )
    return True


def convert_config_to_assistant_role(config: Config, leads: list[Lead]) -> str:
    handle = config.assistant_handle
    names = ", ".join(lead.display_name for lead in leads) or "none yet"
    return ASSISTANT_ROLE_TEMPLATE.format(
        name=handle.capitalize(),
        handle=handle,
        leads=names,
        owner=config.owner_name,
        night_words=convert_words_to_quoted(config.night_words),
        hidden_marks=" ".join(config.hidden_name_marks),
    )


def convert_words_to_quoted(words: tuple[str, ...]) -> str:
    return " or ".join(f"'{w}'" for w in words)


def convert_notes_to_section(notes: str) -> str:
    """Over the limit, the end goes in: notes grow at the end, so it holds the newest lines and rules."""
    text = notes.strip()
    size = measure_chars(text)
    if size <= NOTES_INJECT_MAX_CHARS:
        return f"Your notes:\n{text}"
    tail = cut_to_last_chars(text, NOTES_INJECT_MAX_CHARS)
    if "\n" in tail:
        tail = tail.split("\n", 1)[1]
    return f"{convert_size_to_notes_warning(size)} Their last {measure_chars(tail):,} characters:\n{tail}"


def convert_size_to_notes_warning(size: int) -> str:
    return (
        f"Your notes are {size:,} characters, over the {NOTES_INJECT_MAX_CHARS:,} that reach you at each start, so "
        "the beginning is left out (`guild notes` prints all). Rewrite them shorter with `guild notes --write`: keep "
        "the rules and facts that still hold, drop what is done or stale."
    )


def lead_dir(name: str) -> Path:
    return state_dir() / "leads" / name


def assistant_dir() -> Path:
    """Not by handle: the assistant's handle is configurable, and its notes must survive a change."""
    return state_dir() / "assistant"


def read_notes(directory: Path) -> str:
    try:
        with (directory / "notes.md").open("rb") as notes:
            return notes.read().decode(errors="replace")
    except OSError:
        return ""


def write_notes(directory: Path, text: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / "notes.md.tmp"
    temporary.write_text(text)
    temporary.replace(directory / "notes.md")


def read_handovers(directory: Path) -> str:
    try:
        return (directory / "handovers.md").read_text()
    except OSError:
        return ""


def append_handover(name: str, from_handle: str, stamp: str, text: str) -> None:
    directory = lead_dir(name)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "handovers.md").open("a") as handovers:
        handovers.write(f"## From {from_handle}, {stamp}\n\n{text.strip()}\n\n")


def sync_lead_terminal(config: Config, lead: Lead, agterm_session: str) -> list[str]:
    """Idempotent, so a failure halfway is fixed by running it again. Moving a tab carries its nested workers."""
    state_dir().mkdir(parents=True, exist_ok=True)
    # Two syncs at once would each find no TEAM and create one.
    with (state_dir() / "team-workspace.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _sync_lead_terminal(config, lead, agterm_session)


def _sync_lead_terminal(config: Config, lead: Lead, agterm_session: str) -> list[str]:
    workspaces = _read_workspaces(config)
    if workspaces is None:
        return ["agterm is not reachable"]
    workspace = next((w for w in workspaces if w.name == TEAM_WORKSPACE), None)
    if workspace is None:
        workspace = _create_team_workspace(config)
        if workspace is None:
            return [f"could not create workspace {TEAM_WORKSPACE}"]
    problems: list[str] = []
    in_place = agterm_session in workspace.sessions
    if not in_place and run_agterm(config, ["session", "move", workspace.id, "--target", agterm_session]) is None:
        problems.append(f"could not move the tab to workspace {TEAM_WORKSPACE}")
    if run_agterm(config, ["session", "rename", lead.tab_title, "--target", agterm_session]) is None:
        problems.append("could not rename the tab")
    if run_agterm(config, ["session", "tint", lead.tint, "--on", "icon,name", "--target", agterm_session]) is None:
        problems.append("could not tint the tab")
    context = lead.scope.encode()[:CONTEXT_MAX_BYTES].decode(errors="ignore")
    if run_agterm(config, ["session", "context", " ".join(context.split()), "--target", agterm_session]) is None:
        problems.append("could not set the tab's context")
    return problems


def read_tab_context(config: Config, agterm_session: str) -> str | None:
    for workspace in _read_workspaces(config) or []:
        if agterm_session in workspace.sessions:
            return workspace.contexts.get(agterm_session)
    return None


def restore_tab_after_retirement(config: Config, lead: Lead, agterm_session: str) -> list[str]:
    workspaces = _read_workspaces(config)
    if workspaces is None:
        return ["agterm is not reachable"]
    problems: list[str] = []
    if lead.previous_workspace:
        workspace = next((w for w in workspaces if w.name == lead.previous_workspace), None)
        if workspace is None:
            problems.append(f"workspace {lead.previous_workspace} is gone")
        elif agterm_session not in workspace.sessions and (
            run_agterm(config, ["session", "move", workspace.id, "--target", agterm_session]) is None
        ):
            problems.append(f"could not move the tab back to {lead.previous_workspace}")
    if lead.previous_tab_name and (
        run_agterm(config, ["session", "rename", lead.previous_tab_name, "--target", agterm_session]) is None
    ):
        problems.append("could not restore the tab's name")
    context = [lead.previous_context] if lead.previous_context else ["--clear"]
    if run_agterm(config, ["session", "context", *context, "--target", agterm_session]) is None:
        problems.append("could not restore the tab's context")
    if run_agterm(config, ["session", "tint", "clear", "--target", agterm_session]) is None:
        problems.append("could not clear the tab's tint")
    return problems


@dataclass(frozen=True)
class _Workspace:
    id: str
    name: str
    sessions: tuple[str, ...]
    contexts: dict[str, str]


def _create_team_workspace(config: Config) -> _Workspace | None:
    output = run_agterm(config, ["workspace", "new", TEAM_WORKSPACE, "--json"])
    if output is None:
        return None
    try:
        return _Workspace(str(json.loads(output)["result"]["id"]), TEAM_WORKSPACE, (), {})
    except (ValueError, KeyError, TypeError):
        return None


def _read_workspaces(config: Config) -> list[_Workspace] | None:
    output = run_agterm(config, ["tree", "--json"])
    if output is None:
        return None
    try:
        raw = json.loads(output)
        nodes = cast(list[dict[str, object]], raw["result"]["tree"]["workspaces"])
    except (ValueError, KeyError, TypeError):
        return None
    workspaces: list[_Workspace] = []
    for node in nodes:
        sessions = cast(list[dict[str, object]], node.get("sessions", []))
        contexts = {str(s.get("id")): str(s["context"]) for s in sessions if s.get("context")}
        workspaces.append(
            _Workspace(str(node.get("id")), str(node.get("name")), tuple(str(s.get("id")) for s in sessions), contexts)
        )
    return workspaces
