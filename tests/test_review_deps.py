import pytest

from bridge.review_deps import map_markdown_to_dependency, map_markdown_to_named_prs, map_states_to_blockers

DOCS = "Acme-Org/acme-platform-docs"
APP = "Acme-Org/monorepo"

DOCS_22_BODY = """🤖 Claude: PROJ-40. A page for the new widgets endpoint (`GET /api/v1/widgets` and `/widgets/{id}`), which replaces the old `GET /api/v1/items`.

Code: https://github.com/Acme-Org/monorepo/pull/50 (draft). Merge this page only after that PR is released.

Covers authentication, parameters, an example request, fields, paging and errors.

Not built locally; the CI build checks the links.
"""  # noqa: E501


def test_the_docs_page_that_merges_after_its_code_pr_names_it() -> None:
    assert map_markdown_to_dependency(DOCS_22_BODY, DOCS, 22) == "Acme-Org/monorepo#50"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Depends on #49.", f"{APP}#49"),
        ("Blocked by Acme-Org/acme-infra#23 (the queue).", "Acme-Org/acme-infra#23"),
        ("Blocked on https://github.com/Acme-Org/acme-infra/pull/23", "Acme-Org/acme-infra#23"),
        ("Merge only after acme-infra#23 is deployed.", "Acme-Org/acme-infra#23"),
        ("Ship after #48 is merged; it adds the table.", f"{APP}#48"),
        ("Do not merge until #48 is released.", f"{APP}#48"),
        ("Merge #48 first.", f"{APP}#48"),
        ("This needs #48 merged.", f"{APP}#48"),
        ("- Part of PROJ-1.\n- Merge after #48 lands.\n- Related: #999", f"{APP}#48"),
        ("Merge this after the code PR is released.\nCode PR: #48", f"{APP}#48"),
        ("Code PR: #48. Merge only after the code PR ships.", f"{APP}#48"),
        ("After https://github.com/Org/lib/pull/12 is merged, merge this PR.", "Org/lib#12"),
        ("Depends on [#12](https://github.com/Org/lib/pull/12).", "Org/lib#12"),
        ("Merge only after:\n- https://github.com/Org/lib/pull/12", "Org/lib#12"),
        ("Do not merge until #48 is released; see #999 for context.", f"{APP}#48"),
        ("Ship together with or after the release that contains #48.", f"{APP}#48"),
    ],
)
def test_a_sentence_that_says_it_merges_after_one_pr_names_it(body: str, expected: str | None) -> None:
    assert map_markdown_to_dependency(body, APP, 205) == expected


@pytest.mark.parametrize(
    "body",
    [
        "Follow-up to #45. Related: #46.",
        "Follow-ups to the review of https://github.com/Acme-Org/monorepo/pull/45.",
        "Who would hit it: every deployment on the first release that contains #45, the first time its worker stops.",
        "Merged `main` after #29 landed (squash `abcdef012`).",
        "#52 depends on this one.",
        "Depends on #45 and #46.",
        "Depends on #45.\n\nBlocked by #46.",
        "Merge after review.\n\nRelated: #45",
        "Merge after review; related: #45.",
        "No longer blocked by #45.",
        "This is not blocked on #45 anymore.",
        "Depends on #45.\n\nDepends on #46 and #47.",
        "<!-- Depends on #45 -->\nNothing waits.",
        "```\nDepends on #45\n```",
        "Depends on PROJ-27.",
        "Depends on #205, this PR.",
        "Merge only after https://github.com/Acme-Org/monorepo/issues/12 is fixed.",
        "Merge acme-infra#23 together with or after the release containing this PR.",
    ],
)
def test_a_bare_mention_a_reverse_or_an_ambiguous_dependency_names_none(body: str) -> None:
    assert map_markdown_to_dependency(body, APP, 205) is None


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Goes with Acme-Org/acme-infra#23.", ("Acme-Org/acme-infra#23",)),
        (
            "Companion to Acme-Org/acme-infra#23.\n\nShip together with or after the release that contains #23.",
            ("Acme-Org/acme-infra#23",),
        ),
        ("Follow-up to #45. Related: https://github.com/Org/lib/pull/7", (f"{APP}#45", "Org/lib#7")),
        ("This is #205, the same as Acme-Org/monorepo#205.", ()),
        ("Org/lib#3 and Org/web#3 both touch it; see #3.", ("Org/lib#3", "Org/web#3")),
        ("<!-- template: goes with #45 -->\nNothing else.", ()),
        ("No PR named, only PROJ-27.", ()),
    ],
)
def test_every_pr_a_body_names_is_found_and_a_bare_number_takes_its_full_name(
    body: str, expected: tuple[str, ...]
) -> None:
    assert map_markdown_to_named_prs(body, APP, 205) == expected


def test_a_bare_number_in_a_dependency_sentence_is_the_pr_the_body_names_in_full() -> None:
    body = "Companion to Acme-Org/acme-infra#23. Ship only after the release that contains #23."
    assert map_markdown_to_dependency(body, APP, 205) == "Acme-Org/acme-infra#23"


@pytest.mark.parametrize(
    ("state", "strict", "expected"),
    [
        ("OPEN", False, ("waits for Org/lib#7 to merge",)),
        (None, False, ("could not read Org/lib#7",)),
        ("MERGED", False, ()),
        ("CLOSED", False, ()),
        ("ISSUE", False, ()),
        ("OPEN", True, ("waits for Org/lib#7 to merge",)),
        ("CLOSED", True, ("Org/lib#7 was closed without merging",)),
        ("ISSUE", True, ()),
    ],
)
def test_a_named_pr_blocks_while_open_or_unread_and_one_it_must_merge_after_until_merged(
    state: str | None, strict: bool, expected: tuple[str, ...]
) -> None:
    named = ("Org/lib#7",)
    assert map_states_to_blockers(named, named if strict else (), set(), {"org/LIB#7": state}) == expected


def test_a_pr_stacked_on_this_one_blocks_nothing() -> None:
    assert map_states_to_blockers(("Org/app#8",), (), {"org/app#8"}, {"Org/app#8": "OPEN"}) == ()
