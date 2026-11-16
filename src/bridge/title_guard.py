import re
from dataclasses import dataclass
from itertools import pairwise
from pathlib import PurePosixPath
from urllib.parse import unquote

from bridge.agent_prefix import CLAUDE_PREFIX, LINEAR_ISSUE_TOOLS, TITLE_RULE, is_prefixed, strip_agent_prefix
from bridge.guard import SHELL_KEYWORDS, SHELLS, segment_command

_HEREDOC_SUBSTITUTION_RE = re.compile(r"^\$\(\s*cat\s+<<-?\s*(['\"]?)(\w+)\1[^\n]*\n(.*?)\n\s*\2\s*\)$", re.DOTALL)
_GIT_GLOBAL_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
_GH_GLOBAL_VALUE_OPTIONS = frozenset({"-R", "--repo", "--hostname"})


@dataclass(frozen=True)
class _Options:
    """The value-taking options of one command, by long name and short letter, each mapped to one key."""

    longs: dict[str, str]
    shorts: dict[str, str]
    git: bool = False
    """git takes an unambiguous prefix of a long name, and keeps the '=' of -m=x in the value; pflag does neither."""


def _options(spec: str, git: bool = False) -> _Options:
    """From space-separated parts such as 'title/t': the key and long name first, then any short letter."""
    longs: dict[str, str] = {}
    shorts: dict[str, str] = {}
    for part in spec.split():
        names = part.split("/")
        for name in names:
            if len(name) == 1:
                shorts[name] = names[0]
            else:
                longs[name] = names[0]
    return _Options(longs, shorts, git)


_GH_GLOBAL = "repo/R hostname"
_GH_ISSUE_PR = _options(
    f"{_GH_GLOBAL} title/t body/b body-file/F base/B head/H assignee/a label/l milestone/m project/p reviewer/r "
    "template/T recover add-assignee remove-assignee add-label remove-label add-project remove-project add-reviewer "
    "remove-reviewer"
)
_GH_MERGE = _options(f"{_GH_GLOBAL} subject/t body/b body-file/F author-email/A match-head-commit")
_GH_API = _options(f"{_GH_GLOBAL} method/X header/H field/F raw-field/f input jq/q template/t preview/p cache")
_GIT_COMMIT = _options(
    "message/m file/F reuse-message/C reedit-message/c template/t author date fixup squash trailer cleanup "
    "pathspec-from-file",
    git=True,
)
_GIT_MERGE = _options("message/m file/F strategy/s strategy-option/X into-name cleanup", git=True)
_GH_LABEL = _options(f"{_GH_GLOBAL} name/n color/c description/d")
_API_LABELS_RE = re.compile(
    r"(?:^|/)repos/[^/\s]+/[^/\s]+/(?:(issues/[^/\s?]+)(/labels)?|labels)(?:/([^?\s]*))?/?(?:\?|$)"
)
_GH_TITLE_COMMANDS = {
    (group, action): _GH_ISSUE_PR for group in ("pr", "issue") for action in ("create", "new", "edit")
}


def find_prefixed_titles(command: str) -> list[str]:
    """Titles a shell command would give a PR, an issue or a commit that start with the robot."""
    found: list[str] = []
    for tokens, _ in segment_command(command):
        found.extend(t for t in _map_tokens_to_titles(tokens) if is_prefixed(t))
    return found


