# Squadron Architecture Deep Dive

This document provides a comprehensive technical overview of Squadron's architecture, design decisions, and implementation details.

## System Overview

Squadron is a **GitHub-native multi-agent autonomous development framework** built on the GitHub Copilot SDK. It transforms GitHub repositories into self-organizing development teams using AI agents.

### Core Principles

1. **GitHub as State Machine**: Issues, PRs, and webhooks drive all agent behavior
2. **Tool-Based Architecture**: Agents select from specialized tools rather than having hardcoded capabilities
3. **Introspection Over Injection**: Agents use tools to understand state rather than receiving injected context
4. **Human-Compatible Interface**: All actions use GitHub's native UI and audit trails

## Component Architecture

```mermaid
graph TB
    subgraph "GitHub"
        GH_ISSUES[Issues]
        GH_PRS[Pull Requests]  
        GH_WEBHOOKS[Webhooks]
        GH_API[GitHub API]
    end

    subgraph "Squadron Service"
        WEBHOOK_SERVER[Webhook Server<br/>FastAPI]
        EVENT_ROUTER[Event Router]
        AGENT_MANAGER[Agent Manager]
        PIPELINE_ENGINE[Pipeline Engine]
        PIPELINE_REGISTRY[Pipeline Registry<br/>SQLite]
        RECOVERY[Recovery System]
        REGISTRY[Agent Registry<br/>SQLite]
    end

    subgraph "Agent Runtime"
        PM_AGENT[PM Agent<br/>Ephemeral]
        DEV_AGENTS[Dev Agents<br/>Persistent] 
        REVIEW_AGENTS[Review Agents<br/>Persistent]
        COPILOT_SDK[Copilot SDK<br/>Sessions]
    end

    subgraph "Tools & Context"
        SQUADRON_TOOLS[Squadron Tools<br/>20+ specialized tools]
        GIT_WORKTREES[Git Worktrees<br/>Per-agent isolation]
        GITHUB_CLIENT[GitHub Client]
    end

    GH_WEBHOOKS --> WEBHOOK_SERVER
    WEBHOOK_SERVER --> EVENT_ROUTER
    EVENT_ROUTER --> AGENT_MANAGER
    AGENT_MANAGER --> PM_AGENT
    AGENT_MANAGER --> DEV_AGENTS
    AGENT_MANAGER --> REVIEW_AGENTS
    
    PM_AGENT --> SQUADRON_TOOLS
    DEV_AGENTS --> SQUADRON_TOOLS
    REVIEW_AGENTS --> SQUADRON_TOOLS
    
    SQUADRON_TOOLS --> GITHUB_CLIENT
    SQUADRON_TOOLS --> GIT_WORKTREES
    GITHUB_CLIENT --> GH_API
    
    AGENT_MANAGER --> REGISTRY
    PIPELINE_ENGINE --> PIPELINE_REGISTRY
    RECOVERY --> AGENT_MANAGER
```

## Core Components

### 1. Webhook Server (`src/squadron/server.py`)

FastAPI-based HTTP server that receives GitHub webhooks and provides API endpoints.

**Responsibilities:**
- Receive and validate GitHub webhooks
- Authentication and signature verification  
- Route events to the event processing system
- Provide health and status endpoints
- Serve metrics and monitoring data

**Key Endpoints:**
```python
POST /webhook          # GitHub webhook receiver
GET  /health           # Health check
GET  /api/agents       # Agent status API
GET  /api/metrics      # Monitoring metrics
```

### 2. Event Router (`src/squadron/event_router.py`)

Processes GitHub webhooks and routes events to the pipeline engine and framework-level handlers.

**Event Flow:**
1. **Webhook Reception**: Receive and parse GitHub event
2. **Event Classification**: Determine event type (issue_opened, pr_created, etc.)
3. **Pipeline Evaluation**: Route to Pipeline Engine for trigger matching
4. **Framework Handlers**: Handle cross-cutting concerns (command routing, issue lifecycle, inbox enrichment)
5. **Reactive Events**: Notify running pipelines of relevant events

**Supported Events:**
- `issues.opened`, `issues.edited`, `issues.labeled`
- `pull_request.opened`, `pull_request.synchronize`  
- `pull_request_review.submitted`
- `issue_comment.created` (for @squadron-dev mentions)

### 3. Agent Manager (`src/squadron/agent_manager.py`)

