import json
import sqlite3
import threading
import time
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from bridge.board import SYSTEM_CONVERSATION, USER_CONVERSATION, Party, Recipient, send_message
from bridge.config import Config, map_toml_to_config
from bridge.db import connect
from bridge.shadow_filter import (
    CATCH_UP_MS,
    Verdict,
    ask_model,
    find_start_id,
    log_path,
    map_rows_to_report,
    read_rows,
    run_shadow_tick,
)

NOW = 1_800_000_000_000
CONFIG = Config(assistant_handle="aide", shadow_filter=True, shadow_filter_threshold=0.2)
FAKE_KEY = "fake-key-0123456789"
WORKER = Party("conv-worker", "fix-login")


class FakeModel:
    def __init__(self, p_now: float | None = 0.5, error: str | None = None) -> None:
        self.p_now = p_now
        self.error = error
        self.states: list[str] = []

    def __call__(self, state: str) -> Verdict:
        self.states.append(state)
        return Verdict(p_now=self.p_now, error=self.error, latency_ms=7)


def _no_call(state: str) -> Verdict:
    raise AssertionError("the model was asked")


@pytest.fixture
def conn(tmp_path: Path) -> Generator[sqlite3.Connection]:
    db = connect(tmp_path / "bridge.db")
    for name in ("ana", "ben"):
        db.execute(
            "INSERT INTO leads (name, display_name, scope, linear_refs, tint, created_at, domains) "
            "VALUES (?, ?, 'scope', '[]', 'blue', 0, '[]')",
            (name, name.capitalize()),
        )
    yield db
    db.close()


def _recipient(handle: str) -> Recipient:
    return Recipient(
        handle=handle, conversation_id=f"conv-{handle}", pid=0, status="idle", peer_name=None, peer_messaging=False
    )


def _send(
    conn: sqlite3.Connection,
    body: str,
    to: tuple[str, ...] = ("ana",),
    kind: str = "info",
    sender: Party = WORKER,
    post_type: str | None = None,
    at: int = NOW,
) -> int:
    result = send_message(conn, sender, [_recipient(h) for h in to], kind, body, at)
    if post_type is not None:
        conn.execute("UPDATE messages SET post_type = ? WHERE id = ?", (post_type, result.message_id))
    return result.message_id


@pytest.mark.parametrize(
    ("kind", "sender", "post_type", "body", "rule"),
    [
        ("request", Party("conv-worker", "fix-login"), None, "can I merge?", "kind:request"),
        ("reply", Party("conv-worker", "fix-login"), None, "done", "kind:reply"),
        ("info", Party("conv-worker", "fix-login"), "question", "which branch?", "type:question"),
        ("info", Party(SYSTEM_CONVERSATION, "bridge"), "blocked-tab", "tab waits on a dialog", "type:blocked-tab"),
        ("info", Party("conv-aide", "aide"), None, "the owner says: ship it", "assistant"),
        ("info", Party(USER_CONVERSATION, "owner"), None, "look at this", "owner"),
        ("info", Party("conv-worker", "fix-login"), None, "STOP the deploy", "urgent"),
        ("info", Party("conv-worker", "fix-login"), None, "URGENT: prod is down", "urgent"),
    ],
)
def test_an_important_message_goes_now_by_rule_without_asking_the_model(
    conn: sqlite3.Connection, kind: str, sender: Party, post_type: str | None, body: str, rule: str
) -> None:
    message = _send(conn, body, kind=kind, sender=sender, post_type=post_type)

    assert run_shadow_tick(conn, CONFIG, 0, NOW, _no_call) == message
    [row] = read_rows()
    assert (row["source"], row["decision"], row["rule"], row["lead"]) == ("rule", "now", rule, "ana")


def test_the_threshold_decides_now_or_batch(conn: sqlite3.Connection) -> None:
    for p_now, expected in ((0.19, "batch"), (0.2, "now"), (0.9, "now")):
        before = conn.execute("SELECT max(id) FROM messages").fetchone()[0] or 0
        _send(conn, "deployed to staging")
        run_shadow_tick(conn, CONFIG, before, NOW, FakeModel(p_now))
        row = read_rows()[-1]
        assert (row["source"], row["decision"], row["p_now"], row["latency_ms"]) == ("model", expected, p_now, 7)


def test_a_model_error_logs_now_with_the_error(conn: sqlite3.Connection) -> None:
    _send(conn, "deployed to staging")

    run_shadow_tick(conn, CONFIG, 0, NOW, FakeModel(None, "timeout"))

    [row] = read_rows()
    assert (row["source"], row["decision"], row["p_now"], row["error"]) == ("model", "now", None, "timeout")


def test_secrets_are_scrubbed_before_the_model_sees_the_text(conn: sqlite3.Connection) -> None:
    token = "ghp_" + "A" * 30
    _send(conn, f"deployed, token={token} and {'sk-' + 'b' * 20}, mail dev@example.com, db postgres://u:p@h/x")
    model = FakeModel(0.1)

    run_shadow_tick(conn, CONFIG, 0, NOW, model)

    [state] = model.states
    assert token not in state and "sk-bbbb" not in state and "dev@example.com" not in state and "u:p@h" not in state
    assert "[secret]" in state and "[email]" in state and "[connection-string]" in state