def find_owner_label_writes(command: str, label: str) -> list[str]:
    """gh commands that would put the owner's label on a PR or issue, take it off, or edit or delete the label itself,
    also inside a loop, an if or a shell's -c script. Only commands that name it: one that clears every label is undone
    by the next poll."""
    wanted = label.strip().casefold()
    if not wanted:
        return []
    found: list[str] = []
    bodies: list[tuple[int, str]] = []
    for index, (tokens, _) in enumerate(segment_command(command, bodies)):
        while tokens and tokens[0] in SHELL_KEYWORDS:
            tokens = tokens[1:]
        if not tokens:
            continue
        program = PurePosixPath(tokens[0]).name
        if program in SHELLS:
            stdin = [text for owner, text in bodies if owner == index]
            scripts = [script for flag, script in pairwise(tokens[1:]) if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", flag)]
            found += [w for script in [*scripts, *stdin] for w in find_owner_label_writes(script, label)]
        elif program == "eval":
            found += find_owner_label_writes(" ".join(tokens[1:]), label)
        elif program == "gh" and _writes_label(tokens[1:], wanted, command.casefold()):
            found.append(" ".join(tokens))
    return found


def map_label_write_to_denial(label: str, owner: str, command: str) -> str:
    return (
        f"bridge: only {owner}'s Publish sets the label {label}: it tells the review bot {owner} reviewed the PR. "
        f"This command would change it ({command[:120]}). Leave that label alone."
    )


def _writes_label(args: list[str], wanted: str, text: str) -> bool:
    """text: the whole command, casefolded, for a value the shell or a request body fills in."""
    group, i = _read_positional(args, 0)
    if group == "api":
        found, positional = _scan(args[i:], _GH_API)
        methods = [value.upper() for key, value in found if key == "method"]
        fields = [value.partition("=") for key, value in found if key in ("field", "raw-field")]

        def field(*names: str) -> list[str]:
            return [value for name, _, value in fields if name in names]

        has_input = any(key == "input" for key, _ in found)
        method = methods[-1] if methods else ("POST" if fields or has_input else "GET")
        match = _API_LABELS_RE.search(positional[0]) if positional else None
        if match is None or method in ("GET", "HEAD"):
            return False
        issue, on_labels, name = match.groups()
        if issue and not on_labels:
            named = field("labels", "labels[]")
        elif issue:
            named = [unquote(name or ""), *field("labels", "labels[]")]
        elif name:
            named = [unquote(name), *field("new_name")]
        else:
            # A repo's labels endpoint with no name creates a label, which puts it on nothing.
            return False
        return _names_label(named, wanted, text) or (has_input and wanted in text)
    action, i = _read_positional(args, i)
    if group in ("pr", "issue") and action in ("create", "new", "edit"):
        found, _ = _scan(args[i:], _GH_ISSUE_PR)
        return _names_label([v for k, v in found if k in ("label", "add-label", "remove-label")], wanted, text)
    if group == "label" and action in ("edit", "delete", "create"):
        found, positional = _scan(args[i:], _GH_LABEL)
        # create changes the label only with --force, which updates one that exists.
        if action == "create" and not any(a in ("-f", "--force") for a in args[i:]):
            return False
        return _names_label([*positional[:1], *(v for k, v in found if k == "name")], wanted, text)
    return False


def _names_label(values: list[str], wanted: str, text: str) -> bool:
    """gh and the labels endpoint take several labels joined by commas; GitHub matches names ignoring case. A value the
    shell fills in counts when the command spells the label anywhere, as in LABEL=…; gh … "$LABEL"."""
    parts = [part.strip().casefold() for value in values for part in _unwrap(value).split(",")]
    return wanted in parts or (any("$" in part for part in parts) and wanted in text)


def find_prefixed_linear_title(tool: str, tool_input: dict[str, object]) -> str | None:
    title = tool_input.get("title")
    if tool in LINEAR_ISSUE_TOOLS and isinstance(title, str) and is_prefixed(title):
        return title
    return None


def map_titles_to_denial(titles: list[str]) -> str:
    shown = titles[0].strip().splitlines()[0][:120]
    return (
        f"bridge: {TITLE_RULE}. This would title something '{shown}'. Retitle it without the prefix "
        f"('{strip_agent_prefix(shown)[:120]}') and keep '{CLAUDE_PREFIX}' at the start of the body."
    )


def _map_tokens_to_titles(tokens: list[str]) -> list[str]:
    if not tokens:
        return []
    if tokens[0] == "git":
        return _git_titles(tokens[1:])
    if tokens[0] == "gh":
        return _gh_titles(tokens[1:])
    return []


def _git_titles(args: list[str]) -> list[str]:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in _GIT_GLOBAL_VALUE_OPTIONS else 1
    if i >= len(args) or args[i] not in ("commit", "merge"):
        return []
    found, _ = _scan(args[i + 1 :], _GIT_COMMIT if args[i] == "commit" else _GIT_MERGE)
    keys = [key for key, _ in found]
    # A fixup or squash commit takes its subject from the commit it names; -m only adds to the body.
    if "fixup" in keys or "squash" in keys:
        return []
    return [_first_line(value) for key, value in found if key == "message"][:1]


def _gh_titles(args: list[str]) -> list[str]:
    group, i = _read_positional(args, 0)
    if group == "api":
        found, positional = _scan(args[i:], _GH_API)
        methods = [value.upper() for key, value in found if key == "method"]
        # graphql fields are query variables, and a GET sends fields as query parameters: neither names a title.
        if positional[:1] == ["graphql"] or methods[-1:] == ["GET"]:
            return []
        fields = (value.partition("=") for key, value in found if key in ("field", "raw-field"))
        return [_unwrap(value) for name, eq, value in fields if name == "title" and eq]
    action, i = _read_positional(args, i)
    if (group, action) == ("pr", "merge"):
        found, _ = _scan(args[i:], _GH_MERGE)
        return [_first_line(value) for key, value in found if key == "subject"]
    options = _GH_TITLE_COMMANDS.get((group or "", action or ""))
    if options is None:
        return []
    found, _ = _scan(args[i:], options)
    return [_unwrap(value) for key, value in found if key == "title"]


def _read_positional(args: list[str], start: int) -> tuple[str | None, int]:
    """The next word that is no option, and the index after it."""
    i = start
    while i < len(args):
        if args[i] in _GH_GLOBAL_VALUE_OPTIONS:
            i += 2
        elif args[i].startswith("-"):
            i += 1
        else:
            return args[i], i + 1
    return None, i


def _scan(args: list[str], options: _Options) -> tuple[list[tuple[str, str]], list[str]]:
    """Every value-taking option with its value, in order, and the positional words. A value is consumed with its
    option, so a body that reads like an option is never taken for one; an unknown option counts as a switch."""
    found: list[tuple[str, str]] = []
    positional: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == "--":
            positional.extend(args[i:])
            break
        if arg.startswith("--"):
            name, eq, value = arg[2:].partition("=")
            key = _match_long(name, options)
            if key is None:
                continue
            if not eq:
                if i >= len(args):
                    break
                value, i = args[i], i + 1
            found.append((key, value))
        elif arg.startswith("-") and len(arg) > 1:
            for k, letter in enumerate(arg[1:], start=1):
                key = options.shorts.get(letter)
                if key is None:
                    continue
                value = arg[k + 1 :]
                if value and not options.git:
                    value = value.removeprefix("=")
                elif not value and i < len(args):
                    value, i = args[i], i + 1
                found.append((key, value))
                break
        else:
            positional.append(arg)
    return found, positional


def _match_long(name: str, options: _Options) -> str | None:
    if name in options.longs:
        return options.longs[name]
    if not options.git or len(name) < 2:
        return None
    matches = {key for long, key in options.longs.items() if long.startswith(name)}
    return matches.pop() if len(matches) == 1 else None


def _unwrap(value: str) -> str:
    """The text a `$(cat <<'EOF' … EOF)` substitution or a $'…' string would yield; any other value as written."""
    match = _HEREDOC_SUBSTITUTION_RE.match(value.strip())
    if match:
        return match.group(3)
    # The lexer keeps the $ of $'…' before its text; Bash drops it and turns \n into a newline.
    if re.match(r"\$[^\w({]", value):
        return value[1:].replace("\\n", "\n")
    return value


def _first_line(value: str) -> str:
    text = _unwrap(value).lstrip("\n")
    return text.split("\n", 1)[0]
