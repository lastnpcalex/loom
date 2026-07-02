# Tool Reference (per-tool footguns and authoring detail)

This is the deep reference for the tools in the lean OPERATOR.md's Tool Inventory.
The lean prompt lists tool names grouped by purpose; this doc holds the per-tool
usage guidance, footguns, and semantics that are too detailed for the always-on
prompt.

## What You Can Do

- Inspect state: `nrol_status`, `topic_status`, `list_topics`,
  `list_hypotheses`, `read_topic`, `list_activity`.
- Triage new information: `triage_headline` first, always, before anything
  else. Do not invent relevance. If it does not match, it does not match.
- Run scans: `run_news_scan` is the operational path for server-side search,
  dedupe, matcher extraction, and model deliberation. **Pass `brief=true`**
  in operator mode — the full packet (articles, excerpts, deliberation) is
  large and, combined with a `digest_path` you can't reach without file
  tools, is a common trigger for sandbox break-out attempts. `brief=true`
  returns a compact summary (decision counts by kind, proposals filed,
  freshness downgrades, scan coverage) plus read-back pointers
  (`read_scan_run` / `latest_digest` / the dashboard) — brief the human from
  the compact form and act on the proposal queue; the full packet stays on
  disk in the digest for review.
- Govern durable search coverage through MCP:
  `read_search_queries`, `propose_search_query_update`,
  `red_team_search_query_update`, `list_search_query_updates`,
  `apply_search_query_update`, and `withdraw_search_query_update`. Query
  updates change retrieval metadata only; they never move posteriors.
- Run deliberation explicitly when needed:
  `deliberate_candidates(slug, articles, output_text)` runs the visible
  advocate/rebut/jury pass over matcher DECISION blocks without mutating
  state. Use it before filing a manual posterior-moving proposal.
  **Footgun:** `output_text` must be matcher DECISION blocks in the exact
  shape `build_matcher_prompt` emits — the `ARTICLE:` key matches the
  prompt's `## Articles to evaluate` labels (e.g. `A1`), NOT
  `submit_article`'s `article_id` (e.g. `art-8d7b…`). Always generate the
  prompt with `build_matcher_prompt` first and copy its DECISION-block shape,
  or prefer `run_matcher_with_llama(commit=false)` which emits correctly
  formatted output server-side. A hand-written block with the wrong key
  returns empty with no error — don't diagnose that as "parser rejected"
  without reading `parse_matcher_output` first.
- Review cross-day duplicates explicitly:
  `review_duplicate_candidate(slug, article, decision, ...)` compares a
  candidate FIRE/OBSERVE against recent evidence and returns a typed
  duplicate judgment. Use it when a proposal may be a re-reporting of an
  already counted event.
- Work schema gaps through the reviewed workflow:
  `list_schema_gaps`, `run_schema_gap_resolver`, `list_schema_extension_proposals`,
  `red_team_schema_extension_proposal`, `mark_schema_extension_proposal`, then
  `apply_schema_extension_proposal` only after red-team APPROVE and operator
  approval. Red-team review is mandatory for every schema extension. Applying a
  schema extension changes schema only; it never replays evidence or moves
  posteriors.
- `propose_schema_extension` files a **hand-authored** indicator proposal into the
  same review queue — use it when the resolver cannot draft the indicator you
  need (e.g. a window-specific declaratory anti-indicator requiring causal
  reasoning about a date band). It is a zero-authority queue append; the same
  red-team → mark-approved → apply lifecycle applies. For anti-indicators pass
  `tier="anti_indicators"` and `target_hypothesis` (single H or list) — the
  engine inversion lint validates at apply that the targeted H carries the
  lowest LR, blocking the dangerous direction (firing lifts the target instead
  of suppressing it).
- `publish_black_hole_snapshot` regenerates the public dashboard snapshot. With
  no args it refreshes the local `surfaces/nrol-ao/data.json` (programmatic, no
  git, no gate). `commit=True, push=True` stages only `data.json` and pushes to
  `master` so the live site updates — `push` goes through the Loom approval gate.
- Replay stored scans:
  `list_scan_runs`, `read_scan_run`, and `replay_scan_run` can inspect or
  replay a digest in dry-run, proposal-only, or safe-apply mode.
- Undo dirty scan ledger records:
  `undo_scan_run` removes MCP activity/digest records for a bad scan run
  by `job_id`, `slug`, or article-count threshold. It does not roll back
  topic evidence, proposals, posteriors, or `lastScanned`.
- Submit typed transitions via `submit_transition`:
  - `PARK`: relevant but no matching indicator. No posterior movement.
  - `FIRE`: a pre-committed binary indicator's threshold is met.
  - `OBSERVE`: a numeric value for an indicator with an observable block.
  - `SCHEMA_GAP`: relevant evidence the schema cannot express; queues review.
  - `IGNORE`: not relevant. Writes nothing.

## Anti-indicators

**Anti-indicators** are directional indicators whose likelihoods are
*authored* to suppress a target hypothesis (the target H carries the lowest
LR). They are not a distinct posterior semantic: firing one moves posteriors
through the same `bayesian_update` path as any tier indicator, with the LRs
applied verbatim — "anti" is a design-time authoring convention, not a
runtime inversion. In scan briefs (`brief=true`) an anti-indicator FIRE is
tallied as `ANTI_FIRE` (not `FIRE`) so you read it correctly: it is
*falsification* evidence against its target hypothesis, not
hypothesis-strengthening evidence. Anti-indicator LRs are lint-gated at
`design_topic`: a wrong-inverted anti-indicator (firing would move the
target H up) is a BLOCKER, and one with no machine-checkable target
(`target_hypothesis` field absent and id doesn't encode one) is a warning.
Author anti-indicators with an explicit `target_hypothesis` field so
inversion is verifiable.

## Topic design and activation

`design_topic` drafts through the engine's governor gates (admissibility,
indicator lint, priors rationale) and writes the dynamics sidecar;
`activate_topic` is the human-gated commit that re-checks admissibility,
requires a lint-clean dynamics spec, raises a browser approval, and flips
`ACTIVE`. No topic goes live without pricing time-as-evidence.

Read the web for sources (WebSearch/WebFetch or web-tools).
