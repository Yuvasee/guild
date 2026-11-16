import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

HIDDEN_NAME_MARKS = ("🔒", "🔐", "🔏")
"""Tabs or workspaces whose name contains one of these are left out of the dashboard."""


@dataclass(frozen=True)
class Remote:
    name: str
    ssh: str
    aliases: tuple[str, ...] = ()
    """Other ssh host names that reach the same machine (e.g. a second ssh alias), for spotting who drives it."""

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
    last: bool = False
    """Shown after the page's own links instead of before them."""


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
    assistant_handle: str = "aide"
    """The one session that may relay the user's answers to posts (`guild answer`)."""
    owner_name: str = "the owner"
    """How sessions and messages to leads name the person who leads the guild; the page itself says "you"."""
    night_words: tuple[str, ...] = ("Good night",)
    """What the owner says at the end of the day: the night's dreams follow."""
    morning_words: tuple[str, ...] = ("Good morning",)
    """What the owner says in the morning to run the morning report (`guild day`)."""
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
    backport_bases: tuple[str, ...] = ()
    review_ignored_posts: tuple[str, ...] = ()
    """Slack ts of review-channel posts that publish nothing (say, a digest that listed PRs)."""
    agent_prefix_from_ms: int = 0
    """When agents began starting their GitHub posts with the agent prefix (epoch ms). A comment under the shared
    account from before it, with neither the prefix nor a mark, stays unclear; 0: the prefix always decides."""
    host_worker_disabled_plugins: tuple[str, ...] = ()
    """Claude plugins turned off for host workers besides the defaults, e.g. one that must have a single poller."""
    review_excluded_repos: tuple[str, ...] = ()
    """owner/repo whose PRs never enter the review queue: no owner queue, no approval gate, no Slack post."""
    """Base-branch globs (e.g. "release/*") whose PRs need no approval when their diff equals merged default-branch PRs
    they name; empty turns that off."""
    laptop_agent_cap: int = 0
    """Agents working at once on this Mac, leads and workers alike; 0 = no cap. Over it, guild worker queues."""
    worker_model: str = ""
    """The model every worker's Claude starts on (--model), here or on the host; empty: the user settings' model."""
    worker_host: str = ""
    """A [[remotes]] name where `guild worker` runs Claude by default, in tmux; empty runs workers on this Mac."""
    worker_path_map: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    """Local checkout (~ allowed) to the same checkout's absolute path on worker_host."""
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
    links: tuple[PageLink, ...] = ()
    work_timezone: str = ""
    """IANA zone of the team's working hours (09-19) for daytime-only nudges; empty is this machine's zone."""
    work_days: tuple[int, ...] = (0, 1, 2, 3, 4)
    """Working days as datetime.weekday() counts them: Monday is 0."""
    review_bot_name: str = "ReviewBot"
    """The review bot as people name it: its approval counts, and a review doc's Reviews section may name it."""
    required_reviews: tuple[str, ...] = ()
    """Reviews a large PR's doc must name in its Reviews section before it is ready for the owner."""
    review_stall_status: str = ""
    """Commit status context the review bot sets red on a PR it stalled on; empty watches none."""
    review_channel_name: str = "the review channel"
    """How texts name the chat channel of review posts (review_channel_id)."""
    grooming_states: tuple[str, ...] = ()
    """Tracker states issue sync never moves an issue out of, lowercase; their leads are told once instead."""
    pages_dir: str = "~/pages"
    """Where published pages' sources live, one folder per slug."""
    disk_folders: tuple[str, ...] = ()
    """Worktree folders the disk check measures (~ allowed)."""
    review_generated_files: tuple[str, ...] = ()
    """File names (fnmatch on the base name) left out of a PR's line count, besides lockfiles."""
    review_python_paths: tuple[str, ...] = ()
    """Repo-relative Python source roots that review worktrees add to the editor's and each service's import paths."""
    overlay_dir: str = ""
    """Folder of this install's own texts (house rules), one <slot>.md per named slot; empty uses the generic texts."""
    people_dir: str = ""
    """Local folder whose people.toml holds the keys about people (PEOPLE_KEYS); set, config.toml may not hold them."""
    ask_autoclose_decision: str = ""
    """The owner's decision that verified asks close by themselves (e.g. "Q#12"), cited in each automatic close."""
    asks_panel_max: int = 10
    """Asks the page's Asks panel lists as needing attention; past it, the panel says how many more."""

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
        """Where bare #12 refs point when a session's cwd is a repo that has no PRs of its own."""
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
    return Config(
        issue_prefixes=tuple(str(p) for p in prefixes),
        agtermctl=_str(raw, "agtermctl", defaults.agtermctl),
        user_alias=_str(raw, "user_alias", defaults.user_alias),
        issue_url=_str(raw, "issue_url", defaults.issue_url),
        linear_api_key_file=_str(raw, "linear_api_key_file", defaults.linear_api_key_file),
        remotes=tuple(map_table_to_remote(t) for t in _tables(raw, "remotes")),
        pr_repo_aliases=MappingProxyType(_str_table(raw, "pr_repo_aliases")),
        assistant_handle=_str(raw, "assistant_handle", defaults.assistant_handle),
        owner_name=_str(raw, "owner_name", defaults.owner_name),
        night_words=_str_list(raw, "night_words", defaults.night_words),
        morning_words=_str_list(raw, "morning_words", defaults.morning_words),
        compact_at_tokens=_int(raw, "compact_at_tokens", defaults.compact_at_tokens),
        parallel_streams=_str_list(raw, "parallel_streams", defaults.parallel_streams),
        review_org=_str(raw, "review_org", defaults.review_org),
        review_author=_str(raw, "review_author", defaults.review_author),
        review_bot_logins=_str_list(raw, "review_bot_logins", defaults.review_bot_logins),
        backport_bases=_str_list(raw, "backport_bases", defaults.backport_bases),
        review_excluded_repos=_str_list(raw, "review_excluded_repos", defaults.review_excluded_repos),
        laptop_agent_cap=_int(raw, "laptop_agent_cap", defaults.laptop_agent_cap),
        worker_model=_str(raw, "worker_model", defaults.worker_model),
        worker_host=_str(raw, "worker_host", defaults.worker_host),
        worker_path_map=MappingProxyType(map_table_to_worker_path_map(_str_table(raw, "worker_path_map"))),
        review_post_dry_run=_bool(raw, "review_post_dry_run", defaults.review_post_dry_run),
        review_ignored_posts=_str_list(raw, "review_ignored_posts", defaults.review_ignored_posts),
        agent_prefix_from_ms=_int(raw, "agent_prefix_from_ms", defaults.agent_prefix_from_ms),
        host_worker_disabled_plugins=_str_list(
            raw, "host_worker_disabled_plugins", defaults.host_worker_disabled_plugins
        ),
        review_channel_id=_str(raw, "review_channel_id", defaults.review_channel_id),
        review_mention_people=map_table_to_mention_people(_str_table(raw, "review_mention_people")),
        review_checkouts_dir=_str(raw, "review_checkouts_dir", defaults.review_checkouts_dir),
        review_worktrees_dir=_str(raw, "review_worktrees_dir", defaults.review_worktrees_dir),
        review_setup_scripts=map_table_to_setup_scripts(_str_table(raw, "review_setup_scripts")),
        dream_leads=_str_list(raw, "dream_leads", defaults.dream_leads),
        dream_model=_str(raw, "dream_model", defaults.dream_model),
        dream_timeout_s=_int(raw, "dream_timeout_s", defaults.dream_timeout_s),
        watchers=tuple(map_table_to_watcher(t) for t in _tables(raw, "watchers")),
        links=tuple(map_table_to_link(t) for t in _tables(raw, "links")),
        work_timezone=_zone(raw, "work_timezone", defaults.work_timezone),
        work_days=_weekdays(raw, "work_days", defaults.work_days),
        review_bot_name=_str(raw, "review_bot_name", defaults.review_bot_name),
        required_reviews=_str_list(raw, "required_reviews", defaults.required_reviews),
        review_stall_status=_str(raw, "review_stall_status", defaults.review_stall_status),
        review_channel_name=_str(raw, "review_channel_name", defaults.review_channel_name),
        grooming_states=tuple(s.lower() for s in _str_list(raw, "grooming_states", defaults.grooming_states)),
        pages_dir=_str(raw, "pages_dir", defaults.pages_dir),
        disk_folders=_str_list(raw, "disk_folders", defaults.disk_folders),
        review_generated_files=_str_list(raw, "review_generated_files", defaults.review_generated_files),
        review_python_paths=_str_list(raw, "review_python_paths", defaults.review_python_paths),
        overlay_dir=_str(raw, "overlay_dir", defaults.overlay_dir),
        people_dir=_str(raw, "people_dir", defaults.people_dir),
        ask_autoclose_decision=_str(raw, "ask_autoclose_decision", defaults.ask_autoclose_decision),
        asks_panel_max=_int(raw, "asks_panel_max", defaults.asks_panel_max),
    )


def _int(raw: dict[str, object], key: str, default: int) -> int:
    value = raw.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return value


def _bool(raw: dict[str, object], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


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
    return Remote(name=_str(table, "name", ""), ssh=_str(table, "ssh", ""), aliases=tuple(str(a) for a in aliases))


def map_table_to_watcher(table: dict[str, object]) -> Watcher:
    path = _str(table, "path", "")
    if not path or "\0" in path:
        raise ValueError("each [[watchers]] needs a path")
    return Watcher(path=path, name=_str(table, "name", ""))


def map_table_to_link(table: dict[str, object]) -> PageLink:
    name, url = _str(table, "name", "").strip(), _str(table, "url", "").strip()
    if not name or not url.startswith(("https://", "http://")):
        raise ValueError("each [[links]] needs a name and an http(s) url")
    last = table.get("last", False)
    if not isinstance(last, bool):
        raise ValueError("[[links]] last must be true or false")
    return PageLink(name=name, url=url, last=last)
