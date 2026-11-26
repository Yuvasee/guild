import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from conftest import BridgeEnv, SpawnProcess
from test_leads import Team
from test_typing import NBSP, PROMPT, RULE, FakeClaudeBox

from bridge.board import USER_CONVERSATION, Party, Recipient, send_message
from bridge.compaction import TrailingFailure, read_trailing_failure
from bridge.config import Config
from bridge.feed import (
    TURN_CUT,
    UndeliveredTab,
    convert_screen_to_hold_reason,
    deliver_typed,
    record_failed_delivery,
    take_new_undelivered_tabs,
    take_oldest_typable,
    tell_assistant_of_draft,
    tell_of_undelivered_tab,
)
from bridge.hooks import HOOK_EVENTS, handle_hook
from bridge.tabinput import TypeResult, type_when_idle
from bridge.turncut import Inspected, sweep_turn_cuts

SLEEP_TEXT = "API Error: Your computer went to sleep mid-response. The response above may be incomplete."
BASE_NS = 1_800_000_000_000 * 1_000_000
CONFIG = Config(assistant_handle="old-b")


def _at(t_s: int) -> str:
    """A transcript timestamp t_s seconds after the base the hooks here run at."""
    return datetime.fromtimestamp(BASE_NS // 1_000_000_000 + t_s, UTC).isoformat()


def _answer(t_s: int) -> dict[str, object]:
    at = _at(t_s)
    usage = {"input_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 5}
    message = {"model": "claude-opus-5-5", "role": "assistant", "content": [{"type": "text", "text": "on it"}]}
    return {"type": "assistant", "timestamp": at, "isSidechain": False, "message": {**message, "usage": usage}}


def _failure(t_s: int, text: str = SLEEP_TEXT, error: str = "server_error") -> dict[str, object]:
    at = _at(t_s)
    message = {"model": "<synthetic>", "role": "assistant", "content": [{"type": "text", "text": text}]}
    usage = {"input_tokens": 0, "output_tokens": 0}
    entry = {"type": "assistant", "timestamp": at, "isApiErrorMessage": True, "error": error, "isSidechain": False}
    return {**entry, "message": {**message, "stop_reason": "stop_sequence", "usage": usage}}


def _write_transcript(path: Path, *entries: dict[str, object]) -> str:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return str(path)


class Cut:
    """Lead alex in tab A, the assistant old-b in tab B; hooks run in this process and record what they would type."""

    def __init__(self, bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
        self.team = Team(bridge_env, spawn_process)
        assert self.team.enroll().returncode == 0
        self.env = bridge_env
        self.transcript = bridge_env.root / "conv-a.jsonl"
        self.spawned: list[tuple[int, str]] = []

    def hook(self, event: str, t_s: int, config: Config = CONFIG, **payload: object) -> None:
        env = {"CLAUDE_PID": str(self.team.a), "AGTERM_PANE_ID": "PANE-A", "AGTERM_SESSION_ID": "TAB-A"}
        base = {"session_id": "conv-a", "cwd": str(self.env.root), "transcript_path": str(self.transcript)}

        def spawn(message_id: int, recipients: list[Recipient]) -> None:
            self.spawned += [(message_id, r.handle) for r in recipients]

        with self.env.connect() as conn, mock.patch("bridge.hooks.spawn_typed_delivery", spawn):
            handle_hook(conn, event, {**base, **payload}, env, config, BASE_NS + t_s * 1_000_000_000)

    def fail(self, t_s: int, text: str = SLEEP_TEXT, error: str = "server_error") -> None:
        self.hook("StopFailure", t_s, error=error, last_assistant_message=text)

    def messages(self, post_type: str | None = None) -> list[sqlite3.Row]:
        with self.env.connect() as conn:
            return conn.execute(
                "SELECT m.id, m.body, m.post_type, m.cancelled_at, r.handle FROM messages m"
                " JOIN message_recipients r ON r.message_id = m.id"
                " WHERE m.from_handle = 'bridge' AND (? IS NULL OR m.post_type = ?) ORDER BY m.id",
                (post_type, post_type),
            ).fetchall()


@pytest.fixture
def cut(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> Cut:
    return Cut(bridge_env, spawn_process)


def test_stop_failure_is_a_hook_bridge_installs() -> None:
    assert "StopFailure" in HOOK_EVENTS


def test_a_turn_cut_by_sleep_is_resumed_once_and_the_tab_shows_idle(cut: Cut) -> None:
    _write_transcript(cut.transcript, _answer(-300), _failure(-10))
    cut.hook("UserPromptSubmit", 0, prompt="go on", prompt_id="p1")
    cut.fail(5)

    resumes = cut.messages(TURN_CUT)
    with cut.env.connect() as conn:
        status = conn.execute("SELECT status FROM instances WHERE pid = ?", (cut.team.a,)).fetchone()["status"]
    assert [r["handle"] for r in resumes] == ["alex"]
    assert resumes[0]["body"].startswith("[bridge] Your last turn was cut by Mac sleep at ")
    assert resumes[0]["body"].endswith("Read bridge inbox and continue where you left off.")
    assert cut.spawned == [(resumes[0]["id"], "alex")]
    assert status == "idle"


def test_any_other_api_error_is_resumed_too_and_named(cut: Cut) -> None:
    rate = "API Error: Rate limit reached"
    _write_transcript(cut.transcript, _answer(-300), _failure(-10, rate))
    cut.fail(5, rate, "rate_limit")

    assert [r["body"].split(" at ")[0] for r in cut.messages(TURN_CUT)] == [
        "[bridge] Your last turn was cut by an API error (rate_limit)"
    ]


def test_a_resumed_turn_that_fails_again_is_told_to_the_assistant_once_and_never_typed_again(cut: Cut) -> None:
    _write_transcript(cut.transcript, _answer(-300), _failure(-10))
    cut.fail(5)
    # The resume went in and its turn failed at once: no real answer since.
    _write_transcript(cut.transcript, _answer(-300), _failure(-10), _failure(30))
    cut.fail(60)
    cut.fail(120)

    resumes, alerts = cut.messages(TURN_CUT), cut.messages("to-assistant")
    assert len(resumes) == 1 and resumes[0]["cancelled_at"] is not None
    assert [a["handle"] for a in alerts] == ["old-b"]
    assert "was cut again by Mac sleep" in alerts[0]["body"]
    assert f"after bridge resumed it once (#{resumes[0]['id']}" in alerts[0]["body"]
    assert "agtermctl session text --target TAB-A --pane-id PANE-A" in alerts[0]["body"]
    assert [handle for _, handle in cut.spawned] == ["alex", "old-b"]


def test_a_cut_after_the_resumed_turn_answered_is_a_new_cut_and_resumed_again(cut: Cut) -> None:
    _write_transcript(cut.transcript, _answer(-300), _failure(-10))
    cut.fail(5)
    _write_transcript(cut.transcript, _answer(30), _failure(40))
    cut.fail(60)

    assert len(cut.messages(TURN_CUT)) == 2
    assert cut.messages("to-assistant") == []


def test_a_tab_that_is_neither_a_lead_nor_a_worker_is_not_resumed(cut: Cut) -> None:
    with cut.env.connect() as conn:
        conn.execute("UPDATE leads SET retired_at = 1")
    _write_transcript(cut.transcript, _failure(-10))
    cut.fail(5)

    assert cut.messages(TURN_CUT) == []


def test_a_worker_tab_is_resumed(cut: Cut) -> None:
    with cut.env.connect() as conn:
        conn.execute("UPDATE leads SET retired_at = 1")
        conn.execute("INSERT INTO tab_parents (agterm_session, parent_session) VALUES ('TAB-A', 'TAB-B')")
    _write_transcript(cut.transcript, _failure(-10))
    cut.fail(5)

    assert len(cut.messages(TURN_CUT)) == 1


def test_a_context_over_its_window_is_left_to_compaction(cut: Cut) -> None:
    too_long = "Prompt is too long"
    _write_transcript(cut.transcript, _failure(-10, too_long, "invalid_request"))
    cut.fail(5, too_long, "invalid_request")

    assert cut.messages(TURN_CUT) == []


def test_a_cut_after_a_compaction_since_the_resume_is_a_new_cut(cut: Cut) -> None:
    _write_transcript(cut.transcript, _failure(-10))
    cut.fail(5)
    cut.fail(60)
    resume = cut.messages(TURN_CUT)[0]
    with cut.env.connect() as conn:
        conn.execute("UPDATE conversations SET compacted_at = ? WHERE conversation_id = 'conv-a'", (2**62,))
    # The transcript after the compaction holds no answer yet: only its boundary and the new failure.
    _write_transcript(cut.transcript, {"type": "system", "subtype": "compact_boundary"}, _failure(100))
    cut.fail(120)

    assert resume["cancelled_at"] is not None
    assert len(cut.messages(TURN_CUT)) == 2


def test_a_cut_turn_over_its_window_is_recorded_for_compaction_at_once(cut: Cut) -> None:
    too_long = "Prompt is too long"
    _write_transcript(cut.transcript, _failure(-10, too_long, "invalid_request"))
    cut.hook("StopFailure", 5, Config(assistant_handle="old-b", compact_at_tokens=600_000), error="invalid_request",
             last_assistant_message=too_long)  # fmt: skip

    with cut.env.connect() as conn:
        why = conn.execute("SELECT context_dead_why FROM conversations WHERE conversation_id = 'conv-a'").fetchone()
    assert why["context_dead_why"] == "too-long"
    assert cut.messages(TURN_CUT) == []


def test_a_compaction_request_the_cut_turn_never_answered_no_longer_reads_as_compacting(cut: Cut) -> None:
    with cut.env.connect() as conn:
        conn.execute("UPDATE conversations SET compact_phase = 'requested', compact_warned_at = 690000")
    _write_transcript(cut.transcript, _failure(-10))
    cut.fail(5)
    resume = cut.messages(TURN_CUT)[0]
    with cut.env.connect() as conn:
        phase = conn.execute(
            "SELECT compact_phase, compact_warned_at FROM conversations WHERE conversation_id = 'conv-a'"
        )
        assert tuple(phase.fetchone()) == (None, None)
        conn.execute("UPDATE messages SET created_at = 1000 WHERE id = ?", (resume["id"],))
        conn.execute("UPDATE message_recipients SET typing_wanted_at = 1000 WHERE message_id = ?", (resume["id"],))
        tabs = take_new_undelivered_tabs(conn, CONFIG, 1000 + 6 * 60_000)

    assert [(t.compacting, t.cut is not None) for t in tabs] == [(False, True)]
    reason = convert_screen_to_hold_reason(tabs[0], _screen("Ok, push it"))
    assert reason is not None and "compacting" not in reason


def _screen(box: str) -> str:
    return "\n".join([RULE, PROMPT + NBSP + box, RULE, "  -- INSERT --"])


def _cut_tab(draft_proven: bool) -> UndeliveredTab:
    return UndeliveredTab("c", "alex", "TAB-A", "PANE-A", None, 7, 0, 1, None, False, False, draft_proven,
                          cut="cut by Mac sleep at 09:50")  # fmt: skip


def test_a_resume_held_by_text_bridge_could_not_check_is_still_named_with_its_cause() -> None:
    reason = convert_screen_to_hold_reason(_cut_tab(False), _screen("Ok, push it"))

    assert reason is not None
    assert reason.startswith("Its last turn was cut by Mac sleep at 09:50, and bridge's resume has not gone in.")
    assert "grey suggestion or someone's draft" in reason


def test_a_resume_held_by_a_proven_draft_names_the_draft_and_the_cut() -> None:
    reason = convert_screen_to_hold_reason(_cut_tab(True), _screen("my own words"))

    assert reason is not None and "cut by Mac sleep at 09:50" in reason and "'my own words'" in reason


def test_a_cut_workers_waiting_resume_reaches_the_assistant_in_five_minutes(cut: Cut) -> None:
    with cut.env.connect() as conn:
        conn.execute("UPDATE leads SET retired_at = 1")
        conn.execute("INSERT INTO tab_parents (agterm_session, parent_session) VALUES ('TAB-A', 'TAB-B')")
    _write_transcript(cut.transcript, _failure(-10))
    cut.fail(5)
    resume = cut.messages(TURN_CUT)[0]
    with cut.env.connect() as conn:
        conn.execute("UPDATE messages SET created_at = 1000 WHERE id = ?", (resume["id"],))
        conn.execute("UPDATE message_recipients SET typing_wanted_at = 1000 WHERE message_id = ?", (resume["id"],))
        early = take_new_undelivered_tabs(conn, CONFIG, 1000 + 4 * 60_000)
        tabs = take_new_undelivered_tabs(conn, CONFIG, 1000 + 6 * 60_000)
        told = tell_of_undelivered_tab(conn, CONFIG, tabs[0], _screen("Ok, push it"), 1000 + 6 * 60_000)
        body = conn.execute("SELECT body FROM messages WHERE id = ?", (told[0] if told else 0,)).fetchone()

    assert early == []
    assert told is not None and told[1].handle == "old-b"
    assert "Its last turn was cut by Mac sleep at " in body["body"]


def _type_briefly(*args: Any, **kw: Any) -> TypeResult:
    return type_when_idle(*args, **{**kw, "wait_s": 6})


def _deliver_resume(cut: Cut, fake: FakeClaudeBox) -> str | None:
    _write_transcript(cut.transcript, _failure(-10))
    cut.hook("UserPromptSubmit", 0, prompt="go on", prompt_id="p1")
    cut.fail(5)
    message_id = cut.messages(TURN_CUT)[0]["id"]
    with (
        cut.env.connect() as conn,
        mock.patch("bridge.tabinput.run_agterm", fake.run),
        mock.patch("bridge.feed.is_tab_selected", lambda *_: False),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        mock.patch("bridge.feed.type_when_idle", _type_briefly),
    ):
        return deliver_typed(conn, Config(), message_id, "conv-a")


def test_after_a_cut_the_grey_suggestion_is_probed_and_the_resume_typed_over_it(cut: Cut) -> None:
    fake = FakeClaudeBox({"TAB-A": "Ok, push it"}, suggestions={"TAB-A"})

    result = _deliver_resume(cut, fake)

    assert result == "typed"
    assert fake.typed[:2] == [".", "\x7f"]
    assert [s.split(" at ")[0] for s in fake.submitted] == ["[bridge] Your last turn was cut by Mac sleep"]


def test_after_a_cut_a_real_draft_gets_no_typing(cut: Cut) -> None:
    fake = FakeClaudeBox({"TAB-A": "my own words"})

    result = _deliver_resume(cut, fake)

    assert result == "draft"
    assert fake.typed == [".", "\x7f"]
    assert fake.boxes["TAB-A"] == "my own words"


def _prompt(t_s: int, text: str = "go on") -> dict[str, object]:
    return {"type": "user", "timestamp": _at(t_s), "isSidechain": False, "message": {"role": "user", "content": text}}


def _write_idle_transcript(cut: Cut, written_s: int, *entries: dict[str, object]) -> None:
    _write_transcript(cut.transcript, *entries)
    at = BASE_NS // 1_000_000_000 + written_s
    os.utime(cut.transcript, (at, at))


def _sweep(cut: Cut, t_s: int, inspected: Inspected | None = None) -> list[str]:
    with cut.env.connect() as conn:
        sent = sweep_turn_cuts(conn, CONFIG, BASE_NS // 1_000_000 + t_s * 1000, {} if inspected is None else inspected)
    return [r.handle for _, r in sent]


def _cut_unseen_by_hooks(cut: Cut) -> None:
    cut.hook("UserPromptSubmit", 0, prompt="go on", prompt_id="p1")
    after: dict[str, object] = {"type": "system", "timestamp": _at(11), "isSidechain": False, "content": "hook"}
    _write_idle_transcript(cut, 11, _answer(-300), _prompt(0), _failure(10), after)


def test_a_tab_idle_on_a_cut_no_hook_took_is_told_to_the_assistant_once_and_not_resumed(cut: Cut) -> None:
    _cut_unseen_by_hooks(cut)
    inspected: Inspected = {}

    early = _sweep(cut, 60, inspected)
    first = _sweep(cut, 200, inspected)
    again = _sweep(cut, 400, inspected)
    after_restart = _sweep(cut, 600)

    alerts = cut.messages("to-assistant")
    assert (early, first, again, after_restart) == ([], ["old-b"], [], [])
    assert cut.messages(TURN_CUT) == [] and len(alerts) == 1
    clock = datetime.fromtimestamp(BASE_NS // 1_000_000_000 + 10).astimezone().strftime("%H:%M")
    assert alerts[0]["body"].startswith(f"alex's tab (Old A) has been idle since Mac sleep cut its turn at {clock},")
    assert "agtermctl session text --target TAB-A --pane-id PANE-A" in alerts[0]["body"]
    assert 'bridge send alex "continue where you left off"' in alerts[0]["body"]


def test_a_hook_that_takes_the_cut_after_the_sweep_resumes_it_as_a_first_cut(cut: Cut) -> None:
    _cut_unseen_by_hooks(cut)
    _sweep(cut, 200)
    cut.fail(210)

    resumes = cut.messages(TURN_CUT)
    assert len(resumes) == 1 and resumes[0]["cancelled_at"] is None
    assert len(cut.messages("to-assistant")) == 1
    assert _sweep(cut, 400) == []


def test_the_sweep_leaves_a_cut_a_hook_already_resumed(cut: Cut) -> None:
    cut.hook("UserPromptSubmit", 0, prompt="go on", prompt_id="p1")
    _write_idle_transcript(cut, 10, _answer(-300), _prompt(0), _failure(10))
    cut.fail(11)

    assert _sweep(cut, 300) == []
    assert len(cut.messages()) == 1


@pytest.mark.parametrize(
    "entries",
    [
        pytest.param((_answer(-300), _prompt(0), _answer(10)), id="last-turn-answered"),
        pytest.param((_answer(-300), _failure(10), _prompt(20, "try again")), id="prompted-after-the-cut"),
    ],
)
def test_the_sweep_leaves_a_tab_not_idle_on_a_cut(cut: Cut, entries: tuple[dict[str, object], ...]) -> None:
    cut.hook("UserPromptSubmit", 0, prompt="go on", prompt_id="p1")
    _write_idle_transcript(cut, 20, *entries)

    assert _sweep(cut, 600) == []
    assert cut.messages() == []


def _count_reads(reads: list[Path]) -> Any:
    def read(path: Path) -> TrailingFailure | None:
        reads.append(path)
        return read_trailing_failure(path)

    return mock.patch("bridge.turncut.read_trailing_failure", read)


def test_the_sweep_reads_an_unchanged_transcript_once_and_never_one_of_a_tab_it_could_not_resume(cut: Cut) -> None:
    cut.hook("UserPromptSubmit", 0, prompt="go on", prompt_id="p1")
    _write_idle_transcript(cut, 20, _answer(-300), _prompt(0), _answer(10))
    inspected: Inspected = {}
    reads: list[Path] = []

    with _count_reads(reads):
        for t_s in (200, 400, 600):
            _sweep(cut, t_s, inspected)
        answered = len(reads)
        with cut.env.connect() as conn:
            conn.execute("UPDATE leads SET retired_at = 1")
        _write_idle_transcript(cut, 30, _answer(-300), _prompt(0), _failure(10))
        told = _sweep(cut, 800, inspected)

    assert (answered, len(reads), told) == (1, 1, [])


def test_a_transcript_written_during_the_sweep_is_not_told_of(cut: Cut) -> None:
    _cut_unseen_by_hooks(cut)
    inspected: Inspected = {}

    def read_then_prompt(path: Path) -> TrailingFailure | None:
        failure = read_trailing_failure(path)
        with path.open("a") as f:
            f.write(json.dumps(_prompt(190, "still there?")) + "\n")
        return failure

    with mock.patch("bridge.turncut.read_trailing_failure", read_then_prompt):
        during = _sweep(cut, 200, inspected)
    after = _sweep(cut, 400, inspected)

    assert (during, after) == ([], [])
    assert cut.messages() == []


def _cut_resumed_by_hook(cut: Cut, older_mail: bool = False) -> int:
    cut.hook("UserPromptSubmit", 0, prompt="go on", prompt_id="p1")
    if older_mail:
        alex = Recipient("alex", "conv-a", cut.team.a, "idle", None, False)
        with cut.env.connect() as conn:
            send_message(conn, Party(USER_CONVERSATION, "dana"), [alex], "info", "older", BASE_NS // 1_000_000 + 1)
    _write_idle_transcript(cut, 10, _answer(-300), _prompt(0), _failure(10))
    cut.fail(11)
    return int(cut.messages(TURN_CUT)[0]["id"])


def _fail_typing(cut: Cut, message_id: int, t_s: int) -> None:
    with cut.env.connect() as conn:
        record_failed_delivery(conn, message_id, "conv-a", BASE_NS // 1_000_000 + t_s * 1000)


def test_a_cut_whose_resume_could_not_be_typed_is_told_to_the_assistant_once(cut: Cut) -> None:
    resume = _cut_resumed_by_hook(cut)
    inspected: Inspected = {}
    waiting = _sweep(cut, 200, inspected)
    _fail_typing(cut, resume, 900)

    told = [_sweep(cut, 1000, inspected), _sweep(cut, 1200, inspected), _sweep(cut, 1400)]

    alerts = cut.messages("to-assistant")
    assert (waiting, told) == ([], [["old-b"], [], []])
    assert len(alerts) == 1
    assert f", and bridge's resume #{resume} could not be typed there." in alerts[0]["body"]
    assert "agtermctl session text --target TAB-A --pane-id PANE-A" in alerts[0]["body"]
    assert "no StopFailure hook" not in alerts[0]["body"]


def test_a_resume_typed_on_a_retry_after_failing_still_takes_the_cut(cut: Cut) -> None:
    resume = _cut_resumed_by_hook(cut)
    _fail_typing(cut, resume, 900)
    with cut.env.connect() as conn:
        conn.execute("UPDATE message_recipients SET offered_at = ?, acked_at = ? WHERE message_id = ?", (1, 1, resume))

    assert _sweep(cut, 1000) == []
    assert cut.messages("to-assistant") == []


def test_a_resume_waiting_in_a_batch_behind_mail_that_could_not_be_typed_still_takes_the_cut(cut: Cut) -> None:
    resume = _cut_resumed_by_hook(cut, older_mail=True)
    with cut.env.connect() as conn:
        typed = take_oldest_typable(conn, Config(batch_count=10, batch_age_min=5), resume, "conv-a")
    _fail_typing(cut, typed, 900)

    assert typed < resume
    assert _sweep(cut, 1000) == []
    assert cut.messages("to-assistant") == []


def _ms(t_s: int) -> int:
    return BASE_NS // 1_000_000 + t_s * 1000


def _want_typed(cut: Cut, message_id: int) -> None:
    """As its typer marks it, so the undelivered alert counts it."""
    with cut.env.connect() as conn:
        conn.execute("UPDATE message_recipients SET typing_wanted_at = 1 WHERE message_id = ?", (message_id,))


def _undelivered_alerts(cut: Cut, t_s: int, counted_from: int = 0) -> int:
    """How many alerts maintenance's undelivered watch sends at t_s."""
    with cut.env.connect() as conn:
        tabs = take_new_undelivered_tabs(conn, CONFIG, _ms(t_s), counted_from)
        return sum(tell_of_undelivered_tab(conn, CONFIG, tab, _screen(""), _ms(t_s)) is not None for tab in tabs)


def test_a_failed_resume_the_undelivered_alert_told_of_gets_no_second_alert_even_after_a_wake(cut: Cut) -> None:
    resume = _cut_resumed_by_hook(cut)
    _want_typed(cut, resume)
    undelivered = _undelivered_alerts(cut, 400)
    _fail_typing(cut, resume, 410)
    inspected: Inspected = {}
    swept = _sweep(cut, 600, inspected)
    # A wake moves the count on: nothing is overdue, so the undelivered episode and its record go.
    after_wake = _undelivered_alerts(cut, 700, counted_from=_ms(700))
    with cut.env.connect() as conn:
        episodes = conn.execute("SELECT count(*) FROM undelivered_tabs").fetchone()[0]

    assert (undelivered, swept, after_wake, episodes) == (1, [], 0, 0)
    assert _sweep(cut, 800, inspected) == [] and _sweep(cut, 1000) == []
    assert len(cut.messages("to-assistant")) == 1


def test_a_failed_resume_the_sweep_told_of_gets_no_undelivered_alert(cut: Cut) -> None:
    resume = _cut_resumed_by_hook(cut)
    _want_typed(cut, resume)
    _fail_typing(cut, resume, 60)

    assert _sweep(cut, 200) == ["old-b"]
    assert _undelivered_alerts(cut, 400) == 0 and _undelivered_alerts(cut, 1000) == 0
    assert len(cut.messages("to-assistant")) == 1


def test_an_undelivered_alert_of_older_mail_does_not_stand_for_the_resume(cut: Cut) -> None:
    resume = _cut_resumed_by_hook(cut, older_mail=True)
    with cut.env.connect() as conn:
        older = int(conn.execute("SELECT id FROM messages WHERE body = 'older'").fetchone()[0])
        conn.execute("UPDATE messages SET created_at = ? WHERE id = ?", (_ms(-600), older))
    _want_typed(cut, older)
    _want_typed(cut, resume)
    told_of_older = _undelivered_alerts(cut, 100)
    _fail_typing(cut, resume, 200)

    assert told_of_older == 1
    assert _sweep(cut, 400) == ["old-b"]
    assert _undelivered_alerts(cut, 500) == 0
    alerts = cut.messages("to-assistant")
    assert len(alerts) == 2 and f"resume #{resume} could not be typed" in alerts[1]["body"]


def test_a_draft_alert_that_reached_nobody_does_not_stand_for_the_resume(cut: Cut) -> None:
    resume = _cut_resumed_by_hook(cut)
    with cut.env.connect() as conn:
        draft = tell_assistant_of_draft(conn, Config(assistant_handle="away"), resume, "conv-a", _ms(100))
    _fail_typing(cut, resume, 200)

    assert draft is None
    assert _sweep(cut, 400) == ["old-b"]
