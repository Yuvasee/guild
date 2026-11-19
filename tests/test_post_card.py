def test_a_long_card_field_with_no_line_break_is_nudged_never_refused() -> None:
    from bridge.cli_board import find_card_line_problem

    wall = "The export moves to a background job. It writes to a bucket. Without it the user gets a 503. " * 2
    assert find_card_line_problem(wall) == f"is one block of {len(wall.strip())} characters"
    assert find_card_line_problem("- The export moves to a job\n- It needs a bucket\n- Else a 503") is None
    assert find_card_line_problem("Short and clear.") is None
    assert find_card_line_problem(None) is None
