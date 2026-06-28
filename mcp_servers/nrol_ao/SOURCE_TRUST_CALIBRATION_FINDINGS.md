# Source-Trust Calibration: Findings Report

**Date:** 2026-06-27
**Scope:** Topic-local source-trust calibration (`framework/source_ledger.py`) + cross-topic DB (`framework/source_db.py`), as observed on the `calibration-hormuz-reopen-2027` topic.
**Verdict:** The topic-local source-trust calibration system is **not producing meaningful signal.** Do not put stock in its trust values. It does **not** move posteriors (verified), so it can be ignored for the Brier lane; it does influence evidence-entry weighting/ordering internally. Three grounded root causes below.

This report supersedes the speculation in the operator's initial field report. All claims cite code (`file:line`) or live probes of the actual topic ledger.

---

## TL;DR — why the trust values are meaningless

- A "refutation" is **not** a determination that a source was wrong. It is purely structural: *a later evidence entry tagged `CONTESTED`, from a different source, that shares ≥30% of its significant nouns with an earlier entry* (`source_ledger.py:159-211`). No falsity is established.
- On a single-subject topic (Hormuz: open/closed), the `CONTESTED` tag and the 30% noun-overlap test are **near-trivially satisfied for almost every cross-source pair**, producing a 96.8% refutation rate (9,609 REFUTED vs 320 CONFIRMED ledger records on the live topic).
- The wire services (Reuters, CNN, BBC, Bloomberg, NYT, AP) sit at the 0.05 floor not because they're distrusted, but because they were early reporters and absorbed ≥8 such structural refutations each — each cutting their odds by a fixed 0.7 likelihood ratio. The "quantized" trust values (0.4118, 0.3289, 0.2554 … 0.0545) are the geometric decay of that 0.7 LR, **not** small-denominator confirmation fractions.

**Conclusion:** the topic-local trust numbers reflect topic topology (single subject, high noun overlap, many CONTESTED tags) far more than they reflect source reliability. Treat them as not-working.

## What it *doesn't* affect (verified)

The posterior-update path `bayesian_update` (`engine.py:1733`) consumes **explicit per-hypothesis likelihood ratios gated by a fired indicator** (`engine.py:1774`). It does **not** read `effectiveWeight` or `source_trust_factor`. The source-trust machinery feeds the evidence *entry's* stored `effectiveWeight` (via `get_effective_weight`, `governor.py:463-549`) and thereby compaction ordering (`framework/compaction.py:186-300`) and extrapolation weighting (`framework/extrapolation.py:100-111`) — but it does **not** enter the posterior math. The committed Brier lane is driven by pre-committed indicator LRs + the dynamics spec, separate by design.

So: **source trust is read-only relative to posteriors.** Ignoring it is safe for the Brier/scoring lane. It is *not* safe to treat its surfaced trust values as reliable metadata.

---

## Root cause 1 — Refutation is structural, not evidential

`scan_for_resolutions` (`source_ledger.py:115-216`) builds confirmation/refutation *pairs* across the evidence log. A pair `(i, j)` becomes a REFUTED ledger record when all four hold:

1. `j` is later than `i`, pair not already in ledger (`:168-170`)
2. **Different source** — `sources_i & sources_j` empty after splitting on `+`/`/` (`:173-175`)
3. **Noun overlap ≥ 0.30** — `len(nouns_i & nouns_j) / min(len(nouns_i), len(nouns_j))` (`:187-189`)
4. `claimState[j] ∈ {CONTESTED, INVALIDATED}` (`:192-199`)

There is no truth determination, no outcome check, no verification that claim `i` was wrong. "Refuted" = "a later contested item exists that overlaps in wording."

**Live probe** (`calibration-hormuz-reopen-2027.json`):
```
evidenceLog: 684 entries
  claimState: CONTESTED 643 (94%) | SUPPORTED 29 (4%) | PROPOSED 12
ledger: 9,929 entries
  resolution: REFUTED 9,609 (96.8%) | CONFIRMED 320 (3.2%)
  flagged SAME_SOURCE: 0
```
684 evidence entries → 9,929 ledger records is the O(n²) blow-up signature of the 30% overlap test on a constant-noun-set topic.

## Root cause 2 — `CONTESTED` is assigned by crude heuristics, not verification

The `claimState` that root cause 1 keys off is set at evidence-insert time (`engine.py:3484-3498`) by one of two paths, both text-heuristic:

**Path A — `assess_claim_state`** (`governor.py:388-460`): scans the entire log for an entry sharing ≥3 words (len>4); if that entry's text contains any of `["denied","denies","false","incorrect","not true","contradicts","refuted"]` (`governor.py:449-450`), the new entry is CONTESTED. `contradicted`/`corroborated` are not reset between iterations and contradiction wins (`governor.py:456`), so one marker-bearing overlapping entry anywhere in the log flips the state.

**Path B — `detect_contradictions` override** (`engine.py:3493-3498`, `framework/contradictions.py`): runs after Path A, overrides to CONTESTED on antonym pairs (`contradictions.py:38-70` — notably `("open","closed")`, `("opened","closed")`), negation markers, numeric divergence >10–20%, or text-vs-datafeed mismatch.

On a topic whose entire subject is whether the strait is open or closed, the `open`/`closed` antonym pair and denial markers fire on nearly every status-related entry — which is why 643/684 entries are CONTESTED.

*(Not determined: which path produced each CONTESTED tag — would require per-entry text sampling. Either path is sufficient to explain the 94% rate.)*

## Root cause 3 — The trust math turns refutation-count into a geometric ladder

