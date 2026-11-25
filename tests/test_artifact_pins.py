import json
import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest
from conftest import BridgeEnv

from bridge.artifacts import (
    UNKNOWN_SESSION,
    ArtifactPublish,
    TranscriptPublish,
    backfill_artifact_publishes,
    map_tool_call_to_publish,
    scan_transcripts,
)
from bridge.config import Config
from bridge.db import connect
from bridge.install import install_hooks, uninstall_hooks
from bridge.pages import ARTIFACT_SOURCE, add_page, find_page, set_page_url
from bridge.pinned import PinnedRequest, list_pinned_items
from bridge.pins import add_pin, list_pins, map_pin_to_html, record_artifact_publish

URL = "https://claude.ai/artifact/Ex4mpleArtifactId00001"
OTHER = "https://claude.ai/artifact/Ex4mpleArtifactId00002"
THIRD = "https://claude.ai/artifact/Ex4mpleArtifactId00003"
DAY = 86_400_000
NOON = 1_794_484_800_000


@pytest.fixture
def conn(tmp_path: Path) -> Generator[sqlite3.Connection]:
    c = connect(tmp_path / "bridge.db")
    yield c
    c.close()


def add_tab(
    conn: sqlite3.Connection, conversation_id: str, handle: str, tab_name: str, workspace: str = "Work"
) -> None:
    surface = f"PANE-{handle}"
    conn.execute(
        "INSERT INTO surfaces (surface, tab_name, workspace_name, handle, updated_at) VALUES (?, ?, ?, ?, 0)",
        (surface, tab_name, workspace, handle),
    )
    conn.execute(
        "INSERT INTO instances (instance_id, pid, proc_start_ms, conversation_id, surface, first_seen_at, "
        "last_observed_at) VALUES (?, 1, 1, ?, ?, 0, 0)",
        (f"i-{handle}", conversation_id, surface),
    )
    conn.execute(
        "INSERT INTO conversations (conversation_id, instance_id, first_seen_at, last_seen_at) VALUES (?, ?, 0, 0)",
        (conversation_id, f"i-{handle}"),
    )


def published(url: str = URL, title: str = "Billing gate: what was done") -> dict[str, object]:
    return {"url": url, "title": title, "updated": False, "owned": True, "seq": 1}


@pytest.mark.parametrize(
    ("tool_input", "response", "expected"),
    [
        ({"file_path": "/s/gate.html"}, published(), ArtifactPublish(URL, "Billing gate: what was done")),
        (
            {"action": "publish", "url": URL, "file_path": "/s/gate.html"},
            published(),
            ArtifactPublish(URL, "Billing gate: what was done"),
        ),
        (
            {"type_url": OTHER, "title": "Report"},
            {"url": URL, "created_from_type": True},
            ArtifactPublish(URL, "Report"),
        ),
        (
            {"file_path": "/s/week_page.html"},
            f"Published /s/week_page.html at {URL} (Version 8, version id 1-2)\n",
            ArtifactPublish(URL, "week_page"),
        ),
        (
            {"file_path": "/s/a.html"},
            [{"type": "text", "text": f"Published /s/a.html at {URL} (Version 1"}],
            ArtifactPublish(URL, "a"),
        ),
        ({"action": "read", "url": URL}, {"url": URL, "title": "x"}, None),
        ({"action": "open", "url": URL}, {"url": URL, "title": "x", "opened": True}, None),
        ({"action": "quickstart", "intent": "other"}, {"quickstart": "..."}, None),
        ({"url": URL, "file_path": "/s/logo.png", "asset": True}, {"url": URL, "title": "x"}, None),
        ({"file_path": "/s/a.html"}, f'Error: <artifact-deleted url="{URL}"/> the artifact was deleted', None),
        ({"file_path": "/s/a.html"}, {"url": "https://evil.example/artifact/x", "title": "x"}, None),
    ],
)
def test_only_a_publish_with_an_artifact_link_counts(
    tool_input: dict[str, object], response: object, expected: ArtifactPublish | None
) -> None:
    assert map_tool_call_to_publish(tool_input, response) == expected


