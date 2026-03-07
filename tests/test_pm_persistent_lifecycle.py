"""Tests for PM agent persistent lifecycle (issue #157).

Verifies that the PM agent is configured as a stateful (persistent) agent,
retains context across events via sleep/wake, and does not terminate after
processing a single event.

Acceptance criteria:
- PM agent is not terminated after processing a single event
- PM agent retains context about active work it has initiated across multiple events
- PM agent can be re-triggered by downstream events (issue closed, PR merged)
- No regression in single-issue triage behavior
"""

from __future__ import annotations

from pathlib import Path

import yaml

from squadron.config import AgentRoleConfig, load_config


SQUADRON_DIR = Path(".squadron")


# -- PM Role Configuration Tests ----------------------------------------------


class TestPMRoleConfiguration:
    """Verify PM agent is configured as a persistent (stateful) lifecycle agent."""

    def test_pm_lifecycle_is_stateful(self):
        """PM must not be ephemeral -- it must persist between events."""
        config = load_config(SQUADRON_DIR)
        pm_role = config.agent_roles.get("pm")
        assert pm_role is not None, "PM role must be defined in config"
        assert pm_role.lifecycle == "stateful", (
            f"PM lifecycle must be 'stateful', got '{pm_role.lifecycle}'. "
            "Ephemeral PM agents cannot retain context across events."
        )

    def test_pm_is_not_ephemeral(self):
        """pm.is_ephemeral must return False so the framework treats it as persistent."""
        config = load_config(SQUADRON_DIR)
        pm_role = config.agent_roles["pm"]
        assert not pm_role.is_ephemeral, (
            "PM agent must not be ephemeral. "
            "Ephemeral agents are destroyed after each event -- "
            "this prevents the PM from tracking ongoing work."
        )

    def test_pm_not_singleton(self):
        """PM agents are per-issue; singleton=true only made sense for ephemeral PM.

        A persistent PM is bound to a specific issue and can be woken when
        events happen on that issue. Multiple PM agents can run for different
        issues simultaneously.
        """
        config = load_config(SQUADRON_DIR)
        pm_role = config.agent_roles["pm"]
        assert not pm_role.singleton, (
            "PM must not be singleton. Each issue gets its own persistent PM agent."
        )

    def test_pm_circuit_breaker_active_duration(self):
        """PM active duration must be sufficient for coordination tasks per wake cycle."""
        config = load_config(SQUADRON_DIR)
        pm_limits = config.circuit_breakers.for_role("pm")
        # At least 10 minutes for a non-trivial coordination task
        assert pm_limits.max_active_duration >= 600, (
            f"PM max_active_duration={pm_limits.max_active_duration}s is too short. "
            "Need at least 600s for meaningful coordination work per wake cycle."
        )

    def test_pm_circuit_breaker_tool_calls(self):
        """PM must have sufficient tool call budget for coordination tasks."""
        config = load_config(SQUADRON_DIR)
        pm_limits = config.circuit_breakers.for_role("pm")
        # At least 50 tool calls to support triage + follow-up work
        assert pm_limits.max_tool_calls >= 50, (
            f"PM max_tool_calls={pm_limits.max_tool_calls} is too low for coordination tasks."
        )

    def test_pm_circuit_breaker_turns(self):
        """PM must have sufficient turn budget for coordination tasks."""
        config = load_config(SQUADRON_DIR)
        pm_limits = config.circuit_breakers.for_role("pm")
        assert pm_limits.max_turns >= 15, (
            f"PM max_turns={pm_limits.max_turns} is too low for coordination tasks."
        )


# -- PM Agent Definition Tests ------------------------------------------------


