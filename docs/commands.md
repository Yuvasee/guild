# Command reference

Every `bridge` and `guild` command, generated from the CLIs' own `--help`. `bridge <command> --help` prints the same.

- **bridge**: one session's registry entry, messages between sessions, your feed, the team board, claims, the dashboard.
- **guild**: team leads on top of bridge: leads, workers, asks, notes, the PR review queue, dreams.

## bridge

<details>
<summary><b>This session</b>: <code>sessions</code>, <code>whoami</code>, <code>focus</code>, <code>keep</code>, <code>handle</code></summary>

### bridge sessions

list live sessions

```text
usage: bridge sessions [-h] [--json] [--all]

options:
  -h, --help  show this help message and exit
  --json
  --all       include ended sessions
```

### bridge whoami

show this session's registry entry

```text
usage: bridge whoami [-h] [--json]

options:
  -h, --help  show this help message and exit
  --json
```

### bridge focus

set what this session is working on

```text
usage: bridge focus [-h] [--clear] [text]

positional arguments:
  text

options:
  -h, --help  show this help message and exit
  --clear
```

### bridge keep

save notes that bridge gives back to this session after compaction

```text
usage: bridge keep [-h] [--show] [text]

positional arguments:
  text        the notes; read from stdin when omitted

options:
  -h, --help  show this help message and exit
  --show      print the saved notes
```

### bridge handle

set this session's stable handle (survives restarts of the tab)

```text
usage: bridge handle [-h] name

positional arguments:
  name

options:
  -h, --help  show this help message and exit
```

</details>

<details>
<summary><b>Messages between sessions</b>: <code>send</code>, <code>reply</code>, <code>ack</code>, <code>done</code>, <code>decline</code>, <code>inbox</code>, <code>show</code></summary>

### bridge send

message other sessions by handle (comma-separated)

```text
usage: bridge send [-h] [--request] [--respond-within RESPOND_WITHIN] to text

positional arguments:
  to                    handle[,handle…]
  text

options:
  -h, --help            show this help message and exit
  --request             expects a response (ack/reply/done/decline)
  --respond-within RESPOND_WITHIN
```

### bridge reply

reply to a message

```text
usage: bridge reply [-h] [--done] id text

positional arguments:
  id
  text

options:
  -h, --help  show this help message and exit
  --done      also close the request
```

### bridge ack

mark message(s): seen, working on it

```text
usage: bridge ack [-h] ids [ids ...]

positional arguments:
  ids

options:
  -h, --help  show this help message and exit
```

### bridge done

mark message(s): request handled

```text
usage: bridge done [-h] ids [ids ...]

positional arguments:
  ids

options:
  -h, --help  show this help message and exit
```

### bridge decline

mark message(s): will not do it

```text
usage: bridge decline [-h] ids [ids ...]

positional arguments:
  ids

options:
  -h, --help  show this help message and exit
```

### bridge inbox

messages for this session

```text
usage: bridge inbox [-h] [--all]

options:
  -h, --help  show this help message and exit
  --all       include closed ones
```

### bridge show

a message with its thread

```text
usage: bridge show [-h] id

positional arguments:
  id

options:
  -h, --help  show this help message and exit
```

</details>

<details>
<summary><b>Your feed and the team board</b>: <code>post</code>, <code>tell-user</code>, <code>board</code></summary>

### bridge post

a post in the user's feed: blocked, decision, done or insight; `post resolve N` withdraws yours

```text
usage: bridge post [-h] [--body BODY] [--link LINK] [--option OPTION] [--what WHAT] [--why WHY]
                   [--cost COST] [--pick PICK] [--pick-why PICK_WHY] [--urgent] [--ask N] [--pin]
                   {blocked,decision,done,insight,resolve} title

positional arguments:
  {blocked,decision,done,insight,resolve}
  title                 one line; the dashboard shows it first (with resolve: the post's number)

options:
  -h, --help            show this help message and exit
  --body BODY           details; '-' reads stdin
  --link LINK           label=https://… (repeatable)
  --option OPTION       an answer button (repeatable; blocked/decision)
  --what WHAT           blocked/decision: what is being decided, in plain words; a line of the
                        card
  --why WHY             blocked/decision: why it is needed now; a line of the card
  --cost COST           what the --option before it costs; shown under it
  --pick PICK           the answer you recommend, by its number (1 = the first --option)
  --pick-why PICK_WHY   one line: why that answer
  --urgent              blocked/decision only, when prod or users are hurting, money is leaking,
                        or a deadline is today
  --ask N               done/insight: the ask it answers
  --pin                 done/insight: also pin the first --link (an artifact) to the Pinned page
```