@pytest.mark.parametrize(
    "body",
    [
        'config {"password": "hunter2-invented"}',
        "Authorization: Bearer hunter2-invented",
        "curl -H bearer hunter2-invented",
    ],
)
def test_quoted_and_bearer_credentials_are_scrubbed(conn: sqlite3.Connection, body: str) -> None:
    _send(conn, body)
    model = FakeModel(0.1)

    run_shadow_tick(conn, CONFIG, 0, NOW, model)

    assert "hunter2" not in model.states[0]


@pytest.mark.parametrize(
    "body",
    [
        'the customer said "my card was charged twice"',
        'The customer says "where is my order"',
        "Ticket:\nCustomer: my card was charged twice",
    ],
)
def test_a_message_quoting_a_customer_is_never_sent_and_logged_now(conn: sqlite3.Connection, body: str) -> None:
    _send(conn, body)

    run_shadow_tick(conn, CONFIG, 0, NOW, _no_call)

    [row] = read_rows()
    assert (row["source"], row["decision"]) == ("skipped", "now")


def test_one_call_per_message_and_one_row_per_lead_leaving_out_others(conn: sqlite3.Connection) -> None:
    board = _send(conn, "merged the login fix", to=("ana", "ben", "fix-login"))
    cancelled = _send(conn, "never mind", to=("ben",))
    conn.execute("UPDATE messages SET cancelled_at = ? WHERE id = ?", (NOW, cancelled))
    _send(conn, "to a worker only", to=("fix-login",))
    model = FakeModel(0.05)

    run_shadow_tick(conn, CONFIG, 0, NOW, model)

    assert len(model.states) == 1
    assert [(r["message"], r["lead"], r["conversation"], r["decision"]) for r in read_rows()] == [
        (board, "ana", "conv-ana", "batch"),
        (board, "ben", "conv-ben", "batch"),
    ]


