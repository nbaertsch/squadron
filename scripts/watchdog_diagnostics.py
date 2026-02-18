#!/usr/bin/env python3
"""
Watchdog Diagnostics Script

This script helps diagnose watchdog system health and investigate timeout issues
like the one reported in issue #70.

Usage:
    python scripts/watchdog_diagnostics.py [--agent-id AGENT_ID] [--check-health]
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from squadron.agent_registry import AgentRegistry
from squadron.config import Config
from squadron.models import AgentStatus


async def check_watchdog_health(registry: AgentRegistry, config: Config):
    """Check the health of the watchdog system."""
    print("🔍 Squadron Watchdog System Health Check")
    print("=" * 50)
    
    # Get all active agents
    active_agents = await registry.get_agents_by_status(AgentStatus.ACTIVE)
    
    print(f"📊 Active Agents: {len(active_agents)}")
    
    current_time = datetime.now(timezone.utc)
    timeout_warnings = []
    
    for agent in active_agents:
        if not agent.active_since:
            continue
            
        # Calculate how long the agent has been active
        active_duration = (current_time - agent.active_since).total_seconds()
        
        # Get timeout limits for this role
        limits = config.circuit_breakers.for_role(agent.role)
        max_duration = limits.max_active_duration
        
        # Calculate percentage of timeout used
        timeout_percentage = (active_duration / max_duration) * 100
        
        status_icon = "✅"
        if timeout_percentage > 90:
            status_icon = "🚨"
            timeout_warnings.append(agent)
        elif timeout_percentage > 80:
            status_icon = "⚠️"
        elif timeout_percentage > 60:
            status_icon = "🟡"
        
        print(f"{status_icon} {agent.agent_id} ({agent.role})")
        print(f"   Active for: {int(active_duration)}s / {max_duration}s ({timeout_percentage:.1f}%)")
        print(f"   Issue: #{agent.issue_number}, Branch: {agent.branch}")
        print()
    
    if timeout_warnings:
        print("🚨 TIMEOUT WARNINGS:")
        for agent in timeout_warnings:
            active_duration = (current_time - agent.active_since).total_seconds()
            limits = config.circuit_breakers.for_role(agent.role)
            overage = active_duration - limits.max_active_duration
            
            if overage > 0:
                print(f"   🚨 {agent.agent_id}: TIMEOUT EXCEEDED by {int(overage)}s!")
                print(f"      This may indicate a watchdog failure like issue #70")
            else:
                time_remaining = limits.max_active_duration - active_duration
                print(f"   ⚠️  {agent.agent_id}: {int(time_remaining)}s until timeout")
    
    return len(timeout_warnings)


async def investigate_agent_timeout(registry: AgentRegistry, config: Config, agent_id: str):
    """Investigate a specific agent's timeout situation."""
    print(f"🕵️ Investigating Agent: {agent_id}")
    print("=" * 50)
    
    agent = await registry.get_agent(agent_id)
    if not agent:
        print(f"❌ Agent {agent_id} not found")
        return
    
    print(f"Agent ID: {agent.agent_id}")
    print(f"Role: {agent.role}")
    print(f"Status: {agent.status}")
    print(f"Issue: #{agent.issue_number}")
    print(f"Branch: {agent.branch}")
    print(f"Active Since: {agent.active_since}")
    print()
    
    if agent.active_since:
        current_time = datetime.now(timezone.utc)
        active_duration = (current_time - agent.active_since).total_seconds()
        
        limits = config.circuit_breakers.for_role(agent.role)
        max_duration = limits.max_active_duration
        
        print(f"Active Duration: {int(active_duration)}s")
        print(f"Max Duration: {max_duration}s")
        
        if active_duration > max_duration:
            overage = active_duration - max_duration
            print(f"🚨 TIMEOUT EXCEEDED by {int(overage)}s")
            print("⚠️  This indicates a potential watchdog failure!")
            print()
            
            print("🔧 Recommended Investigation Steps:")
            print("1. Check agent logs for blocking operations")
            print("2. Verify watchdog task creation in agent manager")
            print("3. Check system resource usage (CPU/memory)")
            print("4. Review any recent configuration changes")
            print("5. Check reconciliation loop logs for timeout detection")
            
        else:
            time_remaining = max_duration - active_duration
            timeout_percentage = (active_duration / max_duration) * 100
            print(f"Time Remaining: {int(time_remaining)}s ({timeout_percentage:.1f}% used)")
            
            if timeout_percentage > 90:
                print("⚠️  Agent is very close to timeout!")
            elif timeout_percentage > 80:
                print("🟡 Agent is approaching timeout threshold")
            else:
                print("✅ Agent is within normal timeout limits")


async def main():
    parser = argparse.ArgumentParser(description="Squadron Watchdog Diagnostics")
    parser.add_argument("--agent-id", help="Investigate specific agent timeout")
    parser.add_argument("--check-health", action="store_true", help="Check overall watchdog system health")
    parser.add_argument("--config", default=".squadron/config.yaml", help="Path to configuration file")
    
    args = parser.parse_args()
    
    # Load configuration
    try:
        config = Config.from_file(args.config)
    except Exception as e:
        print(f"❌ Error loading configuration: {e}")
        return 1
    
    # Initialize registry (this would need actual database connection in real usage)
    # For now, we'll use a mock registry
    from unittest.mock import AsyncMock
    registry = AsyncMock()
    
    if args.agent_id:
        await investigate_agent_timeout(registry, config, args.agent_id)
    elif args.check_health:
        warning_count = await check_watchdog_health(registry, config)
        return 1 if warning_count > 0 else 0
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
