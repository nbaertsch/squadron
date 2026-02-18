"""Test security-review agent role behavior.

Regression test for issue #58 - Security-review agent incorrectly claims to have 
fixed code instead of delegating to fix agents.
"""

import pytest
from unittest.mock import Mock, patch
from squadron.config import SquadronConfig
from squadron.models import AgentState, SquadronEvent, EventType


class TestSecurityReviewAgentRole:
    """Test security-review agent role and delegation behavior."""
    
    @pytest.fixture
    def mock_config(self):
        """Mock squadron config."""
        config = Mock(spec=SquadronConfig)
        config.agent_roles = {
            "security-review": Mock(
                name="security-review",
                branch_template="security/issue-{issue_number}"
            )
        }
        return config
    
    def test_security_review_agent_config_has_issue_context(self):
        """Regression test for #58: security-review agent should handle issue assignment."""
        # Read the actual security-review agent configuration
        with open(".squadron/agents/security-review.md", "r") as f:
            agent_config = f.read()
            
        # The config should mention issue analysis, not just PR review
        assert "issue" in agent_config.lower(), "Security-review agent should handle issue assignment"
        
        # The config should have instructions for delegation
        assert "@squadron-dev" in agent_config, "Security-review agent should delegate via @ mentions"
        
        # The config should NOT claim to implement fixes
        problematic_phrases = [
            "this issue has been resolved",
            "the codebase now properly implements", 
            "issue can be marked as resolved",
            "code fixes are already implemented"
        ]
        config_lower = agent_config.lower()
        for phrase in problematic_phrases:
            assert phrase not in config_lower, f"Security-review agent should not claim '{phrase}'"
    
    def test_security_review_agent_config_requires_delegation(self):
        """Regression test for #58: security-review agent must delegate to fix agents."""
        # Read the actual security-review agent configuration
        with open(".squadron/agents/security-review.md", "r") as f:
            agent_config = f.read()
        
        # Should contain delegation instructions
        delegation_indicators = [
            "@squadron-dev bug-fix",
            "@squadron-dev feat-dev", 
            "delegate",
            "mention"
        ]
        
        has_delegation = any(indicator in agent_config for indicator in delegation_indicators)
        assert has_delegation, "Security-review agent config should include delegation instructions"
    
    def test_security_review_agent_config_separates_pr_and_issue_workflows(self):
        """Security-review agent should have different workflows for PRs vs issues."""
        # Read the actual security-review agent configuration  
        with open(".squadron/agents/security-review.md", "r") as f:
            agent_config = f.read()
            
        # Should handle both PR and issue contexts
        assert "{pr_number}" in agent_config or "{issue_number}" in agent_config, (
            "Security-review agent should handle PR or issue context"
        )
        
        # Should have conditional behavior based on context
        context_indicators = [
            "if.*pr",
            "if.*issue", 
            "when.*assigned.*issue",
            "when.*reviewing.*pr",
            "pr.*review",
            "issue.*analysis"
        ]
        
        has_conditional = any(
            indicator.replace(".*", " ") in agent_config.lower() 
            for indicator in context_indicators
        )
        assert has_conditional, "Security-review agent should have conditional PR/issue behavior"

    def test_security_review_agent_config_prevents_fix_claims(self):
        """Security-review agent should never claim to have implemented fixes."""
        # Read the actual security-review agent configuration
        with open(".squadron/agents/security-review.md", "r") as f:
            agent_config = f.read()
        
        # Should explicitly state review-only role
        review_only_indicators = [
            "review only",
            "analysis only", 
            "do not implement",
            "delegate.*fix",
            "cannot.*fix.*code"
        ]
        
        has_review_only = any(
            indicator.replace(".*", " ") in agent_config.lower()
            for indicator in review_only_indicators  
        )
        assert has_review_only, "Security-review agent should explicitly state review-only role"

if __name__ == "__main__":
    pytest.main([__file__])
