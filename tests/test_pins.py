import json
import sqlite3
from collections.abc import Generator
from contextlib import closing
from pathlib import Path

import pytest
from conftest import BridgeEnv, SpawnProcess, http_request

from bridge.db import MIGRATIONS, connect
from bridge.pinned import PinnedRequest, list_pinned_items, map_slice_to_page
from bridge.pins import PinError, add_pin, archive_pin, find_secret, list_pins

NORMAL_COMMANDS = [
    "git log --oneline origin/main..HEAD",
    "git show 2b7a2c6e4f1a9b8c7d6e5f4a3b2c1d0e9f8a7b6c",
    "git push origin theo/guild-pinned --force-with-lease",
    "kubectl -n prod get secret db-creds -o jsonpath='{.data.password}' | base64 -d",
    "kubectl get pods -l app=api-server-7d9f8c6b5-x2k4p",
    "kubectl logs deploy/monorepo-worker --since=1h | grep -i error",
    "kubectl rollout restart deployment/monorepo-api-gateway-canary-v2",
    "gh pr view 12 --repo Acme-Org/monorepo --json title,state",
    "gh api repos/Acme-Org/monorepo/actions/runs?per_page=5",
    "gh auth token | pbcopy",
    "gh secret set DEPLOY_KEY < ~/.ssh/deploy.pub",
    "aws secretsmanager get-secret-value --secret-id prod/db --query SecretString",
    "docker pull ghcr.io/acme/api@sha256:3f7a9c1e2b4d6f8a0c2e4f6a8b0d2f4a6c8e0a2b4d6f8a0c2e4f6a8b0d2f4a6c",
    "docker login ghcr.io -u theo --password-stdin < ~/.ghcr",
    "ssh devbox-alt cat /home/dev/proj12-staging-login.txt",
    "cat ~/.local/state/bridge/worker-briefs/0123456789abcdef0123456789abcdef.md",
    "op read 'op://Engineering/Prod DB/password'",
    "export GH_TOKEN=$(op read op://Eng/GitHub/token)",
    "PGPASSWORD=$PGPASS psql -h db.internal -U app",
    "TOKEN_FILE=~/.config/gh/hosts.yml gh pr list",
    'curl -H "Authorization: Bearer $TOKEN" https://api.example.com/v1/me',
    "psql postgres://app@db.internal:5432/app",
    "helm upgrade --install api ./charts/api -f values/prod-eu-west-1.yaml --set image.tag=v2.4.0",
    "terraform apply -var-file=prod.tfvars -target=module.redis_cluster_eu_west",
    "python -c 'import secrets; print(secrets.token_hex(16))'",
    "uv run pytest tests/test_pins.py -q > /tmp/out.txt; echo $?",
    "git push origin fix-token:main",
    "kubectl get secrets -o custom-columns=NAME:.metadata.name,TOKEN:.data.token",
    "kubectl delete pod api-server-7d9f8c6b5-x2k4p worker-5c6d7e8f9a-b1c2d",
    "curl -u alice https://example.com/api",
    "curl -u $API_USER:$API_PASS https://example.com/api",
    "mysql -u root -p mydb",
    "ssh -p 2222 devbox && docker run -p 8080:80 nginx",
    "curl -s https://[HOST]/health",
    "python -m pytest -k MyServiceAccountNameForTesting2",
    "docker run --user 1000:1000 alpine id",
    "docker run --name mysql -p3306:3306 mysql:8",
    "helm upgrade redis bitnami/redis --set auth.existingSecret=redis-auth",
    "aws ec2 run-instances --image-id ami-0123456789abcdef0 --client-token deploy-20261109",
    "kubectl create secret generic db --from-file=password=./password.txt",
    'psql "postgresql://$DB_USER:$DB_PASSWORD@db.internal/app"',
    "git grep 'api_key=' -- '*.py'",
    'kubectl patch secret db -p \'{"data":{"password":null}}\'',
    "aws cognito-idp describe-user-pool-client --user-pool-id us-east-1_AbCd12345"
    " --client-id 1exampleclientid2abcdefghi",
]
NORMAL_LINKS = [
    "https://claude.ai/artifact/Ex4mpleArtifactId00004",
    "https://docs.google.com/document/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/edit",
    "https://github.com/Acme-Org/monorepo/releases/tag/v2.4.0",
    "https://github.com/Acme-Org/monorepo/pull/12/files?diff=split&w=1",
    "https://linear.app/acme/issue/PROJ-12/move-the-search-box",
    "https://acme.slack.com/archives/C05ABCDEF12/p1699999999123456",
    "https://www.notion.so/Runbook-0123456789abcdef0123456789abcdef",
    "https://console.aws.amazon.com/cloudwatch/home?region=eu-west-1#logsV2:log-groups",
    "https://app.datadoghq.com/dashboard/abc-12?from_ts=1699999999123&to_ts=1700000000000&live=true",
    "https://drive.google.com/open?id=1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms",
    "https://example.com/search?q=api%20token%20rotation",
]
NORMAL_NOTES = [
    "Password: in 1Password, item 'Prod DB admin'",
    "DB password: 1Password item Prod DB",
    "where it lives: 1Password item 'Staging login'",
    "token lives in Keychain under 'gh:github.com'",
    'Password: "1Password item Prod DB"',
    "api token: rotate in the Linear settings, then update the 1Password item",
    "DB_PASSWORD: 1Password item X",
    'DB_PASSWORD: "1Password item X"',
]
SECRETS = [
    ("export GITHUB_TOKEN=ghp_aB3dE5fG7hI9jK1lM3nO5pQ7rS9tU1vW3xY5", "GitHub token"),  # gitleaks:allow
    ("github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz", "GitHub token"),
    ("gho_aB3dE5fG7hI9jK1lM3nO5pQ7rS9tU1vW3xY5", "GitHub token"),  # gitleaks:allow
    ("claude --key sk-ant-api03-AbCdEf123456GhIjKl789012", "sk-"),
    ("SLACK=xox" + "b-12345678901-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx", "Slack token"),  # gitleaks:allow
    ("xox" + "p-1234567890-abcdefghij", "Slack token"),
    ("aws configure set aws_access_key_id AKIAIOSFODNN7EXAMPLE", "AWS access key"),
    ("-----BEGIN OPENSSH PRIVATE KEY-----", "PEM"),
    ("mysql -u root --password=hunter2", "value given for --password"),
    ("mysql -u root --password hunter2", "value given for password"),
    ("DB_PASSWORD=correcthorse ./run.sh", "value given for DB_PASSWORD"),
    ("client_secret: 'kq8Zabc'", "value given for client_secret"),
    ('{"api_key": "abcd1234"}', "value given for api_key"),
    ("secret=abc123", "value given for secret"),
    ("psql postgres://app:s3cr3tpass@db.internal:5432/app", "user:password@"),
    ("https://admin:hunter2@grafana.internal/", "user:password@"),
    ("curl https://api.example.com/x?token=abc", "value given for token"),
    ("https://hooks.example.com/services?sig=Zk9Lm3Qp7Rt2Vx8Yb4Nc6Hd1Jf5Gs0Ae", "sig="),
    ("echo dGhpc0lzQVZlcnlMb25nUmFuZG9tU2VjcmV0MTIzNDU2Nzg5 | base64 -d", "random-looking"),
    ("the key is 9fJ2kLq8ZxPw3RtY7vBn5Mc1HdGs6Ae4", "random-looking"),
    ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc", "JSON web token"),
    ("DB_PASSWORD: hunter2", "value given for DB_PASSWORD"),
    ("password: hunter2", "value given for password"),
    ("db:\n  password: hunter2\n  host: db.internal", "value given for password"),
    ("mysql --password='/hunter2'", "value given for --password"),
    ("DB_PASSWORD='$hunter2' ./run", "value given for DB_PASSWORD"),
    ("curl -u alice:hunter2 https://example.com", "curl"),  # gitleaks:allow
    ("mysql -uroot -phunter2", "-p"),
    ("sshpass -p hunter2 ssh devbox", "sshpass"),
    ("redis-cli -u redis://:hunter2@localhost:6379/0", "user:password@"),
    ("https://example.com/?%74oken=hunter2", "value given for token"),
    ("https://example.com/?token=%68unter2", "value given for token"),
    ("https://bucket.s3.amazonaws.com/f?X-Amz-Signature=abc", "X-Amz-Signature"),
    ("https://hooks.slack.com/services/" + "T01234567/B01234567/9fJ2kLq8ZxPw3RtY7vBn5Mc1", "webhook"),  # gitleaks:allow
    ("9fJ2kLq8_ZxPw3RtY_7vBn5Mc1_HdGs6Ae4", "random-looking"),
    ("KqLmPwRtYvBcNdFgHjXsUaZe", "random-looking"),
    ("password: hunter2 # production", "value given for password"),
    ('password: "correct horse battery staple"', "value given for password"),
    ("password:hunter2", "value given for password"),
    ("sshpass -p 'hunter2' ssh user@host", "sshpass"),
    ("mysql -uroot -p'hunter2'", "mysql -p"),
    ("curl -ualice:hunter2 https://example.com", "curl"),  # gitleaks:allow
    ("docker login ghcr.io -u alice -p hunter2", "docker login"),
    ("export DB_PASSWORD=$'hunter2'", "value given for DB_PASSWORD"),
    ("curl -H 'Authorization: Basic YWxpY2U6aHVudGVyMg==' https://example.com", "Basic"),
    ("https://example.com/?session=9fJ2kLq8ZxPw3RtY7vBn5Mc1HdGs6Ae4", "session="),
    ("https://discord.com/api/v10/webhooks/123456789012345678/9fJ2kLq8ZxPw3RtY7vBn5Mc1HdGs6Ae4", "webhook"),
]


