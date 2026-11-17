import argparse
import time
from pathlib import Path

import pytest
from test_review_doc_check import make_top
from test_reviews import setup_pr

from bridge.classifier import ModelAnswer
from bridge.cli_guild import cmd_review_doc
from bridge.config import Config
from bridge.doc_screen import SCREENS, Ask, screen_doc, screen_doc_for_warnings
from bridge.review_worktree import read_registered_doc

HISTORY = "The first version of this change kept the list in a file instead."
PLAIN = "Members now see the button greyed out when they may not use it."
LONG = (
    "The worker reads the share first and, when the run is sampled out, queues only the step that stores the summary."
)
DOC = make_top(f"<p>{HISTORY} {PLAIN}</p><ul><li>Short.</li></ul>", f"<p>{LONG}</p>")
CONFIG = Config(doc_screen=True, shadow_filter_key_file="~/key")


def answer(**nouls: float) -> ModelAnswer:
    return ModelAnswer(
        answers={key: {"type": "noul", "noul": value} for key, value in nouls.items()}, error=None, latency_ms=5
    )


def stub(replies: dict[str, ModelAnswer]) -> tuple[Ask, dict[str, list[str]]]:
    asked: dict[str, list[str]] = {}

    def ask(sentence: str, questions: dict[str, object]) -> ModelAnswer:
        asked[sentence] = sorted(questions)
        return replies.get(sentence, answer(**{screen.key: 0.1 for screen in SCREENS}))

    return ask, asked


def test_a_sentence_over_a_screens_threshold_goes_back_quoted_with_the_reason() -> None:
    ask, asked = stub({HISTORY: answer(history=0.9, process=0.1)})
    flags, error = screen_doc(DOC, ask)
    assert error is None and [(flag.sentence, flag.reasons) for flag in flags] == [
        (HISTORY, ("tells how the PR evolved, not what it is",))
    ]
    assert set(asked) == {HISTORY, PLAIN, LONG}, "labels under five words ('Short.') are not asked about"
    assert asked[PLAIN] == ["history", "process"] and asked[LONG] == ["clauses", "history", "process"], (
        "the clause count is asked only of sentences of 20 words or more"
    )
    (warning,) = screen_doc_for_warnings(DOC, CONFIG, ask)
    assert warning == (
        f'a sentence above the folds tells how the PR evolved, not what it is: "{HISTORY}" (rewrite, move or fold '
        "it; this is a warning, not a refusal)"
    )


def test_a_plain_doc_gets_no_warning() -> None:
    ask, _ = stub({})
    assert screen_doc_for_warnings(DOC, CONFIG, ask) == []


def test_a_failed_call_says_the_screen_did_not_run_and_gives_no_partial_verdict() -> None:
    ask, _ = stub({HISTORY: answer(history=0.9, process=0.1), LONG: ModelAnswer(None, "timeout", 10_000)})
    assert screen_doc_for_warnings(DOC, CONFIG, ask) == [
        "the plain-doc screen did not run (timeout); the doc is registered all the same"
    ]
    ask, _ = stub({PLAIN: answer(history=0.1)})
    assert screen_doc_for_warnings(DOC, CONFIG, ask) == [
        "the plain-doc screen did not run (no answer to 'process'); the doc is registered all the same"
    ]

    def broken(sentence: str, questions: dict[str, object]) -> ModelAnswer:
        raise RuntimeError("boom")

    assert screen_doc_for_warnings(DOC, CONFIG, broken) == [
        "the plain-doc screen did not run (RuntimeError); the doc is registered all the same"
    ]


def test_the_screen_is_off_unless_turned_on_with_a_key_file() -> None:
    ask, asked = stub({HISTORY: answer(history=0.9, process=0.1)})
    assert screen_doc_for_warnings(DOC, Config(doc_screen=True), ask) == []
    assert screen_doc_for_warnings(DOC, Config(shadow_filter_key_file="~/key"), ask) == []
    assert asked == {}


def test_a_sentence_quoting_a_customer_is_never_sent() -> None:
    quoted = make_top("<p>The customer said the export never finished for them.</p>")
    ask, asked = stub({})
    assert screen_doc_for_warnings(quoted, CONFIG, ask) == [] and asked == {}


def test_a_screen_over_its_deadline_did_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bridge.doc_screen.DEADLINE_S", 0.05)

    def slow(sentence: str, questions: dict[str, object]) -> ModelAnswer:
        time.sleep(0.2)
        return answer(history=0.1, process=0.1, clauses=0.1)

    assert screen_doc_for_warnings(DOC, CONFIG, slow) == [
        "the plain-doc screen did not run (it took over 0 s); the doc is registered all the same"
    ]


def test_registration_goes_through_when_the_screen_fails_and_prints_the_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path / "state"))
    calls: list[object] = []

    def failing(url: str, key_file: str, state: object, questions: dict[str, object], timeout_s: float) -> ModelAnswer:
        calls.append(state)
        return ModelAnswer(answers=None, error="http 503: unavailable", latency_ms=3)

    monkeypatch.setattr("bridge.doc_screen.call_model", failing)
    conn = setup_pr(tmp_path)
    source = tmp_path / "doc.html"
    source.write_bytes(DOC)
    assert cmd_review_doc(argparse.Namespace(pr="Org/app#7", file=str(source)), conn, CONFIG) == 0
    assert read_registered_doc(conn, "Org/app", 7) == DOC and calls
    out, err = capsys.readouterr()
    assert "document registered" in out
    assert "warning: the plain-doc screen did not run (http 503: unavailable); the doc is registered" in err


def test_a_doc_longer_than_the_screen_reads_says_what_was_left_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bridge.doc_screen.MAX_SENTENCES", 2)
    ask, asked = stub({})
    assert screen_doc_for_warnings(DOC, CONFIG, ask) == [
        "the plain-doc screen read the first 2 sentences only; 1 were not read"
    ]
    assert len(asked) == 2
