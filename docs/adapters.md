# Adapters

Guild's core is the dashboard, the lead board, direct messages between sessions, claims and context compaction.
They need only Claude Code and agterm, and they are always on. Everything that talks to an outside service is an
adapter, picked in `~/.config/bridge/config.toml`:

| Adapter | Config key | Default | Built in |
|---|---|---|---|
| Task tracker | `tracker` | `"tasks"` (a local file) | `tasks`, `exec`, `linear`, `none` |
| Review chat | `chat` | none (Slack when `review_channel_id` is set) | `slack`, `file`, `webhook`, `none` |
| Code host | (GitHub) | on when `gh` is signed in | GitHub |
| Remote hosts | `[[remotes]]` | none | ssh + tmux |

Each one is optional: without it, its column, chip or button is just absent.

## Task tracker

The tracker turns an issue key that sessions mention (`ABC-12`) into a title, a state, a link and the PRs linked to
the issue. The page shows them next to each lead's work, and bridge flags an issue that is still open after every
PR linked to it has merged.

### What bridge does, and what the adapter does

- **Finding refs is bridge's job.** It reads keys of the form `PREFIX-NUMBER` from prompts, branch names, board posts
  and `guild work set`. `issue_prefixes = ["ABC", "OPS"]` limits them to your projects; empty takes any
  `[A-Z][A-Z0-9]+-\d+` that is not an obvious acronym. A tracker URL with `/issue/KEY`, `/issues/KEY` or
  `/browse/KEY` in it counts too. Refs are stored as `issue:ABC-12`.
- **Links are config.** `issue_url = "https://tracker.example/browse/{key}"` makes every key on the page a link;
  without it, the issue's own `url` from the adapter is used.
- **The adapter answers `fetch`:** given keys, return what it knows about each. Bridge calls it from the dashboard
  service, at most every 5 minutes per watched issue (2 minutes after a failure), in batches of 40.

### The issue shape

Every adapter returns this per key, as JSON (the exec adapter) or the matching Python dataclass
(`bridge.trackers.IssueStatus`):

```json
{
  "title": "Speed up the export",
  "state": "In review",
  "state_type": "started",
  "url": "https://tracker.example/browse/ABC-2",
  "assignee": "sam",
  "linked_prs": [
    {"ref": "acme/web#7", "status": "merged", "closes": true, "merged_at": "2026-11-03T08:00:00Z"}
  ],
  "linked_complete": true
}
```

- `title`, `state`: shown as given.
- `state_type`: what bridge reasons with, one of `triage`, `backlog`, `unstarted`, `started`, `completed`,
  `canceled`. Left out or unknown, bridge guesses it from `state` ("Done" → completed, "In progress" → started, …).
- `url`, `assignee`: optional.
- `linked_prs`: optional. `ref` is `owner/repo#N`, `pr:owner/repo#N` or a GitHub PR URL; `status` is the tracker's
  view (`open`, `draft`, `merged`, `closed`); `closes` says the PR is meant to close the issue; `merged_at` is ISO
  8601 or epoch milliseconds.
- `linked_complete`: `true` only when `linked_prs` is the whole list. The "still open after its PRs merged" flag
  needs it, so leave it out when you cannot be sure.

### `tasks`: a local file (the default)

`tasks_file` (default: `tasks.md` next to `config.toml`), read on every poll. A Markdown checklist:

```markdown
- [ ] ABC-1 Write the importer
- [~] ABC-2 Speed up the export https://tracker.example/ABC-2
- [x] ABC-3 Ship the flag
- [-] ABC-4 Old idea
```

`[ ]` todo, `[~]` or `[/]` in progress, `[x]` done, `[-]` canceled; a URL at the end of the line is the link. Any
other line is ignored, so the file can hold notes and headings. A `.json` file takes the issue shape instead, as
`{"issues": {"ABC-1": {...}}}`, `{"ABC-1": {...}}` or a list of issues that each carry `"key"`.

### `exec`: any executable

```toml
tracker = "exec"
tracker_command = ["~/bin/jira-tracker", "--site", "acme"]
```

Bridge runs the command once per batch, writes one JSON object to its stdin and reads one from its stdout:

```json
{"version": 1, "op": "fetch", "keys": ["ABC-1", "ABC-2"]}
```

```json
{"issues": {"ABC-1": {"title": "…", "state": "Done"}}, "errors": {"ABC-2": "no access"}}
```

- A key in neither map shows as "not found or no access".
- A non-zero exit fails the whole batch with the last line of stderr; so does output that is not JSON. Never print
  a token to stderr: it is stored and shown on the page.
- 30 seconds per batch. The command inherits bridge's environment, so keep secrets in a file it reads or in the
  keychain, not in `tracker_command`.

### `linear`

```toml
tracker = "linear"
linear_api_key_file = "~/.secrets/linear-api-key"   # a personal API key, read-only is enough
```

Uses Linear's GraphQL API, and the GitHub attachments of each issue as its linked PRs.

### Writing an adapter with your coding agent

The exec adapter is meant to be written by an agent in one sitting. Give it this file and a prompt like:

