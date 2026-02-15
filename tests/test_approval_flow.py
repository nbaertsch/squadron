"""Tests for approval flow config matching and PR handling logic."""

import pytest

from squadron.config import ApprovalFlowConfig, ApprovalFlowRule
from squadron.agent_manager import AgentManager


class TestApprovalFlowRule:
    def test_matches_label(self):
        rule = ApprovalFlowRule(
            name="security",
            match_labels=["security"],
            reviewers=["security-review"],
        )
        assert rule.matches(["security", "bug"]) is True
        assert rule.matches(["feature"]) is False

    def test_matches_path_glob(self):
        rule = ApprovalFlowRule(
            name="auth",
            match_paths=["src/**/auth/**"],
            reviewers=["security-review"],
        )
        assert rule.matches([], ["src/squadron/auth/login.py"]) is True
        assert rule.matches([], ["src/squadron/models.py"]) is False

    def test_matches_label_and_path(self):
        rule = ApprovalFlowRule(
            name="both",
            match_labels=["security"],
            match_paths=["src/**/auth/**"],
            reviewers=["security-review"],
        )
        # Label must match, path must match
        assert rule.matches(["security"], ["src/squadron/auth/login.py"]) is True
        # Label matches, no path provided — path check skipped
        assert rule.matches(["security"]) is True
        # Label doesn't match
        assert rule.matches(["feature"], ["src/squadron/auth/login.py"]) is False

    def test_empty_rule_matches_everything(self):
        rule = ApprovalFlowRule(name="catchall", reviewers=["pr-review"])
        assert rule.matches([]) is True
        assert rule.matches(["any-label"]) is True

    def test_no_match_when_label_missing(self):
        rule = ApprovalFlowRule(
            name="needs-label",
            match_labels=["infrastructure"],
            reviewers=["pr-review"],
        )
        assert rule.matches([]) is False


class TestApprovalFlowConfig:
    def test_default_reviewers_always_included(self):
        config = ApprovalFlowConfig(
            default_reviewers=["pr-review"],
            rules=[],
        )
        roles = config.get_reviewers_for_pr([])
        assert "pr-review" in roles

    def test_rule_adds_reviewers(self):
        config = ApprovalFlowConfig(
            default_reviewers=["pr-review"],
            rules=[
                ApprovalFlowRule(
                    name="security",
                    match_labels=["security"],
                    reviewers=["security-review"],
                ),
            ],
        )
        roles = config.get_reviewers_for_pr(["security"])
        assert "pr-review" in roles
        assert "security-review" in roles

    def test_no_duplicate_roles(self):
        config = ApprovalFlowConfig(
            default_reviewers=["pr-review"],
            rules=[
                ApprovalFlowRule(
                    name="r1",
                    match_labels=["feature"],
                    reviewers=["pr-review"],
                ),
            ],
        )
        roles = config.get_reviewers_for_pr(["feature"])
        assert roles.count("pr-review") == 1

    def test_multiple_rules_combine(self):
        config = ApprovalFlowConfig(
            default_reviewers=[],
            rules=[
                ApprovalFlowRule(
                    name="a",
                    match_labels=["security"],
                    reviewers=["security-review"],
                ),
                ApprovalFlowRule(
                    name="b",
                    match_labels=["security"],
                    reviewers=["pr-review"],
                ),
            ],
        )
        roles = config.get_reviewers_for_pr(["security"])
        assert "security-review" in roles
        assert "pr-review" in roles

    def test_unmatched_labels_only_defaults(self):
        config = ApprovalFlowConfig(
            default_reviewers=["pr-review"],
            rules=[
                ApprovalFlowRule(
                    name="security",
                    match_labels=["security"],
                    reviewers=["security-review"],
                ),
            ],
        )
        roles = config.get_reviewers_for_pr(["feature"])
        assert roles == ["pr-review"]

    def test_path_matching_in_config(self):
        config = ApprovalFlowConfig(
            default_reviewers=[],
            rules=[
                ApprovalFlowRule(
                    name="infra",
                    match_paths=["Dockerfile", ".github/**"],
                    reviewers=["pr-review"],
                ),
            ],
        )
        assert config.get_reviewers_for_pr([], ["Dockerfile"]) == ["pr-review"]
        assert config.get_reviewers_for_pr([], [".github/workflows/ci.yml"]) == ["pr-review"]
        assert config.get_reviewers_for_pr([], ["src/main.py"]) == []


class TestExtractIssueNumber:
    def test_closes_hash(self):
        assert AgentManager._extract_issue_number("Closes #42") == 42

    def test_fixes_hash(self):
        assert AgentManager._extract_issue_number("Fixes #7") == 7

    def test_resolves_hash(self):
        assert AgentManager._extract_issue_number("Resolves #100") == 100

    def test_case_insensitive(self):
        assert AgentManager._extract_issue_number("CLOSES #5") == 5

    def test_embedded_in_text(self):
        result = AgentManager._extract_issue_number(
            "This PR implements the feature.\nCloses #33\nSee also #44."
        )
        assert result == 33

    def test_no_match(self):
        assert AgentManager._extract_issue_number("No issue reference here") is None

    def test_empty_body(self):
        assert AgentManager._extract_issue_number("") is None
