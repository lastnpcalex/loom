# AGENTS.md — Codebase Map

> Pointers, not prose. Read this file plus two or three of the files it references to orient in under five minutes.

## What This Project Is

A Shadow Loom is a self-hosted web interface for branching AI conversations across multiple LLM providers (Claude Code, ChatGPT Codex, Antigravity/Gemini, local llama.cpp, Hermes Agent). Every conversation is a tree — branch, fork, regenerate, and search across all paths without losing anything. Providers are interchangeable shuttles on the same branching infrastructure: one database, one tree UI, one permission system.

## Top-Level Entry Points

- `server.py` — FastAPI main server (port 3000): WebSocket streaming, REST endpoints, all generation pipelines
- `admin_server.py` — Admin dashboard server (port 3002): instance management, llama-server lifecycle, ttyd terminal, cron
- `static/index.html` — SPA shell; home view, chat view, tree view routing
- `static/app.js` — State management, home view, character/persona/lore CRUD, settings panel
- `static/chat.js` — WebSocket chat client, message rendering, streaming, branching, image handling
- `static/tree.js` — Interactive tree visualization (pan/zoom, drag, Greek-letter branch naming)
- `static/style.css` — Glassmorphism CSS, cyan/purple palette, responsive layout
- `static/blackhole.js` — Pre-compiled Schwarzschild raytracer WebGL background
- `static/canvas-sdk.js` — Canvas postMessage bridge SDK for interactive canvas
- `mcp_servers/nrol_ao/` — NROL-AO MCP server: typed evidence transitions, governance, news scans
- `cc_permission_hook.py` — PreToolUse hook: bridges CC/agy/Codex tool permissions through browser UI
- `pytest` — Test suite; entry via `C:\Python314\python.exe -m pytest` (see `pyproject.toml`)

## Directory Map

### Root — Core Server

(also see **Top-Level Entry Points** for `server.py` and `admin_server.py`)

`config.py` — Configuration dataclass; env vars, config.json persistence, `_PERSISTED_KEYS`
`database.py` — SQLite schema; conversations, messages, summaries, branches, state tables
`prompt_engine.py` — System prompt assembly; style nudges, repetition detection, `assemble_prompt()`
`context_manager.py` — Token counting; context window management, incremental summarization routing
`local_llm.py` — Dispatcher for local-model backend (Weave/OODA); routes to llama_client
`local_summary.py` — Local Gemma 3 1B summarizer via llama-cpp-python; CPU-only, auto-unload
`local_tools.py` — Tool definitions for local-mode agents; read_file, write_file, list_dir
`character_loader.py` — Parse/save character, persona, and lore .md files with YAML frontmatter
`model_context.py` — Model context-window table + handoff gate; 1M detection, per-provider thresholds
`loom_agent_prompt.py` — Shared Loom agent contract loader; `prepend_loom_agent_context()`, prompt merging
`loom_agent.md` — Plaintext Loom agent contract (read by loom_agent_prompt.py)
`skill_scanner.py` — Scan Claude Code skills, built-in commands, user skills for slash-command autocomplete
`canvas_slug.py` — Human-readable slug generator for canvas URLs (adjective-noun-suffix pattern)
`ooda_harness.py` — OODA loop harness for Weave RP; XML parser, state executors, two-pass generation
`backstage.md` — Backstage feature description or design notes

### Root — Provider Client Adapters

`claude_client.py` — Claude Code CLI subprocess wrapper; NDJSON stream parser, fork-every-turn sessions; also the umans launch path (`use_umans=True`, same CLI at `api.code.umans.ai`)
`codex_client.py` — ChatGPT Codex app-server subprocess wrapper; JSONL protocol, NROL MCP config
`gemini_client.py` — Antigravity (agy) CLI subprocess wrapper; plain text mode, log error scanning
`hermes_client.py` — Hermes Agent ACP subprocess wrapper; stdio JSON-RPC over `hermes acp`
`llama_client.py` — llama-server client; OpenAI-compatible /v1/chat/completions, vision, model resolution
`ollama_client.py` — Staged for deletion (`D`); legacy Ollama adapter; **do not re-enable**
`vllm_client.py` — Staged for deletion (`D`); legacy vLLM adapter; **do not re-enable**

