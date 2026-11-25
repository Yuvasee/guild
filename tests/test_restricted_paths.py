import json
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest import mock

import pytest
from test_auto_merge import CLEAN, LIVE
from test_auto_merge import ON as AUTO_ON
from test_auto_merge import run as auto_run
from test_auto_merge import setup as auto_setup
from test_reviews import CONFIG as REVIEW_CONFIG
from test_reviews import REF, add_session, make_pr

from bridge.config import RestrictedPath, map_toml_to_config
from bridge.db import connect, transaction
from bridge.gitinfo import RepoFacts
from bridge.guard import Target, find_merge_blocks
from bridge.hooks import handle_pre_tool_use
from bridge.restricted import is_ok_message
from bridge.review_github import GitHubError
from bridge.reviews import map_db_to_reviews, store_open_prs
from bridge.title_guard import find_restricted_path_refusal

PRICING = RestrictedPath(path="services/pricing/", label="Pricing service", owner="Mira")
CONFIG = replace(REVIEW_CONFIG, restricted_paths=(PRICING,), assistant_handle="aide")
WHY = "## Pricing service: why\nQuotes in euros round to whole cents twice.\nThe rounding sits inside its formatter.\n"


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A clone on dev/x whose one commit over origin/main changes services/pricing/round.py."""
    repo = tmp_path / "app"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    (repo / "README").write_text("app\n")
    git("add", ".")
    git("commit", "-qm", "start")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    git("checkout", "-qb", "dev/x")
    (repo / "services" / "pricing").mkdir(parents=True)
    (repo / "services" / "pricing" / "round.py").write_text("x = 1\n")
    git("add", ".")
    git("commit", "-qm", "round once")
    return repo


def refusal(
    conn: sqlite3.Connection, command: str, directory: Path, conversation: str | None = "c-worker"
) -> str | None:
    facts = RepoFacts(str(directory), "dev/x", {"origin": "Org/app"})
    return find_restricted_path_refusal(conn, CONFIG, command, str(directory), conversation, lambda _d: facts)


def body_file(tmp_path: Path, text: str) -> str:
    path = tmp_path / "body.md"
    path.write_text(text)
    return str(path)


def send(conn: sqlite3.Connection, sender: str, body: str, to: str, conversation: str | None = None) -> int:
    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO messages (created_at, from_conversation, from_handle, kind, body) "
            "VALUES (0, 'x', ?, 'info', ?)",
            (sender, body),
        )
        message_id = int(cursor.lastrowid or 0)
        conn.execute(
            "INSERT INTO message_recipients (message_id, conversation_id, handle) VALUES (?, ?, ?)",
            (message_id, conversation or f"c-{to}", to),
        )
    return message_id


def test_a_pr_changing_no_restricted_path_passes(tmp_path: Path, checkout: Path) -> None:
    conn = connect(tmp_path / "t.db")
    other = replace(PRICING, path="services/billing/")
    config = replace(CONFIG, restricted_paths=(other,))
    facts = RepoFacts(str(checkout), "dev/x", {"origin": "Org/app"})
    command = "gh pr create -t Round -b 'Rounds once'"
    assert find_restricted_path_refusal(conn, config, command, str(checkout), "c", lambda _d: facts) is None
    assert refusal(conn, "gh pr create -t Round -b x", tmp_path) is None, "not a checkout: git cannot tell"


def test_a_restricted_path_without_its_why_section_is_refused(tmp_path: Path, checkout: Path) -> None:
    conn = connect(tmp_path / "t.db")
    refused = refusal(conn, "gh pr create -t Round -b 'Rounds once'", checkout)
    assert refused == (
        "bridge: this PR changes services/pricing/ (Pricing service, Mira's team) and its body has no section "
        "'Pricing service: why'. Ask aide for an OK with a why of at most 5 lines (what breaks, why it cannot be done "
        "outside Pricing service), then put it under 'Pricing service: why' and 'OK: #N' in the body."
    )


def test_a_why_of_six_lines_is_refused(tmp_path: Path, checkout: Path) -> None:
    conn = connect(tmp_path / "t.db")
    ok = send(conn, "aide", "OK for dev/x", "worker")
    long_why = "## Pricing service: why\n" + "".join(f"line {n}\n\n" for n in range(6)) + f"OK: #{ok}\n"
    refused = refusal(conn, f"gh pr create -t Round -F {body_file(tmp_path, long_why)}", checkout)
    assert refused is not None and "'Pricing service: why' has 6 lines, more than 5" in refused
    five = "## Pricing service: why\n" + "".join(f"line {n}\n" for n in range(5)) + "<details>\nlong\n</details>\n"
    assert refusal(conn, f"gh pr create -t Round -F {body_file(tmp_path, five + f'OK: #{ok}')}", checkout) is None
    empty = refusal(
        conn, f"gh pr create -t Round -F {body_file(tmp_path, f'## Pricing service: why\nOK: #{ok}')}", checkout
    )
    assert empty is not None and "is empty" in empty


def test_a_why_without_the_assistants_ok_is_refused(tmp_path: Path, checkout: Path) -> None:
    conn = connect(tmp_path / "t.db")
    no_line = refusal(conn, f"gh pr create -t Round -F {body_file(tmp_path, WHY)}", checkout)
    assert no_line is not None and "has no 'OK: #N', N aide's bridge message" in no_line
    from_a_lead = send(conn, "eli", "OK for dev/x", "worker")
    not_ok = send(conn, "aide", "Not yet: say what breaks in dev/x", "worker")
    to_someone_else = send(conn, "aide", "OK", "nora")
    for message in (from_a_lead, not_ok, to_someone_else, 999):
        command = f"gh pr create -t Round -F {body_file(tmp_path, WHY + f'OK: #{message}')}"
        assert refusal(conn, command, checkout) is not None, message


def test_a_why_with_the_assistants_ok_passes(tmp_path: Path, checkout: Path) -> None:
    conn = connect(tmp_path / "t.db")
    naming_branch = send(conn, "aide", "OK for dev/x, the rounding fix", "eli")
    assert (
        refusal(conn, f"gh pr create -t Round -F {body_file(tmp_path, WHY + f'OK: #{naming_branch}')}", checkout)
        is None
    )
    with transaction(conn):
        add_session(conn, "c-worker", "worker")
        add_session(conn, "c-eli", "eli")
        conn.execute(
            "INSERT INTO leads (name, display_name, scope, linear_refs, tint, surface, created_at) "
            "VALUES ('eli', 'Eli', '', '[]', '', 's-eli', 0)"
        )
        conn.execute("INSERT INTO tab_parents (agterm_session, parent_session) VALUES ('tab-worker', 'tab-eli')")
    to_the_lead = send(conn, "aide", "ok, go ahead", "eli")
    to_the_worker = send(conn, "aide", "OK", "worker")
    for message in (to_the_lead, to_the_worker):
        command = f"gh pr create -t Round -F {body_file(tmp_path, WHY + f'OK: #{message}')}"
        assert refusal(conn, command, checkout) is None, message
        assert refusal(conn, command, checkout, conversation=None) is not None, "no session: only the branch counts"


def test_an_edit_of_the_body_is_judged_by_the_queues_files(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.db")
    with transaction(conn):
        store_open_prs(conn, [make_pr(files=("services/pricing/round.py", "app/x.py"))], 500)
    ok = send(conn, "aide", "OK for dev/x", "worker")
    refused = refusal(conn, "gh pr edit 7 --body 'Rounds once'", tmp_path)
    assert refused is not None and "no section 'Pricing service: why'" in refused
    assert refusal(conn, f"gh pr edit 7 -F {body_file(tmp_path, WHY + f'OK: #{ok}')}", tmp_path) is None
    assert refusal(conn, "gh pr edit 7 --title Round", tmp_path) is None, "the body stays as it was"
    reviews = map_db_to_reviews(conn, CONFIG, 1000)["prs"]
    assert isinstance(reviews, list) and reviews[0]["restricted"] == ["Pricing service: needs Mira's team"]


def test_the_hook_denies_with_the_refusal(tmp_path: Path, checkout: Path) -> None:
    conn = connect(tmp_path / "t.db")
    payload: dict[str, object] = {
        "session_id": "c", "cwd": str(checkout), "tool_name": "Bash",
        "tool_input": {"command": "gh pr create -t Round -b 'Rounds once'"},
    }  # fmt: skip
    decision = json.loads(handle_pre_tool_use(conn, payload, CONFIG))["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert decision["permissionDecisionReason"].startswith("bridge: this PR changes services/pricing/")


def test_config_reads_restricted_paths() -> None:
    raw: dict[str, object] = {
        "restricted_paths": [{"path": "services/pricing/", "label": "Pricing service", "owner": "Mira"}]
    }
    assert map_toml_to_config(raw).restricted_paths == (PRICING,)
    with pytest.raises(ValueError, match="restricted_paths"):
        map_toml_to_config({"restricted_paths": [{"path": "../x", "label": "X", "owner": "Mira"}]})
    assert PRICING.covers("services/pricing/a.py") and not PRICING.covers("services/pricing2/a.py")


def test_what_the_guard_reads_beyond_a_plain_body(tmp_path: Path, checkout: Path) -> None:
    conn = connect(tmp_path / "t.db")
    fill = refusal(conn, "gh pr create --fill", checkout)
    assert fill is not None and "no section 'Pricing service: why'" in fill, "a body filled from commits is not read"
    assert refusal(conn, "gh pr create -t Round -b x --head someone:dev/x", checkout) is None, "a fork's branch"
    subprocess.run(["git", "-C", str(checkout), "reset", "-q", "--soft", "origin/main"], check=True)
    staged = refusal(conn, "git commit -qm round && gh pr create -t Round -b x", checkout)
    assert staged is not None, "the commit earlier in the command is not made when the hook reads"
    quoted = checkout / "services" / "pricing" / "é.py"
    quoted.write_text("y\n")
    subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
    subprocess.run(["git", "-C", str(checkout), "rm", "-q", "--cached", "services/pricing/round.py"], check=True)
    assert refusal(conn, "gh pr create -t Round -b x", checkout) is not None, "a name git would quote"


def test_a_section_ends_at_the_next_why_and_a_hash_is_not_a_heading(tmp_path: Path, checkout: Path) -> None:
    conn = connect(tmp_path / "t.db")
    ok = send(conn, "aide", "OK for dev/x", "worker")
    merged = f"Pricing service: why\nBilling: why\nIt breaks.\nOK: #{ok}\n"
    empty = refusal(conn, f"gh pr create -t Round -F {body_file(tmp_path, merged)}", checkout)
    assert empty is not None and "is empty" in empty
    numbered = f"## Pricing service: why\n#9 fails today without it.\nOK: #{ok}\n"
    assert refusal(conn, f"gh pr create -t Round -F {body_file(tmp_path, numbered)}", checkout) is None
    huge = WHY + "OK: #99999999999999999999\n"
    assert refusal(conn, f"gh pr create -t Round -F {body_file(tmp_path, huge)}", checkout) is not None


def test_bad_files_in_the_queue_read_as_none(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.db")
    with transaction(conn):
        store_open_prs(conn, [make_pr(files=("services/pricing/round.py",))], 500)
    for stored in ("null", "{", '{"a": 1}'):
        with transaction(conn):
            conn.execute("UPDATE review_prs SET files = ?", (stored,))
        assert refusal(conn, "gh pr edit 7 --body x", tmp_path) is None, stored
    config = map_toml_to_config({"restricted_paths": [{"path": "./services//pricing/", "label": "P", "owner": "M"}]})
    assert config.restricted_paths[0].path == "services/pricing/"


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ("OK", True),
        ("ok, go ahead with dev/x", True),
        ("OK for dev/x: the rounding has to change there", True),
        ("not OK", False),
        ("This isn't OK yet", False),
        ("Not yet. OK once the tests pass", False),
        ("No, ask the pricing team. OK?", False),
        ("hold this one; OK otherwise", False),
        ("Looks fine", False),
    ],
)
def test_only_an_ok_no_word_turns_around_counts(text: str, ok: bool) -> None:
    assert is_ok_message(text) is ok


def test_a_negated_ok_does_not_open_the_pr(tmp_path: Path, checkout: Path) -> None:
    conn = connect(tmp_path / "t.db")
    not_ok = send(conn, "aide", "dev/x is not OK", "worker")
    command = f"gh pr create -t Round -F {body_file(tmp_path, WHY + f'OK: #{not_ok}')}"
    assert refusal(conn, command, checkout) is not None


MERGE_FACTS = (False, True, "SUCCESS", "CLEAN", 0, "a1", (), ())


def merge_waits(conn: sqlite3.Connection, body: str | Exception) -> tuple[str, ...]:
    fetch = mock.Mock(side_effect=body) if isinstance(body, Exception) else mock.Mock(return_value=body)
    with (
        mock.patch("bridge.review_github.fetch_merge_facts", return_value=MERGE_FACTS),
        mock.patch("bridge.review_github.fetch_pr_body", fetch),
    ):
        refusals = find_merge_blocks(conn, CONFIG, [Target(REF, "gh pr merge")], 1000)
    return tuple(w for r in refusals for w in r.waits if "another team's code" in w or "Pricing service" in w)


def test_the_merge_guard_refuses_a_restricted_pr_until_its_body_has_why_and_ok(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.db")
    with transaction(conn):
        store_open_prs(conn, [make_pr(files=("services/pricing/round.py",), approved_by=("mira",))], 500)
        conn.execute("INSERT OR REPLACE INTO review_poll (id, ok_at) VALUES (1, 1000)")
        add_session(conn, "c-worker", "worker", branch="dev/x")
    [no_why] = merge_waits(conn, "Rounds once")
    assert "has no section 'Pricing service: why'" in no_why and not no_why.endswith(".")
    to_worker = send(conn, "aide", "OK", "worker")
    assert merge_waits(conn, WHY + f"OK: #{to_worker}") == (), "sent to the PR's owner, a host worker or not"
    not_ok = send(conn, "aide", "not OK", "worker")
    assert len(merge_waits(conn, WHY + f"OK: #{not_ok}")) == 1
    [unread] = merge_waits(conn, GitHubError("gh failed: timeout"))
    assert "could not be read (gh failed: timeout)" in unread
    with transaction(conn):
        conn.execute("UPDATE review_prs SET files = ?", (json.dumps(["app/x.py"]),))
    with mock.patch("bridge.review_github.fetch_pr_body") as fetch:
        assert merge_waits(conn, "x") == ()
    fetch.assert_not_called()


def test_the_merge_guard_reads_a_restricted_prs_body_once_for_its_waits_and_its_ok(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.db")
    with transaction(conn):
        store_open_prs(conn, [make_pr(files=("services/pricing/round.py",), approved_by=("mira",))], 500)
        conn.execute("INSERT OR REPLACE INTO review_poll (id, ok_at) VALUES (1, 1000)")
        conn.execute(
            """UPDATE review_prs SET dep_blockers = ?, must_merge = '["Org/lib#3"]', reviewed_at = 600""",
            (json.dumps(['waits for Org/lib#3 to merge (its body says: "Merge after Org/lib#3.")']),),
        )
        add_session(conn, "c-worker", "worker", branch="dev/x")
    ok = send(conn, "aide", "OK", "worker")
    fetch = mock.Mock(side_effect=[WHY + f"OK: #{ok}", GitHubError("gh failed: timeout")])
    with (
        mock.patch("bridge.review_github.fetch_merge_facts", return_value=MERGE_FACTS),
        mock.patch("bridge.review_github.fetch_pr_body", fetch),
    ):
        assert find_merge_blocks(conn, CONFIG, [Target(REF, "gh pr merge")], 1000) == []
    assert fetch.call_count == 1


def test_auto_merge_holds_a_restricted_pr_until_its_body_has_why_and_ok(tmp_path: Path) -> None:
    on = replace(AUTO_ON, restricted_paths=(PRICING,))
    conn = auto_setup(tmp_path, make_pr(**CLEAN, files=("services/pricing/round.py",)))
    assert auto_run(conn, config=on, live=replace(LIVE, body="Rounds once")).merges == []
    ok = send(conn, "aide", "OK for dev/x", "lead-x")
    assert len(auto_run(conn, config=on, live=replace(LIVE, body=WHY + f"OK: #{ok}")).merges) == 1


def test_an_ok_follows_the_conversation_it_went_to_not_a_handle_passed_on(tmp_path: Path, checkout: Path) -> None:
    conn = connect(tmp_path / "t.db")
    with transaction(conn):
        add_session(conn, "c-worker", "worker")
        add_session(conn, "c-before", "nora", surface="s-elsewhere")
    to_old_holder = send(conn, "aide", "OK", "worker", conversation="c-before")
    command = f"gh pr create -t Round -F {body_file(tmp_path, WHY + f'OK: #{to_old_holder}')}"
    assert refusal(conn, command, checkout) is not None
