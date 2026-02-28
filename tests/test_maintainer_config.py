"""Tests for the maintainers config via human_groups (issue #137).

The maintainer allowlist lives under ``human_groups.maintainers`` in
SquadronConfig. The ``config.maintainers`` property is a convenience
accessor that reads from that group.
"""

import pytest
import yaml
from pathlib import Path
from pydantic import ValidationError

from squadron.config import SquadronConfig, load_config


class TestMaintainersViaHumanGroups:
    """Unit tests for the human_groups.maintainers validation and property."""

    def test_defaults_to_empty_list_when_no_human_groups(self):
        """maintainers defaults to an empty list when human_groups is absent."""
        config = SquadronConfig(project={"name": "test"})
        assert config.maintainers == []

    def test_defaults_to_empty_list_when_no_maintainers_group(self):
        """maintainers defaults to [] when human_groups exists but has no 'maintainers' key."""
        config = SquadronConfig(
            project={"name": "test"},
            human_groups={"reviewers": ["carol"]},
        )
        assert config.maintainers == []

    def test_accepts_valid_usernames(self):
        """Valid GitHub usernames are accepted."""
        config = SquadronConfig(
            project={"name": "test"},
            human_groups={"maintainers": ["alice", "bob", "charlie-dev"]},
        )
        assert config.maintainers == ["alice", "bob", "charlie-dev"]

    def test_rejects_non_string_values(self):
        """Non-string values in human_groups.maintainers are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            SquadronConfig(
                project={"name": "test"},
                human_groups={"maintainers": [123, "alice"]},
            )
        assert (
            "maintainers" in str(exc_info.value).lower() or "string" in str(exc_info.value).lower()
        )

    def test_rejects_empty_string_username(self):
        """Empty string usernames are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            SquadronConfig(
                project={"name": "test"},
                human_groups={"maintainers": ["alice", ""]},
            )
        assert (
            "empty" in str(exc_info.value).lower() or "maintainers" in str(exc_info.value).lower()
        )

    def test_rejects_whitespace_only_username(self):
        """Whitespace-only usernames are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            SquadronConfig(
                project={"name": "test"},
                human_groups={"maintainers": ["alice", "   "]},
            )
        assert (
            "empty" in str(exc_info.value).lower() or "maintainers" in str(exc_info.value).lower()
        )

    def test_strips_whitespace_from_usernames(self):
        """Leading/trailing whitespace is stripped from usernames."""
        config = SquadronConfig(
            project={"name": "test"},
            human_groups={"maintainers": ["  alice  ", " bob"]},
        )
        assert config.maintainers == ["alice", "bob"]

    def test_accepts_bot_username_in_maintainers(self):
        """The bot identity can be listed in maintainers (though the bot is always permitted)."""
        config = SquadronConfig(
            project={"name": "test"},
            human_groups={"maintainers": ["squadron-dev[bot]", "alice"]},
        )
        assert "squadron-dev[bot]" in config.maintainers

    def test_maintainers_loaded_from_yaml(self, tmp_path: Path):
        """human_groups.maintainers is correctly loaded from config.yaml."""
        sq = tmp_path / ".squadron"
        sq.mkdir()
        (sq / "config.yaml").write_text(
            yaml.dump(
                {
                    "project": {"name": "test-project"},
                    "human_groups": {"maintainers": ["alice", "bob"]},
                }
            )
        )
        config = load_config(sq)
        assert config.maintainers == ["alice", "bob"]

    def test_empty_maintainers_in_yaml(self, tmp_path: Path):
        """An empty maintainers group in YAML is accepted (locks down event processing)."""
        sq = tmp_path / ".squadron"
        sq.mkdir()
        (sq / "config.yaml").write_text(
            yaml.dump(
                {
                    "project": {"name": "test-project"},
                    "human_groups": {"maintainers": []},
                }
            )
        )
        config = load_config(sq)
        assert config.maintainers == []

    def test_missing_human_groups_in_yaml_defaults_to_empty(self, tmp_path: Path):
        """When human_groups is absent from config.yaml, maintainers defaults to []."""
        sq = tmp_path / ".squadron"
        sq.mkdir()
        (sq / "config.yaml").write_text(yaml.dump({"project": {"name": "test-project"}}))
        config = load_config(sq)
        assert config.maintainers == []

    def test_rejects_none_value_in_maintainers(self):
        """None values in the maintainers group are rejected."""
        with pytest.raises(ValidationError):
            SquadronConfig(
                project={"name": "test"},
                human_groups={"maintainers": [None, "alice"]},
            )

    def test_rejects_list_value_in_maintainers(self):
        """Nested lists in the maintainers group are rejected."""
        with pytest.raises(ValidationError):
            SquadronConfig(
                project={"name": "test"},
                human_groups={"maintainers": [["alice"], "bob"]},
            )

    def test_validates_all_groups_not_just_maintainers(self):
        """Validator runs on all human_groups, not just the maintainers group."""
        with pytest.raises(ValidationError) as exc_info:
            SquadronConfig(
                project={"name": "test"},
                human_groups={
                    "maintainers": ["alice"],
                    "reviewers": [""],  # empty string in a different group
                },
            )
        assert "reviewers" in str(exc_info.value).lower()
