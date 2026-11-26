import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from bridge.board import MessageStateError, Party
from bridge.config import Config, map_toml_to_config
from bridge.db import connect, transaction
from bridge.feed import COMMAND_OPTIONS, WAIVER_MS, add_post, answer_post, reply_on_card
from bridge.gbridge import may_need_guard
from bridge.hooks import find_rule_refusal, handle_pre_tool_use
from bridge.install import convert_event_to_command, list_guard_globs

PROD = Config(
    prod_kube_contexts=("acme-prod",),
    prod_kube_writable_namespaces=("staging", "pr-*"),
    assistant_handle="aide",
    owner_name="sam",
)

WRITABLE = [
    "kubectl --context acme-prod -n staging delete pod web-1",
    "kubectl --context acme-prod -n staging delete pods web-1 web-2 --grace-period 0",
    "kubectl --context acme-prod -n staging delete -- pod web",
    "kubectl --context acme-prod --namespace pr-12 rollout restart deployment/web",
    "kubectl --context acme-prod -n staging rollout restart deploy web",
    "kubectl --context acme-prod -n staging rollout undo deployments.apps web --to-revision 3",
    "kubectl --context acme-prod -n staging rollout restart statefulset/db daemonset.apps/agent",
    "kubectl --context acme-prod -nstaging scale deploy web --replicas=0",
    "kubectl --context acme-prod -n staging scale deploy/web --replicas 2",
    "kubectl --context acme-prod -n staging set image deployment/web app=registry.io/web:2",
    "kubectl --context acme-prod -n staging set env deploy/web --containers app LOG=debug OLD-",
    "kubectl --context acme-prod -n staging set resources deployment/web --containers app --limits cpu=200m",
    "kubectl --context acme-prod --namespace=staging apply -f web.yaml",
    "kubectl --context acme-prod -n staging apply -f a.yaml -f b.yaml",
    "kubectl --context acme-prod -n staging apply -f k8s/deploy.yaml -f k8s/service.yaml",
    "kubectl --context acme-prod -n=pr-7 apply --filename=k8s/x.yaml",
    "helm upgrade web ./chart --kube-context acme-prod -n pr-12",
    "helm upgrade --install web ./chart --kube-context acme-prod --namespace staging",
    "helm --kube-context acme-prod -n staging rollback web 3",
]
NOT_WRITABLE = [
    "kubectl --context acme-prod delete pod web-1",
    "kubectl --context acme-prod -n shop delete pod web-1",
    "kubectl --context acme-prod -n staging -n shop delete pod web-1",
    'kubectl --context acme-prod -n "$NS" delete pod web-1',
    "kubectl --context acme-prod -n pr-$N delete pod web-1",
    "kubectl --context acme-prod -n staging delete pod web-1 -A",
    "kubectl --context acme-prod -An staging delete pods --all",
    "kubectl --context acme-prod -n staging delete pods --all --all-namespaces",
    "kubectl --context acme-prod -n staging delete namespace pr-3",
    "kubectl --context acme-prod -n staging create ns pr-4",
    "kubectl --context acme-prod -n staging label node/ip-10-0-0-1 x=y",
    "kubectl --context acme-prod -n staging delete clusterrolebinding admin",
    "kubectl --context acme-prod -n staging delete clusterissuers.cert-manager.io le",
    "kubectl --context acme-prod -n staging delete crd widgets.acme.io",
    "kubectl --context acme-prod -n staging patch pv data -p '{}'",
    "kubectl --context acme-prod -n staging delete pod,storageclass web",
    "kubectl --context acme-prod -n staging drain ip-10-0-0-1",
    "kubectl --context acme-prod -n staging cp ./config.yaml billing/web-0:/app/config.yaml",
    "kubectl --context acme-prod -n staging delete --raw=/api/v1/namespaces/billing",
    "kubectl --context acme-prod -n staging delete -- namespace billing",
    "kubectl --context acme-prod -n staging delete volumesnapshotcontents backup-1",
    "kubectl --context acme-prod -n staging delete deviceclasses gpu",
    "kubectl --context acme-prod -n staging delete deviceclasses/gpu",
    "kubectl --context acme-prod -n staging debug node/ip-10-0-0-1 --image=busybox",
    "kubectl --context acme-prod -n staging debug --image busybox node/worker-1 -- true",
    "kubectl --context acme-prod -n staging debug pod/web-0 node/worker-1 --image=busybox -- true",
    "kubectl --context acme-prod -n staging debug web-0 --image=busybox",
    "kubectl --context acme-prod -n staging proxy",
    "kubectl --context acme-prod -n staging apply -f staging.yaml --prune -l app=web",
    "kubectl --context acme-prod -n staging apply -f s.yaml --prune --prune-allowlist=core/v1/Namespace -l app=web",
    "kubectl --context acme-prod -n staging apply -k overlays/staging",
    "kubectl --context acme-prod -n staging delete roles.iam.aws.upbound.io payments-worker",
    "kubectl --context acme-prod -n staging rollout restart deployments.apps.example.io/web",
    "kubectl --context acme-prod -n staging delete pod web -f ns.yaml",
    "kubectl --context acme-prod -n staging delete pod/web",
    "kubectl --context acme-prod -n staging scale -f web.yaml --replicas 2",
    "kubectl --context acme-prod -n staging delete deploy web",
    "kubectl --context acme-prod -n staging create secret generic web --from-literal=a=b",
    "kubectl --context acme-prod -n staging label pods web app=web",
    "kubectl --context acme-prod -n staging patch deploy web -p '{}'",
    "kubectl --context acme-prod -n staging set image cronjob/web app=registry.io/web:2",
    "kubectl --context acme-prod -n staging exec web-1 -- true",
    "kubectl --context acme-prod -n staging cp ./config.yaml web-0:/app/config.yaml",
    "kubectl --context acme-prod -n staging rollout pause deployment/web",
    "helm --kube-context acme-prod --namespace staging uninstall web",
    "helm upgrade web ./chart --kube-context acme-prod",
    "helm upgrade web ./chart --kube-context acme-prod -n shop",
]