def test_a_republish_moves_the_pin_and_never_adds_a_second(conn: sqlite3.Connection) -> None:
    first = record_artifact_publish(conn, URL, "Gate draft", "xavi", NOON)
    again = record_artifact_publish(conn, URL, "Billing gate: what was done", "eli", NOON + 3_600_000)
    assert first is not None and again is not None and again.id == first.id
    assert (again.title, again.pinned_by, again.created_at, again.shown_at) == (
        "Billing gate: what was done",
        "xavi",
        NOON,
        NOON + 3_600_000,
    )
    # A backfilled publish older than both moves the first publish back and keeps the newest title.
    older = record_artifact_publish(conn, URL, "Old title", "xavi", NOON - DAY)
    assert older is not None
    assert (older.title, older.created_at, older.shown_at) == (
        "Billing gate: what was done",
        NOON - DAY,
        NOON + 3_600_000,
    )
    assert [p.id for p in list_pins(conn)] == [first.id]


def test_a_persons_pin_of_the_link_keeps_its_title_and_pin_time(conn: sqlite3.Connection) -> None:
    mine = add_pin(conn, "Read before we talk", "link", URL, "charlie", "charlie", NOON)
    moved = record_artifact_publish(conn, URL, "Gate", "xavi", NOON + 60_000)
    assert moved is not None and moved.id == mine.id
    assert (moved.title, moved.source, moved.created_at, moved.shown_at) == (
        "Read before we talk",
        "charlie",
        NOON,
        NOON + 60_000,
    )
    assert "pinned by charlie · published <time" in map_pin_to_html(moved)


def test_a_registered_page_shows_the_publish_instead_of_a_pin(conn: sqlite3.Connection, tmp_path: Path) -> None:
    folder = tmp_path / "page"
    folder.mkdir()
    (folder / "index.html").write_text("<html></html>")
    add_page(conn, "gate", "Gate", str(folder), URL, False, "xavi", NOON)
    assert record_artifact_publish(conn, URL, "Gate", "xavi", NOON + 60_000) is None
    page = find_page(conn, "gate")
    assert list_pins(conn) == [] and page is not None and page.updated_at == NOON + 60_000


def test_registering_the_page_of_a_pinned_publish_replaces_the_pin(conn: sqlite3.Connection, tmp_path: Path) -> None:
    folder = tmp_path / "page"
    folder.mkdir()
    (folder / "index.html").write_text("<html></html>")
    mine = add_pin(conn, "Read this", "link", OTHER, None, "charlie", NOON)
    record_artifact_publish(conn, URL, "Gate", "xavi", NOON)
    record_artifact_publish(conn, OTHER, "Digest", "xavi", NOON)
    record_artifact_publish(conn, THIRD, "DNS", "xavi", NOON)
    add_page(conn, "gate", "Gate", str(folder), None, False, "xavi", NOON + 1)
    set_page_url(conn, "gate", URL, NOON + 2)
    add_page(conn, "dns", "DNS", str(folder), THIRD, False, "xavi", NOON + 3)
    assert [p.id for p in list_pins(conn)] == [mine.id]
    assert {p.archived_by for p in list_pins(conn, include_archived=True)} == {None, "page gate", "page dns"}


def test_backfill_counts_a_refused_title_and_keeps_going(conn: sqlite3.Connection) -> None:
    add_tab(conn, "conv-x", "xavi", "Xavi")
    items = [
        TranscriptPublish("conv-x", NOON, ArtifactPublish(URL, "token = ghp_abcdefghijklmnopqrstuvwxyz0123")),
        TranscriptPublish("conv-x", NOON, ArtifactPublish(OTHER, "Digest")),
    ]
    result = backfill_artifact_publishes(conn, Config(), items)
    assert [pin.title for _, pin in result.stored] == ["Digest"]
    assert len(result.refused) == 1 and "ghp_" not in result.refused[0]


def test_an_unpinned_artifact_stays_unpinned_when_republished(conn: sqlite3.Connection) -> None:
    pin = record_artifact_publish(conn, URL, "Gate", "xavi", NOON)
    assert pin is not None
    conn.execute("UPDATE pins SET archived_at = 1, archived_by = 'theo' WHERE id = ?", (pin.id,))
    record_artifact_publish(conn, URL, "Gate", "xavi", NOON + 60_000)
    assert list_pins(conn) == [] and len(list_pins(conn, include_archived=True)) == 1


