# Adapters

Guild's core is the dashboard, the lead board, direct messages between sessions, claims and context compaction.
They need only Claude Code and agterm, and they are always on. Everything that talks to an outside service is an
adapter, picked in `~/.config/bridge/config.toml`:

| Adapter | Config key | Default | Built in |
|---|---|---|---|
| Task tracker | `linear_api_key_file` | none | Linear |
| Review chat | `review_channel_id` | none; with `review_post_dry_run` (the default) posts go to a log | Slack, through the claude.ai connector |
| Code host | (GitHub) | on when `gh` is signed in | GitHub |
| Remote hosts | `[[remotes]]` | none | ssh + tmux |

Each one is optional: without it, its column, chip or button is just absent.

## Task tracker

The tracker turns an issue key that sessions mention (`ABC-12`) into a title, a state, a link and the PRs linked to
the issue. The page shows them next to each lead's work, and bridge flags an issue that is still open after every
PR linked to it has merged.

- **Finding refs is bridge's job.** It reads keys of the form `PREFIX-NUMBER` from prompts, branch names, board posts
  and `guild work set`. `issue_prefixes = ["ABC", "OPS"]` limits them to your projects; empty takes any
  `[A-Z][A-Z0-9]+-\d+` that is not an obvious acronym. A Linear URL with `/issue/KEY` in it counts too. Refs are
  stored as `issue:ABC-12`.
- **Links are config.** `issue_url = "https://linear.app/acme/issue/{key}"` makes every key on the page a link.
- **Linear is the one tracker built in:**

  ```toml
  linear_api_key_file = "~/.secrets/linear-api-key"   # a personal API key; read-only is enough unless issue sync runs
  ```

  Bridge reads each watched issue's title, state, assignee and GitHub attachments (its linked PRs) through Linear's
  GraphQL API, from the dashboard service, in batches. Without the key file, issue columns stay empty.
- **Issue sync** (`bridge issue-sync`, also run by the dashboard service) moves a Linear issue forward when its PRs
  and the work on it say it has moved. It runs only with `review_org`, `review_author` and `issue_prefixes` set, and
  needs a key that may write. `grooming_states` names the states it never moves an issue out of; the lead is told once
  instead. `ready_states` names the states the grooming bot leaves an issue in once its grooming is done: such an
  issue of a project one lead owns (its domains) goes to In Progress with no work on it yet when its last state change
  is the `grooming_bot`'s move into it out of a grooming state, made in the last `ready_handoff_days` (2 by default),
  unless it is assigned to someone else or was set back by hand after an earlier move, and that lead is told once. An
  issue that waits in a ready state from older grooming, or that a person put there, stays. With `grooming_bot` and
  `grooming_questions_re` set, it also reads the newest comments of each issue it holds in grooming: when the newest
  word is that bot's comment asking questions, the issue's lead (no lead: the assistant) is told once, and once more
  if the comment is still unanswered two hours later.

### Another tracker

Porting is a job for your coding agent with this in hand: `fetch_issues` in `src/bridge/enrich.py` takes refs and
returns one `IssueStatus` per ref (title, state, `state_type` as one of `triage`, `backlog`, `unstarted`, `started`,
`completed`, `canceled`, assignee, url, linked PRs and whether that list is complete) or an error string. Replace it
with your tracker's API and keep the shape; the page, the stale-issue flag and the review queue read only that shape.
Issue sync writes back to Linear and stays off for another tracker until it is ported too.

## Review chat

Only the owner's PR review queue posts to a chat: pressing Publish on a PR posts `<PR link> - <description>`, plus
` - direct` and the colleagues picked in the form. Nothing else goes outside, and sessions never post there
themselves. The lead board and direct messages between sessions are separate, and always on.

```toml
review_channel_id = "C0123456789"         # the Slack channel of review posts
review_channel_name = "#reviews"          # how texts name it
review_post_dry_run = false               # true (the default) writes review-posts.log in the state dir and posts nothing
```