@pytest.mark.parametrize("command", WRITABLE)
def test_a_write_in_a_writable_namespace_named_literally_passes(command: str) -> None:
    assert find_rule_refusal(command, PROD) is None
    assert find_rule_refusal(command, Config(prod_kube_contexts=("acme-prod",))) is not None


@pytest.mark.parametrize("command", NOT_WRITABLE)
def test_a_write_without_a_literal_writable_namespace_or_on_a_cluster_wide_kind_is_refused(command: str) -> None:
    refusal = find_rule_refusal(command, PROD)

    assert refusal is not None and "production context" in refusal
    assert "matches staging, pr-*, a deploy passes" in refusal and "send aide the exact command" in refusal


def test_writable_namespaces_come_from_config() -> None:
    config = map_toml_to_config({"prod_kube_writable_namespaces": ["staging", "pr-*"]})

    assert config.prod_kube_writable_namespaces == ("staging", "pr-*")
    assert "Writes pass" not in str(find_rule_refusal(NOT_WRITABLE[0], Config(prod_kube_contexts=("acme-prod",))))


DEPLOY = "helm upgrade web ./chart --kube-context acme-prod -n shop"


def _post(conn: sqlite3.Connection, command: str = DEPLOY) -> int:
    with transaction(conn):
        return add_post(
            conn, Party("conv-aide", "aide"), "decision", "Deploy web?", "", [], list(COMMAND_OPTIONS), 1000,
            command=command,
        )  # fmt: skip


def _decide(conn: sqlite3.Connection, command: str) -> str:
    payload: dict[str, object] = {
        "session_id": "c",
        "cwd": "/w",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }
    output = handle_pre_tool_use(conn, payload, PROD)
    return json.loads(output)["hookSpecificOutput"]["permissionDecision"] if output else "allow"


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    now = [10_000]
    monkeypatch.setattr("bridge.hooks.now_ms", lambda: now[0])
    return now


def test_an_approved_command_runs_once_from_any_session_and_only_with_its_exact_text(
    tmp_path: Path, clock: list[int]
) -> None:
    conn = connect(tmp_path / "b.db")
    item = _post(conn, "helm  upgrade web ./chart\n --kube-context acme-prod -n shop")
    assert _decide(conn, DEPLOY) == "deny", "unanswered: no waiver"
    with transaction(conn):
        reply_on_card(conn, item, "Run it", "sam", clock[0])

    assert _decide(conn, DEPLOY + " --wait") == "deny"
    assert _decide(conn, DEPLOY.replace("shop", "billing")) == "deny"
    assert _decide(conn, "  " + DEPLOY.replace(" -n", "   -n")) == "allow"
    assert _decide(conn, DEPLOY) == "deny", "a second run"


def test_an_expired_waiver_is_refused(tmp_path: Path, clock: list[int]) -> None:
    conn = connect(tmp_path / "b.db")
    item = _post(conn)
    with transaction(conn):
        reply_on_card(conn, item, "Run it", "sam", clock[0])
    clock[0] += WAIVER_MS + 1

    assert _decide(conn, DEPLOY) == "deny"


@pytest.mark.parametrize("answer", ["Don't run", "run it, but tomorrow"])
def test_another_answer_arms_nothing(tmp_path: Path, clock: list[int], answer: str) -> None:
    conn = connect(tmp_path / "b.db")
    item = _post(conn)
    with transaction(conn):
        reply_on_card(conn, item, answer, "sam", clock[0])

    assert _decide(conn, DEPLOY) == "deny"