def test_hook_pins_a_publish_by_the_tabs_handle_and_skips_a_private_tab(bridge_env: BridgeEnv) -> None:
    with bridge_env.connect() as conn:
        add_tab(conn, "conv-x", "xavi", "Xavi · Right Hand")
        add_tab(conn, "conv-p", "notes", "notes", workspace="🔒 Private")
    base = {"hook_event_name": "PostToolUse", "tool_name": "Artifact", "tool_input": {"file_path": "/s/gate.html"}}

    shown = bridge_env.hook("ArtifactPublish", {**base, "session_id": "conv-x", "tool_response": published()}, None)
    private = bridge_env.hook(
        "ArtifactPublish", {**base, "session_id": "conv-p", "tool_response": published(OTHER, "Recipes")}, None
    )
    other_tool = bridge_env.hook(
        "ArtifactPublish",
        {**base, "tool_name": "Write", "session_id": "conv-x", "tool_response": published(OTHER)},
        None,
    )

    assert [(r.returncode, r.stdout) for r in (shown, private, other_tool)] == [(0, "")] * 3
    with bridge_env.connect() as conn:
        pins = list_pins(conn)
    assert [(p.title, p.body, p.source, p.pinned_by) for p in pins] == [
        ("Billing gate: what was done", URL, ARTIFACT_SOURCE, "xavi")
    ]
    assert not (bridge_env.state_dir / "errors.log").exists()


def test_hook_skips_a_publish_from_a_private_tab_bridge_has_not_linked_yet(bridge_env: BridgeEnv) -> None:
    with bridge_env.connect() as conn:
        add_tab(conn, "conv-old", "notes", "🔒 notes")
    payload = {"tool_name": "Artifact", "session_id": "conv-new", "tool_input": {}, "tool_response": published()}
    done = bridge_env.hook("ArtifactPublish", payload, None, {"AGTERM_PANE_ID": "PANE-notes"})
    assert done.returncode == 0
    with bridge_env.connect() as conn:
        assert list_pins(conn) == []


def test_hook_skips_a_tab_whose_names_bridge_has_not_read_yet(bridge_env: BridgeEnv) -> None:
    with bridge_env.connect() as conn:
        add_tab(conn, "conv-new", "fresh", "x")
        conn.execute("UPDATE surfaces SET tab_name = NULL, workspace_name = NULL")
    payload = {"tool_name": "Artifact", "session_id": "conv-new", "tool_input": {}, "tool_response": published()}
    unseen_pane = bridge_env.hook("ArtifactPublish", {**payload, "session_id": "c"}, None, {"AGTERM_PANE_ID": "P-9"})
    unnamed = bridge_env.hook("ArtifactPublish", payload, None)
    assert unseen_pane.returncode == unnamed.returncode == 0
    with bridge_env.connect() as conn:
        assert list_pins(conn) == []


