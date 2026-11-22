import fcntl
import json
import re
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from bridge.agent_prefix import CLAUDE_PREFIX
from bridge.config import Config, read_overlay, state_dir
from bridge.inject import cut_to_last_chars, measure_chars
from bridge.registry import (
    HandleTakenError,
    find_lead_tab_title,
    is_english_name,
    map_config_to_assistant_title,
    protected_lead_handles,
    set_handle,
)
from bridge.tabinput import run_agterm

NAME_RE = re.compile(r"[A-Z][a-z]{1,31}")
ALIAS_RE = re.compile(r"[^\W\d_]{2,32}")
GUILD_WORKSPACE = "\U0001f441 GUILD"
# Earlier names, renamed in place so an install never gets a second workspace.
OLD_GUILD_WORKSPACES = ("GUILD", "TEAM")
# The orange of Guild's eye (docs/images/eye.svg); a hex tint needs agterm 0.41.0.
GUILD_WORKSPACE_TINT = "#e8590c"
TINTS = ("blue", "green", "orange", "purple", "red", "yellow", "gray")
ASSISTANT_TINT = "yellow"
NOTES_INJECT_MAX_CHARS = 4000
CONTEXT_MAX_BYTES = 256
SCOPE_MAX_CHARS = 500
REFS_MAX = 10
REF_MAX_CHARS = 64
LABEL_MAX_CHARS = 40
WORK_NAME_MAX_CHARS = 80
WORK_ABOUT_MAX_CHARS = 200
DOMAIN_MAX_CHARS = 64

INTERNAL_WORDS = ("worker", "lead", "Guild", "bridge")
CLAUDE_RULE = (
    f"- On GitHub and Linear, comments and descriptions start with '{CLAUDE_PREFIX}', never titles (nor Slack); "
    f"name nothing of ours (no lead's or {{assistant}}'s name, nor {', '.join(INTERNAL_WORDS)})."
)
LINEAR_CREATE_RULE = (
    "Create an issue only if a search finds no duplicate; never a sub-issue; rather none than one. Out of scope: "
    "only after {owner} confirms through {assistant}."
)
REVIEW_DOC_SPEC = "~/.claude/skills/bridge/review-doc.md"
ROLE_TEMPLATE = "\n".join(
    (
        "[guild] You are {display}, a team lead under {owner}; `{assistant}` is the assistant.",
        "Your scope: {scope}{refs}",
        "- Linear, your scope: keep what's real, close the stale; shared projects: only {owner}'s part. Move issues "
        f"as you act: In Progress at start, In Review at its closing PR. {LINEAR_CREATE_RULE}",
        "{grooming}",
        "- You coordinate: each task gets a nested tab (`bridge` skill); run independent pieces in parallel workers, "
        "never queued; inline/subagent only for a quick lookup{heavy_work}.",
        '- Team board: `bridge board "…" --type heads-up|question|report|handoff`, one line, what others must not '
        "collide with; @name only for a lead needed now.",
        "- A lead's or {owner}'s `board post` gets one line: "
        '`guild ack N "what it means for you"` or `"nothing for me"`; a question or @name, `bridge reply N`. Never '
        "ack your own post or an ack.",
        "- Ask {owner} only when nothing else settles it: `bridge post blocked|decision`, --body lines `What:`, "
        "`Why now:`, `<answer> cost:`. Done: a `--type report` post. Codex: your 2nd opinion.",
        "- `--urgent` only when prod or users are hurting, money is leaking, or a deadline is today.",
        "- No browser (Chrome, Playwright) unless the task says so, then a hidden tab group: "
        "{owner}'s screen is shared.",
        "- `{assistant}, relaying {user}'s answer` (`guild answer`) is {owner}'s OK for that question only, prod "
        "writes too.",
        '- Main chunks on the page: `guild work set "<chunk>" <refs> --about "<what and why>"` at start, '
        "`guild work done` at end.",
        "- Typing in your tab is {owner}: answer there; send {assistant} one line per decision.",
        "- {owner}'s asks (typed here or relayed): `guild ask add` at once, verbatim; to verified with evidence of "
        "what {owner} sees.",
        "- No PR review request as a decision: the page lists open PRs. For {owner}: essence "
        f"visible, details folded. A PR doc follows {REVIEW_DOC_SPEC}; reasons OBSERVED, REPRODUCED or FROM CODE.",
        "{reviews}",
        "- Merge your own PR yourself: approved on GitHub ({bot} counts), CI green, no "
        "conflicts, none of {owner}'s comments open (settle them), each colleague's comment answered or its thread "
        "resolved (nits too), you judge it safe. Deploys need {owner}.",
        CLAUDE_RULE,
        "- Pages: source in {pages_dir}/<slug>/, `bridge page add`; cite /p/<slug> (`bridge` skill).",
        "- Propose rules and memory changes to {assistant}; only {assistant} writes shared memory and shared rules.",
        "- Notes outlive this chat: `guild notes` (`--write`). Close idle workers with no next job: "
        "`guild worker close <tab>`; back only via `guild worker reopen <id>`.",
    )
)
ASSISTANT_ROLE_TEMPLATE = "\n".join(
    (
        "[guild] You are {name} (`{handle}`), {owner}'s Right Hand; {owner} leads, the team leads ({leads}) run the "
        "projects. You route and manage, and effectiveness comes first.",
        "- Delegate by default: code reading, explainers and investigations go to a lead, who "
        "returns a summary or a self-contained HTML page that you publish; nothing bans depth. Spot-check a lead's "
        "claim when it matters.",
        "- A deep topic can go straight to its lead: offer {owner} to talk to the lead in its tab, and tell the lead "
        "that {owner} decides there.",
        "- Bring {owner} only what needs them, one at a time, as a Needs you post (`bridge post decision|blocked`, "
        "`--option` buttons), never a dialog in your tab: their chat stays free. What, why now, each answer's cost, "
        "your pick. Settle reversible calls yourself; tell them in one line.",
        "- What they must look at goes on a published page, never a file path.",
        '- Relay their answers verbatim, marked as relayed; a chat answer to a post: `guild answer N "…"`. A '
        "relayed answer confirms only that question.",
        "- Keep the project priority current: `guild priority set`.",
        "- Keep every lead loaded to about 80% (a worker slot stays free for urgent work): when a lead has "
        "capacity, assign the next priority item in its domain yourself, with a brief and plan first; ask {owner} "
        "only when the choice is theirs (scope, out of cycle, prod and deploys, conflicting priorities).",
        "- In a report, say what you checked and what is a lead's unverified word.",
        "- {night_words}: run `guild dream --wait-all` (Bash timeout 600000) and tell {owner} its total time. "
        "{morning_words} in the morning runs `guild day`.",
        "- Ignore tabs and workspaces with 🔒 in the name.",
        "{rules}",
        "- Answer briefly, in the language they write.",
        "- Your memory files (MEMORY.md and its links) still hold.",
        "- Notes outlive this conversation: `guild notes`, `guild notes --write` (stdin).",
    )
)
ASSISTANT_RULES = "- No deploys without {owner}'s OK. PR mentions carry full URLs."
"""The generic text of the assistant-rules overlay slot."""
BRIEF_REVIEWS_RULE = (
    "Every code PR's review doc gets a section headed Reviews: each review that ran, its findings one line each, and "
    "the outcome (fixed in <commit> / deferred: reason + issue / rejected: reason). Under 100 lines the same short "
    "list goes in the PR body."
)
"""The generic text of the brief-reviews overlay slot."""
FIRST_JOB = (
    "Your notes are empty, so start here: make an honest pass over your scope in Linear "
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
    domains: tuple[str, ...] = ()


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
        domains=tuple(cast(list[str], json.loads(str(row["domains"])))),
    )


