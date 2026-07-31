# NROL-AO Engine Agent + Dream Anthropic Shim — Plan

Status: **planned, not implemented.** Written to disk 2026-07-06 per operator direction. Two tracks (A: engine-agent migration; B: Anthropic shim for Dream) that can converge.

---

## Context

The current NROL-AO deliberation pipeline is a hand-rolled reimplementation of what a tool-using agent provides natively: it serializes structured intent into prompt text, demands `<one sentence>` fields, and parses the model's text output back into structures with fragile line-based regex. Observed failures:

- "Deliberations" are three models each emitting a one-liner that restates the prior round — no actual engagement because there's nothing substantive to engage *with* (`REASON: <one sentence>` is enforced in the prompt builders at `framework/news_observation_pipeline.py:354, 423, 511`).
- The parsers (`_DECISION_BLOCK`, advocate/rebut/jury parsers) have a documented history of silently swallowing blocks (the END-terminator bug noted in `news_observation_pipeline.py` comments: "12 blocks emitted, 1 parsed").
- The DiffusionGemma nuspy/C++ adapter leaks `<|channel>thought<channel|>` into `content` on the text path, and the strip code (`mcp_servers/nrol_ao/llama.py:27-55`) fails on the empty-thought edge case (the `if extracted_reasoning:` guard at `llama.py:289` skips the strip when thought is empty).
- Line-serialization disease is systemic: article fetches, the evidence ledger, and triage all flatten structured data to text and parse it back.

**The fix is architectural:** stop serializing structured intent to text. Use typed tool calls as the deliberation primitive, so the schema is enforced by the tool-use protocol instead of recovered by regex.

Separately (Track B): Dream (DiffusionGemma) is an effective model for writing but has only been reachable via the OpenAI Chat Completions format, so Claude Code — which speaks the Anthropic Messages API — cannot use it as a provider. A small translation shim makes Dream available as a Claude Code model, which is a quality-of-life win for writing tasks and potentially unifies the engine-agent harness (see Track B §"Relationship to Track A").

## Gating probes — both PASS

**Unknown 1 (Dream tool-use support): PASS, verified live 2026-07-06.**
Probe against `http://127.0.0.1:8787/v1/chat/completions` with an OpenAI `tools` payload returned:
```
finish_reason: "tool_calls"
content: ""                          (empty — no thought-channel contamination)
tool_calls: [{id: "call-13", type: "function",
              function: {name: "get_weather", arguments: '{"location":"Paris"}'}}]
```
Key finding: **the `<|channel>thought` leak that plagues the text-generation path does NOT affect the tool-use path.** When the model emits a tool call, the adapter routes it cleanly into the structured `tool_calls` object and `content` stays empty. The entire channel-strip problem becomes irrelevant under the tool-call architecture.

