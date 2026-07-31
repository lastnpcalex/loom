# NROL-AO Architecture Update

**Status:** partially implemented. Track B's Anthropic-to-Dream shim exists in the current working tree; Track A's engine-agent deliberation path is still planned. Written 2026-07-06, updated after red-team review. Supersedes `ENGINE_AGENT_AND_DREAM_SHIM_PLAN.md` (folded in below) and the diagnostic report in `~/.claude/plans/i-need-a-report-indexed-sprout.md`.

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
| Anthropic shim (Track B) | **MCP server repo** (`anthropic_dream_router.py`) | Sits between Claude Code and Dream; already wired through main `server.py` / `claude_client.py`, not `admin_server.py` |
| Engine agent harness (custom Python loop first; SDK subagent later if useful) | **MCP server repo** (in the new `mcp_servers/nrol_ao_engine/` package) | Launches and drives the agent; lives where the operator MCP can import it in-process |
| Tool-call-trace audit ledger format | **MCP server repo** (`mcp_servers/nrol_ao_engine/`) — persisted alongside (not into) the existing activity ledger | See A.5 |

**Consequence for sequencing:** Track A work no longer requires editing the engine repo for the new tools — they live in `mcp_servers/nrol_ao_engine/` and call the engine repo's `framework/pipeline.py` via `_import_from_repo` (read/write). Separately, the code-consolidation track (§0.7) does require touching/moving engine code, but that is a path-normalization migration, not a prerequisite for proving the tool-call deliberation architecture. The MCP server repo's `_import_from_repo` means a changed engine module is picked up on next server restart — no rebuild, but the configured engine code root must be on `sys.path`.

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
  server.py              # optional later MCP server wrapper; do not make this phase-1
  tools/
    read.py              # read_topic, read_indicator_schema, read_recent_evidence, read_parked_queue
    fetch.py             # fetch_article, run_search
    deliberate.py        # propose_advocate, propose_rebut, submit_jury
    act.py               # fire_indicator, observe_indicator, park_article, flag_schema_gap, flag_duplicate
    ledger.py            # write_evidence
  engine_agent.py        # custom Python loop first; SDK subagent launcher only if needed (A.7)
  audit.py               # tool-call-trace ledger (A.5)
  README.md
```

Each tool module imports the engine repo's state layer via `_import_from_repo` (the same shim, factored to a shared location or duplicated from `mcp_servers/nrol_ao/server.py:132`). The tools are thin wrappers: `fire_indicator(slug, indicator_id, evidence)` calls `framework.pipeline`'s update function with the same arguments the current `submit_transition` commit path uses — no new posterior math, no new gates.

---

## 0.7 Engine code consolidation (verified)

Operator question: should the entire engine live inside `a-shadow-loom`, outside the MCP operator folder, instead of relying on `C:\Claude-Code\NROL-AO\temp-repo`?

**Answer:** move the **engine code** into `a-shadow-loom`; do **not** move the hot mutable state into OneDrive.

Verified facts:

- `C:\Claude-Code\NROL-AO\temp-repo` is outside OneDrive; `a-shadow-loom` is inside OneDrive.
- The temp repo is a separate git repo with active engine history.
- Runtime state is large and hot: `loom/mcp_activity/activity.jsonl` is ~97 MB, `proposals.db` is ~56 MB, `topics.bak/` is ~358 MB and grows by ~18 MB per topic save, and the active Hormuz topic JSON is ~18 MB.
- `engine.py` already supports a code/state split through `NROL_AO_STATE_DIR`: canonical `topics/`, `briefs/`, `dashboards/`, and `topics.bak/` can live outside the code repo.
- The old roadmap already warned that the split is incomplete: several standalone framework scripts still hardcode repo-local paths and must be converted before production flips to an external state root.

Target layout:

```
C:\Users\exast\OneDrive\Documents\Loom2\a-shadow-loom\
  engine/                         # MOVED engine code: engine.py, governor.py, framework/, etc.
  mcp_servers/nrol_ao/            # operator MCP
  mcp_servers/nrol_ao_engine/     # engine MCP + tool-call agent harness

C:\Claude-Code\NROL-AO\state\     # NOT OneDrive-synced
  topics/
  topics.bak/
  briefs/
  dashboards/
  loom/mcp_activity/
    activity.jsonl
    snapshot.json
    proposals.db
  sources/