@pytest.fixture
def conn(tmp_path: Path) -> Generator[sqlite3.Connection]:
    c = connect(tmp_path / "bridge.db")
    yield c
    c.close()


@pytest.mark.parametrize("text", NORMAL_COMMANDS + NORMAL_LINKS + NORMAL_NOTES)
def test_guard_lets_normal_commands_links_and_pointers_through(text: str) -> None:
    assert find_secret(text) is None


@pytest.mark.parametrize(("text", "what"), SECRETS)
def test_guard_names_what_looks_like_a_secret(text: str, what: str) -> None:
    found = find_secret(text)
    assert found is not None and what in found, found


@pytest.mark.parametrize("field", ["title", "body", "source"])
def test_a_secret_in_any_field_is_refused_with_what_to_pin_instead(conn: sqlite3.Connection, field: str) -> None:
    values = {"title": "Deploy", "body": "make deploy", "source": "alex"}
    values[field] = "ghp_aB3dE5fG7hI9jK1lM3nO5pQ7rS9tU1vW3xY5"  # gitleaks:allow
    with pytest.raises(PinError) as caught:
        add_pin(conn, values["title"], "cmd", values["body"], values["source"], "alex", 1000)
    assert "where it lives" in str(caught.value) and "ghp_aB3" not in str(caught.value)
    assert list_pins(conn, include_archived=True) == []