### bridge tell-user

one line for the user's feed (older form of post)

```text
usage: bridge tell-user [-h] [--action | --blocker] text

positional arguments:
  text

options:
  -h, --help  show this help message and exit
  --action
  --blocker
```

### bridge board

the team board: post to everyone (no TEXT: read recent posts)

```text
usage: bridge board [-h] [--topic TOPIC] [--type {heads-up,question,report,handoff}]
                    [--thread THREAD] [--link LINK]
                    [text]

positional arguments:
  text

options:
  -h, --help            show this help message and exit
  --topic TOPIC         a short tag, e.g. a repo or an incident
  --type {heads-up,question,report,handoff}
                        what kind of post this is
  --thread THREAD       an issue or PR id; posts with the same id form a thread
  --link LINK           the PR, issue or doc this post points to
```

</details>

<details>
<summary><b>Claims</b>: <code>claim</code>, <code>renew</code>, <code>release</code>, <code>who</code>, <code>claims</code></summary>

### bridge claim

reserve a shared resource before mutating it

```text
usage: bridge claim [-h] [--reason REASON] [--ttl TTL] [--offline] [--force] resource

positional arguments:
  resource         pr:<n>|pr:<owner>/<repo>#<n>|branch:<name>|env:<name>|path:<p>|issue:<KEY>

options:
  -h, --help       show this help message and exit
  --reason REASON
  --ttl TTL
  --offline        skip GitHub PR/branch aliasing (may miss conflicts)
  --force          user only: take it from a live holder
```

### bridge renew

extend a claim you hold

```text
usage: bridge renew [-h] --token TOKEN [--ttl TTL] resource

positional arguments:
  resource

options:
  -h, --help     show this help message and exit
  --token TOKEN
  --ttl TTL
```

### bridge release

give a claim back

```text
usage: bridge release [-h] --token TOKEN resource

positional arguments:
  resource

options:
  -h, --help     show this help message and exit
  --token TOKEN
```

### bridge who

who holds a resource

```text
usage: bridge who [-h] [--offline] resource

positional arguments:
  resource

options:
  -h, --help  show this help message and exit
  --offline
```

### bridge claims

all claims, or yours with tokens

```text
usage: bridge claims [-h] [--mine] [--json]

options:
  -h, --help  show this help message and exit
  --mine
  --json
```

</details>

<details>
<summary><b>Pins and pages</b>: <code>pin</code>, <code>unpin</code>, <code>pins</code>, <code>page</code>, <code>pages</code></summary>

### bridge pin

pin something the owner needs again to the Pinned page (/pinned): a command, a link or a note; never a secret, only where it lives

```text
usage: bridge pin [-h] (--cmd CMD | --link LINK | --note NOTE) [--source SOURCE] title

positional arguments:
  title

options:
  -h, --help       show this help message and exit
  --cmd CMD        a command; the page gives it a Copy button
  --link LINK      an http(s) URL
  --note NOTE      e.g. where a credential lives: "1Password item 'Prod DB admin'"
  --source SOURCE  which lead or ask it came from, e.g. alex or 'ask #64'
```

### bridge unpin

take a pin off the Pinned page; it is kept, hidden

```text
usage: bridge unpin [-h] id

positional arguments:
  id

options:
  -h, --help  show this help message and exit
```

### bridge pins

the pins on the Pinned page, newest first

```text
usage: bridge pins [-h] [--all] [--json]

options:
  -h, --help  show this help message and exit
  --all       unpinned ones too
  --json
```

### bridge page

register a page published as a claude.ai artifact under a stable bridge link, /p/<slug>

