# NROL-AO Architecture Update

**Status:** planned, not implemented. Canonical reference for the architecture update. Written 2026-07-06. Supersedes `ENGINE_AGENT_AND_DREAM_SHIM_PLAN.md` (folded in below) and the diagnostic report in `~/.claude/plans/i-need-a-report-indexed-sprout.md`.

This document is the single source of truth for: (1) what's broken in the current NROL-AO server, (2) the architectural decision and the probes that validate it, (3) the two implementation tracks and how they converge, (4) the alternatives that were evaluated and rejected, and (5) the end-to-end verification plan.

**Read the repo map (§0.5) before any implementation.** The engine and its framework live in a *different repo* than this document; getting this wrong wastes effort in the wrong place.

---

## 0.5 Repo map (read first)

The NROL-AO system spans **two repos**. This is the single biggest implementation risk and the most common way to waste effort.

| Repo | Path | Role |
|------|------|------|
| **MCP server repo** | `C:\Users\exast\OneDrive\Documents\Loom2\a-shadow-loom` | Where this document lives. The MCP server that operators call. Contains `mcp_servers/nrol_ao/server.py` (MCP tools), `mcp_servers/nrol_ao/llama.py` (sync LLM client), `dream_client.py`, `admin_server.py`, `llama_client.py`, `config.json`. Overrideable via `NROL_AO_REPO` env (`server.py:93`). |
| **Engine repo** | `C:\Claude-Code\NROL-AO\temp-repo` (default; `server.py:34`) | The Bayesian engine + framework. Contains `framework/news_observation_pipeline.py` (prompt builders + parsers), `framework/pipeline.py` (Bayesian update), `framework/source_db.py`, `framework/source_ledger.py`, `framework/calibrate.py`, `topics/*.json` (topic state), `loom/mcp_activity/` (activity ledger + digests), `sources/source_db.json`. |

**Import mechanism:** the MCP server imports the engine repo at runtime via `_import_from_repo(module_name)` (`mcp_servers/nrol_ao/server.py:132`), which inserts the engine repo into `sys.path` (`server.py:126-127`) and returns the imported module. Engine modules are NOT in `a-shadow-loom`; they cannot be edited here. The MCP server depends on them at runtime.

**Where each track's work lives:**

| Work item | Lives in | Why |
|-----------|----------|-----|
| **Engine-side tools** (`propose_advocate`, `submit_jury`, `fetch_article`, `fire_indicator`, etc.) — the tool surface that wraps the state layer | **MCP server repo** — new folder `mcp_servers/nrol_ao_engine/` | See §0.6 below. These are NEW code; putting them in the engine repo would couple the new tool-call architecture to the legacy engine. A new sibling MCP folder keeps the new architecture self-contained in `a-shadow-loom` while it calls into the engine repo's state layer via `_import_from_repo` (the same pattern `mcp_servers/nrol_ao/server.py` already uses). |
| Line-based parsers to delete (`_DECISION_BLOCK`, advocate/rebut/jury parsers) | **Engine repo** (`framework/news_observation_pipeline.py`) | Where the parsers live; deleted in Track A phase 5 once the new path is trusted |
| `build_*_prompt` string builders to delete | **Engine repo** (`framework/news_observation_pipeline.py`) | Where the builders live; deleted in Track A phase 5 |
| `_split_channel_scaffold` strip code to delete (Track A) / fix-at-shim (Track B) | **MCP server repo** (`mcp_servers/nrol_ao/llama.py:27-55`) | Where the strip lives |
| Operator MCP tool flips (`run_news_scan` → "launch engine agent") | **MCP server repo** (`mcp_servers/nrol_ao/server.py`) | Where the MCP tools live |
| Anthropic shim (Track B) | **MCP server repo** (new file, e.g. `mcp_servers/nrol_ao/anthropic_shim.py`) | Sits between Claude Code and Dream; started by `admin_server.py` |
| Engine agent harness (custom Python loop or SDK subagent launcher) | **MCP server repo** (in the new `mcp_servers/nrol_ao_engine/` folder) | Launches and drives the agent; lives where the MCP server can spawn it |
| Tool-call-trace audit ledger format | **MCP server repo** (`mcp_servers/nrol_ao_engine/`) — persisted alongside (not into) the existing activity ledger | See A.5 |

**Consequence for sequencing:** Track A work no longer requires editing the engine repo for the new tools — they live in `mcp_servers/nrol_ao_engine/` and call the engine repo's `framework/pipeline.py` via `_import_from_repo` (read/write). The engine repo is touched only for *deletions* (parsers, prompt builders) in Track A phase 5, once the new path is trusted. The MCP server repo's `_import_from_repo` means a changed engine module is picked up on next server restart — no rebuild, but the engine repo must be on `sys.path`.

---

## 0.6 The new `mcp_servers/nrol_ao_engine/` folder

The engine-side tools live in a new sibling MCP folder, `mcp_servers/nrol_ao_engine/`, in `a-shadow-loom` — **not** in the temp-repo engine. This is the correct call for four reasons:

