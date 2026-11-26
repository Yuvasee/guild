import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bridge.config import Config, map_toml_to_config
from bridge.db import connect
from bridge.gbridge import may_need_guard
from bridge.guard import make_host_repo_reader, segment_command
from bridge.hooks import find_rule_refusal, handle_pre_tool_use
from bridge.install import convert_event_to_command, list_guard_globs

PROD = Config(prod_kube_contexts=("acme-prod", "*-production"), assistant_handle="aide")

SECRET_READS = [
    "kubectl --context test-cluster get secret web -o yaml",
    "kubectl --context test-cluster get secrets -n shop -o json",
    "kubectl --context test-cluster get secret web -ojsonpath='{.data.password}'",
    "kubectl --context test-cluster get secret/web -o=go-template='{{.data}}'",
    "kubectl --context test-cluster get -o yaml secret web",
    "kubectl --context test-cluster get secret web --output=json",
    "kubectl --context test-cluster get secret web --output json | jq .data",
    "kubectl --context test-cluster get secret web --template='{{.data.token}}'",
    "kubectl --context test-cluster get configmap,secret -o yaml",
    "kubectl --context test-cluster get secrets.v1 -o custom-columns=V:.data.password",
    "kubectl --context test-cluster get secret web -o jsonpath-file=/tmp/p",
    "env KUBECONFIG=/k kubectl --context test-cluster get secret web -o yaml",
    "timeout 30 kubectl --context test-cluster get secret web -o yaml",
    "sh -c 'kubectl --context test-cluster get secret web -o yaml'",
    "ssh box kubectl --context test-cluster get secret web -o json",
    'echo "$(kubectl --context test-cluster get secret web -o jsonpath={.data.key})"',
    "kubectl --context test-cluster -o json get secrets",
    "kubectl --context test-cluster get secrets -Aoyaml",
    "kubectl --context test-cluster get secrets -A -o yaml",
    "kubectl --context test-cluster get --sort-by .metadata.name secrets -o yaml",
    "kubectl --context test-cluster get pod/web secret/db -o json",
    "kubectl --context test-cluster exec web -- kubectl get secret web -o yaml",
    "kubectl --context test-cluster exec web -- sh -c 'kubectl get secret web -o yaml'",
    "env -u KUBECONFIG kubectl --context test-cluster get secret web -o yaml",
    "nice -n 5 kubectl --context test-cluster get secret web -o yaml",
    "kubectl --context test-cluster get -Ao yaml secrets",
    "kubectl --context test-cluster get -o yaml -- secrets",
    "kubectl --context test-cluster get --raw=/api/v1/namespaces/default/secrets",
    "kubectl --context test-cluster get --raw /api/v1/namespaces/shop/secrets/db",
    "2>/dev/null kubectl --context test-cluster get secret web -o yaml",
    "sudo env kubectl --context test-cluster get secret web -o yaml",
    "kubectl --context test-cluster get --raw /api/v1/secrets",
    "cat > /tmp/web.yaml <<EOF\n$(kubectl --context test-cluster get secret web -o yaml)\nEOF",
    "cat > /tmp/web.yaml <<EOF\nkey: `kubectl --context test-cluster get secret web -o jsonpath={.data.k}`\nEOF",
]
SECRET_SAFE = [
    "kubectl --context test-cluster describe secret web",
    "kubectl --context test-cluster get secrets",
    "kubectl --context test-cluster get secret web -o name",
    "kubectl --context test-cluster get secrets -o wide",
    "kubectl --context test-cluster get pods -o yaml",
    "kubectl --context test-cluster get configmap web -o json",
    "kubectl --context test-cluster get configmap secret -o json",
    "kubectl --context test-cluster get pods -n secret -o yaml",
    "kubectl --context test-cluster exec web -- cat /etc/hostname",
    "kubectl --context test-cluster get -Ao yaml pods",
    "kubectl --context test-cluster get --raw /api/v1/namespaces/default/pods",
    "kubectl --context test-cluster get -o name -- secrets",
    "kubectl --context test-cluster get --raw /api/v1/namespaces/secrets/pods",
    "kubectl --context test-cluster get --raw /apis/secrets-store.csi.x-k8s.io/v1",
    "kubectl --context test-cluster get secret web -o yaml --help",
    "cat > notes.md <<'EOF'\n$(kubectl --context test-cluster get secret web -o yaml)\nEOF",
    "cat > notes.md <<EOF\n\\$(kubectl --context test-cluster get secret web -o yaml)\nEOF",
    "git commit -m 'kubectl get secret web -o yaml is refused now'",
    "echo kubectl get secret web -o yaml",
]
CLAUDE_LOGIN = [
    ("claude logout", "log Claude out"),
    ("claude auth logout", "log Claude out"),
    ("env HOME=/tmp/x claude logout", "log Claude out"),
    ("claude --model sonnet auth logout", "log Claude out"),
    ("claude --debug hooks auth logout", "log Claude out"),
    ('2>"/dev/null" claude auth logout', "log Claude out"),
    ("env -u CLAUDECODE security delete-generic-password -s 'Claude Code-credentials'", "keychain"),
    ("env -i PATH=/usr/bin security delete-generic-password -s 'Claude Code-credentials'", "keychain"),
    ("security delete-generic-password -s 'Claude Code-credentials'", "keychain"),
    ("security add-generic-password -U -s 'Claude Code-credentials' -w x", "keychain"),
    ("security find-generic-password -s 'Claude Code-credentials' -w", "keychain"),
    ("agtermctl session type --target 3 '/logout'", "/logout"),
    ("agtermctl session type --target 3 $'/logout\\r'", "/logout"),
    ("tmux send-keys -t work:1 '/' 'logout' Enter", "/logout"),
    ("tmux send-keys -t work:1 C-u '/logout' Enter", "/logout"),
    ("tmux send-keys -t work:1 '/logout' Enter", "/logout"),
    ("bash -c \"tmux send-keys -t w '/logout' Enter\"", "/logout"),
]
CLAUDE_LOGIN_SAFE = [
    "claude --version",
    "claude -p 'what does logout do'",
    "claude -p logout",
    "claude auth logout --help",
    "claude --model sonnet doctor",
    "security find-generic-password -s github.com",
    "agtermctl session type --target 3 'git status'",
    "agtermctl session type --target 3 'grep /logout app.log'",
    "tmux send-keys -t work:1 'grep /logout app.log' Enter",
    "tmux send-keys -t work:1 'ls' Enter",
    "tmux display-message '/logout'",
    "grep -rn /logout docs/",
    "echo claude logout",
]
PERMISSION_PROBES = [
    "aws iam simulate-principal-policy --policy-source-arn role-web --action-names s3:GetObject",
    "aws --profile ro iam simulate-custom-policy --policy-input-list file://p.json --action-names s3:*",
    "AWS_PROFILE=ro aws iam simulate-principal-policy --policy-source-arn x --action-names y",
    "env -u AWS_PROFILE aws iam simulate-principal-policy --policy-source-arn x --action-names y",
    "env -i aws iam simulate-principal-policy --policy-source-arn x --action-names y",
    "command -p aws iam simulate-principal-policy --policy-source-arn x --action-names y",
    "env -uAWS_PROFILE aws iam simulate-principal-policy --policy-source-arn x --action-names y",
    "/usr/bin/env aws iam simulate-principal-policy --policy-source-arn x --action-names y",
    "2>/dev/null aws iam simulate-principal-policy --policy-source-arn x --action-names y",
    '2>"$LOG" aws iam simulate-principal-policy --policy-source-arn x --action-names y',
    "aws --profile ro --region eu-west-1 iam simulate-principal-policy --policy-source-arn x --action-names y",
]
AWS_READS = [
    "aws iam get-role --role-name web",
    "aws sts get-caller-identity",
    "aws s3 ls s3://acme-bucket",
    "echo aws iam simulate-principal-policy",
    "aws iam get-role --role-name simulate-deploy",
    "aws iam list-role-policies --role-name simulate-ci",
    "aws iam simulate-principal-policy help",
]
CHAINED_APPLIES = [
    "kubectl --context test-cluster create secret generic web --from-literal=k=v && kubectl --context test-cluster "
    "get pods",
    "cd deploy; kubectl --context test-cluster apply -f web.yaml",
    "kubectl --context test-cluster apply -f a.yaml || echo failed",
    "kubectl --context test-cluster apply -f a.yaml\nkubectl --context test-cluster apply -f b.yaml",
    "helm upgrade web ./chart --kube-context test-cluster && helm --kube-context test-cluster status web",
    "helm install web ./chart --kube-context test-cluster; echo done",
    "bash -c 'cd /w && kubectl --context test-cluster apply -f web.yaml'",
    "kubectl --context test-cluster apply -f a.yaml & kubectl --context test-cluster apply -f b.yaml",
    "sh -c 'kubectl --context test-cluster apply -f web.yaml' && echo done",
    "echo start; ssh box 'kubectl --context test-cluster apply -f web.yaml'",
    "kubectl --context test-cluster -f web.yaml apply && echo done",
    "helm --timeout 5m upgrade web ./chart --kube-context test-cluster && echo done",
    "kubectl --context test-cluster exec web -- kubectl --context test-cluster apply -f web.yaml && echo done",
    "kubectl --context test-cluster 2>/dev/null apply -f web.yaml && echo done",
    'kubectl --context test-cluster 2>"/dev/null" apply -f web.yaml && echo done',
]
LONE_APPLIES = [
    "kubectl --context test-cluster apply -f web.yaml",
    "kubectl --context test-cluster create secret generic web --from-literal=k=v",
    "kubectl --context test-cluster create secret generic web --dry-run=client -o yaml | kubectl --context "
    "test-cluster apply -f -",
    "kubectl --context test-cluster apply -f - <<'EOF'\napiVersion: v1\nkind: Namespace\nEOF",
    "helm upgrade --install web ./chart --kube-context test-cluster 2>&1 | tee helm.log",
    "kubectl --context test-cluster get pods && kubectl --context test-cluster describe pod web",
    "kubectl --context test-cluster create configmap web --from-file=a && echo ok",
    "sh -c 'kubectl --context test-cluster apply -f web.yaml' | tee apply.log",
    "ssh box 'kubectl --context test-cluster apply -f web.yaml'",
]
PROD_WRITES = [
    ("kubectl --context acme-prod -n shop exec deploy/web -c app -- true", "exec"),
    ("kubectl --context=acme-prod cp web-1:/tmp/x ./x", "cp"),
    ("kubectl --context acme-prod port-forward svc/db 5432", "port-forward"),
    ("kubectl --context acme-prod run tmp --image=busybox", "run"),
    ("kubectl --context acme-prod debug web-1 --image=busybox", "debug"),
    ("kubectl --context acme-prod attach web-1", "attach"),
    ("kubectl --context acme-prod edit deploy web", "edit"),
    ("kubectl --context acme-prod delete pod web-1", "delete"),
    ("kubectl --context acme-prod scale deploy web --replicas=0", "scale"),
    ("kubectl --context acme-prod apply -f web.yaml", "apply"),
    ("kubectl -n shop --context acme-prod patch deploy web -p '{}'", "patch"),
    ("kubectl --context eu-production delete pod web-1", "delete"),
    ("helm upgrade web ./chart --kube-context acme-prod", "upgrade"),
    ("helm install web ./chart --kube-context=acme-prod", "install"),
    ("helm --kube-context acme-prod uninstall web", "uninstall"),
    ("helm --kube-context acme-prod rollback web 3", "rollback"),
    ("timeout 60 kubectl --context acme-prod exec web-1 -- true", "exec"),
    ("env FOO=1 kubectl --context acme-prod delete pod web-1", "delete"),
    ("sh -c 'kubectl --context acme-prod exec web-1 -- true'", "exec"),
    ("ssh box 'kubectl --context acme-prod exec web-1 -- true'", "exec"),
    ("kubectl --context acme-prod --output name delete pod web", "delete"),
    ("kubectl --context acme-prod -f web.yaml apply", "apply"),
    ("helm --timeout 5m upgrade web ./chart --kube-context acme-prod", "upgrade"),
    ("env -u KUBECONFIG kubectl --context acme-prod delete pod web-1", "delete"),
    ("kubectl --context test-cluster exec web-1 -- kubectl --context acme-prod delete pod x", "delete"),
    ("kubectl --context acme-prod create namespace scratch", "create"),
    ("kubectl --context acme-prod replace -f web.yaml", "replace"),
    ("kubectl --context acme-prod label deployment/web version=2", "label"),
    ("kubectl --context acme-prod rollout restart deployment/web", "rollout"),
    ("helm --kube-context acme-prod test web", "test"),
    ("/usr/bin/env kubectl --context acme-prod delete pod web", "delete"),
    ("sudo env kubectl --context acme-prod delete pod web", "delete"),
    ("2>/dev/null kubectl --context acme-prod delete pod web", "delete"),
    ("kubectl --context acme-prod 2>/dev/null delete pod web", "delete"),
    ('2>"/dev/null" kubectl --context acme-prod delete pod web', "delete"),
    ("kubectl --context acme-prod rollout undo deployment/web", "rollout"),
    ("kubectl --context acme-prod rollout pause deployment/web", "rollout"),
    ("kubectl --context acme-prod auth reconcile -f rbac.yaml", "auth"),
    ("cat > /tmp/x <<EOF\n$(kubectl --context acme-prod delete pod web)\nEOF", "delete"),
]
UNREAD_CONTEXTS = [
    'CTX=acme-prod; kubectl --context "$CTX" delete pod web',
    'KUBECONFIG=/tmp/k kubectl --context "$EMPTY" delete pod web',
    "kubectl --context=$CTX exec web-1 -- true",
    'helm upgrade web ./chart --kube-context "$(cat ctx)"',
]
PROD_READS = [
    "kubectl --context acme-prod -n shop get pods",
    "kubectl --context acme-prod describe deploy web",
    "kubectl --context acme-prod logs web-1 -f",
    "kubectl --context acme-prod top pods",
    "kubectl --context acme-prod explain pods",
    "kubectl --context acme-prod api-resources",
    "kubectl --context acme-prod auth can-i delete pods",
    "kubectl --context acme-prod events -n shop",
    "kubectl --context acme-prod wait --for=condition=Ready pod/web",
    "kubectl --context acme-prod diff -f web.yaml",
    "kubectl --context acme-prod cluster-info",
    "kubectl --context acme-prod get pods 2>/dev/null",
    "kubectl --context acme-prod rollout status deployment/web --timeout=60s",
    "kubectl --context acme-prod rollout history deployment/web",
    "kubectl --context acme-prod auth whoami",
    'kubectl --context acme-prod 2>"$LOG" get pods -o wide',
    'kubectl --context acme-prod 2>"/dev/null" logs -f deploy/web',
    "kubectl --context acme-prod delete pod web --help",
    "kubectl --context acme-prod rollout restart -h",
    "helm --kube-context acme-prod history web",
    "helm --kube-context acme-prod get values web",
    "helm --kube-context acme-prod diff upgrade web ./chart",
    "kubectl --context acme-prod delete --help",
    "kubectl --context acme-staging exec web-1 -- true",
    "kubectl --context acme-prod-copy delete pod web-1",
    "helm --kube-context acme-prod status web",
    'kubectl --context "$CTX" get pods',
    "helm --kube-context acme-prod list",
    "helm upgrade web ./chart --kube-context acme-staging",
]