### Root — MCP Servers (stdio)

`mcp_loom_workspace.py` — MCP workspace server; file edits, shell commands, sensitive reads via Loom HTTP
`mcp_loom_actions.py` — Deprecated stub; raises RuntimeError (replaced by mcp_loom_workspace)
`mcp_loom_file_edits.py` — Deprecated stub; raises RuntimeError (replaced by mcp_loom_workspace)
`mcp_state_cards.py` — MCP server for Backstage agent; state-card CRUD scoped to parent conversation
`mcp_web_tools.py` — MCP stdio server; web_search (DuckDuckGo) + web_fetch (trafilatura) for local models

### Root — Config and Data

`models_config.json` — Per-model llama-server tuning; context size, GPU layers, KV-cache quantization, flash-attn
`pyproject.toml` — Pytest config; `asyncio_mode = "auto"`, testpaths
`requirements.txt` — Python dependencies (fastapi, uvicorn, aiosqlite, httpx, llama-cpp-python, mcp, etc.)

### Frontend — `static/`

(also see **Top-Level Entry Points** for `static/index.html`, `static/app.js`, `static/chat.js`, `static/tree.js`, `static/style.css`, `static/blackhole.js`, `static/canvas-sdk.js`)

`static/admin/index.html` — Admin dashboard SPA; sidebar nav, view panels, toast notifications
`static/admin/admin.js` — Admin client; instance CRUD, server controls, terminal embed, cron management
`static/admin/admin.css` — Admin dashboard styles; dark sidebar, card layout
`static/img/` — Static images; banner, favicon, background textures (stars, milkyway, spectra)

### NROL-AO MCP — `mcp_servers/nrol_ao/`

`mcp_servers/nrol_ao/__init__.py` — Package marker
`mcp_servers/nrol_ao/server.py` — MCP facade; all `@mcp.tool()` registrations + wrappers (typed transitions, proposals, news scans, debate, design/activate/resolve, shadow, future-cast, source-trust, triage, social-brier)
`mcp_servers/nrol_ao/proposals.py` — SQLite article + proposal store; dedup, lifecycle (submit → propose → commit)
`mcp_servers/nrol_ao/activity.py` — Activity ledger; job tracking, digest writing, scan run persistence
`mcp_servers/nrol_ao/llama.py` — Local llama client for NROL matcher; dispatches through Loom's llama-server
`mcp_servers/nrol_ao/future_cast.py` — Dry-run hypothetical-event analysis; deep-clone + bayesian_update (no save), red-team critique, JSONL save store (list/get/save/withdraw)
`mcp_servers/nrol_ao/resolution.py` — Topic resolution: shadow-trajectory reconstruction, two-lane Brier (shadow vs committed), red-team after-action review packet
`mcp_servers/nrol_ao/source_trust.py` — Read-only views over the LIVE source-trust stores (framework/source_db.py etc.); status/profile/validate/domain-patterns
`mcp_servers/nrol_ao/triage_log.py` — Optional saved-triage audit ledger (loom/triage_log/); list/read; a logged triage is not evidence
`mcp_servers/nrol_ao/social_brier.py` — Greenfield per-handle forecast calibration; log forecasts, Brier-score at resolution via compute_brier_score
`mcp_servers/nrol_ao/README.md` — NROL-AO MCP usage docs; configure, register, fail-closed commits, full grouped tool list
`mcp_servers/nrol_ao/OPERATOR.md` — Operator role + lifecycle docs; shadow-as-guide, design → review → activate → resolve flow, known footguns
`mcp_servers/nrol_ao/ROADMAP.md` — NROL-AO development roadmap
`mcp_servers/nrol_ao/MATH_AUDIT_2026-06-09.md` — Math audit artifact
`mcp_servers/nrol_ao/MERIDIA_AAR_2026-06-12.md` — Post-mortem artifact

