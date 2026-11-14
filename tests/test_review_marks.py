import pytest

from bridge.review_marks import (
    convert_reviews_to_text,
    find_missing_usage_section,
    map_html_to_reviews,
    map_markdown_to_reviews,
    map_text_to_reviews,
)

NAMES = ("Codex", "Claude", "ReviewBot")


def test_a_docs_reviews_section_names_the_reviews_that_ran_and_ends_at_the_next_heading() -> None:
    doc = b"""<h1>PR 7</h1><p>Codex is mentioned in the intro.</p>
    <h2 id="r">Reviews <small>(3)</small></h2>
    <h3>Codex</h3><ul><li>N+1 query: fixed in abc123</li></ul>
    <h3>Review&shy;Bot</h3><p>none</p>
    <h2>Risks</h2><p>Claude says nothing here</p>"""
    assert map_html_to_reviews(doc, NAMES) == ("Codex",)
    both = b"<h2>Reviews</h2><table><tr><td>codex</td><td>CLAUDE</td><td>ReviewBot</td></tr></table>"
    assert map_html_to_reviews(both, NAMES) == ("Codex", "Claude", "ReviewBot")
    assert map_html_to_reviews(b"<h2>Review notes</h2><p>codex Claude</p>", NAMES) is None
    assert map_html_to_reviews(b"<h1>No section</h1><p>codex and Claude</p>", NAMES) is None
    assert map_html_to_reviews(b"<h2>Reviews</h2><p>none yet</p>", NAMES) == ()


def test_a_pr_bodys_reviews_heading_or_label_names_the_reviews() -> None:
    body = "## Summary\nUses codex.\n\n## Reviews\n- Codex: 2 findings, fixed in abc\n- Claude: none\n\n## Test plan\n"
    assert map_markdown_to_reviews(body, NAMES) == ("Codex", "Claude")
    assert map_markdown_to_reviews("**Reviews:**\n- Codex: clean\n", NAMES) == ("Codex",)
    assert map_markdown_to_reviews("## Reviews\n### Codex\nclean\n### Claude\nclean\n## Next\nReviewBot", NAMES) == (
        "Codex",
        "Claude",
    )
    assert map_markdown_to_reviews("Codex and Claude ran, no section", NAMES) is None


def test_stored_reviews_tell_no_section_from_an_empty_one() -> None:
    assert map_text_to_reviews(convert_reviews_to_text(None)) is None
    assert map_text_to_reviews(convert_reviews_to_text(())) == ()
    assert map_text_to_reviews(convert_reviews_to_text(("Codex",))) == ("Codex",)


def test_comments_scripts_and_fenced_examples_are_no_reviews_section() -> None:
    assert map_html_to_reviews(b"<!-- <h2>Reviews</h2><p>Codex, Claude</p> --><h2>Risks</h2>", NAMES) is None
    real = b"<!-- <h2>Reviews</h2> --><h2>Reviews</h2><p>Codex, Claude</p><script>/* <h2>x</h2> */</script>"
    assert map_html_to_reviews(real, NAMES) == ("Codex", "Claude")
    example = "Template:\n```md\n## Reviews\n- Codex\n- Claude\n```\n\n## Reviews\nNone yet\n<!-- Codex -->\n"
    assert map_markdown_to_reviews(example, NAMES) == ()
    assert map_markdown_to_reviews("<!--\n## Reviews\nCodex\n-->\nnothing", NAMES) is None


@pytest.mark.parametrize(
    ("title", "body", "words"),
    [
        ("Add team export flag", "The export is off by default.", "off by default"),
        ("Config: opt-in setting for the cache", "", "opt-in setting"),
        ("x", "Enable it with the opt in flag.", "opt in flag"),
        ("x", "Trace capture is unchanged and still opt-in.", None),
        ("x", "It serves this on its own opt-in port.", None),
        ("Step 2 of 4: queue table", "Nothing reads it yet.", "Step 2 of 4"),
        ("x", "Lives behind a feature flag.", "behind a feature flag"),
        ("x", "No behaviour change for users.", "No behaviour change"),
        ("x", "Inert until the worker reads it; not yet wired.", "Inert until"),
        ("Fix the export total", "Counts rows once.", None),
        ("x", "```\n# off by default\n```\n<!-- opt-in -->", None),
        ("x", "Off by default.\n\n## How this will be used\n1. The problem today.", None),
        ("x", "Off by default.\n\n**How this will be used:**\nThe plan.", None),
        ("x", "Off by default, but only as a test fixture.\n\nNot an enabling PR: the flag is a test fixture.", None),
        ("x", "Off by default.\n\nNot an enabling PR:", "Off by default"),
    ],
)
def test_a_pr_that_says_it_only_enables_something_needs_how_this_will_be_used(
    title: str, body: str, words: str | None
) -> None:
    assert find_missing_usage_section(title, body) == words