@pytest.mark.parametrize("command", SECRET_READS)
def test_reading_secret_values_is_refused_with_the_rule(command: str) -> None:
    refusal = find_rule_refusal(command, Config())

    assert refusal is not None and "Never read secret values" in refusal and "kubectl describe secret" in refusal
    assert may_need_guard(list_guard_globs(), command), "the shell gate must start the guard for it"


@pytest.mark.parametrize("command", SECRET_SAFE)
def test_secret_key_names_and_other_resources_pass(command: str) -> None:
    assert find_rule_refusal(command, Config()) is None


@pytest.mark.parametrize(("command", "what"), CLAUDE_LOGIN)
def test_logging_claude_out_or_touching_its_keychain_entry_is_refused(command: str, what: str) -> None:
    refusal = find_rule_refusal(command, Config())

    assert refusal is not None and "one login serves every session" in refusal and what in refusal
    assert may_need_guard(list_guard_globs(), command)


@pytest.mark.parametrize("command", CLAUDE_LOGIN_SAFE)
def test_other_claude_keychain_and_typing_commands_pass(command: str) -> None:
    assert find_rule_refusal(command, Config()) is None


@pytest.mark.parametrize("command", PERMISSION_PROBES)
def test_an_aws_permission_probe_is_refused(command: str) -> None:
    refusal = find_rule_refusal(command, Config())

    assert refusal is not None and "excludes permission probes" in refusal and "AccessDenied" in refusal
    assert may_need_guard(list_guard_globs(), command)