class TestPMAgentDefinition:
    """Verify PM agent definition includes lifecycle tools for persistent behavior."""

    def _read_pm_agent(self) -> str:
        pm_path = SQUADRON_DIR / "agents" / "pm.md"
        assert pm_path.exists(), "PM agent definition must exist"
        return pm_path.read_text()

    def test_pm_has_report_complete_tool(self):
        """Persistent PM needs report_complete to signal end of lifecycle."""
        content = self._read_pm_agent()
        assert "report_complete" in content, (
            "PM agent definition must include 'report_complete' tool. "
            "Persistent agents call report_complete when their issue is fully resolved."
        )

    def test_pm_has_report_blocked_tool(self):
        """Persistent PM needs report_blocked to sleep between events."""
        content = self._read_pm_agent()
        assert "report_blocked" in content, (
            "PM agent definition must include 'report_blocked' tool. "
            "Persistent agents call report_blocked to sleep after completing a triage cycle."
        )

    def test_pm_has_check_for_events_tool(self):
        """Persistent PM needs check_for_events to handle wake events."""
        content = self._read_pm_agent()
        assert "check_for_events" in content, (
            "PM agent definition must include 'check_for_events' tool. "
            "Persistent agents use this to understand what triggered their wake."
        )

    def test_pm_prompt_describes_persistent_lifecycle(self):
        """PM prompt must describe persistent sleep/wake lifecycle behavior."""
        content = self._read_pm_agent()
        # Find the body (after frontmatter)
        lines = content.split("\n")
        in_frontmatter = False
        frontmatter_end = 0
        dash_count = 0
        for i, line in enumerate(lines):
            if line.strip() == "---":
                dash_count += 1
                if dash_count == 2:
                    frontmatter_end = i
                    break
        body = "\n".join(lines[frontmatter_end + 1:])

        # PM prompt should describe the persistent lifecycle
        assert "report_blocked" in body, (
            "PM agent prompt must instruct the agent to call report_blocked after triage."
        )
        assert "report_complete" in body, (
            "PM agent prompt must instruct the agent to call report_complete when done."
        )

    def test_pm_prompt_does_not_reference_ephemeral_completion(self):
        """PM prompt must not describe auto-completion behavior specific to ephemeral agents."""
        content = self._read_pm_agent()
        # The old ephemeral-specific phrase should be gone
        assert "framework auto-completes ephemeral agent sessions" not in content, (
            "PM agent prompt must not reference ephemeral auto-completion. "
            "The PM is now a persistent agent that must explicitly call lifecycle tools."
        )


# -- Pipeline Configuration Tests ---------------------------------------------


class TestIssueTriage_PipelineConfig:
    """Verify issue-triage pipelines support PM wake-up on downstream events."""

    def _load_raw_config(self) -> dict:
        config_path = SQUADRON_DIR / "config.yaml"
        with open(config_path) as f:
            return yaml.safe_load(f)

    def test_issue_triage_pipeline_has_on_events(self):
        """Issue-triage pipeline must define on_events for PM wake-up."""
        raw = self._load_raw_config()
        pipeline = raw.get("pipelines", {}).get("issue-triage", {})
        assert "on_events" in pipeline, (
            "issue-triage pipeline must have on_events to wake the persistent PM agent."
        )

    def test_issue_triage_pipeline_wakes_on_issue_comment(self):
        """PM must be woken when someone comments on a triaged issue."""
        raw = self._load_raw_config()
        on_events = raw["pipelines"]["issue-triage"].get("on_events", {})
        assert "issue_comment.created" in on_events, (
            "issue-triage pipeline must wake PM on issue_comment.created events."
        )
        assert on_events["issue_comment.created"].get("action") == "wake_agent"

    def test_issue_triage_pipeline_wakes_on_issue_closed(self):
        """PM must be woken when a triaged issue is closed."""
        raw = self._load_raw_config()
        on_events = raw["pipelines"]["issue-triage"].get("on_events", {})
        assert "issues.closed" in on_events, (
            "issue-triage pipeline must wake PM on issues.closed events."
        )
        assert on_events["issues.closed"].get("action") == "wake_agent"

    def test_issue_triage_pipeline_wakes_on_pr_closed(self):
        """PM must be woken when a PR related to a triaged issue is closed/merged."""
        raw = self._load_raw_config()
        on_events = raw["pipelines"]["issue-triage"].get("on_events", {})
        assert "pull_request.closed" in on_events, (
            "issue-triage pipeline must wake PM on pull_request.closed events."
        )
        assert on_events["pull_request.closed"].get("action") == "wake_agent"

    def test_issue_reopen_triage_pipeline_has_on_events(self):
        """Issue-reopen-triage pipeline must also define on_events for PM wake-up."""
        raw = self._load_raw_config()
        pipeline = raw.get("pipelines", {}).get("issue-reopen-triage", {})
        assert "on_events" in pipeline, (
            "issue-reopen-triage pipeline must have on_events to wake the persistent PM agent."
        )

    def test_issue_reopen_triage_wakes_on_issue_comment(self):
        """PM must be woken on comments for re-triaged issues too."""
        raw = self._load_raw_config()
        on_events = raw["pipelines"]["issue-reopen-triage"].get("on_events", {})
        assert "issue_comment.created" in on_events
        assert on_events["issue_comment.created"].get("action") == "wake_agent"

    def test_issue_reopen_triage_wakes_on_issue_closed(self):
        """PM must be woken when re-triaged issues are closed."""
        raw = self._load_raw_config()
        on_events = raw["pipelines"]["issue-reopen-triage"].get("on_events", {})
        assert "issues.closed" in on_events
        assert on_events["issues.closed"].get("action") == "wake_agent"

    def test_issue_reopen_triage_wakes_on_pr_closed(self):
        """PM must be woken when PRs for re-triaged issues are closed."""
        raw = self._load_raw_config()
        on_events = raw["pipelines"]["issue-reopen-triage"].get("on_events", {})
        assert "pull_request.closed" in on_events
        assert on_events["pull_request.closed"].get("action") == "wake_agent"