```text
usage: bridge page [-h] {add,set-url,share,archive} ...

positional arguments:
  {add,set-url,share,archive}
    add                 register a page by its lasting source folder (never /tmp)
    set-url             the page's claude.ai artifact link, after a (re)publish
    share               mark the page as shared with colleagues; --off to unmark
    archive             retire the page: /p/<slug> stops redirecting and the plan skips it

options:
  -h, --help            show this help message and exit
```

### bridge page add

register a page by its lasting source folder (never /tmp)

```text
usage: bridge page add [-h] --title TITLE --source SOURCE [--url URL] [--shared] slug

positional arguments:
  slug             lowercase letters, digits and dashes, up to 60

options:
  -h, --help       show this help message and exit
  --title TITLE
  --source SOURCE  the folder that holds index.html or a single .html file
  --url URL        its claude.ai artifact link, when already published
  --shared         colleagues have the claude.ai link
```

### bridge page set-url

the page's claude.ai artifact link, after a (re)publish

```text
usage: bridge page set-url [-h] slug url

positional arguments:
  slug
  url

options:
  -h, --help  show this help message and exit
```

### bridge page share

mark the page as shared with colleagues; --off to unmark

```text
usage: bridge page share [-h] [--off] slug

positional arguments:
  slug

options:
  -h, --help  show this help message and exit
  --off
```

### bridge page archive

retire the page: /p/<slug> stops redirecting and the plan skips it

```text
usage: bridge page archive [-h] slug

positional arguments:
  slug

options:
  -h, --help  show this help message and exit
```

### bridge pages

the registered pages; --republish-plan after an Anthropic account switch

```text
usage: bridge pages [-h] [--republish-plan]

options:
  -h, --help        show this help message and exit
  --republish-plan  one line per live page, tab-separated: slug, the HTML file to publish, current
                    url or '-'
```

</details>

<details>
<summary><b>Dashboard</b>: <code>serve</code>, <code>install-service</code>, <code>uninstall-service</code>, <code>restart-service</code></summary>

### bridge serve

run the dashboard web server

```text
usage: bridge serve [-h] [--port PORT]

options:
  -h, --help   show this help message and exit
  --port PORT
```

### bridge install-service

run the dashboard permanently under launchd

```text
usage: bridge install-service [-h] [--port PORT]

options:
  -h, --help   show this help message and exit
  --port PORT
```

### bridge uninstall-service

stop and remove the launchd dashboard service

```text
usage: bridge uninstall-service [-h]

options:
  -h, --help  show this help message and exit
```

### bridge restart-service

restart the dashboard service (after reinstalling bridge)

```text
usage: bridge restart-service [-h]

options:
  -h, --help  show this help message and exit
```

</details>

<details>
<summary><b>Setup and deploys</b>: <code>install-hooks</code>, <code>uninstall-hooks</code>, <code>deploy-check</code></summary>

### bridge install-hooks

add bridge hooks to Claude Code settings

```text
usage: bridge install-hooks [-h] [--settings SETTINGS]

options:
  -h, --help           show this help message and exit
  --settings SETTINGS
```

### bridge uninstall-hooks

remove bridge hooks from Claude Code settings

```text
usage: bridge uninstall-hooks [-h] [--settings SETTINGS]

options:
  -h, --help           show this help message and exit
  --settings SETTINGS
```

### bridge deploy-check

type a line and its Enter into a throwaway tab, as bridge types into leads' tabs, and wake an idle throwaway Claude through the rewake waiter; run before and after a deploy, exit 1 if either does not work

```text
usage: bridge deploy-check [-h]

options:
  -h, --help  show this help message and exit
```

</details>

<details>
<summary><b>Outside services</b>: <code>issue-sync</code>, <code>auto-merge</code></summary>

### bridge issue-sync

move Linear issues forward to match their PRs and live work

```text
usage: bridge issue-sync [-h] [--dry-run]

options:
  -h, --help  show this help message and exit
  --dry-run   print the moves without making them
```

### bridge auto-merge

what the auto-merger would do with each open queue PR: merge it, or what blocks it

```text
usage: bridge auto-merge [-h] --dry-run

options:
  -h, --help  show this help message and exit
  --dry-run   read GitHub, merge and write nothing
```