@pytest.mark.parametrize("command", AWS_READS)
def test_other_aws_reads_pass(command: str) -> None:
    assert find_rule_refusal(command, Config()) is None


@pytest.mark.parametrize("command", CHAINED_APPLIES)
def test_a_secret_or_cluster_apply_chained_with_other_steps_is_refused(command: str) -> None:
    refusal = find_rule_refusal(command, Config())

    assert refusal is not None and "runs alone" in refusal


@pytest.mark.parametrize("command", LONE_APPLIES)
def test_an_apply_run_alone_or_piped_and_other_chains_pass(command: str) -> None:
    assert find_rule_refusal(command, Config()) is None


@pytest.mark.parametrize(("command", "verb"), PROD_WRITES)
def test_a_production_context_allows_only_reads(command: str, verb: str) -> None:
    refusal = find_rule_refusal(command, PROD)

    assert refusal is not None and "production context" in refusal and f" {verb} there" in refusal
    assert "get, describe, logs or top" in refusal and "send aide the exact command" in refusal
    assert "'!'" not in refusal and "hook" not in refusal
    assert may_need_guard(list_guard_globs(), command)


@pytest.mark.parametrize("command", PROD_READS)
def test_reads_on_production_and_writes_elsewhere_pass(command: str) -> None:
    assert find_rule_refusal(command, PROD) is None


