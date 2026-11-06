from bridge.review_marks import (
    convert_reviews_to_text,
    map_html_to_reviews,
    map_markdown_to_reviews,
    map_text_to_reviews,
)


def test_a_docs_reviews_section_names_the_reviews_that_ran_and_ends_at_the_next_heading() -> None:
    doc = b"""<h1>PR 7</h1><p>Codex is mentioned in the intro.</p>
    <h2 id="r">Reviews <small>(3)</small></h2>
    <h3>Codex</h3><ul><li>N+1 query: fixed in abc123</li></ul>
    <h3>Review&shy;Bot</h3><p>none</p>
    <h2>Risks</h2><p>Claude says nothing here</p>"""
    assert map_html_to_reviews(doc) == ("Codex",)
    both = b"<h2>Reviews</h2><table><tr><td>codex</td><td>CLAUDE</td><td>ReviewBot</td></tr></table>"
    assert map_html_to_reviews(both) == ("Codex", "Claude", "ReviewBot")
    assert map_html_to_reviews(b"<h2>Review notes</h2><p>codex Claude</p>") is None
    assert map_html_to_reviews(b"<h1>No section</h1><p>codex and Claude</p>") is None
    assert map_html_to_reviews(b"<h2>Reviews</h2><p>none yet</p>") == ()


def test_a_pr_bodys_reviews_heading_or_label_names_the_reviews() -> None:
    body = "## Summary\nUses codex.\n\n## Reviews\n- Codex: 2 findings, fixed in abc\n- Claude: none\n\n## Test plan\n"
    assert map_markdown_to_reviews(body) == ("Codex", "Claude")
    assert map_markdown_to_reviews("**Reviews:**\n- Codex: clean\n") == ("Codex",)
    assert map_markdown_to_reviews("## Reviews\n### Codex\nclean\n### Claude\nclean\n## Next\nReviewBot") == (
        "Codex",
        "Claude",
    )
    assert map_markdown_to_reviews("Codex and Claude ran, no section") is None


def test_stored_reviews_tell_no_section_from_an_empty_one() -> None:
    assert map_text_to_reviews(convert_reviews_to_text(None)) is None
    assert map_text_to_reviews(convert_reviews_to_text(())) == ()
    assert map_text_to_reviews(convert_reviews_to_text(("Codex",))) == ("Codex",)


def test_comments_scripts_and_fenced_examples_are_no_reviews_section() -> None:
    assert map_html_to_reviews(b"<!-- <h2>Reviews</h2><p>Codex, Claude</p> --><h2>Risks</h2>") is None
    real = b"<!-- <h2>Reviews</h2> --><h2>Reviews</h2><p>Codex, Claude</p><script>/* <h2>x</h2> */</script>"
    assert map_html_to_reviews(real) == ("Codex", "Claude")
    example = "Template:\n```md\n## Reviews\n- Codex\n- Claude\n```\n\n## Reviews\nNone yet\n<!-- Codex -->\n"
    assert map_markdown_to_reviews(example) == ()
    assert map_markdown_to_reviews("<!--\n## Reviews\nCodex\n-->\nnothing") is None
