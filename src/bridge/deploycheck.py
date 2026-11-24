import fcntl
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import string
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from bridge.board import HINT_SENT
from bridge.compaction import map_screen_to_busy, map_screen_to_draft, map_screen_to_transcript
from bridge.config import Config, state_dir
from bridge.feed import convert_user_message_to_typed_text
from bridge.install import map_waiter_to_hook_groups
from bridge.procinfo import process_args
from bridge.rewake_marks import install_waiter_script
from bridge.tabinput import AGTERM_SLOW_S, ENTER, is_tab_selected, run_agterm, type_when_idle

CHECK_TAB_NAME = "bridge deploy check"
LANDS_WITHIN_S = 10.0
CAT_STARTS_WITHIN_S = 30.0
CAT_READY = "bridge-check-ready"
# agterm runs a tab's command through an interactive login shell, whose rc files can take over 10 s under load; typing
# starts once cat has said it runs, so the check times the typing, not the shell.
CAT_COMMAND = f"printf '%s\\n' {CAT_READY}; exec /bin/cat"
REWAKE_TAB_NAME = "bridge rewake check"
LONG_TAB_NAME = "bridge long message check"
# A check tab's name ends in the epoch second it was opened: agterm's tree keeps no creation time.
CHECK_TAB_RE = re.compile(rf"(?:{CHECK_TAB_NAME}|{REWAKE_TAB_NAME}|{LONG_TAB_NAME}) (\d+)-[0-9a-f]{{6}}")
# Far past the longest check, its waits stretched by a slow agterm: an older check tab belongs to a check that ended.
CHECK_TAB_STALE_S = 15 * 60
CLOSE_TRIES = 3
LONG_MESSAGE_CHARS = 3_000
CLAUDE_PID_FILE = "claude.pid"
READY_WITHIN_S = 45.0
# Claude Code writes the session record the waiter needs up to 5 s after its input box shows.
WAITER_WITHIN_S = 15.0
ANSWER_WITHIN_S = 60.0
DOWN = "\x1b[B"
TRUST_ANSWER = "Yes, I trust this folder"
# Only answers the waiter's call, from a file the check writes: the check reaches no database and no session of ours.
STAND_IN_HOOK = """#!/bin/sh
[ "$1" = rewake ] && [ -f "$BRIDGE_STATE_DIR/mail" ] || exit 0
cat "$BRIDGE_STATE_DIR/mail" >&"${BRIDGE_REWAKE_FD:-2}" && rm -f "$BRIDGE_STATE_DIR/mail" && exit 2
exit 0
"""


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    detail: str


def check_typing(config: Config) -> CheckResult:
    """Types a line and its Enter into a throwaway tab running cat, the way bridge types into a lead's tab: the line
    shows twice (echo, then cat) only if both the text and the Enter took. The tab is closed whatever happens."""
    started = time.monotonic()
    if run_agterm(config, ["tree", "--json"]) is None:
        return CheckResult(False, "agterm does not answer `tree`")
    tree_s = time.monotonic() - started
    if tree_s >= AGTERM_SLOW_S:
        return CheckResult(False, f"agterm answered `tree` in {tree_s:.1f} s, too slow to type into tabs reliably")
    parent = os.environ.get("AGTERM_SESSION_ID")
    where = ["--parent", parent] if parent else ["--workspace-name", CHECK_TAB_NAME, "--create-workspace"]
    name = _name_check_tab(CHECK_TAB_NAME)
    session: str | None = None
    try:
        created = run_agterm(
            config, ["session", "new", *where, "--no-select", "--name", name, "--command", CAT_COMMAND, "--json"]
        )
        session = _map_created_to_id(created) or _find_session_named(config, name)
        result = (
            _type_and_see(config, session) if session else CheckResult(False, "agterm did not create the check tab")
        )
    finally:
        with _held_signals():
            closed = _close_check_tab(config, session, name)
    if not closed:
        return CheckResult(False, f"{result.detail}; but the check tab {name!r} did not close: close it by hand")
    return result


