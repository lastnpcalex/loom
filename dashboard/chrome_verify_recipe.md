# Chrome-devtools MCP Verification Recipe for Loom UI

**Status:** Starter pattern, not yet wrapped in a test runner.  
**Goal:** Give the next LLM a concrete starting point so they don't have to re-derive how to drive Loom's UI through `mcp__chrome-devtools__*` for UX regression checks.

---

## Why This Exists

UX regressions in Loom (streaming text rendering, WebSocket reconnect, tool-block layout, branch switching) repeatedly land because there's no automated check that exercises the actual rendered DOM. Unit tests pass; the UI is visibly broken. The user has asked, across multiple sessions, for "a robust debugging platform where it proved the ux and ui worked by using mcp web tools with chrome" — and across multiple sessions, no LLM has finished it.

The 29 chrome-devtools MCP tools are connected to this workspace (see top-of-conversation system context). The building blocks exist. What's missing is:
- A reusable recipe (this file)
- A pytest wrapper that runs that recipe and asserts
- A pre-commit hook that triggers it when `static/chat.js` / `static/style.css` change

---

## The Tools You Have

From `mcp__chrome-devtools__*`:

- `navigate_page(url)` — open Loom in the connected Chrome instance
- `take_snapshot()` — get a DOM snapshot (text + selectors)
- `take_screenshot()` — pixel snapshot
- `evaluate_script(js)` — run arbitrary JS in the page
- `fill(selector, text)` / `click(selector)` — interact
- `list_console_messages()` — grab any errors
- `wait_for(selector_or_predicate)` — sync on UI state
- `list_network_requests()` — verify WS / HTTP traffic
- `new_page()` / `select_page()` / `close_page()` — page lifecycle

These are not literal Python functions — they're MCP tools you call via the same mechanism as any other MCP tool in this environment.

---

## Minimum Viable Recipe — "Did the Streaming Render Survive?"

This is the smallest possible end-to-end check. Apply tonight's `.streaming-text { white-space: pre-wrap; }` fix; this recipe should show preserved newlines.

```text
1. navigate_page("https://localhost:3000/")
2. wait_for selector: ".conversation-item" (sidebar populated)
3. click one operator conversation in the sidebar
4. wait_for selector: ".message-content"
5. evaluate_script:
     // synthesize a streaming chunk arrival with newlines
     const div = document.querySelector('.streaming-text') || (() => {
       const m = document.createElement('div');
       m.className = 'message message-generating';
       m.innerHTML = '<div class="message-content"><span class="streaming-text"></span></div>';
       document.getElementById('messages').appendChild(m);
       return m.querySelector('.streaming-text');
     })();
     div.appendChild(document.createTextNode("line one\nline two\nline three"));
     return getComputedStyle(div).whiteSpace;
6. ASSERT result equals "pre-wrap" (or any value that preserves \n)
7. take_screenshot — save to dashboard/screenshots/streaming_after_<commit>.png
8. list_console_messages — assert no new errors
```

If step 6 returns `"normal"`, the CSS fix has regressed. The screenshot at step 7 will visibly show the regression.

---

## Recipe — "WebSocket Reconnect Doesn't Freeze the UI"

Harder, but high-value (this bug ate hours tonight).

```text
1. navigate_page, click a streaming-capable conversation
2. fill #message-input with "say something long"
3. click #btn-send
4. wait_for .streaming-text (stream has started)
5. evaluate_script: window._lwsRef = ...; ...close the WebSocket via JS to simulate disconnect
6. wait 500ms
7. wait_for indicator that reconnect happened (status bar text, or a new ws in list_network_requests)
8. evaluate_script: return document.querySelector('.streaming-text').textContent.length
9. wait 2000ms
10. evaluate_script: return document.querySelector('.streaming-text').textContent.length
11. ASSERT length(step 10) > length(step 8) — i.e., new chunks ARE landing post-reconnect
12. screenshot, console-messages-check
```

If step 11 fails, the reconnect feedback loop has regressed.

---

## Where to Put It When You Finish

- This file is the recipe (human-readable).
- The Python wrapper should live at `tests/test_ui_smoke.py` and use the same MCP-tool-call mechanism.
- The pre-commit hook trigger should be `static/chat.js`, `static/style.css`, anything matching `mcp_*.py`, and provider clients.

---

## Known Gotchas When You Try This

- The chrome-devtools MCP connects asynchronously at session start. If it's not listed in tools yet, call `ToolSearch` with `"chrome-devtools"` and wait — don't assume it's unavailable.
- Loom runs on https://127.0.0.1:3000 with a self-signed cert. Chrome may need to be navigated past the warning once per session.
- `evaluate_script`'s return value comes back as text — JSON-stringify on the JS side for complex returns.
- The user runs on Windows. PowerShell path semantics matter for any subprocess this harness spawns. Use forward slashes in URLs, escape backslashes carefully in JS embedded strings.