def test_pins_list_newest_first_and_unpin_hides_but_keeps(conn: sqlite3.Connection) -> None:
    first = add_pin(conn, "Release", "link", "https://x.example/r", None, "alex", 1000)
    second = add_pin(conn, "  Logs\n of prod ", "cmd", "kubectl logs -f deploy/api\n", "ask #64", "boris", 2000)
    third = add_pin(conn, "DB admin", "note", "1Password item 'Prod DB admin'", None, "xavi", 2000)
    assert [p.id for p in list_pins(conn)] == [third.id, second.id, first.id]
    assert (second.title, second.body, second.source, second.pinned_by) == (
        "Logs of prod",
        "kubectl logs -f deploy/api",
        "ask #64",
        "boris",
    )

    archived = archive_pin(conn, second.id, "eli", 3000)
    assert (archived.archived_at, archived.archived_by) == (3000, "eli")
    assert [p.id for p in list_pins(conn)] == [third.id, first.id]
    assert [p.id for p in list_pins(conn, include_archived=True)] == [third.id, second.id, first.id]
    with pytest.raises(PinError, match="already unpinned"):
        archive_pin(conn, second.id, "eli", 4000)
    with pytest.raises(PinError, match="no pin #99"):
        archive_pin(conn, 99, "eli", 4000)


@pytest.mark.parametrize(
    ("kind", "body", "error"),
    [
        ("link", "javascript:alert(1)", r"http\(s\) URL"),
        ("link", "https://x.example/a b", r"http\(s\) URL"),
        ("link", "https://[::1", r"http\(s\) URL"),
        ("link", "https://admin:hunter2@x.example/a b", "user:password@"),
        ("cmd", "   ", "empty"),
        ("note", "x" * 1001, "keep it under"),
    ],
)
def test_bad_fields_are_refused(conn: sqlite3.Connection, kind: str, body: str, error: str) -> None:
    with pytest.raises(PinError, match=error) as caught:
        add_pin(conn, "t", kind, body, None, "alex", 1000)
    assert "hunter2" not in str(caught.value)


