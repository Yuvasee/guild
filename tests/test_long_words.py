import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest

from bridge.board import convert_user_message_to_parts, retarget_open_messages, split_text_to_parts
from bridge.db import connect
from bridge.inject import Part, pack_parts, save_later_parts, take_later_parts


@pytest.fixture
def conn(tmp_path: Path) -> Generator[sqlite3.Connection]:
    c = connect(tmp_path / "bridge.db")
    yield c
    c.close()


def test_text_splits_at_a_space_into_pieces_that_rejoin_whole() -> None:
    text = " ".join(f"w{i}x" for i in range(2_000))
    pieces = split_text_to_parts(text, 4_000)
    assert all(len(p) <= 4_000 for p in pieces) and " ".join(pieces) == text
    assert split_text_to_parts("y" * 9_000, 4_000) == ["y" * 4_000, "y" * 4_000, "y" * 1_000]


def test_numbered_parts_never_arrive_out_of_order() -> None:
    first, second = convert_user_message_to_parts(3, "user", "a " * 2_000 + "b " * 1_000)
    assert "part 1 of 2" in first.text and "part 2 of 2" in second.text
    notice = Part("n" * 5_000, "a notice")
    packed = pack_parts([notice, first, second])
    assert packed.shown == [notice] and packed.later == [first, second], "part 2 fits, but waits behind part 1"


def test_saved_parts_keep_whether_they_wait_in_order(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO conversations (conversation_id, first_seen_at, last_seen_at) VALUES ('c', 1, 1)")
    parts = [Part("notice", "a notice"), *convert_user_message_to_parts(3, "user", "hold the deploy")]
    save_later_parts(conn, "c", parts)
    assert [p.in_order for p in take_later_parts(conn, "c")] == [False, True]


def test_parts_a_hook_saved_for_later_follow_the_tab_through_clear(conn: sqlite3.Connection) -> None:
    for conversation in ("conv-old", "conv-new"):
        conn.execute(
            "INSERT INTO conversations (conversation_id, first_seen_at, last_seen_at) VALUES (?, 1, 1)", (conversation,)
        )
    parts = convert_user_message_to_parts(3, "user", "a " * 3_000)
    save_later_parts(conn, "conv-old", parts)

    retarget_open_messages(conn, "conv-old", "conv-new", "instance-1")

    assert take_later_parts(conn, "conv-old") == []
    assert take_later_parts(conn, "conv-new") == parts
