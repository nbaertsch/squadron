"""Tests for the workflow engine — event-driven sequential agent pipelines."""

import pytest
import pytest_asyncio
import yaml

from squadron.config import (
    SquadronConfig,
    WorkflowDefinition,
    WorkflowStage,
    WorkflowTrigger,
    load_workflow_definitions,
)
from squadron.models import SquadronEvent, SquadronEventType
from squadron.registry import AgentRegistry
from squadron.workflow_engine import WorkflowEngine


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def registry(tmp_path):
    db_path = str(tmp_path / "test.db")
    reg = AgentRegistry(db_path)
    await reg.initialize()
    yield reg
    await reg.close()


@pytest.fixture
def config():
    return SquadronConfig(project={"name": "test", "bot_username": "squadron[bot]"})


@pytest.fixture
def pr_review_workflow() -> WorkflowDefinition:
    """The canonical test-coverage → security → PR review & merge pipeline."""
    return WorkflowDefinition(
        name="squadron-dev-pr-pipeline",
        description="Sequential PR review for PRs targeting squadron-dev",
        trigger=WorkflowTrigger(
            event="pull_request.opened",
            conditions={"base_branch": "squadron-dev"},
        ),
        stages=[
            WorkflowStage(
                name="test-coverage",
                agent="test-coverage",
                action="review",
                on_approve="next",
                on_reject="stop",
            ),
            WorkflowStage(
                name="security-review",
                agent="security-review",
                action="review",
                on_approve="next",
                on_reject="stop",
            ),
            WorkflowStage(
                name="final-review",
                agent="pr-review",
                action="review_and_merge",
                on_approve="complete",
                on_reject="stop",
            ),
        ],
    )


def make_pr_opened_event(pr_number: int, base_branch: str = "squadron-dev") -> SquadronEvent:
    """Create a SquadronEvent for a PR opened event."""
    return SquadronEvent(
        event_type=SquadronEventType.PR_OPENED,
        pr_number=pr_number,
        data={
            "action": "opened",
            "sender": "developer",
            "payload": {
                "pull_request": {
                    "number": pr_number,
                    "base": {"ref": base_branch},
                    "head": {"ref": "feat/my-feature"},
                    "labels": [],
                    "body": "Closes #42",
                },
            },
        },
    )


def make_review_event(pr_number: int, state: str = "approved") -> SquadronEvent:
    """Create a SquadronEvent for a PR review submitted event."""
    return SquadronEvent(
        event_type=SquadronEventType.PR_REVIEW_SUBMITTED,
        pr_number=pr_number,
        data={
            "action": "submitted",
            "sender": "squadron[bot]",
            "payload": {
                "review": {
                    "state": state,
                    "user": {"login": "squadron[bot]"},
                    "body": "LGTM",
                },
                "pull_request": {
                    "number": pr_number,
                    "base": {"ref": "squadron-dev"},
                    "head": {"ref": "feat/my-feature"},
                },
            },
        },
    )


# ── WorkflowTrigger Tests ───────────────────────────────────────────────────


class TestWorkflowTrigger:
    def test_matches_event_type(self):
        trigger = WorkflowTrigger(event="pull_request.opened")
        assert trigger.matches_event("pull_request.opened")
        assert not trigger.matches_event("issues.opened")

    def test_matches_base_branch_condition(self):
        trigger = WorkflowTrigger(
            event="pull_request.opened",
            conditions={"base_branch": "squadron-dev"},
        )
        payload = {"pull_request": {"base": {"ref": "squadron-dev"}, "head": {"ref": "feat/x"}}}
        assert trigger.matches_conditions(payload)

        wrong_base = {"pull_request": {"base": {"ref": "main"}, "head": {"ref": "feat/x"}}}
        assert not trigger.matches_conditions(wrong_base)

    def test_matches_head_branch_pattern(self):
        trigger = WorkflowTrigger(
            event="pull_request.opened",
            conditions={"head_branch_pattern": "feat/*"},
        )
        matching = {"pull_request": {"base": {"ref": "main"}, "head": {"ref": "feat/login"}}}
        assert trigger.matches_conditions(matching)

        not_matching = {"pull_request": {"base": {"ref": "main"}, "head": {"ref": "fix/bug"}}}
        assert not trigger.matches_conditions(not_matching)

    def test_matches_labels(self):
        trigger = WorkflowTrigger(
            event="pull_request.opened",
            conditions={"labels": ["needs-review", "security"]},
        )
        has_label = {"pull_request": {"labels": [{"name": "security"}]}}
        assert trigger.matches_conditions(has_label)

        no_match = {"pull_request": {"labels": [{"name": "docs"}]}}
        assert not trigger.matches_conditions(no_match)

    def test_no_conditions_always_matches(self):
        trigger = WorkflowTrigger(event="pull_request.opened")
        assert trigger.matches_conditions({})

    def test_multiple_conditions_all_must_match(self):
        trigger = WorkflowTrigger(
            event="pull_request.opened",
            conditions={
                "base_branch": "squadron-dev",
                "head_branch_pattern": "feat/*",
            },
        )
        both_match = {
            "pull_request": {
                "base": {"ref": "squadron-dev"},
                "head": {"ref": "feat/thing"},
            }
        }
        assert trigger.matches_conditions(both_match)

        one_fails = {
            "pull_request": {
                "base": {"ref": "squadron-dev"},
                "head": {"ref": "fix/thing"},
            }
        }
        assert not trigger.matches_conditions(one_fails)