def write_transcript(path: Path, session: str, calls: list[tuple[str, dict[str, object], object, str | None]]) -> None:
    lines: list[dict[str, object]] = []
    for n, (name, tool_input, result, at) in enumerate(calls):
        lines.append(
            {
                "sessionId": session,
                "timestamp": at,
                "message": {"content": [{"type": "tool_use", "id": f"t{n}", "name": name, "input": tool_input}]},
            }
        )
        entry: dict[str, object] = {
            "sessionId": session,
            "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{n}", "content": "Published ..."}]},
            "toolUseResult": result,
        }
        if at is not None:
            entry["timestamp"] = at
        lines.append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def test_backfill_pins_dated_publishes_from_known_open_tabs_oldest_first(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    projects = tmp_path / "projects"
    write_transcript(
        projects / "-Users-x-acme" / "conv-x.jsonl",
        "conv-x",
        [
            ("Artifact", {"file_path": "/s/g.html"}, published(title="Gate v2"), "2026-11-12T10:00:00.000Z"),
            ("Artifact", {"file_path": "/s/g.html"}, published(title="Gate v1"), "2026-11-12T09:00:00.000Z"),
            ("Artifact", {"action": "read", "url": OTHER}, {"url": OTHER, "title": "x"}, "2026-11-12T09:30:00.000Z"),
            ("Artifact", {"file_path": "/s/d.html"}, published(OTHER, "Digest"), None),
            ("Write", {"file_path": "/s/g.html"}, {"url": OTHER, "title": "x"}, "2026-11-12T09:30:00.000Z"),
        ],
    )
    write_transcript(
        projects / "-Users-x" / "conv-p.jsonl",
        "conv-p",
        [("Artifact", {}, published(OTHER, "Recipes"), "2026-11-12T08:00:00.000Z")],
    )
    write_transcript(
        projects / "-Users-x" / "conv-gone.jsonl",
        "conv-gone",
        [("Artifact", {}, published(OTHER, "Unknown"), "2026-11-12T08:00:00.000Z")],
    )
    add_tab(conn, "conv-x", "xavi", "Xavi · Right Hand")
    add_tab(conn, "conv-p", "notes", "🔒 notes")

    scan = scan_transcripts(projects, 0)
    result = backfill_artifact_publishes(conn, Config(), scan.publishes)

    assert scan.undated == 1
    assert [p.publish.title for p in scan.publishes][-2:] == ["Gate v1", "Gate v2"]
    assert (result.locked, result.unknown_tab, result.held_by_page, len(result.stored)) == (1, 1, 0, 2)
    pins = list_pins(conn)
    assert [(p.title, p.pinned_by, p.created_at, p.shown_at) for p in pins] == [
        ("Gate v2", "xavi", 1_794_474_000_000, 1_794_477_600_000)
    ]


def test_backfill_names_a_tab_with_no_handle_yet(conn: sqlite3.Connection) -> None:
    add_tab(conn, "conv-x", "xavi", "Xavi")
    conn.execute("UPDATE surfaces SET handle = NULL")
    item = TranscriptPublish("conv-x", NOON, ArtifactPublish(URL, "Gate"))
    result = backfill_artifact_publishes(conn, Config(), [item])
    assert [pin.pinned_by for _, pin in result.stored] == [UNKNOWN_SESSION]


def test_pinned_lists_by_last_publish_with_the_day_on_each_item(conn: sqlite3.Connection) -> None:
    older = add_pin(conn, "Logs", "cmd", "kubectl logs", None, "alex", NOON - DAY)
    record_artifact_publish(conn, URL, "Gate", "xavi", NOON - 2 * DAY)
    record_artifact_publish(conn, URL, "Gate", "xavi", NOON)

    items = list_pinned_items(conn, PinnedRequest()).items
    assert [item.key for item in items][1] == f"pin-{older.id}"
    gate = items[0].html
    assert 'data-day="' in gate and "published by xavi" in gate and "first published" in gate
    assert '<a class="link" href="https://claude.ai/artifact/' in gate
    days = [item.html.split('data-day="', 1)[1].split('"', 1)[0] for item in items]
    assert days[0] != days[1]


def test_install_adds_one_artifact_hook_outside_the_hint_gate(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    install_hooks(settings, "/u/bin/bridge-hook")
    install_hooks(settings, "/u/bin/bridge-hook")
    groups = json.loads(settings.read_text())["hooks"]["PostToolUse"]
    artifact = [g for g in groups if g.get("matcher") == "Artifact|mcp__claude_ai_Google_Drive__create_file"]
    assert [g["hooks"][0]["command"] for g in artifact] == ["/u/bin/bridge-hook ArtifactPublish"]
    uninstall_hooks(settings)
    assert "PostToolUse" not in json.loads(settings.read_text())["hooks"]


def test_hook_pins_a_drive_file_at_its_view_url_and_builds_one_only_when_the_answer_has_none(
    bridge_env: BridgeEnv,
) -> None:
    with bridge_env.connect() as conn:
        add_tab(conn, "conv-x", "xavi", "Xavi · Right Hand")
    form = {
        "id": "f0Zz", "title": "Signup", "mimeType": "application/vnd.google-apps.form",
        "viewUrl": "https://docs.google.com/forms/d/f0Zz/viewform",
    }  # fmt: skip
    created = {"id": "f1Ab", "title": "Talk draft", "mimeType": "application/vnd.google-apps.presentation"}
    other = {"id": "f2Cd", "title": "notes.txt", "mimeType": "text/plain"}
    folder = {"id": "f3Ef", "title": "Talk", "mimeType": "application/vnd.google-apps.folder"}
    for response in ([{"type": "text", "text": json.dumps(created)}], form, other, folder, "not json"):
        payload = {
            "hook_event_name": "PostToolUse", "tool_name": "mcp__claude_ai_Google_Drive__create_file",
            "tool_input": {"title": "x"}, "session_id": "conv-x", "tool_response": response,
        }  # fmt: skip
        assert bridge_env.hook("ArtifactPublish", payload, None).returncode == 0
    with bridge_env.connect() as conn:
        pins = sorted((p.title, p.body, p.pinned_by) for p in list_pins(conn))
    assert pins == [
        ("Signup", "https://docs.google.com/forms/d/f0Zz/viewform", "xavi"),
        ("Talk", "https://drive.google.com/drive/folders/f3Ef", "xavi"),
        ("Talk draft", "https://docs.google.com/presentation/d/f1Ab/edit", "xavi"),
        ("notes.txt", "https://drive.google.com/file/d/f2Cd/view", "xavi"),
    ]
    assert not (bridge_env.state_dir / "errors.log").exists()
