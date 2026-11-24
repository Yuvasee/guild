import json
import subprocess
import time
from pathlib import Path

import pytest

from bridge.config import Config
from bridge.db import connect
from bridge.gbridge import may_need_guard
from bridge.hooks import find_unnamed_cluster_refusal, handle_pre_tool_use
from bridge.install import convert_event_to_command, list_guard_globs

UNNAMED = [
    "kubectl get pods",
    "kubectl -n shop get pods",
    "kubectl get pods --context=",
    "kubectl explain pods",
    "kubectl version",
    "kubectl apply -f deploy.yaml | tee out.txt",
    "KUBECONFIG=~/.kube/other kubectl get nodes",
    "cd /w && kubectl logs web-1",
    "sudo kubectl get nodes",
    "env FOO=1 kubectl get nodes",
    "timeout 30 kubectl get nodes",
    "command kubectl get nodes",
    "/usr/local/bin/kubectl get nodes",
    "if true; then kubectl get nodes; fi",
    "for c in a b; do kubectl get ns; done",
    "ssh box kubectl get pods",
    "ssh -i ~/.ssh/key -o BatchMode=yes box 'cd /x && kubectl get pods'",
    "bash -c 'kubectl get pods'",
    "bash -lc 'helm list'",
    "x=$(kubectl get pods -o name)",
    "kubectl exec web-1 -- kubectl --context shop-prod get pods",
    "helm list",
    "helm upgrade --install web ./chart -n shop",
    "helm --namespace shop status web",
    "helm template web ./chart --validate",
    "helm install web ./chart --context test-cluster",
    "k9s",
    "k9s -n shop",
    "kubectl get pods --context test-cluster --context=",
    "kubectl get pods --token --context=test-cluster",
    "kubectl --cache-dir config get pods",
    "kubectl version --client --client=false",
    "helm template web ./chart --dry-run=server",
    "helm template web ./chart --validate=1",
    "helm install web ./chart --dry-run",
    'echo "$(kubectl get pods)"',
    'pods="$(kubectl get pods -o name)"',
    "pods=`kubectl get pods -o name`",
    'echo "pods: `kubectl get pods`"',
    "ssh box -t kubectl get pods",
    "ssh -ti ~/.ssh/key box kubectl get pods",
    "ssh box bash -s <<'EOF'\nkubectl get pods\nEOF",
    "ssh box <<'EOF'\nkubectl get pods\nEOF",
    "bash <<'EOF'\nhelm list\nEOF",
    "bash << 'EOF'\nkubectl get pods\nEOF",
    "ssh box << EOF\nkubectl get pods\nEOF",
    "bash -o pipefail <<'EOF'\nkubectl get pods\nEOF",
    'echo "$(kubectl --namespace "shop" get pods)"',
    'echo "$(kubectl get pods) isn\'t empty"',
    "kubectl version --token --client",
    "bash <<'EOF' 2>&1\nkubectl get pods\nEOF",
    "ssh box <<'EOF' > out.txt\nkubectl get pods\nEOF",
]
SWITCHES = [
    "kubectl config use-context shop-prod",
    "kubectl --context test-cluster config use-context shop-prod",
    "kubectl config use test-cluster",
    "kubectl config set-context --current --namespace=shop",
    "kubectl config set current-context shop-prod",
    "kubectx shop-prod",
    "kubectx",
    "kubens shop",
    "ssh box kubectx test-cluster",
    "kubectl --cache-dir config config use-context shop-prod",
]
ALLOWED = [
    "kubectl --context=test-cluster get pods",
    "kubectl --context test-cluster get pods",
    "kubectl -n shop --context test-cluster get pods",
    "kubectl get pods -n shop --context=test-cluster",
    "KUBECONFIG=~/.kube/other kubectl --context test-cluster get nodes",
    "ssh box 'kubectl --context test-cluster get pods'",
    "bash -c 'kubectl --context=test-cluster get pods'",
    "kubectl",
    "kubectl config view",
    "kubectl config get-contexts",
    "kubectl config current-context",
    "kubectl config set-context dev --namespace=shop --cluster=test-cluster",
    "kubectl version --client",
    "kubectl completion zsh",
    "kubectl kustomize ./overlay",
    "kubectl get pods --help",
    "kubectl apply -h",
    "kubectl help get",
    "helm template web ./chart",
    "helm lint ./chart",
    "helm package ./chart",
    "helm repo add shop https://charts.example.com",
    "helm dependency update ./chart",
    "helm dep build ./chart",
    "helm show values shop/web",
    "helm pull shop/web",
    "helm create web",
    "helm version",
    "helm search repo web",
    "helm",
    "helm list --help",
    "helm list --kube-context test-cluster",
    "helm --kube-context=shop-prod -n shop status web",
    "helm upgrade --install web ./chart --kube-context test-cluster",
    "k9s --context test-cluster",
    "k9s version",
    "git commit -m 'kubectl get pods now names its cluster'",
    "git commit -m 'Ran kubectl config use-context by mistake; kubectx too'",
    "grep -rn 'kubectl get' docs/",
    "rg 'helm (list|status)' src/",
    "echo kubectl get pods",
    "cat <<'EOF'\nkubectl get pods\nkubectx shop-prod\nEOF",
    "cat <<EOF > run.sh\nhelm list\nEOF",
    "ls ~/.kube  # kubectl get pods",
    "brew install helm kubectl k9s kubectx",
    "kubectl config set-context dev --current=false --namespace=shop",
    "kubectl label --local -f deploy.yaml app=web -o yaml",
    "helm install web ./chart --dry-run=client",
    "git commit -m 'pods=$(kubectl get pods) is refused now'",
    "echo 'run `kubectl get pods`'",
    'echo "$(date) kubectl --context test-cluster"',
    "ssh box 'cat > run.sh' <<'EOF'\nkubectl get pods\nEOF",
    'echo "$(date) kubectl get pods"',
    "echo `date` kubectl get pods `date`",
    'echo "`date "+%F"` kubectl get pods `date`"',
    "bash -n <<'EOF'\nkubectl get pods\nEOF",
    'echo "$(kubectl --context "test-cluster" get pods)"',
    "bash run.sh <<'EOF'\nkubectl get pods\nEOF",
]


