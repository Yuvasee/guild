import json
import subprocess
from pathlib import Path

import pytest

from bridge.config import Config
from bridge.db import connect
from bridge.gbridge import may_need_guard
from bridge.guard import make_host_repo_reader
from bridge.hooks import find_rule_refusal, handle_pre_tool_use
from bridge.install import convert_event_to_command, list_guard_globs

CONFIG = Config(assistant_handle="aide")

ADMIN_USES = [
    ("gh pr merge 42 --admin", "gh pr merge --admin"),
    ("gh pr merge 42 --squash --admin -R acme/web", "gh pr merge --admin"),
    ("gh -R acme/web pr merge 42 --admin", "gh pr merge --admin"),
    ("gh pr merge https://github.com/acme/web/pull/42 --admin=true", "gh pr merge --admin"),
    ("gh pr --admin=true merge 42", "gh pr merge --admin"),
    ("gh pr --admin merge 42 -R acme/web", "gh pr merge --admin"),
    ("gh pr merge 42 --admin=false --admin", "gh pr merge --admin"),
    ("gh pr merge 42 --body 'ship it' --admin", "gh pr merge --admin"),
    ("gh pr merge 42 -sd --admin", "gh pr merge --admin"),
    ("cd /w && gh pr merge --admin --squash", "gh pr merge --admin"),
    ("bash -c 'gh pr merge 42 --admin'", "gh pr merge --admin"),
    ("ssh box gh pr merge 42 --admin", "gh pr merge --admin"),
    ("agtermctl session type --target 3 'gh pr merge 42 --admin'", "gh pr merge --admin"),
    ("gh api -X PUT repos/acme/web/pulls/42/merge", "a merge through gh api"),
    ("gh api repos/acme/web/pulls/42/merge -X PUT -f merge_method=squash", "a merge through gh api"),
    ("gh api --method PUT /repos/acme/web/pulls/42/merge-async -f sha=abc", "a merge through gh api"),
    ("gh api graphql -f query='mutation { mergePullRequest(input: {pullRequestId: \"PR_1\"}) { clientMutationId } }'",
     "this GraphQL mutation"),
    ("gh api graphql -f query='mutation { updateRepositoryRuleset(input: {}) { clientMutationId } }'",
     "this GraphQL mutation"),
    ("gh api -X PUT repos/acme/web/rulesets/7 --input off.json", "a ruleset or branch protection change"),
    ("gh api -X DELETE repos/acme/web/rulesets/7", "a ruleset or branch protection change"),
    ("gh api -X DELETE repos/acme/web/branches/main/protection", "a ruleset or branch protection change"),
    ("gh api -X DELETE repos/acme/web/branches/main/protection/enforce_admins",
     "a ruleset or branch protection change"),
    ("gh api -X PUT orgs/acme/rulesets/3 -f enforcement=disabled", "a ruleset or branch protection change"),
]  # fmt: skip
NOT_ADMIN = [
    "gh pr merge 42 --squash",
    "gh pr merge 42 --auto --squash",
    "gh pr merge 42 --admin=false",
    "gh pr merge 42 --admin=true --admin=false",
    "gh pr merge 42 --squash --body '--admin'",
    "gh pr merge 42 -b --admin",
    "gh pr merge 42 -t --admin --squash",
    "gh pr merge 42 --subject --admin",
    "gh pr merge 42 --admin --help",
    "gh pr view 42 --json mergeable",
    "gh api repos/acme/web/pulls/42/merge",
    "gh api repos/acme/web/pulls/42 --jq .mergeable",
    "gh api repos/acme/web/rulesets",
    "gh api repos/acme/web/branches/main/protection",
    "gh api graphql -f query='{ repository(owner: \"acme\", name: \"web\") { rulesets(first: 5) { totalCount } } }'",
    "gh ruleset list -R acme/web",
    "git commit -m 'never gh pr merge --admin'",
    "grep -rn -- --admin docs/",
]


@pytest.mark.parametrize(("command", "what"), ADMIN_USES)
def test_admin_rights_on_github_are_refused_with_the_way_to_ask(command: str, what: str) -> None:
    refusal = find_rule_refusal(command, CONFIG)

    assert refusal is not None and refusal.startswith(f"bridge: {what} can use the owner's admin rights")
    assert "send aide the exact command for a card" in refusal
    assert may_need_guard(list_guard_globs(), command), "the host's gate must start the guard for it"


@pytest.mark.parametrize("command", NOT_ADMIN)
def test_plain_merges_reads_and_mentions_pass(command: str) -> None:
    assert find_rule_refusal(command, CONFIG) is None


def test_the_hook_denies_on_the_mac_and_for_a_host_worker(tmp_path: Path) -> None:
    conn = connect(tmp_path / "b.db")
    payload: dict[str, object] = {
        "session_id": "c", "cwd": "/w", "tool_name": "Bash", "tool_input": {"command": "gh pr merge 42 --admin"},
    }  # fmt: skip
    host = make_host_repo_reader([], ("/home/dev",), "/home/dev")

    for checkout in (None, host):
        decision = json.loads(handle_pre_tool_use(conn, payload, CONFIG, checkout))["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
        assert "admin rights" in decision["permissionDecisionReason"]


@pytest.mark.parametrize("command", [c for c, _ in ADMIN_USES])
def test_the_macs_shell_gate_starts_the_hook_with_no_claim(tmp_path: Path, command: str) -> None:
    hook = tmp_path / "hook"
    hook.write_text("#!/bin/sh\ncat > /dev/null; echo started\n")
    hook.chmod(0o755)
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})

    done = subprocess.run(
        ["sh", "-c", convert_event_to_command(str(hook), "PreToolUse")],
        input=payload,
        capture_output=True,
        text=True,
        env={"BRIDGE_STATE_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )

    assert done.stdout.strip() == "started"