**Unknown 2 (engine-agent harness: SDK vs custom loop): resolved by Track B.** Originally deferred. If the Anthropic shim (Track B) lands, the engine agent can be a Claude Code Agent SDK subagent pointed at the shim → Dream, and no custom Python agent loop is needed. If the shim is delayed, a minimal custom loop speaking OpenAI format directly to `:8787` is the fallback (Gemini's recommendation; ~100 lines). Track A is harness-agnostic until phase 2.

---

# Track A: Engine Agent Migration

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  OPERATOR NROL MCP  (thin, human-facing)             │
│  Tools: run_news_scan, resolve_topic, read_topic,   │
│  list_proposals, commit_match, publish_snapshot      │
│  NO direct LLM calls. NO prompt strings.            │
└───────────────┬─────────────────────────────────────┘
                │  launches per-scan
                ▼
┌─────────────────────────────────────────────────────┐
│  ENGINE AGENT  (Claude Code SDK subagent OR custom   │
│  Python loop; provider → Dream :8787 via shim if    │
│  Track B landed, else direct OpenAI format)          │
│  Tool surface: engine-side MCP tools (see §tools)   │
│  Spawns advocate / rebut / jury subagents           │
│  Tool-call trace = the audit record                │
└───────────────┬─────────────────────────────────────┘
                │  writes via tools
                ▼
┌─────────────────────────────────────────────────────┐
│  STATE LAYER  (UNCHANGED)                            │
│  topics/*.json, evidence log, source_db,            │
│  framework/pipeline.py Bayesian update engine        │
└─────────────────────────────────────────────────────┘
```

## Engine-side tool surface (the schema IS the spec)

Typed params enforce what the current regex tries to recover. `analysis`/`rationale` params are free strings — multi-paragraph, unconstrained.

**Reading:**
- `read_topic(slug) → {meta, hypotheses, posteriors}`
- `read_indicator_schema(slug) → [{id, tier, observable, likelihoods, direction}]`
- `read_recent_evidence(slug, limit) → [evidence]`
- `fetch_article(url) → {headline, source, text, published_at}` — replaces line-serialized fetch
- `run_search(slug, query) → [results]`

**Deliberation (typed verdicts replace the regex parsers):**
- `propose_advocate(article_id, verdict: enum, proposed_action: {kind: enum, indicator_id?, value?}, citation: string, analysis: string) → proposal_id`
- `propose_rebut(article_id, verdict: enum, objection_raised: bool, objection_details: string, corrected_action: {kind, indicator_id?, value?}, rebuttal_analysis: string) → rebuttal_id`
- `submit_jury(article_id, final_action: {kind: enum, parent_idx?, indicator_id?, value?}, jury_rationale: string) → verdict_id`

The `verdict` and `kind` enums are enforced by the tool schema. No `DUPLICATE_OF`-without-a-kind contradiction (the bug in the prior JSON-mode spec) — `parent_idx` is the discriminator on `final_action` when `kind` is `DUPLICATE_OF`.

**Action (existing FIRE/OBSERVE/PARK semantics as calls — wrap existing `framework/pipeline.py` update logic):**
- `fire_indicator(slug, indicator_id, evidence) → posterior_delta`
- `observe_indicator(slug, indicator_id, observed_value, evidence) → posterior_delta`
- `park_article(slug, article_id, reason)`
- `flag_schema_gap(slug, description, target_hypothesis?)`
- `flag_duplicate(article_id, parent_article_id)`

**Ledger:**
- `write_evidence(slug, ...) → evidence_id`
- `read_parked_queue(slug, limit)`

The Bayesian engine, topic JSON, evidence log, and `framework/pipeline.py` update logic are **unchanged**. These tools are thin wrappers over existing state-layer functions. What dies is everything above the state layer that currently hand-rolls prompts and parses text.

## Deliberation as subagents

1. Engine agent runs `run_search` + `fetch_article` per candidate (structured returns, no line parsing).
2. Spawns **Advocate subagent** with candidates + indicator schema as context. Advocate calls `propose_advocate` per article — full multi-paragraph `analysis` field.
3. Spawns **Rebut subagent** with advocate's structured proposals (full `analysis` fields) injected. Rebut calls `propose_rebut`.
4. Spawns **Jury subagent** with both prior rounds' structured output. Jury calls `submit_jury` per article.

Each subagent can read the indicator schema, check prior evidence, reason across multiple tool calls — actual deliberation. The jury sees the advocate's multi-paragraph `analysis` and the rebut's `objection_details` in full, passed as structured records, not collapsed to a sentence.

## What survives vs what's deleted

**Survives:** topic/evidence JSON, Bayesian engine, indicator schema definitions, source_db, `framework/pipeline.py` update logic, the Loom permission + governance commit gates.

**Deleted:** `mcp_servers/nrol_ao/llama.py:chat()` prompt machinery, all line-based parsers (`_DECISION_BLOCK`, advocate/rebut/jury parsers in `news_observation_pipeline.py`), `build_*_prompt` string builders, `run_matcher_with_llama` / `deliberate_candidates` text tools, the `<one sentence>` prompt constraints, the `<|channel>thought` strip code (irrelevant — tool calls have no thought channel).

**New:** engine agent definition + system prompt, engine-side MCP tool implementations (thin wrappers over existing state-layer functions), operator→engine bridge, tool-call-trace audit ledger format.

## Audit shape

Current: activity ledger records per-HTTP-call `{backend, model, finish_reason, output_chars, raw_text}`.

Engine agent: audit unit is the **tool-call trace** — the sequence of tool calls the agent and subagents made, with arguments and return values. Strictly more informative: you see *what the agent decided* (typed verdicts) and *why* (the analysis params), not just "446 chars of text came back, parsed to 3 blocks." Raw LLM completions can still be logged at the provider level for debugging; the audit ledger is the tool trace.

## Migration path (additive, never breaks the working scan)

The state layer and commit gates never change, so a bad engine-agent run cannot corrupt topic state that the current system couldn't also corrupt. Migration is reversible through phase 5.

1. **Build one engine-side tool** (`fetch_article`) + a minimal engine agent that uses it. Validate the tool-call round-trip end-to-end on Dream. (Probe already confirms this works; this step confirms it works through the chosen harness — SDK via Track B shim, or custom loop.)
2. **Decide harness layer** (Unknown 2): if Track B shim landed → Claude Code Agent SDK subagent pointed at shim; else → minimal custom Python agent loop speaking OpenAI format to `:8787`. Validate multi-turn tool use with a 2-call sequence.
3. **Add deliberation tools** (`propose_advocate`, `propose_rebut`, `submit_jury`). Run **advocate-only** as a subagent. Compare output quality vs the current one-liner advocate — confirm the `analysis` field carries actual multi-paragraph reasoning.
4. **Add rebut + jury subagents.** Wire to the existing commit gates (Loom approval, governance, evidence log) — the commit path is unchanged, the engine agent calls the same gates the current tools do.
5. **Operator MCP tools flip** from "build prompt + parse" to "launch engine agent + collect trace." Old tools coexist behind a flag until the new path is trusted. Then delete the prompt-build + regex-parse code.

---

# Track B: Anthropic Shim for Dream

## Purpose

Primary (operator-stated): **QoL improvement** — let Dream (DiffusionGemma) serve as a Claude Code model for writing tasks, by launching Claude Code with a custom provider URL pointing at the shim. Dream is an effective writing model but has only been reachable via OpenAI Chat Completions; Claude Code speaks the Anthropic Messages API, so the two cannot talk directly.

Secondary (architectural): **unifies the engine-agent harness** (Unknown 2). If the shim exists, the Track A engine agent can be a Claude Code Agent SDK subagent pointed at the shim → Dream, eliminating the need for a custom Python agent loop. The shim becomes the single integration point for both operator-facing Claude Code sessions and the NROL-AO engine agent.

Operator has acknowledged this is scope creep relative to the engine-agent migration, but wants it for the QoL benefit.

## Architecture

```
Claude Code (launched with ANTHROPIC_BASE_URL=http://127.0.0.1:SHIM_PORT)
    │  Anthropic Messages API (/v1/messages, SSE streaming, tool_use blocks)
    ▼
Anthropic↔OpenAI Shim  (small Python server: aiohttp/FastAPI)
    │  OpenAI Chat Completions (/v1/chat/completions, tool_calls)
    │  + thought-channel strip on text responses (fixes the empty-thought bug
    │    at the shim layer for ALL Claude Code text generation through Dream)
    ▼
Dream sidecar (:8787, DiffusionGemma, llama-diffusion-gemma-server.exe)
```

## Translation concerns

**Request (Anthropic Messages → OpenAI Chat Completions):**
- `messages` with content blocks (`text`, `image`, `tool_use`, `tool_result`) → OpenAI messages (content string / `tool_calls` / tool-role messages).
- Top-level `system` param → OpenAI system message (prepend).
- `tools` (Anthropic: `{name, description, input_schema}`) → OpenAI `tools` (`{type:"function", function:{name, description, parameters}}`).
- `max_tokens` (required in Anthropic) -> `max_tokens` unchanged. Dream's sidecar computes the needed canvases and clamps to context; do not add a hidden thought budget. Dream accepts `chat_template_kwargs.enable_thinking`, so callers can explicitly choose the Gemma4 thought-channel path or the no-thinking reference prompt.
- `temperature`, `top_p`, `stream` → passthrough.
- Model name: shim ignores incoming `model` and uses Dream's loaded model (`diffusiongemma-26b-a4b-it-nvfp4`), OR passes through. Simplest: always target Dream's model (only one is loaded).

**Response (OpenAI → Anthropic):**
- Non-streaming: `choices[0].message` → Anthropic `content` array with `text` and `tool_use` blocks.
- `finish_reason` mapping: `stop`→`end_turn`, `tool_calls`→`tool_use`, `length`→`max_tokens`, `content_filter`→`end_turn`.
- `usage` → Anthropic `usage` (`input_tokens`/`output_tokens`).
- **Thought-channel strip (text responses only):** when `content` is non-empty text, strip `<|channel>thought...<channel|>`. Reuse `_split_channel_scaffold` logic from `mcp_servers/nrol_ao/llama.py:27-55` (or `dream_client._split_channel_scaffold`), but **fire unconditionally when the regex matches** — do NOT replicate the `if extracted_reasoning:` guard that causes the empty-thought edge-case bug in `llama.py:289`. This fixes the strip bug at the shim layer for all Claude Code text generation through Dream. Tool-call responses need no strip (verified: `content` is empty on tool_calls).

**Streaming (OpenAI delta stream → Anthropic SSE):** the hard part. Anthropic requires a specific 6-event sequence:
1. `message_start` (message id, role, model, usage with input_tokens)
2. `content_block_start` (index, type `text` or `tool_use`; for tool_use, include id + name)
3. `content_block_delta` (`text_delta` for text, `input_json_delta` for tool arguments — OpenAI streams tool args in pieces via `delta.tool_calls[].function.arguments`)
4. `content_block_stop` (index)
5. `message_delta` (stop_reason, usage with output_tokens)
6. `message_stop`

OpenAI delta stream → map each `delta.content` chunk to a `content_block_delta`/`text_delta`; accumulate `delta.tool_calls[].function.arguments` pieces and emit as `input_json_delta`. Emit `content_block_start`/`stop` on transitions between text and tool_use blocks.

## Endpoints the shim must serve
- `POST /v1/messages` — main, Anthropic format, SSE streaming + non-streaming.
- `GET /v1/models` — Claude Code probes for available models on provider connect; return Dream's loaded model.
- `POST /v1/messages/count_tokens` — Anthropic token counting; stub with a rough estimate (len-based) or best-effort. Claude Code uses this for context budgeting; a rough estimate is acceptable.

## Claude Code launch wiring
When a "dream model" is selected in Loom (model selector), Loom launches `claude` with `ANTHROPIC_BASE_URL=http://127.0.0.1:SHIM_PORT` and `ANTHROPIC_API_KEY=dummy` (shim does not auth against a real key). Existing dream-model detection (`llama_client.py:_is_dream_model` / `_chat_host_for_model`, `llama_client.py:125-135`) already routes dream models — extend the claude-session spawn path to set the env vars when a dream model is detected. The shim sidecar is started by `admin_server.py` alongside the dream sidecar (extend the dream-start/dream-stop lifecycle at `admin_server.py:1192/1227` to also start/stop the shim).

## Relationship to Track A
- If Track B lands before Track A phase 2: the engine agent uses the Claude Code Agent SDK pointed at the shim. No custom Python loop needed. The shim is the single integration point for both operator Claude Code sessions and the NROL-AO engine agent.
- If Track A phase 2 lands first: the engine agent uses a minimal custom Python loop speaking OpenAI format directly to `:8787` (no shim). Track B then lands later as pure Claude Code QoL, and Track A can optionally migrate to the SDK-via-shim path afterward.
- The two tracks are decoupled and can proceed independently. Convergence is optional.

## Shim complexity & risk
- Bounded. The translation is well-defined with reference implementations (LiteLLM, claude-code-proxy projects do exactly this). A custom shim (~300-500 lines) lets us bake in the Dream thought-channel strip, which off-the-shelf proxies won't handle.
- Main risk: streaming SSE tool-use (`input_json_delta` piecewise assembly). Fiddly but well-specified. Mitigate with a non-streaming fallback path first (get correctness on non-streaming tool calls, then add streaming).
- The probe already proved Dream emits clean tool_calls on the OpenAI path, so the shim's downstream is verified.

---

# Out of scope: Gemini "Expose Endpoints for Modern Agentic Harnesses" spec

Evaluated; NOT included. Reasoning recorded so it isn't relitigated:

- **§2B (OpenAI `/v1/chat/completions` with tools): already done.** The gating probe proved Dream's C++ server returns clean `tool_calls` with empty `content` and no thought-channel contamination. Building it would rebuild what exists.
- **§2A (native Anthropic `/v1/messages` + SSE + `tool_use` blocks in the C++ server): rejected in favor of Track B shim.** A Python shim (~one file, ~300-500 lines) is cheaper than native C++ SSE in the forked llama.cpp + recompile + ongoing fork maintenance. Track B IS the Anthropic endpoint — just implemented as a shim, not in C++.
- **§1 (Images / multimodal): scope creep.** Gemini correctly notes the NVFP4 GGUF is missing the vision tower entirely — native vision (Path B) requires a different GGUF + C++ vision encoder integration, a separate project. Path A (visual-to-text fallback) is real but NROL-AO has no image inputs today (text articles, numeric/textual indicators). *Future caveat:* satellite imagery / AIS visual confirmation could be a tier-3 Hormuz evidence source someday — but that's a different spec, different GGUF, not this work.
- **§2C (Hermes ACP/HTTP): already wired.** Loom has Hermes-on-Dream at `server.py:6509-6874` (`_ensure_dream_hermes_home`). Not needed for the NROL-AO engine path.
- **§2D (WebSocket real-time / voice): no real-time need in NROL-AO.** Pure scope creep.

Bottom line: the Gemini spec is mostly already-done, duplicated by Track B, scope creep, or premature C++ work. Track A + Track B cover the real needs. If a future NROL-AO need (vision evidence, real-time) arises, evaluate it as its own spec against the actual need at that time.

---

# Verification (end-to-end)

**Track A:**
- Phase 1: `fetch_article` tool call round-trips through the engine agent on Dream, returns a structured article object (not a line of text).
- Phase 3: Advocate subagent's `propose_advocate` calls carry multi-paragraph `analysis` strings (verify char count >> the old one-sentence `REASON`), and `verdict`/`kind` enums are schema-valid (no malformed values possible).
- Phase 4: A full scan through the engine agent produces jury verdicts that move posteriors via the existing commit gates — same posterior delta math, no regression vs current scan output. Run a known article through both paths and compare the evidence logged.
- Phase 5: Operator MCP `run_news_scan` launches the engine agent, returns a tool-call trace as the audit record. Old `run_matcher_with_llama` / `deliberate_candidates` still work behind the flag for parity checks.

**Track B:**
- Shim serves `POST /v1/messages` (non-streaming) and returns a valid Anthropic response for a text prompt → Claude Code can complete against Dream.
- Tool-use round-trip: a `tools`-bearing Anthropic request returns a `tool_use` content block with valid `input` JSON, assembled from Dream's `tool_calls`.
- Streaming: SSE event sequence matches Anthropic's 6-event contract (`message_start` → `content_block_start` → `content_block_delta` → `content_block_stop` → `message_delta` → `message_stop`); a reference Claude Code client consumes it without error.
- Thought-channel strip: a text response from Dream arrives at Claude Code with no `<|channel>thought<channel|>` scaffold (the empty-thought edge case is handled — strip fires on regex match, unconditionally).
- `GET /v1/models` returns Dream's loaded model; Claude Code provider-connect probe succeeds.
- Loom launch: selecting a dream model spawns `claude` with `ANTHROPIC_BASE_URL` → shim, and a writing task completes against Dream.

---

# Files inspected (read-only, for grounding)
- `mcp_servers/nrol_ao/llama.py` — `chat()` (212), `resolve_backend()` (135), `_split_channel_scaffold()` (27-55, the strip that becomes irrelevant on the tool path and gets fixed-at-shim on the text path), `dream_host()`/`llama_host()` (76-106)
- `mcp_servers/nrol_ao/server.py` — `_run_debate()` (1035-1160), `run_news_scan` (3709+), model forwarding to `_run_debate` (3948)
- `framework/news_observation_pipeline.py` — `build_advocate_prompt`/`build_rebut_prompt`/`build_jury_prompt` (294-520), the line parsers, `<one sentence>` constraints at 354/423/511
- `dream_client.py` — async Dream sidecar client (`dream_chat` 185, `dream_chat_sync` 255, `_split_channel_scaffold` 44-72 canonical copy)
- `admin_server.py` — dream sidecar broker (958-1416), `dream-start`/`dream-stop` lifecycle (1192/1227), launch cmd with `-ngl 999` (1049-1073)
- `server.py` — `_handle_dream_generation` Hermes-on-Dream (6509-6874), `_dream_openai_base_url` (6513)
- `llama_client.py` — `_is_dream_model`/`_chat_host_for_model` (125-135) for dream-model detection
- `config.json` — `dream_host` (:8787), `dream_model` (diffusiongemma-26b-a4b-it-nvfp4), `dream_server_exe`
- Live probe of `http://127.0.0.1:8787/v1/chat/completions` with OpenAI `tools` payload (Unknown 1, PASS)
- `framework/pipeline.py`, `source_db.py`, `source_ledger.py`, `calibrate.py` — state layer (unchanged, wrapped not rewritten)