@pytest.mark.parametrize("command", UNREAD_CONTEXTS)
def test_a_write_on_a_context_the_shell_fills_in_is_refused(command: str) -> None:
    refusal = find_rule_refusal(command, PROD)

    assert refusal is not None and "Name the context literally" in refusal
    assert find_rule_refusal(command, Config()) is None, "with no production contexts there is nothing to protect"


def test_a_redirection_with_a_quoted_target_leaves_the_words_and_a_quoted_operator_stays_one() -> None:
    redirected: list[list[str]] = []

    segments = segment_command("""echo '2>x' 2\\>y "2>"z 2>"$LOG" 2>'/dev/null' >"out" done""", redirections=redirected)

    assert segments == [(["echo", "2>x", "2>y", "2>z", "done"], ";")]
    assert redirected == [["2>$LOG", "2>/dev/null", ">out"]]


def test_with_no_production_contexts_configured_nothing_is_refused_as_production() -> None:
    assert find_rule_refusal("kubectl --context acme-prod delete pod web-1", Config()) is None


def test_production_contexts_come_from_config() -> None:
    config = map_toml_to_config({"prod_kube_contexts": ["acme-prod", "*-production"]})

    assert config.prod_kube_contexts == ("acme-prod", "*-production")
    with pytest.raises(ValueError, match="prod_kube_contexts"):
        map_toml_to_config({"prod_kube_contexts": "acme-prod"})


