import time

import pytest

from bridge.review_deps import (
    map_markdown_to_dependencies,
    map_markdown_to_named_prs,
    map_markdown_to_preconditions,
    map_markdown_to_related_prs,
    map_states_to_blockers,
)


def map_markdown_to_dependency(body: str, repo: str, number: int) -> str | None:
    found = map_markdown_to_dependencies(body, repo, number)
    return found[0][0] if len(found) == 1 else None


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
        ("Deploy after #48 merges.", f"{APP}#48"),
        ("This deploys after #48.", f"{APP}#48"),
        ("**Note:** please deploy this PR only after #48 is released.", f"{APP}#48"),
        ("1) Deploy after #48 merges.", f"{APP}#48"),
        ("To deploy, merge this PR after #48.", f"{APP}#48"),
        (
            "4. **After this change and Acme-Org/acme-infra#23 are deployed:** the API accepts only its own app's "
            "tokens.",
            "Acme-Org/acme-infra#23",
        ),
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
        "3. **What switches it on:** `WIDGET_ALLOWED_IDS` in a deployment's environment, set by Acme-Org/acme-infra#23 "
        "and applied by each deployment's next deploy after that PR merges.",
        "The setting is applied by the next deploy after #48 merges.",
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


@pytest.mark.parametrize(
    "body",
    ["_" * 20000 + ":" + " " * 20000 + "x deploy after #48 merges.", "the deploy after " * 3000 + "#48 merges."],
    ids=["one-long-prefix", "many-deploys"],
)
def test_a_long_run_before_a_deploy_is_read_in_linear_time(body: str) -> None:
    started = time.monotonic()
    assert map_markdown_to_dependency(body, APP, 205) is None
    assert time.monotonic() - started < 1


def test_a_dependency_comes_with_the_sentence_that_made_it_cut_to_show() -> None:
    body = "Adds the widgets page.\n\n**Merge only after** #48 is released; it adds the table."
    said = "Merge only after #48 is released; it adds the table."
    assert map_markdown_to_dependencies(body, APP, 205) == ((f"{APP}#48", said),)
    ((_, long),) = map_markdown_to_dependencies("Merge after #48 " + "ships to every region " * 9, APP, 205)
    assert len(long) <= 120 and long.endswith("every\u2026")


def test_each_pr_a_sentence_says_it_merges_after_is_a_dependency_with_its_own_sentence() -> None:
    body = "Depends on #12 and #13.\n\nBlocked by Org/lib#4 (the queue)."
    assert map_markdown_to_dependencies(body, APP, 205) == (
        (f"{APP}#12", "Depends on #12 and #13."),
        (f"{APP}#13", "Depends on #12 and #13."),
        ("Org/lib#4", "Blocked by Org/lib#4 (the queue)."),
    )


@pytest.mark.parametrize(
    "body",
    [
        "Depends on #12 and\n#13.",
        "Depends on #12; also blocked by #13.",
        "Merge after #12 lands.\n- Merge after #13 lands.",
    ],
    ids=["wrapped-line", "two-triggers", "list-items"],
)
def test_every_pr_a_sentence_says_it_merges_after_is_found_however_the_sentence_runs(body: str) -> None:
    assert [ref for ref, _ in map_markdown_to_dependencies(body, APP, 205)] == [f"{APP}#12", f"{APP}#13"]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("## Deployment\nDeploy after #12 merges.", (f"{APP}#12",)),
        ("### What depends on this PR\n#13 (follow-up)", ()),
        ("This no longer depends on #12 landing after #13.", ()),
        ("Merge only after:\nOrg/lib#12", ("Org/lib#12",)),
        ("No longer depends on #12;\nbut still blocked by #13.", (f"{APP}#13",)),
        ("**Depends on #12.**\n#13 depends on this PR.", (f"{APP}#12",)),
        ("No longer depends on #12, which also lands after #13.", ()),
        ("| Action | PR |\n| --- | --- |\n| Merge after CI | |\n| Historical fix | #12 |", ()),
    ],
    ids=[
        "heading-then-line",
        "heading-then-pr",
        "negated-sentence",
        "colon-line",
        "negated-clause",
        "emphasis-end",
        "negated-also",
        "table-rows",
    ],
)
def test_a_heading_and_a_negation_end_what_they_cover(body: str, expected: tuple[str, ...]) -> None:
    assert tuple(ref for ref, _ in map_markdown_to_dependencies(body, APP, 205)) == expected