def test_pins_survive_reconnect_and_live_in_migration_62(tmp_path: Path) -> None:
    with closing(connect(tmp_path / "bridge.db")) as first:
        add_pin(first, "Release", "link", "https://x.example/r", None, "alex", 1000)
    with closing(connect(tmp_path / "bridge.db")) as again:
        assert [p.title for p in list_pins(again)] == ["Release"]
        assert again.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    assert "CREATE TABLE pins" in MIGRATIONS[61]


def test_page_escapes_agent_text_and_renders_each_kind(conn: sqlite3.Connection) -> None:
    add_pin(conn, "<script>alert(1)</script>", "note", "</p><img src=x onerror=alert(1)>", "<b>x</b>", "a&b", 1000)
    add_pin(conn, 'Run "it"', "cmd", "echo '<b>' && ls | grep \"x\"", None, "alex", 2000)
    add_pin(conn, "Doc", "link", 'https://x.example/?q="><script>', None, "alex", 3000)
    page = render_pinned(conn)

    assert "<script>alert" not in page and "<img" not in page and "<b>x</b>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "&lt;/p&gt;&lt;img src=x onerror=alert(1)&gt;" in page
    assert "pinned by a&amp;b" in page
    assert "<code>echo &#x27;&lt;b&gt;&#x27; &amp;&amp; ls | grep &quot;x&quot;</code>" in page
    assert "data-copy>Copy</button>" in page
    assert 'href="https://x.example/?q=&quot;&gt;&lt;script&gt;"' in page
    assert page.index("Doc") < page.index("Run &quot;it&quot;") < page.index("&lt;script&gt;alert")
    assert "<!--PINS-->" not in page and "<!--TOTAL-->" not in page and "<!--EMPTY-->" not in page
    assert '<p id="empty" class="empty" hidden>' in page


def test_empty_page_says_so(conn: sqlite3.Connection) -> None:
    assert '<p id="empty" class="empty">Nothing pinned yet.</p>' in render_pinned(conn)


def render_pinned(conn: sqlite3.Connection) -> str:
    return map_slice_to_page(list_pinned_items(conn, PinnedRequest()), PinnedRequest())


def test_cli_pin_unpin_and_list(bridge_env: BridgeEnv, spawn_process: SpawnProcess) -> None:
    pid = spawn_process()
    bridge_env.write_session_file(pid, "conv-p")
    pinned = bridge_env.cli("pin", "Prod logs", "--cmd", "kubectl logs -f deploy/api", "--source", "alex", pid=pid)
    assert pinned.returncode == 0, pinned.stderr
    pin_id = int(pinned.stdout.strip())
    note = bridge_env.cli("pin", "DB admin", "--note", "1Password item 'Prod DB'", pid=pid)
    assert note.returncode == 0, note.stderr

    secret = "export GH_TOKEN=ghp_aB3dE5fG7hI9jK1lM3nO5pQ7rS9tU1vW3xY5"  # gitleaks:allow
    refused = bridge_env.cli("pin", "Token", "--cmd", secret, pid=pid)
    assert refused.returncode == 2 and "where it lives" in refused.stderr and "ghp_aB3" not in refused.stderr
    both = bridge_env.cli("pin", "x", "--cmd", "ls", "--link", "https://x.example", pid=pid)
    assert both.returncode == 2 and "not allowed with" in both.stderr
    outside = bridge_env.cli("pin", "x", "--cmd", "ls")
    assert outside.returncode == 2 and "not inside a Claude Code session" in outside.stderr

    listed = json.loads(bridge_env.cli("pins", "--json").stdout)
    assert [p["title"] for p in listed] == ["DB admin", "Prod logs"]
    assert listed[1]["source"] == "alex" and listed[1]["pinned_by"] == listed[0]["pinned_by"] != ""

    unpinned = bridge_env.cli("unpin", f"#{pin_id}", pid=pid)
    assert unpinned.returncode == 0 and "unpinned: Prod logs" in unpinned.stdout, unpinned.stderr
    text = bridge_env.cli("pins").stdout
    assert "DB admin" in text and "Prod logs" not in text
    assert "Prod logs" in bridge_env.cli("pins", "--all").stdout


def test_pinned_page_is_served(bridge_env: BridgeEnv, dashboard: str) -> None:
    with closing(bridge_env.connect()) as db:
        add_pin(db, "<i>Release</i>", "link", "https://x.example/r", "ask #64", "alex", 1000)
    page = http_request(dashboard + "/pinned")
    assert "<title>Pinned</title>" in page and "&lt;i&gt;Release&lt;/i&gt;" in page and "ask #64" in page
    assert '<a href="/pinned">' in http_request(dashboard + "/guild")
