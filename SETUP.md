# Setting up Guild (a guide for the coding agent)

You are a Claude Code session in agterm, and the user asked you to set up Guild. Follow these steps in order. Run
every command yourself; the user types nothing except answers to your questions. After each step, run its check and
tell the user one line on what happened. If a check fails, stop, say what failed with its output, and propose a fix;
do not work around it silently.

Ask the user only what you cannot find out, all at once at step 3: their name, what to call Right Hand, and the first
team lead's project. Everything else has a default below.

The dashboard's port is 8780 unless step 1 finds it taken and the user picks another. Below it is `PORT`: use the same
value in every command.

## 1. Check what is there

```sh
sw_vers -productName                      # must be macOS
echo "$AGTERM_SESSION_ID"; agtermctl version   # must be inside agterm; agterm 0.39 or later
uv --version                              # needed; Python 3.12+ comes through uv
claude --version
gh auth status                            # optional: only the PR features need it
command -v bridge guild bridge-hook       # anything found here that is not Guild is a name clash
lsof -nP -iTCP:8780 -sTCP:LISTEN          # the dashboard's default port must be free
ls ~/.claude/skills/bridge 2>/dev/null    # an existing skill of that name would be replaced
```

- Not macOS, or not inside agterm: stop. Guild needs both; point the user to https://github.com/umputun/agterm.
- No `uv`: ask before installing it (`brew install uv`, or the installer at https://docs.astral.sh/uv/).
- `bridge`, `guild` or `bridge-hook` already exist and are not from this repo, port 8780 is taken, or
  `~/.claude/skills/bridge` exists: tell the user what you found and ask how to proceed before going on.
- No signed-in `gh`: fine. Say that PR states, PR claims and the review queue stay off until it is signed in.

## 2. Install

```sh
git clone https://github.com/Yuvasee/guild ~/.local/share/guild    # or git -C ~/.local/share/guild pull if present
uv tool install ~/.local/share/guild
bridge install-hooks          # adds bridge's hooks to ~/.claude/settings.json; other hooks stay
mkdir -p ~/.claude/skills && cp -R ~/.local/share/guild/agent/skills/bridge ~/.claude/skills/
```

Check: `command -v bridge guild bridge-hook` prints three paths; `grep -c bridge-hook ~/.claude/settings.json` is not
0; `ls ~/.claude/skills/bridge/SKILL.md` exists.

## 3. Ask the user, then write the config

The config comes before the dashboard: a running dashboard keeps the config it started with.

Ask in one message:

1. Their name, as the agents should call them (e.g. "Mira").
2. What to call Right Hand: one short first name (e.g. "Xavi"). Its tab and handle are that name in lowercase.
3. The first team lead: a project directory, one sentence on what it owns, a one-word name for the team lead (one
   capitalised word, e.g. "Alex"), and a one-word project label (e.g. "Checkout"). Suggest the user's own project
   directories (look in the usual places, such as `~/code` or `~/projects`); never suggest the Guild checkout itself.

Write `~/.config/bridge/config.toml` (create the directory; if the file exists, change only these keys):

```toml
owner_name = "<their name>"
user_alias = "<their name>"
assistant_handle = "<right hand name, lowercase>"
compact_at_tokens = 150000
```

`compact_at_tokens` turns on compaction with keep notes; tell the user they can set it to 0 to turn it off. Other
keys are in the README and `docs/adapters.md`; leave them at their defaults.

Check: `bridge sessions` runs without error (it reads the config).

## 4. Start the dashboard

Default: a launchd service, so it survives reboots.

```sh
bridge install-service --port PORT
```

If the user prefers no background service, run `bridge serve --port PORT` in a separate agterm tab instead (it stops
when that tab closes).

If the config changes later, restart the dashboard so it reads it: `bridge restart-service`, or restart `bridge serve`.

Check: `curl -sf http://127.0.0.1:PORT/api/state >/dev/null && echo up`.

## 5. Start Right Hand

```sh
agtermctl session new --name "<right hand name, lowercase>" --cwd ~ --no-select --json   # note .result.id
sleep 2
agtermctl session type --target <id> "claude"
agtermctl session type --target <id> $'\r'
```

A fresh Claude Code tab usually shows dialogs before its prompt: "trust this folder" (answer yes), "Try the new
fullscreen renderer?" (answer "Not now"), and a login if the user is not signed in. Ask the user to answer them in
that tab, and say which answers to pick.

Check: within a minute, `bridge sessions` lists a session with that handle. Right Hand's role arrives with its
session start; its first prompt from the user will show it.

## 6. Start the first team lead

```sh
agtermctl session new --name "<label, lowercase>" --cwd "<project directory>" --no-select --json   # note .result.id
sleep 2
agtermctl session type --target <id> "claude"
agtermctl session type --target <id> $'\r'
```

The same dialogs as in step 5 may appear; ask the user to answer them the same way.

Wait until `bridge sessions` lists it (its handle is the tab name), then:

```sh
guild lead add <Name> --tab <handle> --scope "<one sentence>" --label <Label>
guild lead wake <Name>
```

`guild lead add` renames the tab to `<Name> · <Label>`, tints it and moves it to a `TEAM` workspace.

If `guild lead wake` prints `<Name>: unproven` and exits 1, nothing was typed: the empty prompt's grey hint can look
like a draft to it. Type the first prompt yourself:

```sh
agtermctl session type --target <id> "Guild: you are <Name>, a team lead now. Your role is in the context above; start with it."
agtermctl session type --target <id> $'\r'
```

Check: `guild leads` lists the team lead; the dashboard shows it under **Team Leads**.

## 7. Open the dashboard and hand over

```sh
bridge open --browser --port PORT    # or without --browser, to show it as an overlay over this tab
```

Tell the user, in a few lines:

- the dashboard's address (http://127.0.0.1:PORT/);
- that from now on they talk to Right Hand in its tab, and to a team lead in its tab when they want to go deep;
- that questions for them appear on the page under **Needs you**;
- that team leads use the connectors of their Claude account: if it is connected to a tracker or chat, the first team
  lead may start by reading it to learn its scope;
- how to undo everything: `bridge uninstall-service`, `bridge uninstall-hooks`, `uv tool uninstall bridge`,
  and removing `~/.claude/skills/bridge`, `~/.config/bridge` and `~/.local/state/bridge`.

Finish with one summary line of what was installed and where.
