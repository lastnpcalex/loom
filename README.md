<p align="center">
  <img src="static/img/banner.png" alt="Ex Astris Umbra">
</p>

# Ex Astris Umbra: A Shadow Loom

![Python](https://img.shields.io/badge/python-3.12+-blue) ![License](https://img.shields.io/badge/license-MIT-green)

A self-hosted web interface for branching AI conversations across Claude Code, ChatGPT Codex, Antigravity (agy), Hermes Agent, and local llama.cpp models — and for handing off between them mid-conversation. Every conversation is a tree: branch, fork, regenerate, and full-text search across every path without losing anything.

## What is a loom?

An LLM loom treats every conversation as a **tree, not a thread**. Each message is a node. At any point you can branch — regenerate, edit, fork — and explore alternate paths without losing the originals. The metaphor comes from weaving: every response is a thread, and the loom holds them all in tension so you can compare, backtrack, and choose.

This matters because LLM output is non-deterministic. The same prompt can produce a brilliant answer on one roll and a mediocre one on the next. A linear chat hides that variance — you see one path and lose the rest. A loom preserves them all. Regenerate five times, keep the best, branch from the second-best later. Edit a message from ten turns ago and watch the conversation diverge. The tree is the conversation's real shape; a single thread is just one path through it.

## The point of the system

Different work wants different engines. Frontier Claude for hard problems, a local model for private or always-on work, an RP harness for creative writing, a locked-down operator for tasks where the model must not freelance. Normally each of those is a separate app with separate history, separate UI habits, and no way to move a conversation between them.

A Shadow Loom runs them all on one branching infrastructure: one database, one tree UI, one search index, one set of chat habits. Start a refactor on a frontier model, branch the conversation, rerun the branch on a local model, compare. Search across months of agent sessions regardless of which engine produced them. The loom is the constant; the engines are interchangeable shuttles.

## Five modes, one loom

### Loom — Claude Code in the browser

Connects to the [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) as a subprocess. Full tool suite with streaming responses, thinking blocks, and permission proxying.

- Tool call blocks with expandable input/output and success/error indicators
- Edit tool diff rendering (red/green inline diffs)
- Collapsible extended thinking display
- Permission proxying — tool approvals appear in the browser UI
- Model selection across **three provider groups in one dropdown**: Anthropic (auto aliases plus pinned versions, refreshed live from `/v1/models`), Antigravity/Gemini, and ChatGPT Codex — changeable mid-conversation
- Thinking effort control for Anthropic and Codex models (`max` is Opus-only, `xhigh` is Opus/Codex)
- Plan/Act mode toggle
- Immutable session snapshots — every turn forks the CC session, enabling clean branching at any point
- Progressive draft saving — generation survives navigation, reconnects, and server restarts
- Per-turn and cumulative cost tracking
- Slash commands — Claude Code skills are scanned and exposed with autocomplete

### Braid — Claude Code on local models

The full Claude Code harness running against a local llama.cpp `llama-server`. Same tools, same permissions, same UI — just running on your hardware.

- Full Claude Code tool suite (Bash, Read, Write, Edit, Grep, etc.)
- Web search and page fetch via the bundled DuckDuckGo/trafilatura MCP server
- Permission prompts proxied through the browser UI
- Works with any GGUF with sufficient context (64k+ recommended)

### Hermes — ACP agent on local models

[Hermes Agent](https://github.com/NousResearch) over the Agent Client Protocol, powered by the same local llama-server. A different agent architecture on the same loom: ACP session management, its own slash commands, permission requests bridged into the browser like CC's. Enable with `LOOM_ENABLE_HERMES=1`.

### Weave — structured roleplay and creative writing

Character cards, personas, lore files, style nudges, and incremental summarization. A full RP harness on local models with context management that scales beyond the model's native window.

- Character system with personality, appearance, goals, relationships, scenario, greeting, and example messages
- Personas (player characters) and lore for richer world-building context
- Style nudge rotation and repetition detection
- Thinking model support (`<think>` stripping, content token counting)
- Multi-branch generation — 1-5 parallel responses per turn, pick the best
- Backstage — a side conversation with an agent that edits state cards while the RP stays untouched

#### OODA Harness

The OODA (Observe-Orient-Decide-Act) harness is cognitive scaffolding that guides the model through a structured reasoning loop before writing each response. Inspired by [metacog](https://github.com/inanna-malick/metacog) (tools as cognitive scaffolding — LLMs treat tool results as ground truth) and [popup-mcp](https://tidepool.leaflet.pub/3mcbegnuf2k2i) (amortize latency into fewer, richer passes).

**The loop:** Before generating RP prose, the model emits a structured `<ooda>` block. The server parses it, executes the state operations against the database, and returns the results. The model then writes its prose grounded in fresh state reads rather than stale context.

1. **Observe** — read the current state of characters, scenes, and relationships from the database
2. **Orient** — reason about what changed, how characters would react, what the scene demands
3. **Decide** — plan the response: what happens, who speaks, what shifts
4. **Act** — execute state updates (mood changes, location shifts, relationship evolution) and write the prose

Each step appears as a collapsible tool block in the conversation, so you can see exactly what the model observed, how it reasoned, and what state it changed.

**State cards** track the evolving state of the RP — character state, scene state, persona state, and read-only lore — in a three-tier hierarchy:

1. **Tier 1 (Character Global)** — baseline cards defined on the character itself, editable from the home page. The template copied when a character enters a conversation.
2. **Tier 2 (Conversation)** — copied from Tier 1 when OODA is enabled. The pristine base state.
3. **Tier 3 (Branch Deltas)** — state changes are saved as deltas on each assistant message. Different branches see different state; navigating a branch reconstructs base cards plus deltas along the path.

State cards are editable inline — click any field to edit, changes auto-save, the model reads them next turn.

### NROL-AO — epistemic engine operator

A locked-down Claude Code profile for operating the NROL-AO forecasting engine. File and shell tools are stripped; the conversation can only act through the typed MCP interface (`mcp_servers/nrol_ao/`), which is the authority boundary: beliefs move through validated transitions, never freeform edits.

- Evidence flows through a proposal lifecycle — submit article, propose match, human-approved commit
- Scheduled scans with a safe commit policy: decisions that cannot move posteriors auto-apply, everything else queues for operator review
- Every scan writes a digest, so "nothing happened" is reported with the same weight as "something did"

## How the harnesses interoperate

The modes are different engines, but the loom around them is shared — which is what makes mid-conversation handoff work:

- **One tree, one store.** Every mode writes the same message-tree schema in SQLite. Branching, bookmarks, search, import/export, and the tree visualization don't care which engine produced a node.
- **Cross-provider switching.** A Loom conversation can move between Anthropic, Gemini, Codex, and local models mid-conversation. Session resume is skipped across providers (their session formats are incompatible); the conversation falls back to a full history rebuild from the database whenever the nearest ancestor came from a different provider, so no context is lost. Each message records the model that generated it, and labels shift accordingly.
- **One chat surface.** Highlight-to-reply (select text in any message, quote it in your next send), attachments, image paste, message queuing, and slash commands work identically in every mode, because the excerpt and the attachments ride in the message itself rather than in provider-specific plumbing.
- **One permission system.** CC, Codex, and Hermes tool approvals all bridge into the same browser prompt UI and notification bell.
- **One canvas.** Any mode can drive the Interactive Canvas; the AI writes files, the iframe live-refreshes.

> **Note:** `AskUserQuestion` is disabled in CC modes. CC's headless `-p` mode has no mechanism to send user responses back to an active `AskUserQuestion` call — stdin is closed after the initial prompt ([open feature request](https://github.com/anthropics/claude-code/issues/16712)). Until then, CC proceeds with its best judgment instead of asking.

## Search

Every message across every conversation is searchable from the home page. Type a query, get highlighted snippet results grouped by conversation, click to jump directly to the matching message on its branch.

This is particularly useful for **Claude Code sessions**. Loom mode stores every tool call, every thinking block, every response in SQLite — an indexed, searchable archive of your entire CC history that you can branch from at any point. Find that one-off bash command from two weeks ago, locate the conversation where you debugged that migration, pull up every time the agent touched a specific file.

There's also **per-conversation search** and **tree search** (find and navigate between matching nodes on the visual tree).

## Interactive Canvas

Any conversation can enable an **Interactive Canvas** — a live website (HTML/CSS/JS) rendered within the Loom UI that the AI builds and maintains through chat. Toggle it with the Canvas button.

The canvas appears as a **meta-root node** in the tree visualization — a glowing thumbnail positioned above all branch roots, connected by bezier curves. Click it to open the fullview, where the iframe fills the screen while the chat bar stays visible.

- Live refresh — when the AI writes to the `canvas/` directory, the iframe auto-updates via WebSocket
- Progressive LOD — the thumbnail blurs as you zoom out, with a pulsing beacon at maximum zoom
- Zero-config — enabling canvas auto-creates the workspace, a `canvas/CLAUDE.md` for the AI, and a `.gitignore`

### Canvas SDK

Canvas pages can talk back to Loom through a postMessage bridge:

```html
<script src="/static/canvas-sdk.js"></script>
<script>
  Loom.send("Update the chart with the latest data");
  Loom.uploadAndSend(file, "Analyze this CSV");
  const prompt = await Loom.loadTrigger('analyze', { filename: 'data.csv' });
  Loom.dropZone(element, { trigger: 'process' });
  Loom.on('canvas_updated', () => location.reload());
</script>
```

| Method | What it does |
|--------|-------------|
| `Loom.send(content, opts)` | Send a message and trigger generation. Options: `imagePaths`, `parentId` |
| `Loom.getConvId()` | Get the current conversation ID |
| `Loom.upload(file)` | Upload a file to the conversation |
| `Loom.uploadAndSend(file, content)` | Upload a file and send a message referencing it |
| `Loom.loadTrigger(name, vars)` | Load a prompt template from `canvas/triggers/{name}.md` with `{{variable}}` interpolation |
| `Loom.dropZone(element, opts)` | Wire drag-and-drop — uploads files and sends a trigger prompt |
| `Loom.on(event, handler)` | Listen for Loom events (`canvas_updated`, `message-sent`, ...) |

## Common features

All modes share the same conversation infrastructure:

- **Tree-based conversations** — every message is a node. Branch at any point, explore alternate paths, switch between branches. Branch names use Unicode Greek letters (`α2.β1.ε6`), extending to double/triple Greek for large trees.
- **Highlight to reply** — select text in any message bubble and a floating Reply button pins the excerpt above the input; the next send carries it as an attributed quote.
- **Fork and branch** — fork any message into a new conversation; regenerate creates a sibling branch; edit any message (including the root) to diverge.
- **Ghost nodes** — active generations appear as pulsing nodes on the tree in real time.
- **Tree visualization** — interactive pan/zoom canvas, horizontal or vertical layout.
- **Bookmarks** — bookmark any message or branch.
- **Streaming generation** — real-time token streaming over WebSocket with live token rate and tool indicators.
- **Background generation** — navigate away mid-generation and come back; responses save progressively and survive reconnects, tab switches, and server restarts.
- **Notifications** — bell dropdown for landed branches and permission requests; browser push when the tab is in the background.
- **Cron jobs** — schedule a script against a conversation on an interval; manage jobs from the admin dashboard.
- **Image handling** — clipboard paste, attachments, vision-model description for local modes, inline display of images the agent generates.
- **Message queuing** — send your next message while the model is still responding.
- **Per-tab state** — each browser tab remembers its own conversation and view.
- **HTTPS / Tailscale** — serves on `0.0.0.0` over HTTPS with auto-detected SSL certs for access across your tailnet.
- **WebGL black hole** — Schwarzschild raytracer background with procedural galaxy texture and glassmorphism UI.

## Session management (CC modes)

- **Fork-every-turn** — every generation uses `--resume <parent_session> --fork-session`. Each assistant message gets its own immutable session snapshot, so branching, editing, and regenerating all work from any point.
- **Progressive drafts** — a draft message is created immediately when generation starts, updated as tools execute, finalized on stream end.
- **History rebuild fallback** — when no session exists to resume (first message, or after a provider switch), the full history is rebuilt from the database and sent as a single prompt, tool calls included.

## Admin dashboard

A separate admin server (`admin_server.py`, port 3002) runs a single-page dashboard for operating the whole stack:

- **Overview** — Loom instance cards (start / restart / shutdown), active generation tracking with kill switches, live CPU/RAM/VRAM specs
- **Servers** — llama-server lifecycle with per-model launch config and a switch-model control, ComfyUI management, NROL-AO dashboard and MCP tools
- **Terminal** — a real web terminal ([ttyd](https://github.com/tsl0922/ttyd)) embedded in the page: PowerShell, cmd, or a Claude Code session. Binds localhost by default; `TTYD_HOST`/`TTYD_CRED` expose it over your tailnet.
- **Tools** — Claude auth status and OAuth refresh flow, VRAM cleanup, disk usage
- **Cron** — enable, disable, and archive scheduled jobs

llama-server is managed like a service: per-model context size, GPU layers, KV-cache quantization, flash attention, and mmproj are stored per GGUF and applied on launch, restartable from the Loom settings panel or the dashboard.

## Quick start

```bash
# Clone
git clone https://github.com/lastnpcalex/a-shadow-loom.git
cd a-shadow-loom

# Install dependencies
pip install -r requirements.txt

# Run
python server.py
```

Open `https://localhost:3000` in your browser.

**Loom mode** needs the [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) on PATH with an active subscription or API key. Codex and Antigravity models additionally need their CLIs (`codex`, `agy`) authenticated.

**Braid / Weave / Hermes modes** need [llama.cpp](https://github.com/ggml-org/llama.cpp)'s `llama-server` and a GGUF model. Point `llama_server_exe` and `llama_models_dir` at your install (Settings → Advanced, or `config.json`), pick a model, and Apply & Restart — the server is launched and managed for you. Hermes additionally needs the [Hermes Agent](https://github.com/NousResearch) installed and `LOOM_ENABLE_HERMES=1`.

**Web search for local models** — register the bundled MCP server once:

```bash
claude mcp add --scope user --transport stdio web-tools -- python /absolute/path/to/mcp_web_tools.py
```

This gives local models `web_search` (DuckDuckGo) and `web_fetch` (trafilatura) as CC tools.

**Admin dashboard** (optional):

```bash
python admin_server.py  # dashboard on https://localhost:3002
```

## Project structure

```
server.py              -- FastAPI server, WebSocket streaming, REST endpoints
database.py            -- SQLite schema, message tree CRUD, branch management
config.py              -- Configuration (llama-server, context budget, SSL, generation)
prompt_engine.py       -- System prompt assembly, repetition detection, style nudges
context_manager.py     -- Token counting, context window management
ooda_harness.py        -- OODA loop: XML parser, state executors, prompt builder
character_loader.py    -- Parse/save character, persona, and lore .md files
llama_client.py        -- llama-server client (OpenAI-compatible chat + vision)
claude_client.py       -- Claude Code CLI subprocess wrapper, NDJSON stream parser
codex_client.py        -- ChatGPT Codex app-server wrapper
gemini_client.py       -- Antigravity (agy) subprocess wrapper
hermes_client.py       -- Hermes Agent ACP client and permission bridge
cc_permission_hook.py  -- PreToolUse hook for browser-based permission prompts
mcp_web_tools.py       -- MCP stdio server: web_search + web_fetch for local models
mcp_servers/nrol_ao/   -- NROL-AO MCP server: typed transitions, proposals, scans
admin_server.py        -- Admin dashboard: instances, llama-server, ttyd, cron
static/
  index.html           -- Single-page app shell
  app.js               -- State management, home view, character/persona/lore CRUD
  chat.js              -- WebSocket chat, message rendering, streaming, branching
  tree.js              -- Interactive tree visualization
  style.css            -- Glassmorphism, cyan/purple palette
  blackhole.js         -- Schwarzschild raytracer (pre-compiled GLSL)
  admin/               -- Admin dashboard SPA

characters/            -- Character definition files (.md)
personas/              -- User persona files (.md)
lore/                  -- Lore/history context files (.md)
certs/                 -- SSL certificates (auto-detected, gitignored)
```

## Configuration

Settings are adjustable from the UI (gear icon) or by editing `config.json`:

| Setting | Default | Description |
|---------|---------|-------------|
| `llama_host` | `http://localhost:8000` | llama-server address |
| `llama_model` | (your GGUF) | Default local model |
| `llama_server_exe` | `llama-server` | Path to the llama-server binary |
| `llama_models_dir` | `C:\LlamaServer\models` | Directory scanned for .gguf files |
| `max_context_tokens` | `32768` | Context window budget |
| `verbatim_window` | `6` | Recent messages kept verbatim |
| `temperature` | `0.8` | Generation temperature |
| `top_p` | `0.9` | Nucleus sampling |
| `max_tokens` | `1024` | Max generation length |
| `repeat_penalty` | `1.08` | Repetition penalty |
| `ssl_certfile` | `certs/cert.pem` | SSL certificate path |
| `ssl_keyfile` | `certs/key.pem` | SSL key path |

Per-model llama-server tuning (context size, GPU layers, KV-cache quant, threads, batch sizes, mmproj, extra args) lives in `models_config.json` and is editable from Settings → Advanced.

## Character file format

Characters are Markdown files in `characters/` with YAML frontmatter:

```markdown
---
name: Lyra Ashwood
avatar: null
tags: [fantasy, rogue, adventurer]
---
# Personality
Description of who this character is, how they speak, their mannerisms...

# Scenario
The setting and situation where the RP begins...

# Greeting
The character's opening message to the player...

# Example Messages
## Example 1
user: Player says something
assistant: Character responds in their style
```

Characters, personas, and lore can also be created, edited, and imported/exported from the home page UI.

## Data safety

- **SQLite WAL mode** — Write-Ahead Logging for crash resilience
- **WAL checkpoint on shutdown** — the `/shutdown` endpoint checkpoints the WAL before closing
- **Graceful restart** — the admin server uses `/shutdown` rather than force-killing the process

## Credits

- Black hole raytracer based on [pyokosmeme/black-hole](https://github.com/pyokosmeme/black-hole)
- OODA harness inspired by [metacog](https://github.com/inanna-malick/metacog) and [popup-mcp](https://tidepool.leaflet.pub/3mcbegnuf2k2i)
