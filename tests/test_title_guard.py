import json
import subprocess
from pathlib import Path

import pytest
from conftest import VENV_BIN, BridgeEnv

from bridge.agent_prefix import strip_agent_prefix
from bridge.enrich import map_node_to_pr
from bridge.install import convert_event_to_command, install_hooks
from bridge.title_guard import find_owner_label_writes, find_prefixed_linear_title, find_prefixed_titles

LABEL = "owner-reviewed"

HEREDOC_COMMIT = """git commit -m "$(cat <<'EOF'
🤖 Claude: Fix the export

🤖 Claude: body line
EOF
)\""""


@pytest.mark.parametrize(
    ("command", "title"),
    [
        ('gh pr create --title "🤖 Claude: Fix x" --body b', "🤖 Claude: Fix x"),
        ("echo '>'&git commit -m '🤖 bad'", "🤖 bad"),
        ('echo ""&gh pr create --title "🤖 x" --body b 2>&1', "🤖 x"),
        ("gh pr create --title='🤖 Fix x'", "🤖 Fix x"),
        ("gh pr edit 7 -t '🤖 Claude: Fix x'", "🤖 Claude: Fix x"),
        ("gh pr edit 7 '-t🤖 Fix x'", "🤖 Fix x"),
        ("gh -R acme/web issue create -t '  🤖 Claude: Bug' -b body", "  🤖 Claude: Bug"),
        ("gh issue edit 3 --title '🤖Bug'", "🤖Bug"),
        ("git commit -m '🤖 Claude: Fix x'", "🤖 Claude: Fix x"),
        ("git commit -am '🤖 Claude: Fix x'", "🤖 Claude: Fix x"),
        ("git commit '-m🤖 Fix x'", "🤖 Fix x"),
        ("git -C ../web commit --message='🤖 Claude: Fix x\n\nbody'", "🤖 Claude: Fix x"),
        ("cd web && git commit -s --message '🤖 Fix x'", "🤖 Fix x"),
        (HEREDOC_COMMIT, "🤖 Claude: Fix the export"),
        ("git merge feat -m '🤖 Claude: Merge feat'", "🤖 Claude: Merge feat"),
        ("gh pr merge 7 --squash --subject '🤖 Claude: Fix x'", "🤖 Claude: Fix x"),
        ("gh api -X PATCH repos/acme/web/pulls/7 -f 'title=🤖 Claude: Fix x'", "🤖 Claude: Fix x"),
        ("gh api repos/acme/web/issues --raw-field=title='🤖 Bug' -f body=x", "🤖 Bug"),
        ("gh api repos/acme/web/issues -F'title=🤖 Bug'", "🤖 Bug"),
        ("gh pr create -dt '🤖 Claude: Fix' -b body", "🤖 Claude: Fix"),
        ("gh pr merge 7 -st '🤖 Claude: Fix'", "🤖 Claude: Fix"),
        ("gh api repos/acme/web/issues -if 'title=🤖 Claude: Fix'", "🤖 Claude: Fix"),
        ("gh issue new -t '🤖 Claude: Bug' -b body", "🤖 Claude: Bug"),
        ("gh pr new -t '🤖 Claude: Fix' -b body", "🤖 Claude: Fix"),
        ("git commit -m $'🤖 Claude: Fix\\n\\nBody'", "🤖 Claude: Fix"),
        ("git commit --mess '🤖 Claude: Fix'", "🤖 Claude: Fix"),
        ("gh api -X PATCH repos/acme/web/pulls/7 -f title='🤖 Fix'", "🤖 Fix"),
    ],
)
def test_a_robot_title_is_found_in_every_spelling(command: str, title: str) -> None:
    assert find_prefixed_titles(command) == [title]


@pytest.mark.parametrize(
    "command",
    [
        "gh pr create --title 'Fix x' --body '🤖 Claude: Fixes the export.'",
        "gh pr comment 7 --body '🤖 Claude: Fixed in 93e.'",
        "gh issue create -t 'Bug' -b '🤖 Claude: Steps'",
        "git commit -m 'Fix x' -m '🤖 Claude: body'",
        "git commit -Fm '🤖 Claude: Fix x'",
        "gh api repos/acme/web/issues/7/comments -f 'body=🤖 Claude: Done'",
        "gh api graphql -f 'query=mutation { x }' -f 'name=🤖'",
        "echo \"gh pr create --title '🤖 Claude: Fix x'\"",
        "gh pr view 7 --json title",
        "gh pr create --title 'Fix 🤖 later' --body x",
        "git commit --fixup HEAD -m '🤖 Claude: Extra fix details'",
        "git commit --squash=HEAD -m '🤖 Claude: More'",
        "gh pr create --title Clean --body '--title=🤖 Claude: example'",
        "gh pr create --title Clean -b '-t🤖 Claude: example'",
        "git commit --author '-m🤖 Claude <a@example.com>' -m Clean",
        "gh api graphql -f 'title=🤖 Claude:' -f 'query=query($title:String!){search(query:$title){issueCount}}'",
        "gh api -X GET search/issues -f 'title=🤖 Claude:'",
        "gh pr merge 7 -m -b '🤖 Claude: merged'",
    ],
)
def test_bodies_and_other_commands_pass(command: str) -> None:
    assert find_prefixed_titles(command) == []


