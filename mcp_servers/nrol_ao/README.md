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

State & status: `nrol_status`, `help`, `list_topics`, `list_hypotheses`,
`read_topic`, `topic_status`, `read_evidence`, `list_activity`,
`llama_server_status`, `model_endpoint_status`, `latest_digest`.

Public surface: `publish_black_hole_snapshot` — regenerate the sanitized
`surfaces/nrol-ao/data.json` snapshot in the black-hole repo from current topic
state, and optionally `git add`/commit/push to `master` so the live dashboard
updates. `commit=False,push=False` (default) is a purely programmatic
regenerate-only refresh (no git mutation, no gate); `push=True` goes through the
Loom approval gate (fail-closed — it publishes to the public site). Only ever
stages `surfaces/nrol-ao/data.json`. This is the server's only subprocess
(`git`) entry point; config the repo path with `NROL_AO_BLACK_HOLE_REPO`.

Topic design & lifecycle: `design_topic` (draft + dynamics sidecar),
`activate_topic` (human-gated; requires a lint-clean dynamics spec),
`red_team_topic` (mandatory DRAFT design review), `resolve_topic` (set
RESOLVED + record outcome + two-lane Brier + after-action review),
`resolution_brier` (read-only post-hoc two-lane Brier).

Calibration & shadow (guide, not authority — never move posteriors):
`shadow_posteriors` (dynamics-derived first-passage posteriors; `asof`
counterfactual), `future_cast` + store companions `list_future_casts`,
`get_future_cast`, `save_future_cast`, `withdraw_future_cast` (dry-run
hypothetical-event analysis; saved casts outside topic state).

News scans & matching: `build_news_scan_plan`, `run_news_scan`,
`list_scan_runs`, `read_scan_run`, `replay_scan_run`, `undo_scan_run`,
`apply_news_scan_results`, `build_matcher_prompt`, `parse_matcher_output`,
`apply_matcher_output`, `run_matcher_with_model`, `run_matcher_with_llama`,
`deliberate_candidates`, `review_duplicate_candidate`, `triage_headline`
(`save=true` logs to `loom/triage_log/`).

Typed transitions & proposals: `submit_transition` (`PARK`/`FIRE`/`OBSERVE`/
`SCHEMA_GAP`/`IGNORE`), `submit_article`, `propose_match`, `commit_match`,
`list_proposals`, `withdraw_proposal`, `review_parked`,
`acknowledge_parked_reviews`.

Schema governance: `list_schema_gaps`, `run_schema_gap_resolver`,
`list_schema_extension_proposals`, `propose_schema_extension` (file a
hand-authored indicator proposal into the review queue — the operator path for
window-specific indicators the resolver cannot draft),
`red_team_schema_extension_proposal`, `mark_schema_extension_proposal`,
`apply_schema_extension_proposal` (now accepts `anti_indicators` as a tier; the
engine inversion lint validates LR direction at apply).

Search-query governance: `read_search_queries`,
`propose_search_query_update`, `red_team_search_query_update`,
`list_search_query_updates`, `apply_search_query_update`,
`withdraw_search_query_update`.

Source trust (LIVE trust ledger, read-only — NOT a Brier score):
`source_calibration_status`, `source_profile`, `validate_source_db`,
`source_domain_patterns`.

Social-media-user Brier (per-handle forecast calibration, greenfield):
`log_social_forecast`, `social_user_brier`, `list_social_handles`.

Runtime transitions accepted by `submit_transition` are `PARK`, `FIRE`,
`OBSERVE`, `SCHEMA_GAP`, and `IGNORE`.

`run_matcher_with_model` is the provider-neutral entry point. With
`provider=llama` or `provider=local`, it builds the repository's matcher prompt,
sends it to llama-server, parses the response, records the job lifecycle for
dashboard monitoring, and only mutates topic JSON when called with
`commit=true`. With other providers, it records the job and returns the matcher
prompt plus the follow-up `apply_matcher_output` handoff.

Every LLM-job tool that takes a `model` argument also accepts `model="dream"`
(or `model="dream:<id>"`) to route the job through the Dream Engine — the
DiffusionGemma OpenAI-compatible sidecar on `dream_host` (default `:8787`) —
instead of llama-server. Setting `NROL_AO_LLM_BACKEND=dream` flips the default
backend for all jobs without per-call arguments. `model_endpoint_status`
reports both endpoints and the active default; `chat` job responses carry a
`backend` field so activity records show where a deliberation actually ran.

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

`propose_schema_extension` files a hand-authored indicator proposal directly
into the same review queue — the operator path for window-specific indicators
that require human causal reasoning (e.g. a declaratory anti-indicator tied to
a specific date band) which the resolver, an LLM over gap clusters, does not
produce. It validates shape early (mirroring apply's gates), synthesizes the
YAML-ish `body` string apply re-parses, and is a zero-authority queue append
(no indicator mutation, no posterior movement, no gate) — the same tier as the
resolver's persist step. `apply_schema_extension_proposal` now accepts
`anti_indicators` as a tier; the engine's inversion lint
(`_check_anti_indicator_inversion`) validates at apply that an anti-indicator's
target hypothesis carries the lowest LR (so firing suppresses, not lifts — the
dangerous direction is blocked). The red-team prompt is sharpened to flag
wrong inversion for anti-indicators too.
