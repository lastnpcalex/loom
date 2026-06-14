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
- `list_scan_runs`
- `read_scan_run`
- `replay_scan_run`
- `undo_scan_run`
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
- `submit_article`
- `propose_match`
- `commit_match`
- `list_proposals`
- `withdraw_proposal`

Runtime transitions accepted by `submit_transition` are `PARK`, `FIRE`,
`OBSERVE`, `SCHEMA_GAP`, and `IGNORE`.

`run_matcher_with_model` is the provider-neutral entry point. With
`provider=llama` or `provider=local`, it builds the repository's matcher prompt,
sends it to llama-server, parses the response, records the job lifecycle for
dashboard monitoring, and only mutates topic JSON when called with
`commit=true`. With other providers, it records the job and returns the matcher
prompt plus the follow-up `apply_matcher_output` handoff.

For news refreshes, prefer `run_news_scan`. It is the MCP-side worker path:
the server selects stale topics, performs web search, strips tracker query
parameters for duplicate detection, dedupes articles, fetches readable article
text and best-effort publication metadata, freshness-gates matcher input, runs
matcher deliberation through the configured local model endpoint, and returns
an operator packet. `build_news_scan_plan` and `apply_news_scan_results` remain
debug/manual override tools.

`run_news_scan` separates scan coverage from evidence mutation:

- `dry_run=false` records successful scan coverage by updating
  `topic.meta.lastScanned`, even when `commit=false`.
- `dry_run=true` performs the scan preview without stamping `lastScanned`.
- `commit_policy="safe"` is the review-first scan policy. PARK/SCHEMA_GAP may
  auto-apply because they cannot move posteriors; FIRE/OBSERVE are filed as
  pending proposals with deliberation attached. This remains true even if
  `commit=true` is accidentally supplied.
- Without `commit_policy="safe"`, `commit=true` is the direct evidence/posterior
  mutation path and still requires Loom approval.

Freshness rules are part of scan safety. Dated articles older than the adaptive
scan window are dropped. Search results with no date may still be used as
context, but if full-article metadata reveals an old publication date they are
dropped before matching. Undated FIRE/OBSERVE candidates are downgraded to PARK
instead of proposal filing.

Parked review accounting is intentionally retained. `flaggedForIndicatorReview`
and `parkedTotal` are the archived parked-evidence corpus, not the active work
queue. The operational queue is `parkedReviewDebt.dueCount`. A withdrawn
proposal preserves the proposal decision but does not delete or unflag the
original evidence row; use `acknowledge_parked_reviews` to timestamp already
reviewed due items without re-running the matcher.

`undo_scan_run` is a ledger cleanup tool for dirty scan activity/digest records.
It defaults to dry-run and can match by `job_id`, `slug`, or
`min_article_count`. It does not roll back topic evidence, pending proposals,
posteriors, or `lastScanned`.

Schema-gap resolution is also review-first. `run_schema_gap_resolver` can draft
schema-extension proposals, but every proposal must pass
`red_team_schema_extension_proposal` with verdict `APPROVE` before it can be
marked approved or applied. `apply_schema_extension_proposal` changes schema
only; it never replays evidence or moves posteriors.
