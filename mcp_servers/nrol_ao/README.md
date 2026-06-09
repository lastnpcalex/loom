# NROL-AO MCP Server

This module exposes the NROL-AO engine to Loom as a narrow MCP facade.

It does not accept target posteriors or ad hoc likelihoods. Mutating tools route
through the source repo's `framework.pipeline.process_evidence`,
`framework.pipeline.apply_observation`, or schema-gap queue path.

## Configure

Set `NROL_AO_REPO` if the repo is not at the default path:

```powershell
$env:NROL_AO_REPO = "C:\Claude-Code\NROL-AO\temp-repo"
python -m mcp_servers.nrol_ao.server
```

Register with Claude Code:

```powershell
claude mcp add --scope user --transport stdio nrol-ao -- python <path-to-loom-repo>\nrol_ao_mcp_server.py
```

When launched from Loom, mutating calls use Loom's permission endpoint
(`LOOM_CONV_ID` and `LOOM_PORT` are injected per conversation by
`claude_client.py`). Commits are **fail-closed**: without `LOOM_CONV_ID`
there is no human to approve, so `commit=true` is denied unless
`NROL_AO_ALLOW_UNGATED_COMMITS=1` is set explicitly (headless/dev only).

The MCP interface is model-agnostic. Cloud/provider-side reasoning can be done
by Claude, Codex, agy, or another model by using the `build_*` prompt tools and
returning the model output through the `apply_*` tools.

Local direct execution is available through llama-server's OpenAI-compatible
`/v1/chat/completions` endpoint. The server reads Loom's `config.json` for
`llama_host` / `llama_model`, with optional overrides:

```powershell
$env:NROL_AO_LLAMA_HOST = "http://localhost:8000"
$env:NROL_AO_LLAMA_MODEL = "Qwen3.6-27B-NVFP4.gguf"
```

Activity is written to:

```text
<NROL_AO_REPO>\loom\mcp_activity\snapshot.json
<NROL_AO_REPO>\loom\mcp_activity\activity.jsonl
```

Override with `NROL_AO_ACTIVITY_DIR` if the dashboard should read another path.

## Tools

- `nrol_status`
- `help`
- `list_topics`
- `list_hypotheses`
- `read_topic`
- `topic_status`
- `build_news_scan_plan`
- `run_news_scan`
- `apply_news_scan_results`
- `triage_headline`
- `build_matcher_prompt`
- `parse_matcher_output`
- `apply_matcher_output`
- `llama_server_status`
- `model_endpoint_status`
- `list_activity`
- `run_matcher_with_model`
- `run_matcher_with_llama`
- `submit_transition`

Runtime transitions accepted by `submit_transition` are `PARK`, `FIRE`,
`OBSERVE`, `SCHEMA_GAP`, and `IGNORE`.

`run_matcher_with_model` is the provider-neutral entry point. With
`provider=llama` or `provider=local`, it builds the repository's matcher prompt,
sends it to llama-server, parses the response, records the job lifecycle for
dashboard monitoring, and only mutates topic JSON when called with
`commit=true`. With other providers, it records the job and returns the matcher
prompt plus the follow-up `apply_matcher_output` handoff.

For news refreshes, prefer `run_news_scan`. It is the MCP-side worker path:
the server selects stale topics, performs web search, dedupes articles, runs
matcher deliberation through the configured local model endpoint, and returns
an operator packet. `build_news_scan_plan` and `apply_news_scan_results` remain
debug/manual override tools.

`run_news_scan` separates scan coverage from evidence mutation:

- `dry_run=false` records successful scan coverage by updating
  `topic.meta.lastScanned`, even when `commit=false`.
- `dry_run=true` performs the scan preview without stamping `lastScanned`.
- `commit=true` is the separate evidence/posterior mutation path and still
  requires Loom approval.
