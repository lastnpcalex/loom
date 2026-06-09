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

### Stage 0 — Define "correct" executably (tasks #1–#7)

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

- NROL-AO operator mode in Loom (see decision above).
- Fail-closed permission default in the MCP server (today: no LOOM_CONV_ID
  + flag unset → commit proceeds ungated).
- State/code split: `NROL_AO_STATE_DIR` owned by the MCP server; repo
  becomes code, state becomes data with one writer.
- Proposal lifecycle: `submit_article` → `propose_match` →
  `commit_match(proposal_id)` with persistent proposal store — review
  queue + per-commit provenance.
- Deprecate legacy `--posteriors` paths for active topics.

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
