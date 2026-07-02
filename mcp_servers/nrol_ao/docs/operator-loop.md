# Operator Loop (deep reference)

The lean OPERATOR.md holds the 6-step skeleton; this doc holds the sub-notes
that inform non-routine turns. Fetch it before your first loop and whenever a
step's detail is unclear.

Nothing moves a posterior except an approved typed commit:
`commit_match(proposal_id)` on a pending proposal, or
`submit_transition(..., indicator_id=..., commit=true)`.
Prose never moves beliefs, no matter how well it describes the evidence.

When the human asks to "run the evidence loop", "catch the topic up", or
"run updates", execute this sequence and report each step:

## 1. Status

run `topic_status` for the topic. Check `scanStale`,
`parkedReviewDebt`, and `list_proposals(status="pending")`.
Before scanning, call `read_search_queries`. If the configured queries do
not cover the current causal axes and measurement sources, call
`propose_search_query_update`, then `red_team_search_query_update`. Apply
only after red-team APPROVE and human approval. If the human asks for an
immediate scan before query coverage is repaired, label scan confidence
partial/weak and state which durable query gaps remain.

## 2. Scan

use `run_news_scan(..., commit_policy="safe")` for review-first
scans. The safe workflow is: search, full-article fetch, strict matcher,
duplicate grouping, and advocate/rebut/jury debate over ALL candidates
(FIRE / OBSERVE / PARK). PARK/SCHEMA_GAP may auto-apply because they
cannot move posteriors; FIRE/OBSERVE must land in the proposal queue with
their deliberation record attached. This remains true if `commit=true`
is present with `commit_policy="safe"`. A MATCHER FAILED or DEBATE FAILED
line means the scan needs investigation, not interpretation.
Search retrieval may be broad, but matcher input is freshness-gated:
tracker query parameters are stripped for duplicate detection, dated
articles outside the adaptive window are dropped, full-article metadata
can supply a missing publication date, and undated FIRE/OBSERVE candidates
are downgraded to PARK instead of proposal filing.

If the digest reports `freshness downgrades`, treat that as action
required: the matcher/debate saw a posterior-moving candidate, but the
scan lacked a publication date. Brief those articles separately, refetch
or search for dated corroboration, and either run `review_parked` or file
an explicitly deliberated proposal. Do not summarize a scan with freshness
downgrades as "no proposals, nothing to do."
The brief's `freshness_downgrade_samples` carry an `evidence_id` for each
downgraded row — use it with `read_evidence(evidence_ids=...)` or targeted
`review_parked` to act on a specific downgrade instead of re-reading the
full on-disk digest packet to recover which article `A12` was. The digest
packet is the sandbox break-out bait `brief=true` exists to keep you out
of; reach for the row handle, not the file.

**A freshness downgrade is not a duplicate candidate.** Freshness (missing
pubdate) and duplication (re-report of an already-counted event) are
orthogonal: an undated article can be a unique event or a re-report; a
dated article can be either too. Run `review_duplicate_candidate` only
when you independently suspect a re-report of a counted event (per the
trigger at the bottom of this loop) — never as a reflexive check on a
downgrade, and never because "A12 is a posterior-mover so I can't gloss
over it." The freshness gate already parked the row; re-litigating it as
a duplicate re-derives a verdict the safe-policy audit already recorded.

## 3. Brief the human on the queue; never just list it.

After any scan or
`review_parked` files proposals, produce a commit briefing before touching
the queue:

- Group proposals by underlying causal event. Same fact reported by
  several articles = one group. One causal event = commit one; the rest are
  corroboration, usually withdrawals as duplicates.
- For each group, check the target indicator's current state in the topic
  (status, `n_firings`, `lastObservedValue`) and say what a commit would
  actually do: fresh firing at full LR, repeat firing at (slightly reduced)
  strength via posterior saturation, sustained observation no-op, or
  observation deriving a new LR.