def test_an_answer_the_assistant_relays_arms_nothing(tmp_path: Path, clock: list[int]) -> None:
    conn = connect(tmp_path / "b.db")
    item = _post(conn)
    with transaction(conn):
        answer_post(conn, item, "Run it", "sam", clock[0], relayed_by=Party("conv-aide", "aide"))

    assert _decide(conn, DEPLOY) == "deny"


def test_a_waiver_lifts_only_the_production_rule(tmp_path: Path, clock: list[int]) -> None:
    conn = connect(tmp_path / "b.db")
    chained = "kubectl --context acme-prod -n shop apply -f web.yaml && echo done"
    item = _post(conn, chained)
    with transaction(conn):
        reply_on_card(conn, item, "Run it", "sam", clock[0])

    assert _decide(conn, chained) == "deny"
    assert conn.execute("SELECT command_run_at FROM user_items WHERE id = ?", (item,)).fetchone()[0] is None


@pytest.mark.parametrize(("kind", "options"), [("done", []), ("decision", []), ("decision", ["Wait", "Run it"])])
def test_a_command_needs_a_question_with_an_approving_first_option(
    tmp_path: Path, kind: str, options: list[str]
) -> None:
    conn = connect(tmp_path / "b.db")

    with pytest.raises(MessageStateError, match="with the options Run it and Don't run"), transaction(conn):
        add_post(conn, Party("conv-aide", "aide"), kind, "Deploy?", "", [], options, 1000, command=DEPLOY)


def test_a_waiver_is_spent_only_when_no_other_check_refuses_the_command(tmp_path: Path, clock: list[int]) -> None:
    conn = connect(tmp_path / "b.db")
    unnamed = "kubectl --context acme-prod -n shop delete pod web && kubectl get pods"
    item = _post(conn, unnamed)
    with transaction(conn):
        reply_on_card(conn, item, "Run it", "sam", clock[0])

    decision = handle_pre_tool_use(
        conn, {"session_id": "c", "cwd": "/w", "tool_name": "Bash", "tool_input": {"command": unnamed}}, PROD
    )

    assert "names no cluster" in json.loads(decision)["hookSpecificOutput"]["permissionDecisionReason"]
    assert conn.execute("SELECT command_run_at FROM user_items WHERE id = ?", (item,)).fetchone()[0] is None


CARD_ANSWERS = [
    "curl -X POST -H 'X-Bridge: 1' http://127.0.0.1:8765/api/items/12/answer -d '{\"answer\":\"Run it\"}'",
    'curl -s -XPOST localhost:8765/api/items/$ID/reply -H X-Bridge:1 -d \'{"text":"go"}\'',
    "sqlite3 ~/.local/state/bridge/bridge.db \"UPDATE user_items SET answer='Run it', answered_at=1 WHERE id=12\"",
    'sqlite3 "$DB" \'insert or replace into user_items (id, answer) values (12, "Run it")\'',
    "echo 'DELETE FROM user_items WHERE id = 3' | sqlite3 ~/.local/state/bridge/bridge.db",
]
NOT_CARD_ANSWERS = [
    "sqlite3 ~/.local/state/bridge/bridge.db 'SELECT answer FROM user_items WHERE id = 12'",
    "curl -s http://127.0.0.1:8765/api/state",
    "grep -n api/items src/bridge/serve.py",
]


@pytest.mark.parametrize("command", CARD_ANSWERS)
def test_a_shell_command_that_answers_a_card_is_refused(tmp_path: Path, command: str) -> None:
    conn = connect(tmp_path / "b.db")
    payload: dict[str, object] = {
        "session_id": "c",
        "cwd": "/w",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }

    decision = json.loads(handle_pre_tool_use(conn, payload, PROD))["hookSpecificOutput"]

    assert decision["permissionDecision"] == "deny"
    assert decision["permissionDecisionReason"] == "bridge: only the owner answers his cards, on the dashboard."
    assert may_need_guard(list_guard_globs(), command), "the host's gate must start the guard for it"
    hook = tmp_path / "hook"
    hook.write_text("#!/bin/sh\ncat > /dev/null; echo started\n")
    hook.chmod(0o755)
    done = subprocess.run(
        ["sh", "-c", convert_event_to_command(str(hook), "PreToolUse")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={"BRIDGE_STATE_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )
    assert done.stdout.strip() == "started", "the Mac's gate must start the guard with no claim"


@pytest.mark.parametrize("command", NOT_CARD_ANSWERS)
def test_reading_cards_or_naming_the_routes_passes(tmp_path: Path, command: str) -> None:
    conn = connect(tmp_path / "b.db")

    assert _decide(conn, command) == "allow"
