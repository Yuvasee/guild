import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bridge.review_doc_check import DEFAULT_LIMITS

HIDDEN_NAME_MARKS = ("🔒", "🔐", "🔏")
"""Tabs or workspaces whose name contains one of these are left out of the dashboard."""
AUTO_MERGE_MODES = ("off", "dry-run", "on")


@dataclass(frozen=True)
class Remote:
    name: str
    ssh: str
    aliases: tuple[str, ...] = ()
    """Other ssh host names that reach the same machine (e.g. a second ssh alias), for spotting who drives it."""
    share_tabs: bool = False
    """Host worker tabs attach through bridge's shared connection to the host: only for an sshd whose MaxSessions
    exceeds the tabs open there at once, since a session past it is refused and that tab cannot attach."""

    def host_names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.ssh, self.name, *self.aliases)))


@dataclass(frozen=True)
class Watcher:
    path: str
    """A status JSON that a background job rewrites after every run; ~ is expanded when read."""
    name: str = ""
    """Shown while the file is missing or unreadable; otherwise the file names itself."""


@dataclass(frozen=True)
class PageLink:
    name: str
    url: str
    """http or https only; the page opens it in a new tab."""


@dataclass(frozen=True)
class RestrictedPath:
    path: str
    """A repo-relative file or directory another team owns, e.g. services/pricing/."""
    label: str
    owner: str

    def covers(self, file: str) -> bool:
        prefix = self.path.strip("/")
        return file == prefix or file.startswith(prefix + "/")


