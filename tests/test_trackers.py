import json
import sqlite3
import sys
from pathlib import Path

import pytest

from bridge.config import Config, map_toml_to_config
from bridge.db import connect
from bridge.enrich import enrich_due, load_enrichment, provider_freshness
from bridge.trackers import (
    ExecTracker,
    IssueStatus,
    LinkedPr,
    TasksFileTracker,
    guess_state_type,
    load_tracker,
    map_json_to_issues,
    map_markdown_to_issues,
)

TASKS_MD = """# Tasks

- [ ] ABC-1 Write the importer
- [~] ABC-2: Speed up the export https://tracker.example/ABC-2
- [x] abc-3 Ship the flag
- [-] ABC-4 Old idea
* [/] ABC-5 Half done
- not a task ABC-6
"""


def test_a_markdown_checklist_gives_title_state_and_link() -> None:
    issues = map_markdown_to_issues(TASKS_MD)
    assert sorted(issues) == ["ABC-1", "ABC-2", "ABC-3", "ABC-4", "ABC-5"]
    assert issues["ABC-1"] == IssueStatus("Write the importer", "Todo", "unstarted", None, "")
    assert issues["ABC-2"] == IssueStatus(
        "Speed up the export", "In progress", "started", None, "https://tracker.example/ABC-2"
    )
    assert (issues["ABC-3"].state_type, issues["ABC-4"].state_type, issues["ABC-5"].state_type) == (
        "completed", "canceled", "started",
    )  # fmt: skip


def test_json_issues_take_three_shapes_and_guess_a_missing_state_type() -> None:
    issue = {"title": "T", "state": "In Review", "url": "https://x/1", "assignee": "sam"}
    for data in ({"issues": {"abc-1": issue}}, {"ABC-1": issue}, [{"key": "ABC-1", **issue}]):
        parsed = map_json_to_issues(data)
        assert parsed["ABC-1"] == IssueStatus("T", "In Review", "started", "sam", "https://x/1")
    with pytest.raises(ValueError):
        map_json_to_issues("nope")
    assert [guess_state_type(s) for s in ("Done", "Won't do", "Backlog", "Triage", "Todo")] == [
        "completed", "canceled", "backlog", "triage", "unstarted",
    ]  # fmt: skip


def test_json_linked_prs_take_refs_or_urls_and_iso_or_ms_merge_times() -> None:
    issue = {
        "title": "T", "state": "Todo", "state_type": "unstarted", "linked_complete": True,
        "linked_prs": [
            {"ref": "acme/web#7", "status": "Merged", "closes": True, "merged_at": "2026-11-03T08:00:00+00:00"},
            {"url": "https://github.com/acme/web/pull/8", "status": "open", "merged_at": 5},
            {"ref": "not a pr"},
        ],
    }  # fmt: skip
    parsed = map_json_to_issues({"ABC-1": issue})["ABC-1"]
    assert parsed.linked_complete is True
    assert parsed.linked_prs == [
        LinkedPr("pr:acme/web#7", "merged", True, 1_793_692_800_000),
        LinkedPr("pr:acme/web#8", "open", False, 5),
    ]


