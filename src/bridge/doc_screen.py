"""A per-sentence screen of a review doc's visible top by a classifier: each sentence gets a few yes/no questions about
what can be seen in it alone, and a sentence above the threshold goes back to the author, quoted, as a warning."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from typing import cast

from bridge.classifier import ModelAnswer, call_model, quotes_customer, scrub
from bridge.config import Config
from bridge.review_doc_check import count_words, list_top_sentences, read_visible_top

CALL_TIMEOUT_S = 5.0
DEADLINE_S = 30.0
"""The whole screen; a call usually takes about 0.3 s."""
PARALLEL_CALLS = 6
MIN_WORDS = 5
"""Shorter ones are labels ("The problem.") with nothing to judge."""
MAX_SENTENCES = 60


@dataclass(frozen=True)
class Screen:
    key: str
    question: str
    true: str
    false: str
    examples_true: tuple[str, ...]
    examples_false: tuple[str, ...]
    reason: str
    """Told to the author with the quoted sentence."""
    threshold: float
    min_words: int = MIN_WORDS


SCREENS = (
    Screen(
        key="history",
        question="`sentence` mentions an earlier version of this pull request or of its design: a previous draft or "
        "head, an approach the author tried and replaced, or something changed because a reviewer asked.",
        true="It refers to an earlier version, draft or design of this pull request, or to what was changed in it "
        "during work or review.",
        false="It describes the system or code as it behaves after this change, or as it behaved before this change "
        "(a before/after of the product is not a history of the pull request), the problem, or the plan.",
        examples_true=(
            "The first version of this PR used a cron job; after review it uses a queue instead.",
            "An earlier head stored the flag in Redis, which was dropped.",
        ),
        examples_false=(
            "Before this change the page reloaded on every click; now it updates in place.",
            "The export used to time out on large tenants.",
        ),
        reason="tells how the PR evolved, not what it is",
        threshold=0.6,
    ),
    Screen(
        key="process",
        question="`sentence` is about the reviewing of this pull request: which reviews or reviewers looked at it, "
        "what they found, which checks the author ran on it, or what the review can wait for.",
        true="Its subject is reviews, reviewers, review findings, or the checks and test runs the author did on "
        "this pull request.",
        false="Its subject is the product, the system, users, the code change, a test the change adds, the plan and "
        "its steps, or what is not done yet.",
        examples_true=(
            "Codex and the review bot found nothing that blocks.",
            "The author ran the suite locally, so review need not wait for CI.",
        ),
        examples_false=(
            "Not done yet: CI, and a live check on staging.",
            "A new test fails when the generated file is stale.",
            "Admins skip every permission check.",
        ),
        reason="is about the review process, not the change",
        threshold=0.7,
    ),
    Screen(
        key="clauses",
        question="`sentence` has two or more subordinate clauses (clauses that start with words such as which, "
        "that, because, when, while, if, so that, since, although, where, who).",
        true="Two or more subordinate clauses can be counted in it.",
        false="It has at most one subordinate clause.",
        examples_true=(),
        examples_false=(),
        reason="has more than one subordinate clause",
        threshold=0.9,
        min_words=20,
    ),
)


@dataclass(frozen=True)
class Flag:
    sentence: str
    reasons: tuple[str, ...]


Ask = Callable[[str, dict[str, object]], ModelAnswer]
"""(sentence, questions) to the model's answer."""


def map_screens_to_questions(words: int) -> dict[str, object]:
    questions: dict[str, object] = {}
    for screen in SCREENS:
        if words < screen.min_words:
            continue
        instructions: dict[str, object] = {"question": screen.question}
        if screen.examples_true:
            instructions["examples_true"] = list(screen.examples_true)
        if screen.examples_false:
            instructions["examples_false"] = list(screen.examples_false)
        questions[screen.key] = {
            "type": "noul",
            "instructions": instructions,
            "criteria": {"true": screen.true, "false": screen.false},
        }
    return questions


def make_ask(url: str, key_file: str) -> Ask:
    def ask(sentence: str, questions: dict[str, object]) -> ModelAnswer:
        return call_model(url, key_file, {"sentence": scrub(sentence)}, questions, CALL_TIMEOUT_S)

    return ask


def read_noul(answers: dict[str, object], key: str) -> float | None:
    answer = answers.get(key)
    value = cast(dict[str, object], answer).get("noul") if isinstance(answer, dict) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1:
        return float(value)
    return None


def list_screened_sentences(page: bytes) -> list[str]:
    """A sentence that quotes a customer is left out: their words never leave this machine."""
    top, _ = read_visible_top(page)
    sentences = dict.fromkeys(list_top_sentences(top))
    return [s for s in sentences if count_words(s) >= MIN_WORDS and not quotes_customer(s)]


def screen_doc(page: bytes, ask: Ask) -> tuple[list[Flag], str | None]:
    """The flagged sentences, and why the screen did not run when it did not: one failed call fails the whole
    screen, so the author never gets a partial verdict that reads as a full one."""
    sentences = list_screened_sentences(page)[:MAX_SENTENCES]
    if not sentences:
        return [], None

    def ask_one(sentence: str) -> ModelAnswer:
        return ask(sentence, map_screens_to_questions(count_words(sentence)))

    pool = ThreadPoolExecutor(max_workers=PARALLEL_CALLS)
    futures = {pool.submit(ask_one, sentence): sentence for sentence in sentences}
    replies: dict[str, ModelAnswer] = {}
    try:
        for future in as_completed(futures, timeout=DEADLINE_S):
            reply = future.result()
            if reply.answers is None:
                return [], reply.error or "no answer"
            replies[futures[future]] = reply
    except FuturesTimeout:
        return [], f"it took over {DEADLINE_S:.0f} s"
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    flags: list[Flag] = []
    for sentence in sentences:
        reply = replies[sentence]
        assert reply.answers is not None
        reasons: list[str] = []
        for screen in SCREENS:
            if count_words(sentence) < screen.min_words:
                continue
            value = read_noul(reply.answers, screen.key)
            if value is None:
                return [], f"no answer to '{screen.key}'"
            if value >= screen.threshold:
                reasons.append(screen.reason)
        if reasons:
            flags.append(Flag(sentence=sentence, reasons=tuple(reasons)))
    return flags, None


def convert_flags_to_warnings(flags: list[Flag], error: str | None) -> list[str]:
    if error is not None:
        return [f"the plain-doc screen did not run ({error}); the doc is registered all the same"]
    return [
        f'a sentence above the folds {" and ".join(flag.reasons)}: "{flag.sentence}" (rewrite, move or fold it; '
        "this is a warning, not a refusal)"
        for flag in flags
    ]


def screen_doc_for_warnings(page: bytes, config: Config, ask: Ask | None = None) -> list[str]:
    """Off unless the install turns it on and has a key file; never raises, so a failed screen cannot undo a
    registration."""
    if not config.doc_screen or not config.shadow_filter_key_file:
        return []
    try:
        flags, error = screen_doc(page, ask or make_ask(config.shadow_filter_url, config.shadow_filter_key_file))
    except Exception as exc:  # the screen is advice: it must not fail a registration that already happened
        flags, error = [], type(exc).__name__
    warnings = convert_flags_to_warnings(flags, error)
    unread = len(list_screened_sentences(page)) - MAX_SENTENCES
    if error is None and unread > 0:
        warnings.append(f"the plain-doc screen read the first {MAX_SENTENCES} sentences only; {unread} were not read")
    return warnings
