# Goose / Antigravity Provider Contract Bug Report

Date: 2026-08-20

Scope: code audit of Loom behavior when leaving the mature Claude Code / Codex paths and using Goose ACP or Antigravity/agy. This report is based on source inspection plus the existing Goose test run, not a live Goose/agy repro session.

## Executive Summary

Claude and Codex are closest to the Loom provider contract: they have explicit session ownership, retry-on-bad-resume behavior, active-generation snapshots for websocket reconnects, token accounting, and mostly consistent cancel/finalization behavior.

Goose and Antigravity did not meet that same contract at the start of this audit. Fixed on 2026-08-20: Antigravity no longer layers Loom replay prompts onto an unrelated persistent `--conversation`, and Goose now has final-token persistence, compact-handoff gating, bad-resume retry, live snapshots, no-output error finalization, thinking-block merging, and subprocess serialization. Remaining risk is mostly around Goose's split permission UI path and live validation against the real Goose ACP binary.

The core issue is provider parity, not just isolated UI glitches. Loom needs a provider contract layer that every CLI/ACP adapter must satisfy before it is treated as interchangeable with Claude/Codex.

## Expected Provider Contract

Every provider lane should guarantee:

1. Bounded context before launch: compact, summarize, or otherwise cap prompt/session context before a model-specific limit is exceeded.
2. Single source of conversation truth: either resume provider-native state or replay Loom history, but do not mix both unless the adapter proves the merge is safe.
3. Cross-provider isolation: never resume a provider session created by another provider, and do not trust unscoped legacy session IDs.
4. Retryable stale-session handling: if a resumed/forked session errors or returns no useful content, retry once from bounded Loom history.
5. Live websocket recovery: active generations must keep `_generation_snapshots` current with text, tools, thinking blocks, token usage, parent/draft IDs, and mode.
6. Permission visibility: a permission request must be visible both inline and in notifications across reconnects, including no-snapshot providers.
7. Consistent cancellation: cancel must kill the right provider process tree and preserve or delete drafts using the same rules across providers.
8. Final metadata integrity: saved assistant messages must record `cc_session_id`, `cc_session_mode`, `cc_model_used`, token counts, and generation time with the database field names the schema actually accepts.

## Confirmed Findings

### P0 - Goose finalization uses invalid database keywords

Evidence:
- `server.py:7380` calls `db.update_message_content(...)`.
- `server.py:7384` passes `input_tokens=input_tokens`.
- `server.py:7385` passes `output_tokens=output_tokens`.
- `database.py:843` defines `update_message_content(...)` with `turn_input_tokens` and `turn_output_tokens`, not `input_tokens` / `output_tokens`.

Impact:
- A Goose turn can stream useful output, then fail at final save with `TypeError: update_message_content() got an unexpected keyword argument 'input_tokens'`.
- The broad Goose exception handler at `server.py:7420` catches that and overwrites the draft with an error-shaped branch. This burns the provider turn and makes the UI look like Goose failed after doing real work.
- Existing Goose tests pass because they assert launch/resume arguments, not the final saved assistant message.

Fix:
- Fixed 2026-08-20: replaced the bad keywords with `turn_input_tokens=` and `turn_output_tokens=`.
- Added a regression test that runs `_handle_goose_generation` with a fake text delta + usage event and asserts the saved message content, session mode, model, and token fields.

### P0 - Goose bypasses compact-handoff / large-context protection

Evidence:
- `server.py:5453` routes `mode == "goose"` to `_handle_goose_generation`.
- The compact-handoff gate is inside `_handle_claude_generation` at `server.py:5608` through `server.py:5625`.
- `_handle_goose_generation` builds/resumes prompt state at `server.py:7226` through `server.py:7243` without calling `db.sum_branch_tokens(...)`, `model_context.needs_handoff(...)`, or `_run_compact_handoff(...)`.

Impact:
- Goose can be launched against oversized Loom history or an oversized provider-native session until Goose fails internally.
- This directly matches "larger context causes errors instead of falling back or auto-compactifying."
- Goose OpenRouter selectors also are not modeled in `model_context.py`; `goose:openrouter:...` is not treated like the underlying OpenRouter model.

