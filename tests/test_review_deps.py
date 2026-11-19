import pytest

from bridge.review_deps import (
    map_markdown_to_dependency,
    map_markdown_to_named_prs,
    map_markdown_to_preconditions,
    map_states_to_blockers,
)

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


@pytest.mark.parametrize(
    ("body", "sentence"),
    [
        (
            "Docs for the warp drive.\n\nMerge after the warp drive is deployed.",
            "Merge after the warp drive is deployed.",
        ),
        ("Merge once the flag is on in prod", "Merge once the flag is on in prod"),
        ("- Merge when the vendor confirms the quota.", "Merge when the vendor confirms the quota."),
        ("Please merge this only after the freeze ends.", "Please merge this only after the freeze ends."),
        ("Merge only if the canary stays green.", "Merge only if the canary stays green."),
        ("**Do not merge until the migration has run.**", "Do not merge until the migration has run."),
        ("**Merge** only\nwhen the API is deployed.", "Merge only when the API is deployed."),
        ("Docs.\n\nDO NOT\nMERGE", "DO NOT MERGE"),
        ("**DO NOT MERGE.** Related: #12.", "DO NOT MERGE."),
        ("> Warning: do not merge before the freeze ends.", "Warning: do not merge before the freeze ends."),
        ("Only merge after the vendor\u2019s go-ahead.", "Only merge after the vendor\u2019s go-ahead."),
        ("Don't merge before Friday.", "Don't merge before Friday."),
        ("Don\u2019t merge this until legal signs off.", "Don\u2019t merge this until legal signs off."),
        ("Never merge until the backfill ends.", "Never merge until the backfill ends."),
        ("## DO NOT MERGE", "DO NOT MERGE"),
        ("Hold off merging: the API is not out yet.", "Hold off merging: the API is not out yet."),
        ("Hold off on merging for now.", "Hold off on merging for now."),
        ("Keep unmerged until after the night deploy.", "Keep unmerged until after the night deploy."),
        ("Keep this PR unmerged for now.", "Keep this PR unmerged for now."),
        ("Don't merge yet: the bucket is not there.", "Don't merge yet: the bucket is not there."),
        ("Docs.\n\nguild: hold", "guild: hold"),
        ("Merge after #12 is deployed.", "Merge after #12 is deployed."),
        (
            "Do not merge until Acme-Org/acme-infra#23 is released.",
            "Do not merge until Acme-Org/acme-infra#23 is released.",
        ),
        ("Merge after the API change in\n#12 is deployed.", "Merge after the API change in #12 is deployed."),
        (
            "**Merge gate:** merge only after the operator runs a build that includes #7.",
            "Merge gate: merge only after the operator runs a build that includes #7.",
        ),
    ],
)
def test_each_merge_precondition_in_a_body_is_a_blocker_quoting_its_sentence(body: str, sentence: str) -> None:
    assert map_markdown_to_preconditions(body, APP, 7) == (
        f'its body says: "{sentence}" (edit the body once that is done)',
    )


@pytest.mark.parametrize(
    "body",
    [
        "Merge once https://github.com/Acme-Org/acme-infra/pull/23 is out.",
        "<!-- Do not merge until the API is out. -->Docs for the API.",
        "```\ngit commit -m 'do not merge'\n```",
        "We merged the parser after review.",
        "The bot merges it after CI passes.",
        "Enables auto-merge when the queue is green.",
        "Adds a do-not-merge label.",
        "The records merge when their keys match.",
        "Hold the old export until the new one is out.",
        "We keep unmerged branches for a week.",
        "Duplicate records do not merge automatically.",
        "",
    ],
)
def test_a_sentence_naming_a_pr_hidden_or_not_an_instruction_holds_nothing_in_words(body: str) -> None:
    assert map_markdown_to_preconditions(body, APP, 7) == ()


def test_a_sentence_naming_only_this_pr_still_holds_it_and_a_long_one_is_cut() -> None:
    assert map_markdown_to_preconditions("Do not merge #7 until the API is out.", APP, 7) == (
        'its body says: "Do not merge #7 until the API is out." (edit the body once that is done)',
    )
    (long,) = map_markdown_to_preconditions("Merge after " + "the warp drive " * 20 + "ships.", APP, 7)
    shown = long.split('"')[1]
    assert len(shown) == 120 and shown.endswith("\u2026")


def test_merge_after_a_pr_stays_a_dependency_that_its_merge_clears() -> None:
    body = "Merge after #12 is deployed."
    named = map_markdown_to_named_prs(body, APP, 7)
    assert named == (f"{APP}#12",) and map_markdown_to_dependency(body, APP, 7) == f"{APP}#12"
    assert map_states_to_blockers(named, named, set(), {f"{APP}#12": "OPEN"}) == (f"waits for {APP}#12 to merge",)
    assert map_states_to_blockers(named, named, set(), {f"{APP}#12": "MERGED"}) == ()