Central orchestrator for agent lifecycle management.

**Key Functions:**
- **Session Management**: Create, resume, and destroy Copilot SDK sessions
- **Worktree Management**: Create isolated git worktrees per agent
- **Lifecycle Control**: Handle ephemeral vs. persistent agent patterns
- **Tool Provisioning**: Provide agents with configured tools
- **Circuit Breaker Enforcement**: Apply resource limits and timeouts

**Agent Types:**
```python
class AgentLifecycle(Enum):
    EPHEMERAL = "ephemeral"    # Run once and terminate
    PERSISTENT = "persistent"   # Can sleep/wake across sessions
```

### 4. Pipeline Engine (`src/squadron/pipeline/engine.py`)

Unified orchestration engine that replaces triggers, review policy, and the legacy workflow engine. All agent spawning, review orchestration, approval tracking, and merge automation are expressed as YAML pipeline definitions.

**Features:**
- **Event-Driven Activation**: Pipelines trigger on GitHub webhook events
- **Stage Execution**: Agent, gate, human, parallel, delay, action, webhook, and sub-pipeline stages
- **Reactive Events**: Running pipelines respond to GitHub events (re-evaluate gates, invalidate/restart, cancel)
- **Gate System**: Pluggable gate checks (CI status, PR approvals, commands, labels, custom)
- **Human-in-the-Loop**: Human stages with approval tracking, reminders, and timeout
- **Sub-Pipelines**: Compose pipelines from reusable sub-pipeline definitions
- **Template Expressions**: `{{ }}` expressions resolved against pipeline context
- **Recovery**: Active pipelines resume after server restart

See [Pipeline Configuration Reference](reference/pipeline-configuration.md) and [Pipeline Monitoring](reference/pipeline-monitoring.md) for details.

### 5. Agent Registry (`src/squadron/registry.py`)

SQLite-based persistent storage for agent state and metadata.

**Schema:**
```sql
CREATE TABLE agents (
    id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    issue_number INTEGER,
    pr_number INTEGER,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    metadata JSON
);
```

**Agent Status States:**
- `ACTIVE`: Currently running
- `SLEEPING`: Blocked and waiting
- `COMPLETED`: Successfully finished
- `FAILED`: Encountered unrecoverable error
- `ESCALATED`: Escalated to humans

### 6. Recovery System (`src/squadron/recovery.py`)

Monitors agent health and handles failures.

**Recovery Strategies:**
- **Timeout Detection**: Identify agents that exceed time limits
- **Circuit Breaker Triggers**: Handle resource exhaustion
- **Graceful Degradation**: Escalate rather than crash
- **State Cleanup**: Clean up orphaned resources

## Agent Architecture

### Agent Definition Format

Agents are defined using Markdown files with YAML frontmatter:

```yaml
---
name: feat-dev
description: Feature development agent
tools:
  - read_issue
  - open_pr  
  - git_push
circuit_breaker:
  max_turns: 50
  max_tool_calls: 100
lifecycle: persistent
---

# Agent System Prompt
Markdown content becomes the agent's system prompt...
```

### Tool System

Squadron provides 20+ specialized tools organized into categories:

**Framework Tools** (Agent Lifecycle):
- `check_for_events`: Check inbox for pending events
- `report_blocked`: Declare blocking on another issue  
- `report_complete`: Mark work as completed
- `escalate_to_human`: Request human intervention

**Issue Management**:
- `create_issue`, `read_issue`, `label_issue`, `assign_issue`
- `close_issue`, `update_issue`

**Pull Request Tools**:  
- `open_pr`, `get_pr_details`, `get_pr_feedback`
- `submit_pr_review`, `merge_pr`

**Repository Context**:
- `get_ci_status`, `get_repo_info`, `delete_branch`

**Introspection Tools**:
- `check_registry`: Query agent states
- `get_recent_history`: Recent agent activity
- `list_agent_roles`: Available agent types

**Communication**:
- `comment_on_issue`: Post status updates

### Tool Selection

Tools are selected per-agent via frontmatter configuration:

```python
# In agent_manager.py
def get_agent_tools(self, agent_def: AgentDefinition) -> list[Tool]:
    return self.squadron_tools.get_tools(
        agent_id=agent.id,
        names=agent_def.tools  # From YAML frontmatter
    )
```