def list_internal_names(conn: sqlite3.Connection, config: Config) -> list[str]:
    """Our names, which colleagues must never see: every lead's, retired ones too, the assistant's, and our words."""
    names = [lead.display_name for lead in list_leads(conn, include_retired=True)]
    return list(dict.fromkeys([*names, config.assistant_handle.capitalize(), *INTERNAL_WORDS]))


def find_internal_names(text: str, names: list[str]) -> list[str]:
    """Whole words, any case; a plural counts."""
    return [name for name in names if re.search(rf"\b{re.escape(name)}s?\b", text, re.IGNORECASE)]


EVIDENCE_RULE = (
    "Every reason a change exists says where it comes from: OBSERVED (it happened to a real user: cite "
    "the log, data or report), REPRODUCED (we made it happen: on which commit and how) or FROM CODE (read from source "
    "or docs, not seen). Never write 'users hit' or 'onboarding X found' unless it was observed; if nothing was "
    "observed, say who would hit it and when."
)


def map_names_to_brief_rules(names: list[str], config: Config) -> str:
    """The rules every worker starts with, its own names spelled out."""
    assistant, owner = config.assistant_handle.capitalize(), config.owner_name
    claude = (
        f"On GitHub and Linear, each comment and description body you write to colleagues starts with "
        f"'{CLAUDE_PREFIX}'; a PR, issue or commit title never does, nor anything on Slack. "
        f"On all three it names nothing of ours: not {', '.join(names)}."
    )
    grooming = read_overlay(config, "grooming", owner=owner, assistant=assistant)
    linear = "\n".join(t for t in (LINEAR_CREATE_RULE.format(assistant=assistant, owner=owner), grooming) if t)
    reviews = read_overlay(config, "brief-reviews", BRIEF_REVIEWS_RULE, owner=owner)
    review_doc = (
        f"A PR's review doc follows {REVIEW_DOC_SPEC}: its sections, order and rules. It opens with the plain story "
        "of what is changed, in full sentences, for a reader who never saw the code, then a User story: one person, "
        "at least three numbered steps, what they do and see today and after the PR; every internal name is a click "
        "term at its first use in each section; one concrete scenario per change. A PR that only enables something "
        "(a flag off by default, an opt-in value, one step of a plan) has a description section 'How this will be "
        "used': the problem today, the plan's numbered steps with links (built / not built), what switches it on "
        "and who decides, what happens if it never is, the risk of using it wrong. Before writing, read the reference "
        "example house.md names, if any; before registering, pass the spec's first-reader check. A PR's description "
        "opens, for a colleague with no context, with the spec's three parts: context (what, where from, why now), a "
        "user story in numbered steps with concrete actors, and how it is solved in principle; an enabling PR's 'How "
        "this will be used' comes right after them, not folded; other details folded after; "
        'a few key terms defined at first use as <ins title="definition">term</ins>.'
    )
    scope = (
        "Work beyond the task's stated scope goes to a follow-up, even if already written: say what you left out "
        "and why. When a merge from main conflicts with a narrower scope main chose on purpose, keep main's intent "
        "and read that PR before overriding it or its tests."
    )
    return f"{claude}\n{linear}\n{EVIDENCE_RULE}\n{' '.join(t for t in (reviews, review_doc) if t)}\n{scope}"


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
    about: str | None = None


