# Bug: Orphaned "Generating..." message after cancelling generation

## Description
When a user cancels an active generation, a ghostly semi-transparent "Generating..." message remains visible in the chat after the cancel dialog is closed, instead of disappearing cleanly.

## Expected Behavior
On cancel, the streaming message should be removed from the DOM entirely and replaced with a retry bar ("Generation cancelled") and a fresh message list.

## Observed Behavior
A semi-transparent "Generating..." indicator persists in the message area after cancellation.

## Root Cause Analysis

### Cancellation flow (`static/chat.js`)

The `cancelled` WebSocket event handler (line 923-936) does the right thing:
1. Calls `removeStreamingMessage()` — removes `streamingDiv` from DOM and nulls the reference
2. Sets `State.isStreaming = false`
3. Calls `hideGenStatus()` — hides the `#generation-status` bar
4. Calls `showRetryBar('Generation cancelled')`
5. Calls `loadMessages(State.currentConvId)` to reload the message list

The issue is likely a **race condition** or **DOM state conflict** between steps 1 and 5:

### Likely Causes

1. **Multiple "Generating..." elements**: There are four places that render "Generating..." UI:
   - Status bar (`#generation-status`, line 274-282)
   - Live streaming div (`appendStreamingMessage`, line 3195-3226)
   - Draft messages in chat list (`createMessageElement`, lines 2577-2579)
   - Tree view ghost nodes (`tree.js`, lines 900-908)

   `removeStreamingMessage()` only removes the live streaming div. If a draft message element exists (rendered by `createMessageElement` with `isDraft=true`), it will survive the cancel. When `loadMessages` re-renders, if the server still considers it a draft (partial content was saved), it re-renders the "Generating..." text.

2. **`loadMessages` async race**: `loadMessages(State.currentConvId)` is called at the end of the `cancelled` handler. If the server-side draft deletion hasn't completed, the reload will still include the draft message, re-rendering it with "Generating..." text. There's no explicit wait for the server-side cleanup before reloading.

3. **CSS transition artifact**: The `.message-generating` CSS class (defined in `static/style.css` lines 1631-1652) may have a transition/opacity animation that creates a visual "ghost" effect when the element is removed from the message list by `renderMessages()` but hasn't fully faded yet.

### Files Involved
- `static/chat.js`: lines 923-936 (`cancelled` handler), 3195-3226 (`appendStreamingMessage`), 3380-3387 (`removeStreamingMessage`), 2577-2579 (draft rendering)
- `static/tree.js`: lines 900-908 (tree ghost nodes — less likely but possible if tree view is active)
- `static/style.css`: lines 1631-1652 (`.message-generating` styling)
- `server.py`: lines 3216-3249 (server-side cancel handler), lines 5064-5085 (partial draft save on CancelledError)

### Reproduction Steps
1. Send a message that triggers generation
2. Click the cancel button while the "Generating..." indicator is visible
3. Observe: semi-transparent "Generating..." text remains in the chat area

### Suggested Fix
1. Ensure `loadMessages` is called AFTER the server has fully cleaned up the draft (add a brief delay or a server-side confirmation event)
2. Remove ALL draft elements from the DOM in the `cancelled` handler, not just the streaming div — check for elements matching `.message-generating` or `[data-draft]` selectors
3. If using CSS transitions, ensure `removeStreamingMessage` waits for transition-end or uses `remove()` with forced reflow