def test_the_shadow_leaves_delivery_exactly_as_it_was(conn: sqlite3.Connection) -> None:
    _send(conn, "deployed to staging", to=("ana", "ben"))
    _send(conn, "can I merge?", kind="request")
    _send(conn, "STOP", to=("ben",))
    conn.execute("UPDATE message_recipients SET offered_at = ?, acked_at = ? WHERE handle = 'ana'", (NOW, NOW))

    def snapshot() -> list[tuple[object, ...]]:
        tables = ("messages", "message_recipients", "conversations", "instances", "user_items")
        return [tuple(r) for t in tables for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")]

    before = snapshot()
    run_shadow_tick(conn, CONFIG, 0, NOW, FakeModel(0.01))
    assert snapshot() == before
    assert len(read_rows()) == 4


def test_a_restart_resumes_where_the_log_left_off_and_skips_old_messages(conn: sqlite3.Connection) -> None:
    _send(conn, "before the shadow was on")
    assert find_start_id(conn) == 1

    second = _send(conn, "deployed")
    assert run_shadow_tick(conn, CONFIG, 1, NOW, FakeModel(0.1)) == second
    stale = _send(conn, "sent while serve was down", at=NOW - CATCH_UP_MS - 1)
    fresh = _send(conn, "sent just before the restart")

    after = find_start_id(conn)
    assert after == second
    run_shadow_tick(conn, CONFIG, after, NOW, FakeModel(0.1))
    assert [r["message"] for r in read_rows()] == [second, fresh]
    assert stale not in [r["message"] for r in read_rows()]


def test_the_report_counts_each_delivery_once_by_rule_model_batch_latency_and_error() -> None:
    rows: list[dict[str, object]] = [
        {"at": NOW, "message": 1, "lead": "ana", "source": "rule", "rule": "kind:request", "decision": "now"},
        {"at": NOW, "message": 2, "lead": "ana", "source": "model", "decision": "batch", "p_now": 0.1, "latency_ms": 9},
        {
            "at": NOW,
            "message": 2,
            "lead": "ana",
            "source": "model",
            "decision": "batch",
            "p_now": 0.1,
            "latency_ms": 200,
        },
        {
            "at": NOW,
            "message": 2,
            "lead": "ben",
            "source": "model",
            "decision": "batch",
            "p_now": 0.1,
            "latency_ms": 200,
        },
        {"at": NOW, "message": 3, "lead": "ana", "source": "model", "decision": "now", "p_now": 0.7, "latency_ms": 300},
        {
            "at": NOW,
            "message": 4,
            "lead": "ana",
            "source": "model",
            "decision": "now",
            "latency_ms": 3000,
            "error": "timeout",
        },
        {"at": NOW, "message": 5, "lead": "ana", "source": "skipped", "decision": "now"},
        {"at": NOW - 10, "message": 0, "lead": "ana", "source": "model", "decision": "batch", "latency_ms": 1},
    ]

    assert map_rows_to_report(rows, NOW) == [
        "messages to leads: 5 (6 lead deliveries)",
        "by rule: 1 (kind:request 1)",
        "by model: 4; skipped (customer text): 1",
        "would batch: 2 = 33% of lead deliveries, 50% of model decisions",
        "model latency ms: p50 300, p95 3000",
        "model errors (logged as now): 1",
        "  1 x timeout",
    ]


def test_the_log_moves_aside_when_full_and_the_report_reads_both(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("bridge.shadow_filter.LOG_MAX_BYTES", 10)
    _send(conn, "one")
    run_shadow_tick(conn, CONFIG, 0, NOW, FakeModel(0.1))
    _send(conn, "two")
    run_shadow_tick(conn, CONFIG, 1, NOW, FakeModel(0.1))

    assert log_path().with_name(log_path().name + ".1").exists()
    assert [r["message"] for r in read_rows()] == [1, 2]


class _Api(BaseHTTPRequestHandler):
    reply: ClassVar[tuple[int, object]] = (200, {})
    delay_s: ClassVar[float] = 0.0
    seen: ClassVar[list[tuple[str, dict[str, object]]]] = []

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append((self.headers["Authorization"], body))
        time.sleep(self.delay_s)
        status, payload = self.reply
        data = json.dumps(payload).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def api(tmp_path: Path) -> Generator[tuple[str, str]]:
    _Api.reply, _Api.delay_s, _Api.seen = (200, {}), 0.0, []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Api)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    key_file = tmp_path / "key"
    key_file.write_text(FAKE_KEY + "\n")
    yield f"http://127.0.0.1:{server.server_address[1]}/v1/systemone", str(key_file)
    server.shutdown()


def test_the_model_call_reads_p_now_and_sends_the_key_only_in_its_header(api: tuple[str, str]) -> None:
    url, key_file = api
    _Api.reply = (200, {"answers": {"reaction": {"choice": "wait", "probabilities": {"now": 0.12, "wait": 0.88}}}})

    verdict = ask_model(url, key_file, "Message:\ndeployed")

    assert (verdict.p_now, verdict.error) == (0.12, None)
    [(auth, body)] = _Api.seen
    assert auth == f"Bearer {FAKE_KEY}"
    assert body["state"] == "Message:\ndeployed" and FAKE_KEY not in json.dumps(body)
    assert list(json.loads(json.dumps(body["questions"]))) == ["reaction"]


def test_a_slow_model_times_out(api: tuple[str, str]) -> None:
    url, key_file = api
    _Api.delay_s = 1.0

    verdict = ask_model(url, key_file, "x", timeout_s=0.2)

    assert (verdict.p_now, verdict.error) == (None, "timeout")
    assert verdict.latency_ms < 900


def test_an_http_error_never_carries_the_key(api: tuple[str, str]) -> None:
    url, key_file = api
    _Api.reply = (401, {"error": f"bad key {FAKE_KEY}"})

    verdict = ask_model(url, key_file, "x")

    assert verdict.p_now is None and verdict.error is not None
    assert verdict.error.startswith("http 401") and FAKE_KEY not in verdict.error


def test_a_missing_key_file_or_bad_answer_is_an_error(api: tuple[str, str], tmp_path: Path) -> None:
    url, key_file = api
    assert ask_model(url, str(tmp_path / "none"), "x").error == "key file unreadable: FileNotFoundError"
    _Api.reply = (200, {"answers": {}})
    assert ask_model(url, key_file, "x").error == "no probability for 'now' in the answer"


def test_a_probability_out_of_range_is_an_error(api: tuple[str, str]) -> None:
    url, key_file = api
    for bad in (-0.1, 1.5):
        _Api.reply = (200, {"answers": {"reaction": {"probabilities": {"now": bad}}}})
        verdict = ask_model(url, key_file, "x")
        assert verdict.p_now is None and "out of range" in str(verdict.error)


def test_a_malformed_key_never_shows_in_the_error(api: tuple[str, str], tmp_path: Path) -> None:
    url, _ = api
    key_file = tmp_path / "two-line-key"
    key_file.write_text("fake-part-one\nfake-part-two\n")

    verdict = ask_model(url, str(key_file), "x")

    assert verdict.p_now is None and verdict.error is not None
    assert "fake-part" not in verdict.error


def test_a_row_cut_short_by_a_crash_does_not_swallow_the_next(conn: sqlite3.Connection) -> None:
    log_path().parent.mkdir(parents=True, exist_ok=True)
    log_path().write_text('{"at": 1, "mess')
    _send(conn, "deployed")

    run_shadow_tick(conn, CONFIG, 0, NOW, FakeModel(0.1))

    assert [r["message"] for r in read_rows()] == [1]


def test_config_keys_and_their_defaults() -> None:
    assert not Config().shadow_filter
    config = map_toml_to_config(
        {
            "shadow_filter": True,
            "shadow_filter_threshold": 0.3,
            "shadow_filter_key_file": "~/k",
            "shadow_filter_url": "u",
        }
    )
    assert (config.shadow_filter, config.shadow_filter_threshold, config.shadow_filter_key_file) == (True, 0.3, "~/k")
    assert config.shadow_filter_url == "u"
    with pytest.raises(ValueError, match="from 0 to 1"):
        map_toml_to_config({"shadow_filter_threshold": 2})
