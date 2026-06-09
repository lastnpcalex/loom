# NROL-AO Math Audit — 2026-06-09

Scope: `engine.py` update path (`bayesian_update`, helpers), `governor.py`
`check_update_proposal`, `framework/pipeline.py` (`process_evidence`,
`apply_observation`), `framework/likelihood_models.py`, audited against
`MATH.md` and the migration spec. Verified by the 16-test capability suite
in `a-shadow-loom/tests/test_nrol_ao_mcp.py` (all green).

## Verified correct

- **Bayes pass** (`_bayes_pass`, engine.py:1464): standard
  `posterior × LR / Σ` with eliminated-hypothesis mass pinning (0.005 each)
  and renormalization. Matches MATH.md.
- **Mixture attenuation** (`_attenuate_lrs`, engine.py:1496):
  `w·LR + (1−w)·mean(LR)` — exactly the documented generative mixture;
  uniform at w=0, direction-preserving at all w.
- **LR sanity gates**: ≥0.99 / ≤0.01 rejected with the "dishonest
  likelihood" message; indicator-bound LRs proportionally scaled to
  max 0.95 (Bayes-invariant) before the gate.
- **Phase 3.5 calibration gate**: status enum enforced; `VALIDATED_*`
  statuses additionally require the backtest fixture artifacts on disk
  (5+red+5 structural check); `SKIPPED_OPERATOR_JUDGMENT` requires a signed
  reason. No unsigned bypass found.
- **Governor pre-commit gates** (governor.py:910+): no-evidence (>2pp shift
  needs refs), confidence inflation (>15pp needs ≥2 refs), repetition
  (canonical URL/text/informationChain dedup — catches same article cited
  via different ev_ids). Wired as hard blockers via `GovernanceError`.
- **Epistemic clamp** (`clamp_posteriors_with_redistribution`,
  engine.py:3533): [0.005, 0.98] with excess mass redistributed only across
  non-bound values; iterative; preserves Σ=1.
- **Observable evaluation** (`likelihood_models.evaluate`): alpha linear in
  value space between baseline (LR=1) and threshold (LR=committed), applied
  geometrically (`committed^alpha`) in log-LR space; saturates outside.
  The near-uniform low-alpha edge case is rescued by the 0.95 proportional
  cap before the ≥0.99 gate.
- **Sustained-observation guard** (`apply_observation`): unchanged
  observable value on an already-FIRED indicator parks instead of
  re-firing — the direct fix for the hormuz H3 0.44→0.80-in-21-minutes
  incident.
- **Provenance**: every update stamps lens (`lrSource`), raw + adjusted
  likelihoods, weight detail, evidence refs, ranges, turning points.

## Bugs found — all fixed this session

1. **FIRE path dead since key rename (critical).**
   `pipeline.process_evidence` read `suggested.get("likelihoods")` but
   `suggest_likelihoods` returns `"suggested_likelihoods"` — always None,
   so every indicator-bound FIRE without caller-supplied LRs raised
   "Either likelihoods or lr_range must be supplied." This includes the
   production news-scan FIRE path (matcher FIRE decisions died silently
   into `engine_rejections`) and the MCP `submit_transition` FIRE. OBSERVE
   was unaffected (passes derived LRs explicitly) — which is why hormuz
   validation worked while FIRE was broken.
   *Fix:* correct key + raise informative error when derivation fails.
2. **lr_decay never applied on the process_evidence FIRE path.** The
   `n_firings` increment was added there so decay could work, but the
   attenuation itself was never wired (it lives only in
   `apply_indicator_effect`, which the news/MCP path bypasses). Repeat
   firings would have applied full-strength LRs every time.
   *Fix:* `LR_eff = LR_base ** (lr_decay ** prior_firings)`, identical to
   `apply_indicator_effect` semantics; regression test asserts the second
   firing moves posteriors less than the first.
3. **`topic_status` crash on non-topic files** (`manifest.json` in
   `topics/`). *Fix:* tolerant per-topic loading in the MCP server;
   unloadable files reported under `skipped` instead of failing the call.
4. **SCHEMA_GAP commit bypassed the engine pipeline** — the MCP server
   wrote topic JSON directly. *Fix:* new `framework.pipeline.log_schema_gap`,
   now used by both the MCP server and `apply_decisions` (which had its own
   inline duplicate).

## Doc-vs-code discrepancies (documented, intentionally not changed)

- `apply_indicator_effect` docstring says `LR_eff = LR_base × lr_decay^n` —
  the multiplicative form is a Bayes no-op (scale-invariant). The code uses
  the exponent form (`base ** decay^n`), which is the meaningful one. Docs
  wrong, code right.
- `_attenuate_lrs_for_cluster` docstring claims harmonic-mean log-LR
  attenuation; the code linearly interpolates LRs toward 1.0. Direction is
  preserved and the result is *more* conservative than the documented form
  for strong evidence — acceptable, but mislabeled.
- `apply_observation` docstring lists `lr_decay` among applied gates; it is
  not applied to observation-derived LRs (changed-value observations fire
  at full derived strength; the sustained guard covers unchanged values).
  Whether repeat observations at *new* values should decay is a design
  decision to make explicitly, not silently.
- `CLAUDE.md`/`AGENTS.md` list a 5-value `calibrationStatus` enum; the
  engine accepts 7 + 1 internal (`VALIDATED_VIA_REFERENCE_CLASS_REVIEWED`,
  `VALIDATED_SOURCED_OPERATOR_JUDGMENT` are valid). The "invalid" statuses
  observed on live topics are fine — the docs are stale (roadmap task #14
  is therefore a doc fix, not a data migration).

## Design observations for Stage 1 hardening

- `process_evidence(likelihoods=...)` accepts caller-supplied LRs when an
  indicator fires. Inside the boundary this is how `apply_observation`
  passes mechanically derived LRs, and the MCP server never exposes it —
  but any repo-side caller can pass arbitrary LRs with a valid indicator
  id. Candidate for restriction when legacy paths are deprecated (task #12).
- `_resolve_evidence_weight` returns weight **1.0** when no refs resolve —
  a generous default. Unattributed evidence arguably deserves the unknown
  -source 0.5 attenuation instead.
- Posteriors are rounded to 4 decimals per pass; sums hold to ±5e-4
  (validate_topic tolerates 0.02). Tests assert at 5e-4.
