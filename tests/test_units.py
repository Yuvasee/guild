import json
import subprocess
from pathlib import Path

from bridge.gitinfo import read_git_info
from bridge.install import install_hooks, uninstall_hooks
from bridge.refs import extract_branch_refs, extract_refs, map_ref_to_label
from bridge.registry import convert_tab_name_to_handle, map_agterm_tree_to_tabs
from bridge.restore import build_restore_command


def test_extract_refs_from_prose_with_configured_prefixes() -> None:
    text = (
        "Fix abc-12 and ABC-345; see https://github.com/acme/web/pull/31 and acme/api#77. "
        "PR #35 is green, #12 too. UTF-8 and SHA-25 are not issues; color &#12; is not a PR."
    )
    refs = extract_refs(text, issue_prefixes=("ABC",), default_repo="acme/web")
    assert refs == [
        "pr:acme/web#31",
        "pr:acme/api#77",
        "pr:acme/web#35",
        "pr:acme/web#12",
        "issue:ABC-12",
        "issue:ABC-345",
    ]


def test_extract_refs_generic_issue_pattern_skips_common_acronyms() -> None:
    refs = extract_refs("XYZ-9, UTF-8, ISO-8601, RFC-7231", issue_prefixes=(), default_repo=None)
    assert refs == ["issue:XYZ-9"]


def test_github_issue_urls_and_single_digit_prs() -> None:
    refs = extract_refs("https://github.com/acme/web/issues/12 and PR #5", issue_prefixes=(), default_repo="acme/web")
    assert refs == ["gh-issue:acme/web#12", "pr:acme/web#5"]


def test_bare_numbers_need_a_repo() -> None:
    assert extract_refs("PR #35", issue_prefixes=(), default_repo=None) == []


def test_branch_refs_are_case_insensitive() -> None:
    assert extract_branch_refs("dev/abc-37-cache-fix", ("ABC",)) == ["issue:ABC-37"]


def test_restore_command_replaces_resume_and_drops_prompt() -> None:
    argv = [
        "/usr/local/bin/claude",
        "--model",
        "opus",
        "--dangerously-skip-permissions",
        "--resume",
        "old-id",
        "--add-dir",
        "/a",
        "/b",
        "--fork-session",
        "fix the bug please",
    ]
    command = build_restore_command(argv, "new-id")
    assert command == (
        "/usr/local/bin/claude --model opus --dangerously-skip-permissions --add-dir /a /b --resume new-id"
    )


def test_restore_command_drops_continue_and_positional_prompt() -> None:
    argv = ["/bin/claude", "-c", "--permission-mode=plan", "do things", "--effort", "high"]
    assert build_restore_command(argv, "abc") == ("/bin/claude --permission-mode=plan --effort high --resume abc")


def test_restore_command_bare_resume_picker() -> None:
    assert build_restore_command(["/bin/claude", "-r"], "x") == "/bin/claude --resume x"


def test_git_info_in_repo_and_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "remote", "add", "origin", "git@github.com:acme/web.git")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    _git(repo, "worktree", "add", "-q", "-b", "feature/x", str(tmp_path / "wt"))
    (repo / "sub").mkdir()

    main = read_git_info(str(repo / "sub"))
    worktree = read_git_info(str(tmp_path / "wt"))

    assert main is not None and worktree is not None
    assert (main.repo_root, main.branch, main.repo) == (str(repo), "main", "acme/web")
    assert (worktree.repo_root, worktree.branch, worktree.repo) == (
        str(tmp_path / "wt"),
        "feature/x",
        "acme/web",
    )
    assert read_git_info(str(tmp_path)) is None


def test_install_hooks_preserves_foreign_hooks_and_is_idempotent(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    foreign = {"hooks": [{"type": "command", "command": "/x/agterm-agent-status.sh active"}]}
    settings.write_text(json.dumps({"model": "opus", "hooks": {"Stop": [foreign]}}))

    install_hooks(settings, "/home/u/.local/bin/bridge-hook")
    install_hooks(settings, "/home/u/.local/bin/bridge-hook")
    installed = json.loads(settings.read_text())

    assert installed["model"] == "opus"
    assert installed["hooks"]["Stop"][0] == foreign
    assert len(installed["hooks"]["Stop"]) == 2
    assert installed["hooks"]["Stop"][1]["hooks"][0]["command"] == "/home/u/.local/bin/bridge-hook Stop"
    assert set(installed["hooks"]) == {
        "SessionStart", "UserPromptSubmit", "Stop", "SessionEnd", "PostToolUse", "PostToolUseFailure",
        "Notification", "PreToolUse",
    }  # fmt: skip
    assert installed["hooks"]["Notification"][0]["matcher"] == "permission_prompt"
    assert installed["hooks"]["PostToolUse"][0]["hooks"][0]["command"].endswith(
        "exec /home/u/.local/bin/bridge-hook PostToolUse; exit 0"
    )

    uninstall_hooks(settings)
    assert json.loads(settings.read_text()) == {"model": "opus", "hooks": {"Stop": [foreign]}}


def test_agterm_tree_maps_panes_to_tab_names() -> None:
    tree = {
        "ok": True,
        "result": {
            "tree": {
                "workspaces": [
                    {
                        "name": "AUX",
                        "sessions": [
                            {"name": "HQ", "surfaces": [{"paneID": "P1"}, {"paneID": "P2"}]},
                            {"name": "(P12) Data", "surfaces": [{"paneID": "P3"}]},
                        ],
                    }
                ]
            }
        },
    }
    tabs = map_agterm_tree_to_tabs(tree)
    assert {pane: (t.tab_name, t.workspace_name) for pane, t in tabs.items()} == {
        "P1": ("HQ", "AUX"),
        "P2": ("HQ", "AUX"),
        "P3": ("(P12) Data", "AUX"),
    }
    assert convert_tab_name_to_handle("(P12) Data") == "p12-data"
    assert convert_tab_name_to_handle("✨") == "session"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_repo_labels_are_four_letters_without_the_org_prefix() -> None:
    assert map_ref_to_label("pr:Acme-Org/acme-infra#21", {}) == "infr#21"
    assert map_ref_to_label("pr:Acme-Org/acme-helm-charts#12", {}) == "helm#12"
    assert map_ref_to_label("pr:Acme-Org/monorepo#36", {}) == "mono#36"
    assert map_ref_to_label("pr:Acme-Org/monorepo#1", {"Acme-Org/monorepo": "core"}) == "core#1"
    assert map_ref_to_label("issue:PROJ-1", {}) == "PROJ-1"