def test_a_linear_issue_title_with_the_robot_is_found_and_a_body_is_not() -> None:
    tool = "mcp__claude_ai_Linear__save_issue"
    assert find_prefixed_linear_title(tool, {"title": "🤖 Claude: Export fails", "team": "PROJ"}) is not None
    assert find_prefixed_linear_title(tool, {"title": "Export fails", "description": "🤖 Claude: Steps"}) is None
    assert find_prefixed_linear_title(tool, {"id": "PROJ-1", "description": "🤖 Claude: Steps"}) is None
    assert find_prefixed_linear_title("mcp__claude_ai_Linear__save_comment", {"title": "🤖 x"}) is None


def test_the_prefix_is_stripped_from_shown_titles() -> None:
    assert strip_agent_prefix("🤖 Claude: Fix the export") == "Fix the export"
    assert strip_agent_prefix("🤖Claude:Fix") == "Fix"
    assert strip_agent_prefix("  🤖 Fix the export") == "Fix the export"
    assert strip_agent_prefix("Fix 🤖 later") == "Fix 🤖 later"
    assert strip_agent_prefix("🤖 Claude:") == "🤖 Claude:", "a title that is only the prefix stays readable"
    assert map_node_to_pr({"title": "🤖 Claude: Fix the export"}).title == "Fix the export"