Fix:
- Fixed 2026-08-20 for Goose: `_handle_goose_generation` now checks branch tokens before launch, runs the existing compact handoff when the Goose target crosses its threshold, and then treats Goose as a cross-provider bounded-replay target.
- Fixed 2026-08-20: `model_context.py` unwraps `goose:` and `goose:auto:` selectors before applying OpenRouter/local thresholds.
- Longer-term cleanup: move context gating into a shared prelaunch function used by every provider lane.

### P0 - Goose resume/fork fallback is effectively unreachable for real session RPC failures

Evidence:
- `server.py:7265` wraps `goose_client.run_goose(...)` in a `try`.
- `goose_client.run_goose(...)` returns an async event stream immediately; the session RPCs are performed later inside `_event_stream`.
- The actual Goose resume/fork RPC happens at `goose_client.py:532` through `goose_client.py:541`.
- Therefore the server fallback at `server.py:7281` only catches launch-time failures, not `session/fork` or `session/load` failures.
- The shared Claude/Codex/AGY handler has post-stream bad-resume retry logic at `server.py:6429` through `server.py:6450`; Goose has no equivalent.

Impact:
- A stale or poisoned Goose session produces an error instead of falling back to a bounded history rebuild.
- Empty resume results are handled as terminal failure, not as "retry from Loom history."

Fix:
- Fixed 2026-08-20: `_handle_goose_generation` now catches async stream-time resume/fork failures and empty resumed turns, retires the bad process, clears partial state, and relaunches once from bounded Loom history with no `resume_session_id`.

### P0 - Goose was not serialized as a single subprocess provider

Evidence:
- The websocket generation router classified Claude, local, Hermes, Dream, AGY, Codex, Umans, and OpenRouter as single-subprocess modes.
- `goose` was missing from that set even though `_active_goose_procs` is keyed only by `conv_id`.

Impact:
- Parallel Goose turns in one conversation could race the same active-process slot and cancel/finalize inconsistently.
- This could contribute to "not finishing turns" and odd cancel behavior.

Fix:
- Fixed 2026-08-20: `goose` is now included in the single-subprocess provider gate.

### P0 - Goose does not maintain live generation snapshots

Evidence:
- `_generation_snapshots` is the reconnect source of truth (`server.py:1132`, `server.py:4418` through `server.py:4434`).
- The shared handler initializes and updates snapshots at `server.py:5927` through `server.py:5942` and `server.py:6417` through `server.py:6425`.
- Hermes and Weave have similar snapshot updates.
- `_handle_goose_generation` has no `_update_gen_snapshot(...)` call while processing stream events.

Impact:
- On websocket reconnect, Loom can know a Goose generation is active but cannot reconstruct its text/tool/thinking state.
- This explains dropped inline tool/permission/thinking UI while a notification entry may still exist.

Fix:
- Fixed 2026-08-20: Goose snapshots are initialized after draft creation and updated as events stream, including `mode="goose"`, `cc_model`, `draft_msg_id`, `parent_id`, content blocks, text, and usage.

### P0 - Permission prompts can be stranded during no-snapshot reconnects

Evidence:
- `static/chat.js:458` through `static/chat.js:462` queues stream events during reconstruction, but `permission_request` is not in `_RECONSTRUCT_QUEUE_EVENT_TYPES`.
- Permission requests arriving during reconstruction are pushed to `State._pendingPermPrompts` at `static/chat.js:879` through `static/chat.js:883`.
- The snapshot reconnect path drains those pending prompts at `static/chat.js:1156` through `static/chat.js:1164`.
- The no-snapshot reconnect path at `static/chat.js:1175` through `static/chat.js:1190` drains queued stream events but does not call `_drainPendingPermPrompts()`.
- Goose currently hits the no-snapshot path because it does not maintain snapshots.

Impact:
- A Goose permission can show in the notification bell but not inline in the stream after reconnect/rebuild.
- This matches "open/closing the websocket causes permissions requests in Goose to drop from the stream extremely often."