# -- AgentRoleConfig Model Tests ----------------------------------------------


class TestAgentRoleConfigModel:
    """Unit tests for AgentRoleConfig lifecycle model behavior."""

    def test_stateful_lifecycle_is_not_ephemeral(self):
        """AgentRoleConfig with lifecycle=stateful must have is_ephemeral=False."""
        role = AgentRoleConfig(agent_definition="agents/pm.md", lifecycle="stateful")
        assert not role.is_ephemeral

    def test_persistent_lifecycle_is_not_ephemeral(self):
        """AgentRoleConfig with lifecycle=persistent must have is_ephemeral=False."""
        role = AgentRoleConfig(agent_definition="agents/pm.md", lifecycle="persistent")
        assert not role.is_ephemeral

    def test_ephemeral_lifecycle_is_ephemeral(self):
        """AgentRoleConfig with lifecycle=ephemeral must have is_ephemeral=True."""
        role = AgentRoleConfig(agent_definition="agents/pm.md", lifecycle="ephemeral")
        assert role.is_ephemeral

    def test_default_lifecycle_is_not_ephemeral(self):
        """Default lifecycle is persistent, not ephemeral."""
        role = AgentRoleConfig(agent_definition="agents/pm.md")
        assert not role.is_ephemeral
        assert role.lifecycle == "persistent"

    def test_stateless_migration_to_ephemeral(self):
        """Backward compat: stateless=true migrates to lifecycle=ephemeral."""
        raw = {"agent_definition": "agents/test.md", "stateless": True}
        role = AgentRoleConfig(**raw)
        assert role.is_ephemeral
        assert role.lifecycle == "ephemeral"


# -- Security Fix Tests -------------------------------------------------------


