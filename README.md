<p align="center">
  <img src="static/img/banner.png" alt="Ex Astris Umbra">
</p>

# Ex Astris Umbra: A Loom Interface

![Python](https://img.shields.io/badge/python-3.12+-blue) ![License](https://img.shields.io/badge/license-MIT-green)

## What is a loom?

An LLM loom treats every conversation as a **tree, not a thread**. Each message is a node. At any point you can branch — regenerate, edit, fork — and explore alternate paths without losing the originals. The metaphor comes from weaving: every response is a thread, and the loom holds them all in tension so you can compare, backtrack, and choose.

This matters because LLM output is non-deterministic. The same prompt can produce a brilliant answer on one roll and a mediocre one on the next. A linear chat hides that variance — you see one path and lose the rest. A loom preserves them all. Regenerate five times, keep the best, branch from the second-best later. Edit a message from ten turns ago and watch the conversation diverge. The tree is the conversation's real shape; a single thread is just one path through it.

A Shadow Loom applies this to three backends — Anthropic's Claude API, local Ollama models, and Claude Code as a subprocess — with a shared branching infrastructure, persistent storage, and full-text search across everything.

## Search

Every message across every conversation is searchable from the home page. Type a query, get highlighted snippet results grouped by conversation, click to jump directly to the matching message on its branch.

This is particularly useful for **Claude Code sessions**. Loom mode runs CC as a subprocess and stores every tool call, every thinking block, every response in SQLite. That means you can search across months of CC sessions — find that one-off bash command from two weeks ago, locate the conversation where you debugged that migration, pull up every time Claude touched a specific file. It's an indexed, searchable archive of your entire CC history that you can branch from at any point.

There's also **per-conversation search** and **tree search** (find and navigate between matching nodes on the visual tree).

## Three modes, one loom

The Loom weaves conversations across three modes — pick the thread that fits the task.

### Weave — structured roleplay and creative writing

Character cards, personas, lore files, style nudges, and incremental summarization. A full RP harness built on local Ollama models with context window management that scales beyond what the model natively supports.

- Character system with personality, appearance, goals, relationships, scenario, greeting, and example messages
- Personas (player characters) and lore for richer world-building context
- Style nudge selection and repetition detection
- Thinking model support (`<think>` stripping, content token counting)
- Incremental context summarization via Gemma 3 1B on CPU
- Multi-branch generation — generate 1-5 parallel responses per turn, pick the best
- Tree-based branching — regenerate, edit, or fork at any point in the conversation

#### OODA Harness

The OODA (Observe-Orient-Decide-Act) harness is cognitive scaffolding that guides the model through a structured reasoning loop before writing each response. Inspired by [metacog](https://github.com/inanna-malick/metacog) (tools as cognitive scaffolding — LLMs treat tool results as ground truth) and [popup-mcp](https://tidepool.leaflet.pub/3mcbegnuf2k2i) (amortize latency into fewer, richer passes).

**The loop:** Before generating RP prose, the model emits a structured `<ooda>` block. The server parses this block, executes the state operations against the database, and returns the results. The model then writes its prose grounded in fresh state reads rather than stale context.

1. **Observe** — read the current state of characters, scenes, and relationships from the database
2. **Orient** — reason about what changed, how characters would react, what the scene demands
3. **Decide** — plan the response: what happens, who speaks, what shifts
4. **Act** — execute state updates (mood changes, location shifts, relationship evolution) and write the prose

Each step appears as a collapsible tool block in the conversation, so you can see exactly what the model observed, how it reasoned, and what state it changed.

**State cards** are persistent, structured data that track the evolving state of the RP:

- **Character State** — personality, appearance, current mood, goals, relationships, physical situation
- **Scene State** — location, time, atmosphere, present characters, recent events
- **Persona State** — the player's character (description, appearance, goals)
- **Lore** — read-only background information referenced when relevant

**Three-tier state hierarchy:**

1. **Tier 1 (Character Global)** — baseline state cards defined on the character itself. Editable from the home page via the state button. These are the template that gets copied when a character enters a conversation.
2. **Tier 2 (Conversation)** — copied from Tier 1 when OODA is enabled. Represents the starting state for the conversation. Stays pristine as the base.
3. **Tier 3 (Branch Deltas)** — state changes are saved as deltas on each assistant message, not applied to the base. Different branches see different state. When you navigate a branch, the effective state is reconstructed: base cards + deltas along the branch path.

State cards are editable inline — click any field value to edit, changes auto-save. The model reads them on the next turn, so manual edits steer the story.

### Braid — Claude Code powered by local models

The full Claude Code harness running on a local model via [`ollama launch claude`](https://docs.ollama.com/integrations/claude-code). Same tools, same permissions, same UI — just running on your hardware.

- Full Claude Code tool suite (Bash, Read, Write, Edit, Grep, etc.)
- Web search and page fetch via local DuckDuckGo/trafilatura MCP server (see setup below)
- Permission prompts proxied through the browser UI
- Generated image display inline in responses
- Works with any Ollama model with sufficient context (64k+ recommended)

### Loom — Claude Code in the browser

Connects to the [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) as a subprocess. Full access to Claude's tool suite with streaming responses, thinking blocks, and permission proxying.

- Tool call blocks with expandable input/output and success/error indicators
- Edit tool diff rendering (red/green inline diffs)
- Collapsible extended thinking display
- Permission proxying — tool approvals appear in the browser UI
- Model selection (Sonnet, Opus, Haiku) and thinking effort control — changeable mid-conversation
- Plan/Act mode toggle — switch between planning and execution modes
- Immutable session snapshots — every turn forks the CC session, enabling clean branching at any point
- Progressive draft saving — generation survives navigation, reconnects, and server restarts
- Per-turn and cumulative cost tracking
- Image attachments via the Read tool or clipboard paste (Ctrl+V)

#### Ollama model switching

Loom and Braid conversations can switch between Anthropic and local Ollama models mid-conversation. The model dropdown shows both Anthropic models (Sonnet, Opus, Haiku) and available Ollama models (currently filtered to the qwen3.5 family). When an Ollama model is selected, the effort selector hides (local models don't support Anthropic's thinking effort parameter).

On provider switch, the session resume is skipped for one turn to avoid thinking block signature conflicts — the Anthropic API cryptographically signs thinking blocks, and blocks from local models don't carry valid signatures. The conversation falls back to a database history rebuild for that single transition turn, then resumes normally.

> **Note:** `AskUserQuestion` is disabled in Loom's CC modes. CC's headless `-p` mode has no mechanism to send user responses back to an active `AskUserQuestion` tool call — stdin is closed after the initial prompt. This is an [open feature request](https://github.com/anthropics/claude-code/issues/16712) in Claude Code. When CC adds support for `--input-format stream-json` responses to pending tool calls, Loom can re-enable interactive questions. Until then, CC proceeds with its best judgment instead of asking.

## Common features

All three modes share the same conversation infrastructure:

- **Tree-based conversations** — Every message is a node. Branch at any point, explore alternate paths, switch between branches. Nothing is lost. Branch names use Unicode Greek letters (`2a.4b.6`).
- **Fork and branch** — Fork from any message to create a new conversation. Regenerate creates a sibling branch, preserving the original. Edit any message to create a new branch — including the root.
- **Ghost nodes** — Active generations appear as pulsing nodes on the tree in real time, so you always know where a response is growing.
- **Child branch hints** — When viewing a message with responses on other branches, clickable hints show where they are.
- **Tree visualization** — Interactive pan/zoom canvas with horizontal or vertical layout toggle (persists across reloads).
- **Bookmarks** — Bookmark any message or branch for quick access later.
- **Conversation filtering** — Filter conversations by mode (All / Weave / Braid / Loom) on the home page.
- **Import / export** — Characters, personas, lore (.md) and conversations (.json).
- **Streaming generation** — Real-time token streaming over WebSocket with live token rate and tool success/error indicators.
- **Background generation** — Navigate away mid-generation, come back later. Responses are saved progressively and survive reconnects, tab switches, and server restarts.
- **Notifications** — Bell icon with dropdown for completed generations and permission requests. Browser push notifications when the tab is in the background (plan completions, stream completions on followed conversations).
- **Image detection** — Images referenced in responses are detected and displayed inline with filename captions and click-to-preview.
- **Clipboard paste** — Ctrl+V to paste images directly into the chat.
- **Message queuing** — Send your next message while the model is still responding.
- **Per-tab state** — Each browser tab remembers its own conversation and view, surviving refreshes without cross-tab interference.
- **HTTPS / Tailscale** — Serves on `0.0.0.0` over HTTPS with auto-detected SSL certs for secure access across your Tailscale network.
- **WebGL black hole** — Schwarzschild raytracer background with procedural galaxy texture and glassmorphism UI.

## Session management (CC modes)

Loom and Braid use Claude Code's session system for efficient multi-turn conversations:

- **Fork-every-turn** — Every generation uses `--resume <parent_session> --fork-session`. Each assistant message gets its own immutable session snapshot. This means any message is forkable: branching, editing, and regenerating all work because every turn is its own snapshot.
- **Progressive drafts** — A draft message is created in the database immediately when generation starts, updated with content blocks as tools execute, and finalized on stream end. Navigate away and come back — the draft is still there.
- **History rebuild fallback** — When no session exists to resume (first message, or after a provider switch), the full conversation history is rebuilt from the database and sent as a single prompt. Tool call inputs and outputs are included (truncated to 2000 characters each).

## Admin server

A lightweight admin dashboard (`admin_server.py`) runs on port 3002 and provides:

- Status monitoring for all Loom instances (main on 3000, test on 3001)
- Graceful shutdown, start, and restart actions per instance
- Auto-refreshing dashboard at `http://localhost:3002`

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

For Loom mode, ensure the [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) is installed and on PATH with an active API key.

For Braid/Weave modes, ensure [Ollama](https://ollama.com) is installed with a model pulled (e.g. `ollama pull qwen3.5:9b`).

For web search in Braid/Loom with local models, register the bundled MCP server once after cloning:

```bash
claude mcp add --scope user --transport stdio web-tools -- python /absolute/path/to/mcp_web_tools.py
```

This gives local models access to `web_search` (DuckDuckGo) and `web_fetch` (trafilatura) as CC tools. The MCP server is launched automatically by Claude Code when needed — no separate process to manage.

Optional: start the admin server for instance management:

```bash
python admin_server.py  # dashboard on http://localhost:3002
```

## Project structure

```
server.py              -- FastAPI server, WebSocket streaming, REST endpoints
database.py            -- SQLite schema, message tree CRUD, branch management
config.py              -- Configuration (model, context budget, SSL, generation params)
prompt_engine.py       -- System prompt assembly, repetition detection, style nudges
context_manager.py     -- Token counting, rolling summary, context window management
ooda_harness.py        -- OODA loop: XML parser, state executors, prompt builder
character_loader.py    -- Parse/save character, persona, and lore .md files
ollama_client.py       -- Ollama API client (chat streaming, image description)
claude_client.py       -- Claude Code CLI subprocess wrapper, NDJSON stream parser
cc_permission_hook.py  -- PreToolUse hook script for browser-based permission prompts
mcp_web_tools.py       -- MCP stdio server: web_search (DuckDuckGo) + web_fetch (trafilatura) for local models
admin_server.py        -- Admin dashboard for managing Loom instances
local_summary.py       -- Gemma 3 1B via llama-cpp-python for CPU summarization

static/
  index.html           -- Single-page app shell
  app.js               -- State management, home view, character/persona/lore CRUD
  chat.js              -- WebSocket chat, message rendering, streaming, branching
  tree.js              -- Interactive tree visualization (pan/zoom/expand)
  style.css            -- Acidburn aesthetic (glassmorphism, cyan/purple palette)
  blackhole.js         -- Schwarzschild raytracer (pre-compiled GLSL)
  acidburn-galaxy.js   -- Procedural galaxy texture generator

characters/            -- Character definition files (.md)
personas/              -- User persona files (.md)
lore/                  -- Lore/history context files (.md)
certs/                 -- SSL certificates (auto-detected, gitignored)
```

## Configuration

Settings are adjustable from the UI (gear icon) or by editing `config.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| `ollama_host` | `http://localhost:11434` | Ollama server address |
| `ollama_model` | `qwen3.5:9b` | Default model for Weave generation |
| `max_context_tokens` | `32768` | Context window budget |
| `verbatim_window` | `6` | Recent messages kept verbatim |
| `temperature` | `0.8` | Generation temperature |
| `top_p` | `0.9` | Nucleus sampling |
| `max_tokens` | `1024` | Max generation length |
| `repeat_penalty` | `1.08` | Repetition penalty |
| `ssl_certfile` | `certs/cert.pem` | SSL certificate path |
| `ssl_keyfile` | `certs/key.pem` | SSL key path |

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

- **SQLite WAL mode** — the database uses Write-Ahead Logging for crash resilience
- **WAL checkpoint on shutdown** — the `/shutdown` endpoint checkpoints the WAL before closing, ensuring all data is flushed to the main database file
- **Graceful restart** — the admin server and restart script use the `/shutdown` endpoint rather than force-killing the process

## Credits

- Black hole raytracer based on [pyokosmeme/black-hole](https://github.com/pyokosmeme/black-hole)
- Summarization via [Gemma 3 1B IT](https://huggingface.co/google/gemma-3-1b-it) (abliterated Q4_K_M quantization)
- OODA harness inspired by [metacog](https://github.com/inanna-malick/metacog) and [popup-mcp](https://tidepool.leaflet.pub/3mcbegnuf2k2i)