With `auto_merge = "on"` the dashboard service merges, after each queue poll, at most one of the owner's PRs that the
owner's standing rule lets through: a colleague approved it (the review bot's approval or a waiver does not count), CI
is green or absent, the review bot's latest review is of the head and asks for no changes, GitHub says CLEAN, it is not
a draft, held or stacked, none of the owner's comments is open, a backport's default-branch PRs and a PR its body
says it merges after are merged (a PR the body only names is no wait), every PR a body says it goes with ("goes with
#12", "merge together with #12") passes the same rule, and its body has no `PERSIST_SANDBOX`. It checks the PR live
first, merges with `gh pr merge --squash --match-head-commit`, posts one feed line and tells the PR's lead
it is merged, not deployed. Each head is tried once; a failure goes once to `auto_merge_failures_to`. `dry-run` only
logs what it would merge.

</details>

## guild

<details>
<summary><b>Team leads</b>: <code>leads</code>, <code>lead</code></summary>

### guild leads

list team leads

```text
usage: guild leads [-h] [--json] [--all]

options:
  -h, --help  show this help message and exit
  --json
  --all       include retired leads
```

### guild lead

enroll, sync or retire a team lead

```text
usage: guild lead [-h] {add,label,sync,retire,domains,park,wake,model} ...

positional arguments:
  {add,label,sync,retire,domains,park,wake,model}
    add                 make a tab a team lead: name, tint, a place in the 👁 GUILD workspace
    label               change a lead's specialization label and rename its tab
    sync                apply a lead's tab name, tint and place in 👁 GUILD again
    retire              undo enrollment; the tab goes back, the name is never reused
    domains             the projects a lead takes new work from: priority project names or Linear
                        project refs
    park                no 'has room' notices for a lead left idle on purpose, until a date
    wake                type a first prompt into a lead's idle tab, so its role arrives
    model               type /model <model> into lead tabs once each is idle with an empty box;
                        locked tabs are skipped

options:
  -h, --help            show this help message and exit
```

### guild lead add

make a tab a team lead: name, tint, a place in the 👁 GUILD workspace

```text
usage: guild lead add [-h] --tab TAB --scope SCOPE [--ref REF] --label LABEL [--alias ALIAS]
                      [--tint TINT]
                      name

positional arguments:
  name           one capitalised word, e.g. Ada

options:
  -h, --help     show this help message and exit
  --tab TAB      handle of the tab that becomes the lead
  --scope SCOPE
  --ref REF      Linear project or issue; repeat
  --label LABEL  the lead's specialization; the tab is named '<Name> · <label>'
  --alias ALIAS  spelling for dictation, e.g. Sami
  --tint TINT
```

### guild lead label

change a lead's specialization label and rename its tab

```text
usage: guild lead label [-h] name label

positional arguments:
  name
  label

options:
  -h, --help  show this help message and exit
```

### guild lead sync

apply a lead's tab name, tint and place in 👁 GUILD again

```text
usage: guild lead sync [-h] name

positional arguments:
  name

options:
  -h, --help  show this help message and exit
```

### guild lead retire

undo enrollment; the tab goes back, the name is never reused

```text
usage: guild lead retire [-h] [--force] name

positional arguments:
  name

options:
  -h, --help  show this help message and exit
  --force     retire even if the tab could not be restored
```

### guild lead domains

the projects a lead takes new work from: priority project names or Linear project refs

```text
usage: guild lead domains [-h] [--clear] name [domains ...]

positional arguments:
  name
  domains     none shows them; given ones replace them (the assistant or the lead)

options:
  -h, --help  show this help message and exit
  --clear     remove every domain
```

### guild lead park

no 'has room' notices for a lead left idle on purpose, until a date

```text
usage: guild lead park [-h] (--until YYYY-MM-DD[THH:MM] | --clear) name

positional arguments:
  name

options:
  -h, --help            show this help message and exit
  --until YYYY-MM-DD[THH:MM]
                        local time
  --clear               notices again
```

### guild lead wake

type a first prompt into a lead's idle tab, so its role arrives

```text
usage: guild lead wake [-h] name

positional arguments:
  name

options:
  -h, --help  show this help message and exit
```

### guild lead model

type /model <model> into lead tabs once each is idle with an empty box; locked tabs are skipped

```text
usage: guild lead model [-h] [--all] model [names ...]

positional arguments:
  model       a model id or alias, e.g. claude-opus-5-5
  names       lead names or the assistant's handle

options:
  -h, --help  show this help message and exit
  --all       every active lead and the assistant
```

</details>

<details>
<summary><b>Workers</b>: <code>worker</code></summary>

### guild worker

start a task in its own tab nested under yours, and check that it started; `guild worker close <tab>` closes one, `guild worker closed` lists them, `guild worker reopen <id>` resumes one

```text
usage: guild worker [-h] --prompt PROMPT [--cwd CWD] [--local] name

positional arguments:
  name             the task, in English; the tab's name

options:
  -h, --help       show this help message and exit
  --prompt PROMPT  the brief; - reads stdin
  --cwd CWD        default: this directory
  --local          run Claude on this Mac even when a worker host is configured
```

### guild worker close

close your own nested tab and keep what resumes it

```text
usage: guild worker close [-h] [--force] [--to LEAD] tab

positional arguments:
  tab         the tab's name or agterm id

options:
  -h, --help  show this help message and exit
  --force     close even if it is mid-turn or has a live background run
  --to LEAD   hand it to that lead (yours or the assistant's call): it becomes the worker's lead, is told, and brings
              it back with its whole conversation by guild worker reopen from its own tab
```

### guild worker closed

closed workers, newest first, with the command that reopens each

```text
usage: guild worker closed [-h] [--limit LIMIT]

options:
  -h, --help     show this help message and exit
  --limit LIMIT
```

### guild worker reopen

resume a closed worker in a tab nested under yours (its lead or the assistant)

```text
usage: guild worker reopen [-h] worker

positional arguments:
  worker      its id from guild worker closed, or its tab name

options:
  -h, --help  show this help message and exit
```

### guild worker attach

a host worker whose tab was closed: open it again under yours

```text
usage: guild worker attach [-h] [worker]

positional arguments:
  worker      its g-... name; none lists host workers without a tab

options:
  -h, --help  show this help message and exit
```

</details>

<details>
<summary><b>What team leads show and keep</b>: <code>work</code>, <code>notes</code>, <code>handover</code>, <code>ack</code>, <code>day</code>, <code>capacity</code>, <code>stats week</code></summary>

### guild work

the main chunks of work a lead declares; the page shows these

```text
usage: guild work [-h] {set,done,list} ...

positional arguments:
  {set,done,list}
    set            add a chunk, or update it by name; given refs replace its refs
    done           a chunk ended; it leaves the page
    list           the open chunks

options:
  -h, --help       show this help message and exit
```

### guild work set

add a chunk, or update it by name; given refs replace its refs

```text
usage: guild work set [-h] [--about ABOUT] [--lead LEAD] name [refs ...]

positional arguments:
  name           a few plain words, e.g. 'Nightly export worker'
  refs           PROJ-34, Acme-Org/monorepo#45, #45 (your repo), or a URL

options:
  -h, --help     show this help message and exit
  --about ABOUT  what the chunk is and why, one line; the page shows it beside the name
  --lead LEAD    default: your own
```

### guild work done

a chunk ended; it leaves the page

```text
usage: guild work done [-h] [--lead LEAD] name

positional arguments:
  name

options:
  -h, --help   show this help message and exit
  --lead LEAD  default: your own
```

### guild work list

the open chunks

```text
usage: guild work list [-h] [--lead LEAD]

options:
  -h, --help   show this help message and exit
  --lead LEAD  default: your own
```

### guild notes

a lead's or the assistant's notes that outlive conversations

```text
usage: guild notes [-h] [--write] [--handovers] [lead]

positional arguments:
  lead         default: your own

options:
  -h, --help   show this help message and exit
  --write      replace your notes from stdin
  --handovers  show handovers from other tabs
```

### guild assistant sync

name Right Hand's tab '<Name> · Right Hand', tint it, move it to 👁 GUILD and pin its handle

Right Hand is the tab whose handle is `assistant_handle`, or, failing that, a running tab named after it or after
`<Name> · Right Hand`; run this once its Claude is listed in `bridge sessions`. It pins that handle first (taking it
from a tab with no running Claude that still held it), so the new tab name does not change it, then names the tab, tints
it yellow on icon and name, moves it to the `👁 GUILD` workspace and sets its title-bar context, as `guild lead sync` does
for a lead. Safe to run again. Exit 1 if agterm refused a step; exit 2 if no such tab runs, the tab is locked, or
another running tab holds the handle.

```text
usage: guild assistant sync [-h]

options:
  -h, --help  show this help message and exit
```

### guild handover

hand your work to a lead, from stdin

```text
usage: guild handover [-h] lead

positional arguments:
  lead

options:
  -h, --help  show this help message and exit
```

### guild ack

a lead's one line on a board post; shown faint, delivered to nobody

```text
usage: guild ack [-h] id text

positional arguments:
  id
  text

options:
  -h, --help  show this help message and exit
```

### guild day

the owner is on again; how loaded each lead is and its next work

```text
usage: guild day [-h]

options:
  -h, --help  show this help message and exit
```

### guild capacity

a dry run of the capacity signal: who has room for planned work now, why, what to suggest

```text
usage: guild capacity [-h]

options:
  -h, --help  show this help message and exit
```

### guild stats week

PRs merged, their commits, and how often the owner did not understand

```text
usage: guild stats week [-h] [--days DAYS]

options:
  -h, --help   show this help message and exit
  --days DAYS  how many days back (default 7)
```

Prints plain lines for a weekly report: the PRs `review_author` merged in `review_org` (count, median commits, how
many have 20 or more), then a rough count by phrase match (config `confusion_phrases`) of the owner's messages that say
they did not understand, per day, from the dashboard and the prompts typed in the assistant's tab.

</details>

<details>
<summary><b>Comments on PRs and issues</b>: <code>comment</code></summary>

### guild comment

your call on a colleague's comment on our PR; the page shows it

```text
usage: guild comment [-h] {small,draft,list} ...

positional arguments:
  {small,draft,list}
    small             a small one (typo, rename, factual question) you handled
    draft             a substantive one: your draft reply and plan go to the owner, who chooses;
                      bridge posts after
    list              colleague comments still waiting on you or on the owner

options:
  -h, --help          show this help message and exit
```

### guild comment small

a small one (typo, rename, factual question) you handled

```text
usage: guild comment small [-h] comment did

positional arguments:
  comment     the comment's link or id
  did         what you did

options:
  -h, --help  show this help message and exit
```

### guild comment draft

a substantive one: your draft reply and plan go to the owner, who chooses; bridge posts after

```text
usage: guild comment draft [-h] --reply-file REPLY_FILE --plan PLAN comment

positional arguments:
  comment               the comment's link or id

options:
  -h, --help            show this help message and exit
  --reply-file REPLY_FILE
                        the draft reply, as it would go on GitHub
  --plan PLAN           what you would do about it
```

### guild comment list

colleague comments still waiting on you or on the owner

```text
usage: guild comment list [-h] [--all]

options:
  -h, --help  show this help message and exit
  --all       every lead's, not only yours
```

</details>

<details>
<summary><b>Your asks</b>: <code>ask</code></summary>

### guild ask

the owner's asks, each taken to verified with evidence of what they would see

```text
usage: guild ask [-h] {add,list,show,start,note,wait,done,verify,close,drop,reopen} ...

positional arguments:
  {add,list,show,start,note,wait,done,verify,close,drop,reopen}
    add                 record an ask in the owner's own words
    list                asks not closed, oldest first
    show                one ask with its history
    start               open → in_progress
    note                news on an ask not done yet or not verified yet; it counts as an update
    wait                it cannot move: its reminders pause until its next start, note, done,
                        verify or reopen, or until --until passes
    done                claim it done; another session verifies
    verify              done_claimed → verified, with evidence of what the owner would see
    close               verified → closed (the assistant only, relaying the owner)
    drop                close an ask not yet claimed as obsolete, with why (its owner session or
                        the assistant)
    reopen              back to open: the result is not what the owner asked for

options:
  -h, --help            show this help message and exit
```

### guild ask add

record an ask in the owner's own words

```text
usage: guild ask add [-h] [--words WORDS] [--from N] [--ref REF] [--question] owner

positional arguments:
  owner          the lead that answers for it, or the assistant's handle

options:
  -h, --help     show this help message and exit
  --words WORDS  the owner's words verbatim; default: stdin
  --from N       quote message N's body as the words
  --ref REF      a PR, issue or page URL; repeat
  --question     the owner asked it as a question: once verified, it goes to Answers
```

### guild ask list

asks not closed, oldest first

```text
usage: guild ask list [-h] [--owner OWNER] [--all] [--json]

options:
  -h, --help     show this help message and exit
  --owner OWNER
  --all          closed ones too
  --json
```

### guild ask show

one ask with its history

```text
usage: guild ask show [-h] id

positional arguments:
  id

options:
  -h, --help  show this help message and exit
```

### guild ask start

open → in_progress

```text
usage: guild ask start [-h] id

positional arguments:
  id

options:
  -h, --help  show this help message and exit
```

### guild ask note

news on an ask not done yet or not verified yet; it counts as an update

```text
usage: guild ask note [-h] id text

positional arguments:
  id
  text

options:
  -h, --help  show this help message and exit
```

### guild ask wait

it cannot move: its reminders pause until its next start, note, done, verify or reopen, or until --until passes

```text
usage: guild ask wait [-h] (--on owner|approval|release|verifier | --until YYYY-MM-DD[THH:MM])
                      id why

positional arguments:
  id
  why                   who exactly, and for what

options:
  -h, --help            show this help message and exit
  --on owner|approval|release|verifier
                        who or what it waits on; --on owner: waiting on the owner
  --until YYYY-MM-DD[THH:MM]
                        its owner pauses it until then, local time (a date alone: its 00:00);
                        reminders resume after
```

### guild ask done

claim it done; another session verifies

```text
usage: guild ask done [-h] --claim CLAIM id

positional arguments:
  id

options:
  -h, --help     show this help message and exit
  --claim CLAIM  what was done and where it lives
```

### guild ask verify

done_claimed → verified, with evidence of what the owner would see

```text
usage: guild ask verify [-h] --evidence EVIDENCE --how HOW id

positional arguments:
  id

options:
  -h, --help           show this help message and exit
  --evidence EVIDENCE  what the owner would see, and where
  --how HOW            a URL, a screenshot path, or a command and its output
```

### guild ask summary

one Answers post listing the asks you verified that the owner was not told of yet; one per batch

```text
usage: guild ask summary [-h]

options:
  -h, --help  show this help message and exit
```

### guild ask close

verified → closed (the assistant only, relaying the owner)

```text
usage: guild ask close [-h] [--dropped WHY] id

positional arguments:
  id

options:
  -h, --help     show this help message and exit
  --dropped WHY  close an unfinished ask the owner let go, and why
```

### guild ask drop

close an ask not yet claimed as obsolete, with why (its owner session or the assistant)

```text
usage: guild ask drop [-h] id why

positional arguments:
  id
  why         why it is no longer wanted; kept in its history

options:
  -h, --help  show this help message and exit
```

### guild ask reopen

back to open: the result is not what the owner asked for

```text
usage: guild ask reopen [-h] --why WHY id

positional arguments:
  id

options:
  -h, --help  show this help message and exit
  --why WHY
```

</details>

<details>
<summary><b>Right Hand</b>: <code>answer</code>, <code>priority</code>, <code>urgent</code></summary>

### guild answer

relay the user's answer to a post in the feed (the assistant only)

```text
usage: guild answer [-h] id text

positional arguments:
  id
  text

options:
  -h, --help  show this help message and exit
```

### guild priority

the ordered projects behind the page's PR numbers

```text
usage: guild priority [-h] {set,show} ...

positional arguments:
  {set,show}
    set       replace the list, first project first (the assistant only)
    show      the list as the page ranks by it

options:
  -h, --help  show this help message and exit
```

### guild priority set

replace the list, first project first (the assistant only)

```text
usage: guild priority set [-h] name=PROJ-1,PROJ-2 [name=PROJ-1,PROJ-2 ...]

positional arguments:
  name=PROJ-1,PROJ-2

options:
  -h, --help          show this help message and exit
```

### guild priority show

the list as the page ranks by it

```text
usage: guild priority show [-h]

options:
  -h, --help  show this help message and exit
```

### guild urgent

questions marked urgent, newest first, to audit against the criteria

```text
usage: guild urgent [-h] [--days DAYS]

options:
  -h, --help   show this help message and exit
  --days DAYS
```

</details>

<details>
<summary><b>PR review queue (GitHub)</b>: <code>review</code></summary>

### guild review

the PR review queue on the page

```text
usage: guild review [-h] {list,sentence,doc,published,ready,hold,unhold,waive,unwaive,repost} ...

positional arguments:
  {list,sentence,doc,published,ready,hold,unhold,waive,unwaive,repost}
    list                what the queue shows
    sentence            the one sentence of project context for a PR's Slack post
    doc                 register a large PR's HTML document; the page serves this copy
    published           record a Slack post made by hand, so it is never posted again
    ready               retired: mark the draft ready on GitHub with gh pr ready instead
    hold                keep a PR for the owner's own review, never ready to merge (its lead or
                        the assistant)
    unhold              lift a hold before the review ends it (its lead or the assistant)
    waive               merge without a GitHub approval because the owner said so in an answered
                        decision (its lead or the assistant)
    unwaive             take the waiver back: the PR needs a GitHub approval again
    repost              send again a post whose sender stopped (look in the channel first) or that
                        a dry run only logged

options:
  -h, --help            show this help message and exit
```

### guild review list

what the queue shows

```text
usage: guild review list [-h]

options:
  -h, --help  show this help message and exit
```

### guild review sentence

the one sentence of project context for a PR's Slack post

```text
usage: guild review sentence [-h] pr text

positional arguments:
  pr          URL, repo#n or a number
  text

options:
  -h, --help  show this help message and exit
```

### guild review doc

register a large PR's HTML document; the page serves this copy

```text
usage: guild review doc [-h] pr file

positional arguments:
  pr
  file

options:
  -h, --help  show this help message and exit
```

### guild review published

record a Slack post made by hand, so it is never posted again

```text
usage: guild review published [-h] pr link

positional arguments:
  pr
  link

options:
  -h, --help  show this help message and exit
```

### guild review hold

keep a PR for the owner's own review, never ready to merge (its lead or the assistant)

```text
usage: guild review hold [-h] pr why

positional arguments:
  pr
  why

options:
  -h, --help  show this help message and exit
```

### guild review unhold

lift a hold before the review ends it (its lead or the assistant)

```text
usage: guild review unhold [-h] pr

positional arguments:
  pr

options:
  -h, --help  show this help message and exit
```

### guild review waive

merge without a GitHub approval because the owner said so in an answered decision (its lead or the assistant)

```text
usage: guild review waive [-h] --decision DECISION pr

positional arguments:
  pr

options:
  -h, --help           show this help message and exit
  --decision DECISION  the answered decision or blocked post on the page
```

### guild review unwaive

take the waiver back: the PR needs a GitHub approval again

```text
usage: guild review unwaive [-h] pr

positional arguments:
  pr

options:
  -h, --help  show this help message and exit
```

### guild review repost

send again a post whose sender stopped (look in the channel first) or that a dry run only logged

```text
usage: guild review repost [-h] pr

positional arguments:
  pr

options:
  -h, --help  show this help message and exit
```

</details>

<details>
<summary><b>Dreams</b>: <code>dream</code></summary>

### guild dream

the night's dreams, in parallel and detached; default: config dream_leads

```text
usage: guild dream [-h] [--wait | --wait-all] [leads ...]

positional arguments:
  leads       lead names, the assistant's handle, or all; none means config dream_leads

options:
  -h, --help  show this help message and exit
  --wait      dream here, one after another, and print each outcome
  --wait-all  dream in parallel and return when all are done or cut at the limit
```

</details>
