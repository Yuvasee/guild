import fcntl
import json
import os
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
REWAKE_TAB_NAME = "bridge rewake check"
LONG_TAB_NAME = "bridge long message check"
LONG_MESSAGE_CHARS = 3_000
CLAUDE_PID_FILE = "claude.pid"
WAITER_WITHIN_S = 45.0
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
    # A unique name finds the tab again when agterm made it but its reply was lost.
    name = f"{CHECK_TAB_NAME} {secrets.token_hex(3)}"
    created = run_agterm(
        config, ["session", "new", *where, "--no-select", "--name", name, "--command", "/bin/cat", "--json"]
    )
    session = _map_created_to_id(created) or _find_session_named(config, name)
    if session is None:
        return CheckResult(False, "agterm did not create the check tab")
    try:
        result = _type_and_see(config, session)
    finally:
        closed = run_agterm(config, ["session", "close", "--target", session]) is not None
    if not closed:
        return CheckResult(False, f"{result.detail}; but the check tab {session} did not close: close it by hand")
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
        name = f"{tab_name} {secrets.token_hex(3)}"
        created = run_agterm(
            config,
            ["session", "new", *where, "--no-select", "--name", name, "--cwd", str(work), "--command", str(launcher),
             "--json"],
        )  # fmt: skip
        session = _map_created_to_id(created) or _find_session_named(config, name)
        if session is None:
            return CheckResult(False, f"agterm did not create the {folder} tab")
        try:
            result = run(config, session, work, state)
        finally:
            closed = run_agterm(config, ["session", "close", "--target", session]) is not None
            end_check_claude(state / CLAUDE_PID_FILE, settings_path=str(root / "settings.json"))
            shutil.rmtree(work, ignore_errors=True)
            shutil.rmtree(state, ignore_errors=True)
    if not closed:
        return CheckResult(False, f"{result.detail}; but the check tab {session} did not close: close it by hand")
    return result


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
    pid = _await_waiter(config, session, work, state)
    if pid is None:
        screen = run_agterm(config, ["session", "text", "--target", session]) or ""
        return CheckResult(
            False, f"no rewake waiter started within {WAITER_WITHIN_S:.0f} s; the tab shows: {_tail(screen)}"
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


def _await_waiter(config: Config, session: str, work: Path, state: Path) -> int | None:
    """The waiter's pid once it listens; the trust dialog naming the check's own folder is answered yes, once."""
    answered = False
    started = time.monotonic()
    while time.monotonic() - started < WAITER_WITHIN_S:
        for lock in (state / "rewake").glob("*.waiter"):
            stem = lock.name.removesuffix(".waiter")
            if stem.isdigit() and (lock / "pid").exists():
                return int(stem)
        screen = run_agterm(config, ["session", "text", "--target", session]) or ""
        if not answered:
            answered = _answer_trust(config, session, work, screen)
        time.sleep(0.5)
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
    started = time.monotonic()
    answered = False
    screen = ""
    # Ready once Claude Code shows its input box, empty or with its grey suggestion, and no dialog.
    while map_screen_to_draft(screen) is None or map_screen_to_busy(screen):
        if time.monotonic() - started >= WAITER_WITHIN_S:
            return CheckResult(
                False, f"the check Claude showed no input box in {WAITER_WITHIN_S:.0f} s: {_tail(screen)}"
            )
        time.sleep(0.5)
        screen = run_agterm(config, ["session", "text", "--target", session, "--pane-id", pane]) or ""
        if not answered:
            answered = _answer_trust(config, session, work, screen)
    word = "".join(secrets.choice(string.ascii_lowercase) for _ in range(12))
    body = f"This is a bridge deploy check; reply with only the word {word}. " + "Filler to make it long. " * 120
    text = convert_user_message_to_typed_text("deploy-check", body[:LONG_MESSAGE_CHARS], 0)
    typed_at = time.monotonic()
    result = type_when_idle(
        config, session, pane, text, lambda: True, wait_s=int(WAITER_WITHIN_S),
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
    token = f"bridge-check-{secrets.token_hex(4)}"
    started = time.monotonic()
    if run_agterm(config, ["session", "type", "--target", session, token]) is None:
        return CheckResult(False, "agterm refused the typed text")
    if run_agterm(config, ["session", "type", "--target", session, ENTER]) is None:
        return CheckResult(False, "agterm refused the Enter")
    while time.monotonic() - started < LANDS_WITHIN_S:
        screen = run_agterm(config, ["session", "text", "--target", session]) or ""
        if screen.count(token) >= 2:
            return CheckResult(True, f"typed and submitted in {time.monotonic() - started:.1f} s")
        time.sleep(0.3)
    screen = run_agterm(config, ["session", "text", "--target", session]) or ""
    seen = "the text but not its Enter" if token in screen else "nothing"
    return CheckResult(False, f"after {LANDS_WITHIN_S:.0f} s the check tab shows {seen}")


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
    if not output:
        return None
    try:
        raw: object = json.loads(output)
    except ValueError:
        return None
    return _find_named(raw, name)


def _find_named(node: object, name: str) -> str | None:
    if isinstance(node, list):
        for item in cast(list[object], node):
            if (found := _find_named(item, name)) is not None:
                return found
    elif isinstance(node, dict):
        fields = cast(dict[str, object], node)
        session = fields.get("id")
        if fields.get("name") == name and isinstance(session, str):
            return session
        for value in fields.values():
            if (found := _find_named(value, name)) is not None:
                return found
    return None


def _shows_answer(screen: str, word: str) -> bool:
    """The word alone on an answer line (⏺ word), as Claude Code prints a reply."""
    return any(line.strip().lstrip("⏺").strip().lower() == word for line in screen.splitlines())