def test_that_pr_in_a_paragraph_naming_two_is_no_dependency() -> None:
    assert map_markdown_to_dependencies("Related: #8 and #9. This merges after that PR.", APP, 205) == ()


def test_the_wait_on_the_pr_it_merges_after_quotes_its_sentence_and_a_backport_main_pr_quotes_none() -> None:
    must = ("Org/lib#7", "Org/lib#8")
    states: dict[str, str | None] = {"Org/lib#7": "OPEN", "Org/lib#8": "OPEN"}
    assert map_states_to_blockers(must, set(), states, {"org/lib#7": "Merge after Org/lib#7."}) == (
        'waits for Org/lib#7 to merge (its body says: "Merge after Org/lib#7.")',
        "waits for Org/lib#8 to merge",
    )


def test_a_bare_number_in_a_dependency_sentence_is_the_pr_the_body_names_in_full() -> None:
    body = "Companion to Acme-Org/acme-infra#23. Ship only after the release that contains #23."
    assert map_markdown_to_dependency(body, APP, 205) == "Acme-Org/acme-infra#23"


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("OPEN", ("waits for Org/lib#7 to merge",)),
        (None, ("could not read Org/lib#7",)),
        ("MERGED", ()),
        ("CLOSED", ("Org/lib#7 was closed without merging",)),
        ("ISSUE", ()),
    ],
)
def test_a_pr_it_must_merge_after_blocks_until_merged(state: str | None, expected: tuple[str, ...]) -> None:
    assert map_states_to_blockers(("Org/lib#7",), set(), {"org/LIB#7": state}) == expected


def test_a_pr_stacked_on_this_one_blocks_nothing() -> None:
    assert map_states_to_blockers(("Org/app#8",), {"org/app#8"}, {"Org/app#8": "OPEN"}) == ()


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
    assert map_states_to_blockers(named, set(), {f"{APP}#12": "OPEN"}) == (f"waits for {APP}#12 to merge",)
    assert map_states_to_blockers(named, set(), {f"{APP}#12": "MERGED"}) == ()


def test_related_lines_name_prs_by_number_by_repo_and_by_url() -> None:
    body = (
        "Fixes the retry.\n\n"
        "Related: #8 — stacked\n"
        "- **Related:** Org/lib#4, https://github.com/Org/app/pull/9 — independent because they touch other paths\n"
        "Not related: #10\n"
        "See #11.\n"
    )
    assert map_markdown_to_related_prs(body, "Org/app", 7) == ("Org/app#8", "Org/app#9", "Org/lib#4")


def test_a_pr_a_related_line_calls_independent_is_named_but_not_waited_for() -> None:
    body = "Related: #8 — independent because it changes another branch of the parser\nRelated: #9 — stacked\n"
    assert map_markdown_to_named_prs(body, "Org/app", 7) == ("Org/app#9",)
    assert map_markdown_to_related_prs(body, "Org/app", 7) == ("Org/app#8", "Org/app#9")


@pytest.mark.parametrize(
    "body",
    [
        "Related: #8 — not independent because both change the runtime contract",
        "Related: #8 — stacked | independent because …",
        "Related: #8 — stacked; #9 — independent because they touch other paths",
        "Related: #8 — independent",
        "Related: #8 — **not** independent because both change the runtime contract",
        "Related: #8 — independent because it changes another parser branch\nRelated: #8 — stacked",
    ],
    ids=["negated", "template-pasted", "mixed-line", "no-reason", "emphasised-negation", "contradicted"],
)
def test_a_related_line_that_does_not_settle_independence_keeps_the_wait(body: str) -> None:
    assert "Org/app#8" in map_markdown_to_named_prs(body, "Org/app", 7)


def test_an_independent_pr_is_not_waited_for_though_the_body_names_it_elsewhere() -> None:
    body = "Related: #8 — independent because it changes another parser branch.\n\nUnlike #8, this keeps the cache."
    assert map_markdown_to_named_prs(body, "Org/app", 7) == ()
    other_case = "Related: Org/app#8 — independent because it changes another parser branch.\n\nUnlike org/app#8, …"
    assert map_markdown_to_named_prs(other_case, "Org/app", 7) == ()
