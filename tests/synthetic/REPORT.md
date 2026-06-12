# Synthetic Meridia replay — run log

Authored truth: **H3** (reopen 2026-09-12). Oracle = gold transitions
through submit_transition (deterministic reference). Pipeline = corpus →
Qwen3.6-27B matcher (temp 0) → auto-commit, one simulated day at a time.
Regenerate: `replay.py --out o.jsonl`, `replay.py --lane pipeline ...`,
then `score.py`.

## Run 2 — 2026-06-11, after the duplicate-FIRE fix (engine 88da253)

Final: oracle H3 = 0.98, pipeline H3 = 0.9833, TV 0.003. Day-1 TV
**0.000** — the bundled FIRE tracks the oracle exactly through the first
four simulated days (run 1: 0.079).

Peak divergence moved to E04 (2026-07-24) at **0.135**: the matcher
missed E04's gold FIRE on t2_blockade_reinforcement (parked a1, observed
a2's insurance metric) in both runs, but run 1 hid the miss — the
engine's duplicate over-count pushed toward H4 while the missed fire
starved H4, two errors canceling to a flattering 0.063. Run 2 measures
the miss honestly. Same story in Brier H2@Sep-1: 0.164 → 0.205, the old
number was unearned. All remaining divergence is perception error, and
its direction is conservative (missed FIREs = underconfidence, never
overconfidence).

Scorer note: the matcher emitted two DECISION blocks for E10-a2 (one
article, two extractable metrics). apply_decisions is last-wins by idx;
score.py now dedupes the same way before building the confusion matrix.

## Run 1 — 2026-06-11, pre-fix baseline

Final: pipeline H3 = 0.9833, peak TV 0.079 on day 1. The matcher
triple-FIREd E01's duplicate coverage (also E10/E11/E12 multi-fires) —
the live hormuz failure reproduced synthetically. lr_decay bounded the
blockade refires; the decree (lr_decay 1.0) triple-applied its full LR
and helped only because it pointed at the truth. Off-diagonal errors all
conservative: 4 gold-FIREs parked, 2 distractors parked-not-ignored,
zero distractors acted on, zero invented indicators. E06 SCHEMA_GAP and
E07 PARK bait handled correctly; OBSERVE extraction 5/6 exact,
awkward-units piece within 2pp.

## Open items

- Cross-day duplicate detection (v2): LLM yes/no "same causal event?"
  filed as a typed duplicate-of proposal, biased toward duplicate when
  uncertain; candidates retrieved by indicator + publication-date window.
- A harder world: this timeline's third act drags any mediocre matcher
  to the right answer. Author a head-fake next (a decree that gets
  walked back) so duplicate amplification and credulity get measured
  instead of forgiven.
