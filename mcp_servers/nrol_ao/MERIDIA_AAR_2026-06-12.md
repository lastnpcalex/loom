# Meridia Synthetic Replay AAR - 2026-06-12

## Scope

This run tested the current NROL-AO perception stack after the operator
workflow fixes:

- deliberation is enforced for posterior-moving decisions
- `commit_policy="safe"` cannot apply FIRE/OBSERVE
- schema gaps have a reviewed proposal/apply workflow
- scan digests can be replayed
- cross-day duplicate candidates can be judged explicitly

The harness compared three lanes:

- `oracle`: authored gold transitions
- `pipeline`: one-pass matcher/apply path
- `deliberative`: matcher -> duplicate grouping -> advocate -> rebuttal -> jury

Artifacts were written to:

`%TEMP%\meridia-lasttwo`

## Result

All lanes converged to the authored truth:

- Authored truth: `H3` (reopen 2026-09-12)
- Oracle final: `{"H1": 0.0067, "H2": 0.0067, "H3": 0.98, "H4": 0.0067}`
- Fast final: `{"H1": 0.005, "H2": 0.005, "H3": 0.9833, "H4": 0.0067}`
- Deliberative final: `{"H1": 0.005, "H2": 0.005, "H3": 0.9833, "H4": 0.0067}`

Deliberation did not change final posterior accuracy in this corpus. It did
change the decision trace.

## Metrics

| Metric | Fast | Deliberative | Read |
|---|---:|---:|---|
| Final argmax | H3 | H3 | Both correct |
| Final vector Brier | 0.0004 | 0.0004 | Tie, slightly better than oracle rounding |
| H2 checkpoint Brier | 0.2048 | 0.2048 | No improvement |
| Max TV vs oracle | 0.1348 | 0.1348 | No improvement |
| Average TV vs oracle | 0.0552 | 0.0552 | No improvement |
| Indicator routing on move decisions | 17 correct / 2 wrong | 11 correct / 2 wrong | Delib made fewer moves |
| Distractors acted on | 0 / 4 | 0 / 4 | Tie |

## Duplicate Discipline

The deliberative lane materially reduced duplicate movement.

| Event | Articles | Fast move decisions | Deliberative move decisions |
|---|---:|---:|---:|
| E01 | 3 | 3 | 1 |
| E08 | 3 | 3 | 1 |
| E10 | 2 | 2 | 2 |
| E11 | 3 | 2 | 1 |
| E12 | 2 | 2 | 1 |

This is the clearest win. The jury emitted `DUPLICATE_OF` decisions for 5
gold FIRE articles, 3 gold OBSERVE articles, and 1 PARK article. That means
deliberation is doing useful duplicate suppression, but not perfectly: E10
still produced two move decisions.

## Confusion Matrix Summary

Fast lane:

- FIRE gold: 10 FIRE, 2 OBSERVE, 3 PARK
- OBSERVE gold: 7 OBSERVE, 2 PARK
- IGNORE gold: 2 IGNORE, 2 PARK
- PARK gold: 2 PARK
- SCHEMA_GAP gold: 1 SCHEMA_GAP

Deliberative lane:

- FIRE gold: 5 FIRE, 5 DUPLICATE_OF, 2 OBSERVE, 3 PARK
- OBSERVE gold: 6 OBSERVE, 3 DUPLICATE_OF
- IGNORE gold: 2 IGNORE, 2 PARK
- PARK gold: 1 PARK, 1 DUPLICATE_OF
- SCHEMA_GAP gold: 1 SCHEMA_GAP

The deliberative lane reduced over-counting but did not fix routing mistakes
or missed PARK/FIRE distinctions.

## Interpretation

The authority layer is now close to the design promise: model outputs are
typed, deliberation is enforced or waived loudly, safe scans do not authorize
posterior movement, and schema/replay/duplicate workflows are exposed through
MCP tools.

The perception layer is useful but not fully trustworthy. In this test,
deliberation improved duplicate discipline without improving trajectory
divergence. That is a real gain, but not enough to claim the model now
understands the scenario better overall.

## Follow-ups

1. Improve duplicate review for the remaining E10 class. The current jury can
   suppress many same-event articles, but not all.
2. Add a head-fake corpus where a decree is walked back. The present corpus
   still rewards broadly credulous late-H3 convergence.
3. Track deliberation changes in score output directly: count
   `DUPLICATE_OF`, `UNIQUE_EVENT`, and jury overrides as first-class metrics.
4. Add reviewed schema application tests for `add_new_indicator` once shape
   coverage/lint requirements are represented cleanly in resolver output.
5. Preserve scan provenance fields (`surfaced_via`, `scanRound`,
   `queryProvenance`) in the engine evidence ledger. The docs previously
   claimed this happened; implementation still drops those fields unless
   explicitly carried.

## Bottom Line

The system is now structurally much closer to its purpose. It does not let
deliberation replace authority, and it gives operators tools for schema
review, scan replay, and duplicate judgment.

The measured epistemic result is narrower: deliberation currently buys
duplicate discipline, not broad accuracy improvement. That is worth keeping,
but the next design iteration should target adversarial perception rather
than more authority plumbing.
