from pathlib import Path

from bridge.compaction import KEEP_MAX_CHARS, convert_keep_to_context
from bridge.db import connect
from bridge.inject import (
    INJECT_MAX_CHARS,
    Part,
    add_part,
    cap_context,
    measure_chars,
    pack_parts,
    save_later_parts,
    take_later_parts,
)


def test_characters_are_counted_as_claude_code_counts_them() -> None:
    assert measure_chars("éß") == 2
    assert measure_chars("🤖") == 2, "outside the BMP a character is two UTF-16 units, as in JavaScript"


def test_parts_that_do_not_fit_wait_in_priority_order_and_are_named() -> None:
    packed = pack_parts([Part("k" * 5_000, "the list"), Part("r" * 3_000, "the role"), Part("n" * 2_000, "notes", "x")])

    assert [p.name for p in packed.shown] == ["the list", "the role"]
    assert [p.name for p in packed.later] == ["notes"]
    assert "notes. Now: `x`." in packed.text
    assert measure_chars(packed.text) <= INJECT_MAX_CHARS


def test_a_part_bigger_than_any_message_is_cut_with_a_pointer() -> None:
    packed = pack_parts([Part("🤖" * 9_000, "huge", "bridge keep --show")])

    assert not packed.later
    assert packed.text.endswith("`bridge keep --show` prints all of it)")
    assert measure_chars(packed.text) <= INJECT_MAX_CHARS
    assert measure_chars(cap_context("é" * 20_000)) <= INJECT_MAX_CHARS


def test_mail_that_does_not_fit_waits_and_a_start_keeps_waiting_notices(tmp_path: Path) -> None:
    packed = add_part(pack_parts([Part("k" * 8_000, "the list")]), Part("m" * 700, "your bridge messages"))
    conn = connect(tmp_path / "b.db")
    conn.execute("INSERT INTO conversations (conversation_id, first_seen_at, last_seen_at) VALUES ('c', 0, 0)")
    save_later_parts(conn, "c", [*packed.later, Part("role", "your lead role", rebuilt=True), Part("nudge")])

    assert [p.name for p in packed.later] == ["your bridge messages"]
    assert [p.rebuilt for p in take_later_parts(conn, "c")] == [False, True, False]
    assert take_later_parts(conn, "c") == [], "taken once"


def test_the_longest_keep_list_fits_one_message_on_its_own() -> None:
    restored = convert_keep_to_context("🤖" * (KEEP_MAX_CHARS // 2), 0, 86_400_000 * 30)
    packed = pack_parts([Part(restored, "the list"), Part("n" * 700)])

    assert [p.name for p in packed.shown] == ["the list"]
    assert packed.shown[0].text == restored
