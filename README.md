# Loom

A local RP (roleplay) harness with tree-based conversation branching, a WebGL black hole background, and Ollama-powered text generation.

![Python](https://img.shields.io/badge/python-3.12+-blue) ![License](https://img.shields.io/badge/license-MIT-green)

## What it does

- **Tree-based conversations** — Every message is a node in a tree. Branch at any point, explore alternate paths, switch between branches. Full conversation history is never lost.
- **Character system** — Define characters as Markdown files with personality, scenario, greeting, and example messages. Create and edit characters from the UI.
- **Personas and lore** — Attach a user persona and lore/history files to conversations for richer context.
- **Incremental context summarization** — As conversations grow past the context budget (default 32K tokens), older messages are summarized in small batches by a local Gemma 3 1B model running on CPU. Recent messages stay verbatim. The LLM never sees stale or truncated context.
- **WebGL black hole** — A scientifically accurate Schwarzschild raytracer runs as the UI background, with a procedural cyberpunk galaxy texture, glassmorphism panels, and scanline overlay.
- **Streaming generation** — Real-time token streaming over WebSocket.
- **Image support** — Attach images to messages; they're described by the vision model and included in context.

## Requirements

- **Python 3.12+**
- **Ollama** running and accessible (default: `http://100.64.0.1:11434`)
- A model pulled in Ollama (default: `qwen3.5:9b`)
- ~1GB disk for the Gemma 3 1B summarizer (downloaded automatically on first run)

## Setup

```bash
# Clone
git clone https://github.com/lastnpcalex/loom.git
cd loom

# Install dependencies
pip install -r requirements.txt

# Run
python server.py
```

Open `http://localhost:3000` in your browser.

On first launch, the server downloads the Gemma 3 1B GGUF model (~806MB) for local CPU summarization. This happens in the background and doesn't block startup.

## Project structure

```
server.py              — FastAPI server, WebSocket streaming, REST endpoints
database.py            — SQLite schema, message tree CRUD, branch management
config.py              — All configuration (model, context budget, generation params)
prompt_engine.py       — System prompt assembly, repetition detection, style nudges
context_manager.py     — Token counting, rolling summary, context window management
character_loader.py    — Parse/save character, persona, and lore .md files
ollama_client.py       — Ollama API client (chat streaming, image description)
local_summary.py       — Gemma 3 1B via llama-cpp-python for CPU summarization

static/
  index.html           — Single-page app shell
  app.js               — State management, home view, character CRUD
  chat.js              — WebSocket chat, message rendering, streaming
  tree.js              — Interactive tree visualization (pan/zoom/expand)
  style.css            — Acidburn aesthetic (glassmorphism, cyan/purple palette)
  blackhole.js         — Schwarzschild raytracer (pre-compiled GLSL)
  acidburn-galaxy.js   — Procedural galaxy texture generator

characters/            — Character definition files (.md)
personas/              — User persona files (.md)
lore/                  — Lore/history context files (.md)
```

## Configuration

Settings are adjustable from the UI (gear icon) or by editing `config.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| `ollama_host` | `http://100.64.0.1:11434` | Ollama server address |
| `ollama_model` | `qwen3.5:9b` | Model for generation |
| `max_context_tokens` | `32768` | Context window budget |
| `verbatim_window` | `6` | Recent messages kept verbatim |
| `temperature` | `0.8` | Generation temperature |
| `top_p` | `0.9` | Nucleus sampling |
| `max_tokens` | `1536` | Max generation length |
| `repeat_penalty` | `1.08` | Repetition penalty |

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

## Credits

- Black hole raytracer based on [pyokosmeme/black-hole](https://github.com/pyokosmeme/black-hole)
- Summarization via [Gemma 3 1B IT](https://huggingface.co/google/gemma-3-1b-it) (abliterated Q4_K_M quantization)