1. **The pattern already exists.** `mcp_servers/nrol_ao/` is itself an MCP server that imports the temp-repo engine at runtime via `_import_from_repo()` (`server.py:132`). A new `mcp_servers/nrol_ao_engine/` folder is the same pattern: new code in `a-shadow-loom`, calling into the engine repo's state layer. No new coupling mechanism invented.
2. **Keeps the new architecture self-contained.** Putting the new tool-call code in the engine repo would couple the new architecture to the legacy engine's release cycle and git history. A sibling folder lets the new architecture evolve independently in `a-shadow-loom` while the engine repo's `framework/` stays focused on the Bayesian engine + state layer (which is unchanged — see §1.3).
3. **The engine repo is imported, not owned.** The engine repo's `framework/pipeline.py` (Bayesian update), `framework/source_db.py`, `topics/*.json`, `loom/mcp_activity/` — these are imported by the new folder via the same `_import_from_repo` shim. The new folder owns the *tool surface* and the *agent harness*; it does not own the state layer.
4. **Operator-facing MCP stays clean.** The operator MCP (`mcp_servers/nrol_ao/`) becomes thin — it dispatches to the engine agent and records audit metadata. The engine MCP (`mcp_servers/nrol_ao_engine/`) holds the tool surface + agent harness that the engine agent (and its advocate/rebut/jury subagents) calls. Two MCP servers, two responsibilities, one engine repo underneath.

**Folder layout (proposed):**
```
mcp_servers/nrol_ao_engine/
  __init__.py
  server.py              # the engine MCP server: registers the engine-side tools
  tools/
    read.py              # read_topic, read_indicator_schema, read_recent_evidence, read_parked_queue
    fetch.py             # fetch_article, run_search
    deliberate.py        # propose_advocate, propose_rebut, submit_jury
    act.py               # fire_indicator, observe_indicator, park_article, flag_schema_gap, flag_duplicate
    ledger.py            # write_evidence
  engine_agent.py        # the custom Python loop OR SDK subagent launcher (A.7)
  audit.py               # tool-call-trace ledger (A.5)
  README.md
```

Each tool module imports the engine repo's state layer via `_import_from_repo` (the same shim, factored to a shared location or duplicated from `mcp_servers/nrol_ao/server.py:132`). The tools are thin wrappers: `fire_indicator(slug, indicator_id, evidence)` calls `framework.pipeline`'s update function with the same arguments the current `submit_transition` commit path uses — no new posterior math, no new gates.

---

## 0. TL;DR

The NROL-AO deliberation pipeline is a hand-rolled reimplementation of what a tool-using agent provides natively: it serializes structured intent into prompt text, constrains reasoning to `<one sentence>`, and parses the model's text output back into structures with fragile line-based regex. The result is "deliberations" where three models each emit a one-liner restating the prior round — not actual engagement.

The fix is architectural: **stop serializing structured intent to text. Use typed tool calls as the deliberation primitive**, so the schema is enforced by the tool-use protocol instead of recovered by regex. This was validated by a live probe — Dream (DiffusionGemma) returns clean `tool_calls` objects with empty `content` and zero thought-channel contamination on the tool-use path, so the entire `<|channel>thought` strip problem becomes irrelevant.

Two tracks implement the change:
- **Track A — Engine Agent Migration:** replace the prompt-build + regex-parse deliberation pipeline with an engine agent (and advocate/rebut/jury subagents) that emit typed verdicts via tool calls.
- **Track B — Anthropic Shim for Dream:** a small Python server translating Anthropic Messages API ↔ OpenAI Chat Completions, so Claude Code can use Dream as a provider. QoL win for writing tasks, and it unifies the engine-agent harness decision (the engine agent becomes a Claude Code Agent SDK subagent pointed at the shim → Dream, no custom Python loop needed).

The tracks are decoupled and can proceed independently. Convergence is optional but preferred.

---

## 1. Current-state diagnostic

### 1.1 What works

The GPU scan path functions. A news scan at 2026-07-06T17:29–17:33Z produced 17 decisions on the DiffusionGemma GPU backend. Evidence:
- `nvidia-smi` (probed 17:46Z): `llama-diffusion-gemma-server.exe` PID 3540 holding 23395 MiB / 32607 MiB VRAM, listening on `127.0.0.1:8787`. Model JIT-resident (`-ngl 999`).
- Activity ledger: all 3 debate stages record `"backend": "dream"`, `finish_reason: "stop"`, real output (1861–3032 chars).
- The stored matcher_output and all 3 debate responses contain the `<|channel>thought\n<channel|>` scaffold prefix — DiffusionGemma-specific markup that Qwen3.6 (the llama backend) never emits. Proof the 4 LLM calls hit `:8787`.

A user report of "0% GPU during the scan" is most likely a monitoring artifact: the scan is CPU-heavy for ~2 min of search/fetch (GPU idle), GPU bursts only ~90–120s for matcher + 3 debate calls, and Windows Task Manager's default GPU graph shows the 3D engine (which under-reports CUDA compute). Verify with `nvidia-smi -l 1` or the Cuda/Compute graph during a scan.

### 1.2 What's broken or degraded