Posts go through the Slack connector of claude.ai (`claude -p` with only the send tool). Bridge also reads the
channel back, with a token in `~/.secrets/slack` or else through the connector: a post with a PR's link, by anyone,
marks the PR published. `review_mention_people` (in the people file, below) maps names to Slack member ids.

To post somewhere else, `send_to_slack` in `src/bridge/review_post.py` is the function to replace: it gets the text
and returns posted, failed or a link.

## Code host: GitHub

GitHub is built in, through the `gh` CLI and its sign-in. Without it, everything except the items below still runs.

What is GitHub-specific:

- **PR state on the page** (`enrich.py`, `fetch_prs`): one `gh api graphql` query for title, state, checks, review
  decision, mergeability and head branch of every PR sessions mention.
- **PR refs** (`refs.py`): `github.com/owner/repo/pull/N` URLs, `owner/repo#N`, and `PR #N` in a repo whose
  `origin` is on GitHub; a lone `#N` is not a PR, as bridge messages and asks are numbered the same way.
- **Claims on PRs** (`claims.py`): `pr:owner/repo#N` and its head branch are one resource, resolved with
  `gh pr view` / `gh pr list`.
- **The push and merge guard** (`guard.py`): reads `git push`, `gh pr merge`, `gh pr close` and `gh api …/merge`
  commands, and asks GitHub whether a PR may merge.
- **The owner's review queue** (`review_github.py`, `reviews.py`, `review_worktree.py`), off unless `review_org`
  is set: a GraphQL search of the org's open PRs, REST reads of comments and review threads, and review worktrees
  cloned with `gh`'s token.

Adapting to GitLab, Gitea, Bitbucket or plain git is a job for your agent with this list in hand:

1. **PR refs:** add the host's merge-request URL and `group/project!N` forms to `refs.py`, keeping the `pr:` prefix
   so the rest of bridge needs no change.
2. **PR state:** replace `fetch_prs` with the host's API (GitLab: `glab api graphql` or REST
   `/projects/:id/merge_requests/:iid`; Gitea: `/repos/{owner}/{repo}/pulls/{index}`), filling the same `PrStatus`
   fields; fields the host lacks stay `None`.
3. **Claims and the guard:** map `glab mr merge` / `tea pr merge` next to `gh pr merge` in `guard.py`, and resolve
   head branches in `claims.py` with the same CLI.
4. **The review queue** is the largest piece; leave `review_org` empty until it is ported.

With **plain git** and no host, leave `gh` signed out: branch claims (`branch:`), the push guard for claimed
branches, the board, the feed and compaction all work; PR columns stay empty.

## Status watchers

```toml
[[watchers]]
path = "~/.local/state/nightly-backup/status.json"
name = "Backup"
```

A job of yours (cron, launchd, a CI poller) rewrites a small JSON file after each run, and the page shows it. Bridge
only reads the file:

```json
{"status": "ok", "last_run_utc": "2026-01-01T03:00:00Z", "name": "Backup", "summary": "12 GB, 4 min",
 "detail": "optional longer text", "stale_after_min": 1500}
```

`status` is `ok` or `failed`; a run older than `stale_after_min` shows as stale, and a missing or unreadable file
says so.

Set `watcher_alert_to = "<handle>"` and a watcher that leaves ok (failed, stale, missing or unreadable) is told to that
session once, by bridge; still not ok after `watcher_escalate_after_min` (default 60), the assistant hears it once too,
and everyone told hears when it is back to ok. Empty, the default, tells nobody.

A failure whose end is known ahead, such as a usage limit with a reset time, may carry `"known_until":
"2026-01-01T11:00:00Z"` (UTC; a bad value is ignored). Until then the alert goes out once, ending "Expected back by
<local time>", the assistant is not told, and failing again with the same summary after a short ok (which still says
back to ok once) is the same outage, not a new alert. Still failing after it, the usual escalation follows.

A failed status with `"needs_owner": true` is also a Needs-you card on the page, with its summary, for as long as
the file reads so: for what only the owner's hands can fix, such as a login.

