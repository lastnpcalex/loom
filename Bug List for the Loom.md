# Bug List for the Loom

## Continued Behavior bugs

* [ ] **Agents keep forgetting loom + admin server run via https **
* [ ] **Local agensts "I keep forgetting the Bash tool uses POSIX sh, not PowerShell. Let me use the PowerShell tool instead."**

## Branching, Tree State, and Conversation Routing

* [ ] **Add optional “git tree” branching mode for parallel conversations**

  * If we fully lock multiple threads in the same tree, we lose the ability to run parallel conversations.
  * Proposed design: make parallel tree branching explicit and optional, instead of allowing accidental concurrent branch activity.

* [ ] **Model selection dropdown should default to the last model used on that branch**

  * Current behavior appears to use the global last-used model.
  * Expected behavior: each branch should remember its own last-used model.

## Loom editing message that is queued.

* [ ] ** Loom editing queued message ux**
    * When a message is queued, waiting for an LLM turn to end, it can be edited. currently, the only buttons when editing are "cancel, and "send as new branch" there is no way to accept the edit on the UI, making the editing of a queued message useless. 
    * sometimes in very fast moving sessions the button for the cancel a queued message is hard to hit because everything on the screen moves too fast. sub-optimal ux, though not as important as the prior issue.

## Search, Navigation, and Bookmarking

* [ ] **Fix search mode inside trees**

  * Search works from the main home page.
  * After clicking into a conversation, finding specific posts becomes extremely hard.
  * Search often glitches badly enough that the window has to be killed.
  * In some cases, the full Loom app needs to be restarted to recover operation.

* [ ] **Fix bookmark landing behavior**

  * Current behavior: loading a conversation from a bookmark does not reliably land the user on the bookmarked message.
  * Expected behavior: bookmarked messages should open directly at the bookmarked message location.

## AGY Session Handling

* [ ] **Fix AGY history rebuild / session resume behavior**

  * On subsequent turns, AGY often rebuilds history instead of resuming the existing AGY session.
  * This burns tokens turn by turn by re-reading build history.
  * Expected behavior: AGY sessions should resume cleanly after the first message.

* [ ] **Append prior-run user context when switching companies**

  * AGY needs user context from prior runs appended when the user has switched companies.
  * After that first context-appending message, AGY sessions should be resumable.

* [ ] **Write a working script for AGY to parse the message/context payload**

  * Current behavior causes too much repeated history-reading.
  * Expected behavior: build history/context should already be available on each resume and parsed correctly.

* [~] **Stabilize AGY session lifecycle**

  * AGY remains extremely buggy.
  * Symptoms include:

    * History rebuilds acting amnesiac. — **FIXED for NROL-operator convs 2026-06-26**: agy-operator turns launched fresh-conv (no `--conversation`, by design) but `server.py` still took the resume short-circuit (`prompt = latest_user_content` — one line, trusting the provider to hold history). Correct for codex (stateful `thread/fork`) and claude (stateful `--resume`), but agy is a stateless CLI with no server-side memory → the one-line prompt landed on a process with zero conversation history. Symptom: agy couldn't answer "what was the first message I sent?" and went filesystem-spelunking for prior-turn terms. Fix: `server.py` now forces the full-history-rebuild path (`_build_claude_history_prompt`) when `is_gemini and nrol_operator`, while keeping `resume_session_id` propagation intact for the client override. Regression test `tests/test_operator_parity.py::test_agy_operator_turn2_server_builds_full_history_prompt` guards the server-side half (the client-side half is `test_gemini_operator_turn2_forces_fresh_conv`). See memory `agy-operator-turn2-no-response`.
    * Strange subsequent-turn behavior. — **FIXED for NROL-operator convs 2026-06-26**: root cause was a launch/poller-mode contradiction. `--conversation` was suppressed for operator turns (correct: fresh tool registry per turn) but `use_resume`/`fork_session` stayed `True` (driven by the persisted turn-1 `cc_session_id`), so the poller pinned to the turn-1 transcript and skipped the new-UUID-folder scan while agy wrote its real output to a fresh folder the poller never inspected → `[Error: Antigravity (agy) exited with no response]` on every turn 2+. Fix: `gemini_client.run_gemini` now forces fresh-conv on both halves when `nrol_operator=True`. Regression test `tests/test_operator_parity.py::test_gemini_operator_turn2_forces_fresh_conv` guards the invariant; `tools/probe_agy_turn2.py` is the end-to-end repro. See memory `agy-operator-turn2-no-response`.
    * AGY periodically closing itself.
    * Resumed sessions not reliably continuing from the existing state.
  * AGY also continually uses test suites and waits, but it really just closes the session.'
  * AGY sometimes ends a session, but the loom never identifies it as finished, waiting for enternity, even though AGY has finished its turn.

* [ ] **Canceling AGY from Loom sometimes fails to kill the session**

  * Current behavior: cancel from the Loom UX may fail to terminate the AGY process/session.
  * Expected behavior: cancel should reliably kill the active AGY generation.

## Codex / AGY Plan Mode

* [X] **Plan mode currently does nothing in Codex or AGY**

  * Expected behavior needs to be defined.
  * Current behavior appears non-functional.

## Websocket and Long-Running Generation Stability

* [ ] **Fix persistent websocket issues on long sessions**

  * Websocket issues still occur, especially on long sessions.
  * Symptoms include unstable streaming and/or sessions failing to remain connected.

* [ ] **Local llama sometimes quits mid-turn**

  * Usually happens around the 3-minute mark.
  * Expected behavior: local llama should complete the turn or fail with a clear recoverable error.

* [ ] **Large history rebuilds can exceed context window and fail**

  * Affects Codex and local llama.
  * Current behavior: history rebuild can be larger than the context window, causing generation failure.
  * Expected behavior: history should be summarized, truncated, chunked, or otherwise bounded before generation.

## Hermes / Local Model Integration

* [x] **Hermes is labeled as claude in Hermes environments on the Loom**

* [x] **Hermes cannot locate the local model**

  * Current behavior: Hermes fails to find or connect to the local model.
  * Expected behavior: Hermes should detect the configured local model path/server reliably.

* [x] **Hermes sessions can become dead lingering active generations**

  * Current behavior:

    * Hermes sessions activate.
    * They fail to complete.
    * They still linger in active generations as long-running dead sessions.
  * Expected behavior: failed Hermes sessions should be cleaned up and removed from active generations.