def test_the_tasks_tracker_answers_known_keys_and_skips_a_missing_file(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks.md"
    tracker = TasksFileTracker(tasks)
    assert tracker.fetch(["issue:ABC-1"]) == {} and "(missing)" in tracker.describe()
    tasks.write_text(TASKS_MD)
    results = tracker.fetch(["issue:ABC-1", "issue:ABC-9"])
    assert isinstance(results["issue:ABC-1"], IssueStatus) and results["issue:ABC-9"] == "not found or no access"
    broken = tmp_path / "tasks.json"
    broken.write_text("{")
    assert str(TasksFileTracker(broken).fetch(["issue:ABC-1"])["issue:ABC-1"]).startswith("tasks file tasks.json:")


def _write_adapter(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "adapter.py"
    script.write_text(f"import json, sys\nrequest = json.load(sys.stdin)\n{body}\n")
    return script


def test_the_exec_tracker_sends_keys_on_stdin_and_reads_issues_and_errors(tmp_path: Path) -> None:
    script = _write_adapter(
        tmp_path,
        "assert request == {'version': 1, 'op': 'fetch', 'keys': ['ABC-1', 'ABC-2', 'ABC-3']}\n"
        "print(json.dumps({'issues': {'ABC-1': {'title': 'One', 'state': 'Done'}}, 'errors': {'ABC-2': 'no access'}}))",
    )
    results = ExecTracker((sys.executable, str(script))).fetch(["issue:ABC-1", "issue:ABC-2", "issue:ABC-3"])
    assert results["issue:ABC-1"] == IssueStatus("One", "Done", "completed", None, "")
    assert results["issue:ABC-2"] == "no access" and results["issue:ABC-3"] == "not found or no access"


def test_a_failing_or_garbled_exec_tracker_fails_each_ref_with_why(tmp_path: Path) -> None:
    failing = _write_adapter(tmp_path, "sys.stderr.write('token expired\\n'); sys.exit(3)")
    assert ExecTracker((sys.executable, str(failing))).fetch(["issue:ABC-1"]) == {
        "issue:ABC-1": "tracker command failed: token expired"
    }
    garbled = _write_adapter(tmp_path, "print('hello')")
    answer = ExecTracker((sys.executable, str(garbled))).fetch(["issue:ABC-1"])["issue:ABC-1"]
    assert str(answer).startswith("tracker command answered no valid JSON")
    missing = ExecTracker((str(tmp_path / "nope"),)).fetch(["issue:ABC-1"])
    assert missing == {"issue:ABC-1": "tracker command failed: FileNotFoundError"}


def test_config_picks_the_tracker() -> None:
    assert map_toml_to_config({}).tracker == "tasks"
    assert map_toml_to_config({"linear_api_key_file": "~/k", "tracker": "linear"}).tracker == "linear"
    assert map_toml_to_config({"tracker": "none"}).tracker == "none"
    exec_config = map_toml_to_config({"tracker": "exec", "tracker_command": ["~/bin/t", "--json"]})
    assert exec_config.tracker_command == ("~/bin/t", "--json")
    bad_configs: list[dict[str, object]] = [{"tracker": "jira"}, {"tracker": "exec"}]
    for bad in bad_configs:
        with pytest.raises(ValueError):
            map_toml_to_config(bad)
    assert load_tracker(Config(tracker="none")) is None
    linear = load_tracker(Config(tracker="linear"))
    assert linear is not None and linear.name == "linear"


def test_enrichment_stores_tracker_issues_for_watched_refs(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks.md"
    tasks.write_text(TASKS_MD)
    conn: sqlite3.Connection = connect(tmp_path / "bridge.db")
    with conn:
        conn.execute(
            "INSERT INTO claims (resource, token, holder_conversation, holder_handle, acquired_at, renewed_at, "
            "expires_at) VALUES ('issue:ABC-2', 't', 'c', 'h', 0, 0, 9999999999999)"
        )
    counts = enrich_due(conn, Config(tracker="tasks", tasks_file=str(tasks)), 1000)
    assert counts == {"prs": 0, "issues": 1}
    stored = load_enrichment(conn, 1000)["issue:ABC-2"]
    assert (stored["title"], stored["state_type"], stored["url"]) == (
        "Speed up the export", "started", "https://tracker.example/ABC-2",
    )  # fmt: skip
    assert provider_freshness(load_enrichment(conn, 1000), ["issue:ABC-2"])["tracker"] == {
        "newest_ok_at": 1000, "failing": 0, "watched": 1,
    }  # fmt: skip
    assert json.loads(conn.execute("SELECT data FROM enrichment").fetchone()[0])["linked_prs"] == []


def test_an_issue_links_to_the_trackers_url_only_when_it_is_http(tmp_path: Path) -> None:
    from bridge.serve import map_data_to_web_url, map_enrichment_to_url, map_ref_to_url

    assert map_data_to_web_url({"url": "https://tracker.example/ABC-2"}) == "https://tracker.example/ABC-2"
    assert map_data_to_web_url({"url": "javascript:alert(1)"}) == "" and map_data_to_web_url(None) == ""
    tasks = tmp_path / "tasks.md"
    tasks.write_text(TASKS_MD)
    conn = connect(tmp_path / "bridge.db")
    enrich_due(conn, Config(tracker="tasks", tasks_file=str(tasks)), 1000, force=True)
    conn.execute("INSERT INTO enrichment (ref, data, fetched_at, ok_at) VALUES ('issue:ABC-2', ?, 1, 1)",
                 (json.dumps({"url": "https://tracker.example/ABC-2"}),))  # fmt: skip
    assert map_enrichment_to_url(conn, "issue:ABC-2") == "https://tracker.example/ABC-2"
    assert map_enrichment_to_url(conn, "pr:acme/web#1") == ""
    assert map_ref_to_url("issue:ABC-2", Config(issue_url="https://t.example/{key}")) == "https://t.example/ABC-2"
