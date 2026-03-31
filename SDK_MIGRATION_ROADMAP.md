# Claude Agent SDK Migration Roadmap

## Current Architecture (Subprocess)

Loom runs Claude Code as a subprocess via `claude -p "prompt" --output-format stream-json`.
- **claude_client.py**: Launches CC, parses NDJSON stream, yields typed events
- **cc_permission_hook.py**: PreToolUse hook script → HTTP POST → Loom server → WebSocket → browser UI
- **Session resume**: `--resume <id> --fork-session` per turn (immutable session snapshots)
- **Skill invocation**: Natural language only (slash commands don't work in `-p` mode)
- **Sub-agents**: Only see `Agent` tool_use start + tool_result end

## Target Architecture (SDK)

Replace subprocess with `@anthropic-ai/claude-agent-sdk` (TypeScript) or `claude-code-sdk` (Python).

### What We Gain
| Feature | Subprocess | SDK |
|---------|-----------|-----|
| Skills/slash commands | NL workaround only | Native `Skill` tool invocation via `settingSources` |
| Sub-agent visibility | Start/end events only | Streaming with `parent_tool_use_id`, `includePartialMessages` |
| Tool approval | HTTP hook + polling | Programmatic async callbacks |
| Session resume | CLI flags | Built-in session management |
| Streaming | Parse NDJSON stdout | Native async generator / event emitter |
| Error handling | Exit codes + stderr | Structured exceptions |

---

## Phase 1: Drop-In Replacement

**Goal**: Replace subprocess launch with SDK `query()`, keep existing event pipeline.

### Prerequisites
```bash
# Python SDK (if available — check PyPI)
pip install claude-code-sdk

# Or TypeScript SDK (more mature)
npm install @anthropic-ai/claude-agent-sdk
```

### Changes
1. **New `claude_client_sdk.py`** (parallel to existing `claude_client.py`)
   - Import SDK: `from claude_code_sdk import ClaudeCode, StreamEvent`
   - Replace `run_claude()` with SDK equivalent
   - Map SDK `StreamEvent` types → existing Loom event types
   - Keep `_process_event()` interface identical so `server.py` doesn't change

2. **Feature flag**: `USE_SDK = os.getenv("LOOM_USE_SDK", "0") == "1"`
   - Import `claude_client_sdk` if flag set, else `claude_client`
   - Allows instant rollback

3. **Session resume**: SDK handles session management internally
   - Remove manual `--resume` / `--fork-session` flag logic
   - SDK provides `resume(sessionId)` and session forking

### SDK Query (TypeScript reference — Python similar)
```typescript
import { query, StreamEvent } from '@anthropic-ai/claude-agent-sdk';

const stream = query({
  prompt: "...",
  options: {
    model: "sonnet",
    maxTokens: 16384,
    settingSources: ['user', 'project'],  // loads skills
    permissionMode: "default",
  },
  cwd: projectDir,
  resume: { sessionId, forkSession: true },
});

for await (const event of stream) {
  // event.type: 'text', 'tool_use', 'tool_result', 'thinking', etc.
}
```

### Risk: Low
- Existing event pipeline stays the same
- Subprocess fallback always available
- No UI changes needed

---

## Phase 2: Programmatic Tool Approval

**Goal**: Replace HTTP hook bridge with SDK's native permission callbacks.

### What Changes
- **Delete**: `cc_permission_hook.py` (hook script)
- **Delete**: `/api/cc-permission` endpoint in server.py
- **Delete**: `_configure_permission_hook()` in claude_client.py
- **Add**: Permission callback in SDK query options

### SDK Permission Callback
```typescript
const stream = query({
  prompt: "...",
  options: {
    permissionMode: "default",
  },
  onPermissionRequest: async (request) => {
    // request: { tool_name, tool_input, ... }
    // Forward to browser via existing WebSocket
    const response = await forwardToLoomUI(request);
    return response.allow ? 'allow' : 'deny';
  },
});
```

### Migration
1. The `onPermissionRequest` callback replaces the entire hook → HTTP → WS → HTTP → hook pipeline
2. The WebSocket-based UI (notification bell + inline prompts) stays identical
3. Auto-approve logic (`_auto_approve_sessions`) moves into the callback

### Risk: Medium
- Permission flow is critical path — bugs here block all generation
- Need thorough testing with Allow/Deny/Allow All flows
- HTTP hook is a clean boundary; SDK callback is tighter coupling

---

## Phase 3: Skill Discovery & Invocation

**Goal**: Native skill invocation instead of natural language workaround.

### What Changes
- **Update**: `skill_scanner.py` to also query SDK for available skills
- **Update**: Slash command autocomplete to include SDK-discovered skills
- **Add**: Pass `settingSources: ['user', 'project']` to SDK options
  - This tells SDK to load skills from `.claude/skills/`
  - Skills become available as the `Skill` tool

### SDK Skill Loading
```typescript
const stream = query({
  prompt: "Please commit the current changes",
  options: {
    settingSources: ['user', 'project'],
    // Skills from .claude/skills/ are now available
    // Claude can invoke them via the Skill tool
  },
});
```

### Skill Invocation Strategy
- **Before SDK**: `/commit` → NL translation → CC → CC reads SKILL.md → follows it
- **With SDK**: `/commit` → SDK `Skill` tool → loads SKILL.md → follows it
- The NL translation remains as a fallback and for custom user phrasing
- SDK makes skill invocation more reliable (exact tool call, not semantic matching)

### Risk: Low
- Skills are additive — existing NL approach still works
- `settingSources` is a config option, not a code change

---

## Phase 4: Sub-Agent Visibility

**Goal**: Stream intermediate sub-agent events to the UI.

### What Changes
- **Update**: Event stream handler to detect `parent_tool_use_id` on events
- **Add**: `includePartialMessages: true` to SDK options
- **Add**: UI for sub-agent tree (nested tool blocks)

### SDK Sub-Agent Events
```typescript
const stream = query({
  options: {
    includePartialMessages: true,  // get subagent intermediate events
  },
});

for await (const event of stream) {
  if (event.parent_tool_use_id) {
    // This is a subagent event — show nested in UI
    renderSubagentEvent(event.parent_tool_use_id, event);
  }
}
```

### UI Changes
- When `Agent` tool_use starts, create a nested container
- Stream subagent events (text, tool_use, tool_result) inside that container
- Collapse/expand subagent activity
- Show subagent `agent_id` and description

### Risk: Medium
- Subagent events can be high-volume (many tool calls)
- Need to handle concurrent subagents
- Virtual scroll implications for deeply nested content

---

## Phase 5: Channels (Future — Research Preview)

Claude Code Channels allow pushing events into a running session via MCP.
- Currently requires claude.ai login (no API key auth)
- Could enable: injecting commands mid-session, live collaboration
- **Status**: Research preview, not production-ready
- **Action**: Monitor for GA release, evaluate when available

---

## Migration Risks & Rollback

### Breaking Changes
1. **Python SDK maturity**: As of early 2026, the TypeScript SDK is more mature. Python SDK may lag behind on features.
2. **Windows compatibility**: SDK may have different process management on Windows (Loom's ProactorEventLoop pipe error patch may not be needed, or may need new patches).
3. **Environment variables**: SDK may handle `ANTHROPIC_API_KEY` differently.

### Rollback Strategy
- Keep `claude_client.py` (subprocess) alongside `claude_client_sdk.py`
- Feature flag `LOOM_USE_SDK` switches between them
- Both produce the same event types → no server.py changes needed
- Database schema is unchanged (sessions, content_blocks, etc.)

### What to Keep
- **Database schema**: Completely unchanged
- **WebSocket streaming**: Same event pipeline, just different source
- **Permission UI**: Bell notifications, inline prompts — all stay
- **Session model**: Fork-every-turn remains the conceptual model
- **Progressive saves**: Same pattern, same DB calls

### What to Replace
- `claude_client.py` subprocess launch → SDK `query()`
- `cc_permission_hook.py` → SDK `onPermissionRequest` callback
- `_configure_permission_hook()` → Not needed with SDK
- NL skill translation → Direct SDK skill invocation (keep NL as fallback)

---

## Timeline Estimate

| Phase | Complexity | Dependencies |
|-------|-----------|--------------|
| Phase 1: Drop-in replacement | 2-3 days | SDK availability for Python |
| Phase 2: Permission callbacks | 1-2 days | Phase 1 |
| Phase 3: Skill invocation | 1 day | Phase 1 |
| Phase 4: Sub-agent visibility | 2-3 days | Phase 1 + UI work |
| Phase 5: Channels | TBD | GA release |

**Total: ~1-2 weeks** for Phases 1-4, assuming SDK is stable.

---

## References

- [Claude Agent SDK Overview](https://platform.claude.com/docs/en/agent-sdk/overview)
- [Streaming Output (SDK)](https://platform.claude.com/docs/en/agent-sdk/streaming-output)
- [Subagents (SDK)](https://platform.claude.com/docs/en/agent-sdk/subagents)
- [Skills (SDK)](https://platform.claude.com/docs/en/agent-sdk/skills)
- [Headless Mode Docs](https://code.claude.com/docs/en/headless)
- [sugyan/claude-code-webui](https://github.com/sugyan/claude-code-webui) — migrated from subprocess to SDK
- [patoles/agent-flow](https://github.com/patoles/agent-flow) — hooks-based observability