Fix:
- Fixed 2026-08-20: the no-snapshot `generation_active` branch now also calls `_drainPendingPermPrompts()`.
- Remaining cleanup: decide whether to add permission events to the reconstruction queue or rely solely on `State._pendingPermPrompts`; keep notification and inline rendering deduped by request ID.

### P1 - Goose permission events are split between two channels

Evidence:
- `goose_client.py:609` through `goose_client.py:618` yields a local `permission_request` event, then starts `_bridge_permission(...)`.
- The server ignores Goose `permission_request` events at `server.py:7364` through `server.py:7366`.
- The visible browser prompt is instead driven by `_bridge_permission(...)` posting to `/api/cc-permission` at `goose_client.py:348` through `goose_client.py:389`.

Impact:
- The Goose stream event says "permission requested", but Loom's actual prompt lifecycle is handled out-of-band.
- Timing differences between the event stream, the permission HTTP bridge, websocket reconnect, and frontend reconstruction can make the stream and notification bell disagree.

Fix:
- Pick one canonical permission path for ACP providers.
- Preferred: route Goose permission requests through the same server-side pending permission machinery, with `conv_id`, `gen_id`, `permission_scope`, and request fingerprint attached before frontend delivery.
- Keep the direct HTTP bridge only as the waiting/response mechanism, not as a second UI event source.

### P1 - Goose thinking blocks are not normalized as first-class blocks

Evidence:
- Goose maps only `agent_thought_chunk` / `thought_delta` to `thinking_delta` in `goose_client.py:281`.
- `_handle_goose_generation` appends a new thinking block for each chunk at `server.py:7328` through `server.py:7332`, instead of merging with the current thinking block like the shared handler does at `server.py:6270` through `server.py:6278`.
- There is no Goose `thinking_start` / `thinking_end` state.

Impact:
- Thinking blocks can be fragmented, empty, or impossible to reconstruct cleanly.
- If Goose emits thought metadata under a different ACP shape, Loom currently drops it into `goose_raw_update` instead of visible thinking content.

Fix:
- Fixed 2026-08-20: Goose thinking chunks are merged into the current thinking block instead of creating one block per chunk.
- Add parser coverage for actual Goose ACP thought event shapes.
- Render a non-empty placeholder only when thinking is known to exist but content is intentionally unavailable; otherwise do not create empty expanders.

### P1 - Antigravity mixed persistent agy conversation state with Loom history replay

Evidence:
- Fixed 2026-08-20: `gemini_client.run_gemini(...)` now passes `--conversation` only when `resume_session_id` is present.
- Before the fix, non-operator AGY turns always passed `--conversation resume_session_id or str(conv_id)`.
- Before the fix, on cross-provider or no-resume turns, the server built a full Loom history prompt, but AGY still got a persistent `--conversation` ID.
- Operator mode explicitly avoids this because comments at `gemini_client.py:817` through `gemini_client.py:823` say resuming agy carries old tool registry/context and causes unwanted compaction.

Impact:
- Before the fix, AGY could have two sources of memory: its internal conversation DB and the Loom replay prompt.
- If those disagree, contain previous handoff files, or contain prior markdown numbering, the model can continue from the wrong state.
- This aligns with "bizarre continuations" and "handoff number collisions in markdown files."

Fix:
- Implemented: native resume passes only the latest user message and `--conversation <resume_session_id>`.
- Implemented: Loom replay/handoff launches fresh with no `--conversation`, so the replay prompt is the only memory source.
- Remaining work: validate AGY's provider-native session ID emission on every fresh replay and add broader end-to-end coverage.

### P1 - Antigravity large prompts use a stable markdown filename and natural-language parsing

Evidence:
- Large prompts are written to `.agents/loom_prompt_<conv_id>.md` at `gemini_client.py:801` through `gemini_client.py:805`.
- AGY is told in natural language to read that file at `gemini_client.py:806` through `gemini_client.py:810`.
- Cleanup only runs after `proc.wait()` at `gemini_client.py:913` through `gemini_client.py:921`.

Impact:
- The same file path is reused across turns for a conversation.
- A hung/killed AGY process or overlapping/retried turn can leave or race the file.
- The model must parse a large markdown conversation payload itself, which is exactly the behavior the older bug list already called out as needing a real parser.