def _payload(command: str) -> dict[str, object]:
    return {"session_id": "c", "cwd": "/w", "tool_name": "Bash", "tool_input": {"command": command}}


def test_the_hook_denies_on_the_mac_and_for_a_host_worker(tmp_path: Path) -> None:
    conn = connect(tmp_path / "b.db")
    command = "kubectl --context acme-prod -n shop exec deploy/web -- true"
    host = make_host_repo_reader([], ("/home/dev",), "/home/dev")

    for checkout in (None, host):
        output = handle_pre_tool_use(conn, _payload(command), PROD, checkout)
        decision = json.loads(output)["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
        assert "production context" in decision["permissionDecisionReason"]


def test_a_rule_refusal_comes_before_the_unnamed_cluster_one(tmp_path: Path) -> None:
    conn = connect(tmp_path / "b.db")

    output = handle_pre_tool_use(conn, _payload("kubectl get secret web -o yaml"), Config())

    assert "Never read secret values" in json.loads(output)["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize(
    "command",
    [
        "claude logout",
        "security delete-generic-password -s 'Claude Code-credentials'",
        "agtermctl session type --target 3 /logout",
        "tmux send-keys -t work:1 '/' 'logout' Enter",
        "aws iam simulate-principal-policy --policy-source-arn x --action-names y",
    ],
)
def test_the_macs_shell_gate_starts_the_hook_for_the_new_rules(tmp_path: Path, command: str) -> None:
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


@pytest.mark.parametrize(
    "command",
    [
        "rg /logout src",
        "grep -R 'security.*Claude' docs",
        "git log --oneline | grep logout",
        "rg 'simulate' docs",
        "cat ~/.claude/settings.json",
    ],
)
def test_searching_for_the_rules_words_starts_no_python(tmp_path: Path, command: str) -> None:
    hook = tmp_path / "hook"
    hook.write_text("#!/bin/sh\ncat > /dev/null; echo started\n")
    hook.chmod(0o755)
    payload = json.dumps({
        "session_id": "c", "transcript_path": "/home/dev/.claude/projects/-w/c.jsonl", "cwd": "/w",
        "tool_name": "Bash", "tool_input": {"command": command},
    })  # fmt: skip

    done = subprocess.run(
        ["sh", "-c", convert_event_to_command(str(hook), "PreToolUse")],
        input=payload,
        capture_output=True,
        text=True,
        env={"BRIDGE_STATE_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )

    assert done.stdout.strip() == "", "the Mac's gate must not start Python for it"
    assert not may_need_guard(list_guard_globs(), command), "nor the host's"


def test_the_rule_checks_load_no_module_beyond_the_hooks_own() -> None:
    commands = [*SECRET_READS, *[c for c, _ in CLAUDE_LOGIN], *PERMISSION_PROBES, *CHAINED_APPLIES, *LONE_APPLIES]
    code = (
        "import json, sys, bridge.hooks as h; from bridge.config import Config; before = set(sys.modules); "
        "[h.find_rule_refusal(c, Config(prod_kube_contexts=('acme-prod',))) for c in json.loads(sys.argv[1])]; "
        "print(' '.join(sorted(set(sys.modules) - before)))"
    )

    done = subprocess.run(
        [sys.executable, "-c", code, json.dumps(commands)], capture_output=True, text=True, check=True
    )

    assert done.stdout.split() == []


@pytest.mark.parametrize(
    "command",
    [
        "kubectl --context test-cluster get secret " + "-l app=web " * 6000,
        "kubectl --context acme-prod get pods; " * 1800,
        "bash -c : -- -" + "c" * 64000 + "1 kubectl",
    ],
    ids=["options", "segments", "shell-flag"],
)
def test_a_64_kb_command_is_read_quickly(command: str) -> None:
    assert len(command) > 60_000
    started = time.monotonic()
    find_rule_refusal(command, PROD)
    assert time.monotonic() - started < 1


TYPED_INTO_TAB = [
    "tmux send-keys -t work:1 '!kubectl --context acme-prod delete pod web-1' Enter",
    "tmux send-keys -t work:1 '!' 'kubectl --context acme-prod delete pod web-1' Enter",
    "tmux send-keys -t work:1 '!'",
    "tmux send-keys -t work:1 C-u '! ls' Enter",
    "agtermctl session type --target 3 '! kubectl --context acme-prod delete pod web-1'",
    "tmux send-keys -t work:1 'kubectl --context acme-prod delete pod web-1' Enter",
    "agtermctl session type --target 3 $'kubectl --context acme-prod delete pod web-1\\r'",
]


@pytest.mark.parametrize("command", TYPED_INTO_TAB)
def test_typing_a_bang_or_a_refused_command_into_a_tab_is_refused(command: str) -> None:
    assert find_rule_refusal(command, PROD) is not None
    assert may_need_guard(list_guard_globs(), command)


@pytest.mark.parametrize(
    "command",
    [
        "tmux send-keys -t work:1 'echo done!' Enter",
        "agtermctl session type --target 3 'kubectl --context acme-prod get pods'",
        "tmux send-keys -t work:1 'git status' Enter",
    ],
)
def test_typing_a_read_or_plain_text_into_a_tab_passes(command: str) -> None:
    assert find_rule_refusal(command, PROD) is None
