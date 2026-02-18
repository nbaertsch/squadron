"""
Enhanced Watchdog Monitoring for Squadron Agents

This module provides improved timeout handling and monitoring for agent lifecycle management.
Addresses issue #53: pr-review agents timing out without primary watchdog detection.
"""

import asyncio
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

# Role-specific cleanup timeouts to handle different agent complexity
CLEANUP_TIMEOUTS = {
    "pr-review": 60,       # Git operations and PR comments can be slow
    "infra-dev": 90,       # Complex infrastructure operations  
    "security-review": 60, # Security analysis and scanning
    "feat-dev": 45,        # Feature development with multiple files
    "docs-dev": 30,        # Documentation is typically lighter
    "bug-fix": 45,         # Bug fixes may involve complex debugging
    "test-coverage": 60,   # Test analysis and generation  
    "pm": 20,              # Project management is typically quick
    "default": 30          # Conservative fallback
}

class WatchdogMonitor:
    """Enhanced monitoring for agent watchdog health and failures."""
    
    def __init__(self, activity_logger=None):
        self.activity_logger = activity_logger
        self._watchdog_failures = {}  # Track failures per role
        
    def get_cleanup_timeout(self, role: str) -> int:
        """Get role-specific cleanup timeout duration."""
        return CLEANUP_TIMEOUTS.get(role, CLEANUP_TIMEOUTS["default"])
    
    async def record_watchdog_firing(self, agent_id: str, role: str, max_seconds: int) -> None:
        """Record when primary watchdog fires (layer 1 enforcement)."""
        try:
            logger.info(
                "WATCHDOG MONITORING: Primary watchdog fired for %s (%s) after %ds",
                agent_id, role, max_seconds
            )
            
            if self.activity_logger:
                await self.activity_logger.log_activity_event(
                    agent_id=agent_id,
                    event_type="watchdog_fired", 
                    content=f"Primary watchdog fired for {role} agent after {max_seconds}s",
                    metadata={
                        "role": role,
                        "timeout_duration": max_seconds,
                        "enforcement_layer": "watchdog"
                    }
                )
        except Exception:
            logger.exception("Failed to record watchdog firing for %s", agent_id)
    
    async def record_cleanup_timeout(self, agent_id: str, role: str, cleanup_timeout: int) -> None:
        """Record when agent cleanup exceeds timeout (potential stuck operation)."""
        try:
            # Track failures per role for pattern detection
            if role not in self._watchdog_failures:
                self._watchdog_failures[role] = 0
            self._watchdog_failures[role] += 1
            
            logger.warning(
                "WATCHDOG MONITORING: Agent %s (%s) cleanup exceeded %ds timeout (failure #%d for role)",
                agent_id, role, cleanup_timeout, self._watchdog_failures[role]
            )
            
            if self.activity_logger:
                await self.activity_logger.log_activity_event(
                    agent_id=agent_id,
                    event_type="cleanup_timeout",
                    content=f"Agent cleanup exceeded {cleanup_timeout}s timeout", 
                    metadata={
                        "role": role,
                        "cleanup_timeout": cleanup_timeout,
                        "failure_count": self._watchdog_failures[role],
                        "failure_type": "cleanup_timeout"
                    }
                )
        except Exception:
            logger.exception("Failed to record cleanup timeout for %s", agent_id)
    
    async def record_reconciliation_timeout(self, agent_id: str, role: str, overage: int) -> None:
        """Record when reconciliation loop catches timeout (layer 3 - watchdog failure)."""
        try:
            failure_severity = "critical" if overage > 300 else "warning"
            
            logger.error(
                "WATCHDOG MONITORING: Reconciliation caught timeout for %s (%s) - overage %ds (%s)",
                agent_id, role, overage, failure_severity
            )
            
            if self.activity_logger:
                await self.activity_logger.log_activity_event(
                    agent_id=agent_id,
                    event_type="reconciliation_timeout",
                    content=f"Reconciliation loop caught timeout - {overage}s overage indicates watchdog failure",
                    metadata={
                        "role": role,
                        "overage_seconds": overage,
                        "enforcement_layer": "reconciliation", 
                        "severity": failure_severity,
                        "indicates_watchdog_failure": overage > 60
                    }
                )
        except Exception:
            logger.exception("Failed to record reconciliation timeout for %s", agent_id)
    
    def get_failure_stats(self) -> Dict[str, Any]:
        """Get watchdog failure statistics by role."""
        return {
            "cleanup_failures_by_role": dict(self._watchdog_failures),
            "total_cleanup_failures": sum(self._watchdog_failures.values()),
            "roles_with_failures": list(self._watchdog_failures.keys())
        }

# Enhanced timeout classification for reconciliation loop
def classify_timeout_failure(agent_role: str, overage: int) -> Dict[str, Any]:
    """Classify timeout failure for better monitoring and response."""
    classification = {
        "failure_indicators": [],
        "severity": "normal",
        "likely_cause": "normal_timeout",
        "recommended_action": "standard_escalation"
    }
    
    # General overage analysis
    if overage > 60:
        classification["failure_indicators"].append("high-overage")
        classification["likely_cause"] = "watchdog_failure"
        
    if overage > 300:
        classification["failure_indicators"].append("critical-overage") 
        classification["severity"] = "critical"
        classification["likely_cause"] = "watchdog_completely_failed"
        classification["recommended_action"] = "immediate_investigation"
    
    # Role-specific analysis
    if agent_role == "pr-review":
        if overage > 180:  # 3 minutes overage for 30-minute timeout
            classification["failure_indicators"].append("pr-review-stuck")
            classification["likely_cause"] = "git_operation_hang"
            
    elif agent_role == "infra-dev":
        if overage > 600:  # 10 minutes overage for 3-hour timeout  
            classification["failure_indicators"].append("infra-dev-stuck")
            classification["likely_cause"] = "complex_deployment_hang"
            
    return classification