@dataclass(frozen=True)
class Config:
    issue_prefixes: tuple[str, ...] = ()
    agtermctl: str = "agtermctl"
    user_alias: str = ""
    issue_url: str = ""
    linear_api_key_file: str = ""
    remotes: tuple[Remote, ...] = ()
    """Template for issue links, e.g. https://linear.app/acme/issue/{key}."""
    pr_repo_aliases: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    pr_short_names: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    """Short names sessions write for a repo, as in web#12: the page links them to that owner/repo's PR."""
    assistant_handle: str = "aide"
    """The one session that may relay the user's answers to posts (`guild answer`)."""
    lead_skills: tuple[str, ...] = ("bridge", "agterm", "pod")
    """The only skills a lead or the assistant may load; a skill's body would stay in their context for good."""
    owner_name: str = "the owner"
    """How sessions and messages to leads name the person who leads the guild; the page itself says "you"."""
    night_words: tuple[str, ...] = ("Good night",)
    """What the owner says at the end of the day: the night's dreams follow."""
    morning_words: tuple[str, ...] = ("Good morning",)
    """What the owner says in the morning to run the morning report (`guild day`)."""
    confusion_phrases: tuple[str, ...] = (
        "не понимаю",
        "непонятно",
        "не понял",
        "что это значит",
        "don't understand",
        "unclear",
        "what does … mean",
        "confusing",
    )
    """What `guild stats week` counts as the owner not understanding; … stands for a few words."""
    compact_at_tokens: int = 0
    """Context size at which a session saves notes and, in agterm, bridge compacts it right after; 0 is off."""
    parallel_streams: tuple[str, ...] = ()
    """Lead domains the owner made standing streams outside the cycle's priority projects, each with its own backlog."""
    review_org: str = ""
    """GitHub org of the review queue; empty turns the queue and all its jobs off."""
    review_author: str = ""
    """The GitHub account the queue's PRs and the user's comments come from."""
    review_bot_logins: tuple[str, ...] = ()
    """Accounts whose reviews and review requests do not mean a person has seen the PR."""
    review_ui_paths: tuple[str, ...] = ()
    """Globs of UI code: a PR that changes a file there, other than a test, gets a review doc with a screenshot."""
    backport_bases: tuple[str, ...] = ()
    review_ignored_posts: tuple[str, ...] = ()
    """Slack ts of review-channel posts that publish nothing (say, a digest that listed PRs)."""
    agent_prefix_from_ms: int = 0
    """When agents began starting their GitHub posts with the agent prefix (epoch ms). A comment under the shared
    account from before it, with neither the prefix nor a mark, stays unclear; 0: the prefix always decides."""
    host_worker_disabled_plugins: tuple[str, ...] = ()
    """Claude plugins turned off for host workers besides the defaults, e.g. one that must have a single poller."""
    worker_denied_mcp_servers: tuple[str, ...] = ()
    """MCP server names (e.g. "claude.ai Gmail") no worker, local or on a host, may use."""
    worker_denied_tools: tuple[str, ...] = ()
    """Tool names (e.g. "Artifact") no worker may use, local or on a host, wherever it starts."""
    review_excluded_repos: tuple[str, ...] = ()
    """owner/repo whose PRs never enter the review queue: no owner queue, no approval gate, no Slack post."""
    pr_issue_exempt_repos: tuple[str, ...] = ()
    """owner/repo of review_org whose PRs may name no issue: the guard lets gh pr create pass, the queue marks none."""
    """Base-branch globs (e.g. "release/*") whose PRs need no approval when their diff equals merged default-branch PRs
    they name; empty turns that off."""
    laptop_agent_cap: int = 0
    """Agents working at once on this Mac, leads and workers alike; 0 = no cap. Over it, guild worker queues."""
    worker_model: str = ""
    """The model a reopened host worker starts on when its record names none; `guild worker --model` names it now."""
    worker_host: str = ""
    """A [[remotes]] name where `guild worker` runs Claude by default, in tmux; empty runs workers on this Mac."""
    worker_path_map: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    """Local checkout (~ allowed) to the same checkout's absolute path on worker_host."""
    worker_host_watcher: str = ""
    """The [[watchers]] name that watches worker_host's Claude; while it reads failed, workers start on this Mac."""
    worker_trust_roots: tuple[str, ...] = ()
    """The owner's folders (~ allowed): a Mac worker's folder under one that Claude Code has not trusted yet is marked
    trusted at launch; elsewhere guild worker refuses it, since Claude would wait at its trust dialog."""
    heavy_job_load_per_core: float = 3.0
    """Load average per core over which a heavy check started on this Mac (a full test run, a build) gets a note to
    run it on worker_host; 0 turns the note off."""
    gh_quota_reserve: int = 1500
    """Core GitHub quota a worker's gh call must leave: below it the call is refused until the reset; 0 turns the
    guard off. Leads, the assistant and bridge's own polling keep this reserve for themselves."""
    review_post_dry_run: bool = True
    """Writes the Slack post to a log instead of sending it."""
    review_channel_id: str = ""
    review_mention_people: tuple[tuple[str, str], ...] = ()
    """(name, Slack user id) of colleagues the owner may mention in a review post, in the order the form shows them."""
    review_checkouts_dir: str = ""
    """Where each repo's main checkout lives, by repo name."""
    review_worktrees_dir: str = ""
    """Where large PRs' review worktrees go; empty prepares none."""
    review_setup_scripts: tuple[tuple[str, str], ...] = ()
    """(owner/repo, script path in the repo) for repos that build their own dev env; run in each review worktree in
    place of bridge's generic one."""
    dream_leads: tuple[str, ...] = ()
    """Leads whose night `guild dream` runs when it names none; "all" is every lead and the assistant."""
    dream_model: str = "opus"
    dream_timeout_s: int = 150
    """One dream's wall limit: the Mac may go to sleep soon after the night word."""
    watchers: tuple[Watcher, ...] = ()
    watcher_alert_to: str = ""
    """Who hears, once, that a watcher left ok and when it is back; empty sends nothing. A lead or the assistant is
    told at once; another session only at its next turn, as with any peer message."""
    watcher_escalate_after_min: int = 60
    """A watcher still not ok this long after it left ok is named to the assistant, once."""
    links: tuple[PageLink, ...] = ()
    restricted_paths: tuple[RestrictedPath, ...] = ()
    """Another team's code: a PR changing it needs a short why and the assistant's OK in its body."""
    prod_kube_contexts: tuple[str, ...] = ()
    """Kube contexts that are production, as names or fnmatch patterns (acme-prod, *-prod): the guard lets kubectl and
    helm only read there."""
    prod_kube_writable_namespaces: tuple[str, ...] = ()
    """Namespaces on a production context where kubectl and helm may write too, as names or fnmatch patterns
    (staging, pr-*): only when the command names its namespace literally with -n and no cluster-wide kind."""
    work_timezone: str = ""
    """IANA zone of the team's working hours (09-19) for daytime-only nudges; empty is this machine's zone."""
    work_days: tuple[int, ...] = (0, 1, 2, 3, 4)
    """Working days as datetime.weekday() counts them: Monday is 0."""
    weekly_task_day: int | None = None
    """The weekday (Monday is 0) the weekly task is sent on, from 09:00, by bridge serve or `guild day`, or the next
    day when that one sent none; None sends nothing."""
    weekly_task_to: str = ""
    """The lead or the assistant the weekly task goes to, as an ask of theirs."""
    weekly_task_file: str = ""
    """The task's text; '{week}' in it becomes the week, e.g. 2026-w47."""
    review_bot_name: str = "ReviewBot"
    """The review bot as people name it: its approval counts, and a review doc's Reviews section may name it."""
    required_reviews: tuple[str, ...] = ()
    """Reviews a large PR's doc must name in its Reviews section before it is ready for the owner."""
    review_stall_status: str = ""
    """Commit status context the review bot sets red on a PR it stalled on; empty watches none."""
    review_channel_name: str = "the review channel"
    """How texts name the chat channel of review posts (review_channel_id)."""
    review_owner_label: str = ""
    """GitHub label bridge puts on a queue PR once the owner pressed Publish, and refuses to agents; empty is off."""
    grooming_states: tuple[str, ...] = ()
    """Tracker states issue sync never moves an issue out of, lowercase; their leads are told once instead."""
    ready_states: tuple[str, ...] = ()
    """Tracker states the grooming bot leaves an issue in once grooming is done, lowercase: issue sync moves such an
    issue of a lead's project to In Progress with no work on it yet, when its last state change is grooming_bot's
    move out of a grooming state into one of these, made in the last ready_handoff_days. A state also in
    grooming_states stays put."""
    ready_handoff_days: int = 2
    """How recent the grooming bot's hand-off must be. A sweep sees one within minutes while the service runs; two days
    cover a Mac closed overnight or for a day, and an older one is backlog its lead has already had time to see."""
    grooming_bot: str = ""
    """The tracker user whose comments ask an issue's grooming questions; empty leaves its comments unread."""
    grooming_questions_re: str = ""
    """Matches the start of the grooming bot's comment that asks questions; its group 1 captures how many.
    Empty leaves its comments unread."""
    pages_dir: str = "~/pages"
    """Where published pages' sources live, one folder per slug."""
    disk_folders: tuple[str, ...] = ()
    """Worktree folders the disk check measures (~ allowed)."""
    spend_file: str = ""
    """JSON file another process writes with the week's token spend (~ allowed); the page's Spend this week block
    reads it, and is hidden while this is empty or the file is missing."""
    review_generated_files: tuple[str, ...] = ()
    """File names (fnmatch on the base name) left out of a PR's line count, besides lockfiles."""
    review_python_paths: tuple[str, ...] = ()
    """Repo-relative Python source roots that review worktrees add to the editor's and each service's import paths."""
    overlay_dir: str = ""
    host_sync_dirs: tuple[str, ...] = ()
    """More folders under home that every deploy copies to each worker host with their subfolders (skills a host
    worker also uses); the bridge skill and overlay_dir go anyway."""
    """Folder of this install's own texts (house rules), one <slot>.md per named slot; empty uses the generic texts."""
    people_dir: str = ""
    """Local folder whose people.toml holds the keys about people (PEOPLE_KEYS); set, config.toml may not hold them."""
    batch_count: int = 10
    """An idle tab is woken or typed into once this many messages bridge would type wait for it, the oldest waited
    batch_age_min, or one goes now; a busy tab's hooks deliver as they come."""
    batch_age_min: float = 5.0
    """The longest such a message waits for its batch; 0 wakes for each message at once."""
    shadow_filter: bool = False
    """Judges each message to a team lead after delivery, as a wake-up filter would, and only logs the verdict."""
    shadow_filter_url: str = "https://api.typesafe.ai/v1/systemone"
    shadow_filter_threshold: float = 0.2
    """A message the model gives at least this chance of needing the lead now would go at once."""
    shadow_filter_key_file: str = ""
    """File holding the classifier's API key (~ allowed); read for each call, never copied; doc_screen uses it too."""
    shadow_filter_live: bool = False
    """No longer holds anything: batch_count and batch_age_min hold wakes now, and the filter only logs."""
    shadow_filter_hold_min: int = 30
    """The longest a batched message waits before it wakes its lead anyway."""
    shadow_filter_refusal_phrases: tuple[str, ...] = ("Live now:", "is refused unless", "now refuses")
    """A message to a lead containing one of these (any case) announces a new refusal or rule and goes at once."""
    ask_autoclose_decision: str = ""
    """The owner's decision that verified asks close (e.g. "Q#12"), cited in each close on verification."""
    verifier_lead: str = ""
    """The verification lead: it verifies every claim while its tab is live and it is neither the ask's owner, its
    claimer nor the claimer's lead (otherwise, or when empty, another live lead with the fewest open verifications), and
    gets each night's new dreams to find candidate rules in (empty: nobody)."""
    asks_panel_max: int = 10
    """Asks the page's Asks panel lists as needing attention; past it, the panel says how many more."""
    doc_screen: bool = False
    """Sends each visible sentence of a registered review doc to the classifier (shadow_filter_url) and prints the
    sentences it flags as warnings. Needs shadow_filter_key_file."""
    doc_in_short_max_bullets: int = DEFAULT_LIMITS.bullets
    doc_bullet_max_words: int = DEFAULT_LIMITS.bullet_words
    """For each In short bullet."""
    doc_sentence_max_words: int = DEFAULT_LIMITS.sentence_words
    """For each sentence of a review doc's visible top: In short, User story if any and What changes and why, folds
    left out."""
    doc_visible_max_words: int = DEFAULT_LIMITS.visible_words
    """For all a review doc shows before anything is opened, Fields left out: fold summaries count, fold bodies,
    diagrams and code blocks do not."""
    auto_merge: str = "off"
    """off, dry-run (logs what it would merge) or on: bridge merges a queue PR by itself under the owner's standing
    rule (a colleague approved, CI green, the review bot cleared the head, no PR it merges after open)."""
    auto_merge_decision: str = ""
    """The owner's decision that set the standing rule (e.g. "Q#12"), cited in each merge's feed line."""
    auto_merge_failures_to: str = ""
    """Handle told of bridge's own failures: an automatic merge that fails, in place of the assistant; a failing review
    poll and a review state stalled past its limit, beside it. Empty: the assistant alone."""

    @property
    def owner_name_at_start(self) -> str:
        return self.owner_name[:1].upper() + self.owner_name[1:]

    @property
    def review_names(self) -> tuple[str, ...]:
        """The reviews a Reviews section is read for: the required ones, then the bot."""
        return tuple(dict.fromkeys(n for n in (*self.required_reviews, self.review_bot_name) if n))

    def hides(self, *names: str | None) -> bool:
        return any(n and any(mark in n for mark in HIDDEN_NAME_MARKS) for n in names)

    def pr_repo_for(self, repo: str | None) -> str | None:
        """Where a "PR #12" points when a session's cwd is a repo that has no PRs of its own."""
        return self.pr_repo_aliases.get(repo, repo) if repo else None