This enables:
- **Principle of least privilege**: Agents only get needed tools
- **Role-based access**: Dev agents can't manage issues, PM can't push code
- **Security**: Reduced attack surface per agent

### Agent Session Management

**Ephemeral Agents** (PM):
```python
async def invoke_ephemeral_agent(self, agent_def, context):
    session_config = build_session_config(agent_def, context)
    agent = CopilotAgent(session_config)
    
    try:
        result = await agent.run()
        await self.registry.mark_completed(agent.id, result)
    finally:
        await agent.cleanup()
```

**Persistent Agents** (Dev/Review):
```python
async def resume_persistent_agent(self, agent_record, new_context):
    resume_config = build_resume_config(agent_record, new_context)
    agent = CopilotAgent(resume_config)
    
    # Agent continues from where it left off
    result = await agent.resume()
    
    if result.status == "sleeping":
        await self.registry.mark_sleeping(agent.id, result.blocker)
    elif result.status == "completed":
        await self.registry.mark_completed(agent.id, result)
```

## Data Flow

### Issue → Feature Implementation Flow

1. **Issue Created**: User opens issue labeled "feature"
2. **Webhook Reception**: Squadron receives `issues.labeled` event
3. **Pipeline Triggered**: Pipeline engine matches `feature-dev-lifecycle` pipeline
4. **Agent Stage**: Pipeline spawns `feat-dev` agent
5. **Implementation**: Dev agent:
   - Creates git worktree
   - Writes code and tests
   - Commits changes (`git_push`)
   - Opens pull request (`open_pr`)
6. **PR Pipeline Triggered**: PR opening triggers `pr-lifecycle` pipeline
7. **Review Stages**: Review agents analyze PR code
8. **Gate Stage**: Approval gate checks PR reviews and CI status
9. **Action Stage**: Auto-merge stage merges the PR

### Event Processing Pipeline

```python
# Webhook → Event Router → Pipeline Engine

@app.post("/webhook")
async def github_webhook(request: Request):
    event = parse_github_event(request)
    await event_router.route_event(event)

class EventRouter:
    async def route_event(self, event: GitHubEvent):
        # Pipeline engine evaluates event against all triggers
        run = await pipeline_engine.evaluate_event(
            event.type, event.payload, event
        )
        if run:
            logger.info("Pipeline '%s' started: %s", run.pipeline_name, run.run_id)

        # Also notify running pipelines of reactive events
        # (e.g. reevaluate gates when PR review is submitted)
```

## Configuration System

### Project Configuration (`config.yaml`)

```yaml
project:
  name: "squadron"
  owner: "your-org"  
  repo: "squadron"
  default_branch: main

human_groups:
  maintainers: ["alice", "bob"]
  reviewers: ["charlie"]

labels:
  types: [feature, bug, security, docs]
  priorities: [critical, high, medium, low]

circuit_breakers:
  defaults:
    max_turns: 25
    max_tool_calls: 50
    max_duration: 1800
```

### Agent Configuration

Each agent in `.squadron/agents/*.md` specifies:
- **Tools**: Which capabilities the agent has
- **Circuit breakers**: Resource limits and timeouts
- **Lifecycle**: Ephemeral or persistent behavior
- **System prompt**: Instructions and guidelines

### Environment Variables

```bash
# GitHub integration
GITHUB_APP_ID=123456
GITHUB_APP_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----..."
GITHUB_WEBHOOK_SECRET=secret

# LLM API access
OPENAI_API_KEY=sk-...
# or ANTHROPIC_API_KEY=sk-ant-...

# Database and runtime
DATABASE_URL=sqlite:///squadron.db
LOG_LEVEL=INFO
```

## Security Model

### Authentication & Authorization

**GitHub App Authentication**:
- JWT tokens for GitHub API access
- App-level permissions (read/write repository content)
- Webhook signature verification

**LLM API Security**:
- API keys stored as environment variables
- Rate limiting via circuit breakers
- Usage monitoring and alerting

### Agent Sandboxing

**Tool-Level Security**:
```python
# Only PM agents can manage issues
PM_TOOLS = ["create_issue", "label_issue", "assign_issue"]

# Only dev agents can push code  
DEV_TOOLS = ["git_push", "open_pr"]

# Tools are explicitly granted per agent
```

**Git Isolation**:
- Each agent gets its own git worktree
- No shared state between agents
- Automatic cleanup on agent termination

