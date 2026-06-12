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

- Scheduled scans via MCP with auto-commit policy: PARK/SCHEMA_GAP auto,
  OBSERVE auto only for clean numeric official sources, FIRE always
  human-approved. Daily digest surfaces non-updates as first-class output.
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