# ── WorkflowDefinition Tests ────────────────────────────────────────────────


class TestWorkflowDefinition:
    def test_matches_combined(self, pr_review_workflow):
        payload = {
            "pull_request": {
                "base": {"ref": "squadron-dev"},
                "head": {"ref": "feat/x"},
                "labels": [],
            }
        }
        assert pr_review_workflow.matches("pull_request.opened", payload)

    def test_no_match_wrong_event(self, pr_review_workflow):
        payload = {"pull_request": {"base": {"ref": "squadron-dev"}}}
        assert not pr_review_workflow.matches("issues.opened", payload)

    def test_no_match_wrong_branch(self, pr_review_workflow):
        payload = {"pull_request": {"base": {"ref": "main"}}}
        assert not pr_review_workflow.matches("pull_request.opened", payload)

    def test_requires_at_least_one_stage(self):
        with pytest.raises(Exception):
            WorkflowDefinition(
                name="empty",
                trigger=WorkflowTrigger(event="push"),
                stages=[],
            )


# ── YAML Loading Tests ──────────────────────────────────────────────────────


class TestWorkflowLoading:
    def test_load_single_workflow(self, tmp_path):
        sq = tmp_path / ".squadron" / "workflows"
        sq.mkdir(parents=True)

        wf_data = {
            "name": "test-pipeline",
            "trigger": {
                "event": "pull_request.opened",
                "conditions": {"base_branch": "dev"},
            },
            "stages": [
                {"name": "review", "agent": "pr-review", "action": "review"},
            ],
        }
        (sq / "pipeline.yaml").write_text(yaml.dump(wf_data))

        workflows = load_workflow_definitions(tmp_path / ".squadron")
        assert len(workflows) == 1
        assert workflows[0].name == "test-pipeline"
        assert workflows[0].trigger.event == "pull_request.opened"
        assert len(workflows[0].stages) == 1

    def test_load_multiple_workflows_from_list(self, tmp_path):
        sq = tmp_path / ".squadron" / "workflows"
        sq.mkdir(parents=True)

        wf_list = [
            {
                "name": "pipeline-a",
                "trigger": {"event": "pull_request.opened"},
                "stages": [{"name": "s1", "agent": "pr-review"}],
            },
            {
                "name": "pipeline-b",
                "trigger": {"event": "issues.opened"},
                "stages": [{"name": "s1", "agent": "pm"}],
            },
        ]
        (sq / "multi.yaml").write_text(yaml.dump(wf_list))

        workflows = load_workflow_definitions(tmp_path / ".squadron")
        assert len(workflows) == 2
        names = {w.name for w in workflows}
        assert names == {"pipeline-a", "pipeline-b"}

    def test_load_no_workflows_dir(self, tmp_path):
        sq = tmp_path / ".squadron"
        sq.mkdir()
        workflows = load_workflow_definitions(sq)
        assert workflows == []

    def test_bad_yaml_skipped(self, tmp_path):
        sq = tmp_path / ".squadron" / "workflows"
        sq.mkdir(parents=True)
        (sq / "bad.yaml").write_text("not: valid: yaml: [")
        workflows = load_workflow_definitions(tmp_path / ".squadron")
        assert workflows == []


# ── Workflow Engine Tests ────────────────────────────────────────────────────


