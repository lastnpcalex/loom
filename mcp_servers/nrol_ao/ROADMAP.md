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

## Multi-provider operator parity — codex + agy (planned 2026-06-11)

Observed live: a Codex-model operator ran shell freely in the operator
workspace (rg of parent dirs) and had no OPERATOR.md — the lockdown is
claude_client-only. One framing fact before the plan: **the authority gate
is already provider-agnostic.** `_ask_loom_permission` lives inside the MCP
server, so no model can commit a posterior through the MCP without the
browser prompt. The gap is the side-channel (shell writes to topic JSON
bypassing the engine entirely) and the missing role instructions.

Claude lockdown today, for parity reference: `--disallowedTools Write, Edit,
NotebookEdit, Bash, Agent, Task, KillShell, SlashCommand` + reads/web kept
deliberately (operators read sources) + `--strict-mcp-config` (nrol-ao +
web-tools only) + OPERATOR.md via `--append-system-prompt`.

### Step 0 — guard (ship first, ~10 lines)
server.py refuses to create or generate an `nrol_operator` conversation on
a codex/gemini model with a clear error naming this roadmap entry. Removes
the silent-hole state while the ports land.

### Codex port (codex_client.py + server.py call sites)
1. Thread `nrol_operator: bool` into `run_codex` (3 call sites).
2. Sandbox: operator conversations get a **read-only** sandbox policy
   (today: workspace-write) and an approval policy that auto-DENIES
   escalation — write attempts fail instead of raising a clickable prompt.
   Codex cannot drop its shell, so the guarantee becomes "shell exists but
   cannot write"; reads remain possible (acceptable: info exposure on the
   operator's own machine, no authority risk). Document this delta.
3. Instructions: write OPERATOR.md into the operator workspace as
   `AGENTS.md` at conversation creation (codex auto-loads it from cwd);
   also pass via the app-server instructions field if supported.
4. MCP config: operator conversations get ONLY nrol-ao + web-tools
   (mirror --strict-mcp-config semantics when building the codex config).

### agy/Gemini port (gemini_client.py)
1. Same `nrol_operator` threading.
2. gemini-cli supports tool restriction via settings (coreTools /
   excludeTools — verify exact keys for the installed version): write a
   per-conversation `.gemini/settings.json` into the workspace excluding
   shell/write/edit tools, with mcpServers = nrol-ao + web-tools only.
   This can reach closer to claude-parity than codex (true tool removal).
3. Context: OPERATOR.md as `GEMINI.md` in the workspace (auto-loaded).

### Shared acceptance test (per provider)
(a) operator attempts a direct topic-JSON write -> blocked, no prompt;
(b) MCP commit -> browser approval appears; (c) probe "state your role
rules" -> the operator recites OPERATOR.md content (instructions landed).

### Sequencing
Blocked on the parallel session's uncommitted server.py / codex_client.py
work. Step 0 the moment that workspace settles; codex; then agy.