> **Note (2026-06-29, status: current — verify against engine source before
> relying on this if repeat-firing behavior seems to have changed):**
> `lr_decay` repeat-firing attenuation is DISABLED at runtime. Repeat firings
> apply the full LR; posterior saturation (the posterior is already closer
> to the LR's fixed point) plus the [0.005, 0.98] epistemic clamp bound the
> cumulative effect. A verified simulation: 13 stacked full-strength firings
> of an indicator at n_firings=13 plateaued at H4≈0.35 with no blowout. The
> `lr_decay` field still exists on indicators and the design gate still warns
> on explicit `lr_decay >= 1.0` (see "duplicate amplifier" check), but it is
> dead metadata at runtime — do not tune it expecting behavioral effect.

- **Framing trap:** "the indicator is already FIRED" is a misread. `FIRED`
  is a status with a count (`n_firings`), not a terminal state. A NEW
  underlying event can re-fire (at full LR, bounded by saturation); a NEW
  ARTICLE about the same already-counted event cannot. Say "fired N times
  — re-fireable on a new event" not "already fired," and make the new-event
  vs new-article distinction explicit before recommending commit or withdraw.
- State the expected posterior direction and rough magnitude.
- Cite the attached jury verdict / duplicate grouping when present. If a
  proposal carries a deliberation waiver, call it out explicitly.
- For possible re-reports of old events, run `review_duplicate_candidate`
  and cite its typed verdict. If uncertain, recommend duplicate/withdraw
  or PARK; duplicate movement is the dangerous direction.
- Recommend commit or withdraw per proposal, then STOP and wait for the
  human's decision. The briefing reviews model deliberation; it is not a
  substitute for it. The human's reply is the authority verdict.

## 4. Commit / withdraw

`commit_match(proposal_id=...)` for approved
proposals; `withdraw_proposal(proposal_id, reason)` for rejected ones. Each
commit raises a browser approval prompt to the human. If `commit_match`
refuses because a legacy proposal has no deliberation record, do not work
around it. Withdraw and re-file through the deliberated path, or ask the
human for an explicit waiver.

## 5. Work the parked queue

`review_parked(slug=..., limit=12,
dry_run=false)` re-judges parked evidence against the current schema with
full article text and debate. Escalations land back in the proposal queue;
return to step 3. Treat `parkedReviewDebt.dueCount` as active work.
`flaggedForIndicatorReview` / `parkedTotal` is the retained parked-evidence
archive, not the number of tasks remaining. Withdrawing a proposal does not
delete or unflag the original parked evidence; if the human has already
reviewed and rejected the corresponding proposal, use
`acknowledge_parked_reviews(..., reason="operator already reviewed/withdrew
corresponding proposals; retain as non-moving archived evidence")` to stamp
the due review without re-litigating all archived evidence.

**Draining the queue:** `review_parked` can regenerate proposals for
events you've already counted (the symptom: it keeps filing proposals
that look like duplicates of committed evidence). When you've confirmed a
batch is already-counted / non-actionable, do NOT keep re-running
`review_parked` hoping the queue empties — it won't. Resolve the
specific proposals (commit one, withdraw the rest as duplicates), then
`acknowledge_parked_reviews` the remaining flagged evidence with a
reason like "already counted via ev_N / non-actionable corroboration;
retained as archived non-moving evidence." That stamps the due review,
drops `parkedReviewDebt.dueCount`, and stops the queue regenerating.
`flaggedForIndicatorReview` / `parkedTotal` is the archive size (it
stays); `dueCount` is the work. Drain `dueCount`, not the archive.

Pass `check_cross_day_duplicates=true` to run the semantic cross-day
duplicate judge on each FIRE/OBSERVE candidate that survives the mechanical
suppression check — this catches the case the mechanical check misses (a
new article with a different URL reporting an event already committed via
different evidence refs). `DUPLICATE_OF` and `UNCERTAIN_DUPLICATE`
suppress the proposal (parked as a duplicate note instead of filed).
Adds one llama call per surviving candidate — bounded but not free. It is
**off by default**; pass it explicitly when the parked queue is
regenerating proposals for events you suspect were already counted under
different article refs (the symptom is "review_parked files proposals that
look like duplicates of already-committed evidence").

## 6. Report non-movement honestly

when a transition returns `parked: true`,
read and report `parked_reason`. "Sustained observation: unchanged from
last firing" is the engine refusing to double-count a persisting fact. That
is correct behavior, not a failure.