class TestWorkflowEngine:
    @pytest_asyncio.fixture
    async def engine(self, config, registry, pr_review_workflow):
        eng = WorkflowEngine(
            config=config,
            registry=registry,
            workflows=[pr_review_workflow],
        )
        # Register a mock spawn callback
        self._spawned_agents = []

        async def mock_spawn(
            role, pr_number, event, *, workflow_run_id=None, stage_name=None, action=None
        ):
            agent_id = f"{role}-wf-{stage_name}-{pr_number}"
            self._spawned_agents.append(
                {
                    "agent_id": agent_id,
                    "role": role,
                    "pr_number": pr_number,
                    "stage_name": stage_name,
                    "action": action,
                    "workflow_run_id": workflow_run_id,
                }
            )
            return agent_id

        eng.set_spawn_callback(mock_spawn)
        return eng

    @pytest.mark.asyncio
    async def test_evaluate_triggers_workflow(self, engine, registry):
        event = make_pr_opened_event(pr_number=10)
        payload = event.data["payload"]

        triggered = await engine.evaluate_event("pull_request.opened", payload, event)
        assert triggered is True

        # Verify a workflow run was created
        runs = await registry.get_workflow_runs_for_pr(10)
        assert len(runs) == 1
        assert runs[0]["workflow_name"] == "squadron-dev-pr-pipeline"
        assert runs[0]["current_stage"] == "test-coverage"
        assert runs[0]["stage_index"] == 0
        assert runs[0]["status"] == "active"

        # Verify the first stage agent was spawned
        assert len(self._spawned_agents) == 1
        assert self._spawned_agents[0]["role"] == "test-coverage"
        assert self._spawned_agents[0]["stage_name"] == "test-coverage"

    @pytest.mark.asyncio
    async def test_no_trigger_wrong_branch(self, engine):
        event = make_pr_opened_event(pr_number=11, base_branch="main")
        payload = event.data["payload"]

        triggered = await engine.evaluate_event("pull_request.opened", payload, event)
        assert triggered is False

    @pytest.mark.asyncio
    async def test_no_duplicate_workflow_runs(self, engine, registry):
        event = make_pr_opened_event(pr_number=12)
        payload = event.data["payload"]

        # First trigger
        triggered1 = await engine.evaluate_event("pull_request.opened", payload, event)
        assert triggered1 is True

        # Second trigger for same PR — should be skipped
        triggered2 = await engine.evaluate_event("pull_request.opened", payload, event)
        assert triggered2 is False

        runs = await registry.get_workflow_runs_for_pr(12)
        assert len(runs) == 1

    @pytest.mark.asyncio
    async def test_advance_on_approval(self, engine, registry):
        # Trigger the workflow
        event = make_pr_opened_event(pr_number=20)
        await engine.evaluate_event("pull_request.opened", event.data["payload"], event)

        # Simulate approval from bot → advance to security-review
        review_event = make_review_event(pr_number=20, state="approved")
        advanced = await engine.handle_pr_review(
            pr_number=20,
            reviewer="squadron[bot]",
            review_state="approved",
            payload=review_event.data["payload"],
            squadron_event=review_event,
        )
        assert advanced is True

        runs = await registry.get_workflow_runs_for_pr(20)
        assert len(runs) == 1
        assert runs[0]["current_stage"] == "security-review"
        assert runs[0]["stage_index"] == 1
        assert runs[0]["status"] == "active"

        # Check second stage agent was spawned
        assert len(self._spawned_agents) == 2
        assert self._spawned_agents[1]["role"] == "security-review"
        assert self._spawned_agents[1]["stage_name"] == "security-review"

    @pytest.mark.asyncio
    async def test_full_pipeline_to_completion(self, engine, registry):
        # Trigger
        event = make_pr_opened_event(pr_number=30)
        await engine.evaluate_event("pull_request.opened", event.data["payload"], event)

        # Stage 1 approval → advances to security-review
        review1 = make_review_event(pr_number=30, state="approved")
        await engine.handle_pr_review(
            30, "squadron[bot]", "approved", review1.data["payload"], review1
        )

        # Stage 2 approval → advances to final-review
        review2 = make_review_event(pr_number=30, state="approved")
        await engine.handle_pr_review(
            30, "squadron[bot]", "approved", review2.data["payload"], review2
        )

        # Stage 3 approval → workflow complete (on_approve=complete)
        review3 = make_review_event(pr_number=30, state="approved")
        await engine.handle_pr_review(
            30, "squadron[bot]", "approved", review3.data["payload"], review3
        )

        runs = await registry.get_workflow_runs_for_pr(30)
        # After completion, status changes so it's no longer 'active'
        assert len(runs) == 0  # get_workflow_runs_for_pr only returns active

        # Verify via direct lookup
        # The run should exist with status=completed
        all_spawned = self._spawned_agents
        assert len(all_spawned) == 3
        assert all_spawned[0]["role"] == "test-coverage"
        assert all_spawned[1]["role"] == "security-review"
        assert all_spawned[2]["role"] == "pr-review"
        assert all_spawned[2]["action"] == "review_and_merge"

    @pytest.mark.asyncio
    async def test_rejection_stops_pipeline(self, engine, registry):
        # Trigger
        event = make_pr_opened_event(pr_number=40)
        await engine.evaluate_event("pull_request.opened", event.data["payload"], event)

        # Stage 1 rejection → stop
        review = make_review_event(pr_number=40, state="changes_requested")
        stopped = await engine.handle_pr_review(
            40, "squadron[bot]", "changes_requested", review.data["payload"], review
        )
        assert stopped is True

        # Workflow run should be rejected (no longer active)
        runs = await registry.get_workflow_runs_for_pr(40)
        assert len(runs) == 0  # rejected = not active

    @pytest.mark.asyncio
    async def test_no_review_handling_without_workflow(self, engine, registry):
        # Review on a PR with no active workflow
        review = make_review_event(pr_number=99, state="approved")
        result = await engine.handle_pr_review(
            99, "squadron[bot]", "approved", review.data["payload"], review
        )
        assert result is False


