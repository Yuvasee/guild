import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import cast

TRACKERS = ("tasks", "exec", "linear", "none")
CHATS = ("none", "slack", "file", "webhook")


@dataclass(frozen=True)
class GuardRule:
    trigger: str
    """Plain substring the shell guard looks for before starting Python."""
    pattern: re.Pattern[str]
    resource: str


@dataclass(frozen=True)
class Remote:
    name: str
    ssh: str
    aliases: tuple[str, ...] = ()
    """Other ssh host names that reach the same machine (e.g. a jump-host alias), for spotting who drives it."""

    def host_names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.ssh, self.name, *self.aliases)))


@dataclass(frozen=True)
class Watcher:
    path: str
    """A status JSON that a background job rewrites after every run; ~ is expanded when read."""
    name: str = ""
    """Shown while the file is missing or unreadable; otherwise the file names itself."""


@dataclass(frozen=True)
class Config:
    issue_prefixes: tuple[str, ...] = ()
    stale_after_s: int = 900
    focus_stale_after_s: int = 7200
    focus_nudge_every_s: int = 1800
    focus_nudge_min_turns: int = 2
    pin_restore: bool = True
    agtermctl: str = "agtermctl"
    user_alias: str = ""
    issue_url: str = ""
    """Template for issue links, e.g. https://tracker.example/browse/{key}; empty uses the tracker's own link."""
    terminal_app: str = "agterm"
    linear_api_key_file: str = ""
    tracker: str = "tasks"
    """Where issue state comes from: "tasks" (a local file), "exec" (tracker_command), "linear" or "none"."""
    tasks_file: str = ""
    """The tasks tracker's Markdown checklist or JSON file; empty is tasks.md next to config.toml."""
    tracker_command: tuple[str, ...] = ()
    """The exec tracker's argv: JSON in on stdin, JSON out on stdout (docs/adapters.md)."""
    guards: tuple[GuardRule, ...] = ()
    remotes: tuple[Remote, ...] = ()
    pr_repo_aliases: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    repo_labels: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    """Short tags shown before PR numbers, overriding the automatic four letters."""
    assistant_handle: str = "aide"
    """The one session that may relay the user's answers to posts (`guild answer`)."""
    owner_name: str = "the owner"
    """How sessions and messages to leads name the person who leads the guild; the page itself says "you"."""
    night_words: tuple[str, ...] = ("Good night",)
    """What the owner says to start the night's dreams (`guild dream`)."""
    compact_at_tokens: int = 0
    """Context size at which a session saves notes and, in agterm, bridge compacts it right after; 0 is off."""
    hidden_name_marks: tuple[str, ...] = ("🔒", "🔐", "🔏")
    """Tabs or workspaces whose name starts with one of these are left out of the dashboard."""
    review_org: str = ""
    """GitHub org of the review queue; empty turns the queue and all its jobs off."""
    review_author: str = ""
    """The GitHub account the queue's PRs and the user's comments come from."""
    review_bot_logins: tuple[str, ...] = ()
    """Accounts whose reviews and review requests do not mean a person has seen the PR."""
    review_loc_excluded: tuple[str, ...] = (
        "uv.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    )  # fmt: skip
    """File names left out of a PR's line count (fnmatch on the base name)."""
    review_large_loc: int = 100
    worker_cap: int = 5
    """Live workers one lead may run at once; `guild worker` refuses one more."""
    review_post_dry_run: bool = True
    """Writes the review post to a log instead of sending it."""
    chat: str = ""
    """Where Publish posts a PR: "none" (nothing is posted), "slack", "file" (chat_file) or "webhook"; empty is
    Slack when review_channel_id is set, else none."""
    chat_webhook_url: str = ""
    """The webhook chat's incoming-webhook URL; it gets {"text": …, "content": …} as JSON."""
    chat_file: str = ""
    """The file chat's JSON-lines log; empty is chat-posts.log in the state dir."""
    review_channel_id: str = ""
    review_mention_people: tuple[tuple[str, str], ...] = ()
    """(name, chat user id) of colleagues they may mention in a review post, in the order the form shows them; Slack
    wants member ids (U…), other chats any handle."""
    review_post_model: str = "haiku"
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
    """One dream's wall limit, so the night's dreams end in good time."""
    notify_command: str = "osascript"
    """Shows an urgent question's macOS notification; agtermctl notify would raise a badge on a tab."""
    watchers: tuple[Watcher, ...] = ()
    ask_stale_hours: int = 4
    """An open or in-progress ask with no update this long nags its owner and the assistant, and again each period."""
    ask_verify_hours: int = 1
    """A claimed-done ask unverified this long nags the same way."""
    ask_autoclose_hours: int = 24
    """A verified ask closes by itself this long after, unless the owner reopens it."""
    work_timezone: str = ""
    """IANA zone of the team's working hours (09-19) for daytime-only nudges; empty is this machine's zone."""
    work_days: tuple[int, ...] = (0, 1, 2, 3, 4)
    """Working days as datetime.weekday() counts them: Monday is 0."""

    @property
    def chat_adapter(self) -> str:
        return self.chat or ("slack" if self.review_channel_id else "none")

    @property
    def owner_name_at_start(self) -> str:
        return self.owner_name[:1].upper() + self.owner_name[1:]

    def hides(self, *names: str | None) -> bool:
        return any(n and any(mark in n for mark in self.hidden_name_marks) for n in names)

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


def load_config() -> Config:
    path = config_path()
    if not path.exists():
        return Config()
    with path.open("rb") as f:
        raw = tomllib.load(f)
    return map_toml_to_config(raw)


def map_toml_to_config(raw: dict[str, object]) -> Config:
    defaults = Config()
    raw_prefixes = raw.get("issue_prefixes", [])
    prefixes = cast(list[object], raw_prefixes) if isinstance(raw_prefixes, list) else None
    if prefixes is None or not all(isinstance(p, str) for p in prefixes):
        raise ValueError("issue_prefixes must be a list of strings")
    return Config(
        issue_prefixes=tuple(str(p) for p in prefixes),
        stale_after_s=_int(raw, "stale_after_s", defaults.stale_after_s),
        focus_stale_after_s=_int(raw, "focus_stale_after_s", defaults.focus_stale_after_s),
        focus_nudge_every_s=_int(raw, "focus_nudge_every_s", defaults.focus_nudge_every_s),
        focus_nudge_min_turns=_int(raw, "focus_nudge_min_turns", defaults.focus_nudge_min_turns),
        pin_restore=_bool(raw, "pin_restore", defaults.pin_restore),
        agtermctl=_str(raw, "agtermctl", defaults.agtermctl),
        user_alias=_str(raw, "user_alias", defaults.user_alias),
        issue_url=_str(raw, "issue_url", defaults.issue_url),
        terminal_app=_str(raw, "terminal_app", defaults.terminal_app),
        linear_api_key_file=_str(raw, "linear_api_key_file", defaults.linear_api_key_file),
        tracker=_tracker(raw),
        tasks_file=_str(raw, "tasks_file", defaults.tasks_file),
        tracker_command=_str_list(raw, "tracker_command", defaults.tracker_command),
        guards=tuple(map_table_to_guard(t) for t in _tables(raw, "guards")),
        remotes=tuple(map_table_to_remote(t) for t in _tables(raw, "remotes")),
        pr_repo_aliases=MappingProxyType(_str_table(raw, "pr_repo_aliases")),
        repo_labels=MappingProxyType(_str_table(raw, "repo_labels")),
        assistant_handle=_str(raw, "assistant_handle", defaults.assistant_handle),
        owner_name=_str(raw, "owner_name", defaults.owner_name),
        night_words=_str_list(raw, "night_words", defaults.night_words),
        compact_at_tokens=_int(raw, "compact_at_tokens", defaults.compact_at_tokens),
        hidden_name_marks=_str_list(raw, "hidden_name_marks", defaults.hidden_name_marks),
        review_org=_str(raw, "review_org", defaults.review_org),
        review_author=_str(raw, "review_author", defaults.review_author),
        review_bot_logins=_str_list(raw, "review_bot_logins", defaults.review_bot_logins),
        review_loc_excluded=_str_list(raw, "review_loc_excluded", defaults.review_loc_excluded),
        review_large_loc=_int(raw, "review_large_loc", defaults.review_large_loc),
        worker_cap=_int(raw, "worker_cap", defaults.worker_cap),
        review_post_dry_run=_bool(raw, "review_post_dry_run", defaults.review_post_dry_run),
        review_channel_id=_str(raw, "review_channel_id", defaults.review_channel_id),
        chat=_chat(raw),
        chat_webhook_url=_str(raw, "chat_webhook_url", defaults.chat_webhook_url),
        chat_file=_str(raw, "chat_file", defaults.chat_file),
        review_mention_people=map_table_to_mention_people(
            _str_table(raw, "review_mention_people"),
            _chat(raw) or ("slack" if raw.get("review_channel_id") else "none"),
        ),
        review_post_model=_str(raw, "review_post_model", defaults.review_post_model),
        review_checkouts_dir=_str(raw, "review_checkouts_dir", defaults.review_checkouts_dir),
        review_worktrees_dir=_str(raw, "review_worktrees_dir", defaults.review_worktrees_dir),
        review_setup_scripts=map_table_to_setup_scripts(_str_table(raw, "review_setup_scripts")),
        dream_leads=_str_list(raw, "dream_leads", defaults.dream_leads),
        dream_model=_str(raw, "dream_model", defaults.dream_model),
        dream_timeout_s=_int(raw, "dream_timeout_s", defaults.dream_timeout_s),
        notify_command=_str(raw, "notify_command", defaults.notify_command),
        watchers=tuple(map_table_to_watcher(t) for t in _tables(raw, "watchers")),
        ask_stale_hours=_int(raw, "ask_stale_hours", defaults.ask_stale_hours),
        ask_verify_hours=_int(raw, "ask_verify_hours", defaults.ask_verify_hours),
        ask_autoclose_hours=_int(raw, "ask_autoclose_hours", defaults.ask_autoclose_hours),
        work_timezone=_str(raw, "work_timezone", defaults.work_timezone),
        work_days=_weekdays(raw, "work_days", defaults.work_days),
    )


def _tracker(raw: dict[str, object]) -> str:
    tracker = _str(raw, "tracker", Config.tracker)
    if tracker not in TRACKERS:
        raise ValueError(f"tracker must be one of {', '.join(TRACKERS)}")
    if tracker == "exec" and not raw.get("tracker_command"):
        raise ValueError('tracker = "exec" needs tracker_command, e.g. ["~/bin/my-tracker"]')
    return tracker


def _chat(raw: dict[str, object]) -> str:
    chat = _str(raw, "chat", Config.chat)
    if chat and chat not in CHATS:
        raise ValueError(f"chat must be one of {', '.join(CHATS)}")
    if chat == "webhook" and not raw.get("chat_webhook_url"):
        raise ValueError('chat = "webhook" needs chat_webhook_url')
    return chat


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


def _weekdays(raw: dict[str, object], key: str, default: tuple[int, ...]) -> tuple[int, ...]:
    value = raw.get(key, list(default))
    items = cast(list[object], value) if isinstance(value, list) else None
    if items is None or not all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 6 for v in items):
        raise ValueError(f"{key} must be a list of weekday numbers, 0 (Monday) to 6")
    return tuple(int(cast(int, v)) for v in items)


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


def map_table_to_mention_people(table: dict[str, str], chat: str = "slack") -> tuple[tuple[str, str], ...]:
    for name, slack_id in table.items():
        if not slack_id.strip():
            raise ValueError(f"review_mention_people.{name} is empty")
        if chat == "slack" and not re.fullmatch(r"[UW][A-Z0-9]{6,}", slack_id):
            raise ValueError(f"review_mention_people.{name} must be a Slack user id like U01AAAA0001")
    return tuple(table.items())


def map_table_to_guard(table: dict[str, object]) -> GuardRule:
    trigger, pattern, resource = _str(table, "trigger", ""), _str(table, "pattern", ""), _str(table, "resource", "")
    if not (trigger and pattern and resource):
        raise ValueError("each [[guards]] needs trigger, pattern and resource")
    return GuardRule(trigger=trigger, pattern=re.compile(pattern), resource=resource)


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
