"""Static UI invariants for tree-level branch sends."""

import re
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def test_tree_send_uses_generate_helper_even_when_websocket_is_not_open():
    source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")
    match = re.search(
        r"// If sent from tree view, switch to that branch in chat\s*"
        r"if \(isFromTree\) \{(?P<body>.*?)\n\s*return;\n\s*\}",
        source,
        re.S,
    )
    assert match, "tree send launch branch not found in sendMessage"
    body = match.group("body")

    assert "_triggerParallelGenerate(count, msg.id)" in body
    assert "readyState === WebSocket.OPEN" not in body


def test_tree_send_uses_selected_tree_branch_as_parent():
    source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")
    assert "function _getTreePromptParentId()" in source
    assert "treePromptParentId = isFromTree ? _getTreePromptParentId() : null" in source
    assert "msgData.parent_id = treePromptParentId || null" in source
    assert "State.treePromptParentId" not in source


def test_chat_send_uses_visible_leaf_as_parent():
    source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")
    assert "msgData.parent_id = _currentVisibleLeafId() || null" in source
    assert "parent_id: _currentVisibleLeafId() || null" in source
    assert "another agent may update the" in source
    assert "showToast(err.detail || err.message || 'Failed to send message', 'error')" in source


def test_parallel_ws_generate_requires_explicit_parent():
    source = (REPO / "server.py").read_text(encoding="utf-8")
    assert 'parallel_same_checkout and action == "generate" and "parent_id" not in data' in source
    assert "parent_id is required when parallel agents are enabled" in source


def test_stale_generate_reconnects_directly_with_pending_generate():
    source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")
    match = re.search(r"if \(stale\) \{(?P<body>.*?)\n\s*return;\n\s*\}", source, re.S)
    assert match, "stale websocket generate branch not found"
    body = match.group("body")

    assert "_pendingGenerate = { count: n, parentId: targetParentId }" in body
    assert "connectWebSocket(State.currentConvId)" in body
    assert "ws.close()" not in body


def test_tree_keeps_run_control_without_obsolete_prompt_button():
    source = (REPO / "static" / "tree.js").read_text(encoding="utf-8")

    assert "tree-node-send-here-btn" not in source
    assert "focusTreePromptParent" not in source
    assert "tree-node-run-btn" in source
    assert "runTreeUserMessage(data)" in source
    assert "switchToBranch(data.id, data.id, { exact: true })" in source
    assert "_triggerParallelGenerate(count, data.id)" in source


def test_tree_run_uses_exact_branch_snapshot_for_stream_parent():
    chat_source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")

    assert "async function switchToBranch(leafId, scrollToMsgId, options = {})" in chat_source
    assert "options.exact" in chat_source
    assert "API.get(`/api/conversations/${State.currentConvId}/branch/${leafId}`)" in chat_source


def test_tree_delete_confirm_uses_loom_modal():
    source = (REPO / "static" / "tree.js").read_text(encoding="utf-8")
    assert "showLoomConfirm({" in source
    assert "Delete branch?" in source
    assert "confirm(" not in source


def test_main_ui_uses_loom_confirm_instead_of_native_confirm():
    app_source = (REPO / "static" / "app.js").read_text(encoding="utf-8")
    tree_source = (REPO / "static" / "tree.js").read_text(encoding="utf-8")

    assert "function showLoomConfirm" in app_source
    assert "confirm(" not in app_source
    assert "confirm(" not in tree_source


def test_parallel_agents_same_checkout_ui_is_explicit():
    index_source = (REPO / "static" / "index.html").read_text(encoding="utf-8")
    app_source = (REPO / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="parallel-agents-enabled"' in index_source
    assert 'id="cfg-parallel-agents-enabled"' in index_source
    assert "Parallel agents in same checkout" in index_source
    assert "parallel_agents_enabled" in app_source


def test_parallel_branch_loading_is_tab_local_not_global_latest():
    app_source = (REPO / "static" / "app.js").read_text(encoding="utf-8")
    chat_source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")

    assert "sessionStorage.setItem(_tabBranchKey(convId), String(leafId))" in app_source
    assert "function findDescendantBranchLeaf(treeData, startId)" in app_source
    assert "if (next.length !== 1) return current" in app_source
    assert "sibling agent steal the tab" in app_source
    assert "return findDescendantBranchLeaf(treeData, remembered.id) || remembered" in app_source
    assert "findLoadableConversationLeaf(treeData, convId)" in app_source
    assert "findLoadableConversationLeaf(treeData, convId)" in chat_source
    assert "rememberConversationLeaf(State.currentConvId, loadedLeaf.id)" in chat_source


def test_parallel_reconnect_uses_visible_branch_snapshot_only():
    chat_source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")

    assert "snapshots.find(s => _streamParentMatchesVisibleBranch(s.parent_id, s.draft_msg_id))" in chat_source
    assert "State._followingGenId = snap.gen_id ?? null" in chat_source
    assert "const visibleDraft = State.messages.some" in chat_source
    assert "State._streamIsOurBranch = visibleDraft" in chat_source


def test_parallel_stream_ownership_requires_visible_leaf():
    chat_source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")

    assert "function _currentVisibleLeafId()" in chat_source
    assert "return _sameMessageId(parentId, _currentVisibleLeafId())" in chat_source
    assert "const isOnOurBranch = _streamParentMatchesVisibleBranch(parentId, data.draft_msg_id)" in chat_source
    assert "if (_OWNED_STREAM_EVENT_TYPES.has(data.type) && data.gen_id != null) return false" in chat_source
    assert "const targetParentId = parentId || _currentVisibleLeafId()" in chat_source


def test_parallel_stream_finalization_remembers_assistant_draft():
    chat_source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")

    assert "rememberConversationLeaf(State.currentConvId, data.draft_msg_id)" in chat_source
    assert "rememberConversationLeaf(State.currentConvId, snap.draft_msg_id)" in chat_source
    assert "rememberConversationLeaf(State.currentConvId, data.message.id)" in chat_source
    assert "rememberConversationLeaf(State.currentConvId, msg.id)" in chat_source


def test_parallel_permission_prompts_are_generation_scoped():
    chat_source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")

    assert "'permission_request', 'generation_idle'" in chat_source
    assert "function _permissionBelongsToVisibleBranch(data)" in chat_source
    assert "String(data.gen_id) === String(State._followingGenId)" in chat_source
    assert "const permissionIsVisible = _permissionBelongsToVisibleBranch(data)" in chat_source
    assert "genId: data.gen_id" in chat_source
    assert "draftMsgId: data.draft_msg_id" in chat_source
    assert "switchToBranch(targetId, targetId, { exact: true })" in chat_source
    assert "if (!_permissionBelongsToVisibleBranch(data)) continue" in chat_source
    assert "if (!_permissionBelongsToVisibleBranch(data)) return" in chat_source


def test_parallel_cancel_is_generation_scoped():
    chat_source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")

    assert "msg.gen_id = State._followingGenId" in chat_source
    assert "msg.draft_msg_id = draftMsgId" in chat_source
    assert "case 'cancelled':\n            if (!_isOurBranch(data)) break;" in chat_source
    assert "rememberConversationLeaf(State.currentConvId, data.parent_id || data.draft_msg_id)" in chat_source
    assert "case 'permission_request': {" in chat_source
