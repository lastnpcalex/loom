# Open Loom Bugs — Grounded but Not Fixed

A running list of bugs the user has reported that I (Claude/Braid) have
partially characterized but not root-caused. Each entry includes what's
already verified vs. what still needs investigation.

---

## "Messages disappear when session limit hits" — 2026-06-25

**Symptom:** Mid-generation hits Claude Code's 5-hour cap or similar
rate-limit. UI shows only the rate-limit error bubble; all prior rendered
messages disappear from view. The DB still has them.

**Reported by user:** multiple sessions, "i feel like i ran into another bug
recently, but i give up honestly" (2026-06-25). Also previously: "when a
claude or other turn is interrupted by hitting a session limit, all prior
messages which were rendered, disappear".

### What's grounded

- The `error` WS event handler at `static/chat.js:988-1010` does **not**
  touch `State.messages`. It calls `markStreamingMessageInterrupted` then
  `removeStreamingMessage` (conditional).
- **Strong suspect**: `removeStreamingMessage()` at `static/chat.js:3704-3713`
  removes `streamingDiv` AND
  `document.querySelectorAll('.message-generating').forEach(el => el.remove())`.
  If multiple message DOM nodes carry the `.message-generating` class (e.g.,
  from a prior incomplete render, or a draft message from another branch),
  this nukes all of them on error.
- `loadMessages` walks the active tree path; prior messages remain
  `is_active=1` after a rate-limit, so a subsequent reload should restore
  them. The disappearance appears to be **render-only, not DB-level**.
- DB evidence (conv 161): msg 17586 (user, 78 chars, `is_active=0`) is the
  abandoned-reply-after-rate-limit. Its sibling 17588 is `is_active=1` —
  branching state is correct. The *transient* render after the error is
  what shows nothing.

### To root-cause

1. Reproduce with the new debug overlay live (force a generation, kill via
   admin endpoint to simulate error, watch the overlay).
2. Right after the error event fires, in DevTools or `evaluate_script`:
   ```js
   ({
     messagesCount: State.messages.length,
     visibleMessages: document.querySelectorAll('#messages .message').length,
   })
   ```
3. **If `State.messages` is unchanged but visible count is low**: bug is in
   `removeStreamingMessage` over-removing `.message-generating`. Fix: scope
   the querySelectorAll to only remove drafts that are NOT in `State.messages`
   with non-empty content, or only remove the specific `streamingDiv`.
4. **If `State.messages.length` itself drops to 0**: bug is upstream — in
   `error` handler logic, or in `loadMessages` walking the wrong path.

### Adjacent code to read

- `static/chat.js:988-1010` — error event handler
- `static/chat.js:3704-3713` — `removeStreamingMessage`
- `static/chat.js:3725-3739` — `markStreamingMessageInterrupted`
- `static/chat.js:223-289` — `loadMessages` and tree walk
- `server.py` rate_limit_data handling around line 5825-5860 — what server
  writes to the draft message on rate-limit

### Likely simple fix (if hypothesis is right)

In `removeStreamingMessage`, replace:
```js
document.querySelectorAll('.message-generating').forEach(el => el.remove());
```
with a guard that only removes generating-class elements whose msg id is NOT
in `State.messages` with non-empty content. Or scope the selector to a
specific element ID rather than class.

---

## (Other open items)

- **`is_gemini_model` (server.py) vs `is_gemini` (model_context.py) alias
  mismatch.** Server treats `"Claude Sonnet 4.6 (Thinking)"`, `"Claude Opus
  4.6 (Thinking)"`, `"GPT-OSS 120B (Medium)"` as gemini-routed (correct —
  they're agy-served models per user). model_context doesn't know about
  this alias and falls them through to local-llama threshold. Real bug,
  low-priority (threshold issue, not freeze). Fix: move the alias table into
  model_context.py, have both files consume from there.

- **Bash/PowerShell failures in non-operator conversations.** User reported
  but no concrete reproducer captured. Likely either intentional operator
  deny (if conv is operator-flagged) or path-escaping issue (Git Bash
  mangling `C:\…` to `C:…`). Need a real example with conv id + command
  + error before acting.

- **agy `-p` mode buffered vs incremental writes.** Verified empirically on
  2026-06-25 that agy DOES flush incrementally — earlier theory was wrong.
  The "frozen 11 minutes during agy turn" symptom is most likely the same
  `_reconstructing` gate issue as above, NOT a buffer-at-exit issue. Apply
  the same diagnostic (overlay + endpoint) when it recurs.
