from pathlib import Path

import pytest

from bridge.config import Config, load_config, map_toml_to_config, read_overlay


def test_people_keys_come_from_the_people_file_and_never_from_config_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    people = tmp_path / "people"
    people.mkdir()
    (people / "people.toml").write_text('owner_name = "Sam"\n[review_mention_people]\nKai = "U01AB2CD3EF"\n')
    config_file = tmp_path / "config.toml"
    config_file.write_text(f'people_dir = "{people}"\nassistant_handle = "aide"\n')
    monkeypatch.setenv("BRIDGE_CONFIG", str(config_file))
    config = load_config()
    assert config.owner_name == "Sam" and config.review_mention_people == (("Kai", "U01AB2CD3EF"),)
    assert config.assistant_handle == "aide"

    config_file.write_text(f'people_dir = "{people}"\nowner_name = "Sam"\n')
    with pytest.raises(ValueError, match=r"owner_name belong in people\.toml"):
        load_config()

    config_file.write_text(f'people_dir = "{people}"\n')
    (people / "people.toml").write_text('review_org = "Org"\n')
    with pytest.raises(ValueError, match="may hold only"):
        load_config()


def test_without_a_people_dir_config_toml_holds_everything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('owner_name = "Sam"\n')
    monkeypatch.setenv("BRIDGE_CONFIG", str(config_file))
    assert load_config().owner_name == "Sam"


def test_an_overlay_slot_replaces_the_generic_text_and_fills_its_fields(tmp_path: Path) -> None:
    config = Config(overlay_dir=str(tmp_path))
    assert read_overlay(config, "rules", "generic for {owner}", owner="Sam") == "generic for Sam"
    (tmp_path / "rules.md").write_text("house rule for {owner}\n")
    assert read_overlay(config, "rules", "generic", owner="Sam") == "house rule for Sam"
    (tmp_path / "rules.md").write_text("a {brace} that is no field\n")
    assert read_overlay(config, "rules", owner="Sam") == "a {brace} that is no field"
    assert read_overlay(Config(), "rules", "generic") == "generic"


def test_working_days_and_zone_are_checked() -> None:
    assert map_toml_to_config({"work_days": [6, 0], "work_timezone": "Etc/GMT-9"}).work_days == (6, 0)
    with pytest.raises(ValueError, match="weekday numbers"):
        map_toml_to_config({"work_days": [7]})
    with pytest.raises(ValueError, match="IANA time zone"):
        map_toml_to_config({"work_timezone": "Mars/Base"})


def test_review_names_are_the_required_reviews_then_the_bot() -> None:
    config = map_toml_to_config({"required_reviews": ["Codex", "Claude"], "review_bot_name": "ReviewBot"})
    assert config.review_names == ("Codex", "Claude", "ReviewBot")
    assert map_toml_to_config({"grooming_states": ["Grooming"]}).grooming_states == ("grooming",)


def test_worker_brief_rules_take_the_overlays_grooming_and_reviews_texts(tmp_path: Path) -> None:
    from bridge.leads import map_names_to_brief_rules

    generic = map_names_to_brief_rules(["Alex", "Aide"], Config(owner_name="Sam", assistant_handle="aide"))
    assert "a part headed Reviews" in generic and "Grooming" not in generic
    (tmp_path / "grooming.md").write_text("In Grooming, ask {owner} through {assistant}.\n")
    (tmp_path / "brief-reviews.md").write_text("Two reviews before {owner}'s queue.\n")
    config = Config(owner_name="Sam", assistant_handle="aide", overlay_dir=str(tmp_path))
    house = map_names_to_brief_rules(["Alex", "Aide"], config)
    assert "only after Sam confirms through Aide.\nIn Grooming, ask Sam through Aide.\n" in house
    assert "\nTwo reviews before Sam's queue. A PR's review doc follows" in house


def test_the_installs_review_and_worker_keys_default_off() -> None:
    assert (Config().review_ignored_posts, Config().agent_prefix_from_ms, Config().host_worker_disabled_plugins) == (
        (),
        0,
        (),
    )
    config = map_toml_to_config(
        {
            "review_ignored_posts": ["1700000000.000001"],
            "agent_prefix_from_ms": 1_700_000_000_000,
            "host_worker_disabled_plugins": ["chat@plugins"],
            "worker_denied_mcp_servers": ["claude.ai Gmail"],
            "worker_denied_tools": ["Artifact"],
        }
    )
    assert config.review_ignored_posts == ("1700000000.000001",)
    assert config.agent_prefix_from_ms == 1_700_000_000_000
    assert config.host_worker_disabled_plugins == ("chat@plugins",)
    assert (Config().worker_denied_tools, config.worker_denied_tools) == ((), ("Artifact",))
    assert (Config().worker_denied_mcp_servers, config.worker_denied_mcp_servers) == ((), ("claude.ai Gmail",))
