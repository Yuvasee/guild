import sqlite3
from pathlib import Path
from typing import Any

from conftest import BridgeEnv, SpawnProcess
from test_leads import Team
from test_typing import ENTER, FakeClaudeBox, _type  # pyright: ignore[reportPrivateUsage]

from bridge.board import USER_CONVERSATION, Party, send_message
from bridge.config import Config
from bridge.db import transaction
from bridge.feed import (
    TYPED_USER_MAX_CHARS,
    _convert_message_to_typed_text,  # pyright: ignore[reportPrivateUsage]
    convert_cut_to_resume_text,
    convert_user_message_to_typed_text,
    find_stuck_deliveries,
    record_stuck_delivery,
    resolve_tab,
    tell_assistant_of_draft,
    tell_assistant_of_stuck,
)
from bridge.tabinput import CHIP_AT_CHARS, is_paste_chip

LONG = "[bridge] theo on the dashboard (#79): " + "word " * 400


class ChipBox(FakeClaudeBox):
    """Claude Code 2.1.322: an insert of CHIP_AT_CHARS or more shows as a numbered chip, as it did with #79."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.chips = 0

    def run(self, _: Config, args: list[str]) -> str | None:
        key = args[-1]
        if args[:2] == ["session", "type"] and len(key) >= CHIP_AT_CHARS:
            self.chips += 1
            args = [*args[:-1], f"[Pasted text #{self.chips}]"]
        return super().run(_, args)


def test_the_chip_our_long_insert_became_gets_its_enter(tmp_path: Path) -> None:
    fake = ChipBox()

    result = _type(tmp_path, fake, command=LONG)

    assert result == "typed"
    assert fake.submitted == ["[Pasted text #1]"]


def test_a_dropped_enter_on_our_chip_is_pressed_again(tmp_path: Path) -> None:
    fake = ChipBox(dropped_enters=1)

    result = _type(tmp_path, fake, command=LONG, wait_s=12)

    assert result == "typed"
    assert fake.submitted == ["[Pasted text #1]"]


def test_a_chip_already_in_the_box_is_never_ours(tmp_path: Path) -> None:
    fake = ChipBox(boxes={"S": "[Pasted text #4]"})

    result = _type(tmp_path, fake, command=LONG, wait_s=3)

    assert result != "typed"
    assert ENTER not in fake.typed
    assert fake.boxes["S"] == "[Pasted text #4]"


def test_chip_forms() -> None:
    assert is_paste_chip("[Pasted text #1]")
    assert is_paste_chip("[Pasted text #17 +18 lines]")
    assert not is_paste_chip("[Pasted text #1][Pasted text #2]"), "two chips: the second is not this insert"
    assert not is_paste_chip("[Pasted text #1] and more")
    assert not is_paste_chip("[Image #1]")
    assert not is_paste_chip(None)


def test_a_stuck_message_is_told_to_the_assistant_once_a_minute_after_it_first_stuck(
    bridge_env: BridgeEnv, spawn_process: SpawnProcess
) -> None:
    Team(bridge_env, spawn_process)
    config = Config(assistant_handle="old-a")
    with bridge_env.connect() as conn, transaction(conn):
        recipient = resolve_tab(conn, "conv-b", "old-b")
        sent = send_message(conn, Party(USER_CONVERSATION, "theo"), [recipient], "info", "long words", 1_000_000)
        message = sent.message_id
        record_stuck_delivery(conn, message, "conv-b", 1_000_000)
        record_stuck_delivery(conn, message, "conv-b", 1_030_000)
        early = find_stuck_deliveries(conn, 1_050_000)
        due = find_stuck_deliveries(conn, 1_061_000)
        told = tell_assistant_of_stuck(conn, config, due[0], "[Pasted text #1]", 1_061_000)
        again = find_stuck_deliveries(conn, 1_200_000)
        draft = tell_assistant_of_draft(conn, config, message, "conv-b", 1_200_000)
        body = conn.execute("SELECT body FROM messages ORDER BY id DESC LIMIT 1").fetchone()[0]

    assert early == [], "60 s from the first stuck result, not the latest"
    assert [(s.message_id, s.agterm_session, s.surface) for s in due] == [(message, "TAB-B", "PANE-B")]
    assert told is not None and told[1].handle == "old-a"
    assert again == [] and draft is None, "one alert per message"
    assert f"#{message} was typed into old-b's tab (TAB-B)" in body
    assert "'[Pasted text #1]'" in body
    assert "--target TAB-B --pane-id PANE-B" in body


def _row(**fields: object) -> sqlite3.Row:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    columns = ", ".join(f":{k} AS {k}" for k in fields)
    return conn.execute(f"SELECT {columns}", fields).fetchone()


def test_every_typed_form_stays_under_the_chip() -> None:
    """The longest of each form bridge types: a 64-char handle, a six-digit id, a request long overdue."""
    handle = "h" * 64
    words = "word " * 2_000
    longest = {
        "user, whole": convert_user_message_to_typed_text("y" * 20, "x" * TYPED_USER_MAX_CHARS, 999_999),
        "user, cut": convert_user_message_to_typed_text("y" * 20, words, 999_999),
        "peer": _convert_message_to_typed_text(
            _row(from_conversation="c", from_handle=handle, body=words, kind="info", respond_by=None, post_type=None),
            999_999,
            0,
        ),
        "request": _convert_message_to_typed_text(
            _row(
                from_conversation="c",
                from_handle=handle,
                body=words,
                kind="request",
                respond_by=-99_999_000,
                post_type=None,
            ),
            999_999,
            0,
        ),
        "resume": convert_cut_to_resume_text(f"an API error ({'e' * 64})", 0),
    }

    assert {name: len(text) for name, text in longest.items() if len(text) >= CHIP_AT_CHARS} == {}