> Write `~/bin/jira-tracker`, a Guild exec tracker adapter (docs/adapters.md, "exec"). Read the request JSON from
> stdin; for each key call `GET /rest/api/3/issue/{key}?fields=summary,status,assignee` on
> `https://acme.atlassian.net` with the token in `~/.secrets/jira`; map `summary` to title, `status.name` to state,
> `status.statusCategory.key` (new / indeterminate / done) to state_type (unstarted / started / completed), the
> browse URL to url. Put per-key failures in `errors`. Python stdlib only. Add a `--selftest` that prints the answer
> for one key I give it.

What changes per tracker is only the mapping:

- **Jira:** issue keys are native (`ABC-12`). `statusCategory` gives `state_type`; the development panel
  (`/rest/dev-status/latest/issue/detail`) gives linked PRs.
- **Notion:** a database with a unique-ID property (prefix `TASK`) gives keys `TASK-12`; query it with a filter on
  that property, map a Status property to `state` and its group to `state_type`, and the page URL to `url`.
- **A kanban board** (Trello, GitHub Projects, Plane, a YAML file): give cards a key in their title (`OPS-7 Fix
  login`), find them by that prefix, and map the column name to `state`; `state_type` can be left to bridge's guess,
  or set from a column-to-type table in the adapter.

Test it by hand before pointing bridge at it:

```sh
echo '{"version": 1, "op": "fetch", "keys": ["ABC-1"]}' | ~/bin/jira-tracker | python3 -m json.tool
bridge enrich --force    # prints which tracker ran and every ref that failed
```

## Review chat

Only the owner's PR review queue posts to a chat: pressing Publish on a PR posts `<PR link> - <description>`, plus
` - direct` and the colleagues picked in the form. Nothing else goes outside, and sessions never post there
themselves. The lead board and direct messages between sessions are separate, and always on.

```toml
chat = "webhook"                                      # none | slack | file | webhook
chat_webhook_url = "https://hooks.example/T000/B000"  # an incoming webhook
review_post_dry_run = false                           # true (the default) writes review-posts.log and posts nothing
```

- `none`: Publish records the PR as published and posts nothing.
- `file`: one JSON line per post (`{"at": <ms>, "text": "…"}`) in `chat_file` (default: `chat-posts.log` in the
  state dir). A script of yours can forward it anywhere.
- `webhook`: a POST of `{"text": "…", "content": "…"}`: Slack, Mattermost and Rocket.Chat read `text`, Discord reads
  `content`. A 2xx answer counts as posted.
- `slack`: posts through the Slack connector of claude.ai (`claude -p` with only the send tool), and reads the
  channel (`review_channel_id`) back, with a token in `~/.secrets/slack` or else through the connector: a post with
  a PR's link, by anyone, marks the PR published. `review_mention_people` maps names to Slack member ids.

Outside Slack, a mention is `@` plus whatever `review_mention_people` maps the name to.

To add another chat, `send_to_chat` in `src/bridge/review_post.py` is the one function to extend: it gets the text
and returns posted, failed or a link.

## Code host: GitHub

GitHub is built in, through the `gh` CLI and its sign-in. Without it, everything except the items below still runs.

What is GitHub-specific:

- **PR state on the page** (`enrich.py`, `fetch_prs`): one `gh api graphql` query for title, state, checks, review
  decision, mergeability and head branch of every PR sessions mention.
- **PR refs** (`refs.py`): `github.com/owner/repo/pull/N` URLs, `owner/repo#N`, and bare `#N` in a repo whose
  `origin` is on GitHub.
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

## Team rules: defaults you can change

Some rules are a way of working rather than a mechanism. They ship as defaults, written into the lead role and the
bridge skill, and each lives in one place you can edit:

| Rule | Default | Where |
|---|---|---|
| Large PRs need independent reviews before the owner's queue | Codex and a fresh Claude session, at 100+ lines | `REQUIRED_REVIEWS` in `src/bridge/review_marks.py`, `review_large_loc` in config, the role text in `src/bridge/leads.py` |
| How a large PR's review doc looks | sections, folds, provenance marks | `agent/skills/bridge/review-doc.md` |
| What agents write on GitHub and the tracker starts with a prefix, and names no internal session | `🤖 Claude:`; leads' names and the words worker, lead, Guild, bridge | `CLAUDE_PREFIX` and `INTERNAL_WORDS` in `src/bridge/leads.py`, `AGENT_PREFIX` in `src/bridge/reviews.py` |
| Tab, workspace and worker names are in English | non-ASCII letters are refused | `is_english_name` in `src/bridge/registry.py` |

Bridge tells agents' comments from the owner's on a shared GitHub account by that prefix, so keep one if agents post
under your account.

## Remote hosts

```toml
[[remotes]]
name = "devbox"
ssh = "devbox"            # a host from ~/.ssh/config
aliases = ["devbox-alt"]  # other names that reach the same machine
```

Bridge lists the tmux sessions on each remote over ssh, shows the agents running there next to local ones, and knows
which local session drives which remote one. Without `[[remotes]]` nothing connects anywhere.
