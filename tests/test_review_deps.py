import pytest

from bridge.review_deps import map_markdown_to_dependency

DOCS = "Acme-Org/widgets-docs"
APP = "Acme-Org/widgets"

DOCS_3_BODY = """🤖 Claude: PROJ-1. A how-to page for the widgets endpoint (`GET /api/v1/widgets`).

Code: https://github.com/Acme-Org/widgets/pull/7 (draft). Merge this page only after that PR is released.

Covers parameters, an example, paging and errors.
"""


def test_the_docs_page_that_merges_after_its_code_pr_names_it() -> None:
    assert map_markdown_to_dependency(DOCS_3_BODY, DOCS, 3) == "Acme-Org/widgets#7"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Depends on #6.", f"{APP}#6"),
        ("Blocked by Acme-Org/acme-infra#3 (the queue).", "Acme-Org/acme-infra#3"),
        ("Blocked on https://github.com/Acme-Org/acme-infra/pull/3", "Acme-Org/acme-infra#3"),
        ("Merge only after acme-infra#3 is deployed.", "Acme-Org/acme-infra#3"),
        ("Ship after #5 is merged; it adds the table.", f"{APP}#5"),
        ("Do not merge until #5 is released.", f"{APP}#5"),
        ("Merge #5 first.", f"{APP}#5"),
        ("This needs #5 merged.", f"{APP}#5"),
        ("- Part of PROJ-1.\n- Merge after #5 lands.\n- Related: #30", f"{APP}#5"),
        ("Merge this after the code PR is released.\nCode PR: #5", f"{APP}#5"),
        ("Code PR: #5. Merge only after the code PR ships.", f"{APP}#5"),
        ("After https://github.com/Org/lib/pull/12 is merged, merge this PR.", "Org/lib#12"),
        ("Depends on [#12](https://github.com/Org/lib/pull/12).", "Org/lib#12"),
        ("Merge only after:\n- https://github.com/Org/lib/pull/12", "Org/lib#12"),
        ("Do not merge until #5 is released; see #30 for context.", f"{APP}#5"),
    ],
)
def test_a_sentence_that_says_it_merges_after_one_pr_names_it(body: str, expected: str | None) -> None:
    assert map_markdown_to_dependency(body, APP, 20) == expected


@pytest.mark.parametrize(
    "body",
    [
        "Follow-up to #8. Related: #9.",
        "Follow-ups to the review of https://github.com/Acme-Org/widgets/pull/8.",
        "Who would hit it: every deployment on the first release that contains #8, the first time its worker stops.",
        "Merged `main` after #4 landed (squash `abcdef012`).",
        "#11 depends on this one.",
        "Depends on #8 and #9.",
        "Depends on #8.\n\nBlocked by #9.",
        "Merge after review.\n\nRelated: #8",
        "Merge after review; related: #8.",
        "No longer blocked by #8.",
        "This is not blocked on #8 anymore.",
        "Depends on #8.\n\nDepends on #9 and #10.",
        "<!-- Depends on #8 -->\nNothing waits.",
        "```\nDepends on #8\n```",
        "Depends on PROJ-2.",
        "Depends on #20, this PR.",
        "Merge only after https://github.com/Acme-Org/widgets/issues/12 is fixed.",
    ],
)
def test_a_bare_mention_a_reverse_or_an_ambiguous_dependency_names_none(body: str) -> None:
    assert map_markdown_to_dependency(body, APP, 20) is None
