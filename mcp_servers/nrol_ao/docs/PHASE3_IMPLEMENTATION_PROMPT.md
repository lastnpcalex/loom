# NROL-AO Track A Phase 3 Implementation Prompt

Use this prompt for the next implementation agent.

```text
You are implementing NROL-AO Track A Phase 3 in:

C:\Users\exast\OneDrive\Documents\Loom2\a-shadow-loom

Primary architecture doc:
mcp_servers\nrol_ao\docs\ARCHITECTURE_UPDATE.md

Read first:
- mcp_servers\nrol_ao\docs\ARCHITECTURE_UPDATE.md
  - §A.2 Engine-side tool surface
  - §A.3 Deliberation as subagents
  - §A.6 Migration path
  - §4.1 Track A verification
  - §6 Tool calls are necessary but not sufficient
- mcp_servers\nrol_ao_engine\engine_agent.py
- mcp_servers\nrol_ao_engine\advocate_agent.py
- mcp_servers\nrol_ao_engine\tools\advocate.py
- mcp_servers\nrol_ao_engine\tools\read.py
- mcp_servers\nrol_ao_engine\tools\__init__.py
- tests\test_nrol_ao_engine_agent_advocate.py
- C:\Claude-Code\NROL-AO\temp-repo\framework\news_observation_pipeline.py
  - legacy build_rebut_prompt
  - legacy build_jury_prompt
  - legacy rebut/jury parsers

Goal:
Implement Track A Phase 3: rebuttal + jury deliberation as typed tool calls.
This phase produces a full advocate -> rebut -> jury deliberation packet, but
still does NOT commit, mutate topics, move posteriors, or write to the proposal DB.

Current shipped baseline:
- Phase 0.5 PASS: Dream handles long multi-paragraph JSON tool arguments cleanly.
- Phase 1 PASS: mcp_servers/nrol_ao_engine package, fetch_article, minimal tool-call loop.
- Phase 2 PASS: read tools + propose_advocate + advocate_agent.run_advocate.
- Important Phase 2 finding: keep the system prompt terse and imperative. Put rich
  requirements in tool descriptions. Do not use verbose "you have tools" prompts.
- Important schema finding: read_indicator_schema returns real indicator fields:
  id, tier, desc, likelihoods, posteriorEffect, observable, shape, target_hypothesis.
  Do NOT idealize this into top-level direction or midpoint. Direction, when present,
  lives inside observable.direction.

Hard constraints:
- No commits.
- No topic mutation.
- No posterior movement.
- No proposal DB writes.
- No second MCP server process.
- Do not delete legacy prompt builders/parsers yet.
- Do not move engine code or state yet.
- Do not touch C:\Claude-Code\NROL-AO\temp-repo except read-only inspection.
- Do not introduce direct paths around existing Loom approval/governance gates.

Implementation tasks:

1. Add rebuttal tool: mcp_servers/nrol_ao_engine/tools/rebut.py
   - Tool name: propose_rebut
   - Arguments:
     - article_id: string
     - advocate_proposal_id: string
     - verdict: enum ["COMMIT", "PARK", "WITHDRAW", "DUPLICATE_OF", "SCHEMA_GAP"]
     - objection_raised: boolean
     - objection_details: string
     - corrected_action: object
       - kind: enum ["FIRE", "OBSERVE", "PARK", "IGNORE", "SCHEMA_GAP"]
       - optional indicator_id
       - optional value
       - optional parent_idx
     - rebuttal_analysis: string
   - Tool description must demand:
     - multi-paragraph rebuttal_analysis
     - explicit reference to at least one advocate claim/proposal/action
     - citation of real indicator ids or article evidence where relevant
   - Store records in memory only, similar to tools/advocate.py.
   - Return {rebuttal_id, recorded: true}.
   - Validate defensively and return structured errors, but rely on the OpenAI tool
     schema for primary enum enforcement.

2. Add jury tool: mcp_servers/nrol_ao_engine/tools/jury.py
   - Tool name: submit_jury
   - Arguments:
     - article_id: string
     - advocate_proposal_id: string
     - rebuttal_id: string
     - final_action: object
       - kind: enum ["FIRE", "OBSERVE", "PARK", "IGNORE", "SCHEMA_GAP", "DUPLICATE_OF"]
       - optional indicator_id
       - optional value
       - optional parent_idx
       - optional description
     - jury_rationale: string
   - Tool description must demand:
     - multi-paragraph rationale
     - explicit reference to both advocate and rebuttal records
     - explanation of why the final action accepts, modifies, or rejects the advocate proposal
   - Store records in memory only.
   - Return {verdict_id, recorded: true}.

3. Register both tools in mcp_servers/nrol_ao_engine/tools/__init__.py.

4. Add Phase 3 runner:
   - Prefer a new module: mcp_servers/nrol_ao_engine/deliberation_agent.py.
   - Expose run_deliberation(slug, articles).
   - It may call advocate_agent.run_advocate(slug, articles) first, then run rebut and jury.
   - The rebut stage should receive:
     - article text/metadata
     - real indicator schema context, either via read_indicator_schema call or injected
       context from the advocate trace
     - the full advocate proposal records including analysis
   - The jury stage should receive:
     - article text/metadata
     - full advocate records
     - full rebut records
   - Keep prompts terse and imperative. Example shapes:
     - Rebut: "Read the advocate proposals. For each article, call propose_rebut with objections or agreement."
     - Jury: "Read advocate and rebuttal records. For each article, call submit_jury with the final action."
   - Put the detailed quality requirements in tool descriptions, not the system prompt.
   - Return:
     {slug, advocate_proposals, rebuttals, jury_verdicts, traces}

5. Tests:
   - Add tests/test_nrol_ao_engine_agent_deliberation.py.
   - Mocked/unit tests:
     - propose_rebut records and validates expected fields.
     - submit_jury records and validates expected fields.
     - bad action/verdict inputs return structured errors.
     - run_deliberation with a fake Dream client runs advocate -> rebut -> jury and returns all records.
     - no topic mutation / no commit path: grep-level or mock-level assertion that Phase 3 modules do not import pipeline.process_evidence, save_topic, proposal DB stores, or transition commit functions.
   - Live test, skipped when Dream :8787 is unavailable:
     - Run 1-2 real Hormuz articles through run_deliberation.
     - Assert:
       - advocate analysis > 400 chars and cites a real indicator/evidence/hypothesis id.
       - rebuttal rebuttal_analysis > 300 chars.
       - rebuttal references an advocate proposal id or a concrete advocate action/indicator.
       - jury jury_rationale > 300 chars.
       - jury references both advocate and rebuttal ids or their concrete contents.
       - final action is structured and schema-valid.
       - no topic JSON mtime changes and no proposal DB writes.

6. Update docs only after implementation facts exist:
   - Update ARCHITECTURE_UPDATE.md §4.1 Phase 3 with exact test/live results.
   - Do not rewrite the whole architecture doc.

Verification commands:
- C:\Python314\python.exe -m py_compile mcp_servers\nrol_ao_engine\*.py mcp_servers\nrol_ao_engine\tools\*.py
- C:\Python314\python.exe -m pytest tests\test_nrol_ao_engine_agent.py tests\test_nrol_ao_engine_agent_advocate.py tests\test_nrol_ao_engine_agent_deliberation.py -v -k "not live"
- If Dream is up, run the live deliberation test explicitly with the existing live marker/env convention.

Report back:
- Files changed.
- Tests run and results.
- Live result details: analysis lengths, cited ids, final actions.
- Any malformed tool-call behavior or prompt failures.
- Any safety concern around mutation/commit paths.
```