| # | Subsystem | State | Evidence |
|---|-----------|-------|----------|
| 1 | Dream GPU scan path | ✅ Working | VRAM resident, `backend: dream` in ledger, `<\|channel\|>` scaffold in output |
| 2 | Dream scaffold strip | ❌ Broken | `<\|channel\|>thought` leaks into stored output; the `if extracted_reasoning:` guard at `mcp_servers/nrol_ao/llama.py:289` skips the strip when the thought block is empty (which it always is — DiffusionGemma emits an empty thought channel) |
| 3 | llama backend `:8000` | ❌ Down | `WinError 10061` (connection refused). Default backend is llama + no auto-fallback (`server.py:2007-2008`) → `red_team_*`, `deliberate_candidates`, `future_cast`, `run_matcher_with_llama` all error unless `model="dream"` is passed explicitly |
| 4 | News search | ❌ Mostly failing | H2/H3/H4/wildcard returned zero results; 11/18 `searchQueries:*` failed (yandex.com blocking + empty results). Scan ran on a thin corpus → all 17 decisions came back PARK/IGNORE, zero FIRE/OBSERVE |
| 5 | Source trust | ❌ Structurally broken (pre-existing) | AP/Bloomberg/CNN/Reuters/CNBC driven to `effective: 0.05` floor (96.8% refutation). Trust is wired in (`framework/topic_search.py:32,218`, `framework/triage.py:97,144`, `framework/source_ledger.py:380,460`, `framework/calibrate.py:262,291`) but values are garbage. Does NOT affect the scan path (trust is used in triage/calibration, not in `news_observation_pipeline.py`). Full report at `mcp_servers/nrol_ao/SOURCE_TRUST_CALIBRATION_FINDINGS.md` |
| 6 | Parked-evidence queue | ❌ 100% in debt | `calibration-hormuz-reopen-2027`: 844 evidence items, 728 PARKED, all 728 due for review (`reviewDebtRatio: 1.0`, oldest 14.7 days). `governanceHealth: DEGRADED` |
| 7 | Deliberation quality | ❌ Not a real deliberation | Three models each emit a one-sentence `REASON`/`RATIONALE` restating the prior round. No substantive engagement because the prompt builders enforce `<one sentence>` (`news_observation_pipeline.py:354, 423, 511`) and the parsers capture reason as `[^\n]*` (single line) |

### 1.3 The root architectural flaw

Items 2, 4 (partially), and 7 share a single root cause: **structured intent is serialized to prompt text and parsed back with line-based regex.** This produces:
- `<one sentence>` constraints (because the regex captures one line)
- The `<|channel>thought` strip bug (because text generation leaks the channel)
- Parser fragility (the documented END-terminator bug: "12 blocks emitted, 1 parsed")
- Line-serialization disease across article fetches, the evidence ledger, and triage

The architecture update deletes this entire class of bug by moving to typed tool calls, where the schema is enforced by the tool-use protocol and there is nothing to regex-parse.

---

## 2. Architectural decision

### 2.1 Tool calls as the deliberation primitive

Instead of:
```
prompt → model emits "ADVOCATE\nARTICLE: A9\nVERDICT: PARK\nREASON: <one sentence>\nEND"
       → regex parses it back into {idx, verdict, reason}
```

Do:
```
model calls propose_advocate(article_id="A9", verdict="PARK",
                            proposed_action={kind:"PARK"},
                            analysis="<multi-paragraph, unconstrained>")
       → tool-use protocol delivers a typed object; no parsing
```

The `analysis`/`rationale` parameters are free strings of any length. The `verdict` and `kind` enums are enforced by the tool schema. There is no `DUPLICATE_OF`-without-a-kind contradiction (a bug in the prior JSON-mode spec) — `parent_idx` is the discriminator on the `final_action` object when `kind` is `DUPLICATE_OF`.

### 2.2 Gating probes — both PASS

**Unknown 1 (Dream tool-use support): PASS, verified live 2026-07-06 — single-turn AND multi-turn.**
Single-turn probe against `http://127.0.0.1:8787/v1/chat/completions` with an OpenAI `tools` payload returned:
```
finish_reason: "tool_calls"
content: ""                          (empty — no thought-channel contamination)
tool_calls: [{id: "call-13", type: "function",
              function: {name: "get_weather", arguments: '{"location":"Paris"}'}}]
```
Multi-turn probe (turn 2 = tool result returned to model, model must consume it and answer):
```
Turn 1: finish_reason=tool_calls, content="", clean tool_calls object (get_weather, {"location":"Paris"})
Turn 2 (tool result {"temperature":18,"condition":"sunny","wind":12} returned):
  finish_reason: stop
  content: "The weather in Paris is currently sunny with a temperature of 18°C and a wind of 12 km/h."
  tool_calls: none
  content has channel tag: False
```
**Two key findings:** (1) the `<|channel>thought` leak that plagues the text-generation path does NOT affect the tool-use path — when the model emits a tool call, the adapter routes it cleanly into the structured `tool_calls` object and `content` stays empty; (2) text generation *after* a tool-call turn is also clean — the multi-turn turn-2 response has no channel contamination. The entire channel-strip problem (item 2 in the diagnostic) becomes irrelevant under the tool-call architecture, including the text-generation turns in a multi-turn tool-use conversation. Multi-turn tool use (the actual deliberation pattern) is verified, not just single-call.