`worker_host_watcher = "<watcher name>"` names the watcher of `worker_host`'s Claude: while it reads failed,
`guild worker` starts the worker on this Mac and says why. Stale or missing, the worker goes to the host as usual.

A worker on this Mac starts only in a folder Claude Code already trusts, since Claude would otherwise wait at its
"Is this a project you trust?" dialog. Set `worker_trust_roots = ["~/projects"]` and a folder under one of them that is
not trusted yet is marked trusted at launch, as answering the dialog would; anywhere else `guild worker` refuses it
and says how to trust it. Empty, the default, marks nothing.

When this Mac's load average is over `heavy_job_load_per_core` (default 3) times its core count, an agent's shell
command that runs a whole-project check or build here (pytest with no test file, node id or -k; pyright, mypy or tsc on
the project; npm, pnpm or yarn test or build; uv sync; docker build; make with no target or test/build; cargo or go
test) gets a note before it runs: the load, and the one line to run it on `worker_host` instead, detached. It is never
refused. Each note is a line in slow.log, `load note: <handle> <job> at load <n>`. 0 turns the note off.

## Team rules: defaults you can change

Some rules are a way of working rather than a mechanism. They ship as defaults, written into the lead role and the
bridge skill:

| Rule | Default | Where |
|---|---|---|
| Large PRs name their reviews before the owner's queue | a review doc at 100+ lines with a Reviews section; no review is required by name | `required_reviews` and `review_bot_name` in config, the `brief-reviews` and `lead-reviews` texts (below), `REVIEW_LARGE_LOC` in `src/bridge/review_worktree.py` |
| How a large PR's review doc looks | sections, folds, provenance marks | `agent/skills/bridge/review-doc.md` |
| What agents write on GitHub, and their replies on the tracker, start with a prefix (a tracker description or an issue's first comment does not); none names an internal session | `🤖 Claude:`; leads' names and the words worker, lead, Guild, bridge | `CLAUDE_PREFIX` in `src/bridge/agent_prefix.py`, `INTERNAL_WORDS` in `src/bridge/leads.py` |
| Tab, workspace and worker names are in English | non-ASCII letters are refused | `is_english_name` in `src/bridge/registry.py` |

Bridge tells agents' comments from the owner's on a shared GitHub account by that prefix, so keep one if agents post
under your account.

## Your install's own values

Everything that differs between teams comes from your config, a folder of house texts and a people file, so the code
stays the same for everyone. Every key is optional.

```toml
work_timezone = "Europe/Lisbon"           # working hours (09-19) for daytime-only nudges; default: this Mac's zone
work_days = [0, 1, 2, 3, 4]               # Monday is 0 (the default)
review_bot_name = "ReviewBot"             # your review bot as people name it; its approval counts
required_reviews = ["Codex"]              # reviews a large PR's doc must name before it reaches you
review_stall_status = "reviewbot/stall"   # the commit status your bot sets red when it stalls; empty watches none
review_owner_label = "owner-reviewed"     # label put on a PR once you press Publish; agents may not set it; empty is off
review_ignored_posts = ["1700000000.000100"]  # Slack ts of review-channel posts that publish nothing (a digest)
agent_prefix_from_ms = 1700000000000      # when agents began prefixing GitHub posts; older unprefixed ones stay unclear
host_worker_disabled_plugins = ["example@plugins"]  # plugins host workers run without, besides the defaults
grooming_states = ["Grooming"]            # tracker states issue sync never moves an issue out of
ready_states = ["Todo"]                   # where grooming leaves a finished issue; a lead's project's go to In Progress
ready_handoff_days = 2                    # how recent the grooming bot's hand-off must be to start the issue
grooming_bot = "groomer"                  # the tracker user that asks grooming questions in comments
grooming_questions_re = '^(\d+) open questions?'  # start of its asking comment; group 1 counts them
review_generated_files = ["api-client.ts"]  # left out of a PR's line count, besides lockfiles
review_python_paths = ["lib/python"]      # extra Python roots for review worktrees' editor and services
pages_dir = "~/pages"                     # where published pages' sources live (the default)
disk_folders = ["~/code/worktrees"]       # measured hourly; Right Hand hears when the disk runs low
overlay_dir = "~/.config/bridge/house"    # your house texts, below
people_dir = "~/.config/bridge/people"    # your people file, below
ask_autoclose_decision = "Q#12"           # your decision that verified asks close by themselves, cited when they do
asks_panel_max = 10                       # asks the page lists as needing attention (the default); the rest are counted
auto_merge = "off"                        # off (the default), dry-run or on: merge queue PRs under the standing rule
auto_merge_decision = "Q#41"              # your decision that set that rule, cited in each merge's feed line
auto_merge_failures_to = "lead-x"         # told of a failed auto-merge, a failing review poll, a stalled review or serve loop; default: the assistant
```

**House texts** (`overlay_dir`): one `<slot>.md` per slot replaces or adds a passage of the roles and briefs. A
missing file keeps the generic text, or drops the line. `{owner}` and `{assistant}` are filled in where noted.

| Slot | Where it goes |
|---|---|
| `grooming.md` | the lead role and worker briefs, after the tracker rule ({owner}, {assistant}) |
| `lead-heavy-work.md` | the lead role's line on what it coordinates ({owner}, {assistant}) |
| `lead-reviews.md` | the lead role, before the merge rule ({owner}, {assistant}) |
| `assistant-rules.md` | Right Hand's role, in place of the generic deploy line ({owner}) |
| `brief-reviews.md` | worker briefs, in place of the generic Reviews rule ({owner}) |
| `doc-reviews.md` | bridge's request for a large PR's review doc |
| `issue-sync-grooming.md` | issue sync's note to a lead about issues held in grooming ({owner}) |
| `issue-sync-grooming-questions.md` | issue sync's note that the grooming bot waits for answers ({bot}) |
| `merge-rule.md` | how an approval names the merge rule (default "the merge rule") |
| `ready-to-merge.md` | how the "may merge" message says ready (default "ready to merge") |
| `host-worker-codex.md` | a host worker's brief, a note on running Codex there ({lead}); none by default |

House rules for the bridge skill go in `agent/skills/bridge/house.md` next to `SKILL.md`, which reads it when
present; it is ignored by git, so a link to a file of yours keeps it out of any commit.

A host worker reads them on its host: each start and reopen of one copies the top-level text files of
`~/.claude/skills/bridge` and of `overlay_dir` (when it is under your home) to the same paths under the host's home,
in the ssh call that starts it. Backups (`*.bak*`, `*.orig`, `*.proposed`) and hidden files stay behind; a file is
written only when its content differs, overlay files as 0600; a file only the host has is kept; one under a symlink
on the host is left alone and named in the start's output. Over 1 MB in all, nothing is copied and the start says so.

**The people file** (`people_dir`): `people.toml` holds the keys about people, and once `people_dir` is set,
`config.toml` may not hold them, so a config you share or commit names no one:

```toml
owner_name = "Mira"                       # how agents name you
user_alias = "mira"                       # the name you post under on the board
review_author = "mira-gh"                 # your GitHub login: the review queue's PRs and your comments

[review_mention_people]                    # colleagues a review post may mention, by Slack member id
Sam = "U0123456789"
```

## Remote hosts

```toml
[[remotes]]
name = "devbox"
ssh = "devbox"            # a host from ~/.ssh/config
aliases = ["devbox-alt"]  # other names that reach the same machine
share_tabs = true         # host worker tabs share one connection; needs sshd MaxSessions above the tabs open there
```

Bridge lists the tmux sessions on each remote over ssh, shows the agents running there next to local ones, and knows
which local session drives which remote one. Without `[[remotes]]` nothing connects anywhere.

Bridge's short ssh calls to a host share one connection: its own master, under `~/.local/state/bridge/ssh`, whatever
your `~/.ssh/config` sets for multiplexing, closed 5 minutes after the last call. Each host worker tab keeps a
connection of its own, since sshd refuses sessions past `MaxSessions` (10 by default) on one connection, unless
`share_tabs` is set for that host: then tabs use the same connection, and when it drops every tab there reattaches.