def state_dir() -> Path:
    return Path(os.environ.get("BRIDGE_STATE_DIR") or Path.home() / ".local/state/bridge")


def db_path() -> Path:
    return state_dir() / "bridge.db"


def config_path() -> Path:
    return Path(os.environ.get("BRIDGE_CONFIG") or Path.home() / ".config/bridge/config.toml")


def claude_sessions_dir() -> Path:
    return Path(os.environ.get("BRIDGE_CLAUDE_SESSIONS_DIR") or Path.home() / ".claude/sessions")


def claude_projects_dir() -> Path:
    return Path(os.environ.get("BRIDGE_CLAUDE_PROJECTS_DIR") or Path.home() / ".claude/projects")


PEOPLE_KEYS = ("owner_name", "user_alias", "review_author", "review_mention_people")
PEOPLE_FILE = "people.toml"


def load_config() -> Config:
    path = config_path()
    if not path.exists():
        return Config()
    with path.open("rb") as f:
        raw = tomllib.load(f)
    return map_toml_to_config(merge_people_file(raw))


def merge_people_file(raw: dict[str, object]) -> dict[str, object]:
    """People data lives outside config.toml when people_dir is set, so a shared config never carries it."""
    people_dir = raw.get("people_dir", "")
    if not people_dir:
        return raw
    if not isinstance(people_dir, str):
        raise ValueError("people_dir must be a string")
    misplaced = [key for key in PEOPLE_KEYS if key in raw]
    if misplaced:
        raise ValueError(f"{', '.join(misplaced)} belong in {PEOPLE_FILE} under people_dir, not in config.toml")
    path = Path(people_dir).expanduser() / PEOPLE_FILE
    if not path.exists():
        return raw
    with path.open("rb") as f:
        people = tomllib.load(f)
    unknown = sorted(set(people) - set(PEOPLE_KEYS))
    if unknown:
        raise ValueError(f"{path} may hold only {', '.join(PEOPLE_KEYS)}; not {', '.join(unknown)}")
    return {**raw, **people}


