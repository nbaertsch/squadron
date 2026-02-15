# SDK Research: Context Serialization & Rehydration

**Date:** 2026-02-14  
**Question:** Do popular LLM agent SDKs support serializing an agent's conversation/context state to disk and restoring it later?

**Relevance:** Squadron agents need to SLEEP when blocked (serialize context) and WAKE when unblocked (rehydrate from checkpoint). See AD-003.

---

## Summary Table

| SDK | Context Serialization | Mechanism | Checkpoint Mid-Execution | Effort Level |
|---|---|---|---|---|
| **Anthropic Claude SDK** | Manual (trivial) | Save `messages[]` as JSON | No (manual loop mgmt) | Very Low |
| **OpenAI SDK** | Native + Manual | `conversations`, `previous_response_id`, or manual messages | Server-side via Conversations API | Low |
| **GitHub Copilot Extensions** | Not supported | GitHub sends history to you; custom state is DIY | No | Medium-High |
| **LangGraph** | **Native, comprehensive** | Checkpointers (SQLite/Postgres/Memory), time-travel, replay | **Yes** — automatic at every super-step | Very Low |
| **CrewAI** | Partial (Flows state) | Pydantic state in Flows; memory system for agents | No native mid-crew checkpoint | Medium |
| **AutoGen (Microsoft)** | Config only | `dump_component()` / `load_component()` for definitions | No (config only, not runtime state) | Medium |

---

## Detailed Findings

### 1. Anthropic Claude SDK (Python & TypeScript)

**Natively supported?** No dedicated serialization API. But trivially easy due to stateless design.

**How it works:** The Messages API is stateless. Every request takes a `messages` array. There is no server-side session.

**Serialization approach:**
- The `messages` list IS the state. Save as JSON, reload, pass back to `client.messages.create()`.
- Response objects are Pydantic models with `.to_json()` / `.to_dict()`.
- Tool-use flows: append `tool_use` response and `tool_result` to the same messages array — all JSON-serializable.

**Limitations:**
- No built-in checkpoint/resume for agentic tool loops — must manage the loop yourself.
- System prompts, model config, and tool definitions must be saved separately.
- Full history re-sent every call (and billed for input tokens).

---

### 2. OpenAI SDK (Responses API / Chat Completions)

**Natively supported?** Yes, with multiple mechanisms.

| Method | Description |
|---|---|
| Manual message array | Like Anthropic — serialize history as JSON |
| `previous_response_id` | Chain responses server-side. Store only the ID. |
| Conversations API | Create a `conversation` object, pass ID to subsequent calls. Server-side durable state. |
| Assistants API | Threads + Runs (deprecated, shutting down Aug 2026). |

**Limitations:**
- `previous_response_id` chains re-bill all prior input tokens.
- Server-side state tied to OpenAI account/API key.

---

### 3. GitHub Copilot Extensions / SDK

**Natively supported?** No.

Copilot Extensions follow a stateless request/response model. GitHub sends conversation history to the agent on every invocation. No server-side session management from GitHub's side.

**Workaround:** All custom state persistence is DIY (database, file, Redis, etc.).

---

### 4. LangChain / LangGraph ⭐

**Natively supported?** Yes — first-class, deeply integrated. The most comprehensive checkpointing system reviewed.

**Key features:**
- **Checkpointers:** `InMemorySaver`, `SqliteSaver`, `PostgresSaver`, `CosmosDBSaver`. State saved at every super-step automatically.
- **Threads:** Each conversation keyed by `thread_id`.
- **State snapshots:** `graph.get_state(config)` returns full state.
- **Time-travel / Replay:** Invoke with a specific `checkpoint_id` to replay from any prior point.
- **Update state:** `graph.update_state(config, values)` — modify state at any checkpoint.
- **Fault tolerance:** Pending writes from successful nodes preserved on failure.
- **Memory Store:** Cross-thread long-term memory with optional semantic search.

**Limitations:**
- `InMemorySaver` not durable across process restarts — use SQLite/Postgres.
- Some object types may not serialize cleanly.
- Checkpointing overhead per super-step.

---

### 5. CrewAI

**Natively supported?** Partially.

- **Memory system:** Short-term, long-term, entity memory. But this is for agent learning, not execution state.
- **Flows state:** `Flow[StateModel]` uses Pydantic. Not automatically persisted to disk.
- **No `save_state` / `load_state` API** for crews or agents mid-task.

**Workaround:** Manual Pydantic model serialization, task `output_file` for intermediate results.

---

### 6. AutoGen (Microsoft)

**Natively supported?** Config serialization only.

- `dump_component()` / `load_component()`: Serialize/deserialize agent definitions (model, system message, handoffs) — not conversation history.
- No mid-execution checkpoint/resume.
- Tool serialization not yet supported.

**Workaround:** Save agent configs + manually extract and inject conversation messages.

---

## Key Takeaway

**LangGraph** is the clear leader for full context serialization/rehydration with built-in checkpointing, time-travel, and durable persistence backends. If Squadron adopts LangGraph, the sleep/wake lifecycle (AD-003) is largely solved at the framework level.

If we prefer maximum flexibility and provider independence, the **raw Anthropic/OpenAI SDK** approach (manually serialize `messages[]` as JSON) is trivial to implement and gives full control, at the cost of building the agent loop and checkpoint logic ourselves.

This is a key input to OR-001 (framework selection).
