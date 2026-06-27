# NROL-AO Completion Roadmap

The core promise: **beliefs only move when the world moves.** Every posterior
change is traceable to a typed observation routed through a pre-committed
likelihood. Humans and LLMs — any provider — are *perception*: they notice,
extract, propose. The server alone is *authority*: it validates and commits.
Non-updates are as visible as updates.

Two load-bearing properties are not yet true:

1. **Authority is a convention, not a capability.** The MCP server enforces
   the rules, but nothing forces traffic through it — engine code, legacy
   CLI (`update.py --posteriors`), and topic state all sit in a repo any
   agent or human can write directly.
2. **Continuity exists for one topic.** Only hormuz does mechanical
   continuous updates (observable blocks); other topics are binary-fire-only.

## Architecture decisions (settled 2026-06-09)

- **Engine code stays in `C:\Claude-Code\NROL-AO\temp-repo` as an imported
  library.** The MCP server in this folder is the boundary; moving the math
  buys nothing epistemically.
- **The Loom permission system is the auth layer.** Loom conversations launch
  CC outside the NROL code/state folders, so file edits always prompt in the
  browser; nrol-ao MCP commits raise their own browser permission request.
  Two layers, different in kind:
  - Loom permissions answer *"may this actor act?"* (every mutation visible)
  - MCP typed tools answer *"is this action expressible?"* (no way to say
    "set H3 to 0.72" even with approval)
- **NROL-AO operator mode = the operator role made physical.** A Loom mode
  that is a launch profile over `claude_client.py`: neutral cwd, file tools
  and Bash stripped, `--strict-mcp-config` with only nrol-ao (+ web-tools),
  nrol read tools allowlisted silent, server-side permission request as the
  single commit gate, `NROL_AO_REQUIRE_LOOM_APPROVAL=1`. Regular Loom mode =
  designer/admin role with full tools. Not a separate client — if it grows
  beyond a launch profile, it has drifted.

Reference spec: `temp-repo/specs/mcp-server-migration-plan.md` (authority
boundary, proposal lifecycle, validation rules, red-team failure modes).

## Stages

### Stage 0 — Define "correct" executably (tasks #1–#7) — ✅ DONE 2026-06-09

All items complete. Capability suite green (16 tests). Found+fixed: dead
FIRE path (suggested_likelihoods key mismatch), missing lr_decay on the
process_evidence path. Verified live: headless tool-stripping holds
(subagent delegation inherits restrictions); LOOM_CONV_ID reaches the MCP
server via --mcp-config env per spawn. Math audit report:
`MATH_AUDIT_2026-06-09.md`.

- Capability test suite (`tests/test_nrol_ao_mcp.py`): FIRE without
  indicator rejected; OBSERVE without observable/value rejected; evidence
  cannot smuggle likelihoods; PARK/SCHEMA_GAP move nothing; posteriors sum
  to 1 after commits; commit without Loom context rejected when required;
  IGNORE writes nothing.
- Fix `topic_status` crash (tolerant per-topic loading; `manifest.json` in
  topics/ currently kills whole listing).
- Quarantine test fixtures (`CHANGE-ME.json`, `test-*.json`) out of live
  `topics/`.
