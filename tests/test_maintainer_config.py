"""Tests for the maintainers config field (issue #137)."""

import pytest
import yaml
from pathlib import Path
from pydantic import ValidationError

from squadron.config import SquadronConfig, load_config


class TestMaintainersField:
    """Unit tests for the maintainers config field validation."""

    def test_defaults_to_empty_list(self):
        """maintainers defaults to an empty list when not set."""
        config = SquadronConfig(project={"name": "test"})
        assert config.maintainers == []

    def test_accepts_valid_usernames(self):
        """Valid GitHub usernames are accepted."""
        config = SquadronConfig(
            project={"name": "test"},
            maintainers=["alice", "bob", "charlie-dev"],
        )
        assert config.maintainers == ["alice", "bob", "charlie-dev"]

    def test_rejects_non_string_values(self):
        """Non-string values in maintainers list are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            SquadronConfig(
                project={"name": "test"},
                maintainers=[123, "alice"],
            )
        assert "maintainers" in str(exc_info.value).lower() or "string" in str(exc_info.value).lower()

    def test_rejects_empty_string_username(self):
        """Empty string usernames are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            SquadronConfig(
                project={"name": "test"},
                maintainers=["alice", ""],
            )
        assert "empty" in str(exc_info.value).lower() or "maintainers" in str(exc_info.value).lower()

    def test_rejects_whitespace_only_username(self):
        """Whitespace-only usernames are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            SquadronConfig(
                project={"name": "test"},
                maintainers=["alice", "   "],
            )
        assert "empty" in str(exc_info.value).lower() or "maintainers" in str(exc_info.value).lower()

    def test_strips_whitespace_from_usernames(self):
        """Leading/trailing whitespace is stripped from usernames."""
        config = SquadronConfig(
            project={"name": "test"},
            maintainers=["  alice  ", " bob"],
        )
        assert config.maintainers == ["alice", "bob"]

    def test_accepts_bot_username_in_maintainers(self):
        """The bot identity can be listed in maintainers (though the bot is always permitted)."""
        config = SquadronConfig(
            project={"name": "test"},
            maintainers=["squadron-dev[bot]", "alice"],
        )
        assert "squadron-dev[bot]" in config.maintainers

    def test_maintainers_loaded_from_yaml(self, tmp_path: Path):
        """maintainers field is correctly loaded from config.yaml."""
        sq = tmp_path / ".squadron"
        sq.mkdir()
        (sq / "config.yaml").write_text(
            yaml.dump(
                {
                    "project": {"name": "test-project"},
                    "maintainers": ["alice", "bob"],
                }
            )
        )
        config = load_config(sq)
        assert config.maintainers == ["alice", "bob"]

    def test_empty_maintainers_in_yaml(self, tmp_path: Path):
        """An empty maintainers list in YAML is accepted (locks down event processing)."""
        sq = tmp_path / ".squadron"
        sq.mkdir()
        (sq / "config.yaml").write_text(
            yaml.dump(
                {
                    "project": {"name": "test-project"},
                    "maintainers": [],
                }
            )
        )
        config = load_config(sq)
        assert config.maintainers == []

    def test_missing_maintainers_field_in_yaml_defaults_to_empty(self, tmp_path: Path):
        """When maintainers is absent from config.yaml, it defaults to []."""
        sq = tmp_path / ".squadron"
        sq.mkdir()
        (sq / "config.yaml").write_text(
            yaml.dump({"project": {"name": "test-project"}})
        )
        config = load_config(sq)
        assert config.maintainers == []

    def test_rejects_none_value_in_maintainers(self):
        """None values in the maintainers list are rejected."""
        with pytest.raises(ValidationError):
            SquadronConfig(
                project={"name": "test"},
                maintainers=[None, "alice"],
            )

    def test_rejects_list_value_in_maintainers(self):
        """Nested lists in the maintainers list are rejected."""
        with pytest.raises(ValidationError):
            SquadronConfig(
                project={"name": "test"},
                maintainers=[["alice"], "bob"],
            )
