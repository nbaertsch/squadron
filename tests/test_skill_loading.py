"""Tests for skill loading and resolution in agent_manager.py."""

from pathlib import Path
from unittest.mock import MagicMock


from squadron.config import AgentDefinition, SkillDefinition, SkillsConfig, SquadronConfig


def _make_config(tmp_path: Path, skills_config: SkillsConfig | None = None) -> SquadronConfig:
    """Create a minimal SquadronConfig for testing."""
    raw = {
        "project": {"name": "test", "default_branch": "main"},
        "runtime": {
            "default_model": "claude-sonnet-4.6",
            "provider": {"type": "copilot"},
        },
    }
    config = SquadronConfig.model_validate(raw)
    if skills_config is not None:
        config.skills = skills_config
    return config


def _make_agent_def(role: str, skills: list[str]) -> AgentDefinition:
    """Create an AgentDefinition with given skills."""
    return AgentDefinition(
        role=role,
        raw_content="",
        prompt="",
        name=role,
        skills=skills,
    )


def _make_agent_manager_stub(config: SquadronConfig, repo_root: Path) -> MagicMock:
    """Create a minimal stub that exposes _resolve_skill_directories."""
    # Import the real method and bind it to a mock instance
    from squadron.agent_manager import AgentManager

    stub = MagicMock(spec=AgentManager)
    stub.config = config
    stub.repo_root = repo_root
    # Bind the real method so we test the actual implementation
    stub._resolve_skill_directories = AgentManager._resolve_skill_directories.__get__(stub)
    return stub


class TestResolveSkillDirectories:
    def test_empty_skills_returns_empty(self, tmp_path: Path):
        config = _make_config(tmp_path)
        stub = _make_agent_manager_stub(config, tmp_path)
        agent_def = _make_agent_def("feat-dev", [])
        result = stub._resolve_skill_directories(agent_def)
        assert result == []

    def test_resolves_existing_skill_directory(self, tmp_path: Path):
        # Create the skill directory
        skill_dir = tmp_path / ".squadron" / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)

        skills_config = SkillsConfig(
            base_path=".squadron/skills",
            definitions={"my-skill": SkillDefinition(path="my-skill")},
        )
        config = _make_config(tmp_path, skills_config)
        stub = _make_agent_manager_stub(config, tmp_path)
        agent_def = _make_agent_def("feat-dev", ["my-skill"])

        result = stub._resolve_skill_directories(agent_def)

        assert len(result) == 1
        assert result[0] == str(skill_dir)

    def test_resolves_multiple_skills(self, tmp_path: Path):
        # Create both skill directories
        base = tmp_path / ".squadron" / "skills"
        (base / "skill-a").mkdir(parents=True)
        (base / "skill-b").mkdir(parents=True)

        skills_config = SkillsConfig(
            base_path=".squadron/skills",
            definitions={
                "skill-a": SkillDefinition(path="skill-a"),
                "skill-b": SkillDefinition(path="skill-b"),
            },
        )
        config = _make_config(tmp_path, skills_config)
        stub = _make_agent_manager_stub(config, tmp_path)
        agent_def = _make_agent_def("feat-dev", ["skill-a", "skill-b"])

        result = stub._resolve_skill_directories(agent_def)

        assert len(result) == 2
        assert str(base / "skill-a") in result
        assert str(base / "skill-b") in result

    def test_warns_on_unknown_skill(self, tmp_path: Path, caplog):
        import logging

        config = _make_config(tmp_path)
        stub = _make_agent_manager_stub(config, tmp_path)
        agent_def = _make_agent_def("feat-dev", ["nonexistent-skill"])

        with caplog.at_level(logging.WARNING):
            result = stub._resolve_skill_directories(agent_def)

        assert result == []
        assert "nonexistent-skill" in caplog.text
        assert "feat-dev" in caplog.text

    def test_warns_on_missing_directory(self, tmp_path: Path, caplog):
        import logging

        # Define skill but don't create directory
        skills_config = SkillsConfig(
            base_path=".squadron/skills",
            definitions={"my-skill": SkillDefinition(path="my-skill")},
        )
        config = _make_config(tmp_path, skills_config)
        stub = _make_agent_manager_stub(config, tmp_path)
        agent_def = _make_agent_def("feat-dev", ["my-skill"])

        with caplog.at_level(logging.WARNING):
            result = stub._resolve_skill_directories(agent_def)

        assert result == []
        assert "my-skill" in caplog.text

    def test_skips_missing_and_includes_valid(self, tmp_path: Path, caplog):
        import logging

        # Only create one of the two skill directories
        base = tmp_path / ".squadron" / "skills"
        (base / "existing-skill").mkdir(parents=True)
        # "missing-skill" directory is NOT created

        skills_config = SkillsConfig(
            base_path=".squadron/skills",
            definitions={
                "existing-skill": SkillDefinition(path="existing-skill"),
                "missing-skill": SkillDefinition(path="missing-skill"),
            },
        )
        config = _make_config(tmp_path, skills_config)
        stub = _make_agent_manager_stub(config, tmp_path)
        agent_def = _make_agent_def("feat-dev", ["existing-skill", "missing-skill"])

        with caplog.at_level(logging.WARNING):
            result = stub._resolve_skill_directories(agent_def)

        # Only existing-skill should be returned
        assert len(result) == 1
        assert str(base / "existing-skill") in result
        # Warning for missing-skill
        assert "missing-skill" in caplog.text

    def test_custom_base_path(self, tmp_path: Path):
        # Custom base path — must be relative (absolute paths are rejected by SkillsConfig validator)
        custom_base = tmp_path / "custom" / "skills"
        (custom_base / "my-skill").mkdir(parents=True)

        skills_config = SkillsConfig(
            base_path="custom/skills",  # relative to repo_root (tmp_path)
            definitions={"my-skill": SkillDefinition(path="my-skill")},
        )
        config = _make_config(tmp_path, skills_config)
        stub = _make_agent_manager_stub(config, tmp_path)
        agent_def = _make_agent_def("feat-dev", ["my-skill"])

        result = stub._resolve_skill_directories(agent_def)

        assert len(result) == 1
        assert str(custom_base / "my-skill") in result

    def test_unknown_skill_does_not_block_agent(self, tmp_path: Path):
        """Missing skills should log warnings but not raise exceptions."""
        config = _make_config(tmp_path)
        stub = _make_agent_manager_stub(config, tmp_path)
        agent_def = _make_agent_def("feat-dev", ["does-not-exist", "also-missing"])

        # Should not raise — just return empty list with warnings
        result = stub._resolve_skill_directories(agent_def)
        assert result == []

    def test_mixed_known_unknown_skills(self, tmp_path: Path, caplog):
        import logging

        base = tmp_path / ".squadron" / "skills"
        (base / "known-skill").mkdir(parents=True)

        skills_config = SkillsConfig(
            base_path=".squadron/skills",
            definitions={"known-skill": SkillDefinition(path="known-skill")},
        )
        config = _make_config(tmp_path, skills_config)
        stub = _make_agent_manager_stub(config, tmp_path)
        # "unknown-skill" is not in definitions
        agent_def = _make_agent_def("pm", ["known-skill", "unknown-skill"])

        with caplog.at_level(logging.WARNING):
            result = stub._resolve_skill_directories(agent_def)

        assert len(result) == 1
        assert str(base / "known-skill") in result
        assert "unknown-skill" in caplog.text