`compute_effective_trust` (`source_ledger.py:359-417`):
- base LR = `{CONFIRMED: 1.2, REFUTED: 0.7}` (`source_ledger.py:383`) — note these are weaker than the cross-topic DB's `3.0/0.33` (`source_db.py:186`), and the two systems disagree.
- posterior = `prior·LR / (prior·LR + 1−prior)`, clamped `[0.05, 0.99]` (`source_ledger.py:416-417`).
- The penalty lands on the **earlier** entry's source only (`resolve_claim` records `source: original.get("source")`, `source_ledger.py:261`). The later contesting source gets nothing — no credit, no penalty.

From a 0.5 prior, m refutations give `0.7^m / (0.7^m + 1)`:

| m | value |
|---|---|
| 1 | 0.4118 |
| 2 | 0.3289 |
| 3 | 0.2554 |
| 4 | 0.1936 |
| 5 | 0.1439 |
| 6 | 0.1053 |
| 7 | 0.0772 |
| 8 | 0.0545 |
| ≥9 | 0.05 (floor) |

These exact values appear across the topic's sources. Each step = one more structural refutation. The wires reach the floor because they accrued ≥8 each.

**Live probe — top refuted sources:**
```
Tasnim News 841, msn.com 827, Al Jazeera 685, reuters.com 660,
yahoo.com 624, CNBC 509, NPR 451, PBS News 305, UN News 303,
memesita.com 253, Bloomberg 228, Axios 173, CNN 171, WSFA/AP 166, TIME 156
```
These refutation counts are dominated by root causes 1+2 (topology + heuristic tagging), not by source unreliability.

---

## Secondary issues

### Aggregator republication isn't normalized (the wires get refuted by their own republished stories)
`extract_sources` splits on `+`/`/` only (`source_ledger.py:100`). The `source` field is the aggregator domain ("msn.com", "yahoo.com"), not the underlying wire ("Reuters"), so `sources_i & sources_j` never matches and the retroactive same-source cleanup (`source_ledger.py:471-479`) flagged **zero** records despite msn.com (827) and yahoo.com (624) being pure wire-republishers. A wire's own story, republished by an aggregator, refutes the wire.

### `trust_delta` field is vestigial / misleading
`resolve_claim` writes `trust_delta: {CONFIRMED:+0.05, REFUTED:−0.10}` (`source_ledger.py:252-253`), but `compute_effective_trust` **ignores it** and recomputes via the Bayesian formula with LR 1.2/0.7. The stored `trust_delta` does not reflect the actual penalty applied. *(Not grepped for other readers — claim is that the trust-computation path ignores it, which is read-confirmed.)*

### Cross-topic `source_db.json` is corrupted — NOT an OneDrive sync conflict
The file at `sources/source_db.json` contains:
```json
{ "topic": "hormuz-closure", "ledger_entries": 50, "ingested": 132, "skipped": 1 }
```
This is the **return value of `ingest_from_topic()`** (`source_db.py:345-350`) — the ingest *summary*, written over the database. `validate_source_db` confirms live: `valid:false, sources_checked:0, "sources/ missing or not an object"`. As a result `cross_topic_trust` is `null` for every source.

All current `save_db(` callers save the mutated `db` correctly (`pipeline.py:73`, `backfill.py:371`, CLI `source_db.py:502`); the MCP server is read-only on this file (`server.py:4591`, `source_trust.py:7`). **No current code path produces this corruption** — it's a stale artifact from a past code version or past agent action (the slug `hormuz-closure` is the archived topic, predating the active `calibration-hormuz-reopen-2027`). Note: the usual "malformed DB = OneDrive sync conflict" diagnosis (see project memory `project_onedrive_db_sync`) is **wrong for this case** — this is a code/agent write bug, not a sync conflict. The memory needs updating.

---

## Recommendations

**Do not regenerate `source_db.json` yet.** Regeneration would re-ingest the bloated 9,609-refutation ledger and propagate the skew cross-topic. Fix the generation path first (root causes 1+2), then regenerate.

If the system is to be made trustworthy later, in priority order:
1. **Pairing** (`source_ledger.py:159-189`): replace global O(n²) noun-overlap with subject-aware overlap, time-windowing, or one-refutation-per-claim dedupe.
2. **`CONTESTED` assignment** (`governor.py:388-460`, `contradictions.py`): the open/closed antonym and denial-marker heuristics are too promiscuous for a single-subject topic.
3. **Aggregator normalization** (`source_ledger.py:100`): map republisher domains to underlying wires so a wire isn't refuted by its own republished story.
4. **Reconcile the two LR sets**: topic-local uses 1.2/0.7 (`source_ledger.py:383`), cross-topic uses 3.0/0.33 (`source_db.py:186`). They feed off the same overlap-generated records.
5. **Remove or fix the vestigial `trust_delta`** (`source_ledger.py:252`).

Until then: **ignore source-trust values for any decision.** They reflect topic topology, not reliability.

---

## Provenance / method note

This report is the result of code tracing + live probes, not inference. Traced: `scan_for_resolutions`, `resolve_claim`, `compute_effective_trust`, `auto_calibrate` (source_ledger.py); `assess_claim_state`, `get_effective_weight` (governor.py); `detect_contradictions` + detection passes (contradictions.py); `ingest_from_topic`, `_bayesian_update`, `save_db` (source_db.py); `bayesian_update` posterior path (engine.py). Probed live: `calibration-hormuz-reopen-2027.json` evidence/ledger breakdown, `validate_source_db`, `source_db.json` contents, `nrol_status`.

Honest gaps (stated, not hidden): (a) did not determine which CONTESTED-assignment path (A vs B) dominates per entry; (b) did not grep for non-trust readers of `trust_delta`; (c) did not pin the exact writer that corrupted `source_db.json` — only confirmed no *current* code path does it.
