"""
Regression test for issue #32: Agents failing to collaborate via @ mention system.

This tests the specific fix for agents inappropriately blocking themselves on
the same issue they're assigned to work on.
"""
import asyncio
import tempfile
import os
import sys

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from squadron.registry import AgentRegistry
from squadron.models import AgentRecord, AgentStatus


class TestSelfBlockingPrevention:
    """Test that agents cannot block on their own issue (issue #32 fix)."""
    
    async def test_prevents_self_blocking(self):
        """Agent should not be able to block on the same issue it's working on."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        try:
            registry = AgentRegistry(db_path)
            await registry.initialize()
            
            # Create an agent working on issue #26
            agent = AgentRecord(
                agent_id="test-agent-issue-26",
                role="feat-dev",
                issue_number=26,  # Agent is working on issue #26
                status=AgentStatus.ACTIVE,
            )
            await registry.create_agent(agent)
            
            # Attempt to block the agent on the same issue it's working on
            # This should be prevented (return False)
            result = await registry.add_blocker("test-agent-issue-26", 26)
            
            # Should return False because it's a self-blocking attempt
            assert result is False, "Agent should not be allowed to block on its own issue"
            
            # Agent should not have the blocker added
            updated_agent = await registry.get_agent("test-agent-issue-26")
            assert 26 not in updated_agent.blocked_by, "Self-blocker should not be added"
        
        finally:
            await registry.close()
            os.unlink(db_path)

    async def test_normal_blocking_still_works(self):
        """Normal blocking (agent on issue A blocking on issue B) should still work."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        try:
            registry = AgentRegistry(db_path)
            await registry.initialize()
            
            # Create an agent working on issue #26
            agent = AgentRecord(
                agent_id="test-agent-issue-26", 
                role="feat-dev",
                issue_number=26,
                status=AgentStatus.ACTIVE,
            )
            await registry.create_agent(agent)
            
            # Block on a different issue - this should work
            result = await registry.add_blocker("test-agent-issue-26", 42)
            
            # Should return True because it's valid blocking
            assert result is True, "Normal blocking should work correctly"
            
            # Agent should have the blocker added
            updated_agent = await registry.get_agent("test-agent-issue-26")
            assert 42 in updated_agent.blocked_by, "Normal blocker should be added"
        
        finally:
            await registry.close()
            os.unlink(db_path)

    async def test_complex_cycle_detection_still_works(self):
        """Complex cycle detection should still prevent real circular dependencies."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        try:
            registry = AgentRegistry(db_path)
            await registry.initialize()
            
            # Create agents: A works on #10, B works on #20
            agent_a = AgentRecord(
                agent_id="agent-a-issue-10",
                role="feat-dev", 
                issue_number=10,
                status=AgentStatus.ACTIVE,
            )
            agent_b = AgentRecord(
                agent_id="agent-b-issue-20",
                role="bug-fix",
                issue_number=20, 
                status=AgentStatus.ACTIVE,
            )
            await registry.create_agent(agent_a)
            await registry.create_agent(agent_b)
            
            # Agent A blocks on issue #20 (B's issue)
            result1 = await registry.add_blocker("agent-a-issue-10", 20)
            assert result1 is True, "Initial blocking should work"
            
            # Agent B tries to block on issue #10 (A's issue) - this should create a cycle
            result2 = await registry.add_blocker("agent-b-issue-20", 10)
            assert result2 is False, "Circular dependency should be prevented"
            
        finally:
            await registry.close() 
            os.unlink(db_path)


async def run_all_tests():
    """Run all regression tests."""
    print("Running regression tests for issue #32: Agent self-blocking fix\n")
    
    test_class = TestSelfBlockingPrevention()
    tests_passed = 0
    total_tests = 3
    
    # Test 1: Self-blocking prevention
    print("1. Testing self-blocking prevention...")
    try:
        await test_class.test_prevents_self_blocking()
        print("   ✅ Self-blocking correctly prevented")
        tests_passed += 1
    except Exception as e:
        print(f"   ❌ Test failed: {e}")
    
    # Test 2: Normal blocking still works
    print("2. Testing normal blocking still works...")
    try:
        await test_class.test_normal_blocking_still_works()
        print("   ✅ Normal blocking works correctly")
        tests_passed += 1
    except Exception as e:
        print(f"   ❌ Test failed: {e}")
    
    # Test 3: Complex cycle detection
    print("3. Testing complex cycle detection...")
    try:
        await test_class.test_complex_cycle_detection_still_works()
        print("   ✅ Complex cycle detection works correctly")
        tests_passed += 1
    except Exception as e:
        print(f"   ❌ Test failed: {e}")
        
    print(f"\n📊 Results: {tests_passed}/{total_tests} tests passed")
    
    if tests_passed == total_tests:
        print("✅ All regression tests passed! The fix is working correctly.")
        return True
    else:
        print("❌ Some tests failed! The fix needs more work.")
        return False


if __name__ == "__main__":
    result = asyncio.run(run_all_tests())
    exit(0 if result else 1)