def _run_guard(bridge_env: BridgeEnv, tmp_path: Path, tool: str, tool_input: dict[str, object]) -> str:
    guard = convert_event_to_command(str(VENV_BIN / "bridge-hook"), "PreToolUse")
    payload = json.dumps(
        {"session_id": "conv-t", "cwd": str(tmp_path), "tool_name": tool, "tool_input": tool_input},
        ensure_ascii=False,
    )
    done = subprocess.run(
        ["/bin/sh", "-c", guard], input=payload, env=bridge_env.env, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_the_hook_denies_a_robot_title_with_no_claim_held(bridge_env: BridgeEnv, tmp_path: Path) -> None:
    assert not (bridge_env.state_dir / "claims-active").exists()

    denied = _run_guard(bridge_env, tmp_path, "Bash", {"command": "gh pr create -t '🤖 Claude: Fix x' -b body"})
    linear = _run_guard(
        bridge_env, tmp_path, "mcp__claude_ai_Linear__save_issue", {"title": "🤖 Claude: Bug", "team": "PROJ"}
    )
    body = _run_guard(bridge_env, tmp_path, "Bash", {"command": "gh pr comment 7 -b '🤖 Claude: Fixed.'"})
    plain = _run_guard(bridge_env, tmp_path, "Bash", {"command": "gh pr create -t 'Fix x' -b '🤖 Claude: Fixes'"})

    output = json.loads(denied)["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "never at the start of a PR, issue or commit title" in output["permissionDecisionReason"]
    assert "Retitle it without the prefix ('Fix x')" in output["permissionDecisionReason"]
    assert json.loads(linear)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert body == "" and plain == ""


def test_a_robot_payload_runs_the_hook_once_with_a_claim_flag_present(bridge_env: BridgeEnv, tmp_path: Path) -> None:
    bridge_env.state_dir.mkdir(parents=True, exist_ok=True)
    (bridge_env.state_dir / "claims-active").touch()
    denied = _run_guard(bridge_env, tmp_path, "Bash", {"command": "gh pr merge 7 --squash --subject '🤖 Claude: Fix'"})
    assert json.loads(denied)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_the_hook_runs_for_an_escaped_robot_too(bridge_env: BridgeEnv, tmp_path: Path) -> None:
    guard = convert_event_to_command(str(VENV_BIN / "bridge-hook"), "PreToolUse")
    payload = json.dumps({"tool_name": "Bash", "cwd": str(tmp_path), "tool_input": {"command": "git commit -m '🤖 x'"}})
    assert "\\ud83e\\udd16" in payload
    done = subprocess.run(
        ["/bin/sh", "-c", guard], input=payload, env=bridge_env.env, capture_output=True, text=True, check=False
    )
    assert json.loads(done.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_install_hooks_matches_the_linear_issue_tool_and_artifact(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    install_hooks(settings, "/home/u/.local/bin/bridge-hook")
    group = json.loads(settings.read_text())["hooks"]["PreToolUse"][0]
    assert group["matcher"] == "Bash|Artifact|Skill|mcp__claude_ai_Linear__save_issue"


@pytest.mark.parametrize(
    "command",
    [
        "gh pr edit 7 --add-label owner-reviewed",
        "gh pr edit https://github.com/acme/web/pull/7 --add-label 'bug,Owner-Reviewed'",
        "gh pr edit 7 --remove-label=owner-reviewed",
        "gh -R acme/web issue edit 7 --add-label owner-reviewed",
        "gh pr create -t Fix -b body -l owner-reviewed",
        "gh issue create --title Bug --label owner-reviewed",
        "gh api repos/acme/web/issues/7/labels -f 'labels[]=owner-reviewed'",
        "gh api -X PUT repos/acme/web/issues/7/labels -f labels[]=bug -f labels[]=owner-reviewed",
        "gh api --method DELETE /repos/acme/web/issues/7/labels/owner-reviewed",
        "gh api -X DELETE repos/{owner}/{repo}/issues/7/labels/owner%2Dreviewed",
        "gh api -X PATCH repos/acme/web/labels/bug -f new_name=owner-reviewed",
        "gh label delete owner-reviewed -R acme/web --yes",
        "gh label edit owner-reviewed --color 000000",
        "gh label edit bug --name owner-reviewed",
        "cd web && gh pr edit 7 --add-label owner-reviewed",
        "gh pr new -t Fix -b body -l owner-reviewed",
        "gh pr create -dl owner-reviewed -t Fix -b body",
        "/opt/homebrew/bin/gh pr edit 7 --add-label owner-reviewed",
        "bash -lc 'gh pr edit 7 --add-label owner-reviewed'",
        'for pr in 7 8; do gh pr edit "$pr" --add-label owner-reviewed; done',
        'gh api repos/acme/web/issues/7/labels -X POST --input - <<\'JSON\'\n{"labels":["owner-reviewed"]}\nJSON',
        "gh api -X PATCH repos/acme/web/issues/7 -f 'labels[]=owner-reviewed'",
        'LABEL=owner-reviewed; gh pr edit 7 --add-label "$LABEL"',
        "gh label create owner-reviewed --force --color 000000",
    ],
)
def test_every_spelling_that_changes_the_owners_label_is_found(command: str) -> None:
    assert find_owner_label_writes(command, LABEL), command


@pytest.mark.parametrize(
    "command",
    [
        "gh pr edit 7 --add-label bug --remove-label wip",
        "gh pr edit 7 --body 'needs owner-reviewed first'",
        "gh pr view 7 --json labels",
        "gh api repos/acme/web/issues/7/labels",
        "gh api repos/acme/web/issues/7/labels -X GET",
        "gh api repos/acme/web/labels -f name=owner-reviewed",
        "gh label create owner-reviewed -R acme/web",
        "gh label list --search owner-reviewed",
        "echo gh pr edit 7 --add-label owner-reviewed",
        "gh api -X PATCH repos/acme/web/labels/bug -f description=owner-reviewed",
        "gh api -X PATCH repos/acme/web/issues/7 -f body=owner-reviewed",
        'gh pr edit 7 --add-label "$BUG"',
    ],
)
def test_other_labels_and_reads_pass(command: str) -> None:
    assert find_owner_label_writes(command, LABEL) == []


def test_with_no_owner_label_configured_nothing_is_refused() -> None:
    assert find_owner_label_writes("gh pr edit 7 --add-label owner-reviewed", "") == []


def test_the_hook_refuses_the_owners_label_with_no_claim_held(bridge_env: BridgeEnv, tmp_path: Path) -> None:
    config = Path(bridge_env.env["BRIDGE_CONFIG"])
    config.write_text(config.read_text() + f'review_owner_label = "{LABEL}"\nowner_name = "Theo"\n')
    assert not (bridge_env.state_dir / "claims-active").exists()

    denied = _run_guard(bridge_env, tmp_path, "Bash", {"command": "gh pr edit 7 --add-label owner-reviewed"})
    short = _run_guard(bridge_env, tmp_path, "Bash", {"command": "gh pr create -t Fix -b b -l owner-reviewed"})
    other = _run_guard(bridge_env, tmp_path, "Bash", {"command": "gh pr edit 7 --add-label bug"})

    reason = json.loads(denied)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "only Theo's Publish sets the label owner-reviewed" in reason
    assert json.loads(short)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert other == ""