class TestSkillFrontmatterIntegration:
    """Integration tests: frontmatter → AgentDefinition.skills → resolution."""

    def test_agent_definition_skills_parsed_and_resolved(self, tmp_path: Path):
        """End-to-end: parse agent def with skills, resolve to directories."""
        from squadron.config import parse_agent_definition

        # Create skill directory
        base = tmp_path / ".squadron" / "skills"
        (base / "squadron-internals").mkdir(parents=True)

        skills_config = SkillsConfig(
            base_path=".squadron/skills",
            definitions={
                "squadron-internals": SkillDefinition(
                    path="squadron-internals", description="Framework arch"
                )
            },
        )
        config = _make_config(tmp_path, skills_config)
        stub = _make_agent_manager_stub(config, tmp_path)

        content = (
            "---\n"
            "name: feat-dev\n"
            "skills: [squadron-internals]\n"
            "---\n\nYou are a feature developer.\n"
        )
        agent_def = parse_agent_definition("feat-dev", content)

        assert agent_def.skills == ["squadron-internals"]

        result = stub._resolve_skill_directories(agent_def)
        assert len(result) == 1
        assert str(base / "squadron-internals") in result


class TestSkillDirectoryContainment:
    """Regression tests for runtime path containment in _resolve_skill_directories.

    These tests verify that skill paths are validated to be within the repo root,
    even if config-level validators are somehow bypassed.
    """

    def _make_manager(self, tmp_path: Path, skills_config: SkillsConfig):
        from unittest.mock import MagicMock

        from squadron.agent_manager import AgentManager

        config = _make_config(tmp_path, skills_config)
        manager = AgentManager.__new__(AgentManager)
        manager.config = config
        manager.repo_root = tmp_path
        manager._github = MagicMock()
        manager._active_agents = {}
        manager._sleeping_agents = {}
        return manager

    def test_containment_blocks_absolute_skill_path(self, tmp_path: Path, caplog):
        """A skill path that escapes the repo root is skipped with a warning."""
        import logging

        # Create a skill dir OUTSIDE tmp_path (simulating escape)
        outside_dir = tmp_path.parent / "outside-repo-skill"
        outside_dir.mkdir(exist_ok=True)
        (outside_dir / "secret.md").write_text("sensitive data")

        # The skill path is relative but resolves outside repo_root via symlink
        # We test by patching: create a subdir that is actually a symlink to outside
        evil_link = tmp_path / "skills" / "evil-skill"
        evil_link.parent.mkdir(parents=True, exist_ok=True)
        evil_link.symlink_to(outside_dir)

        skills_config = SkillsConfig(
            base_path="skills",
            definitions={"evil-skill": SkillDefinition(path="evil-skill")},
        )
        manager = self._make_manager(tmp_path, skills_config)
        agent_def = _make_agent_def("test-agent", ["evil-skill"])

        with caplog.at_level(logging.WARNING):
            result = manager._resolve_skill_directories(agent_def)

        # The symlinked outside-repo path should be blocked
        assert result == [], f"Expected empty list but got: {result}"
        assert any(
            "outside" in r.message or "containment" in r.message or "repo" in r.message.lower()
            for r in caplog.records
        ), f"Expected containment warning but got: {[r.message for r in caplog.records]}"

    def test_containment_allows_valid_skill_path(self, tmp_path: Path):
        """A skill path within the repo root is allowed."""
        skill_dir = tmp_path / "skills" / "good-skill"
        skill_dir.mkdir(parents=True)

        skills_config = SkillsConfig(
            base_path="skills",
            definitions={"good-skill": SkillDefinition(path="good-skill")},
        )
        manager = self._make_manager(tmp_path, skills_config)
        agent_def = _make_agent_def("test-agent", ["good-skill"])

        result = manager._resolve_skill_directories(agent_def)
        assert len(result) == 1
        assert "good-skill" in result[0]
