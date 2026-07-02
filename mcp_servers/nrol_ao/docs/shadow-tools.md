# Calibration & Shadow Tools (a guide, not the system)

The committed posterior — moved only by approved typed commits through
pre-committed indicator likelihoods — is authoritative. The shadow tools
derive an *independent* posterior from the topic's pre-committed dynamics
spec and are for calibration, not action. They never write topic state.

## shadow_posteriors

- `shadow_posteriors(slug, asof="")` derives first-passage posteriors from
  a regime-switching process whose transition-rate priors are pre-committed
  (with rationales, lint-gated) in
  `loom/topics/dynamics/<slug>.dynamics.json`. Elapsed time in the current
  regime updates the exit-rate posterior exactly (Gamma conjugacy), so
  "still closed, N days later" is priced instead of ignored — a channel the
  committed posterior lacks (it quantizes sub-threshold evidence to LR=1.0
  and has only a deadline cliff for time). Pass `asof=YYYY-MM-DD` for a
  counterfactual run.
- When to call it: alongside `topic_status` after a scan, or for a
  counterfactual `asof` to ask "what should the posterior have been on day
  N?" Compare the shadow posterior to the committed posterior; **divergence
  is the calibration conversation, not an error.** Shadow mass drifting
  toward a later hypothesis than committed means the committed posterior is
  underpricing elapsed time; drift toward the residual means time-as-evidence
  says the event is increasingly unlikely.
- What divergence does NOT do: it never moves beliefs. If the divergence
  warrants action, you still file a typed transition
  (`submit_transition` / `propose_match` -> `commit_match`). The shadow
  output is evidence for *why*, not a commit.
- Shadow posteriors require a dynamics spec, which `activate_topic` already
  enforces. A live topic already has one; a missing or lint-failing spec is a
  design error to fix through `design_topic`, not a runtime signal to act on.

## future_cast

- `future_cast(slug, scenario, target="", proposed_transition="", observed_value="", asof="", assumptions=[], save=False)`
  is a dry-run shadow analysis: it asks what would happen if a hypothetical
  event or indicator firing occurred, without mutating topic JSON. It
  deep-clones the topic in memory, applies the hypothetical through the
  engine's own `bayesian_update` (no save), and reports a `shadow_posteriors`
  before/after/delta plus a red-team critique. Output posteriors are named
  `shadow_posteriors`, never `posteriors`; synthetic evidence is labeled
  `HYPOTHETICAL`. Pass `asof=YYYY-MM-DD` to also report the dynamics shadow
  posterior at that date. `save=true` writes the cast to
  `future_casts/future_casts.jsonl` (outside topic state); a saved cast is
  never evidence and never satisfies evidence requirements. If divergence
  warrants action, you still file a typed transition — the cast is evidence
  for *why*, not a commit.

## Resolution

- At resolution: `resolve_topic(slug, resolved_hypothesis, note="", skip_aar=False)`
  is the single sanctioned resolution entry point — it sets `meta.status=RESOLVED`,
  records the outcome via the engine's existing `record_outcome`, and computes
  a **two-lane Brier** comparing the shadow posterior trajectory (reconstructed
  from the dynamics spec at each `posteriorHistory` date) against the committed
  posterior trajectory, both vs the resolved truth. Optionally it runs a
  red-team **after-action review** over the evidence log and recent scan
  digests, asking where perception vs authority diverged. Resolution raises a
  browser approval (fail-closed); on denial the topic is NOT resolved. The
  Brier/AAR analytics are read-only and never move the committed posterior.
- `resolution_brier(slug, asof="")` recomputes the two-lane Brier for an
  already-resolved topic without re-resolving — read-only, for post-hoc
  calibration review.

## Source trust (LIVE, not a Brier score)

Source trust is a Bayesian trust ledger of confirmed/refuted claims, exposed
read-only through:

- `source_calibration_status(slug="")` — topic-local `sourceCalibration`
  summary, or cross-topic DB summary.
- `source_profile(source, domain="")` — one source's full profile + domain
  fallback chain.
- `validate_source_db()` — schema sanity check.
- `source_domain_patterns(min_claims=3)` — cross-source reliability patterns.

These read `framework/source_db.py`/`source_ledger.py`/`calibrate.py` and never
move posteriors or write to `source_db.json`. Forecast Brier and source trust
are kept separate by design.

## Triage log

Triage is LIVE (`triage_headline` first, always). For auditability, pass
`save=true` to append the result to `loom/triage_log/triage_log.jsonl` (an
audit ledger outside topic state). A logged triage is NOT evidence — it
never moves posteriors; promotion to real action still goes through
`submit_transition` / `propose_match` -> `commit_match`. Review prior
triages with `list_triage_log(slug="", limit=25)` and
`read_triage_log(triage_id)`.

## Social-media-user Brier

A social-media handle is a *forecaster*. Log its probability forecasts with
`log_social_forecast(handle, slug, posteriors, note="")` (stored at
`loom/social_forecasts/`, outside topic state; the forecast is NOT evidence
and never moves posteriors). At resolution, score the handle's forecasts with
`social_user_brier(handle, slug="")` — Brier against the resolved truth via
`compute_brier_score`. Unresolved forecasts report as pending.
`list_social_handles()` lists handles + counts. This is forecast calibration,
kept separate from source trust by design.
