import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from bridge.config import Config, map_toml_to_config
from bridge.review_post import (
    DRY_RUN,
    FAILED,
    POSTED,
    map_pr_to_slack_text,
    map_row_to_published_text,
    map_typed_comment_to_slack,
    send_to_chat,
)


def test_config_picks_the_chat_and_keeps_slack_for_an_old_config() -> None:
    assert map_toml_to_config({}).chat_adapter == "none"
    assert map_toml_to_config({"review_channel_id": "C1"}).chat_adapter == "slack"
    assert map_toml_to_config({"review_channel_id": "C1", "chat": "file"}).chat_adapter == "file"
    assert Config(review_channel_id="C1").chat_adapter == "slack"
    hook = map_toml_to_config({"chat": "webhook", "chat_webhook_url": "https://hooks.example/x"})
    assert hook.chat_webhook_url == "https://hooks.example/x"
    bad_configs: list[dict[str, object]] = [{"chat": "teams"}, {"chat": "webhook"}]
    for bad in bad_configs:
        with pytest.raises(ValueError):
            map_toml_to_config(bad)


def test_only_slack_needs_member_ids_for_mentions() -> None:
    with pytest.raises(ValueError):
        map_toml_to_config({"review_channel_id": "C1", "review_mention_people": {"Kai": "kai"}})
    people = map_toml_to_config({"chat": "file", "review_mention_people": {"Kai": "kai.dev"}}).review_mention_people
    assert people == (("Kai", "kai.dev"),)


def test_outside_slack_a_mention_is_a_plain_handle() -> None:
    assert map_pr_to_slack_text("http://u", "Adds X", True, ["kai"], "look", slack=False) == (
        "http://u - Adds X - direct @kai look"
    )
    assert map_typed_comment_to_slack("@Kai see this", {"Kai": "kai.dev"}, slack=False) == "@kai.dev see this"
    assert map_typed_comment_to_slack("@Kai see this", {"Kai": "U01AAAA0001"}) == "<@U01AAAA0001> see this"


def test_the_file_chat_appends_one_json_line_per_post(tmp_path: Path) -> None:
    log = tmp_path / "chat" / "posts.log"
    config = Config(chat="file", chat_file=str(log))
    first = send_to_chat(config, "http://u - one")
    send_to_chat(config, "http://u - two ✓")
    assert (first.state, first.link, first.detail) == (POSTED, None, f"written to {log}")
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [line["text"] for line in lines] == ["http://u - one", "http://u - two ✓"]


def test_the_webhook_chat_posts_text_and_content_and_reports_failures() -> None:
    bodies: list[dict[str, object]] = []
    statuses = [200, 500]

    class Hook(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(statuses.pop(0))
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/hook"
        config = Config(chat="webhook", chat_webhook_url=url)
        ok = send_to_chat(config, "http://u - posted")
        failed = send_to_chat(config, "http://u - again")
    finally:
        server.shutdown()
    assert bodies[0] == {"text": "http://u - posted", "content": "http://u - posted"}
    assert (ok.state, ok.detail) == (POSTED, "webhook HTTP 200")
    assert (failed.state, failed.detail) == (FAILED, "webhook HTTP 500")
    unreachable = send_to_chat(Config(chat="webhook", chat_webhook_url="http://127.0.0.1:9/x"), "t")
    assert unreachable.state == FAILED and "127.0.0.1" not in str(unreachable.detail)


def test_no_chat_posts_nothing_and_the_owner_is_told_so() -> None:
    assert send_to_chat(Config(chat="none"), "t").state == FAILED
    row = {"url": "http://u", "draft": False, "post_link": None, "post_direct": 0, "reviewed_at": 5,
           "post_state": DRY_RUN, "requested": 1, "others_reviewed": 0}  # fmt: skip
    assert "Nothing was posted" in map_row_to_published_text(row, "Sam")  # type: ignore[arg-type]
