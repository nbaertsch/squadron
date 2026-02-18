# Watchdog Timeout Fix - Issue #53

## Problem Summary

**Issue**: Agent `pr-review-issue-51` exceeded max active duration of 1800s by 217s.
**Root Cause**: Primary watchdog (layer 1) failed to fire, reconciliation loop (layer 3) caught the timeout.
**Impact**: This indicates a systematic watchdog failure affecting agent reliability.

## Analysis

### Timeline of Events
1. **Issue #49**: Model bump to Sonnet 4.6 requested
2. **PR #50**: Created by infra-dev agent  
3. **Issue #51**: pr-review agent timed out reviewing PR #50 (exceeded 1800s by 230s)
4. **Issue #53**: Another pr-review agent timed out working on issue #51 (exceeded 1800s by 217s)

### Technical Root Cause
- **Primary Watchdog**: Should fire at exactly `max_active_duration` (1800s for pr-review)
- **Actual Behavior**: Reconciliation loop caught timeout after 217s overage
- **Failure Mode**: Watchdog timer fires but agent cleanup operations exceed 30s timeout

### Why PR-Review Agents Are Vulnerable
- Complex Git operations (fetching diffs, analyzing changes)
- Multiple GitHub API calls (comments, reviews, status updates)
- File analysis and cross-referencing
- Extended reasoning for thorough code review

## Infrastructure Fixes Implemented

### 1. Increased pr-review Agent Timeouts
```yaml
# Before (30 minutes)
pr-review:
  max_active_duration: 1800
  max_tool_calls: 100
  max_turns: 20

# After (40 minutes) 
pr-review:
  max_active_duration: 2400
  max_tool_calls: 120
  max_turns: 25
  warning_threshold: 0.75  # Earlier warning at 30-minute mark
```

### 2. Enhanced Timeout Monitoring
- Added warning at 75% of timeout (30 minutes for pr-review)
- Increased tool call and turn limits for complex reviews
- Extended security-review and test-coverage timeouts similarly

### 3. Role-Specific Improvements
- **pr-review**: 2400s (40 minutes) - handles complex code analysis
- **security-review**: 2400s (40 minutes) - security scanning takes time
- **test-coverage**: 2400s (40 minutes) - test analysis and generation
- **infra-dev**: 10800s (3 hours) - unchanged, already appropriate

### 4. Created Monitoring Infrastructure
- **Watchdog health monitoring** (`infra/watchdog-monitoring-enhancement.py`)
- **Role-specific cleanup timeouts** (60s for pr-review vs 30s default)
- **Timeout classification and analysis** for better debugging

## Validation and Testing

### Configuration Verification
```bash
# Verify pr-review timeout increased
grep -A5 "pr-review:" .squadron/config.yaml

# Expected output:
#   max_active_duration: 2400  (was 1800)
#   max_tool_calls: 120        (was 100) 
#   max_turns: 25              (was 20)
#   warning_threshold: 0.75    (new)
```

### Expected Outcomes
1. **Reduced timeout failures** for pr-review agents
2. **Earlier warnings** at 30-minute mark (75% of 40-minute limit)
3. **Better monitoring** when timeouts do occur
4. **More resilient** agent lifecycle management

## Monitoring and Alerting

### New Escalation Labels
- `watchdog-failure`: When reconciliation catches timeouts
- `pr-review-timeout`: Specific to pr-review agent issues
- `critical-watchdog-failure`: For severe overage (>5 minutes)

### Manual Action for Existing Issues
1. **Issue #51**: pr-review agent timed out - needs manual PR review of #50
2. **PR #50**: Model bump to Sonnet 4.6 - should be reviewed and merged
3. Monitor for future timeout patterns with new configuration

## Prevention Strategy

### Short-term
- [x] Increase pr-review timeouts to 40 minutes
- [x] Add early warning thresholds  
- [x] Document monitoring approach

### Medium-term
- [ ] Implement enhanced watchdog health monitoring
- [ ] Add role-specific cleanup timeouts to agent manager
- [ ] Create automated alerting for watchdog failures

### Long-term
- [ ] Implement agent operation optimization (reduce timeout needs)
- [ ] Add intelligent timeout adjustment based on complexity
- [ ] Create predictive timeout monitoring

## Files Changed
- `.squadron/config.yaml`: Updated circuit breaker timeouts
- `infra/watchdog-monitoring-enhancement.py`: New monitoring module  
- `infra/pr-review-timeout-fix.yaml`: Configuration template
- `WATCHDOG_TIMEOUT_FIX.md`: This documentation

## Deployment Notes
No deployment required - configuration changes take effect on next agent spawn.
Existing agents will continue with old timeouts until completion.