def check_rewake(config: Config, claude: str | None = None) -> CheckResult:
    """A throwaway Claude in a tab of its own, with only the installed waiter for hooks, gets a message through the
    waiter while idle and must answer it; nothing is typed but the trust dialog's answer for its own folder.

    Its folder is the same each run, so Claude Code keeps one trusted folder and one transcript folder for it.
    """
    return _run_in_check_claude(config, claude, "rewake-check", REWAKE_TAB_NAME, _wake_and_see)


def check_long_message(config: Config, claude: str | None = None) -> CheckResult:
    """A 3,000-character message from the user, typed the way bridge types it into a lead's tab, into a throwaway
    Claude: it must be submitted whole, the box empty after and the prompt in the transcript. Claude Code folds a long
    insert into a paste chip, which once left such messages unsent."""
    return _run_in_check_claude(config, claude, "long-check", LONG_TAB_NAME, _type_long_and_see)


def _run_in_check_claude(
    config: Config,
    claude: str | None,
    folder: str,
    tab_name: str,
    run: Callable[[Config, str, Path, Path], CheckResult],
) -> CheckResult:
    claude = claude or shutil.which("claude") or str(Path.home() / ".local/bin/claude")
    if not os.access(claude, os.X_OK):
        return CheckResult(False, f"no claude executable at {claude}")
    root = state_dir() / folder
    root.mkdir(parents=True, exist_ok=True)
    with _hold_check_lock(root) as only:
        if not only:
            return CheckResult(False, f"another {folder} is running")
        work, state = root / "work", root / "state"
        for stale in (work, state):
            shutil.rmtree(stale, ignore_errors=True)
            stale.mkdir()
        launcher = _write_check_files(root, work, state, claude)
        parent = os.environ.get("AGTERM_SESSION_ID")
        where = ["--parent", parent] if parent else ["--workspace-name", CHECK_TAB_NAME, "--create-workspace"]
        name = _name_check_tab(tab_name)
        session: str | None = None
        try:
            created = run_agterm(
                config,
                ["session", "new", *where, "--no-select", "--name", name, "--cwd", str(work), "--command",
                 str(launcher), "--json"],
            )  # fmt: skip
            session = _map_created_to_id(created) or _find_session_named(config, name)
            result = (
                run(config, session, work, state)
                if session
                else CheckResult(False, f"agterm did not create the {folder} tab")
            )
        finally:
            with _held_signals():
                closed = _close_check_tab(config, session, name)
                end_check_claude(state / CLAUDE_PID_FILE, settings_path=str(root / "settings.json"))
                shutil.rmtree(work, ignore_errors=True)
                shutil.rmtree(state, ignore_errors=True)
    if not closed:
        return CheckResult(False, f"{result.detail}; but the check tab {name!r} did not close: close it by hand")
    return result


def is_check_tab(name: str) -> bool:
    return CHECK_TAB_RE.fullmatch(name) is not None


def close_stale_check_tabs(config: Config, now: float) -> list[str]:
    """Check tabs a killed check left open, under any tab; one a running check holds is younger than the cut."""
    output = run_agterm(config, ["tree", "--json"])
    notes: list[str] = []
    for session, name in _list_named(_parse_json(output)):
        opened = CHECK_TAB_RE.fullmatch(name)
        if opened is None or now - int(opened.group(1)) < CHECK_TAB_STALE_S:
            continue
        closed = _close_check_tab(config, session, name)
        notes.append(f"{'closed' if closed else 'could not close'} the leftover check tab {name!r} ({session})")
    return notes


def _name_check_tab(prefix: str) -> str:
    # Unique per run, so a tab whose create reply was lost is found again by name and no older one is taken for it.
    return f"{prefix} {int(time.time())}-{secrets.token_hex(3)}"


