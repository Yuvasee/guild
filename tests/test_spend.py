import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from conftest import http_request

from bridge.spend import map_file_to_spend


def write_spend(path: Path, made: datetime) -> None:
    path.write_text(
        json.dumps(
            {
                "generated_at": made.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "week_start": "2026-11-25T00:00:00Z",
                "accounts": [
                    {"id": "A", "active": True, "weekly_pct": 89.0, "resets_at": "2026-12-02T00:00:00Z"},
                    {"id": "B", "active": False, "weekly_pct": 0, "resets_at": "2026-12-03T05:00:00Z"},
                ],
                "leads": [{"lead": "alex", "share": 0.103, "est_pct_of_active_week": 9.3, "requests": 4141}],
                "harness": {"share": 0.147, "est_pct_of_active_week": 13.4},
                "other": {"share": 0.021, "est_pct_of_active_week": 2.0},
                "page": "http://127.0.0.1:8765/p/usage-vs-work",
            }
        )
    )


def ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def test_a_fresh_file_gives_each_account_lead_harness_and_other(tmp_path: Path) -> None:
    now = datetime(2026, 11, 26, 13, 30, tzinfo=UTC)
    write_spend(tmp_path / "spend.json", now - timedelta(minutes=25))

    spend = map_file_to_spend(str(tmp_path / "spend.json"), ms(now))

    assert spend is not None
    accounts = cast(list[dict[str, object]], spend["accounts"])
    assert [(a["id"], a["active"], a["weekly_pct"]) for a in accounts] == [("A", True, 89.0), ("B", False, 0.0)]
    assert accounts[0]["resets_at"] == ms(datetime(2026, 12, 2, tzinfo=UTC))
    assert spend["rows"] == [{"name": "alex", "share": 0.103, "est_pct": 9.3}]
    assert spend["harness"] == {"name": "harness", "share": 0.147, "est_pct": 13.4}
    assert spend["other"] == {"name": "other", "share": 0.021, "est_pct": 2.0}
    assert spend["page"] == "http://127.0.0.1:8765/p/usage-vs-work"


def test_no_setting_a_missing_file_or_a_broken_one_hides_the_block(tmp_path: Path) -> None:
    now = ms(datetime(2026, 11, 26, tzinfo=UTC))
    (tmp_path / "bad.json").write_text("{not json")

    assert map_file_to_spend("", now) is None
    assert map_file_to_spend(str(tmp_path / "missing.json"), now) is None
    assert map_file_to_spend(str(tmp_path / "bad.json"), now) is None


def test_a_file_older_than_three_hours_gives_only_its_age(tmp_path: Path) -> None:
    now = datetime(2026, 11, 26, 13, 30, tzinfo=UTC)
    write_spend(tmp_path / "spend.json", now - timedelta(hours=5, minutes=10))

    assert map_file_to_spend(str(tmp_path / "spend.json"), ms(now)) == {"stale_hours": 5}
    write_spend(tmp_path / "spend.json", now - timedelta(hours=3))
    assert map_file_to_spend(str(tmp_path / "spend.json"), ms(now)) is not None


def test_the_guild_api_has_no_block_without_a_spend_file(dashboard: str) -> None:
    assert json.loads(http_request(dashboard + "/api/guild"))["spend"] is None


def test_an_unknown_weekly_pct_stays_unknown_and_only_web_links_are_kept(tmp_path: Path) -> None:
    path = tmp_path / "spend.json"
    now = 1_800_000_000_000
    made = datetime.fromtimestamp(now / 1000, UTC).isoformat()
    path.write_text(
        json.dumps({"generated_at": made, "accounts": [{"id": "B", "weekly_pct": None}], "page": "javascript:x"})
    )
    spend = map_file_to_spend(str(path), now)
    assert spend is not None
    assert spend["accounts"] == [{"id": "B", "active": False, "weekly_pct": None, "resets_at": None}]
    assert spend["page"] == ""