def read_overlay(config: Config, slot: str, default: str = "", **fields: str) -> str:
    """This install's text for a named slot: <overlay_dir>/<slot>.md without its final newline, else the default.
    Fields such as {owner} are filled in; a text whose braces do not parse is used as written."""
    path = Path(config.overlay_dir).expanduser() / f"{slot}.md" if config.overlay_dir else None
    text = path.read_text(encoding="utf-8").removesuffix("\n") if path is not None and path.is_file() else default
    try:
        return text.format_map(fields)
    except (KeyError, IndexError, ValueError):
        return text


def map_toml_to_config(raw: dict[str, object]) -> Config:
    defaults = Config()
    raw_prefixes = raw.get("issue_prefixes", [])
    prefixes = cast(list[object], raw_prefixes) if isinstance(raw_prefixes, list) else None
    if prefixes is None or not all(isinstance(p, str) for p in prefixes):
        raise ValueError("issue_prefixes must be a list of strings")
    config = Config(
        issue_prefixes=tuple(str(p) for p in prefixes),
        agtermctl=_str(raw, "agtermctl", defaults.agtermctl),
        user_alias=_str(raw, "user_alias", defaults.user_alias),
        issue_url=_str(raw, "issue_url", defaults.issue_url),
        linear_api_key_file=_str(raw, "linear_api_key_file", defaults.linear_api_key_file),
        remotes=tuple(map_table_to_remote(t) for t in _tables(raw, "remotes")),
        pr_repo_aliases=MappingProxyType(_str_table(raw, "pr_repo_aliases")),
        pr_short_names=MappingProxyType(_str_table(raw, "pr_short_names")),
        assistant_handle=_str(raw, "assistant_handle", defaults.assistant_handle),
        lead_skills=_str_list(raw, "lead_skills", defaults.lead_skills),
        owner_name=_str(raw, "owner_name", defaults.owner_name),
        night_words=_str_list(raw, "night_words", defaults.night_words),
        morning_words=_str_list(raw, "morning_words", defaults.morning_words),
        confusion_phrases=_str_list(raw, "confusion_phrases", defaults.confusion_phrases),
        compact_at_tokens=_int(raw, "compact_at_tokens", defaults.compact_at_tokens),
        parallel_streams=_str_list(raw, "parallel_streams", defaults.parallel_streams),
        review_org=_str(raw, "review_org", defaults.review_org),
        review_author=_str(raw, "review_author", defaults.review_author),
        review_bot_logins=_str_list(raw, "review_bot_logins", defaults.review_bot_logins),
        review_ui_paths=_str_list(raw, "review_ui_paths", defaults.review_ui_paths),
        backport_bases=_str_list(raw, "backport_bases", defaults.backport_bases),
        review_excluded_repos=_str_list(raw, "review_excluded_repos", defaults.review_excluded_repos),
        pr_issue_exempt_repos=_str_list(raw, "pr_issue_exempt_repos", defaults.pr_issue_exempt_repos),
        laptop_agent_cap=_int(raw, "laptop_agent_cap", defaults.laptop_agent_cap),
        worker_model=_str(raw, "worker_model", defaults.worker_model),
        worker_host=_str(raw, "worker_host", defaults.worker_host),
        worker_path_map=MappingProxyType(map_table_to_worker_path_map(_str_table(raw, "worker_path_map"))),
        worker_host_watcher=_str(raw, "worker_host_watcher", defaults.worker_host_watcher),
        worker_trust_roots=_str_list(raw, "worker_trust_roots", defaults.worker_trust_roots),
        heavy_job_load_per_core=_non_negative(raw, "heavy_job_load_per_core", defaults.heavy_job_load_per_core),
        gh_quota_reserve=int(_non_negative(raw, "gh_quota_reserve", defaults.gh_quota_reserve)),
        review_post_dry_run=_bool(raw, "review_post_dry_run", defaults.review_post_dry_run),
        review_ignored_posts=_str_list(raw, "review_ignored_posts", defaults.review_ignored_posts),
        agent_prefix_from_ms=_int(raw, "agent_prefix_from_ms", defaults.agent_prefix_from_ms),
        host_worker_disabled_plugins=_str_list(
            raw, "host_worker_disabled_plugins", defaults.host_worker_disabled_plugins
        ),
        worker_denied_mcp_servers=_str_list(raw, "worker_denied_mcp_servers", defaults.worker_denied_mcp_servers),
        worker_denied_tools=_str_list(raw, "worker_denied_tools", defaults.worker_denied_tools),
        review_channel_id=_str(raw, "review_channel_id", defaults.review_channel_id),
        review_mention_people=map_table_to_mention_people(_str_table(raw, "review_mention_people")),
        review_checkouts_dir=_str(raw, "review_checkouts_dir", defaults.review_checkouts_dir),
        review_worktrees_dir=_str(raw, "review_worktrees_dir", defaults.review_worktrees_dir),
        review_setup_scripts=map_table_to_setup_scripts(_str_table(raw, "review_setup_scripts")),
        dream_leads=_str_list(raw, "dream_leads", defaults.dream_leads),
        dream_model=_str(raw, "dream_model", defaults.dream_model),
        dream_timeout_s=_int(raw, "dream_timeout_s", defaults.dream_timeout_s),
        watchers=tuple(map_table_to_watcher(t) for t in _tables(raw, "watchers")),
        watcher_alert_to=_str(raw, "watcher_alert_to", defaults.watcher_alert_to).strip(),
        watcher_escalate_after_min=_positive(raw, "watcher_escalate_after_min", defaults.watcher_escalate_after_min),
        links=tuple(map_table_to_link(t) for t in _tables(raw, "links")),
        restricted_paths=tuple(map_table_to_restricted_path(t) for t in _tables(raw, "restricted_paths")),
        prod_kube_contexts=_str_list(raw, "prod_kube_contexts", defaults.prod_kube_contexts),
        prod_kube_writable_namespaces=_str_list(
            raw, "prod_kube_writable_namespaces", defaults.prod_kube_writable_namespaces
        ),
        work_timezone=_zone(raw, "work_timezone", defaults.work_timezone),
        work_days=_weekdays(raw, "work_days", defaults.work_days),
        weekly_task_day=_weekday_or_none(raw, "weekly_task_day"),
        weekly_task_to=_str(raw, "weekly_task_to", defaults.weekly_task_to),
        weekly_task_file=_str(raw, "weekly_task_file", defaults.weekly_task_file),
        review_bot_name=_str(raw, "review_bot_name", defaults.review_bot_name),
        required_reviews=_str_list(raw, "required_reviews", defaults.required_reviews),
        review_stall_status=_str(raw, "review_stall_status", defaults.review_stall_status),
        review_channel_name=_str(raw, "review_channel_name", defaults.review_channel_name),
        review_owner_label=_str(raw, "review_owner_label", defaults.review_owner_label).strip(),
        grooming_states=tuple(s.lower() for s in _str_list(raw, "grooming_states", defaults.grooming_states)),
        ready_states=tuple(s.lower() for s in _str_list(raw, "ready_states", defaults.ready_states)),
        ready_handoff_days=_positive(raw, "ready_handoff_days", defaults.ready_handoff_days),
        grooming_bot=_str(raw, "grooming_bot", defaults.grooming_bot).strip(),
        grooming_questions_re=_questions_regex(raw, "grooming_questions_re", defaults.grooming_questions_re),
        pages_dir=_str(raw, "pages_dir", defaults.pages_dir),
        disk_folders=_str_list(raw, "disk_folders", defaults.disk_folders),
        spend_file=_str(raw, "spend_file", defaults.spend_file),
        review_generated_files=_str_list(raw, "review_generated_files", defaults.review_generated_files),
        review_python_paths=_str_list(raw, "review_python_paths", defaults.review_python_paths),
        overlay_dir=_str(raw, "overlay_dir", defaults.overlay_dir),
        host_sync_dirs=_str_list(raw, "host_sync_dirs", defaults.host_sync_dirs),
        people_dir=_str(raw, "people_dir", defaults.people_dir),
        batch_count=_positive(raw, "batch_count", defaults.batch_count),
        batch_age_min=_non_negative(raw, "batch_age_min", defaults.batch_age_min),
        shadow_filter=_bool(raw, "shadow_filter", defaults.shadow_filter),
        shadow_filter_url=_str(raw, "shadow_filter_url", defaults.shadow_filter_url),
        shadow_filter_threshold=_fraction(raw, "shadow_filter_threshold", defaults.shadow_filter_threshold),
        shadow_filter_key_file=_str(raw, "shadow_filter_key_file", defaults.shadow_filter_key_file),
        shadow_filter_live=_bool(raw, "shadow_filter_live", defaults.shadow_filter_live),
        shadow_filter_hold_min=_positive(raw, "shadow_filter_hold_min", defaults.shadow_filter_hold_min),
        shadow_filter_refusal_phrases=_str_list(
            raw, "shadow_filter_refusal_phrases", defaults.shadow_filter_refusal_phrases
        ),
        ask_autoclose_decision=_str(raw, "ask_autoclose_decision", defaults.ask_autoclose_decision),
        verifier_lead=_str(raw, "verifier_lead", defaults.verifier_lead).strip().lower(),
        asks_panel_max=_positive(raw, "asks_panel_max", defaults.asks_panel_max),
        doc_screen=_bool(raw, "doc_screen", defaults.doc_screen),
        doc_in_short_max_bullets=_positive(raw, "doc_in_short_max_bullets", defaults.doc_in_short_max_bullets),
        doc_bullet_max_words=_positive(raw, "doc_bullet_max_words", defaults.doc_bullet_max_words),
        doc_sentence_max_words=_positive(raw, "doc_sentence_max_words", defaults.doc_sentence_max_words),
        doc_visible_max_words=_positive(raw, "doc_visible_max_words", defaults.doc_visible_max_words),
        auto_merge=_choice(raw, "auto_merge", defaults.auto_merge, AUTO_MERGE_MODES),
        auto_merge_decision=_str(raw, "auto_merge_decision", defaults.auto_merge_decision),
        auto_merge_failures_to=_str(raw, "auto_merge_failures_to", defaults.auto_merge_failures_to),
    )
    if config.shadow_filter_live and not config.shadow_filter:
        # Only the filter's loop lets a held message through before its hold runs out.
        raise ValueError("shadow_filter_live needs shadow_filter = true")
    named = [w for w in config.watchers if w.name == config.worker_host_watcher]
    if config.worker_host_watcher and len(named) != 1:
        raise ValueError(f"worker_host_watcher {config.worker_host_watcher!r} must name exactly one [[watchers]] entry")
    return config


