# Bug: Stream attaches to viewed branch, then drifts to generating branch during navigation

## Description
When a user is navigating the conversation tree (viewing one branch) while another branch has an active generation, the incoming stream chunks incorrectly attach to the currently viewed branch's message list. Then the UI "shifts" to show the branch where the generation actually landed — creating a confusing UX where the user is looking at one conversation, then unexpectedly jumps to another mid-stream.

## Expected Behavior
When viewing branch A while branch B is generating, incoming stream data for branch B should NOT appear in branch A's message list. The user should see branch A's messages unchanged, with an indication that another branch is generating (e.g., in the tree view or status bar).

## Observed Behavior
1. User is viewing branch A (no active generation)
2. Branch B is generating (either started before or triggered by another tab/session)
3. WebSocket stream chunks for branch B appear appended to branch A's message area
4. The UI then "shifts" to show branch B's content — the user is unexpectedly navigated away from branch A

## Root Cause Analysis

### Branch tracking (`_isOurBranch`, `static/chat.js`, lines 325-347)

The `_streamIsOurBranch` flag determines whether incoming WebSocket events belong to the currently viewed branch. The logic:
1. If following a specific `gen_id`, only match that one
2. If not streaming and tracking is established, reject stale parallel sibling events
3. If `parent_id` is available, check if it's in the current message chain
4. **Default: assume it IS ours** (corrected on `stream_start`)

**The bug lives in step 4**: The default assumption is "yes, it's ours." This means when the user navigates to branch A, `_streamIsOurBranch` may not be reset. When branch B's chunks arrive, they pass the `_isOurBranch` check and get appended to branch A's message area.

### `stream_start` reset (`static/chat.js`, lines 615-625)

On `stream_start`, the code does set `_streamIsOurBranch` based on the `gen_id` and `parent_id` match. However, this only fires for the branch that triggered the generation. If the user navigates to a different branch AFTER the generation started, `_streamIsOurBranch` is NOT re-evaluated.

### `switchToBranch` interaction (`static/chat.js`, lines 2901-2953)

When switching branches:
```javascript
async function switchToBranch(leafId, scrollToMsgId) {
    State.messages = branch;
    State.treeData = treeData;
    renderMessages();
    renderTree();
```

**Critical gap**: `switchToBranch` does NOT reset `_streamIsOurBranch` or `_followingGenId`. If the user navigates away from the generating branch, the flag still points to the old branch. Incoming chunks for the generating branch will then be incorrectly attached to the newly viewed branch.

### The "shift" mechanism

The "shift" the user observes is likely triggered by one of these:
1. **`refreshTree()`** (lines 3842-3851): Called on `stream_start`, `stream_end`, `cancelled`, `error`. Re-fetches the tree data and re-renders the tree view. This could update the active branch indicator, making the UI "jump."
2. **`loadMessages`** after `stream_end`: When the generation ends on branch B, `stream_end` calls `loadMessages`. If `_streamIsOurBranch` was false but chunks leaked through, the cleanup might trigger a reload that switches the view.

### Files Involved
- `static/chat.js`: lines 325-347 (`_isOurBranch`), lines 615-625 (`stream_start` handler), lines 2901-2953 (`switchToBranch`), lines 3842-3851 (`refreshTree`)
- `static/app.js`: lines 44-76 (`State` object — `messages`, `treeData`)
- `server.py`: WebSocket event dispatching for parallel generations

### Reproduction Steps
1. Start a generation on branch A (e.g., send a message)
2. While generation is active, open the tree view
3. Navigate to branch B (a sibling or earlier branch with no active generation)
4. Observe: stream chunks from branch A's generation appear in branch B's message area
5. Observe: the UI shifts/jumps to show branch A's content

### Suggested Fix
1. In `switchToBranch()`, reset `_streamIsOurBranch` to `false` and clear `_followingGenId` — the newly viewed branch has no active generation
2. On `stream_start`, only set `_streamIsOurBranch = true` if the generating branch IS the currently viewed branch (check `parent_id` against `State.messages` chain)
3. Add a visual indicator (e.g., status bar badge) when a generation is active on a different branch, so the user knows data is being generated elsewhere
4. On `generation_active` reconnect event, verify the snapshot belongs to the current branch before reconstructing the streaming UI
