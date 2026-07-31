# llama-diffusion-gemma-server (vendored snapshot)

**What this is:** an OpenAI-compatible HTTP server for block-diffusion models
(DiffusionGemma), vendored here as a record of the exact source Loom launches
and depends on. The canonical build/repo lives at
`C:\tmp\llama-diffusion-gemma-pr` (a llama.cpp fork); this folder is a **read-only
snapshot** so the Loom git history always carries the server's source — even if
the upstream checkout is moved, rebuilt, or wiped.

## Why it's vendored

Loom's Dream Space (Hermes-on-DiffusionGemma agent) and Weave (OODA passes) both
talk to this server's `/v1/chat/completions` endpoint on `127.0.0.1:8787`. A
2026-07-07 regression (`tool_turns=0` across every dream turn) was caused by Loom
launching a **different** server — the Python `agent.openai_server` adapter, which
is tool-blind. The C++ server is the tool-aware one. Vendoring the source makes
that distinction auditable from the Loom repo itself, and pins the behavior Loom
relies on (native `tools=` + `tool_calls`).

## What it does (from the source header)

> OpenAI-compatible HTTP server for block-diffusion models (diffusion-gemma).
> This is the llama-server analogue for the diffusion family: it loads a
> block-diffusion model once and serves the same denoising generation loop as
> diffusion-gemma-cli over HTTP, exposing the OpenAI endpoints
> (`/v1/chat/completions`, `/v1/completions`, `/v1/models`) plus the llama-server
> observability surface (`/health`, `/v1/health`, `/props`, `/metrics`, `/slots`).

Generation is **not autoregressive**. Each request denoises one or more
256-token "canvases" against a cached prompt prefix (see `diffusion-gemma-cli.cpp`
upstream for the full denoising description). A single `llama_context` is reused
across requests and is not thread-safe, so generation is serialized behind a
mutex (one slot); the HTTP layer still accepts many connections concurrently.

### Endpoints

| Endpoint | Behavior |
|---|---|
| `POST /v1/chat/completions` | OpenAI chat completions. Accepts `messages`, `tools`, `tool_choice`, `stream`, `max_tokens`, etc. |
| `POST /v1/completions` | Plain completions. |
| `GET /v1/models` | Lists the loaded model (id only — the loaded GGUF). |
| `GET /health`, `/v1/health` | Returns **only** `{"status":"ok"}` (bare — no model/ctx metadata). |
| `GET /props`, `/metrics`, `/slots` | Observability (llama-server-style). |

## Tool calls — how they actually work

This server has **native OpenAI tool-calling support** (live-proven
2026-07-07): send a request with `tools=[...]` + `tool_choice`, and it returns a
structured `tool_calls` array with `finish_reason: "tool_calls"`.

The flow (line numbers refer to `diffusion-gemma-server.cpp` in this folder):

1. **Request parsing** — `format_chat_request` (line ~222) reads `tools` and
   `tool_choice` from the OpenAI body via `common_chat_tools_parse_oaicompat`
   and `common_chat_tool_choice_parse_oaicompat`, then applies the chat template
   (`common_chat_templates_apply`) so the tools are rendered into the prompt the
   model sees. `chat_template_kwargs.enable_thinking` can enable Gemma4's
   thought channel per request; otherwise the server default follows
   `DREAM_ENABLE_THINKING` and falls back to the no-thinking reference prompt.

2. **Generation** — `generate` (line ~268) runs the block-diffusion denoising
   loop. `max_tokens` maps to canvas blocks via `_blocks_for` (256 tokens/block,
   ceil division, capped at 64). The model emits text — possibly including
   `<|tool_call>call:name{json}<tool_call|>` markup.

3. **Response parsing** — `parse_chat_answer_oaicompat` (line ~796) calls
   `common_chat_parse` with `parse_tool_calls = true`. If that finds nothing,
   `parse_diffusion_gemma_text_tool_calls` (line ~767) is a **regex fallback**
   that matches the model's native `<|tool_call>call:name{json}<tool_call|>`
   format and lifts it into `msg.tool_calls`.