Fix:
- Use per-generation unique filenames including the generation ID or draft ID.
- Put a machine-readable envelope around the payload: explicit JSON metadata plus delimited messages.
- Add a tiny parser/instruction contract for "latest user message" instead of asking the model to infer it from markdown.
- Remove stale prompt files on startup or before launch.

### P1 - Antigravity session forking is fragile

Evidence:
- AGY fork support copies `brain/<session>` to a new UUID at `gemini_client.py:738` through `gemini_client.py:746`.
- It then copies and mutates SQLite/protobuf conversation files at `gemini_client.py:750` through `gemini_client.py:789`, including byte replacement of the old session ID in a `.pb` file.

Impact:
- This is a brittle approximation of a provider-native fork.
- Any hidden IDs, indexes, checksums, protobuf layout changes, or AGY schema changes can produce corrupted or semantically stale sessions.
- Failures here can look like schizophrenia: the branch ID is new, but internal AGY state can still point at old trajectory/cascade data.

Fix:
- Prefer AGY's native fork/export/import if available.
- If not available, disable same-provider AGY forking and use fresh stateless bounded replay.
- At minimum, validate copied AGY sessions before launch and fall back to fresh replay on any mismatch.

### P2 - AGY token accounting is incomplete at the UI boundary

Evidence:
- `_agy_usage(...)` preserves `thinking_tokens` at `gemini_client.py:499` through `gemini_client.py:515`.
- The shared server usage handler forwards only `input_tokens` and `output_tokens` at `server.py:6280` through `server.py:6294` and `server.py:6755` through `server.py:6768`.
- `static/chat.js:822` through `static/chat.js:833` renders only input/output token counts.

Impact:
- AGY can spend thinking tokens without the UI or saved turn metadata making that cost visible.
- This matches "not exporting tokens" for at least the thinking-token portion.

Fix:
- Add `thinking_tokens` to server usage aggregation, websocket payloads, saved turn metadata if desired, and frontend token display.

## Test Coverage Gaps

Existing command run:

```powershell
C:\Python314\python.exe -m pytest tests\test_goose_acp.py -q
```

Current result after the 2026-08-20 Goose fixes: 13 passed, with a `.pytest_cache` permission warning.

Coverage added 2026-08-20:
- Goose final persisted content, session metadata, and usage token fields.
- Goose resume/fork failure during event-stream startup.
- Goose empty resumed output fallback.
- Goose live snapshot updates and thinking-block merging.
- Goose selector unwrapping for context thresholds.

Remaining gaps:
- No browser E2E test covers full websocket reconnect during a real Goose permission request.
- No test covers permission prompt replay in the no-snapshot `generation_active` path.
- AGY tests cover stream-json happy path and some operator-mode fixes, but not normal-mode mixed memory, prompt-file collisions, or raw copied session validity.

## Recommended Fix Order

1. Run live Goose ACP handoff/resume/reconnect validation against the installed Goose binary.
2. Canonicalize Goose permission UI so the stream event and `/api/cc-permission` bridge cannot disagree.
3. Normalize provider cancellation through a provider-aware active process registry instead of treating `_active_claude_procs` as Claude-only while storing AGY/Codex there.
4. Continue hardening AGY memory mode after the 2026-08-20 launch fix: native resume uses `--conversation`, stateless Loom replay does not.
5. Replace AGY stable markdown prompt files with unique structured payloads and a parser contract.
6. Remove or harden AGY raw session-copy forking.
7. Add thinking-token and thinking-block parity tests for Goose and AGY using real provider event captures.

## Acceptance Criteria

- Switching from Claude/Codex to Goose or AGY either resumes a valid same-provider session or uses a bounded Loom replay. It never silently uses stale provider state.
- A large-context Goose or AGY turn compacts, truncates, or falls back before launching the provider, and the UI says what happened.
- Closing/reopening the websocket during a Goose permission request restores the inline prompt and notification consistently.
- Cancelling any provider kills the correct process tree and leaves either a useful partial draft or no draft, using the same rule everywhere.
- Empty-output and errored resumed sessions retry once from bounded history before saving an error branch.
- Saved messages consistently include provider session mode, model used, generation time, and token metadata.