def set_lead_work(
    conn: sqlite3.Connection, lead: str, name: str, refs: list[str] | None, at_ms: int, about: str | None = None
) -> None:
    """Refs and about replace the chunk's when given; a chunk finished earlier comes back when set again."""
    clean = " ".join(name.split())
    if not clean or len(clean) > WORK_NAME_MAX_CHARS:
        raise LeadError(f"a chunk's name is one line of 1 to {WORK_NAME_MAX_CHARS} characters")
    about = " ".join((about or "").split()) or None
    if about is not None and len(about) > WORK_ABOUT_MAX_CHARS:
        raise LeadError(f"--about is one line of at most {WORK_ABOUT_MAX_CHARS} characters")
    conn.execute(
        """
        INSERT INTO lead_work (lead, name, refs, created_at, updated_at, about) VALUES (?1, ?2, ?3, ?4, ?4, ?6)
        ON CONFLICT (lead, name) DO UPDATE SET
            name = excluded.name, updated_at = excluded.updated_at, done_at = NULL,
            refs = CASE WHEN ?5 THEN excluded.refs ELSE refs END,
            about = coalesce(excluded.about, about)
        """,
        (lead, clean, json.dumps(refs or []), at_ms, refs is not None, about),
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
        LeadWork(str(r["name"]), tuple(cast(list[str], json.loads(str(r["refs"])))), r["about"])
        for r in conn.execute(
            "SELECT name, refs, about FROM lead_work WHERE lead = ? AND done_at IS NULL ORDER BY created_at, name",
            (lead,),
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
    row = find_conversation_tab(conn, conversation_id)
    return row is not None and config.hides(row["tab_name"], row["workspace_name"])


def find_conversation_tab(conn: sqlite3.Connection, conversation_id: str) -> sqlite3.Row | None:
    """The tab that holds or last held the conversation: tab_name, workspace_name, handle."""
    row: sqlite3.Row | None = conn.execute(
        """
        SELECT s.tab_name, s.workspace_name, s.handle FROM conversations c
        JOIN instances i ON i.instance_id = c.instance_id JOIN surfaces s ON s.surface = i.surface
        WHERE c.conversation_id = ?
        """,
        (conversation_id,),
    ).fetchone()
    return row


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


def set_lead_domains(conn: sqlite3.Connection, name: str, domains: list[str]) -> Lead:
    """Each a project of the guild priority, by name, or a Linear project ref like P-PROJ-41."""
    lead = get_lead(conn, name)
    if lead.retired_at is not None:
        raise LeadError(f"{lead.display_name} is retired")
    clean = list(dict.fromkeys(" ".join(d.split()) for d in domains))
    if any(not d or len(d) > DOMAIN_MAX_CHARS for d in clean) or len(clean) > REFS_MAX:
        raise LeadError(f"at most {REFS_MAX} domains of 1 to {DOMAIN_MAX_CHARS} chars")
    conn.execute("UPDATE leads SET domains = ? WHERE name = ?", (json.dumps(clean), lead.name))
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
    used = {str(r[0]) for r in conn.execute("SELECT tint FROM leads WHERE retired_at IS NULL")} | {ASSISTANT_TINT}
    chosen = tint or next((t for t in TINTS if t not in used), TINTS[0])
    if chosen not in TINTS or chosen == ASSISTANT_TINT:
        raise LeadError(f"tint must be one of {', '.join(t for t in TINTS if t != ASSISTANT_TINT)}")
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
    refs = f" (Linear: {', '.join(lead.linear_refs)})" if lead.linear_refs else ""
    names = {"owner": config.owner_name, "assistant": config.assistant_handle}
    grooming = read_overlay(config, "grooming", **names)
    role = ROLE_TEMPLATE.format(
        display=lead.display_name,
        scope=lead.scope,
        refs=refs,
        user=config.user_alias or "the user",
        grooming=f"- {grooming}" if grooming else "",
        heavy_work=read_overlay(config, "lead-heavy-work", **names),
        reviews=read_overlay(config, "lead-reviews", **names),
        bot=config.review_bot_name,
        pages_dir=config.pages_dir,
        **names,
    )
    return drop_empty_lines(role)


def drop_empty_lines(text: str) -> str:
    """An overlay slot left empty takes its line with it."""
    return "\n".join(line for line in text.split("\n") if line)


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
    role = ASSISTANT_ROLE_TEMPLATE.format(
        name=handle.capitalize(),
        handle=handle,
        leads=names,
        owner=config.owner_name,
        night_words=convert_words_to_quoted(config.night_words),
        morning_words=convert_words_to_quoted(config.morning_words),
        rules=read_overlay(config, "assistant-rules", ASSISTANT_RULES, owner=config.owner_name),
    )
    return drop_empty_lines(role)


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
    """Not by handle: the assistant's handle has changed before, and its notes must not."""
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


def sync_lead_terminal(conn: sqlite3.Connection, config: Config, lead: Lead, agterm_session: str) -> list[str]:
    return sync_team_tab(config, agterm_session, find_lead_tab_title(conn, lead.name), lead.tint, lead.scope)


def sync_assistant_terminal(config: Config, agterm_session: str) -> list[str]:
    context = "Right Hand: routes the team leads' work"
    return sync_team_tab(config, agterm_session, map_config_to_assistant_title(config), ASSISTANT_TINT, context)


def sync_team_tab(config: Config, agterm_session: str, title: str, tint: str, context: str) -> list[str]:
    """Idempotent, so a failure halfway is fixed by running it again. Moving a tab carries its nested workers."""
    state_dir().mkdir(parents=True, exist_ok=True)
    # Two syncs at once would each find no GUILD and create one.
    with (state_dir() / "team-workspace.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _sync_team_tab(config, agterm_session, title, tint, context)


def _sync_team_tab(config: Config, agterm_session: str, title: str, tint: str, context: str) -> list[str]:
    workspaces = _read_workspaces(config)
    if workspaces is None:
        return ["agterm is not reachable"]
    workspace = next((w for w in workspaces if _is_named_guild(w.name)), None)
    if workspace is None:
        workspace = _rename_old_guild_workspace(config, workspaces) or _create_guild_workspace(config)
        if workspace is None:
            return [f"could not create workspace {GUILD_WORKSPACE}"]
    problems: list[str] = []
    tinted = ["workspace", "tint", GUILD_WORKSPACE_TINT, "--on", "icon,name", "--target", workspace.id]
    if run_agterm(config, tinted) is None:
        problems.append(f"could not tint workspace {GUILD_WORKSPACE}")
    in_place = agterm_session in workspace.sessions
    if not in_place and run_agterm(config, ["session", "move", workspace.id, "--target", agterm_session]) is None:
        problems.append(f"could not move the tab to workspace {GUILD_WORKSPACE}")
    if run_agterm(config, ["session", "rename", title, "--target", agterm_session]) is None:
        problems.append("could not rename the tab")
    if run_agterm(config, ["session", "tint", tint, "--on", "icon,name", "--target", agterm_session]) is None:
        problems.append("could not tint the tab")
    context = context.encode()[:CONTEXT_MAX_BYTES].decode(errors="ignore")
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


def _is_named_guild(name: str) -> bool:
    # The eye may come back with an emoji variation selector after it.
    return name.replace("\ufe0f", "") == GUILD_WORKSPACE


def _rename_old_guild_workspace(config: Config, workspaces: list[_Workspace]) -> _Workspace | None:
    old = next((w for w in workspaces if w.name in OLD_GUILD_WORKSPACES), None)
    if old is None or run_agterm(config, ["workspace", "rename", GUILD_WORKSPACE, "--target", old.id]) is None:
        return None
    return replace(old, name=GUILD_WORKSPACE)


def _create_guild_workspace(config: Config) -> _Workspace | None:
    output = run_agterm(config, ["workspace", "new", GUILD_WORKSPACE, "--json"])
    if output is None:
        return None
    try:
        return _Workspace(str(json.loads(output)["result"]["id"]), GUILD_WORKSPACE, (), {})
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
