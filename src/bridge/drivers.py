import json
import re
import shlex
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from bridge.config import Remote
from bridge.db import transaction
from bridge.guard import segment_command
from bridge.registry import NEVER_RECENT_TURN
from bridge.reviews import record_agent_marks, record_branch_writes
from bridge.transcript import find_transcript

FIRST_SCAN_BYTES = 5_000_000
RECENTLY_SEEN_MS = 6 * 3_600_000
LOCAL_HOST = "local"
"""The host name of links to tmux sessions on this machine."""
_SCRIPT_NAME_RE = re.compile(r"(resume|launch)[\w-]*\.sh$")
_SESSION_RE = re.compile(r"\w[\w.-]*|\$\d+")
_SHELL_KEYWORDS = frozenset({"until", "while", "if", "then", "do", "else", "elif", "!"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash"})
_SSH_VALUE_OPTIONS = frozenset(
    {"-B", "-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L", "-l", "-m", "-O", "-o", "-p", "-Q", "-R",
     "-S", "-W", "-w"}
)  # fmt: skip
_REPO_RE = re.compile(r"[\w.-]+/[\w.-]+")
_REDIRECTION_RE = re.compile(r"\d*(<<-?|<<<|>>|>&|<&|<|>)")


@dataclass(frozen=True)
class Link:
    conversation_id: str
    host: str
    session: str
    used_at: int


@dataclass
class NewEntries:
    commands: list[tuple[str, int]]
    """Bash commands with the time each ran."""
    pr_links: list[tuple[str, int]]
    """PRs Claude Code recorded as created or linked by this conversation, as pr: refs with their time."""
    offset: int


def scan_transcripts(conn: sqlite3.Connection, remotes: tuple[Remote, ...], at_ms: int) -> int:
    """Reads new transcript lines of live conversations: ssh commands that drive remote tmux sessions, and PR links.

    Transcripts are read incrementally from a stored offset, so each scan only parses new turns.
    """
    # A session that exits or clears right after its last command still has lines nobody has read.
    rows = conn.execute(
        """
        SELECT c.conversation_id, c.transcript_path, coalesce(s.offset, -1) AS offset
        FROM conversations c
        LEFT JOIN transcript_scans s ON s.conversation_id = c.conversation_id
        WHERE c.last_seen_at > ?
           OR EXISTS (SELECT 1 FROM instances i WHERE i.conversation_id = c.conversation_id AND i.ended_at IS NULL)
        """,
        (at_ms - RECENTLY_SEEN_MS,),
    ).fetchall()
    found = 0
    for row in rows:
        conversation_id = str(row["conversation_id"])
        path = find_transcript(conversation_id, row["transcript_path"])
        if path is None:
            continue
        entries = read_new_entries(path, int(row["offset"]), at_ms, conversation_id)
        subagent_scans = _read_new_subagent_commands(conn, path, conversation_id, at_ms)
        latest: dict[tuple[str, str], int] = {}
        for command, used_at in entries.commands:
            links = map_command_to_links(command, conversation_id, remotes, used_at) if remotes else []
            for link in links + map_command_to_local_links(command, conversation_id, used_at):
                key = (link.host, link.session)
                latest[key] = max(latest.get(key, 0), link.used_at)
        with transaction(conn):
            _store_scan_offset(conn, conversation_id, entries.offset)
            record_agent_marks(conn, conversation_id, entries.commands)
            record_branch_writes(conn, conversation_id, entries.commands)
            for key, offset, commands in subagent_scans:
                _store_scan_offset(conn, key, offset)
                record_agent_marks(conn, conversation_id, commands)
                record_branch_writes(conn, conversation_id, commands)
            for (host, session), used_at in latest.items():
                conn.execute(
                    """
                    INSERT INTO remote_drivers (conversation_id, host, session, last_seen_at) VALUES (?, ?, ?, ?)
                    ON CONFLICT (conversation_id, host, session)
                    DO UPDATE SET last_seen_at = max(last_seen_at, excluded.last_seen_at)
                    """,
                    (conversation_id, host, session, used_at),
                )
            conn.executemany(
                """
                INSERT INTO refs (conversation_id, ref, source, first_seen_at, last_seen_at, last_turn)
                VALUES (?, ?, 'pr-link', ?, ?, ?)
                ON CONFLICT (conversation_id, ref) DO UPDATE SET
                    source = 'pr-link', first_seen_at = min(first_seen_at, excluded.first_seen_at),
                    last_seen_at = max(last_seen_at, excluded.last_seen_at)
                """,
                [
                    (conversation_id, ref, linked_at, linked_at, NEVER_RECENT_TURN)
                    for ref, linked_at in entries.pr_links
                ],
            )
        found += len(latest) + len(entries.pr_links)
    return found


def _store_scan_offset(conn: sqlite3.Connection, key: str, offset: int) -> None:
    conn.execute(
        """
        INSERT INTO transcript_scans (conversation_id, offset) VALUES (?, ?)
        ON CONFLICT (conversation_id) DO UPDATE SET offset = excluded.offset
        """,
        (key, offset),
    )


def _read_new_subagent_commands(
    conn: sqlite3.Connection, transcript: Path, conversation_id: str, at_ms: int
) -> list[tuple[str, int, list[tuple[str, int]]]]:
    """(scan key, offset, commands) per subagent file: they post comments under the account too."""
    scans: list[tuple[str, int, list[tuple[str, int]]]] = []
    for path in sorted(transcript.with_suffix("").glob("subagents/*.jsonl")):
        key = f"{conversation_id}/{path.stem}"
        row = conn.execute("SELECT offset FROM transcript_scans WHERE conversation_id = ?", (key,)).fetchone()
        entries = read_new_entries(path, int(row[0]) if row else -1, at_ms)
        scans.append((key, entries.offset, entries.commands))
    return scans


def load_drivers(conn: sqlite3.Connection, since_ms: int) -> list[Link]:
    rows = conn.execute(
        "SELECT conversation_id, host, session, last_seen_at FROM remote_drivers WHERE last_seen_at >= ?", (since_ms,)
    ).fetchall()
    return [Link(str(r["conversation_id"]), str(r["host"]), str(r["session"]), int(r["last_seen_at"])) for r in rows]


def read_new_entries(
    path: Path, offset: int, at_ms: int, conversation_id: str | None = None, first_scan_bytes: int = FIRST_SCAN_BYTES
) -> NewEntries:
    """Entries appended since offset; a first scan reads PR links from the whole file, commands from the tail."""
    result = NewEntries([], [], offset)
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        # A transcript shorter than our offset was rewritten: read it again from the start.
        start = 0 if offset < 0 or offset > size else offset
        result.offset = start
        if start > 0:
            # The byte before start tells whether start is on a line boundary; if not, skip the partial line.
            f.seek(start - 1)
            if f.read(1) != b"\n":
                partial = f.readline()
                if not partial.endswith(b"\n"):
                    return result
                result.offset += len(partial)
        else:
            f.seek(0)
        commands_from = size - first_scan_bytes if offset < 0 else 0
        # Streamed line by line: first scans read whole transcripts, which can be tens of megabytes.
        for raw in f:
            if not raw.endswith(b"\n"):
                break  # half-written entry: read it next time
            line_start = result.offset
            result.offset += len(raw)
            is_command = b'"Bash"' in raw and line_start >= commands_from
            if not is_command and b'"pr-link"' not in raw:
                continue
            try:
                entry: object = json.loads(raw)
            except ValueError:
                continue
            data_entry = cast(dict[str, object], entry) if isinstance(entry, dict) else {}
            used_at = map_timestamp_to_ms(data_entry.get("timestamp"))
            if used_at is None:
                continue
            # A clock-skewed future time would keep a link "just used" until that time arrives.
            used_at = min(used_at, at_ms)
            if data_entry.get("type") == "pr-link":
                repo, number = data_entry.get("prRepository"), data_entry.get("prNumber")
                # A forked or copied history can carry another conversation's links.
                owner = data_entry.get("sessionId")
                if conversation_id and isinstance(owner, str) and owner != conversation_id:
                    continue
                if isinstance(repo, str) and _REPO_RE.fullmatch(repo) and isinstance(number, int):
                    result.pr_links.append((f"pr:{repo}#{number}", used_at))
                continue
            if not is_command:
                continue
            message = data_entry.get("message")
            content = cast(dict[str, object], message).get("content") if isinstance(message, dict) else None
            for block in cast(list[object], content) if isinstance(content, list) else []:
                data = cast(dict[str, object], block) if isinstance(block, dict) else {}
                tool_input = data.get("input")
                command = cast(dict[str, object], tool_input).get("command") if isinstance(tool_input, dict) else None
                if data.get("type") == "tool_use" and data.get("name") == "Bash" and isinstance(command, str):
                    result.commands.append((command, used_at))
    return result


def map_timestamp_to_ms(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value).timestamp() * 1000)
    except ValueError:
        return None


def map_command_to_links(
    command: str, conversation_id: str, remotes: tuple[Remote, ...], used_at: int = 0
) -> list[Link]:
    """tmux sessions a command addresses on a remote host over ssh: tmux targets, or a resume/launch script."""
    links: list[Link] = []
    bodies: list[tuple[int, str]] = []
    segments = segment_command(command, bodies)
    for index, (tokens, _) in enumerate(segments):
        parsed = _parse_ssh(_strip_shell_keywords(tokens), remotes)
        if parsed is None:
            continue
        remote, remote_command = parsed
        lines = [remote_command]
        # `ssh host <<EOF` and `ssh host bash <<EOF` run the heredoc body as a script; `ssh host cat <<EOF` does not.
        if _reads_script_from_stdin(remote_command):
            lines.extend(body for owner, body in bodies if owner == index)
        for line in lines:
            for name in _names_in(line, depth=3):
                links.append(Link(conversation_id, remote.name, name, used_at))
    return links


def map_command_to_local_tmux_sessions(command: str) -> list[str]:
    return _names_in(command, depth=3)


def map_command_to_remote_names(command: str, remotes: tuple[Remote, ...]) -> set[str]:
    """Configured remotes a command runs something on over ssh, tmux or not."""
    parsed = (_parse_ssh(_strip_shell_keywords(tokens), remotes) for tokens, _ in segment_command(command))
    return {found[0].name for found in parsed if found is not None}


def map_command_to_local_links(command: str, conversation_id: str, used_at: int = 0) -> list[Link]:
    """tmux sessions a command addresses on this machine; an ssh line's words are not tmux's, so they add nothing."""
    links: list[Link] = []
    for tokens, _ in segment_command(command):
        line = " ".join(shlex.quote(token) for token in _strip_shell_keywords(tokens))
        links.extend(Link(conversation_id, LOCAL_HOST, name, used_at) for name in _names_in(line, depth=3))
    return links


def _reads_script_from_stdin(remote_command: str) -> bool:
    words = remote_command.split()
    if not words:
        return True
    return (
        Path(words[0]).name in _SHELLS
        and not any(re.fullmatch(r"-[a-z]*c", w) for w in words[1:])
        and all(w.startswith("-") for w in words[1:])
    )


def _strip_shell_keywords(tokens: list[str]) -> list[str]:
    while tokens and tokens[0] in _SHELL_KEYWORDS:
        tokens = tokens[1:]
    if tokens and tokens[0] == "sudo":
        tokens = tokens[1:]
        while tokens and tokens[0].startswith("-"):
            tokens = tokens[2:] if tokens[0] in ("-u", "-g") else tokens[1:]
    if len(tokens) > 1 and tokens[0] == "timeout":
        tokens = tokens[2:]
    return tokens


def _parse_ssh(tokens: list[str], remotes: tuple[Remote, ...]) -> tuple[Remote, str] | None:
    """The remote and the command line ssh would run there; ssh joins its trailing words with spaces."""
    if not tokens or Path(tokens[0]).name != "ssh":
        return None
    i = 1
    while i < len(tokens) and tokens[i].startswith("-"):
        option = tokens[i]
        i += 2 if option in _SSH_VALUE_OPTIONS else 1
    if i >= len(tokens):
        return None
    host = tokens[i].rpartition("@")[2]
    remote = next((r for r in remotes if host in r.host_names()), None)
    if remote is None:
        return None
    return remote, " ".join(_drop_redirections(tokens[i + 1 :]))


def _drop_redirections(tokens: list[str]) -> list[str]:
    kept: list[str] = []
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
        elif _REDIRECTION_RE.fullmatch(token):
            skip_next = True
        elif not re.fullmatch(_REDIRECTION_RE.pattern + r"\S+", token):
            kept.append(token)
    return kept


def _names_in(line: str, depth: int) -> list[str]:
    """Remote commands nest (ssh 'bash -lc "tmux …"'), so a shell's -c argument is lexed again a few levels deep."""
    names: list[str] = []
    for tokens, _ in segment_command(line):
        tokens = _strip_shell_keywords(_drop_redirections(tokens))
        names.extend(_session_names(tokens))
        if depth > 0 and tokens and Path(tokens[0]).name in _SHELLS:
            for i, token in enumerate(tokens[1:-1], start=1):
                if re.fullmatch(r"-[a-z]*c", token):
                    names.extend(_names_in(tokens[i + 1], depth - 1))
                    break
    return names


def _session_names(tokens: list[str]) -> list[str]:
    names: list[str] = []
    if tokens and Path(tokens[0]).name == "tmux":
        targets: list[str] = []
        seen_command = False
        for i, token in enumerate(tokens[1:], start=1):
            # `--` before the tmux command ends global options; after it, the rest are arguments such as keys.
            if token == "--" and seen_command:
                break
            seen_command = seen_command or not token.startswith("-")
            # -tname carries its value; -t, -dt, -ds take the next word.
            if re.fullmatch(r"-[ts].+", token):
                targets.append(token[2:])
            elif re.fullmatch(r"-[a-zA-Z]*[ts]", token) and i + 1 < len(tokens):
                targets.append(tokens[i + 1])
        names.extend(t.lstrip("=").split(":")[0].split(".")[0] for t in targets)
    else:
        script = 1 if tokens and Path(tokens[0]).name in _SHELLS else 0
        if (
            len(tokens) > script + 1
            and _SCRIPT_NAME_RE.search(tokens[script])
            and not tokens[script + 1].startswith("-")
        ):
            names.append(tokens[script + 1])
    return [n for n in names if _SESSION_RE.fullmatch(n)]
