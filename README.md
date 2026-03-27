<p align="center">
  <img src="static/img/banner.png" alt="Ex Astris Umbra">
</p>

# Ex Astris Umbra: A Loom Interface

A multi-modal conversation interface with tree-based branching, local and cloud AI backends, tool-calling agents, and a WebGL black hole.

![Python](https://img.shields.io/badge/python-3.12+-blue) ![License](https://img.shields.io/badge/license-MIT-green)

## Three modes, one loom

The Loom weaves conversations across three modes — pick the thread that fits the task.

### Weave — structured roleplay and creative writing

Character cards, personas, lore files, style nudges, and incremental summarization. A full RP harness built on local Ollama models with context window management that scales beyond what the model natively supports.

- Character system with personality, scenario, greeting, and example messages
- Personas and lore for richer world-building context
- Style nudge rotation and repetition detection
- Thinking model support (`<think>` stripping, content token counting)
- Incremental context summarization via Gemma 3 1B on CPU
- **OODA Harness** (optional) — cognitive scaffolding for better RP quality:
  - Two-pass generation: model observes, orients, decides, then acts
  - Persistent state cards (character state, scene state, lore) updated each turn
  - State reads/updates visible as collapsible tool blocks in the conversation
  - Inspired by [metacog](https://github.com/inanna-malick/metacog) and [popup-mcp](https://tidepool.leaflet.pub/3mcbegnuf2k2i)

### Local — Claude Code powered by any Ollama model

The full Claude Code harness running on a local model via [`ollama launch claude`](https://docs.ollama.com/integrations/claude-code). Same tools, same permissions, same UI — just running on your hardware.

- Full Claude Code tool suite (Bash, Read, Write, Edit, Grep, WebSearch, etc.)
- Permission prompts proxied through the browser UI
- Generated image display inline in responses
- Works with any Ollama model with sufficient context (64k+ recommended)

### Claude — Claude Code in the browser

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

## Common features

All three modes share the same conversation infrastructure:

- **Tree-based conversations** — Every message is a node. Branch at any point, explore alternate paths, switch between branches. Nothing is lost. Branch names use Unicode Greek letters (`2α.4β.6`).
- **Fork and branch** — Fork from any message to create a new conversation. Regenerate creates a sibling branch, preserving the original. Edit any message to create a new branch — including the root.
- **Ghost nodes** — Active generations appear as pulsing nodes on the tree in real time, so you always know where a response is growing.
- **Child branch hints** — When viewing a message with responses on other branches, clickable hints show where they are.
- **Tree visualization** — Interactive pan/zoom canvas with horizontal or vertical layout toggle (persists across reloads).
- **Import / export** — Characters, personas, lore (.md) and conversations (.json).
- **Streaming generation** — Real-time token streaming over WebSocket with live token rate and tool success/error indicators.
- **Background generation** — Navigate away mid-generation, come back later. Responses are saved progressively and survive reconnects, tab switches, and server restarts.
- **Browser notifications** — Get notified when a response completes while the tab is in the background.
- **Image detection** — Images referenced in responses are detected and displayed inline with filename captions and click-to-preview.
- **Clipboard paste** — Ctrl+V to paste images directly into the chat.
- **Message queuing** — Send your next message while the model is still responding.
- **HTTPS / Tailscale** — Serves over HTTPS with auto-detected SSL certs for secure access across your network.
- **WebGL black hole** — Schwarzschild raytracer background with procedural galaxy texture and glassmorphism UI.

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

For Claude mode, ensure the [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) is installed and on PATH with an active API key.

For Local mode, ensure [Ollama](https://ollama.com) is installed with a model pulled (e.g. `ollama pull qwen3.5:9b`).

## Project structure

```
server.py              — FastAPI server, WebSocket streaming, REST endpoints
database.py            — SQLite schema, message tree CRUD, branch management
config.py              — Configuration (model, context budget, SSL, generation params)
prompt_engine.py       — System prompt assembly, repetition detection, style nudges
context_manager.py     — Token counting, rolling summary, context window management
ooda_harness.py        — OODA loop: XML parser, state executors, prompt builder
character_loader.py    — Parse/save character, persona, and lore .md files
ollama_client.py       — Ollama API client (chat streaming, image description)
claude_client.py       — Claude Code CLI subprocess wrapper, NDJSON stream parser
cc_permission_hook.py  — PreToolUse hook script for browser-based permission prompts
local_summary.py       — Gemma 3 1B via llama-cpp-python for CPU summarization

static/
  index.html           — Single-page app shell
  app.js               — State management, home view, character/persona/lore CRUD
  chat.js              — WebSocket chat, message rendering, streaming, branching
  tree.js              — Interactive tree visualization (pan/zoom/expand)
  style.css            — Acidburn aesthetic (glassmorphism, cyan/purple palette)
  blackhole.js         — Schwarzschild raytracer (pre-compiled GLSL)
  acidburn-galaxy.js   — Procedural galaxy texture generator

characters/            — Character definition files (.md)
personas/              — User persona files (.md)
lore/                  — Lore/history context files (.md)
certs/                 — SSL certificates (auto-detected, gitignored)
```

## Configuration

Settings are adjustable from the UI (gear icon) or by editing `config.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| `ollama_host` | `http://localhost:11434` | Ollama server address |
| `ollama_model` | `qwen3.5:9b` | Model for generation |
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

## Credits

- Black hole raytracer based on [pyokosmeme/black-hole](https://github.com/pyokosmeme/black-hole)
- Summarization via [Gemma 3 1B IT](https://huggingface.co/google/gemma-3-1b-it) (abliterated Q4_K_M quantization)