def _close_check_tab(config: Config, session: str | None, name: str) -> bool:
    """True once no tab of that name is left. Under load a close can time out, or the create's reply be lost while
    agterm still made the tab; each try looks it up by name again."""
    for _ in range(CLOSE_TRIES):
        if session is None:
            tree = _parse_json(run_agterm(config, ["tree", "--json"]))
            if tree is None:
                continue
            session = next((found for found, named in _list_named(tree) if named == name), None)
            if session is None:
                return True
        if run_agterm(config, ["session", "close", "--target", session]) is not None:
            return True
        session = None
    return False


def end_check_claude(pid_file: Path, settings_path: str) -> None:
    """A tab closed while agterm is slow can leave its Claude running, detached; only the check's own is ended."""
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return
    args = process_args(pid)
    if args is None or settings_path not in args.argv:
        return
    with suppress(OSError):
        os.kill(pid, signal.SIGTERM)


@contextmanager
def _held_signals() -> Generator[None]:
    """A kill or Ctrl-C that lands while a check cleans up waits until it is done, then ends the run."""
    before = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGHUP, signal.SIGINT})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, before)


@contextmanager
def _hold_check_lock(root: Path) -> Generator[bool]:
    with (root / "check.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def _write_check_files(root: Path, work: Path, state: Path, claude: str) -> Path:
    stand_in = state / "bin" / "bridge-hook"
    stand_in.parent.mkdir(parents=True)
    stand_in.write_text(STAND_IN_HOOK)
    stand_in.chmod(0o755)
    waiter = install_waiter_script(state / "bin")
    groups = map_waiter_to_hook_groups(waiter, str(stand_in))
    settings = root / "settings.json"
    settings.write_text(json.dumps({"hooks": {event: [group] for event, group in groups.items()}}, indent=2))
    launcher = root / "run.sh"
    # No user or project settings: none of the user's hooks, bridge's included, run in it.
    launcher.write_text(
        "#!/bin/sh\n"
        f"cd {shlex.quote(str(work))} || exit 1\n"
        f"export BRIDGE_STATE_DIR={shlex.quote(str(state))}\n"
        "unset BRIDGE_CLAUDE_SESSIONS_DIR\n"
        f"echo $$ > {shlex.quote(str(state / CLAUDE_PID_FILE))}\n"
        f"exec {shlex.quote(claude)} --model haiku --setting-sources '' --settings {shlex.quote(str(settings))}"
        " --permission-mode default\n"
    )
    launcher.chmod(0o755)
    return launcher


def _wake_and_see(config: Config, session: str, work: Path, state: Path) -> CheckResult:
    started = time.monotonic()
    screen = _await_input_box(config, session, work, [])
    if screen is not None:
        return CheckResult(False, f"the check Claude showed no input box in {READY_WITHIN_S:.0f} s: {_tail(screen)}")
    ready_s = time.monotonic() - started
    pid = _await_waiter(state)
    if pid is None:
        screen = run_agterm(config, ["session", "text", "--target", session]) or ""
        return CheckResult(
            False,
            f"no rewake waiter started within {WAITER_WITHIN_S:.0f} s of Claude's input box, which showed in "
            f"{ready_s:.0f} s; {_describe_session_record(state)}; the tab shows: {_tail(screen)}",
        )
    word = "".join(secrets.choice(string.ascii_lowercase) for _ in range(12))
    (state / "mail").write_text(f"This is a bridge deploy check. Reply with only this word, in capitals: {word}\n")
    hints = state / "hints"
    hints.mkdir(exist_ok=True)
    (hints / str(pid)).write_text(f"{HINT_SENT}\n")
    sent = time.monotonic()
    screen = ""
    while time.monotonic() - sent < ANSWER_WITHIN_S:
        screen = run_agterm(config, ["session", "text", "--target", session]) or ""
        # Only the answer can hold the word in capitals, but a model sometimes answers it in lowercase: the mail's
        # own text never shows on screen, so either case proves the delivered message was read.
        if word.upper() in screen or _shows_answer(screen, word):
            return CheckResult(
                True,
                f"an idle Claude answered a message its waiter delivered in {time.monotonic() - sent:.1f} s "
                f"({time.monotonic() - started:.0f} s with its start)",
            )
        time.sleep(0.5)
    taken = "took the message" if not (state / "mail").exists() else "never took the message"
    return CheckResult(False, f"the waiter {taken}; no answer within {ANSWER_WITHIN_S:.0f} s: {_tail(screen)}")


def _await_waiter(state: Path) -> int | None:
    started = time.monotonic()
    while time.monotonic() - started < WAITER_WITHIN_S:
        for lock in (state / "rewake").glob("*.waiter"):
            stem = lock.name.removesuffix(".waiter")
            if stem.isdigit() and (lock / "pid").exists():
                return int(stem)
        time.sleep(0.2)
    return None


def _describe_session_record(state: Path) -> str:
    """The waiter leaves silently on a missing or non-cli record; a failure says which it would have read."""
    try:
        pid = int((state / CLAUDE_PID_FILE).read_text().strip())
    except (OSError, ValueError):
        return "no Claude pid"
    try:
        entrypoint = json.loads((Path.home() / ".claude/sessions" / f"{pid}.json").read_text()).get("entrypoint")
    except (OSError, ValueError, AttributeError):
        return f"Claude {pid} has no readable session record"
    return f"Claude {pid}'s session record says entrypoint {entrypoint!r}"


def _await_input_box(config: Config, session: str, work: Path, text_args: list[str]) -> str | None:
    """None once Claude Code shows its input box, empty or with its grey suggestion, and no dialog; else the last
    screen. The trust dialog naming the check's own folder is answered yes, once."""
    started = time.monotonic()
    answered = False
    screen = ""
    while map_screen_to_draft(screen) is None or map_screen_to_busy(screen):
        if time.monotonic() - started >= READY_WITHIN_S:
            return screen
        time.sleep(0.5)
        screen = run_agterm(config, ["session", "text", "--target", session, *text_args]) or ""
        if not answered:
            answered = _answer_trust(config, session, work, screen)
    return None


def _answer_trust(config: Config, session: str, work: Path, screen: str) -> bool:
    if TRUST_ANSWER not in screen or str(work) not in screen:
        return False
    run_agterm(config, ["session", "type", "--target", session, DOWN])
    time.sleep(0.5)
    chosen = run_agterm(config, ["session", "text", "--target", session]) or ""
    if any(line.strip().endswith(TRUST_ANSWER) and "\u276f" in line for line in chosen.splitlines()):
        run_agterm(config, ["session", "type", "--target", session, ENTER])
        return True
    return False


def _type_long_and_see(config: Config, session: str, work: Path, state: Path) -> CheckResult:
    pane = _find_pane(config, session)
    if pane is None:
        return CheckResult(False, "agterm shows no pane for the check tab")
    screen = _await_input_box(config, session, work, ["--pane-id", pane])
    if screen is not None:
        return CheckResult(False, f"the check Claude showed no input box in {READY_WITHIN_S:.0f} s: {_tail(screen)}")
    word = "".join(secrets.choice(string.ascii_lowercase) for _ in range(12))
    body = f"This is a bridge deploy check; reply with only the word {word}. " + "Filler to make it long. " * 120
    text = convert_user_message_to_typed_text("deploy-check", body[:LONG_MESSAGE_CHARS], 0)
    typed_at = time.monotonic()
    result = type_when_idle(
        config, session, pane, text, lambda: True, wait_s=int(READY_WITHIN_S),
        may_probe_box=lambda: not is_tab_selected(config, session),
    )  # fmt: skip
    if result != "typed":
        screen = run_agterm(config, ["session", "text", "--target", session, "--pane-id", pane]) or ""
        return CheckResult(False, f"a {len(text)}-char typed message ended {result!r}: {_tail(screen)}")
    screen = run_agterm(config, ["session", "text", "--target", session, "--pane-id", pane, "--lines", "80"]) or ""
    if map_screen_to_draft(screen) not in ("", None) and not map_screen_to_busy(screen):
        return CheckResult(False, f"typed, but the box still holds text: {_tail(screen)}")
    if word not in "".join(map_screen_to_transcript(screen).split()):
        return CheckResult(False, f"typed, but the prompt is not in the transcript: {_tail(screen)}")
    return CheckResult(
        True, f"a {len(text)}-char message from the user was typed and submitted in {time.monotonic() - typed_at:.1f} s"
    )


def _find_pane(config: Config, session: str) -> str | None:
    output = run_agterm(config, ["tree", "--json"])
    try:
        tree = json.loads(output or "")["result"]["tree"]
        return next(
            str(tab["surfaces"][0]["paneID"])
            for workspace in tree["workspaces"]
            for tab in workspace["sessions"]
            if tab.get("id") == session
        )
    except (ValueError, KeyError, TypeError, IndexError, StopIteration):
        return None


def _tail(screen: str) -> str:
    lines = [line.strip() for line in screen.splitlines() if line.strip()]
    return " | ".join(lines[-6:])[-400:] or "nothing"


def _type_and_see(config: Config, session: str) -> CheckResult:
    started = time.monotonic()
    if not _await_screen(config, session, lambda screen: CAT_READY in screen, CAT_STARTS_WITHIN_S):
        return CheckResult(False, f"the check tab did not start cat within {CAT_STARTS_WITHIN_S:.0f} s")
    cat_s = time.monotonic() - started
    token = f"bridge-check-{secrets.token_hex(4)}"
    started = time.monotonic()
    if run_agterm(config, ["session", "type", "--target", session, token]) is None:
        return CheckResult(False, "agterm refused the typed text")
    if run_agterm(config, ["session", "type", "--target", session, ENTER]) is None:
        return CheckResult(False, "agterm refused the Enter")
    if _await_screen(config, session, lambda screen: screen.count(token) >= 2, LANDS_WITHIN_S):
        return CheckResult(
            True, f"typed and submitted in {time.monotonic() - started:.1f} s (cat started in {cat_s:.1f} s)"
        )
    screen = run_agterm(config, ["session", "text", "--target", session]) or ""
    seen = "the text but not its Enter" if token in screen else "nothing"
    return CheckResult(False, f"after {LANDS_WITHIN_S:.0f} s the check tab shows {seen}")


def _await_screen(config: Config, session: str, shows: Callable[[str], bool], within_s: float) -> bool:
    started = time.monotonic()
    while time.monotonic() - started < within_s:
        if shows(run_agterm(config, ["session", "text", "--target", session]) or ""):
            return True
        time.sleep(0.3)
    return False


def _map_created_to_id(output: str | None) -> str | None:
    if not output:
        return None
    try:
        raw: object = json.loads(output)
    except ValueError:
        return None
    result = cast(dict[str, object], raw).get("result") if isinstance(raw, dict) else None
    session = cast(dict[str, object], result).get("id") if isinstance(result, dict) else None
    return session if isinstance(session, str) and session else None


def _find_session_named(config: Config, name: str) -> str | None:
    output = run_agterm(config, ["tree", "--json"])
    return next((session for session, named in _list_named(_parse_json(output)) if named == name), None)


def _parse_json(output: str | None) -> object:
    try:
        return json.loads(output) if output else None
    except ValueError:
        return None


def _list_named(node: object) -> list[tuple[str, str]]:
    """Each (id, name) anywhere in agterm's tree."""
    found: list[tuple[str, str]] = []
    if isinstance(node, list):
        for item in cast(list[object], node):
            found += _list_named(item)
    elif isinstance(node, dict):
        fields = cast(dict[str, object], node)
        session, name = fields.get("id"), fields.get("name")
        if isinstance(session, str) and isinstance(name, str):
            found.append((session, name))
        for value in fields.values():
            found += _list_named(value)
    return found


def _shows_answer(screen: str, word: str) -> bool:
    """The word alone on an answer line (⏺ word), as Claude Code prints a reply."""
    return any(line.strip().lstrip("⏺").strip().lower() == word for line in screen.splitlines())