### Tests — `tests/`

`tests/__init__.py` — Package marker
`tests/conftest.py` — Shared fixtures; `tmp_database` (temp SQLite), basetemp redirect, autouse
`tests/test_anthropic_model_list.py` — Anthropic model list parsing tests
`tests/test_api.py` — REST API endpoint tests
`tests/test_codex_permissions.py` — Codex permission mode and approval mapping tests
`tests/test_context_generation.py` — Context window generation tests
`tests/test_database.py` — SQLite CRUD and tree operations tests
`tests/test_hermes_smoke.py` — Hermes ACP smoke tests
`tests/test_local_mode.py` — Local mode generation tests
`tests/test_loom_agent_prompt.py` — Loom agent contract injection tests
`tests/test_nrol_ao_mcp.py` — NROL-AO MCP server tests
`tests/test_ooda_harness.py` — OODA harness parsing and execution tests
`tests/test_operator_parity.py` — Operator parity tests
`tests/test_plan_approval_hook.py` — Plan approval hook tests
`tests/test_synthetic_replay.py` — Synthetic corpus replay tests
`tests/fixtures/synthetic_topic/` — NROL-AO synthetic test corpus; topic.json, timeline, corpus JSON files
`tests/synthetic/` — Synthetic test harness; `generate_corpus.py`, `replay.py`, `score.py`, `REPORT.md`

### Content Directories

`characters/` — Character definition files (.md with YAML frontmatter: name, tags, personality, scenario, greeting)
`personas/` — User persona files (.md); player characters for RP
`lore/` — Lore/history context files (.md); world-building references
`templates/` — Chat template Jinja files for local models (e.g., qwen3.6-froggeric)
`tools/` — Dev/probe scripts (e.g., probe_hermes_acp.py for Hermes ACP diagnostics)
`canvas/` — Canvas workspace (generated per conversation; .gitignore inside)
`character-creator/` — Character creator workspace; CLAUDE.md, .gitkeep placeholder

### Utility Scripts (tracked)

`start_test_server.bat` — Windows batch to start test server
`stop_test_server.py` — Stop test server script
`test_ooda_live.py` — Live OODA test harness

## Subsystem Locator

| Concern | Where it lives |
|---------|----------------|
| LLM client adapters | `claude_client.py`, `codex_client.py`, `gemini_client.py`, `hermes_client.py`, `llama_client.py` |
| Active generations / session state | `server.py` — module-level `_active_generations` dict, `_reap_orphan_generations()`, `/api/generations` endpoints |
| Model registry / discovery | `llama_client.py` — `list_local_models()` scans `config.llama_models_dir`; `models_config.json` for per-model tuning |
| Model context + handoff gate | `model_context.py` — `is_1m_anthropic()`, provider thresholds, `needs_handoff()` |
| Context window management | `context_manager.py` — token counting, rolling summaries via `local_summary.py` |
| System prompt assembly | `prompt_engine.py` — `build_system_prompt()`, `assemble_prompt()`, style nudges |
| Database (tree storage) | `database.py` — SQLite schema, conversations/messages/summaries/branches tables |
| Configuration | `config.py` — `Config` dataclass, `config.json` persistence, env var defaults |
| MCP workspace coordination | `mcp_loom_workspace.py` — read/write/edit/bash/sensitive-read tools via Loom HTTP |
| Backstage state cards | `mcp_state_cards.py` — conversation-scoped state-card CRUD for backstage agent |
| Web tools (local models) | `mcp_web_tools.py` — DuckDuckGo search + trafilatura fetch MCP stdio server |
| NROL-AO engine | `mcp_servers/nrol_ao/` — `server.py` (MCP facade), `proposals.py` (SQLite store), `activity.py` (ledger), `llama.py` (local matcher) |
| Admin panel (backend) | `admin_server.py` — FastAPI on port 3002; instances, servers, ttyd, cron, tools |
| Admin panel (frontend) | `static/admin/index.html`, `static/admin/admin.js`, `static/admin/admin.css` |
| Permission gating | `cc_permission_hook.py` — PreToolUse hook for CC/agy/Codex; browser UI bridge |
| Loom agent contract | `loom_agent.md` (plaintext), `loom_agent_prompt.py` (loader + merger) |
| OODA harness | `ooda_harness.py` — two-pass generation: observe-orient-decide-act before RP prose |
| Slash commands / skills | `skill_scanner.py` — `BUILTIN_COMMANDS`, `get_all_skills()` scans CC skills |
| Canvas | `canvas_slug.py` (slug gen), `server.py` routes, `static/canvas-sdk.js` (bridge), `static/tree.js` (meta-root nodes) |
| Character system | `character_loader.py` (parser), `static/app.js` (CRUD UI), `characters/`, `personas/`, `lore/` |
| Tests | `tests/` — `conftest.py` (temp DB fixture), provider tests, `test_nrol_ao_mcp.py`, synthetic corpus in `tests/fixtures/` |

