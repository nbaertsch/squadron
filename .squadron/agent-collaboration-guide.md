# Agent Collaboration Guide

## Available Agents & When to Mention Them

The Squadron system includes specialized agents that you can collaborate with via @ mentions. Use this system when you need expertise outside your domain or want to delegate specific tasks.

### Agent Roster

- **@squadron-dev pm** - Project Manager
  - **When to use**: Issue triage, cross-agent coordination, creating blocking issues
  - **Example**: `@squadron-dev pm This requires creating a security audit issue for the auth changes`

- **@squadron-dev feat-dev** - Feature Developer  
  - **When to use**: Need feature implementation, code architecture advice for new functionality
  - **Example**: `@squadron-dev feat-dev Please implement the user authentication middleware described in the requirements`

- **@squadron-dev bug-fix** - Bug Fix Specialist
  - **When to use**: Bug reproduction, regression test creation, debugging complex issues
  - **Example**: `@squadron-dev bug-fix Can you investigate the memory leak reported in the logs?`

- **@squadron-dev security-review** - Security Reviewer
  - **When to use**: Security analysis, vulnerability assessment, cryptography review
  - **Example**: `@squadron-dev security-review Please assess the security implications of this API endpoint`

- **@squadron-dev pr-review** - Pull Request Reviewer
  - **When to use**: Code quality review, best practices enforcement, general code review
  - **Example**: `@squadron-dev pr-review Ready for review - please check the error handling patterns`

- **@squadron-dev docs-dev** - Documentation Developer
  - **When to use**: Documentation updates, API docs, user guides
  - **Example**: `@squadron-dev docs-dev Please update the API documentation for these new endpoints`

- **@squadron-dev infra-dev** - Infrastructure Developer  
  - **When to use**: CI/CD issues, deployment configs, Dockerfiles, IaC templates
  - **Example**: `@squadron-dev infra-dev We need a new deployment pipeline for the staging environment`

- **@squadron-dev test-coverage** - Test Coverage Reviewer
  - **When to use**: Test adequacy review, coverage analysis, test strategy
  - **Example**: `@squadron-dev test-coverage Please review test coverage for the new authentication module`

## Mention Format & Best Practices

### Proper Mention Format
Always use the full mention format: `@squadron-dev {agent-role}`

**✅ Correct:**
- `@squadron-dev feat-dev`
- `@squadron-dev security-review`
- `@squadron-dev pm`

**❌ Incorrect:**
- `@feat-dev` (missing squadron-dev prefix)
- `@squadron feat-dev` (missing hyphen)
- `@squadron-dev feature` (wrong role name)

### Effective Collaboration Patterns

1. **Provide Context**: Always include relevant context when mentioning an agent
   ```
   @squadron-dev security-review The new OAuth integration in PR #45 introduces 
   external token storage. Please assess potential security risks.
   ```

2. **Be Specific**: Clearly state what you need from the other agent
   ```
   @squadron-dev feat-dev Please implement the rate limiting middleware 
   described in issue requirements, targeting the /api/v1/* endpoints.
   ```

3. **Reference Related Work**: Link to relevant issues, PRs, or documentation
   ```
   @squadron-dev docs-dev The API changes in PR #67 need documentation. 
   See the schema changes in src/api/models.py for details.
   ```

4. **Cross-Domain Collaboration**: When your work affects multiple areas
   ```
   @squadron-dev infra-dev @squadron-dev security-review 
   The new deployment needs both infrastructure setup and security hardening.
   Infra: Please set up the staging environment.
   Security: Please review the container security configuration.
   ```

## When NOT to Mention Other Agents

- **Don't mention agents for their current assignment**: If an agent is already working on an issue, they'll see comments automatically
- **Don't mention for status updates**: Use issue comments for general updates
- **Don't mention PM for routine questions**: PM is for coordination and escalation, not technical questions
- **Don't spam multiple agents**: Be selective about who needs to be involved

## Collaboration Workflow

1. **Initial mention**: Use @ mention to request specific help
2. **Wait for response**: Give the mentioned agent time to respond (agents may be working on other tasks)
3. **Provide clarifications**: Answer any questions the mentioned agent has
4. **Coordinate handoffs**: Clearly define responsibilities and dependencies
5. **Follow up appropriately**: Check progress without being disruptive

## Troubleshooting Mentions

If an agent doesn't respond to your mention:
1. **Check the format**: Ensure you used the correct `@squadron-dev {role}` format
2. **Wait appropriately**: Agents may be busy with other tasks
3. **Provide more context**: The agent may need more information to help
4. **Escalate if needed**: Mention `@squadron-dev pm` if there's a persistent issue