# ── Registry Workflow CRUD Tests ─────────────────────────────────────────────


class TestRegistryWorkflowCRUD:
    @pytest.mark.asyncio
    async def test_create_and_get_workflow_run(self, registry):
        await registry.create_workflow_run(
            run_id="wf-test-001",
            workflow_name="test-pipeline",
            current_stage="stage-1",
            pr_number=42,
            stage_index=0,
        )

        run = await registry.get_workflow_run("wf-test-001")
        assert run is not None
        assert run["workflow_name"] == "test-pipeline"
        assert run["current_stage"] == "stage-1"
        assert run["pr_number"] == 42
        assert run["status"] == "active"

    @pytest.mark.asyncio
    async def test_get_workflow_runs_for_pr(self, registry):
        await registry.create_workflow_run(
            run_id="wf-pr-a", workflow_name="pipe-a", current_stage="s1", pr_number=50
        )
        await registry.create_workflow_run(
            run_id="wf-pr-b", workflow_name="pipe-b", current_stage="s1", pr_number=50
        )

        runs = await registry.get_workflow_runs_for_pr(50)
        assert len(runs) == 2

    @pytest.mark.asyncio
    async def test_advance_workflow_run(self, registry):
        await registry.create_workflow_run(
            run_id="wf-adv", workflow_name="pipe", current_stage="s1", pr_number=60
        )

        await registry.advance_workflow_run("wf-adv", "s2", 1, stage_agent_id="agent-x")

        run = await registry.get_workflow_run("wf-adv")
        assert run["current_stage"] == "s2"
        assert run["stage_index"] == 1
        assert run["stage_agent_id"] == "agent-x"

    @pytest.mark.asyncio
    async def test_complete_workflow_run(self, registry):
        await registry.create_workflow_run(
            run_id="wf-done", workflow_name="pipe", current_stage="s1", pr_number=70
        )

        await registry.complete_workflow_run("wf-done", "completed")

        run = await registry.get_workflow_run("wf-done")
        assert run["status"] == "completed"

        # Should not appear in active runs
        active = await registry.get_workflow_runs_for_pr(70)
        assert len(active) == 0

    @pytest.mark.asyncio
    async def test_get_workflow_run_by_agent(self, registry):
        await registry.create_workflow_run(
            run_id="wf-agent",
            workflow_name="pipe",
            current_stage="s1",
            pr_number=80,
            stage_agent_id="myagent-1",
        )

        run = await registry.get_workflow_run_by_agent("myagent-1")
        assert run is not None
        assert run["run_id"] == "wf-agent"

        # Non-existent agent
        none_run = await registry.get_workflow_run_by_agent("nonexistent")
        assert none_run is None