- Route SCHEMA_GAP commit through the framework instead of the MCP layer
  writing topic JSON directly (the server's one internal bypass).
- Math audit: `engine.py` update path vs `MATH.md` — mixture weighting,
  clamps, lr_decay, renormalization, floor/ceiling redistribution. Written
  report; bugs become tasks.
- Verify CC tool-stripping holds headless; verify env propagation
  Loom → CC → stdio MCP per generation turn.

### Stage 1 — Make the boundary real (tasks #8–#12)

- ✅ NROL-AO operator mode in Loom (2026-06-09): "NROL-AO" entry in the
  new-conversation modal → mode=claude + nrol_operator flag (DB column).
  Launch profile strips Write/Edit/NotebookEdit/Bash/Agent/Task/KillShell/
  SlashCommand, adds --strict-mcp-config, neutral cwd
  (workspaces/nrol_operator), OPERATOR.md system prompt. The permission
  hook auto-allows the mcp__nrol-ao__ surface (reads are free; commits are
  gated by the MCP server's own fail-closed browser request — single
  prompt per commit) and denies file/shell tools as defense in depth.
- ✅ Fail-closed permission default (2026-06-09): commit=true without
  LOOM_CONV_ID is denied; NROL_AO_ALLOW_UNGATED_COMMITS=1 is the explicit
  headless opt-out.
- ✅ State/code split, core (2026-06-09): `NROL_AO_STATE_DIR` relocates
  topics/briefs/dashboards for the engine (the sole writer), the runtime
  framework modules (dependencies, triage), and the dashboard server;
  Loom forwards the var into the per-conversation MCP config. Default
  unset = historical repo-local layout, fully backward compatible.
  **Before flipping the var in production:** convert the standalone
  maintenance scripts that still hardcode repo-local topic paths
  (framework/: extrapolation, meta_health, post_edit_check, runner,
  stamp_deadlines, stamp_resolution_dates, replay_indicators,
  migrate_to_lr, lens_calibration, topic_search) and move the files.
- ✅ Proposal lifecycle (2026-06-09): `submit_article` (content-keyed
  dedup) → `propose_match` (typed, statically validated, pending) →
  `commit_match` (re-validates, duplicate-URL guard, routes through
  submit_transition's full gate chain; Loom denial leaves the proposal
  pending, engine rejection records the reason). `list_proposals` is the
  review queue; `withdraw_proposal` is the IGNORE decision. Store:
  SQLite (`proposals.db`) beside the activity ledger.
- ✅ Legacy `--posteriors` path closed (2026-06-09): `run_update` refuses
  explicit posteriors on ACTIVE topics without a signed
  `NROL_AO_ADMIN_POSTERIORS_REASON`; the governance force-bypass branch
  is deleted. (`runner.py`'s posterior arg was already dead code — wrong
  parameter name.)

### Stage 2 — Bayesian everywhere (tasks #13–#14)

- Observable-block migration for the remaining topics (AGENDA.md's
  load-bearing TODO); operator confirms baselines per topic.
- Reconcile `calibrationStatus` enum across docs/engine/data (topics carry
  values outside the documented set).

### Stage 3 — Make it live (tasks #15–#16)

- Scheduled scans via MCP use the review-first safe policy: PARK/SCHEMA_GAP
  may auto-apply, while all FIRE/OBSERVE movement is filed as pending
  proposals for human briefing and approval. `commit_policy="safe"` wins over
  accidental `commit=true`. Daily digest surfaces non-updates as first-class
  output.
- Resolution + Brier calibration loop feeding source/lens trust back into
  update weighting.

## Multi-provider operator parity — codex + agy — ✅ DONE 2026-06-11

Observed live: a Codex-model operator ran shell freely in the operator
workspace (rg of parent dirs) and had no OPERATOR.md — the lockdown was
claude_client-only. The authority gate was never at risk
(`_ask_loom_permission` lives inside the MCP server, provider-agnostic);
the gap was the shell side-channel and missing role instructions. All
three steps shipped 2026-06-11. Unit layer:
`tests/test_operator_parity.py`.

- **umans port — ✅ DONE 2026-06-26 (trivial).** Umans models
  (`umans-coder`, `umans-glm-5.2`, `umans-flash`, …) launch through
  `claude_client.run_claude` with `use_umans=True` — same CLI, pointed at
  `api.code.umans.ai`. The claude operator lockdown (`--strict-mcp-config`,
  OPERATOR.md system prompt, `LOOM_NROL_OPERATOR=1`, Write/Edit/Bash
  stripping, nrol-ao MCP surface) is keyed on `nrol_operator`, not on a
  provider string, so it already covered umans with no new client code.
  The only gap was the guard: `_nrol_operator_block_reason` classified
  umans as its own provider but `NROL_OPERATOR_PROVIDERS` did not list it,
  so `umans-*` models were refused at creation (live error: "NROL operator
  mode is not ported to provider 'umans' yet"). Fix: added `"umans"` to the
  allowlist as its own entry. **Umans is tracked as a distinct provider in
  the matrix, not folded into "claude"** — the labeling is a signpost so a
  future debugger reading the allowlist sees umans was considered and
  ported, rather than re-deriving it from the claude_client launch path.
  Regression: `tests/test_operator_parity.py::test_operator_guard_allows_umans`.

- **Guard (Step 0):** `NROL_OPERATOR_PROVIDERS` allowlist in server.py,
  checked at conversation creation (400) and at generation dispatch. The
  actual hole was the per-generation model picker — operator convs are
  forced to mode=claude at creation, but cc_model routing happens per
  generation. The guard stays as the safety net for future providers.
- **Codex port:** operator threads launch sandbox=read-only +
  approvalPolicy=never (set in BOTH thread/start and turn/start), so
  shell writes and escalations fail with no clickable prompt. OPERATOR.md
  lands as `AGENTS.md` in the workspace (the app-server baseInstructions
  field is deliberately unused — it replaces codex's defaults wholesale
  instead of adding role rules). Thread MCP surface = exactly nrol-ao +
  web-tools; that mirrors --strict-mcp-config only while
  `~/.codex/config.toml` carries no mcp_servers of its own (true
  2026-06-11) — if user-scope servers ever appear, emit `enabled=false`
  overrides per foreign server. Documented delta vs claude: codex cannot
  drop its shell, so the guarantee is "shell exists but cannot write";
  reads remain possible (info exposure on the operator's own machine, no
  authority risk).
- **agy port:** the excludeTools/coreTools hope did not survive contact —
  the installed agy CLI exposes no tool-restriction settings (verified
  via --help and settings.json, 2026-06-11), so there is no true tool
  removal on this provider. Enforcement is the permission hook's NROL
  deny-list keyed on `LOOM_NROL_OPERATOR` (the same proven combination
  backstage mode uses: --dangerously-skip-permissions skips agy's own
  approvals, never the PreToolUse hook; Write/Edit/Bash deny with no
  prompt). The `--sandbox` flag is omitted, unverified: bare headless
  smoke runs hang with and without it outside the Loom harness
  (2026-06-11), so it could not be validated; the hook deny-list does
  not depend on it. OPERATOR.md
  lands as `GEMINI.md`; `.agents/mcp_config.json` carries exactly
  nrol-ao + web-tools. Open verification: whether workspace
  mcp_config.json replaces or merges with global agy MCP config — probe
  via acceptance test (d).

Corrections to the original plan: `run_codex`/`run_gemini` have 2 call
sites each (primary + resume-fallback), not 3.

### Acceptance (per provider, live)
(a) operator attempts a direct topic-JSON write -> blocked, no prompt;
(b) MCP commit -> browser approval appears; (c) probe "state your role
rules" -> the operator recites OPERATOR.md content; (d) probe "list your
tools/MCP servers" -> only nrol-ao + web-tools surface.

## Synthetic-topic replay harness (started 2026-06-11)

End-to-end acceptance for the whole epistemic pipeline against authored
ground truth: a fictional topic (Strait of Meridia reopen), an authored
90-day timeline with gold typed transitions, and two replay lanes — the
oracle lane (gold transitions straight through submit_transition = the
deterministic reference trajectory) and the pipeline lane (generated
articles -> matcher -> commits), whose divergence from the oracle is
perception error isolated from engine math. Plan file:
`~/.claude/plans/it-seems-to-be-smooth-bee.md`.

- ✅ Simulation clock (2026-06-11): `NROL_AO_AS_OF` pins engine AND
  governor "now" (temp-repo 4aa250f, 286ed79); deadline eliminations,
  dayCount, R_t freshness all run in simulated time. Found+fixed live
  bug: a future-dated lastUpdated crashed R_t (log2 of negative).
- ✅ Evidence dated by publication (2026-06-11): article `published`
  flows into evidence `time` through _evidence_entry/commit_match — late
  processing no longer falsifies evidence dates (live fix too).
- ✅ Oracle lane green (2026-06-11): `tests/synthetic/replay.py` +
  `tests/test_synthetic_replay.py` (8 assertions: commits, sum-to-1,
  deadline floors in simulated time, PARK/SCHEMA_GAP no-ops, refire
  attenuation, baseline observation as visible non-update, convergence
  on authored truth H3=0.98, determinism). Authoring against the real
  design gates surfaced the conventions: posteriorEffect text IS the
  coverage matrix; >15pp moves need >=2 evidence_refs; saturating past
  0.85 needs a red-team record within 30 (simulated) days.
- ✅ Corpus committed (2026-06-11): 31 Haiku-authored articles via
  `tests/synthetic/generate_corpus.py` — gold labels copied from the
  timeline by the script (Haiku never sees the indicator schema), dupes
  as dedup bait, awkward-unit observables, authored near-misses. The
  world bible is time-aware (generation caught July articles citing the
  August escort op); `--scan` validates leakage/anachronism/label-drift
  and runs in CI as test_corpus_is_valid. Spot-check gate: corpus is
  provisional ground truth until a human reviews sampled articles.
- ✅ Pipeline lane + scoring (2026-06-11): `replay.py --lane pipeline`
  (per-day submit_article -> run_matcher_with_llama(Qwen, temp 0) ->
  auto-commit) + `score.py` (TV divergence naming culprit articles,
  confusion matrix, Brier at authored resolutions). First run: both
  lanes converge on authored truth (oracle H3=0.98, pipeline H3=0.9833;
  peak TV 0.079 day 1 decaying to 0.003). The matcher reproduced the
  live hormuz failure on day 1 — three duplicate articles, three FIREs
  of one causal event — bounded by lr_decay; dedup belongs in the
  perception layer. Off-diagonal errors all conservative (FIREs parked,
  distractors parked-not-ignored = review-debt generators); E06
  SCHEMA_GAP and E07 PARK bait both handled correctly; OBSERVE
  extraction 5/6 exact, awkward-units piece within 2pp. Matcher-path
  evidence now also dated by publication (engine repo 59f439a).
- ✅ Duplicate-FIRE fix, measured (2026-06-11, engine repo 88da253):
  apply_decisions bundles same-batch FIREs on one indicator the way it
  bundled OBSERVEs — canonical article fires once, duplicates parked as
  corroborating evidence_refs; design gate warns on explicit
  lr_decay >= 1.0 (duplicate amplifier). Harness re-run as regression
  test: day-1 TV collapsed 0.079 -> 0.000 (first four simulated days now
  track the oracle exactly); final posteriors unchanged (H3=0.9833).
  The honest cost: peak divergence moved to E04 at 0.135 — the old run's
  lower mid-run TV was two errors canceling (engine duplicate over-count
  toward H4 masking the matcher's genuine miss of E04's gold FIRE, also
  toward H4). Same for Brier H2@Sep-1 (0.164 -> 0.205): the old number
  was flattered. Remaining divergence is pure perception error, and its
  direction is conservative (missed FIREs = underconfidence). Scorer now
  dedupes multi-block decisions per article (last-wins, mirroring
  apply_decisions) after the matcher emitted two OBSERVEs for one
  article carrying two metrics. v2 (not started): cross-day duplicate
  detection — LLM yes/no "same causal event?" filed as a typed
  duplicate-of proposal, biased toward duplicate when uncertain.

## Deliberation layer — honest accounting (2026-06-12)

**The deliberation layer was underbuilt, and prior descriptions of it
(in this file's history, in scan-tick docstrings, and in session
reports) let design intent stand in for implementation.** What actually
existed through engine commit 88da253: the advocate/rebut/jury debate
ran ONLY as PARK rescue — `build_advocate_prompt(topic, articles,
parks)`, jury overrides hard-gated on `action["kind"] == "PARK"`. FIREs
were never red-teamed, accepted OBSERVEs were never red-teamed, IGNOREs
were never reviewed for false negatives, and duplicate-event judgment
was not a deliberation step (only the mechanical same-batch bundle from
88da253). One-pass matcher output flowed to apply/proposals for every
posterior-moving action. Discovered live 2026-06-12 when the operator
queue drained suspiciously fast and a codex audit traced the gap.

Standing rule, here as protocol: **claims about NROL-AO behavior must
cite code, a test, or a live probe.** "Deliberation" in any doc or
report means the generalized debate below, nothing thinner.

Patches (codex/gemini-authored, 2026-06-12, pending commit): advocate
runs over all FIRE/OBSERVE/PARK candidates; jury emits typed verdicts
(COMMIT / PARK / WITHDRAW / DUPLICATE_OF / SCHEMA_GAP, unknown ->
PARK); explicit duplicate grouping before apply with the mechanical
same-batch grouping as fallback; scan path fails closed when
deliberation errors (queue untouched, window left open); deliberation
runs with reasoning mode on. Capability suite green (64 passed) incl.
+112 lines of new tests.

- ✅ Deliberation as a capability constraint (2026-06-12): the authority
  layer now REFUSES posterior-moving actions without a deliberation
  record or an explicit waiver — skipping deliberation silently is not
  expressible. One rule, one place: `_require_deliberation`
  (mcp_servers/nrol_ao/server.py), enforced at submit_transition
  (chokepoint for all manual + proposal commits), propose_match (filing),
  commit_match (refuses undeliberated/legacy pending rows — the queue is
  not a path around the gate), and apply_matcher_output /
  run_matcher_with_llama (debate-by-default before apply; skipping needs
  a waiver). Waivers are recorded on the evidence entry
  (deliberationWaiver, carried by engine add_evidence b809d1a), in the
  activity ledger, and in the Loom approval payload — the human approving
  a commit sees the jury verdict or the confession that there is none.
  New tool `deliberate_candidates` exposes the debate to operators.
  Pinned by 8 gate tests (refusals, waiver/record stamping, legacy-row
  refusal, empty-debate-mints-no-stamps); suites green (73 passed).
  Oracle lane carries the waiver "gold transitions are authored ground
  truth"; fast pipeline lane carries "measuring the one-pass matcher".
- ⬜ Meridia deliberative lane measurement: `replay.py --lane
  deliberative` (matcher -> advocate -> rebut -> jury -> apply) scored
  against the fast lane and the oracle. Acceptance: endpoint no worse,
  duplicate move decisions reduced (E01/E11 clusters), wrong-direction
  FIREs caught by rebuttal, PARK rescue still works.
- ✅ Reviewed schema evolution + replay tooling (2026-06-12): schema gaps
  now have first-class operator tools: list gaps, run resolver, list/mark
  extension proposals, and apply an approved proposal. Application uses
  the engine cleanup-session gate and changes schema only; it does not
  replay evidence or move posteriors. Stored scans can now be listed,
  inspected, and replayed in dry_run / proposal_only / safe_apply modes.
  `commit_policy="safe"` regression fixed at the splitter: apply_decisions
  never receives FIRE/OBSERVE, including duplicate-map members.
- ✅ Cross-day duplicate judgment tool (2026-06-12): operators can run
  `review_duplicate_candidate` on a FIRE/OBSERVE candidate; it retrieves
  recent plausible evidence and asks the local model for a typed
  DUPLICATE_OF / UNIQUE_EVENT / UNCERTAIN_DUPLICATE verdict. It is
  perception-only (no mutation) and exists for the month-old re-report
  class that same-batch grouping cannot catch.
- ⬜ Head-fake world (decree walked back) so credulity and duplicate
  amplification are punished, not forgiven, by the corpus.
- ⬜ Provenance carryover: add_evidence drops surfaced_via / scanRound /
  queryProvenance (whitelist; docstring corrected in engine b809d1a) —
  carry them explicitly like the deliberation fields.
