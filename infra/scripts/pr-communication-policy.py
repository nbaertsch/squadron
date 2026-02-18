#!/usr/bin/env python3
"""
PR Communication Policy Enforcement Script

This script validates and enforces that:
1. Agents use comment_on_pr for PR-related communication
2. The correct agent responds to PR review feedback
3. Cleanup actions are properly triggered

Usage:
    python3 infra/scripts/pr-communication-policy.py --validate
    python3 infra/scripts/pr-communication-policy.py --enforce
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Any
import re


class PRCommunicationPolicy:
    """Enforces PR-based communication policies for Squadron agents."""
    
    def __init__(self, config_path: str = ".squadron/config.yaml"):
        self.config_path = Path(config_path)
        self.violations = []
        
    def validate_agent_tools(self) -> bool:
        """Validate that agent tools are correctly configured for PR communication."""
        valid = True
        
        # Check agent definitions for proper tool usage
        agents_dir = self.config_path.parent / "agents"
        if not agents_dir.exists():
            self.violations.append("Agents directory not found")
            return False
            
        for agent_file in agents_dir.glob("*.md"):
            agent_content = agent_file.read_text()
            
            # Check for comment_on_issue usage in PR-related contexts
            if self._check_pr_context_violations(agent_file.name, agent_content):
                valid = False
                
        return valid
    
    def _check_pr_context_violations(self, agent_name: str, content: str) -> bool:
        """Check if agent improperly uses comment_on_issue for PR contexts."""
        violations_found = False
        
        # Look for patterns that suggest PR communication should use comment_on_pr
        pr_keywords = [
            r'review.*comment',
            r'PR.*feedback', 
            r'pull.*request.*comment',
            r'changes.*requested',
            r'review.*response'
        ]
        
        # Check if agent mentions comment_on_issue in PR contexts
        lines = content.split('\n')
        for i, line in enumerate(lines, 1):
            if 'comment_on_issue' in line:
                # Check surrounding context for PR-related terms
                context = '\n'.join(lines[max(0, i-3):i+3])
                
                for pattern in pr_keywords:
                    if re.search(pattern, context, re.IGNORECASE):
                        self.violations.append(
                            f"{agent_name}: Line {i} uses comment_on_issue in PR context. "
                            f"Should use comment_on_pr for PR-related communication."
                        )
                        violations_found = True
                        
        return violations_found
    
    def validate_workflows(self) -> bool:
        """Validate that GitHub workflows support PR-based communication."""
        workflows_dir = Path(".github/workflows")
        if not workflows_dir.exists():
            self.violations.append("GitHub workflows directory not found")
            return False
            
        required_workflows = [
            "pr-cleanup.yml",
            "pr-review-flow.yml"
        ]
        
        valid = True
        for workflow in required_workflows:
            workflow_path = workflows_dir / workflow
            if not workflow_path.exists():
                self.violations.append(f"Required workflow missing: {workflow}")
                valid = False
            else:
                # Validate workflow content
                if not self._validate_workflow_content(workflow_path):
                    valid = False
                    
        return valid
    
    def _validate_workflow_content(self, workflow_path: Path) -> bool:
        """Validate specific workflow has required features."""
        content = workflow_path.read_text()
        
        if workflow_path.name == "pr-cleanup.yml":
            required_features = [
                "pull_request:",
                "types: [closed]",
                "delete merged branch", 
                "update linked issue",
                "close linked issue"
            ]
        elif workflow_path.name == "pr-review-flow.yml":
            required_features = [
                "pull_request_review:",
                "issue_comment:",
                "redirect issue comment to pr",
                "tag responsible agent"
            ]
        else:
            return True  # Unknown workflow, skip validation
            
        for feature in required_features:
            if feature.lower() not in content.lower():
                self.violations.append(
                    f"{workflow_path.name}: Missing required feature '{feature}'"
                )
                return False
                
        return True
    
    def validate_squadron_config(self) -> bool:
        """Validate Squadron configuration supports PR-based flow."""
        if not self.config_path.exists():
            self.violations.append("Squadron config file not found")
            return False
            
        # For now, just check file exists and is readable
        # In a real implementation, would parse YAML and validate agent triggers
        try:
            content = self.config_path.read_text()
            
            # Check for proper PR event handling
            required_events = [
                "pull_request.opened",
                "pull_request.closed", 
                "pull_request_review.submitted"
            ]
            
            for event in required_events:
                if event not in content:
                    self.violations.append(
                        f"Squadron config missing proper handling for {event}"
                    )
                    
        except Exception as e:
            self.violations.append(f"Error reading Squadron config: {e}")
            return False
            
        return len([v for v in self.violations if "Squadron config" in v]) == 0
    
    def validate_all(self) -> bool:
        """Run all validations."""
        results = [
            self.validate_agent_tools(),
            self.validate_workflows(), 
            self.validate_squadron_config()
        ]
        
        return all(results)
    
    def enforce_policies(self) -> bool:
        """Enforce PR communication policies."""
        if not self.validate_all():
            print("❌ Policy violations found. Cannot enforce until resolved.")
            return False
            
        print("✅ All PR communication policies validated successfully!")
        
        # In a real implementation, this would:
        # 1. Update agent configurations to use comment_on_pr 
        # 2. Enable policy enforcement in runtime
        # 3. Configure webhooks/triggers appropriately
        
        return True
    
    def print_violations(self):
        """Print all policy violations found."""
        if not self.violations:
            print("✅ No policy violations found!")
            return
            
        print("❌ Policy violations found:")
        print()
        for violation in self.violations:
            print(f"  • {violation}")
        print()


def main():
    parser = argparse.ArgumentParser(description="Validate and enforce PR communication policies")
    parser.add_argument("--validate", action="store_true", help="Validate current configuration")
    parser.add_argument("--enforce", action="store_true", help="Enforce policies")
    parser.add_argument("--config", default=".squadron/config.yaml", help="Path to Squadron config")
    
    args = parser.parse_args()
    
    if not args.validate and not args.enforce:
        parser.print_help()
        return 1
        
    policy = PRCommunicationPolicy(args.config)
    
    if args.validate:
        print("🔍 Validating PR communication policies...")
        valid = policy.validate_all()
        policy.print_violations()
        return 0 if valid else 1
        
    if args.enforce:
        print("🔧 Enforcing PR communication policies...")
        success = policy.enforce_policies()
        return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