## How to Run It Locally

```powershell
# Install dependencies
C:\Python314\python.exe -m pip install -r requirements.txt

# Start main server (HTTPS on :3000)
C:\Python314\python.exe server.py

# Start admin dashboard (HTTPS on :3002) — optional
C:\Python314\python.exe admin_server.py

# Run tests
C:\Python314\python.exe -m pytest

# Open browser at https://localhost:3000
```

**Prerequisites per mode:** Claude Code CLI on PATH (Loom), `llama-server` + GGUF (Braid/Weave), `codex` CLI (Codex), `agy` CLI (Antigravity), `LOOM_ENABLE_HERMES=1` (Hermes — see **Conventions** for env var list). See `README.md` for provider-specific setup and MCP registration.

## Conventions Worth Knowing

- **Python interpreter** is `C:\Python314\python.exe`; bare `python` may resolve to a dep-less python-manager install
- **Client adapter naming** is `<provider>_client.py` — each wraps one provider CLI or protocol
- **MCP servers** use `mcp.server.fastmcp.FastMCP()` factory; each has a unique server name (`loom-workspace`, `nrol-ao`, `loom-state-cards`, `web-tools`)
- **Config** lives in `config.json` (user-editable, gitignored) loaded by `config.py`; per-model tuning in `models_config.json` (tracked)
- **Database** is SQLite with WAL mode; `database.py` uses `aiosqlite` for async access
- **Frontend** has no build step — plain HTML/CSS/JS served static from `static/`
- **Test fixtures** use `conftest.py`'s `tmp_database` fixture (temp SQLite file per test)
- **Env vars**: `LOOM_PORT` (main server, default 3000), `ADMIN_PORT` (admin), `NROL_AO_REPO`, `LOOM_ENABLE_HERMES`, `LLAMA_HOST`, `LOOM_DB`, `LOOM_SSL_CERT`, `LOOM_SSL_KEY`
- **Branch naming** uses Unicode Greek letters (α, β, γ, …) with numeric suffixes; double/triple Greek for large trees
- **Session model** is fork-every-turn: each CC generation gets its own immutable session snapshot
- **Generation** survives restarts via progressive drafts — draft message created immediately, updated as tokens arrive
- **Deprecated MCP** files (`mcp_loom_actions.py`, `mcp_loom_file_edits.py`) raise `RuntimeError` — do not re-enable

## Needs Human Review

- `ollama_client.py` and `vllm_client.py` are staged for deletion (`D`); Ollama and vLLM are deprecated — llama-server is the only local LLM backend
- `start_test_server.bat` and `stop_test_server.py` are tracked but not referenced in README; purpose unclear
- `backstage.md` is tracked; may be a design doc or scratch — skip or add description
- `mcp_servers/nrol_ao/MATH_AUDIT_2026-06-09.md` and `MERIDIA_AAR_2026-06-12.md` — dated artifacts; keep or move to archive?