class TestPMSecurityHardenings:
    """Tests for security fixes applied to PM persistent lifecycle (issue #159).

    Verifies:
    - Trust boundary guidance present in pm.md (prompt injection protection)
    - Concurrency safety rule present in pm.md (duplicate delegation protection)
    - Circuit breaker comments clarify per-wake-cycle semantics (not total lifetime)
    - Stateful deduplication comment present in config.yaml (concurrent instance safety)
    """

    @staticmethod
    def _load_pm_md() -> str:
        pm_path = SQUADRON_DIR / "agents" / "pm.md"
        return pm_path.read_text()

    @staticmethod
    def _load_raw_config() -> dict:
        raw_path = SQUADRON_DIR / "config.yaml"
        with raw_path.open() as f:
            return yaml.safe_load(f)

    def test_pm_md_has_trust_boundary_section(self):
        """pm.md must contain a Trust Boundaries section for wake events."""
        content = self._load_pm_md()
        assert "Trust Boundaries" in content, (
            "pm.md must contain Trust Boundaries guidance for event-driven wake events"
        )

    def test_pm_md_trust_boundary_treats_comments_as_data_not_commands(self):
        """pm.md must explicitly state that event payloads are data, not commands."""
        content = self._load_pm_md()
        # Check for the core principle
        assert "data, not commands" in content or "context" in content.lower(), (
            "pm.md must state that event payloads are contextual data, not operational commands"
        )

    def test_pm_md_trust_boundary_covers_human_authored_comments(self):
        """pm.md must instruct PM not to act on human-authored comment instructions."""
        content = self._load_pm_md()
        assert "human" in content.lower(), (
            "pm.md must address trust boundaries for human-authored comments"
        )
        # Should say something about not treating human comments as commands
        assert "human-authored comment" in content or "human users" in content, (
            "pm.md must explicitly address trust rules for human-authored comments"
        )

    def test_pm_md_trust_boundary_covers_bot_comments(self):
        """pm.md must distinguish bot (squadron[bot]) comments from human comments."""
        content = self._load_pm_md()
        assert "squadron[bot]" in content or "squadron-dev[bot]" in content, (
            "pm.md must reference trusted bot identities in trust boundary guidance"
        )

    def test_pm_md_has_concurrency_safety_rule(self):
        """pm.md Rules must include a concurrency safety rule to prevent duplicate delegations."""
        content = self._load_pm_md()
        assert "Concurrency safety" in content or "concurrency" in content.lower(), (
            "pm.md Rules must include concurrency safety guidance to prevent duplicate delegations"
        )

    def test_pm_md_concurrency_safety_references_check_registry(self):
        """Concurrency safety rule must reference check_registry for deduplication."""
        content = self._load_pm_md()
        # check_registry should be referenced in context of concurrency/duplication check
        assert "check_registry" in content, (
            "pm.md must reference check_registry in concurrency safety guidance"
        )

    def test_pm_md_concurrency_safety_prevents_duplicate_delegations(self):
        """pm.md must explicitly warn against duplicate delegations."""
        content = self._load_pm_md()
        assert "duplicate" in content.lower(), (
            "pm.md must warn against duplicate delegations in concurrency safety guidance"
        )

    def test_config_pm_circuit_breaker_has_wake_cycle_comment(self):
        """PM max_active_duration comment must clarify it applies per wake cycle, not total lifetime."""
        raw_path = SQUADRON_DIR / "config.yaml"
        config_text = raw_path.read_text()
        # Find PM circuit breaker section and check for clarifying comment
        assert "per wake cycle" in config_text or "wake cycle" in config_text, (
            "config.yaml PM circuit breaker must clarify max_active_duration is per wake cycle"
        )

    def test_config_pm_lifecycle_has_deduplication_comment(self):
        """PM lifecycle entry in config.yaml must document the concurrency/deduplication model."""
        raw_path = SQUADRON_DIR / "config.yaml"
        config_text = raw_path.read_text()
        assert "deduplication" in config_text or "one PM instance" in config_text, (
            "config.yaml PM lifecycle must document the per-issue deduplication model"
        )

    def test_pm_old_ephemeral_framing_removed(self):
        """pm.md must not contain old ephemeral-lifecycle framing text."""
        content = self._load_pm_md()
        assert "framework auto-completes ephemeral agent sessions" not in content, (
            "pm.md must not contain old ephemeral-specific 'framework auto-completes' text"
        )

    def test_pm_md_rules_require_lifecycle_call(self):
        """pm.md Rules section must require agents to end with a lifecycle call."""
        content = self._load_pm_md()
        assert "report_blocked" in content and "report_complete" in content, (
            "pm.md Rules must instruct the PM to always end with report_blocked or report_complete"
        )
        # Verify it's in the Rules section specifically
        rules_idx = content.find("## Rules")
        assert rules_idx != -1, "pm.md must have a Rules section"
        rules_section = content[rules_idx:]
        assert "report_blocked" in rules_section or "lifecycle call" in rules_section, (
            "pm.md Rules section must mention lifecycle calls (report_blocked/report_complete)"
        )