@pytest.mark.parametrize("command", UNNAMED)
def test_a_cluster_command_that_names_no_context_is_refused(command: str) -> None:
    refusal = find_unnamed_cluster_refusal(command)

    assert refusal is not None and "--context <cluster>" in refusal and "--kube-context <cluster>" in refusal
    assert may_need_guard(list_guard_globs(), command), "the shell gate must start the guard for it"


@pytest.mark.parametrize("command", SWITCHES)
def test_changing_the_machines_default_cluster_is_refused(command: str) -> None:
    refusal = find_unnamed_cluster_refusal(command)

    assert refusal is not None and "every session" in refusal
    assert may_need_guard(list_guard_globs(), command)


@pytest.mark.parametrize("command", ALLOWED)
def test_a_named_cluster_local_subcommand_or_mention_passes(command: str) -> None:
    assert find_unnamed_cluster_refusal(command) is None


def test_the_hook_denies_it_with_the_reason(tmp_path: Path) -> None:
    conn = connect(tmp_path / "b.db")
    payload: dict[str, object] = {
        "session_id": "c",
        "cwd": "/w",
        "tool_name": "Bash",
        "tool_input": {"command": "kubectl get pods -n shop"},
    }

    decision = json.loads(handle_pre_tool_use(conn, payload, Config()))["hookSpecificOutput"]

    assert decision["permissionDecision"] == "deny"
    assert "--context <cluster>" in decision["permissionDecisionReason"]


@pytest.mark.parametrize(
    "command",
    [
        "printf '%s' '" + "kubectl get pods; " * 3500 + "'",
        "kubectl --context test-cluster get pods " + "-l app=web " * 6000,
        "bash -c : -- -" + "c" * 64000 + "1 kubectl",
        "! " * 32000 + "kubectl --context test-cluster get pods",
        'echo "' + "$(" * 32000 + '"',
        "`" * 64000,
        "ssh box " * 8000 + "<<'EOF'\nkubectl get pods\nEOF",
        "# kubectl\n" + "\n" * 31990 + ": <<E\nE\n" * 4000,
        'echo "' + '$("$(' * 13000 + '"',
        "# kubectl\n" + '"$(" ' * 12798,
    ],
    ids=[
        "quoted",
        "options",
        "shell-flag",
        "keywords",
        "substitutions",
        "backticks",
        "ssh",
        "heredocs",
        "nested",
        "unclosed",
    ],
)
def test_a_64_kb_command_is_read_quickly(command: str) -> None:
    assert len(command) > 60_000
    started = time.monotonic()
    find_unnamed_cluster_refusal(command)
    assert time.monotonic() - started < 1


@pytest.mark.parametrize("command", ["kubectl get pods", "helm list", "kubectx shop-prod", "kubens shop", "k9s"])
def test_the_macs_shell_gate_starts_the_hook_without_a_claim(tmp_path: Path, command: str) -> None:
    hook = tmp_path / "hook"
    hook.write_text("#!/bin/sh\ncat > /dev/null; echo started\n")
    hook.chmod(0o755)
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    gate = convert_event_to_command(str(hook), "PreToolUse")

    done = subprocess.run(
        ["sh", "-c", gate],
        input=payload,
        capture_output=True,
        text=True,
        env={"BRIDGE_STATE_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )

    assert done.stdout.strip() == "started", "no claims-active file: the hook must start anyway"
