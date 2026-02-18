"""Simplified regression test for issue #58 - Security-review agent role bug."""

def test_security_review_agent_config_prevents_fix_claims():
    """Regression test for #58: security-review agent should not claim to implement fixes."""
    # Read the actual security-review agent configuration
    with open(".squadron/agents/security-review.md", "r") as f:
        agent_config = f.read()
    
    print("Testing security-review agent configuration...")
    
    # Test 1: Should mention issue analysis, not just PR review
    has_issue_context = "issue" in agent_config.lower()
    print(f"✓ Has issue context: {has_issue_context}")
    
    # Test 2: Should have instructions for delegation
    has_delegation = "@squadron-dev" in agent_config
    print(f"✓ Has delegation instructions: {has_delegation}")
    
    # Test 3: Should NOT claim to implement fixes (excluding negative examples)
    # Remove sections that show what NOT to say (❌ examples)
    lines = agent_config.split('\n')
    filtered_lines = []
    in_negative_example = False
    
    for line in lines:
        if '❌' in line:
            in_negative_example = True
        elif '✅' in line:
            in_negative_example = False
        elif not in_negative_example:
            filtered_lines.append(line)
    
    filtered_config = '\n'.join(filtered_lines).lower()
    
    problematic_phrases = [
        "this issue has been resolved",
        "the codebase now properly implements", 
        "issue can be marked as resolved",
        "code fixes are already implemented"
    ]
    
    fix_claims = []
    for phrase in problematic_phrases:
        if phrase in filtered_config:
            fix_claims.append(phrase)
    
    print(f"✓ No fix claims in instructions: {len(fix_claims) == 0}")
    if fix_claims:
        print(f"  Found problematic phrases: {fix_claims}")
    
    # Test 4: Should explicitly state review-only role
    review_only_indicators = [
        "review and analysis only",
        "analyze only",
        "do not implement", 
        "analysis agent only",
        "you cannot"
    ]
    
    has_review_only = any(
        indicator in agent_config.lower()
        for indicator in review_only_indicators  
    )
    print(f"✓ Has review-only instructions: {has_review_only}")
    
    # Test 5: Should handle both PR and issue contexts  
    has_pr_context = "{pr_number}" in agent_config or "pr review" in agent_config.lower()
    has_issue_context_var = "{issue_number}" in agent_config
    handles_both = has_pr_context and has_issue_context_var
    print(f"✓ Handles both PR and issue contexts: {handles_both}")
    
    # Test 6: Should have explicit delegation requirements
    has_delegation_req = "delegate" in agent_config.lower() and "must delegate" in agent_config.lower()
    print(f"✓ Has explicit delegation requirements: {has_delegation_req}")
    
    print("\n" + "="*50)
    print("CURRENT STATE (after fix):")
    print(f"- Issue context: {'PASS' if has_issue_context else 'FAIL'}")
    print(f"- Delegation instructions: {'PASS' if has_delegation else 'FAIL'}")
    print(f"- No fix claims: {'PASS' if len(fix_claims) == 0 else 'FAIL'}")
    print(f"- Review-only role: {'PASS' if has_review_only else 'FAIL'}")
    print(f"- Both contexts: {'PASS' if handles_both else 'FAIL'}")
    print(f"- Delegation required: {'PASS' if has_delegation_req else 'FAIL'}")
    
    all_pass = (has_issue_context and has_delegation and len(fix_claims) == 0 
                and has_review_only and handles_both and has_delegation_req)
    
    print(f"\nOVERALL: {'PASS' if all_pass else 'FAIL'}")
    
    return all_pass

if __name__ == "__main__":
    success = test_security_review_agent_config_prevents_fix_claims()
    exit(0 if success else 1)