**Resource Limits**:
```yaml
circuit_breaker:
  max_turns: 25          # Conversation limit
  max_tool_calls: 50     # API call limit  
  max_duration: 1800     # Runtime limit (30min)
```

### Audit Trail

All agent actions are recorded in:
- **GitHub audit log**: All API calls are attributed to the GitHub App
- **Squadron logs**: Detailed agent activity and decisions
- **Git history**: All code changes are committed with agent attribution
- **Issue/PR comments**: Human-readable status updates

## Scalability & Performance

### Concurrency Model

```python
# AsyncIO-based concurrency
async def handle_multiple_agents():
    pm_task = asyncio.create_task(invoke_pm_agent())
    dev_tasks = [
        asyncio.create_task(invoke_dev_agent(i)) 
        for i in active_issues
    ]
    await asyncio.gather(pm_task, *dev_tasks)
```

### Resource Management

**Database Connection Pooling**:
```python
# SQLite with connection pooling
DATABASE_POOL_SIZE = 20
DATABASE_TIMEOUT = 30
```

**LLM API Rate Limiting**:
```python
# Built into circuit breakers
max_concurrent_llm_calls = 5
api_rate_limit_per_minute = 60
```

**Memory Management**:
- Agent sessions are cleaned up after completion
- Git worktrees are automatically removed
- Database cleanup of old agent records

### Horizontal Scaling

For high-volume deployments:

**Stateless Service Design**:
- All state stored in database
- Multiple Squadron instances can share database
- Load balancer distributes webhook traffic

**Event Queue (Optional)**:
```python
# For high throughput, use message queue
REDIS_URL = "redis://cache:6379"
CELERY_BROKER = REDIS_URL
```

**Database Scaling**:
- PostgreSQL for production deployments
- Read replicas for agent introspection
- Separate databases per environment

## Monitoring & Observability

### Health Checks

```python
@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "agents": await registry.count_active_agents(),
        "database": await check_database_connectivity(),
        "github_api": await check_github_api_access()
    }
```

### Metrics

```python
# Prometheus-style metrics
AGENT_INVOCATIONS = Counter("squadron_agent_invocations_total")
AGENT_DURATION = Histogram("squadron_agent_duration_seconds")  
TOOL_CALLS = Counter("squadron_tool_calls_total")
CIRCUIT_BREAKER_TRIPS = Counter("squadron_circuit_breaker_trips_total")
```

### Distributed Tracing

```python
# OpenTelemetry integration
@trace.instrument
async def invoke_agent(agent_def: AgentDefinition):
    span = trace.get_current_span()
    span.set_attribute("agent.role", agent_def.role)
    span.set_attribute("agent.tools", len(agent_def.tools))
```

## Deployment Patterns

### Single Repository Deployment
- One Squadron instance per repository
- Simplest setup and isolation
- Recommended for most use cases

### Multi-Repository Deployment  
- One Squadron instance managing multiple repositories
- Requires careful configuration and access control
- Suitable for organizations with many small repos

### Federated Deployment
- Multiple Squadron instances with shared agent definitions
- Complex but provides maximum scalability
- For large organizations with diverse needs

## Design Decisions & Trade-offs

### GitHub-Native vs. Custom UI
**Decision**: Use GitHub's native interface exclusively
**Rationale**: 
- ✅ Familiar interface for developers
- ✅ Built-in access controls and audit trails
- ✅ No additional UI to maintain
- ❌ Limited customization options

### Tool-Based vs. Hardcoded Capabilities
**Decision**: Tool-based architecture with explicit selection
**Rationale**:
- ✅ Flexible agent configuration
- ✅ Principle of least privilege
- ✅ Easy to add new capabilities
- ❌ More complex configuration

### Introspection vs. Context Injection
**Decision**: Agents use tools to understand state
**Rationale**:
- ✅ More predictable behavior
- ✅ Easier to debug and audit
- ✅ Agents learn to ask for what they need
- ❌ Potentially more LLM API calls

### SQLite vs. PostgreSQL
**Decision**: SQLite for development, PostgreSQL for production
**Rationale**:
- ✅ Simple setup for development
- ✅ Production-ready scaling when needed
- ✅ Same schema works for both
- ❌ Additional complexity for production

This architecture provides a robust, scalable foundation for autonomous development while maintaining human oversight and control through GitHub's native interfaces.
