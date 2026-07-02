# LLM Token Budget (empty-REVISE / blank rationale)

Every LLM-backed tool (`red_team_schema_extension_proposal`,
`red_team_search_query_update`, `run_schema_gap_resolver`,
`red_team_topic`, `run_matcher_with_llama`, `review_duplicate_candidate`,
`review_parked`, `deliberate_candidates`, `future_cast`, the resolution
after-action review) takes a `max_tokens` parameter (default 4096). **This is
the reasoning budget.** You can and should raise it per-call when a
deliberation runs hot.

## The model and the failure mode

The local model (Qwen3.6) is a reasoning model: it generates `reasoning_content`
(the thinking channel) and `message.content` (the answer) from one shared
`max_tokens` budget, sequentially — think first, then answer. If thinking runs
long it hits the budget mid-thought (`finish_reason=length`) and the answer is
never emitted — `message.content` comes back **empty**.

The symptom is distinctive and easy to misread: **the tool returns a verdict
(often `REVISE`) with blank `risk` / `directionality` / `recommendation` and an
empty `raw` field.** That is *not* a terse review — it is the parser's default
for "the model returned nothing." Do not re-file or iterate on a blank-rationale
REVISE; the model didn't actually review anything.

## When to raise `max_tokens`

Pass `max_tokens=8192` (or higher) when:

- A review comes back with an empty `raw` / blank rationale fields (the
  tell-tale). This is a reasoning-budget exhaustion, not a substantive verdict.
- You're red-teaming a proposal on a large topic (many indicators → bigger
  prompt → longer thinking). Hormuz (20 indicators) needs ~4-8k for the
  schema red-team; bigger topics more.
- You're running a deliberation-heavy call (advocate/rebut/jury, full news
  scans, parked-evidence review) — these run hotter than bounded reviews.
- You see `finish_reason: "length"` in the response or activity ledger.

The tool's NO_ANSWER_EMITTED guard (on the schema/search-query red-team and the
cross-day duplicate judge) catches the empty case explicitly — if you see
`risk: "NO_ANSWER_EMITTED"`, that is the signal to rerun with a higher
`max_tokens`, not to re-file the proposal.

## Trade-off

Higher `max_tokens` = more room for thinking + answer, but longer latency (the
local model runs ~50-100 tok/s). At 8192 a hot deliberation can take a few
minutes; at 16384, longer. There is no correctness risk to raising it — only
time. If you have the time and a call is running out of budget, raise it. The
default of 4096 is a floor for routine calls, not a ceiling.

## What is *not* parameterized

A few internal paths (`future_cast`, the resolution after-action review) do not
expose `max_tokens` and use the client default (2048). If those return empty,
you cannot bump them per-call — that requires a code change (raise the client
default in `llama.py` or add the parameter to the tool). Report it rather than
chasing the failure.
