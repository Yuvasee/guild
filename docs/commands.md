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
<summary><b>Messages between sessions</b>: <code>send</code>, <code>reply</code>, <code>ack</code>, <code>done</code>, <code>decline</code>, <code>cancel</code>, <code>inbox</code>, <code>show</code></summary>

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

### bridge cancel

withdraw a message you sent

```text
usage: bridge cancel [-h] id

positional arguments:
  id

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
usage: bridge post [-h] [--body BODY] [--link LINK] [--option OPTION] [--urgent]
                   {blocked,decision,done,insight,resolve} title

positional arguments:
  {blocked,decision,done,insight,resolve}
  title                 one line; the dashboard shows it first (with resolve: the post's number)

options:
  -h, --help            show this help message and exit
  --body BODY           details; '-' reads stdin
  --link LINK           label=https://… (repeatable)
  --option OPTION       an answer button (repeatable; blocked/decision)
  --urgent              blocked/decision only, when prod or a customer is hurting, money is
                        leaking, or a deadline is today
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
<summary><b>Dashboard</b>: <code>serve</code>, <code>open</code>, <code>install-service</code>, <code>uninstall-service</code>, <code>restart-service</code></summary>

### bridge serve

run the dashboard web server

```text
usage: bridge serve [-h] [--port PORT]

options:
  -h, --help   show this help message and exit
  --port PORT
```

### bridge open

open the dashboard as an agterm overlay (or in the browser)

```text
usage: bridge open [-h] [--port PORT] [--target TARGET] [--browser]

options:
  -h, --help       show this help message and exit
  --port PORT
  --target TARGET  agterm session id to overlay (default: this tab)
  --browser
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
<summary><b>Setup</b>: <code>install-hooks</code>, <code>uninstall-hooks</code>, <code>hook-stats</code></summary>

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

### bridge hook-stats

hook latency percentiles

```text
usage: bridge hook-stats [-h] [--last LAST]

options:
  -h, --help   show this help message and exit
  --last LAST
```

</details>

<details>
<summary><b>Outside services</b>: <code>enrich</code>, <code>remote-sync</code></summary>

### bridge enrich

refresh GitHub PR and tracker issue state for live sessions

```text
usage: bridge enrich [-h] [--force]

options:
  -h, --help  show this help message and exit
  --force     ignore freshness and refetch everything
```

### bridge remote-sync

collect sessions from configured remote hosts now

```text
usage: bridge remote-sync [-h]

options:
  -h, --help  show this help message and exit
```

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
usage: guild lead [-h] {add,label,sync,retire,wake} ...

positional arguments:
  {add,label,sync,retire,wake}
    add                 make a tab a team lead: name, tint, a place in the TEAM workspace
    label               change a lead's project label and rename its tab
    sync                apply a lead's tab name, tint and place in TEAM again
    retire              undo enrollment; the tab goes back, the name is never reused
    wake                type a first prompt into a lead's idle tab, so its role arrives

options:
  -h, --help            show this help message and exit
```

### guild lead add

make a tab a team lead: name, tint, a place in the TEAM workspace

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
  --ref REF      tracker project or issue; repeat
  --label LABEL  short project label; the tab is named '<Name> · <label>'
  --alias ALIAS  spelling for dictation, e.g. Arlow
  --tint TINT
```

### guild lead label

change a lead's project label and rename its tab

```text
usage: guild lead label [-h] name label

positional arguments:
  name
  label

options:
  -h, --help  show this help message and exit
```

### guild lead sync

apply a lead's tab name, tint and place in TEAM again

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

### guild lead wake

type a first prompt into a lead's idle tab, so its role arrives

```text
usage: guild lead wake [-h] name

positional arguments:
  name

options:
  -h, --help  show this help message and exit
```

</details>

<details>
<summary><b>Workers</b>: <code>worker</code></summary>

### guild worker

start a task in its own tab nested under yours, and check that it started; `guild worker close <tab>` closes one, `guild worker closed` lists them

```text
usage: guild worker [-h] --prompt PROMPT [--cwd CWD] name

positional arguments:
  name             the task, in English; the tab's name

options:
  -h, --help       show this help message and exit
  --prompt PROMPT  the brief; - reads stdin
  --cwd CWD        default: this directory
```

### guild worker close

close your own nested tab and keep what resumes it

```text
usage: guild worker close [-h] [--force] tab

positional arguments:
  tab         the tab's name or agterm id

options:
  -h, --help  show this help message and exit
  --force     close even if it is mid-turn or has a live background run
```

### guild worker closed

closed workers, newest first, with the command that resumes each

```text
usage: guild worker closed [-h] [--limit LIMIT]

options:
  -h, --help     show this help message and exit
  --limit LIMIT
```

</details>

<details>
<summary><b>What team leads show and keep</b>: <code>work</code>, <code>notes</code>, <code>handover</code>, <code>ack</code>, <code>ack-cost</code></summary>

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
usage: guild work set [-h] [--lead LEAD] name [refs ...]

positional arguments:
  name         a few plain words, e.g. 'Nightly export worker'
  refs         PROJ-12, acme/web#34, #34 (your repo), or a URL

options:
  -h, --help   show this help message and exit
  --lead LEAD  default: your own
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

### guild ack-cost

what board posts reaching every lead cost; tokens are an estimate

```text
usage: guild ack-cost [-h] [--hours HOURS] [--json]

options:
  -h, --help     show this help message and exit
  --hours HOURS
  --json
```

</details>

<details>
<summary><b>Your asks</b>: <code>ask</code></summary>

### guild ask

the owner's asks, each taken to verified with evidence of what they would see

```text
usage: guild ask [-h] {add,list,show,start,note,wait,done,verify,close,reopen} ...

positional arguments:
  {add,list,show,start,note,wait,done,verify,close,reopen}
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
    reopen              back to open: the result is not what they asked for

options:
  -h, --help            show this help message and exit
```

### guild ask add

record an ask in the owner's own words

```text
usage: guild ask add [-h] [--words WORDS] [--from N] [--ref REF] owner

positional arguments:
  owner          the lead that answers for it, or the assistant's handle

options:
  -h, --help     show this help message and exit
  --words WORDS  their words verbatim; default: stdin
  --from N       quote message N's body as the words
  --ref REF      a PR, issue or page URL; repeat
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

### guild ask reopen

back to open: the result is not what they asked for

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
usage: guild review [-h] {list,sentence,doc,published,repost} ...

positional arguments:
  {list,sentence,doc,published,repost}
    list                what the queue shows
    sentence            the short description of what a PR does, for its review post
    doc                 register a large PR's HTML document; the page serves this copy
    published           record a post made by hand, so it is never posted again
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

the short description of what a PR does, for its review post

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

record a post made by hand, so it is never posted again

```text
usage: guild review published [-h] pr link

positional arguments:
  pr
  link

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
