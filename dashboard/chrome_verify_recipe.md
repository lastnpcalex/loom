# Loom Stream Debug — Executable Recipe

**Status as of 2026-06-25:** Working recipe — used live in commit 6858ba9 to
diagnose the 103-minute silent-render bug (conv 161 msg 17583). Promote to a
real Claude Code skill (`.claude/skills/loom-stream-debug/SKILL.md`) when
ready; the frontmatter is at the bottom of this file ready to copy.

## When to use this

User reports any of:
- "Loom is stuck" / "frozen"
- "GPU is crunching but nothing is landing in the UI"
- "The UI is empty for X minutes"
- "Messages vanished after a session limit"
- Reconnect-feedback freezes
- Streaming text shows but stops mid-stream

The recipe distinguishes **three layers** where the bug can live:
1. Backend dead → restart Loom server
2. Backend alive but chunks not reaching browser → server-side bug
3. Chunks reach browser but UI drops them → client-side bug

## Required pieces (in repo since 6858ba9)

| Piece | What | Where |
|---|---|---|
| `/api/debug/stream-state/{conv_id}` | localhost-only endpoint, returns last event timestamp, count, ws clients, subprocess state | `server.py` |
| `#stream-debug-overlay` | Fixed-position overlay showing live state | `static/chat.js` |
| `_reconstructing` watchdog | Auto-clears stuck silent-drop gate after 15s | `static/chat.js` |

## Step-by-step

### Step 1 — verify server is reachable

```powershell
Get-NetTCPConnection -State Listen -LocalPort 3000
```

Loom uses HTTPS with self-signed certs. Use `https://localhost:3000`.

### Step 2 — find the conversation

```sql
-- Most recently updated
SELECT id, title, mode, nrol_operator, cc_model, updated_at
FROM conversations ORDER BY updated_at DESC LIMIT 10;

-- Suspicious long generations (likely "silent for N minutes" candidates)
SELECT id, conversation_id, role, length(content) AS clen,
       generation_ms, cc_model_used
FROM messages
WHERE generation_ms > 60000
ORDER BY created_at DESC LIMIT 10;
```

DB filename is environment-specific. List with `ls loom*.db` and pick the
largest.

### Step 3 — query stream state

```bash
curl -sk https://localhost:3000/api/debug/stream-state/<conv_id> | jq .
```

Decision table:

| `seconds_since_last_event` | `claude_subprocess_alive` | `ws_clients_now` | Diagnosis |
|---|---|---|---|
| < 10 | true | ≥ 1 | Actively streaming — UI bug if user sees nothing |
| < 10 | true | 0 | WS dropped, server still emitting into void |
| > 60 | true | (any) | Subprocess hung internally |
| (any) | false | (any) | Subprocess exited — backend done or crashed |
| 404 | — | — | Server stale, restart Loom |

### Step 4 — attach to the page

`mcp__chrome-devtools__*` drives a **separate** Chrome instance — safe to use
while the user is on the real browser.

```
navigate_page  url=https://localhost:3000/?conv=<id>&debug=1  type=url
```

If you land on home (URL params don't always route through Loom's SPA),
`take_snapshot` then `click` the conv title StaticText.

### Step 5 — read live State

```
evaluate_script  function=`async () => {
  for (let i = 0; i < 50; i++) {
    if (typeof State !== 'undefined' && State.currentConvId) break;
    await new Promise(r => setTimeout(r, 100));
  }
  const s = State;
  return {
    currentConvId: s.currentConvId,
    isStreaming: s.isStreaming,
    _reconstructing: s._reconstructing,
    _streamIsOurBranch: s._streamIsOurBranch,
    wsReadyState: s.ws ? s.ws.readyState : null,
    messagesCount: (s.messages || []).length,
    streamingDivConnected: (typeof streamingDiv !== 'undefined' && streamingDiv)
      ? streamingDiv.isConnected : 'undef-or-null',
    overlayText: document.getElementById('stream-debug-overlay')?.textContent || null,
  };
}`
```

`wsReadyState` values: `0=connecting 1=open 2=closing 3=closed`.

### Step 6 — cross-reference

| Server says | Client says | Bug lives in |
|---|---|---|
| events recent | low messagesCount, no streamingDiv | Client (chat.js) — dropping chunks |
| events recent | streamingDiv connected, content rendering | Nothing — user perception off |
| events=0 long time | (any) | Backend client (agy/codex/claude/llama) |
| 404 | — | Server stale — restart |

### Step 7 — list console errors

```
list_console_messages  types=["error","warn"]
```

A stuck `_reconstructing` doesn't log; cascading failures usually do.

## Known failure shapes this diagnoses

- **`_reconstructing=true` stuck** → silent-drop gate from commit 3c28620's
  WS-reconnect fix. Overlay turns yellow. 15s watchdog (commit 6858ba9)
  caps the window. If watchdog itself fails, investigate why neither `.then`
  nor `.catch` of `loadMessages` is firing.
- **`streamingDiv.isConnected=false`** while chunks arriving →
  `_droppedChunkCount` in overlay rises. Means `renderMessages` ran mid-stream
  and detached the div, but reconnect path didn't rebuild.
- **`ws.readyState != 1`** → WS dropped, automatic reconnect should fire from
  `setupWebSocket` in chat.js.
- **`events_sent_this_session=0` after minutes** → subprocess didn't emit.
  Check the relevant client (agy/codex/claude/llama). For agy specifically,
  check `~/.gemini/antigravity-cli/log/cli-*.log` mtime as a liveness signal.

## What this is NOT for

- Fixing bugs — it tells you the *layer*, not the line. Hand off to
  code-editing after.
- Programmatic Loom-driving — that's a UI test harness; this is interactive
  observation.
- Running tests — use pytest.

## Skill frontmatter (copy to `.claude/skills/loom-stream-debug/SKILL.md`)

```yaml
---
name: loom-stream-debug
description: >-
  Diagnose Loom streaming UI bugs (frozen UI, GPU crunching but no tokens,
  messages disappearing after rate limit, reconnect freezes) by attaching to
  the running browser via chrome-devtools MCP and querying the per-conv
  stream-state endpoint. Distinguishes backend-dead vs server-emitting-but-
  client-dropping vs subprocess-hung. Requires Loom on https://localhost:3000
  and chrome-devtools MCP. The MCP drives a separate Chrome — safe alongside
  the user's normal browser. Triggers: "Loom is stuck", "nothing is landing",
  "UI froze", "GPU busy but no output", "messages vanished", /loom-stream-debug.
---
```