**Unknown 2 (engine-agent harness: SDK vs custom loop): resolved by Track B.** Originally deferred. If the Anthropic shim (Track B) lands, the engine agent can be a Claude Code Agent SDK subagent pointed at the shim → Dream, and no custom Python agent loop is needed. If the shim is delayed, a minimal custom Python loop speaking OpenAI format directly to `:8787` is the fallback (~100 lines, Gemini's recommendation). Track A is harness-agnostic until phase 2.

---

# Track A: Engine Agent Migration

## A.1 Architecture

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
│  Tool surface: engine-side MCP tools (see A.2)      │
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

## A.2 Engine-side tool surface (the schema IS the spec)

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

The `verdict` and `kind` enums are enforced by the tool schema.

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

## A.3 Deliberation as subagents

1. Engine agent runs `run_search` + `fetch_article` per candidate (structured returns, no line parsing).
2. Spawns **Advocate subagent** with candidates + indicator schema as context. Advocate calls `propose_advocate` per article — full multi-paragraph `analysis` field.
3. Spawns **Rebut subagent** with advocate's structured proposals (full `analysis` fields) injected. Rebut calls `propose_rebut`.
4. Spawns **Jury subagent** with both prior rounds' structured output. Jury calls `submit_jury` per article.

Each subagent can read the indicator schema, check prior evidence, reason across multiple tool calls — actual deliberation. The jury sees the advocate's multi-paragraph `analysis` and the rebut's `objection_details` in full, passed as structured records, not collapsed to a sentence.

## A.4 What survives vs what's deleted

**Survives:** topic/evidence JSON, Bayesian engine, indicator schema definitions, source_db, `framework/pipeline.py` update logic, the Loom permission + governance commit gates.

**Deleted:** `mcp_servers/nrol_ao/llama.py:chat()` prompt machinery, all line-based parsers (`_DECISION_BLOCK`, advocate/rebut/jury parsers in `news_observation_pipeline.py`), `build_*_prompt` string builders, `run_matcher_with_llama` / `deliberate_candidates` text tools, the `<one sentence>` prompt constraints, the `<|channel>thought` strip code (irrelevant — tool calls have no thought channel).

**New:** engine agent definition + system prompt, engine-side MCP tool implementations (thin wrappers over existing state-layer functions), operator→engine bridge, tool-call-trace audit ledger format.

## A.5 Audit shape

Current: activity ledger records per-HTTP-call `{backend, model, finish_reason, output_chars, raw_text}`.

Engine agent: audit unit is the **tool-call trace** — the sequence of tool calls the agent and subagents made, with arguments and return values. Strictly more informative: you see *what the agent decided* (typed verdicts) and *why* (the analysis params), not just "446 chars of text came back, parsed to 3 blocks." Raw LLM completions can still be logged at the provider level for debugging; the audit ledger is the tool trace.

## A.6 Migration path (additive, never breaks the working scan)

The state layer and commit gates never change, so a bad engine-agent run cannot corrupt topic state that the current system couldn't also corrupt. Migration is reversible through phase 5.

1. **Build one engine-side tool** (`fetch_article`) + a minimal engine agent that uses it. Validate the tool-call round-trip end-to-end on Dream. (Probe already confirms this works; this step confirms it works through the chosen harness — SDK via Track B shim, or custom loop.)
2. **Decide harness layer** (Unknown 2): if Track B shim landed → Claude Code Agent SDK subagent pointed at shim; else → minimal custom Python agent loop speaking OpenAI format to `:8787`. Validate multi-turn tool use with a 2-call sequence (the multi-turn probe in §2.2 already verified Dream handles this).
3. **Add deliberation tools** (`propose_advocate`, `propose_rebut`, `submit_jury`). Run **advocate-only** as a subagent. Compare output quality vs the current one-liner advocate — confirm the `analysis` field carries actual multi-paragraph reasoning (concrete metric: §4.1 phase 3).
4. **Add rebut + jury subagents.** Wire to the existing commit gates (Loom approval, governance, evidence log) — the commit path is unchanged, the engine agent calls the same gates the current tools do.
5. **Operator MCP tools flip** from "build prompt + parse" to "launch engine agent + collect trace." Old tools coexist behind a flag until the new path is trusted. Then delete the prompt-build + regex-parse code.

## A.7 Engine agent launch mechanics

**How the engine agent is spawned from the MCP server:**

- **Custom Python loop path (default if Track B not landed):** the engine MCP server (`mcp_servers/nrol_ao_engine/server.py`) runs the engine agent as a Python function call (in-process). The loop is `mcp_servers/nrol_ao_engine/engine_agent.py` — it builds the OpenAI-format message list, calls a thin Dream-direct client with `tools`, dispatches returned tool calls to the engine-side tool implementations (in `mcp_servers/nrol_ao_engine/tools/`, which call the engine repo's state layer via `_import_from_repo`), appends tool results, loops until `finish_reason=stop` with no tool calls.
- **SDK subagent path (if Track B shim landed):** the engine MCP server spawns a Claude Code Agent SDK subagent via the SDK's Python API, pointed at the shim's `ANTHROPIC_BASE_URL`. The SDK handles the tool-call loop; the engine-side tools (in `mcp_servers/nrol_ao_engine/tools/`) are registered as MCP tools the subagent can call. Subagents (advocate/rebut/jury) are launched via the SDK's `Agent` tool equivalent.
- **Concurrent scans:** the engine agent is launched per-scan by the operator MCP `run_news_scan` tool. Concurrency is bounded by the existing scan-serialization in the MCP server (today, scans run one at a time per worker). The engine agent does not introduce new concurrency — it runs within the scan's existing lifetime. The Dream sidecar is single-model; concurrent LLM calls queue at the sidecar.
- **Failure modes:** a tool-call loop that exceeds N turns (e.g. 20) without `finish_reason=stop` is aborted and recorded as a failed scan. Malformed tool arguments (schema violation) are retried once, then fail-closed. The commit gates (Loom approval, governance) are unchanged — a bad engine-agent run cannot commit evidence the current tools couldn't.

## A.8 Sequencing rationale

Kimi's review correctly noted a sequencing bias: recommending Track B first only makes sense if the QoL win is the priority. If the goal is NROL deliberation quality, **Track A phase 1 with the custom Python loop is lower risk and gives signal faster** — it validates the tool-call deliberation idea end-to-end without building the shim.

**Revised default sequencing (lower-risk first):**
1. **Track A phase 1** — `fetch_article` tool + custom Python loop, validated against Dream. Cheapest possible validation of the core idea. No shim, no SDK, no C++ work.
2. **Track A phase 2** — multi-turn tool use (already probe-verified), advocate-only subagent. Concrete signal on deliberation quality (§4.1 phase 3 metric).
3. **Track A phase 3-5** — rebut/jury, commit gates, operator MCP flip.
4. **Track B** — pursued separately/asynchronously for the QoL win, OR if Track A phase 2 shows the custom loop is painful enough to justify the shim + SDK path.

Track B is not a prerequisite for Track A. The custom Python loop is the default harness; Track B is an optional upgrade that, if it lands, lets Track A migrate to the SDK path. This removes the sequencing bias.

---

# Track B: Anthropic Shim for Dream

## B.1 Purpose

**Primary (operator-stated): QoL improvement.** Let Dream (DiffusionGemma) serve as a Claude Code model for writing tasks, by launching Claude Code with a custom provider URL pointing at the shim. Dream is an effective writing model but has only been reachable via OpenAI Chat Completions; Claude Code speaks the Anthropic Messages API, so the two cannot talk directly.

**Secondary (architectural): unify the engine-agent harness** (Unknown 2). If the shim exists, the Track A engine agent can be a Claude Code Agent SDK subagent pointed at the shim → Dream, eliminating the need for a custom Python agent loop. The shim becomes the single integration point for both operator-facing Claude Code sessions and the NROL-AO engine agent.

Operator has acknowledged this is scope creep relative to the engine-agent migration, but wants it for the QoL benefit.

## B.2 Architecture

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

## B.3 Translation concerns

**Request (Anthropic Messages → OpenAI Chat Completions):**
- `messages` with content blocks (`text`, `image`, `tool_use`, `tool_result`) → OpenAI messages (content string / `tool_calls` / tool-role messages).
- Top-level `system` param → OpenAI system message (prepend).
- `tools` (Anthropic: `{name, description, input_schema}`) → OpenAI `tools` (`{type:"function", function:{name, description, parameters}}`).
- `max_tokens` (required in Anthropic) → `max_tokens` + `dream_thought_budget()` (default 4096, reuse `mcp_servers/nrol_ao/llama.py:120-132` logic) so the thought channel doesn't starve content into `finish_reason=length`. Dream ignores `enable_thinking`/`chat_template_kwargs`, so don't send it (keeps payload clean, mirroring `llama.py:259`).
- `temperature`, `top_p`, `stream` → passthrough.
- Model name: shim ignores incoming `model` and uses Dream's loaded model (`diffusiongemma-26b-a4b-it-nvfp4`), OR passes through. Simplest: always target Dream's model (only one is loaded).

**Response (OpenAI → Anthropic):**
- Non-streaming: `choices[0].message` → Anthropic `content` array with `text` and `tool_use` blocks.
- `finish_reason` mapping: `stop`→`end_turn`, `tool_calls`→`tool_use`, `length`→`max_tokens`, `content_filter`→`end_turn`.
- `usage` → Anthropic `usage` (`input_tokens`/`output_tokens`).
- **Thought-channel strip (text responses only):** when `content` is non-empty text, strip `<|channel>thought...<channel|>`. Reuse `_split_channel_scaffold` logic from `mcp_servers/nrol_ao/llama.py:27-55` (or `dream_client._split_channel_scaffold`), but **fire unconditionally when the regex matches** — do NOT replicate the `if extracted_reasoning:` guard that causes the empty-thought edge-case bug at `llama.py:289`. This fixes the strip bug at the shim layer for all Claude Code text generation through Dream. Tool-call responses need no strip (verified: `content` is empty on tool_calls).

**Streaming (OpenAI delta stream → Anthropic SSE):** the hard part. Anthropic requires a specific 6-event sequence:
1. `message_start` (message id, role, model, usage with input_tokens)
2. `content_block_start` (index, type `text` or `tool_use`; for tool_use, include id + name)
3. `content_block_delta` (`text_delta` for text, `input_json_delta` for tool arguments — OpenAI streams tool args in pieces via `delta.tool_calls[].function.arguments`)
4. `content_block_stop` (index)
5. `message_delta` (stop_reason, usage with output_tokens)
6. `message_stop`

OpenAI delta stream → map each `delta.content` chunk to a `content_block_delta`/`text_delta`; accumulate `delta.tool_calls[].function.arguments` pieces and emit as `input_json_delta`. Emit `content_block_start`/`stop` on transitions between text and tool_use blocks.

## B.4 Endpoints the shim must serve
- `POST /v1/messages` — main, Anthropic format, SSE streaming + non-streaming.
- `GET /v1/models` — Claude Code probes for available models on provider connect; return Dream's loaded model.
- `POST /v1/messages/count_tokens` — Anthropic token counting; stub with a rough estimate (len-based) or best-effort. Claude Code uses this for context budgeting; a rough estimate is acceptable.

## B.5 Claude Code launch wiring
When a "dream model" is selected in Loom (model selector), Loom launches `claude` with `ANTHROPIC_BASE_URL=http://127.0.0.1:SHIM_PORT` and `ANTHROPIC_API_KEY=dummy` (shim does not auth against a real key). Existing dream-model detection (`llama_client.py:_is_dream_model` / `_chat_host_for_model`, `llama_client.py:125-135`) already routes dream models — extend the claude-session spawn path to set the env vars when a dream model is detected. The shim sidecar is started by `admin_server.py` alongside the dream sidecar (extend the dream-start/dream-stop lifecycle at `admin_server.py:1192/1227` to also start/stop the shim).

## B.6 Relationship to Track A
- If Track B lands before Track A phase 2: the engine agent uses the Claude Code Agent SDK pointed at the shim. No custom Python loop needed. The shim is the single integration point for both operator Claude Code sessions and the NROL-AO engine agent.
- If Track A phase 2 lands first: the engine agent uses a minimal custom Python loop speaking OpenAI format directly to `:8787` (no shim). Track B then lands later as pure Claude Code QoL, and Track A can optionally migrate to the SDK-via-shim path afterward.
- The two tracks are decoupled and can proceed independently. Convergence is optional.

## B.7 Shim complexity & risk
- Bounded. The translation is well-defined with reference implementations (LiteLLM, claude-code-proxy projects do exactly this). A custom shim (~300-500 lines) lets us bake in the Dream thought-channel strip, which off-the-shelf proxies won't handle.
- Main risk: streaming SSE tool-use (`input_json_delta` piecewise assembly). Fiddly but well-specified. Mitigate with a non-streaming fallback path first (get correctness on non-streaming tool calls, then add streaming).
- The probe already proved Dream emits clean tool_calls on the OpenAI path, so the shim's downstream is verified.

---

## 3. Rejected alternatives (with reasoning)

### 3.1 "Just loosen the `<one sentence>` prompt constraint" (rejected)
Considered as a cheap alternative to the full migration. Rejected because: the current parsers capture reason as `[^\n]*` — a single line. The moment REASON becomes multi-paragraph, the line-based regex breaks, returning to the exact parser-fragility class that already swallowed 11 of 12 blocks in the END-terminator bug. Multi-paragraph free text in a line format is *more* fragile, not less. The real goal (substantive multi-paragraph argumentation flowing between rounds) requires structured output, and typed tool calls are more robust than JSON-mode-via-`response_format` for the reasons in §3.2.

### 3.2 JSON-mode spec (`response_format: json_object`) (rejected)
A prior Gemini-authored spec proposed transitioning the deliberation engine from line-based regex to structured JSON outputs via `response_format={"type":"json_object"}`. Rejected in favor of typed tool calls because:
- The spec's central premise — "turns only get single-sentence summaries of prior rounds" — was partially true (the `REASON` field is constrained) but the diagnosis was wrong: each stage actually receives the full structured output of every prior stage (6 fields per article in `build_rebut_prompt`, 11 fields per case in `build_jury_prompt`), not a single sentence. The real problem is that those fields are themselves one-liners, which is a prompt constraint, not a serialization format problem.
- JSON-mode parsing is still parsing — it just moves the fragility from line-regex to JSON-schema validation. Tool calls eliminate parsing entirely (the protocol delivers typed objects).
- The spec had enum/schema contradictions (`verdict: DUPLICATE_OF` exists but `proposed_action.kind` enum omits it) that would cause model failure.
- `response_format` is not wired into `llama.py:chat()` at all — it would be a real code change, and DiffusionGemma's honoring of it is unvalidated (the same adapter that leaks the thought channel).
- Multi-paragraph free text in a JSON string field is no more robust than in a line format; the structural win comes from typed tool calls, not from JSON vs text.

### 3.3 Gemini "Expose Endpoints for Modern Agentic Harnesses" spec (rejected)
Evaluated section by section; NOT included:
- **§2B (OpenAI `/v1/chat/completions` with tools): already done.** The gating probe proved Dream's C++ server returns clean `tool_calls` with empty `content` and no thought-channel contamination. Building it would rebuild what exists.
- **§2A (native Anthropic `/v1/messages` + SSE + `tool_use` blocks in the C++ server): rejected in favor of Track B shim.** A Python shim (~one file, ~300-500 lines) is cheaper than native C++ SSE in the forked llama.cpp + recompile + ongoing fork maintenance. Track B IS the Anthropic endpoint — just implemented as a shim, not in C++.
- **§1 (Images / multimodal): scope creep.** Gemini correctly notes the NVFP4 GGUF is missing the vision tower entirely — native vision (Path B) requires a different GGUF + C++ vision encoder integration, a separate project. Path A (visual-to-text fallback) is real but NROL-AO has no image inputs today (text articles, numeric/textual indicators). *Future caveat:* satellite imagery / AIS visual confirmation could be a tier-3 Hormuz evidence source someday — but that's a different spec, different GGUF, not this work.
- **§2C (Hermes ACP/HTTP): already wired.** Loom has Hermes-on-Dream at `server.py:6509-6874` (`_ensure_dream_hermes_home`). Not needed for the NROL-AO engine path.
- **§2D (WebSocket real-time / voice): no real-time need in NROL-AO.** Pure scope creep.

### 3.4 Native C++ Anthropic endpoint (rejected)
Rejected in favor of Track B shim. Cost comparison:
- Native `/v1/messages` in the C++ server: real C++ work on a forked llama.cpp. HTTP route addition, SSE streaming for the 6 Anthropic event types, the `tool_use` content block format. Bounded but non-trivial, plus a recompile and ongoing fork maintenance.
- Translation shim: ~one Python file, ~300-500 lines. Well-trodden territory. Talks the format that already works.
- Custom Python loop (if no shim): talks OpenAI format directly, no SDK, no shim, no C++. ~100 lines.

Even in the SDK path, a shim is cheaper than C++ server mods. And if we go custom loop, native `/v1/messages` is not needed at all.

---

## 4. Verification (end-to-end)

### 4.1 Track A
- **Phase 1:** `fetch_article` tool call round-trips through the engine agent on Dream, returns a structured article object (not a line of text).
- **Phase 3 (concrete, not "compare output quality"):** Advocate subagent's `propose_advocate` calls carry `analysis` strings with **char length > 400** (the current `REASON` field averages ~80–120 chars) AND containing **at least one cited prior evidence id or indicator id** (verifies the agent actually read context, not restated the matcher). `verdict`/`kind` enums are schema-valid (tool-use protocol enforces this — a malformed value is a tool-call error, not a silent PARK).
- **Phase 4:** A full scan through the engine agent produces jury verdicts that move posteriors via the existing commit gates — same posterior delta math, no regression vs current scan output. Run a known article through both paths and compare the evidence logged (evidence_id, posterior delta, indicator_id must match within float tolerance).
- **Phase 5:** Operator MCP `run_news_scan` launches the engine agent, returns a tool-call trace as the audit record. Old `run_matcher_with_llama` / `deliberate_candidates` still work behind the flag for parity checks.

### 4.2 Track B
- Shim serves `POST /v1/messages` (non-streaming) and returns a valid Anthropic response for a text prompt → Claude Code can complete against Dream.
- Tool-use round-trip: a `tools`-bearing Anthropic request returns a `tool_use` content block with valid `input` JSON, assembled from Dream's `tool_calls`.
- Streaming: SSE event sequence matches Anthropic's 6-event contract (`message_start` → `content_block_start` → `content_block_delta` → `content_block_stop` → `message_delta` → `message_stop`); a reference Claude Code client consumes it without error.
- Thought-channel strip: a text response from Dream arrives at Claude Code with no `<|channel>thought<channel|>` scaffold (the empty-thought edge case is handled — strip fires on regex match, unconditionally).
- `GET /v1/models` returns Dream's loaded model; Claude Code provider-connect probe succeeds.
- Loom launch: selecting a dream model spawns `claude` with `ANTHROPIC_BASE_URL` → shim, and a writing task completes against Dream.

---

## 5. Known limitations & out-of-scope problems

This architecture update fixes the deliberation-parsing class of bugs. It does NOT fix everything in the diagnostic table. Being explicit about scope:

| Diagnostic item | Fixed by this architecture? | Notes |
|-----------------|----------------------------|-------|
| #2 Dream scaffold strip | **Yes** — irrelevant on tool path (Track A); fixed at shim on text path (Track B) | |
| #7 Deliberation quality | **Partially** — tool calls remove the parsing constraint and allow multi-paragraph reasoning, but do not *guarantee* it. The system prompts and tool descriptions must explicitly demand multi-paragraph analysis and citation of prior evidence/indicators. See §6. | Tool calls are necessary but not sufficient |
| #3 llama backend `:8000` down | **No** — separate operational issue | Start llama-server on `:8000`, OR set `NROL_AO_LLM_BACKEND=dream` so default-routed tools hit the live GPU sidecar. Not addressed by this architecture. |
| #4 News search failing | **No** — not a parsing problem | Yandex blocking + empty results. The `run_search` engine tool will use the same search channels as today. Fixing search quality is a separate effort (provider config, fallback chain, possibly a different search backend). |
| #5 Source trust structurally broken | **No** — pre-existing, separate | Trust is wired in (`framework/topic_search.py`, `framework/triage.py`, `framework/source_ledger.py`, `framework/calibrate.py`) but values are garbage. Full report at `mcp_servers/nrol_ao/SOURCE_TRUST_CALIBRATION_FINDINGS.md`. Not addressed by this architecture. |
| #6 Parked-evidence queue (728 due) | **No** — but unblocked by fixing #3 | `review_parked` needs a working LLM backend. Once #3 is fixed, a `review_parked` pass (which uses the existing deliberation path, not the new engine agent) would triage the backlog. The engine agent migration doesn't touch the parked-review tool. |

**Consequence:** `governanceHealth` on `calibration-hormuz-reopen-2027` will remain DEGRADED after this architecture lands, because the parked backlog (728) and source trust are not fixed by it. This is acceptable — the architecture update has a specific scope (deliberation quality + parsing robustness), and conflating it with the other faults would expand scope unboundedly.

---

## 6. Tool calls are necessary but not sufficient — system prompt + tool-description requirements

Removing the parsing constraint allows multi-paragraph reasoning; it does not produce it. The engine agent's system prompt and the `description` fields on `propose_advocate` / `propose_rebut` / `submit_jury` must explicitly demand:

- **Multi-paragraph analysis**, not a one-liner. The `analysis` param description should say "detailed multi-paragraph strategic and logical analysis; cite specific evidence from the article and specific indicators from the schema."
- **Citation of prior rounds.** The rebut's `objection_details` should reference specific claims in the advocate's `analysis`. The jury's `jury_rationale` should reference specific points from both advocate and rebut. The subagent context injection (A.3) makes the full prior-round records available; the prompts must tell the model to use them.
- **Reading the indicator schema.** The advocate/rebut/jury subagents must call `read_indicator_schema` (and `read_recent_evidence` where relevant) before proposing a verdict, not just pattern-match the article headline.

These are prompt-engineering requirements on the new architecture, not afterthoughts. Phase 3 verification (§4.1) checks for them concretely (analysis > 400 chars, at least one cited indicator/evidence id).

---

## 7. Appendix: grounding

### 7.1 Live probe results (Unknown 1, PASS)
```
POST http://127.0.0.1:8787/v1/chat/completions
payload: {model: diffusiongemma-26b-a4b-it-nvfp4, messages: [...], tools: [get_weather], tool_choice: auto}

response:
  finish_reason: "tool_calls"
  message.content: ""   (empty — no thought-channel contamination)
  message.tool_calls: [{id: "call-13", type: "function",
                        function: {name: "get_weather", arguments: '{"location":"Paris"}'}}]
  tool_calls arguments parse as valid JSON: yes
  tool_calls has channel tag: False
```
Key finding: the `<|channel>thought` leak that plagues the text-generation path does NOT affect the tool-use path. When the model emits a tool call, the adapter routes it cleanly into the structured `tool_calls` object and `content` stays empty.

### 7.2 Files inspected (read-only, for grounding)
- `mcp_servers/nrol_ao/llama.py` — `chat()` (212), `resolve_backend()` (135), `_split_channel_scaffold()` (27-55, the strip that becomes irrelevant on the tool path and gets fixed-at-shim on the text path), `dream_thought_budget()` (120-132), `dream_host()`/`llama_host()` (76-106)
- `mcp_servers/nrol_ao/server.py` — `_run_debate()` (1035-1160), `run_news_scan` (3709+), model forwarding to `_run_debate` (3948), `_import_from_repo()` (132), `_DEFAULT_REPO` (34)
- `framework/news_observation_pipeline.py` — `build_advocate_prompt`/`build_rebut_prompt`/`build_jury_prompt` (294-520), the line parsers, `<one sentence>` constraints at 354/423/511
- `dream_client.py` — async Dream sidecar client (`dream_chat` 185, `dream_chat_sync` 255, `_split_channel_scaffold` 44-72 canonical copy)
- `admin_server.py` — dream sidecar broker (958-1416), `dream-start`/`dream_stop` lifecycle (1192/1227), launch cmd with `-ngl 999` (1049-1073)
- `server.py` — `_handle_dream_generation` Hermes-on-Dream (6509-6874), `_dream_openai_base_url` (6513)
- `llama_client.py` — `_is_dream_model`/`_chat_host_for_model` (125-135) for dream-model detection
- `config.json` — `dream_host` (:8787), `dream_model` (diffusiongemma-26b-a4b-it-nvfp4), `dream_server_exe`
- `framework/pipeline.py`, `source_db.py`, `source_ledger.py`, `calibrate.py` — state layer (unchanged, wrapped not rewritten)

**Line numbers are accurate as of 2026-07-06 and will drift.** They are pointers for grounding, not stable references. When implementing, locate the code by symbol name (`_run_debate`, `build_advocate_prompt`, `_split_channel_scaffold`, etc.), not by line number. The symbol names are stable; the line numbers are not.

### 7.3 Runtime state at time of writing (2026-07-06 ~17:46Z)
- Dream sidecar (`:8787`): UP, `diffusiongemma-26b-a4b-it-nvfp4` loaded, 23395 MiB VRAM resident, PID 3540
- llama backend (`:8000`): DOWN (WinError 10061)
- `NROL_AO_LLM_BACKEND` env: empty (default backend = llama, no auto-fallback)
- Active topic: `calibration-hormuz-reopen-2027` (ACTIVE, 844 evidence, 728 parked, governanceHealth DEGRADED)
- Latest scan digest: `digest-20260706T173308Z.json` (17 decisions, all PARK/IGNORE, `commit_policy: safe`)

### 7.4 Memory notes corrected by this investigation
- `project_nrol_dream_feature_complete` claimed the `<|channel>thought` strip is "fixed." It is NOT fixed — the `if extracted_reasoning:` guard at `llama.py:289` skips the strip when the thought block is empty (which it always is). The strip becomes irrelevant under the tool-call architecture (Track A) and gets genuinely fixed at the shim layer (Track B).
- `project_nrol_dream_feature_complete` claimed "port default :18081→:8787." The llama default is `:8000` (`llama.py:82`); `:18081` lingers only as a stale default in `config.py:126` and `server.py:6513`'s fallback. The dream default is `:8787` (`llama.py:104`), matching the persisted `config.json`.

---

## Implementation status

**Nothing implemented.** This document is the plan. Implementation begins on operator direction.

**Recommended first concrete step (revised per Kimi review):** Track A phase 1 — create `mcp_servers/nrol_ao_engine/` with `fetch_article` as the first engine-side tool + a minimal custom Python loop in `engine_agent.py`, validated against Dream. This is the lowest-risk, fastest-signal path: it validates the core tool-call-deliberation idea end-to-end without building the shim, without the SDK, without C++ work, and without touching the engine repo (the new folder calls the engine repo's state layer via `_import_from_repo` — read-only from the engine repo's perspective until Track A phase 5 deletions). Track B (the Anthropic shim, QoL win) is pursued separately/asynchronously and is not a prerequisite for Track A.

**Kimi review addressed (2026-07-06):** repo map added (§0.5); multi-turn tool-use probe added to §2.2 (retires the single-probe concern — Dream handles multi-turn tool use cleanly, and text generation after a tool turn is also free of channel contamination); known limitations / out-of-scope problems table added (§5); system-prompt + tool-description requirements added (§6); engine agent launch mechanics added (§A.7); sequencing bias corrected — Track A phase 1 with custom Python loop is now the default first step (§A.8); line-number drift flagged in §7.2; verification metric made concrete (§4.1 phase 3: analysis > 400 chars + at least one cited indicator/evidence id).