def _weekday_or_none(raw: dict[str, object], key: str) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 6:
        raise ValueError(f"{key} must be a weekday from 0 (Monday) to 6 (Sunday)")
    return value


def _int(raw: dict[str, object], key: str, default: int) -> int:
    value = raw.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return value


def _positive(raw: dict[str, object], key: str, default: int) -> int:
    value = _int(raw, key, default)
    if value < 1:
        raise ValueError(f"{key} must be at least 1")
    return value


def _non_negative(raw: dict[str, object], key: str, default: float) -> float:
    value = raw.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} must be a number of at least 0")
    return float(value)


def _fraction(raw: dict[str, object], key: str, default: float) -> float:
    value = raw.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1:
        raise ValueError(f"{key} must be a number from 0 to 1")
    return float(value)


def _choice(raw: dict[str, object], key: str, default: str, choices: tuple[str, ...]) -> str:
    value = _str(raw, key, default)
    if value not in choices:
        raise ValueError(f"{key} must be one of {', '.join(choices)}")
    return value


def _bool(raw: dict[str, object], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _questions_regex(raw: dict[str, object], key: str, default: str) -> str:
    pattern = _str(raw, key, default)
    try:
        groups = re.compile(pattern).groups
    except re.error as exc:
        raise ValueError(f"{key} is not a valid regular expression: {exc}") from None
    if pattern and groups < 1:
        raise ValueError(f"{key} must capture the number of questions as its group 1")
    return pattern


def _str(raw: dict[str, object], key: str, default: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _str_list(raw: dict[str, object], key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = raw.get(key, list(default))
    items = cast(list[object], value) if isinstance(value, list) else None
    if items is None or not all(isinstance(v, str) and v for v in items):
        raise ValueError(f"{key} must be a list of non-empty strings")
    return tuple(str(v) for v in items)


def _zone(raw: dict[str, object], key: str, default: str) -> str:
    value = _str(raw, key, default)
    if value:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"{key} must be an IANA time zone like Europe/Berlin") from None
    return value


def _weekdays(raw: dict[str, object], key: str, default: tuple[int, ...]) -> tuple[int, ...]:
    value = raw.get(key, list(default))
    items = cast(list[object], value) if isinstance(value, list) else None
    if items is None or not all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 6 for v in items):
        raise ValueError(f"{key} must be a list of weekday numbers, 0 (Monday) to 6")
    return tuple(cast(list[int], items))


def _str_table(raw: dict[str, object], key: str) -> dict[str, str]:
    value = raw.get(key, {})
    table = cast(dict[str, object], value) if isinstance(value, dict) else None
    if table is None or not all(isinstance(v, str) for v in table.values()):
        raise ValueError(f"{key} must be a table of strings")
    return {k: str(v) for k, v in table.items()}


def map_table_to_setup_scripts(table: dict[str, str]) -> tuple[tuple[str, str], ...]:
    for repo, script in table.items():
        parts = Path(script).parts
        if "/" not in repo or not parts or Path(script).is_absolute() or ".." in parts:
            raise ValueError(f"review_setup_scripts.{repo} must map owner/repo to a path inside the repo")
    return tuple(table.items())


def map_table_to_worker_path_map(table: dict[str, str]) -> dict[str, str]:
    for local, remote in table.items():
        if not local or not remote.startswith("/") or ".." in Path(remote).parts:
            raise ValueError(f"worker_path_map.{local} must map a local path to an absolute path on the worker host")
    return table


def map_table_to_mention_people(table: dict[str, str]) -> tuple[tuple[str, str], ...]:
    for name, slack_id in table.items():
        if not re.fullmatch(r"[UW][A-Z0-9]{6,}", slack_id):
            raise ValueError(f"review_mention_people.{name} must be a Slack user id like U01AB2CD3EF")
    return tuple(table.items())


def _tables(raw: dict[str, object], key: str) -> list[dict[str, object]]:
    value = raw.get(key, [])
    items = cast(list[object], value) if isinstance(value, list) else None
    if items is None or not all(isinstance(i, dict) for i in items):
        raise ValueError(f"{key} must be an array of tables ([[{key}]])")
    return [cast(dict[str, object], i) for i in items]


def map_table_to_remote(table: dict[str, object]) -> Remote:
    raw_aliases = table.get("aliases", [])
    aliases = cast(list[object], raw_aliases) if isinstance(raw_aliases, list) else None
    if aliases is None or not all(isinstance(a, str) for a in aliases):
        raise ValueError("remotes.aliases must be a list of strings")
    return Remote(
        name=_str(table, "name", ""),
        ssh=_str(table, "ssh", ""),
        aliases=tuple(str(a) for a in aliases),
        share_tabs=_bool(table, "share_tabs", False),
    )


def map_table_to_watcher(table: dict[str, object]) -> Watcher:
    path = _str(table, "path", "")
    if not path or "\0" in path or "\n" in path:
        raise ValueError("each [[watchers]] needs a path")
    return Watcher(path=path, name=_str(table, "name", ""))


def map_table_to_restricted_path(table: dict[str, object]) -> RestrictedPath:
    path, label, owner = (_str(table, key, "").strip() for key in ("path", "label", "owner"))
    # Git names files as services/pricing/x.py: never ./services or services//pricing.
    normal = PurePosixPath(path).as_posix().strip("/")
    if normal in ("", ".") or not label or not owner or path.startswith("/") or ".." in normal.split("/"):
        raise ValueError("each [[restricted_paths]] needs a repo-relative path, a label and an owner")
    return RestrictedPath(path=normal + "/" if path.endswith("/") else normal, label=label, owner=owner)


def map_table_to_link(table: dict[str, object]) -> PageLink:
    name, url = _str(table, "name", "").strip(), _str(table, "url", "").strip()
    if not name or not url.startswith(("https://", "http://")):
        raise ValueError("each [[links]] needs a name and an http(s) url")
    return PageLink(name=name, url=url)