4. **Emission** — `finish_reason` is `"tool_calls"` if `msg.tool_calls` is
   non-empty, else `"stop"` (line ~1375). The response `message` carries
   `tool_calls` (structured) with empty `content`.

**Verified live** (2026-07-07): a request with `tools=[read_file]` +
`tool_choice:"required"` returned
`{"finish_reason":"tool_calls","tool_calls":[{"type":"function","function":{"name":"read_file","arguments":"{\"path\":\"/tmp/test.txt\"}"},"id":"call-0"}],"content":""}`.

> Note on the regex: `parse_diffusion_gemma_text_tool_calls` uses the name char
> class `[A-Za-z0-9_.-]` — it does **not** include `:`. A tool name like
> `hermes:read_file` will not match. Keep tool names to the supported charset.

## How Loom launches it

`admin_server.py:_dream_cmd()` builds:

```
cd /d "<dream_cwd>" && "<dream_server_exe>" -m "<dream_model_path>" --host 127.0.0.1 --port 8787
```

Config (from `config.json` / `config.py`):
- `dream_server_exe` → `C:\tmp\llama-diffusion-gemma-pr\build\bin\llama-diffusion-gemma-server.exe`
- `dream_model_path` → `...\llama-diffusion\models\diffusiongemma-26b-a4b-it-nvfp4.gguf`
- `dream_host` → `http://127.0.0.1:8787` (must be IPv4 — see "Localhost trap" below)
- `dream_context_size` → 131072

The C++ server loads the GGUF **at startup** (no JIT), so the first request after
start pays a ~30-60s cold load; `/health` only answers once the model is resident.
Loom's `dream-start` readiness poll waits up to 120s. The idle-watcher
(`dream_idle_timeout_min`) taskkills the process to free VRAM + RAM when idle.

## Build

Built from the canonical repo, not from this vendored snapshot (the snapshot is
for reference/audit, not to compile against):

```
cd C:\tmp\llama-diffusion-gemma-pr
cmake -B build -DGGML_CUDA=ON ...
cmake --build build --target llama-diffusion-gemma-server
```

Output: `build/bin/llama-diffusion-gemma-server.exe` (~608 KB).

There is a second exe, `llama-diffusion-gemma-server-toolcalls.exe` — despite the
name, **both built exes have the same tool support** (the tool-parsing code is
in the shared source). The `-toolcalls` suffix is a build-target artifact, not a
feature flag.

## The localhost trap (recurs)

Windows resolves `localhost` IPv6-first; this server listens on IPv4 only. Any
client URL using `localhost` pays a ~2s/turn `::1` connect fallback. Loom
rewrites `//localhost → //127.0.0.1` in **two** places — keep them in lockstep:
- `_dream_openai_base_url()` in `server.py` (Hermes path)
- `_chat_host_for_model()` in `llama_client.py` (Weave path)

`config.py:dream_host` default is `http://127.0.0.1:8787`. Never default it back
to `localhost:18081` — `18081` is a dead legacy port with no git history.

## Provenance / sync

- **Snapshot date:** 2026-07-07
- **Upstream:** `C:\tmp\llama-diffusion-gemma-pr\examples\diffusion-gemma\`
- **Built exe mtime at snapshot:** 2026-07-05 10:05 (the running build)
- **Upstream git head:** `dd0cf04 fix chat template issues impacting the mean
  denoising steps in GGUF...`
- **Upstream uncommitted:** the upstream working tree has edits to
  `diffusion-gemma-server.cpp` on top of the build (the tool-parsing code may be
  partially uncommitted there). The vendored file here matches the **built exe's
  behavior**, confirmed by the live tool-calls probe.

To re-sync this snapshot after an upstream rebuild:
```
cp C:\tmp\llama-diffusion-gemma-pr\examples\diffusion-gemma\diffusion-gemma-server.cpp \
   vendored\diffusion-gemma-server\diffusion-gemma-server.cpp
```
and update the snapshot date + built-exe mtime above.

## Related

- `HERMES_PROMETHEUS_DESIGN.md` (repo root) — Dream Space architecture
- `findings-weave-ooda-map.md` (repo root) — Weave OODA path that also hits this server
- Memory: `project_dream_space_agent_latency`, `project_hermes_toolcall_hallucination_nudge`