```

Required path work before the move:

- Introduce a small shared path helper in the moved engine code, for example `engine/paths.py`, with `code_root()`, `state_root()`, `topics_dir()`, `sources_dir()`, `activity_dir()`, and `canvas_projection_dir()` accessors.
- Convert hardcoded framework paths to the helper or `NROL_AO_STATE_DIR`. Verified hardcoded modules include:
  - `engine.py` itself: `_STATE_ROOT` fallback currently fails open to `Path(__file__).parent`; `CANVAS_TOPICS_DIR` and `LOOM_TOPICS_DIR` are projection writes at `engine.py:54-56`, used around `save_topic()`.
  - `framework/pipeline.py`: `_SOURCE_DB_PATH` is anchored to `Path(_REPO) / "sources" / "source_db.json"` and `_write_activity()` writes to `Path(_REPO).parent / "canvas" / "activity-log.json"`; after a move this can land under `a-shadow-loom/canvas/` inside OneDrive.
  - `framework/source_db.py` (`sources/source_db.json`)
  - `framework/topic_search.py` (`topics/`, `sources/source-trust.json`, cold storage)
  - `framework/extrapolation.py`, `framework/lens_calibration.py`, `framework/meta_health.py`
  - `framework/migrate_to_lr.py`, `framework/replay_indicators.py`, `framework/runner.py`
  - `framework/stamp_deadlines.py`, `framework/stamp_resolution_dates.py`
  - maintenance hooks / dashboard projections that assume repo-local topic paths
- Update `mcp_servers/nrol_ao/server.py` defaults so `NROL_AO_REPO` points to `a-shadow-loom/engine` and `NROL_AO_ACTIVITY_DIR` defaults to the external state activity directory.
- Add a startup guard and a leak test. Keep `NROL_AO_STATE_DIR` and `NROL_AO_ACTIVITY_DIR` explicit in local launch/admin config. Do not rely on fallbacks after the move; `engine.py` currently falls back to `Path(__file__).parent`, and `default_activity_dir(_repo_path())` would put `activity.jsonl` and `proposals.db` under the code root. A missing env var must fail loudly or at least warn if the resolved state/activity root is under OneDrive.

Migration rule:

1. Finish the path helper and convert hardcoded modules while code still lives in `temp-repo`.
2. Run the existing NROL-AO tests with `NROL_AO_STATE_DIR` and `NROL_AO_ACTIVITY_DIR` pointing to a copied external state directory. Add an invariant test: perform a representative `save_topic` / proposal-store open with env vars set and assert nothing is written under the engine code root.
3. Move code into `a-shadow-loom/engine`.
4. Set defaults/env to `NROL_AO_REPO=a-shadow-loom/engine` and `NROL_AO_STATE_DIR=C:\Claude-Code\NROL-AO\state`.
5. Only after a successful live scan, retire `temp-repo` as a code source.

This gives the desired consolidation (engine code versioned with Shadow Loom) without reintroducing OneDrive corruption/sync-conflict risk for the mutable topic JSON, backups, activity log, or SQLite proposal store.

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
- `read_indicator_schema(slug) → [{id, tier, desc, likelihoods, posteriorEffect?, observable?, shape?, target_hypothesis?}]`
  - This mirrors the real topic indicator JSON. Do not idealize it into top-level `direction` or `midpoint` fields. Direction, when present, lives inside `observable.direction`; `desc` and `posteriorEffect` are the fields the agent cites against.
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

1. **DONE:** Build one engine-side tool (`fetch_article`) + a minimal engine agent that uses it. Validate the tool-call round-trip end-to-end on Dream.
2. **DONE:** Add read-only topic/schema/evidence tools plus `propose_advocate`. Run advocate-only as a subagent. Confirm `analysis` carries actual multi-paragraph reasoning and cites real indicator ids.
3. **DONE:** Add rebut + jury tools/subagents (`propose_rebut`, `submit_jury`) as proposal-recording tools only. They read the advocate proposals and produce a full deliberation packet, but still do not commit, mutate topics, or move posteriors. `deliberation_agent.run_deliberation` runs the full advocate → rebut → jury packet; live-verified against the real Hormuz topic (§4.1 phase 3).
4. **Later:** Wire final jury proposals to the existing commit gates (Loom approval, governance, evidence log) — the commit path is unchanged, the engine agent calls the same gates the current tools do.
5. **Operator MCP tools flip** from "build prompt + parse" to "launch engine agent + collect trace." Old tools coexist behind a flag until the new path is trusted. Then delete the prompt-build + regex-parse code.

## A.7 Engine agent launch mechanics

**How the engine agent is spawned from the MCP server:**

- **Custom Python loop path (phase-1 default):** `mcp_servers/nrol_ao_engine/` is an importable package, and the operator MCP calls `engine_agent.py` in-process. The loop builds the OpenAI-format message list, calls a thin Dream-direct client with `tools`, dispatches returned tool calls to plain Python tool functions in `mcp_servers/nrol_ao_engine/tools/`, appends tool results, and loops until `finish_reason=stop` with no tool calls. Do not start a second MCP server process for this path.
- **Stage-scoped tool surfaces (implemented 2026-07-12):** `run_engine_agent(..., tool_names=...)` now sends only the allowed tool specs for that stage and dispatches only through that allow-list. Advocate exposes `read_indicator_schema`, `read_recent_evidence`, and `propose_advocate`; rebut exposes `read_indicator_schema` and `propose_rebut`; jury exposes `read_indicator_schema` and `submit_jury`. This is the in-process mirror-MCP behavior: Dream receives real OpenAI `tools` payloads, but each role sees a constrained tool set.
- **SDK subagent path (optional later):** if the SDK route becomes worthwhile, register `mcp_servers/nrol_ao_engine/` as an MCP server so Claude Code can discover/call its tools, then spawn a Claude Code Agent SDK subagent pointed at the shim's `ANTHROPIC_BASE_URL`. This is not the phase-1 path.
- **Concurrent scans:** the engine agent is launched per-scan by the operator MCP `run_news_scan` tool. Concurrency is bounded by the existing scan-serialization in the MCP server (today, scans run one at a time per worker). The engine agent does not introduce new concurrency — it runs within the scan's existing lifetime. The Dream sidecar is single-model; concurrent LLM calls queue at the sidecar.
- **Failure modes:** a tool-call loop that exceeds N turns (e.g. 20) without `finish_reason=stop` is aborted and recorded as a failed scan. Malformed tool arguments (schema violation) are retried once, then fail-closed. The commit gates (Loom approval, governance) are unchanged — a bad engine-agent run cannot commit evidence the current tools couldn't.

## A.8 Sequencing rationale

Kimi's review correctly noted a sequencing bias: recommending Track B first only makes sense if the QoL win is the priority. If the goal is NROL deliberation quality, **Track A phase 1 with the custom Python loop is lower risk and gives signal faster** — it validates the tool-call deliberation idea end-to-end without building the shim.

**Revised default sequencing (lower-risk first):**
0. **Phase 0.5 probe** — before building the full A.2 tool surface, run a Dream tool-call stress probe with one required long-string parameter (multi-paragraph `analysis`), one enum, and one optional numeric/id field, repeated N=20. Gate on valid JSON arguments, correct enum values, and no channel contamination. The existing Paris/weather probe is not enough for the real deliberation payload shape.
1. **Track A phase 1** — `fetch_article` tool + custom Python loop, validated against Dream. Cheapest possible validation of the core idea. No SDK, no C++ work, and no second MCP process.
2. **Track A phase 2** — DONE: multi-turn tool use, read tools, advocate-only subagent. Concrete signal on deliberation quality (§4.1 phase 2 live result).
3. **Track A phase 3** — DONE: rebut/jury proposal-recording tools and subagents (`propose_rebut`, `submit_jury`, `deliberation_agent.run_deliberation`), still no commit path. Live-verified full advocate → rebut → jury packet (§4.1 phase 3).
4. **Track A phase 4-5** — commit gates, operator MCP flip.
5. **Track B verification** — the shim already exists in the working tree; remaining work is verification/QA, not initial implementation.

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
- `max_tokens` (required in Anthropic) -> `max_tokens` unchanged. Dream's sidecar computes the needed canvases and clamps to context; do not add a hidden thought budget. Dream accepts `chat_template_kwargs.enable_thinking`, so callers can explicitly choose the Gemma4 thought-channel path or the no-thinking reference prompt.
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
Implemented in the current working tree, with different wiring than the original plan:

- Shim server: `anthropic_dream_router.py`
- Default shim port: `DREAM_SHIM_PORT` / `:8788`
- Main Loom server lifecycle: `_ensure_dream_shim_running()` in root `server.py`
- Claude launch env: `claude_client.py` sets `ANTHROPIC_BASE_URL` to the local shim for dream-routed Claude Code sessions
- Model-selector route: root `server.py` starts the shim when a dream model is selected

`admin_server.py` is not the current lifecycle owner for the shim; do not implement a duplicate admin lifecycle without first reconciling it with the main-server path.

## B.6 Relationship to Track A
- If Track B lands before Track A phase 2: the engine agent uses the Claude Code Agent SDK pointed at the shim. No custom Python loop needed. The shim is the single integration point for both operator Claude Code sessions and the NROL-AO engine agent.
- If Track A phase 2 lands first: the engine agent uses a minimal custom Python loop speaking OpenAI format directly to `:8787` (no shim). Track B then lands later as pure Claude Code QoL, and Track A can optionally migrate to the SDK-via-shim path afterward.
- The two tracks are decoupled and can proceed independently. Convergence is optional.

## B.7 Shim complexity & risk
- Mostly implemented. `anthropic_dream_router.py` contains the request translation, response translation, SSE stream shape, `/v1/models`, `/v1/messages/count_tokens`, and unconditional Dream channel-strip on returned text.
- Remaining risk is QA, not first implementation: verify the streaming contract against Claude Code, tool-use round trip, error mapping, timeout behavior, and sidecar contention under concurrent shim + engine-agent usage.
- The probe already proved Dream emits clean tool_calls on the OpenAI path, so the shim's downstream is verified for simple and multi-turn tool use. The long-argument phase-0.5 probe is still required for deliberation-shaped payloads.

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
- **Phase 0.5: PASS (implemented 2026-07-11).** `tests/test_dream_long_argument_probe.py` runs N=20 Dream tool-call requests against `:8787` with a required multi-paragraph `analysis` string (>400 chars), `verdict` enum, and optional `indicator_id`/`value` fields. Live run: all 20 returned `finish_reason=tool_calls`, valid JSON arguments, legal enum values, no `<|channel>`/`<channel|>` contamination, mean `analysis_len`=1134 chars. Skipped (module-level) when the sidecar is down so the normal suite never depends on the GPU; force with `LOOM_RUN_LIVE_DREAM_PROBE=1`. This gates the full deliberation tool surface.
- **Phase 1: PASS (implemented 2026-07-11).** `mcp_servers/nrol_ao_engine/` is an importable in-process package with a thin OpenAI tool-call client (`dream_client.py`), the `fetch_article` tool (`tools/fetch.py`, reuses the proven trafilatura+httpx fetch pattern from `server.py:_fetch_article_payload`), and a minimal tool-call loop (`engine_agent.py`, max 10 turns, retry-once-then-fail-closed on malformed JSON, no commits/mutation). Live end-to-end test (`test_engine_agent_live_round_trip_fetch_article`) round-trips fetch_article through Dream and stops. 21 mocked unit tests + 1 live test, all green.
  - **Prompt-engineering finding:** DiffusionGemma is a diffusion text model, not an instruction-tuned chat model. A verbose system prompt describing tool *availability* ("you have tools…") causes it to narrate intent or refuse ("I do not have access to a tool") instead of emitting the structured `tool_calls` object — even with `tool_choice="required"`. A terse imperative prompt ("Call fetch_article with the URL, then answer.") is reliable (4/4). `engine_agent.run_engine_agent` also accepts `force_first_tool_call=True` to send `tool_choice="required"` on turn 1 only. This is a Phase 2 prompt-engineering requirement (see §6) made concrete early.
- **Phase 2 (advocate-only): PASS (implemented 2026-07-11).** Added the reading tools (`tools/read.py`: `read_topic`, `read_indicator_schema`, `read_recent_evidence` — all read-only, project to slim dicts via `import_from_repo` + `walk_indicators`/`iter_indicators_for_topic`) and the advocate tool (`tools/advocate.py`: `propose_advocate` RECORDS a proposal in an in-memory list — never commits, no posterior movement, no topic mutation). `advocate_agent.run_advocate(slug, articles)` is a thin wrapper over `run_engine_agent` with a terse imperative system prompt and `force_first_tool_call=True`; it harvests recorded proposals keyed to the asked-for article ids and returns `{slug, proposals, trace}`. 40 mocked unit tests + 1 live test across the two test files, all green (`tests/test_nrol_ao_engine_agent.py` + `tests/test_nrol_ao_engine_agent_advocate.py`).
  - **Live result against `calibration-hormuz-reopen-2027` (real Dream :8787):** 4 turns — `read_indicator_schema` (forced turn 1) → `propose_advocate` (turn 2) → stop. Verdict `COMMIT` / `proposed_action` `OBSERVE` on `t2_transit_recovery_70pct` @ value 60. `analysis_len` = **905 chars** (gate: >400 ✓), citing indicator id `t2_transit_recovery_70pct` and all four hypothesis ids (`H1`/`H2`/`H3`/`H4`). The §4.1 phase-3 metric (analysis > 400 chars AND ≥1 cited indicator/evidence id) is met by this run. The multi-paragraph `analysis` demand lives in the `propose_advocate` tool description, NOT the system prompt — confirming the Phase-1 terse-prompt finding carries to the deliberation stage.
- **Phase 3 (rebut + jury, no commits): PASS (implemented 2026-07-11).** Added `propose_rebut` (`tools/rebut.py`) and `submit_jury` (`tools/jury.py`) as proposal-recording tools — both RECORD verdicts in in-process lists, never commit, never mutate topic state, never move posteriors. The jury's `final_action.kind` enum includes `DUPLICATE_OF` (§2.1 — the discriminator-on-`parent_idx` form the prior JSON-mode spec contradicted itself over). `deliberation_agent.run_deliberation(slug, articles)` drives the full packet: it runs `advocate_agent.run_advocate` first, then a rebut subagent with the advocate's structured proposals (full `analysis` text) injected as context, then a jury subagent with both advocate + rebut records injected. Each stage is its own `run_engine_agent` loop with a terse imperative system prompt and `force_first_tool_call=True`; the multi-paragraph / cross-reference / citation demands live in the tool descriptions, not the system prompts (Phase-1 finding carried forward). Returns `{slug, advocate_proposals, rebuttals, jury_verdicts, traces}`. 70 mocked unit tests across the three test files, all green (`test_nrol_ao_engine_agent.py` + `test_nrol_ao_engine_agent_advocate.py` + `test_nrol_ao_engine_agent_deliberation.py`, `-k "not live"`); 1 live test, green. The no-mutation safety is enforced by an AST-level test (`test_phase3_modules_do_not_import_or_call_forbidden_commit_symbols`) that parses each phase-3 module and asserts it neither imports nor calls `process_evidence` / `save_topic` / `commit_match` / `propose_match` / `submit_transition` / `fire_indicator` / `observe_indicator` / `write_evidence` — a raw-substring grep was rejected because the module docstrings legitimately mention those names as safety documentation.
  - **Live result against `calibration-hormuz-reopen-2027` (real Dream :8787):** full advocate → rebut → jury packet in ~16-29s. Advocate: 3 turns, `analysis_len`=**1148 chars** (gate >400 ✓), verdict `COMMIT` / `OBSERVE` on `t2_transit_recovery_70pct` @ value 60, citing `t2_transit_recovery_70pct`, `t1_transit_below_25pct_3mo`, `H1`, `H2`. Rebut: 4 turns, `rebuttal_analysis_len`=**479 chars** (gate >300 ✓), verdict `COMMIT`, objection_raised=False, referencing advocate proposal `adv_96778700` and citing `t2_transit_recovery_70pct`. Jury: 3 turns, `jury_rationale_len`=**616 chars** (gate >300 ✓), `final_action` `OBSERVE` on `t2_transit_recovery_70pct` @ value 60, referencing BOTH `adv_96778700` and `reb_641317e6`. All three records chain: the jury verdict's `advocate_proposal_id`/`rebuttal_id` match the harvested ids. **No topic JSON mtime/size change, no proposal DB writes** (verified before/after).
  - **Phase-3 robustness fix:** the harvest filters in `run_advocate` / `_run_rebut` / `_run_jury` now accept a proposal whose `article_id` is the asked article's URL, not just the `article_id` — DiffusionGemma (non-deterministic even at temp 0.2) sometimes uses the URL as the `article_id` in its tool call because the prompt shows both `[A1]` and a `url:` line. Without this, a legitimate proposal was silently filtered out (the live test hit this on the first runs). Locked in by `test_run_advocate_accepts_url_as_article_id`; a genuinely unknown id is still dropped.
- **Phase 4 (review bridge, no commits): PASS (implemented 2026-07-15).** Added `file_engine_deliberation_proposals(slug, articles, deliberation_packet, dry_run=true)` on the operator MCP. It maps engine-agent jury `final_action` records into the existing pending proposal lifecycle (`submit_article` / proposal store / later `commit_match`) without moving posteriors or mutating topic JSON. Default is dry-run preview. `dry_run=false` files pending proposals only; `IGNORE` and `DUPLICATE_OF` jury actions are reported as skipped because they are not commit proposals. Focused verification: 3 new bridge tests plus an existing commit-gate regression, all green.
- **Phase 5 (default scan integration): PASS (implemented 2026-07-15, flipped 2026-07-15, chunked 2026-07-15).** `run_news_scan(...)` launches the engine-agent deliberation path over the scan's deduped articles by default and stores the packet under `engine_deliberation` in each topic packet. `engine_file_proposals=true` is also default; it routes each completed chunk through the Phase 4 review bridge and files pending proposals only when `dry_run=false` and `commit_policy="safe"`, otherwise it returns a dry-run preview. `engine_max_articles` is the internal Dream deliberation chunk size (default 2), not a manual operator cap; one scan call drains the window chunk-by-chunk. Completed chunks file proposals immediately and progress is reported under `engine_coverage` / `engine_progress`. If a multi-article chunk fails, the server splits and retries smaller chunks so one bad batch does not block the tail. If an individual article still fails, `engine_coverage.failed_offsets` records it, `next_offset` points at the first failed/pending article, and `lastScanned` is not stamped. The filing bridge suppresses identical pending duplicate proposals. A cooperative `engine_time_budget_sec` returns partial progress before an outer harness timeout kills the tool call. The legacy matcher/debate path is now opt-in via `legacy_matcher=true` for parity checks or rollback.

### 4.2 Track B
- Shim serves `POST /v1/messages` (non-streaming) and returns a valid Anthropic response for a text prompt → Claude Code can complete against Dream. Existing tests in `tests/test_anthropic_dream_router.py` and `tests/test_dream_claude_routing.py` should be mapped against this checklist before adding new test coverage.
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

These are prompt-engineering requirements on the new architecture, not afterthoughts. Track A verification (§4.1) checks for them concretely: advocate analysis >400 chars with real indicator/evidence citations, rebuttal referencing advocate claims, and jury referencing both prior rounds.

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
- `mcp_servers/nrol_ao/llama.py` — `chat()` (212), `resolve_backend()` (135), `_split_channel_scaffold()` (27-55, the strip that becomes irrelevant on the tool path and gets fixed-at-shim on the text path), `dream_host()`/`llama_host()` (76-106)
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
- `project_nrol_dream_feature_complete` was stale during the initial investigation: the `<|channel>thought` strip was not fixed then because `text = stripped_content` was guarded by `if extracted_reasoning`. In the current working tree, `mcp_servers/nrol_ao/llama.py` now assigns `text = stripped_content` unconditionally for `backend == "dream"`, so the empty-thought edge case is fixed in the sync nrol client as well as at the shim layer.
- `project_nrol_dream_feature_complete` claimed "port default :18081→:8787." The llama default is `:8000` (`llama.py:82`); `:18081` lingers only as a stale default in `config.py:126` and `server.py:6513`'s fallback. The dream default is `:8787` (`llama.py:104`), matching the persisted `config.json`.

---

## Implementation status

**Partially implemented.** Track B's shim exists in the working tree (`anthropic_dream_router.py`) with Loom launch wiring and tests. **Track A Phase 0.5 + Phase 1 are implemented (2026-07-11):** the long-argument Dream tool-call probe (`tests/test_dream_long_argument_probe.py`, PASS) and the `mcp_servers/nrol_ao_engine/` package (in-process, `fetch_article` tool + minimal tool-call loop, live end-to-end verified against Dream) are in the working tree. **Track A Phase 2 (advocate-only) is implemented (2026-07-11):** reading tools + `propose_advocate` (records, never commits) + `run_advocate` runner, live-verified against the real Hormuz topic (905-char analysis citing `t2_transit_recovery_70pct` + H1–H4; §4.1 phase-3 gate met). **Track A Phase 3 (rebut + jury, no commits) is implemented (2026-07-11):** `propose_rebut` + `submit_jury` proposal-recording tools and `deliberation_agent.run_deliberation` (full advocate → rebut → jury packet), live-verified against the real Hormuz topic (1148-char advocate analysis, 479-char rebuttal referencing the advocate proposal id, 616-char jury rationale referencing both advocate + rebut ids; §4.1 phase 3). **Track A Phase 4 and Phase 5 scan integration are implemented (2026-07-15), review-first by default.** The engine-agent scan path is now the `run_news_scan` default; the legacy line-format matcher/debate path is opt-in via `legacy_matcher=true`. Engine code/state consolidation (§0.7) is not implemented yet.

**Stage-scoped Dream tool surfaces are implemented (2026-07-12):** the engine loop now accepts `tool_names`, sends only that stage's OpenAI `tools` payload, and dispatches only through that allow-list. Advocate exposes `read_indicator_schema`, `read_recent_evidence`, and `propose_advocate`; rebut exposes `read_indicator_schema` and `propose_rebut`; jury exposes `read_indicator_schema` and `submit_jury`. Targeted non-live verification is 73 passed / 3 live deselected across the engine-agent, advocate, and deliberation suites.

**Phase 4 review bridge is implemented (2026-07-15):** `file_engine_deliberation_proposals` previews or files engine-agent jury outputs into the existing pending proposal queue. It does not commit, does not move posteriors, and does not mutate topic JSON; `commit_match` remains the only path from a filed proposal to evidence/posterior movement.

**Phase 5 scan integration is implemented (2026-07-15):** `run_news_scan` accepts `engine_deliberation`, `engine_file_proposals`, `engine_max_articles`, `engine_article_offset`, and `legacy_matcher`. The engine path is default with bounded internal chunks (`engine_max_articles=2` by default), legacy matcher/debate is opt-in for parity, and engine jury outputs can file only as pending review proposals. One scan call processes chunks serially and files proposals after each completed chunk. If a chunk fails, `engine_coverage.deferred_after` is nonzero, `engine_coverage.next_offset` tells the operator what offset to resume from, and `lastScanned` is not stamped.

**Recommended next concrete step:** run parity scans on a known corpus with default engine scans versus `legacy_matcher=true` scans, then compare proposed actions and rationale quality. Keep legacy matcher/debate available as an explicit rollback/parity mode until engine outputs and pending proposals match operational expectations.

**Before any full engine-code move:** finish the path-normalization work in §0.7. The safe end state is engine code in `a-shadow-loom/engine` and hot state outside OneDrive via `NROL_AO_STATE_DIR`, not topic JSON and logs inside the Shadow Loom repo. Track B (the Anthropic shim, QoL win) is pursued separately/asynchronously and is not a prerequisite for Track A.

**Operational risks carried forward from red-team review:** keep `mcp_servers/nrol_ao_engine/` in-process as a package for phase 1 to avoid a second writer process; add audit trace format/rotation/read caps before storing multi-paragraph tool traces; treat Dream sidecar contention as real because shim sessions and engine-agent scans share the same model server; keep `fire_indicator` / `observe_indicator` proposal-producing or approval-gated until parity is proven.

**Kimi review addressed (2026-07-06):** repo map added (§0.5); multi-turn tool-use probe added to §2.2 (retires the single-probe concern — Dream handles multi-turn tool use cleanly, and text generation after a tool turn is also free of channel contamination); known limitations / out-of-scope problems table added (§5); system-prompt + tool-description requirements added (§6); engine agent launch mechanics added (§A.7); sequencing bias corrected — Track A phase 1 with custom Python loop is now the default first step (§A.8); line-number drift flagged in §7.2; verification metric made concrete (§4.1 phase 3: analysis > 400 chars + at least one cited indicator/evidence id).
