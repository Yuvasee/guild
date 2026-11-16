"""The hosted yes/no and choice classifier both the shadow filter and the review-doc screen call, and the scrubbing
of what is sent to it."""

import http.client
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

MODEL = "jev-latest"
_SECRET_RES: tuple[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...] = (
    (
        re.compile(
            r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{16,}"
            r"|xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+)"
        ),
        "[secret]",
    ),
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|authorization)[\"']?\s*[:=]\s*(?:bearer\s+)?\S+"
        ),
        r"\1=[secret]",
    ),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [secret]"),
    (re.compile(r"(?i)\b(mongodb(\+srv)?|postgres(ql)?|mysql|redis|amqp)://\S+"), "[connection-string]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[email]"),
    (
        re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),
        lambda m: m.group(0) if re.fullmatch(r"[0-9a-f]{40}", m.group(0)) else "[long-token]",
    ),
)
_CUSTOMER_TEXT_RE = re.compile(
    r"(?im)((customer|user|client) (said|says|wrote|writes|asked|asks)|conversation text|transcript of|\"role\"\s*:"
    r"|\buser:\s|\bassistant:\s|^\s*(customer|client):|message content:|the (bot|agent) (replied|answered):)"
)


def scrub(text: str) -> str:
    for pattern, replacement in _SECRET_RES:
        text = pattern.sub(replacement, text)
    return text


def quotes_customer(text: str) -> bool:
    return bool(_CUSTOMER_TEXT_RE.search(text))


@dataclass(frozen=True)
class ModelAnswer:
    answers: dict[str, object] | None
    error: str | None
    latency_ms: int


def call_model(url: str, key_file: str, state: object, questions: dict[str, object], timeout_s: float) -> ModelAnswer:
    """One call to the classifier. The key is read for each call and kept nowhere; an error text never carries it."""
    started = time.monotonic()

    key = ""

    def failed(error: str) -> ModelAnswer:
        for part in key.split():
            error = error.replace(part, "[key]")
        return ModelAnswer(answers=None, error=error[:200], latency_ms=round((time.monotonic() - started) * 1000))

    try:
        key = Path(key_file).expanduser().read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        return failed(f"key file unreadable: {type(exc).__name__}")
    if not key:
        return failed("key file empty")
    body = json.dumps({"state": state, "model": MODEL, "questions": questions}).encode()
    request = urllib.request.Request(
        url, data=body, method="POST", headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            answer = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode(errors="replace")[:300]
        except (OSError, http.client.HTTPException):
            detail = ""
        return failed(f"http {exc.code}: {scrub(detail)}")
    except TimeoutError:
        return failed("timeout")
    except urllib.error.URLError as exc:
        reason = "timeout" if isinstance(exc.reason, TimeoutError) else str(exc.reason)
        return failed(reason)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        # The name only: a malformed key shows up in the text of some of these.
        return failed(type(exc).__name__)
    answers = cast(dict[str, object], answer).get("answers") if isinstance(answer, dict) else None
    if not isinstance(answers, dict):
        return failed("no answers in the reply")
    return ModelAnswer(
        answers=cast(dict[str, object], answers), error=None, latency_ms=round((time.monotonic() - started) * 1000)
    )
