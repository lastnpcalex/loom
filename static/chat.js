/**
 * Loom — Chat rendering, WebSocket, image upload, streaming, branch nav
 */

// ── Image Path Helper ──

function parseImagePaths(imagePath) {
    if (!imagePath) return [];
    if (typeof imagePath === 'object' && Array.isArray(imagePath)) return imagePath;
    // Try JSON array
    try {
        const parsed = JSON.parse(imagePath);
        if (Array.isArray(parsed)) return parsed;
    } catch {}
    // Single path string
    return [imagePath];
}

// ── WebSocket ──

let _wsReconnectDelay = 2000;

function connectWebSocket(convId) {
    if (State.ws) {
        State.ws.close();
        State.ws = null;
    }

    const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${protocol}://${location.host}/ws/chat/${convId}`);

    ws.onopen = () => {
        console.log('WebSocket connected');
        _wsReconnectDelay = 2000; // reset backoff on success
        // Web Worker keepalive — runs at full speed even in background tabs
        // (setInterval gets throttled to ~60s by Chrome in background)
        const workerBlob = new Blob([`setInterval(() => postMessage('ping'), 15000)`], {type: 'text/javascript'});
        ws._pingWorker = new Worker(URL.createObjectURL(workerBlob));
        ws._pingWorker.onmessage = () => {
            if (ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ action: 'ping' }));
            }
        };
        // Reload messages on reconnect to pick up any responses that completed while away.
        // Delay slightly so generation_active message can arrive first and set isStreaming.
        setTimeout(() => {
            if (State.currentConvId === convId && !State.isStreaming) {
                loadMessages(convId);
            }
        }, 200);
    };

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        handleWSMessage(data);
    };

    ws.onerror = (err) => {
        console.error('WebSocket error:', err);
    };

    ws.onclose = () => {
        console.log('WebSocket closed');
        if (ws._pingWorker) { ws._pingWorker.terminate(); ws._pingWorker = null; }
        // Reset streaming UI — server will keep generating and save the result.
        // On reconnect, loadMessages() will pick up the completed response.
        if (State.isStreaming) {
            State.isStreaming = false;
            document.getElementById('btn-send').disabled = false;
            removeStreamingMessage();
            showGenStatus('Reconnecting... generation continues on server');
        }
        // Reconnect immediately (don't rely on setTimeout which Chrome throttles)
        if (State.currentConvId === convId && State.currentView !== 'home') {
            connectWebSocket(convId);
        }
    };

    State.ws = ws;
}

// ── Generation Status ──
let _streamTokenCount = 0;
let _streamStartTime = 0;

// Force reconnect when tab becomes visible again
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && State.currentConvId) {
        if (!State.ws || State.ws.readyState !== WebSocket.OPEN) {
            console.log('Tab visible — reconnecting WebSocket');
            connectWebSocket(State.currentConvId);
        }
    }
});

async function loadMessages(convId) {
    try {
        const prevCount = State.messages.length;
        const treeData = await API.get(`/api/conversations/${convId}/tree`);
        State.treeData = treeData;  // keep branch indicators in sync
        hideRetryBar();
        const activeNodes = treeData.filter(n => n.is_active);
        if (activeNodes.length > 0) {
            const leafId = activeNodes[activeNodes.length - 1].id;
            State.messages = await API.get(`/api/conversations/${convId}/branch/${leafId}`);
        } else {
            State.messages = [];
        }
        renderMessages();
        scrollToBottom();
        if (State.messages.length > prevCount && prevCount > 0) {
            showToast('Response loaded');
        }
    } catch (err) {
        console.error('loadMessages failed:', err);
    }
}

function showGenStatus(text) {
    const el = document.getElementById('generation-status');
    document.getElementById('gen-status-text').textContent = text;
    el.classList.remove('hidden');
    scrollToBottom();
}

function hideGenStatus() {
    document.getElementById('generation-status').classList.add('hidden');
}

function showRetryBar(errorMsg) {
    hideRetryBar();
    const container = document.getElementById('messages');
    const bar = document.createElement('div');
    bar.id = 'retry-bar';
    bar.className = 'retry-bar';
    bar.innerHTML = `
        <span class="retry-error">${escapeHtml(errorMsg)}</span>
        <button class="btn-small retry-btn" id="btn-retry">Retry</button>
        <button class="retry-dismiss" title="Dismiss">✕</button>
    `;
    bar.querySelector('#btn-retry').addEventListener('click', () => {
        hideRetryBar();
        // Retry: send generate for the current active leaf
        if (State.ws && State.ws.readyState === WebSocket.OPEN) {
            showGenStatus('Retrying...');
            State.ws.send(JSON.stringify({ action: 'generate' }));
        }
    });
    bar.querySelector('.retry-dismiss').addEventListener('click', () => {
        hideRetryBar();
    });
    container.appendChild(bar);
    scrollToBottom();
}

function hideRetryBar() {
    const existing = document.getElementById('retry-bar');
    if (existing) existing.remove();
}

function _isOurBranch(data) {
    // If we're following a specific gen_id, only match that one
    if (State._followingGenId != null && data.gen_id != null) {
        return data.gen_id === State._followingGenId;
    }
    // If we're not streaming, reject any gen_id-tagged events (stale parallel siblings)
    if (!State.isStreaming && data.gen_id != null) {
        return false;
    }
    // If we already determined this via stream_start, use cached result
    if (State._streamIsOurBranch !== undefined) return State._streamIsOurBranch;
    // For pre-stream messages (status, context_info), check parent_id if available
    if (data.parent_id != null) {
        const myMsgIds = new Set(State.messages.map(m => m.id));
        return myMsgIds.has(data.parent_id);
    }
    // Unknown — assume ours (will be corrected on stream_start)
    return true;
}

function handleWSMessage(data) {
    switch (data.type) {
        case 'context_info':
            if (!_isOurBranch(data)) break;
            updateContextInfo(data);
            showGenStatus(`Context: ${data.total_tokens.toLocaleString()} tokens — Waiting for model...`);
            break;

        case 'status':
            if (!_isOurBranch(data)) break;
            showGenStatus(data.text || 'Looming...');
            break;

        case 'stream_start': {
            // Check if this generation is for our current branch
            const parentId = data.parent_id;
            const myMsgIds = new Set(State.messages.map(m => m.id));
            const isOnOurBranch = parentId == null || myMsgIds.has(parentId);
            // Only follow the FIRST stream on our branch — parallel siblings stream silently
            const shouldFollow = isOnOurBranch && !State.isStreaming;
            console.log('[WS] stream_start parent_id=', parentId, 'gen_id=', data.gen_id, 'follow=', shouldFollow);

            if (shouldFollow) {
                State._streamIsOurBranch = true;
                State._followingGenId = data.gen_id ?? null;
                State.isStreaming = true;
                State._parallelCount = (State._parallelCount || 0) + 1;
                if (typeof Notification !== 'undefined' && Notification.permission === 'default') {
                    Notification.requestPermission();
                }
                _streamTokenCount = 0;
                _streamStartTime = Date.now();
                _streamBuffer = '';
                _streamFlushTimer = null;
                hideRetryBar();
                appendStreamingMessage();
            } else if (isOnOurBranch) {
                // Parallel sibling — count it but don't render
                State._parallelCount = (State._parallelCount || 0) + 1;
            } else if (!State.isStreaming) {
                State._streamIsOurBranch = false;
            }
            // Refresh tree to show ghost/draft nodes for all parallel generations
            refreshTree();
            break;
        }

        case 'thinking_start':
            // Only show for CC/Local modes (Weave already has "Looming..." footer)
            if (State.currentConv && State.currentConv.mode !== 'weave') {
                showThinkingIndicator();
            }
            break;

        case 'thinking_end':
            hideThinkingIndicator();
            _streamStartTime = Date.now();
            _streamTokenCount = 0;
            break;

        case 'stream_chunk':
            if (!State._streamIsOurBranch) break;
            hideGenStatus();
            _streamTokenCount++;
            appendStreamChunk(data.content);
            break;

        case 'tool_start':
            if (!State._streamIsOurBranch) break;
            hideGenStatus();
            appendToolBlock(data.name, data.tool_id, data.ooda);
            break;

        case 'tool_input_chunk':
            if (!State._streamIsOurBranch) break;
            appendToolInput(data.content, data.tool_id);
            break;

        case 'tool_result':
            if (!State._streamIsOurBranch) break;
            finalizeToolBlock(data.content, data.tool_id, data.image_url, data.is_error);
            break;

        case 'thinking_chunk':
            if (!State._streamIsOurBranch) break;
            appendThinkingChunk(data.content);
            break;

        case 'ask_user_question':
            renderAskUserQuestion(data.questions, data.tool_id);
            break;

        case 'plan_ready':
            renderPlanReady(data.plan, data.plan_file, data.tool_id);
            break;

        case 'permission_request':
            showPermissionPrompt(data);
            break;

        case 'permission_resolved':
            resolvePermissionPrompt(data.request_id, data.allowed);
            break;

        case 'cc_debug_event':
            console.log('[CC debug]', data.event_type, data.data);
            break;

        case 'state_update':
            // OODA harness updated branch state — refresh with branch-aware data
            if (State.currentConvId) {
                const leaf = State.messages?.filter(m => m.role !== 'system').slice(-1)[0];
                const stateUrl = leaf
                    ? `/api/conversations/${State.currentConvId}/branch-state/${leaf.id}`
                    : `/api/conversations/${State.currentConvId}/state`;
                API.get(stateUrl).then(cards => {
                    State.stateCards = cards;
                    if (typeof renderStateCards === 'function') renderStateCards();
                });
            }
            break;

        case 'stream_end': {
            // Is this the stream we're following, or a parallel sibling?
            const isFollowed = State._followingGenId != null
                ? data.gen_id === State._followingGenId
                : State._streamIsOurBranch;
            // Decrement parallel counter
            if (State._parallelCount > 0) State._parallelCount--;
            const allDone = !State._parallelCount;

            if (!isFollowed) {
                // Parallel sibling finished — refresh tree, notify user
                refreshTree();
                if (data.message && data.message.content?.trim()) {
                    addNotification(data.message);
                }
                // If the completed message is on our current branch (e.g. user navigated
                // to a draft that just finished), reload messages to show the content
                if (data.message && data.message.id) {
                    const viewedIds = new Set(State.messages.map(m => m.id));
                    if (viewedIds.has(data.message.id)) {
                        loadMessages(State.currentConvId);
                    }
                }
                if (allDone) {
                    State._streamIsOurBranch = undefined;
                    State._followingGenId = null;
                }
                break;
            }
            // Our followed stream ended
            State.isStreaming = false;
            State._streamIsOurBranch = undefined;
            State._followingGenId = null;
            document.getElementById('btn-send').disabled = false;
            hideGenStatus();
            // Clear ghost node before tree refresh so it doesn't persist
            // Tree refreshes on stream_end/cancel/error to replace draft with final node
            // Browser notification if tab is hidden
            if (document.hidden && typeof Notification !== 'undefined' && Notification.permission === 'granted') {
                const convTitle = State.currentConv?.title || 'Conversation';
                new Notification('A Shadow Loom', {
                    body: `${convTitle} — response complete`,
                    icon: '/static/img/loom-ico-transparent.png',
                });
            }
            if (streamingDiv) {
                finalizeStreamingMessage(data.message, data.cost);
                // Images are detected client-side in createMessageElement
                // No need to also render data.images (causes duplicates)
                if (false && data.images && data.images.length > 0) {
                    const imgContainer = document.createElement('div');
                    imgContainer.className = 'detected-images';
                    for (const url of data.images) {
                        const filename = decodeURIComponent(url.split('path=').pop() || '').split(/[/\\]/).pop() || 'image';
                        const figure = document.createElement('figure');
                        figure.className = 'detected-image-figure';
                        const img = document.createElement('img');
                        img.src = url;
                        img.alt = filename;
                        img.className = 'generated-image';
                        img.addEventListener('click', () => {
                            const body = document.getElementById('preview-modal-body');
                            body.innerHTML = '<img src="' + url + '" style="max-width:100%;max-height:80vh;">';
                            document.getElementById('modal-preview').classList.remove('hidden');
                        });
                        const caption = document.createElement('figcaption');
                        caption.textContent = filename;
                        figure.appendChild(img);
                        figure.appendChild(caption);
                        imgContainer.appendChild(figure);
                    }
                    const msgDiv = document.querySelector(`.message[data-msg-id="${data.message.id}"]`);
                    if (msgDiv) msgDiv.appendChild(imgContainer);
                }
            } else {
                loadMessages(State.currentConvId);
            }
            refreshTree();
            _flushQueuedGeneration();
            break;
        }

        case 'cancelled':
            if (!_isOurBranch(data)) {
                refreshTree();
                // If viewing a draft that was cancelled (server deletes it), reload
                if (data.message_id) {
                    const viewedIds = new Set(State.messages.map(m => m.id));
                    if (viewedIds.has(data.message_id)) loadMessages(State.currentConvId);
                }
                break;
            }
            removeStreamingMessage();
            State.isStreaming = false;
            State._followingGenId = null;
            document.getElementById('btn-send').disabled = false;
            hideGenStatus();
            showRetryBar('Generation cancelled');
            _flushQueuedGeneration();
            break;

        case 'error':
            if (!_isOurBranch(data)) {
                refreshTree();
                if (data.message_id) {
                    const viewedIds = new Set(State.messages.map(m => m.id));
                    if (viewedIds.has(data.message_id)) loadMessages(State.currentConvId);
                }
                break;
            }
            removeStreamingMessage();
            State.isStreaming = false;
            State._followingGenId = null;
            document.getElementById('btn-send').disabled = false;
            hideGenStatus();
            if (data.error && data.error.includes('another branch')) {
                showToast(data.error, 'error');
            } else {
                showRetryBar(data.error || 'Generation error');
                _flushQueuedGeneration();
            }
            break;

        case 'generation_active':
            // Reconnected while a generation is still running
            hideRetryBar();
            removeStreamingMessage();
            State.isStreaming = true;
            // loadMessages + renderMessages will create streaming div from the draft
            loadMessages(State.currentConvId);
            break;

        case 'generation_idle':
            // Server confirms no generation running — reset any stuck streaming state
            if (State.isStreaming) {
                State.isStreaming = false;
                document.getElementById('btn-send').disabled = false;
                removeStreamingMessage();
                hideGenStatus();
                loadMessages(State.currentConvId);
            }
            break;
    }
}

function updateContextInfo(data) {
    // Context token info now shown via gen status bar, not header
}

// ── Send Message ──

let _queuedGeneration = null;  // queued message to generate after current stream ends

async function sendMessage() {
    if (!State.currentConvId) {
        showToast('Create or select a conversation first', 'error');
        return;
    }

    const input = document.getElementById('user-input');
    const content = input.value.trim();
    const isClaudeMode = State.currentConv && State.currentConv.mode === 'claude';
    const hasImages = State.pendingImages.length > 0;
    if (!content && !(hasImages && !isClaudeMode)) return;

    // Add user message via REST — send image paths as JSON array
    const imagePaths = hasImages ? State.pendingImages.map(img => img.path) : null;
    const isFromTree = State.currentView === 'tree';
    const msgData = {
        role: 'user',
        content: content,
        image_path: imagePaths,
    };
    // From tree view: create a new root branch
    if (isFromTree) {
        msgData.parent_id = null;
    }

    try {
        const msg = await API.post(`/api/conversations/${State.currentConvId}/messages`, msgData);

        // Clear input immediately so user can keep typing
        input.value = '';
        autoResizeTextarea();
        clearPendingImages();

        // If sent from tree view, switch to that branch in chat
        if (isFromTree) {
            await switchToBranch(msg.id, msg.id);
            State._skipLoadOnChat = true;
            switchView('chat');
            if (State.ws && State.ws.readyState === WebSocket.OPEN) {
                showGenStatus('Sending...');
                _triggerParallelGenerate(State.branchCount, msg.id);
            }
            return;
        }

        if (State.isStreaming) {
            // Queue it — will fire when current stream ends
            _queuedGeneration = msg;
            State.messages.push(msg);
            // Show queued message immediately in chat
            const container = document.getElementById('messages');
            const el = createMessageElement(msg);
            el.classList.add('queued-message');
            container.appendChild(el);
            scrollToBottom();
            showToast('Message queued — will send after current turn');
        } else {
            State.messages.push(msg);
            renderMessages();
            scrollToBottom();

            // Request generation via WebSocket
            if (State.ws && State.ws.readyState === WebSocket.OPEN) {
                const count = State.branchCount || 1;
                showGenStatus(count > 1 ? `Generating ${count} branches...` : 'Sending...');
                _triggerParallelGenerate(count, msg.id);
            }
        }
    } catch (err) {
        showToast('Failed to send message', 'error');
    }
}

function _flushQueuedGeneration() {
    if (!_queuedGeneration) return;
    const msg = _queuedGeneration;
    _queuedGeneration = null;
    // Message already in State.messages and rendered — just trigger generation
    // Remove queued styling
    const queuedEl = document.querySelector('.queued-message');
    if (queuedEl) queuedEl.classList.remove('queued-message');
    if (State.ws && State.ws.readyState === WebSocket.OPEN) {
        showGenStatus('Sending queued message...');
        State.ws.send(JSON.stringify({
            action: 'generate',
            parent_id: msg.id,
        }));
    }
}

// ── Regenerate ──

async function regenerateMessage(msgId) {
    if (State.isStreaming) return;

    try {
        const result = await API.post(`/api/conversations/${State.currentConvId}/regenerate/${msgId}`);

        // Remove the old message and everything after it from the view
        const idx = State.messages.findIndex(m => m.id === msgId);
        if (idx !== -1) {
            State.messages = State.messages.slice(0, idx);
            renderMessages();
        }

        if (State.ws && State.ws.readyState === WebSocket.OPEN) {
            showGenStatus('Regenerating...');
            State.ws.send(JSON.stringify({
                action: 'regenerate',
                parent_id: result.parent_id,
            }));
        }
    } catch (err) {
        showToast('Regeneration failed', 'error');
    }
}

// ── Cancel Generation ──

function cancelGeneration() {
    if (State.ws && State.ws.readyState === WebSocket.OPEN) {
        State.ws.send(JSON.stringify({ action: 'cancel' }));
    }
    // If not actively streaming (e.g. viewing a draft), reload after a short
    // delay to pick up the server-side cleanup (draft deletion)
    if (!State.isStreaming && State.currentConvId) {
        setTimeout(() => loadMessages(State.currentConvId), 500);
    }
}

// ── Notifications (background generation landings) ──

const _notifications = [];

function addNotification(message) {
    const preview = (message.content || '').replace(/[#*_`>\[\]]/g, '').trim();
    _notifications.push({
        id: message.id,
        convId: State.currentConvId,
        convTitle: State.currentConv?.title || 'Conversation',
        parentId: message.parent_id,
        preview: preview.slice(0, 120) + (preview.length > 120 ? '…' : ''),
        time: new Date(),
    });
    _renderNotifBell();
}

function _renderNotifBell() {
    const bell = document.getElementById('notif-bell');
    const badge = document.getElementById('notif-badge');
    if (!bell) return;
    bell.classList.remove('hidden');
    if (_notifications.length > 0) {
        badge.textContent = _notifications.length;
        badge.classList.remove('hidden');
        bell.classList.add('notif-active');
    } else {
        badge.classList.add('hidden');
        bell.classList.remove('notif-active');
        document.getElementById('notif-dropdown').classList.add('hidden');
    }
}

function _renderNotifDropdown() {
    const list = document.getElementById('notif-list');
    list.innerHTML = '';
    if (_notifications.length === 0) {
        list.innerHTML = '<div class="notif-empty">No new branches</div>';
        return;
    }
    for (const n of _notifications) {
        const item = document.createElement('div');
        item.className = 'notif-item';
        const timeStr = n.time.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        item.innerHTML = `<span class="notif-time">${timeStr}</span>`
            + `<span class="notif-preview">${escapeHtml(n.preview || '(empty)')}</span>`;
        item.addEventListener('click', async () => {
            document.getElementById('notif-dropdown').classList.add('hidden');
            // Remove this notification
            const idx = _notifications.indexOf(n);
            if (idx !== -1) _notifications.splice(idx, 1);
            _renderNotifBell();
            // Navigate to the branch — load conversation first if needed
            if (State.currentConvId !== n.convId) {
                State._skipLoadOnChat = true;
                await loadConversation(n.convId);
            }
            await switchToBranch(n.id);
            switchView('chat');
        });
        list.appendChild(item);
    }
    // Clear all button at bottom
    const clearBtn = document.createElement('div');
    clearBtn.className = 'notif-clear-all';
    clearBtn.textContent = 'Clear all';
    clearBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        _notifications.length = 0;
        _renderNotifBell();
    });
    list.appendChild(clearBtn);
}

// Wire up bell + clear — called once from initInlineCCControls or on DOMContentLoaded
function _initNotifications() {
    const bell = document.getElementById('notif-bell');
    const dropdown = document.getElementById('notif-dropdown');
    if (!bell) return;
    bell.addEventListener('click', (e) => {
        e.stopPropagation();
        dropdown.classList.toggle('hidden');
        if (!dropdown.classList.contains('hidden')) _renderNotifDropdown();
    });
    document.getElementById('notif-clear').addEventListener('click', (e) => {
        e.stopPropagation();
        _notifications.length = 0;
        _renderNotifBell();
    });
    // Close dropdown on outside click
    document.addEventListener('click', (e) => {
        if (!dropdown.contains(e.target) && e.target !== bell) {
            dropdown.classList.add('hidden');
        }
    });
}

// ── Render Messages ──

const VIRTUAL_SCROLL = {
    initialCount: 15,   // messages to render initially (from the end)
    batchSize: 10,      // messages to load per scroll-up
    renderedStart: 0,   // index into State.messages of the first rendered msg
    observer: null,     // IntersectionObserver for the sentinel
};

function renderMessages() {
    // Ensure branch names are computed from current tree data
    if (State.treeData && State.treeData.length > 0 && typeof computeBranchNames === 'function') {
        const nodeMap = {};
        const childrenMap = {};
        const roots = [];
        for (const n of State.treeData) { nodeMap[n.id] = n; childrenMap[n.id] = []; }
        for (const n of State.treeData) {
            if (n.parent_id && nodeMap[n.parent_id]) childrenMap[n.parent_id].push(n.id);
            else roots.push(n.id);
        }
        State.branchNames = computeBranchNames(roots, nodeMap, childrenMap);
    }

    const container = document.getElementById('messages');
    container.innerHTML = '';

    // Clean up previous observer
    if (VIRTUAL_SCROLL.observer) {
        VIRTUAL_SCROLL.observer.disconnect();
        VIRTUAL_SCROLL.observer = null;
    }

    if (State.messages.length === 0 && State.currentConvId) {
        container.innerHTML = '<div class="empty-loom-hint">' +
            '<p>No messages on this branch.</p>' +
            '<p>Type a message below to start a new thread.</p>' +
            '</div>';
        return;
    }

    // Filter out system messages for rendering
    const renderMsgs = State.messages.filter(m => m.role !== 'system');

    // Only render the last N messages initially
    const startIdx = Math.max(0, renderMsgs.length - VIRTUAL_SCROLL.initialCount);
    VIRTUAL_SCROLL.renderedStart = startIdx;

    // Add sentinel at top if there are older messages to load
    if (startIdx > 0) {
        const sentinel = document.createElement('div');
        sentinel.id = 'scroll-sentinel';
        sentinel.className = 'scroll-sentinel';
        sentinel.textContent = `↑ ${startIdx} older messages`;
        container.appendChild(sentinel);

        // Set up IntersectionObserver to load more on scroll
        const scrollParent = document.getElementById('messages-container');
        VIRTUAL_SCROLL.observer = new IntersectionObserver((entries) => {
            if (entries[0].isIntersecting) {
                loadOlderMessages(renderMsgs, container, scrollParent);
            }
        }, { root: scrollParent, threshold: 0.1 });
        VIRTUAL_SCROLL.observer.observe(sentinel);
    }

    // Render the visible messages
    for (let i = startIdx; i < renderMsgs.length; i++) {
        container.appendChild(createMessageElement(renderMsgs[i]));
    }

    // If streaming and last message is a draft, convert it to a streaming div
    const lastMsg = State.messages[State.messages.length - 1];
    if (lastMsg && lastMsg.role === 'assistant' && State.isStreaming) {
        const lastEl = container.querySelector(`.message[data-msg-id="${lastMsg.id}"]`);
        if (lastEl) lastEl.remove();
        appendStreamingMessage();
        // Render accumulated content_blocks from the draft
        if (lastMsg.content_blocks && streamingDiv) {
            try {
                const blocks = typeof lastMsg.content_blocks === 'string'
                    ? JSON.parse(lastMsg.content_blocks) : lastMsg.content_blocks;
                if (blocks && blocks.length > 0) {
                    const contentEl = streamingDiv.querySelector('.message-content');
                    contentEl.innerHTML = renderContentBlocks(blocks);
                }
            } catch {}
        }
        // Also show any text content accumulated so far
        if (lastMsg.content && lastMsg.content.trim() && streamingDiv) {
            const contentEl = streamingDiv.querySelector('.message-content');
            if (!contentEl.innerHTML.trim()) {
                contentEl.innerHTML = formatContent(lastMsg.content);
            }
        }
    } else {
        // Empty assistant messages are rendered as "Generating..." by createMessageElement.
        // The retry bar is only shown from WS error/cancelled events, not from render path,
        // because empty drafts in the DB are still-generating (server deletes failed drafts).
        if (lastMsg && lastMsg.role === 'user' && !State.isStreaming) {
            showGenerateBar();
        }
    }

    // Check if the last message has children on other branches
    if (lastMsg && !State.isStreaming) {
        showChildBranchHint(lastMsg.id, container);
    }
}

function loadOlderMessages(renderMsgs, container, scrollParent) {
    const currentStart = VIRTUAL_SCROLL.renderedStart;
    if (currentStart <= 0) return;

    // Calculate new range
    const newStart = Math.max(0, currentStart - VIRTUAL_SCROLL.batchSize);
    const batch = renderMsgs.slice(newStart, currentStart);

    // Remember scroll position
    const scrollHeight = scrollParent.scrollHeight;
    const scrollTop = scrollParent.scrollTop;

    // Find the sentinel and insert batch after it (in correct order)
    const sentinel = document.getElementById('scroll-sentinel');
    const fragment = document.createDocumentFragment();
    for (let i = 0; i < batch.length; i++) {
        fragment.appendChild(createMessageElement(batch[i]));
    }
    const refNode = sentinel ? sentinel.nextSibling : container.firstChild;
    container.insertBefore(fragment, refNode);

    VIRTUAL_SCROLL.renderedStart = newStart;

    // Update sentinel text or remove if no more messages
    if (newStart <= 0) {
        if (sentinel) sentinel.remove();
        if (VIRTUAL_SCROLL.observer) {
            VIRTUAL_SCROLL.observer.disconnect();
            VIRTUAL_SCROLL.observer = null;
        }
    } else if (sentinel) {
        sentinel.textContent = `↑ ${newStart} older messages`;
    }

    // Maintain scroll position
    const newScrollHeight = scrollParent.scrollHeight;
    scrollParent.scrollTop = scrollTop + (newScrollHeight - scrollHeight);
}

function showChildBranchHint(msgId, container) {
    // Compute children from State.treeData (no API call needed)
    if (!State.treeData) return;
    const children = State.treeData.filter(n => n.parent_id === msgId);
    if (!children || children.length === 0) return;

    const hint = document.createElement('div');
    hint.className = 'child-branch-hint';
    const count = children.length;
    hint.innerHTML = `<span>${count} response${count > 1 ? 's' : ''} on ${count > 1 ? 'branches' : 'a branch'} below</span>`;

    for (const child of children) {
        const btn = document.createElement('button');
        const preview = (child.preview || '').substring(0, 40) + ((child.preview || '').length > 40 ? '...' : '');
        btn.textContent = preview || child.role;
        btn.title = 'Switch to this branch';
        btn.addEventListener('click', async () => {
            await switchToBranch(child.id);
        });
        hint.appendChild(btn);
    }

    container.appendChild(hint);
}

function _triggerParallelGenerate(count, parentId) {
    if (!State.ws || State.ws.readyState !== WebSocket.OPEN) return;
    // Only Weave mode supports parallel branching
    const isWeave = State.currentConv && State.currentConv.mode === 'weave';
    const n = isWeave ? Math.max(1, Math.min(5, count)) : 1;
    for (let i = 0; i < n; i++) {
        const msg = { action: 'generate' };
        if (parentId) msg.parent_id = parentId;
        State.ws.send(JSON.stringify(msg));
    }
    showGenStatus(n > 1 ? `Generating ${n} branches...` : 'Sending...');
}

function showGenerateBar() {
    hideRetryBar();
    const container = document.getElementById('messages');
    const bar = document.createElement('div');
    bar.id = 'retry-bar';
    bar.className = 'retry-bar generate-bar';
    bar.innerHTML = `
        <span class="retry-error">No response yet</span>
        <button class="btn-small retry-btn" id="btn-generate">Generate</button>
    `;
    bar.querySelector('#btn-generate').addEventListener('click', () => {
        hideRetryBar();
        const count = State.branchCount || 1;
        _triggerParallelGenerate(count);
    });
    container.appendChild(bar);
    scrollToBottom();
}

function createMessageElement(msg, cost) {
    const isErrorMsg = msg.role === 'assistant' && msg.content?.startsWith('[Error:');
    const isDraft = msg.role === 'assistant' && !msg.content?.trim() && !isErrorMsg;
    const div = document.createElement('div');
    div.className = `message ${msg.role}${isErrorMsg ? ' message-error' : ''}${isDraft ? ' message-generating' : ''}`;
    div.dataset.msgId = msg.id;

    const isClaudeMode = State.currentConv && State.currentConv.mode === 'claude';
    const isLocalMode = State.currentConv && State.currentConv.mode === 'local';
    const roleLabel = msg.role === 'user' ? 'You'
        : isClaudeMode ? 'Claude'
        : isLocalMode ? (State.currentConv.local_model || 'Local')
        : getCharacterName();
    const branchLabel = State.branchNames?.[msg.id] || '';

    const isBm = State.bookmarks?.some(b => b.message_id === msg.id);
    const bmBtn = `<button onclick="toggleChatBookmark(${msg.id})" title="${isBm ? 'Remove bookmark' : 'Bookmark'}" class="chat-bookmark-btn${isBm ? ' active' : ''}">${isBm ? '⏣' : '⬡'}</button>`;
    let actionsHtml = '';
    if (msg.role === 'assistant') {
        actionsHtml = '<button onclick="regenerateMessage(' + msg.id + ')" title="Regenerate">&#x21BB;</button>' +
            '<button onclick="forkFromMessage(' + msg.id + ')" title="Fork">&#x2325;</button>' +
            '<button onclick="copyMessage(' + msg.id + ')" title="Copy">&#x29C9;</button>' + bmBtn;
    } else {
        actionsHtml = '<button onclick="editMessage(' + msg.id + ')" title="Edit">&#x270E;</button>' +
            '<button onclick="forkFromMessage(' + msg.id + ')" title="Fork">&#x2325;</button>' +
            '<button onclick="copyMessage(' + msg.id + ')" title="Copy">&#x29C9;</button>' + bmBtn;
    }

    // Branch indicator (async - will fill in after render)
    const branchPlaceholder = `<span class="branch-slot" data-msg-id="${msg.id}"></span>`;

    // Render content: use content_blocks if available (Claude mode), otherwise plain text
    let contentHtml = '';
    let blocks = null;
    if (msg.content_blocks) {
        try {
            blocks = typeof msg.content_blocks === 'string' ? JSON.parse(msg.content_blocks) : msg.content_blocks;
        } catch { blocks = null; }
    }

    if (isDraft) {
        contentHtml = '<span class="generating-placeholder"><span class="thinking-dots"></span> Generating...</span>'
            + ' <button onclick="cancelGeneration()" title="Cancel generation" class="cancel-draft-btn">&#x2298;</button>';
    } else if (blocks && blocks.length > 0) {
        contentHtml = renderContentBlocks(blocks);
    } else {
        contentHtml = formatContent(msg.content);
    }

    // Cost footer
    let costHtml = '';
    if (cost || msg.turn_cost_usd) {
        const c = cost || {};
        const usd = c.cost_usd || msg.turn_cost_usd || 0;
        const inTok = c.input_tokens || msg.turn_input_tokens || 0;
        const outTok = c.output_tokens || msg.turn_output_tokens || 0;
        const durMs = c.duration_ms || 0;
        const parts = [];
        if (inTok || outTok) parts.push(`${(inTok/1000).toFixed(1)}k in / ${(outTok/1000).toFixed(1)}k out`);
        if (usd) parts.push(`$${usd.toFixed(4)}`);
        if (durMs) parts.push(`${(durMs/1000).toFixed(1)}s`);
        if (parts.length) costHtml = `<div class="cost-footer">${parts.join(' · ')}</div>`;
    }

    let imgHtml = '';
    if (msg.image_path) {
        const paths = parseImagePaths(msg.image_path);
        if (paths.length > 0) {
            const imageExts = ['.png', '.jpg', '.jpeg', '.gif', '.webp'];
            imgHtml = '<div class="message-images">' +
                paths.map(p => {
                    const filename = p.split(/[\\/]/).pop();
                    const ext = '.' + filename.split('.').pop().toLowerCase();
                    if (imageExts.includes(ext)) {
                        return '<img class="message-image" src="/uploads/' + filename + '" alt="Attached image">';
                    }
                    return '<a class="message-file-attach" href="/uploads/' + filename + '" target="_blank" title="' + escapeHtml(filename) + '">&#128196; ' + escapeHtml(ext.toUpperCase().slice(1)) + ' file attached</a>';
                }).join('') +
                '</div>';
        }
    }

    // Detect project-relative image paths in assistant CC/local messages
    let projectImgHtml = '';
    if (msg.role === 'assistant' && (isClaudeMode || isLocalMode) && State.currentConv) {
        const allText = (msg.content || '') + ' ' + (typeof msg.content_blocks === 'string' ? msg.content_blocks : JSON.stringify(msg.content_blocks || ''));
        const imgRegex = /[\w/\\._-]+\.(?:png|jpg|jpeg|gif|webp)/gi;
        const matches = allText.match(imgRegex) || [];
        console.log('[IMG] Regex matches:', matches);
        // Dedup by filename — keep the shortest relative path
        // (absolute paths from content_blocks get blocked by path traversal)
        const byFilename = new Map();
        for (const m of matches) {
            const norm = m.replace(/\\/g, '/');
            // Skip absolute paths (start with / or X:/)
            if (norm.startsWith('/') || /^[A-Za-z]:/.test(norm)) {
                console.log('[IMG] Skipped absolute:', norm);
                continue;
            }
            const filename = norm.split('/').pop();
            const existing = byFilename.get(filename);
            if (!existing || norm.length > existing.length) {
                byFilename.set(filename, norm);
            }
        }
        const imgEntries = [];
        for (const [filename, norm] of byFilename) {
            imgEntries.push({
                url: `/api/conversations/${State.currentConvId}/file?path=${norm}`,
                name: filename,
            });
        }
        console.log('[IMG] Final entries:', imgEntries);
        if (imgEntries.length > 0) {
            projectImgHtml = '<div class="detected-images">' +
                imgEntries.map(e =>
                    `<figure class="detected-image-figure">` +
                    `<img class="generated-image" src="${e.url}" alt="${escapeHtml(e.name)}" loading="lazy" onerror="console.warn('[IMG] Failed to load:', this.src, '— removing figure'); this.closest('figure').remove()">` +
                    `<figcaption>${escapeHtml(e.name)}</figcaption></figure>`
                ).join('') + '</div>';
        }
    }

    div.innerHTML = '<div class="message-header">' +
        '<div class="message-header-left">' +
            '<span class="message-role">' + escapeHtml(roleLabel) + '</span>' +
            (branchLabel ? '<span class="message-branch-label" title="Click to copy branch path">' + escapeHtml(branchLabel) + '</span>' : '') +
        '</div>' +
        '<div class="message-actions">' + branchPlaceholder + actionsHtml + '</div>' +
        '</div>' +
        '<div class="message-content">' + contentHtml + '</div>' +
        imgHtml + projectImgHtml + costHtml;

    // Click-to-preview for detected project images
    div.querySelectorAll('.detected-images .generated-image').forEach(img => {
        img.addEventListener('click', () => {
            const body = document.getElementById('preview-modal-body');
            body.innerHTML = '<img src="' + img.src + '" style="max-width:100%;max-height:80vh;">';
            document.getElementById('modal-preview').classList.remove('hidden');
        });
    });

    // Load sibling info for branch indicator
    loadBranchIndicator(msg.id, div.querySelector('.branch-slot'));

    // Click-to-copy branch label
    const branchEl = div.querySelector('.message-branch-label');
    if (branchEl) {
        branchEl.addEventListener('click', () => {
            navigator.clipboard.writeText(branchEl.textContent).then(
                () => showToast('Branch path copied'),
                () => {}
            );
        });
    }

    return div;
}

function renderEditDiff(inputJson) {
    try {
        const data = typeof inputJson === 'string' ? JSON.parse(inputJson) : inputJson;
        const filePath = data.file_path || 'unknown file';
        const oldStr = data.old_string || '';
        const newStr = data.new_string || '';
        const oldLines = oldStr.split('\n');
        const newLines = newStr.split('\n');
        let diffHtml = '<div class="diff-header">' + escapeHtml(filePath) + '</div>';
        diffHtml += '<div class="diff-body">';
        for (const line of oldLines) {
            diffHtml += '<div class="diff-remove">- ' + escapeHtml(line) + '</div>';
        }
        for (const line of newLines) {
            diffHtml += '<div class="diff-add">+ ' + escapeHtml(line) + '</div>';
        }
        diffHtml += '</div>';
        return diffHtml;
    } catch {
        return null;
    }
}

function detectLangFromPath(filePath) {
    if (!filePath) return '';
    const ext = filePath.split('.').pop().toLowerCase();
    const map = {
        js:'javascript', ts:'typescript', jsx:'jsx', tsx:'tsx',
        py:'python', rb:'ruby', rs:'rust', go:'go', java:'java',
        c:'c', cpp:'cpp', h:'c', hpp:'cpp', cs:'csharp', php:'php',
        html:'html', htm:'html', css:'css', scss:'scss',
        json:'json', yaml:'yaml', yml:'yaml', toml:'toml', xml:'xml', svg:'svg',
        md:'markdown', sh:'bash', bash:'bash', sql:'sql',
    };
    return map[ext] || ext;
}

function renderToolBody(toolName, input, resultDisplay) {
    let inputHtml = '', resultHtml = '';
    try {
        const p = input ? JSON.parse(input) : {};
        switch (toolName) {
            case 'Write': {
                const fp = p.file_path || '';
                const content = p.content || '';
                const lang = detectLangFromPath(fp);
                inputHtml = '<div class="tool-file-path">' + escapeHtml(fp) + '</div>';
                if (content) inputHtml += '<div class="tool-code-content">' + formatContent('```' + lang + '\n' + content + '\n```') + '</div>';
                break;
            }
            case 'Read': {
                const fp = p.file_path || '';
                inputHtml = '<div class="tool-file-path">' + escapeHtml(fp) + '</div>';
                if (resultDisplay) {
                    const lang = detectLangFromPath(fp);
                    resultHtml = '<div class="tool-code-content">' + formatContent('```' + lang + '\n' + resultDisplay + '\n```') + '</div>';
                }
                break;
            }
            case 'Bash': {
                const cmd = p.command || '';
                if (cmd) inputHtml = '<div class="tool-code-content">' + formatContent('```bash\n' + cmd + '\n```') + '</div>';
                if (resultDisplay) resultHtml = '<div class="tool-block-result"><pre class="tool-output">' + escapeHtml(resultDisplay) + '</pre></div>';
                break;
            }
            default: {
                if (input) inputHtml = '<div class="tool-block-input">' + escapeHtml(input) + '</div>';
                if (resultDisplay) resultHtml = '<div class="tool-block-result">' + escapeHtml(resultDisplay) + '</div>';
            }
        }
    } catch {
        if (input) inputHtml = '<div class="tool-block-input">' + escapeHtml(input) + '</div>';
        if (resultDisplay) resultHtml = '<div class="tool-block-result">' + escapeHtml(resultDisplay) + '</div>';
    }
    return inputHtml + resultHtml;
}

function renderContentBlocks(blocks) {
    let html = '';
    for (const block of blocks) {
        if (block.type === 'text') {
            if (!block.text || !block.text.trim()) continue;
            html += formatContent(block.text);
        } else if (block.type === 'tool_use') {
            const name = block.name || 'Tool';
            const rawInput = (block.input || '').trim();
            const input = rawInput.length > 3000 ? rawInput.substring(0, 3000) + '\n... (truncated)' : rawInput;
            const result = (block.result || '').trim();
            const resultDisplay = result.length > 2000 ? result.substring(0, 2000) + '\n... (truncated)' : result;

            // Special rendering for Edit tool
            const isEdit = (name === 'Edit');
            const diffHtml = isEdit ? renderEditDiff(input) : null;
            const autoExpand = false; // Collapse all tool blocks in saved messages for performance
            const expanded = autoExpand ? ' expanded' : '';
            const toggleChar = autoExpand ? '&#9662;' : '&#9656;';

            let bodyHtml;
            if (diffHtml) {
                bodyHtml = diffHtml;
            } else {
                bodyHtml = renderToolBody(name, input, resultDisplay);
            }

            html += '<div class="tool-block' + expanded + '">' +
                '<div class="tool-block-header" data-tool-toggle>' +
                    '<span class="tool-name">' + escapeHtml(name) + '</span>' +
                    '<span class="tool-toggle">' + toggleChar + '</span>' +
                '</div>' +
                '<div class="tool-block-body">' + bodyHtml + '</div>' +
            '</div>';
        } else if (block.type === 'thinking') {
            const text = (block.text || '').trim();
            if (!text) continue;
            html += '<div class="cc-thinking">' +
                '<div class="cc-thinking-toggle">Thinking...</div>' +
                '<div class="cc-thinking-content" style="display:none;">' + escapeHtml(text) + '</div>' +
            '</div>';
        }
    }
    return html;
}

function loadBranchIndicator(msgId, slot) {
    // Compute siblings from State.treeData (no API call needed)
    if (!State.treeData) return;
    const msg = State.treeData.find(n => n.id === msgId);
    if (!msg) return;

    // Find siblings: messages with the same parent_id
    const parentId = msg.parent_id;
    const siblings = parentId === null
        ? State.treeData.filter(n => n.parent_id === null && n.role === msg.role)
        : State.treeData.filter(n => n.parent_id === parentId);
    if (siblings.length <= 1) return;

    siblings.sort((a, b) => (a.created_at || 0) - (b.created_at || 0));
    const currentIndex = siblings.findIndex(s => s.id === msgId);
    const total = siblings.length;
    const siblingIds = siblings.map(s => s.id);

    const indicator = document.createElement('span');
    indicator.className = 'branch-indicator';
    indicator.innerHTML = `
        <button ${currentIndex === 0 ? 'disabled' : ''}>‹</button>
        <span>${currentIndex + 1}/${total}</span>
        <button ${currentIndex === total - 1 ? 'disabled' : ''}>›</button>
    `;

    const buttons = indicator.querySelectorAll('button');
    buttons[0].addEventListener('click', async (e) => {
        e.stopPropagation();
        if (currentIndex > 0) await switchToBranch(siblingIds[currentIndex - 1]);
    });
    buttons[1].addEventListener('click', async (e) => {
        e.stopPropagation();
        if (currentIndex < total - 1) await switchToBranch(siblingIds[currentIndex + 1]);
    });

    slot.replaceWith(indicator);
}

async function switchToBranch(leafId, scrollToMsgId) {
    try {
        if (typeof showLoading === 'function') showLoading();
        // Walk to deepest leaf from the clicked node
        const [branch, treeData] = await Promise.all([
            API.post(`/api/conversations/${State.currentConvId}/switch-branch/${leafId}`),
            API.get(`/api/conversations/${State.currentConvId}/tree`),
        ]);
        State.messages = branch;
        State.treeData = treeData;
        hideRetryBar();
        hideGenStatus();
        renderMessages();
        renderTree();

        // Scroll to the clicked message, or bottom if not specified
        const targetId = scrollToMsgId || leafId;
        const renderMsgs = State.messages.filter(m => m.role !== 'system');
        const targetIdx = renderMsgs.findIndex(m => m.id === targetId);

        if (targetIdx >= 0 && targetIdx < VIRTUAL_SCROLL.renderedStart) {
            // Message is above the virtual scroll window — load from that point
            const container = document.getElementById('messages');
            const scrollParent = document.getElementById('messages-container');
            const sentinel = document.getElementById('scroll-sentinel');
            const refNode = sentinel ? sentinel.nextSibling : container.firstChild;
            const fragment = document.createDocumentFragment();
            for (let i = targetIdx; i < VIRTUAL_SCROLL.renderedStart; i++) {
                fragment.appendChild(createMessageElement(renderMsgs[i]));
            }
            container.insertBefore(fragment, refNode);
            VIRTUAL_SCROLL.renderedStart = targetIdx;
            if (sentinel) sentinel.textContent = `↑ ${targetIdx} older messages`;
            if (targetIdx <= 0 && sentinel) sentinel.remove();
        }

        // Now try to scroll to it (defer to allow DOM layout to settle)
        setTimeout(() => {
            const targetEl = document.querySelector(`.message[data-msg-id="${targetId}"]`);
            if (targetEl) {
                targetEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
                targetEl.classList.add('message-highlight');
                setTimeout(() => targetEl.classList.remove('message-highlight'), 2000);
            } else {
                scrollToBottom();
            }
        }, 150);
    } catch (err) {
        showToast('Failed to switch branch', 'error');
    } finally {
        if (typeof hideLoading === 'function') hideLoading();
    }
}

function getCharacterName() {
    if (!State.currentConvId) return 'Assistant';
    const conv = State.conversations.find(c => c.id === State.currentConvId);
    if (!conv) return 'Assistant';
    if (conv.mode === 'local') return conv.local_model || 'Local';
    if (!conv.character_id) return 'Assistant';
    const char = State.characters.find(c => c.id === conv.character_id);
    return char ? char.name : 'Assistant';
}

// Configure marked for chat rendering
if (typeof marked !== 'undefined') {
    const renderer = new marked.Renderer();
    // Force all links to open in new tab via DOMPurify hook (works with any marked version)
    if (typeof DOMPurify !== 'undefined') {
        DOMPurify.addHook('afterSanitizeAttributes', function(node) {
            if (node.tagName === 'A' && node.getAttribute('href')) {
                node.setAttribute('target', '_blank');
                node.setAttribute('rel', 'noopener noreferrer');
            }
        });
    }
    // Override code renderer to add toolbar with copy/preview buttons
    renderer.code = function({ text, lang, escaped }) {
        const safeLang = lang ? escapeHtml(lang.match(/^\S*/)?.[0] || '') : '';
        const langClass = safeLang ? ' class="language-' + safeLang + '"' : '';
        const isPreviewable = safeLang && ['html', 'svg', 'htm'].includes(safeLang.toLowerCase());
        const code = text.replace(/\n$/, '') + '\n';
        const codeHtml = escaped ? code : escapeHtml(code);

        let toolbar = '<div class="code-toolbar">';
        if (safeLang) toolbar += '<span class="code-lang-label">' + safeLang + '</span>';
        else toolbar += '<span class="code-lang-label"></span>';
        toolbar += '<div class="code-toolbar-actions">';
        if (isPreviewable) toolbar += '<button class="code-action-btn" data-code-action="preview" title="Preview">Preview</button>';
        toolbar += '<button class="code-action-btn" data-code-action="copy" title="Copy code">Copy</button>';
        toolbar += '</div></div>';

        return '<div class="code-block-wrapper">' + toolbar +
            '<pre><code' + langClass + '>' + codeHtml + '</code></pre></div>\n';
    };
    marked.setOptions({ breaks: true, gfm: true, renderer });
}

function formatContent(text) {
    if (!text) return '';
    if (typeof marked !== 'undefined') {
        const raw = marked.parse(text);
        return typeof DOMPurify !== 'undefined'
            ? DOMPurify.sanitize(raw, { ADD_TAGS: ['button'], ADD_ATTR: ['data-code-action'] })
            : raw;
    }
    // Fallback if marked not loaded
    let html = escapeHtml(text);
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g, '<em>$1</em>');
    const paras = html.split(/\n\n+/).filter(p => p.trim());
    if (paras.length === 0) return '';
    return paras.map(p => '<p>' + p.replace(/\n/g, '<br>') + '</p>').join('');
}

// ── Thinking Indicator ──

function showThinkingIndicator() {
    if (!streamingDiv) return;
    // Add thinking indicator without replacing content
    let indicator = streamingDiv.querySelector('.thinking-indicator');
    if (!indicator) {
        indicator = document.createElement('span');
        indicator.className = 'thinking-indicator';
        indicator.innerHTML = '<span class="thinking-dots"></span> Thinking...';
        streamingDiv.querySelector('.message-content').appendChild(indicator);
    }
    scrollToBottom();
}

function hideThinkingIndicator() {
    if (!streamingDiv) return;
    const indicator = streamingDiv.querySelector('.thinking-indicator');
    if (indicator) indicator.remove();
}

// ── Streaming Message ──

let streamingDiv = null;

function appendStreamingMessage() {
    const container = document.getElementById('messages');
    streamingDiv = document.createElement('div');
    streamingDiv.className = 'message assistant streaming';
    const isClaudeMode = State.currentConv && State.currentConv.mode === 'claude';
    const isLocalMode = State.currentConv && State.currentConv.mode === 'local';
    const label = isClaudeMode ? 'Claude'
        : isLocalMode ? (State.currentConv.local_model || 'Local')
        : getCharacterName();
    streamingDiv.innerHTML = '<div class="message-header">' +
        '<span class="message-role">' + escapeHtml(label) + '</span>' +
        '</div>' +
        '<div class="message-content"></div>' +
        '<div class="stream-thinking-footer"><span class="thinking-dots"></span> Looming...' +
        ' <button onclick="cancelGeneration()" title="Cancel generation" class="cancel-draft-btn">&#x2298;</button></div>';
    container.appendChild(streamingDiv);
    scrollToBottom();
}

let _streamBuffer = '';
let _streamFlushTimer = null;

function appendStreamChunk(content) {
    if (!streamingDiv) return;
    _streamBuffer += content;
    // Throttle DOM updates to max every 50ms
    if (!_streamFlushTimer) {
        _streamFlushTimer = setTimeout(_flushStreamBuffer, 50);
    }
}

function _flushStreamBuffer() {
    _streamFlushTimer = null;
    if (!streamingDiv || !_streamBuffer) return;
    const contentEl = streamingDiv.querySelector('.message-content');
    // Find or create a text span to stream into (keeps text separate from tool blocks)
    let textSpan = contentEl.querySelector('.streaming-text:last-of-type');
    // If last child is a tool/thinking block, start a new text span after it
    const lastChild = contentEl.lastElementChild;
    if (!textSpan || (lastChild && !lastChild.classList.contains('streaming-text'))) {
        textSpan = document.createElement('span');
        textSpan.className = 'streaming-text';
        contentEl.appendChild(textSpan);
    }
    const existing = textSpan.dataset.rawContent || '';
    const updated = existing + _streamBuffer;
    _streamBuffer = '';
    textSpan.dataset.rawContent = updated;
    textSpan.innerHTML = formatContent(updated) + '<span class="typing-cursor"></span>';
    scrollToBottom();
}

function finalizeStreamingMessage(msg, cost) {
    if (!streamingDiv) return;

    // Flush any remaining buffered text
    if (_streamBuffer) _flushStreamBuffer();

    // Replace the streaming div with a proper message element
    State.messages.push(msg);
    const newEl = createMessageElement(msg, cost);
    streamingDiv.replaceWith(newEl);
    streamingDiv = null;
    scrollToBottom();
}

function removeStreamingMessage() {
    if (streamingDiv) {
        streamingDiv.remove();
        streamingDiv = null;
    }
}

// ── Claude Code: Tool + Thinking Blocks ──

function appendToolBlock(name, toolId, isOoda) {
    if (!streamingDiv) return;
    // Add show-ooda class on first OODA block so they're visible during streaming
    if (isOoda && !streamingDiv.classList.contains('show-ooda')) {
        streamingDiv.classList.add('show-ooda');
    }
    const contentEl = streamingDiv.querySelector('.message-content');
    const block = document.createElement('div');
    block.className = 'tool-block' + (isOoda ? ' ooda-block' : '');
    block.dataset.toolId = toolId;
    block.innerHTML = '<div class="tool-block-header">' +
        '<span class="tool-name">' + escapeHtml(name) + '</span>' +
        '<span class="tool-toggle">&#9656;</span>' +
        '</div>' +
        '<div class="tool-block-body">' +
        '<div class="tool-block-input"></div>' +
        '<div class="tool-block-result"></div>' +
        '</div>';
    block.querySelector('.tool-block-header').addEventListener('click', () => {
        block.classList.toggle('expanded');
        block.querySelector('.tool-toggle').textContent = block.classList.contains('expanded') ? '▾' : '▸';
    });
    contentEl.appendChild(block);
    scrollToBottom();
}

function appendToolInput(json, toolId) {
    if (!streamingDiv) return;
    const block = streamingDiv.querySelector(`.tool-block[data-tool-id="${toolId}"]`)
                || streamingDiv.querySelector('.tool-block:last-child');
    if (!block) return;
    const inputEl = block.querySelector('.tool-block-input');
    // Cap displayed input to prevent DOM overload
    if (inputEl.textContent.length < 3000) {
        inputEl.textContent += json;
    }
}

function finalizeToolBlock(result, toolId, imageUrl, isError) {
    if (!streamingDiv) return;
    const block = streamingDiv.querySelector(`.tool-block[data-tool-id="${toolId}"]`)
                || streamingDiv.querySelector('.tool-block:last-child');
    if (!block) return;
    const resultEl = block.querySelector('.tool-block-result');
    // Truncate long results for display
    const display = result.length > 2000 ? result.substring(0, 2000) + '\n... (truncated)' : result;
    resultEl.textContent = display;

    // Show success/error indicator on the header
    const header = block.querySelector('.tool-block-header');
    if (header) {
        const indicator = document.createElement('span');
        indicator.className = isError ? 'tool-status tool-error' : 'tool-status tool-success';
        indicator.textContent = isError ? '✗' : '✓';
        indicator.title = isError ? 'Failed' : 'Success';
        header.appendChild(indicator);
    }
    if (isError) block.classList.add('tool-errored');

    // If this tool produced an image, display it inline
    if (imageUrl) {
        const filename = decodeURIComponent(imageUrl.split('path=').pop() || '').split(/[/\\]/).pop() || 'image';
        const imgContainer = document.createElement('div');
        imgContainer.className = 'tool-image-result';
        const figure = document.createElement('figure');
        figure.className = 'detected-image-figure';
        const img = document.createElement('img');
        img.src = imageUrl;
        img.alt = filename;
        img.className = 'generated-image';
        img.addEventListener('click', () => {
            const body = document.getElementById('preview-modal-body');
            body.innerHTML = '<img src="' + imageUrl + '" style="max-width:100%;max-height:80vh;">';
            document.getElementById('modal-preview').classList.remove('hidden');
        });
        const caption = document.createElement('figcaption');
        caption.textContent = filename;
        figure.appendChild(img);
        figure.appendChild(caption);
        imgContainer.appendChild(figure);
        block.querySelector('.tool-block-body').appendChild(imgContainer);
        block.classList.add('expanded');
        scrollToBottom();
        return;
    }
    // Auto-collapse after result arrives
    block.classList.remove('expanded');
    block.querySelector('.tool-toggle').textContent = '▸';
    scrollToBottom();
}

function appendThinkingChunk(text) {
    if (!streamingDiv) return;
    const contentEl = streamingDiv.querySelector('.message-content');
    let thinkingEl = contentEl.querySelector('.cc-thinking');
    if (!thinkingEl) {
        thinkingEl = document.createElement('div');
        thinkingEl.className = 'cc-thinking';
        thinkingEl.innerHTML = '<div class="cc-thinking-toggle">Thinking...</div>' +
            '<div class="cc-thinking-content"></div>';
        thinkingEl.querySelector('.cc-thinking-toggle').addEventListener('click', () => {
            const content = thinkingEl.querySelector('.cc-thinking-content');
            content.style.display = content.style.display === 'none' ? 'block' : 'none';
        });
        contentEl.appendChild(thinkingEl);
    }
    const thinkingContent = thinkingEl.querySelector('.cc-thinking-content');
    thinkingContent.textContent += text;
    scrollToBottom();
}

// ── Fork ──

async function forkFromMessage(msgId) {
    if (!State.currentConvId) return;
    try {
        const newConv = await API.post(`/api/conversations/${State.currentConvId}/fork/${msgId}`);
        State.conversations.unshift(newConv);
        showToast(`Forked → "${newConv.title}"`);
        await loadConversation(newConv.id);
        switchView('chat');
    } catch (err) {
        showToast('Fork failed', 'error');
    }
}

// ── Edit User Message ──

function editMessage(msgId) {
    const msg = State.messages.find(m => m.id === msgId);
    if (!msg || msg.role !== 'user') return;

    const msgEl = document.querySelector(`.message[data-msg-id="${msgId}"]`);
    if (!msgEl) return;
    const contentEl = msgEl.querySelector('.message-content');

    // Track files for this edit: start with originals, allow adding more
    const originalPaths = parseImagePaths(msg.image_path);
    const editFiles = originalPaths.map(p => ({ path: p, original: true }));

    const textarea = document.createElement('textarea');
    textarea.className = 'edit-message-input';
    textarea.value = msg.content;
    textarea.rows = Math.max(3, msg.content.split('\n').length);
    contentEl.replaceWith(textarea);
    textarea.focus();

    // File preview area (above buttons)
    const filePreview = document.createElement('div');
    filePreview.className = 'edit-file-preview';
    textarea.after(filePreview);

    function renderEditFiles() {
        if (editFiles.length === 0) {
            filePreview.classList.add('hidden');
            filePreview.innerHTML = '';
            return;
        }
        filePreview.classList.remove('hidden');
        filePreview.innerHTML = editFiles.map((f, i) => {
            const name = f.path.split('/').pop().split('\\').pop();
            return `<span class="edit-file-chip${f.original ? '' : ' new-file'}" title="${f.original ? 'From original' : 'New attachment'}">${name}<button class="edit-file-remove" data-idx="${i}">&times;</button></span>`;
        }).join('');
        filePreview.querySelectorAll('.edit-file-remove').forEach(btn => {
            btn.addEventListener('click', () => {
                editFiles.splice(parseInt(btn.dataset.idx), 1);
                renderEditFiles();
            });
        });
    }
    renderEditFiles();

    const isWeave = State.currentConv && State.currentConv.mode === 'weave';
    const btnRow = document.createElement('div');
    btnRow.className = 'edit-message-actions';
    const pillHtml = isWeave ? `<div class="branch-count-pill pill-compact" title="Branches to generate (click to cycle)"><span class="branch-count-icon">⑂</span><span class="branch-count-value">${State.branchCount}</span></div>` : '';
    btnRow.innerHTML = `
        <label class="btn-small edit-attach" title="Attach file">
            <input type="file" class="edit-file-input" accept="image/*,.md,.txt,.pdf,.json,.csv,.py,.js,.ts,.html,.css" hidden>
            📎
        </label>
        <button class="btn-small edit-save">Send as new branch</button>
        ${pillHtml}
        <button class="btn-small edit-cancel">Cancel</button>
    `;
    if (isWeave) _setupBranchPillClick(btnRow.querySelector('.branch-count-pill'));
    filePreview.after(btnRow);

    // File attach handler
    const editFileInput = btnRow.querySelector('.edit-file-input');
    editFileInput.addEventListener('change', async (e) => {
        const file = e.target.files[0];
        if (!file) return;
        try {
            const result = await API.upload(file);
            editFiles.push({ path: result.path, original: false });
            renderEditFiles();
        } catch { showToast('File upload failed', 'error'); }
        e.target.value = '';
    });

    btnRow.querySelector('.edit-cancel').addEventListener('click', () => {
        const newContent = document.createElement('div');
        newContent.className = 'message-content';
        newContent.innerHTML = formatContent(msg.content);
        textarea.replaceWith(newContent);
        filePreview.remove();
        btnRow.remove();
    });

    btnRow.querySelector('.edit-save').addEventListener('click', async () => {
        const newText = textarea.value.trim();
        if (!newText) return;

        try {
            const parentId = msg.parent_id || null;
            const allPaths = editFiles.map(f => f.path);

            const newMsg = await API.post(`/api/conversations/${State.currentConvId}/messages`, {
                role: 'user',
                content: newText,
                parent_id: parentId,
                image_path: allPaths.length > 0 ? allPaths : null,
            });

            await loadMessages(State.currentConvId);

            if (State.ws && State.ws.readyState === WebSocket.OPEN) {
                const count = State.branchCount || 1;
                showGenStatus(count > 1 ? `Generating ${count} branches...` : 'Generating...');
                _triggerParallelGenerate(count, newMsg.id);
            }
        } catch (err) {
            showToast('Failed to save edit', 'error');
        }
    });
}

// ── Copy ──

function copyMessage(msgId) {
    const msg = State.messages.find(m => m.id === msgId);
    if (msg) {
        navigator.clipboard.writeText(msg.content).then(
            () => showToast('Copied'),
            () => showToast('Copy failed', 'error')
        );
    }
}

// ── Chat Bookmark Toggle ──

async function toggleChatBookmark(msgId) {
    if (!State.currentConvId) return;
    const existing = State.bookmarks.find(b => b.message_id === msgId);
    if (existing) {
        await API.del(`/api/bookmarks/${existing.id}`);
        State.bookmarks = State.bookmarks.filter(b => b.id !== existing.id);
        showToast('Bookmark removed');
    } else {
        const branchName = State.branchNames?.[msgId] || '';
        const bm = await API.post(`/api/conversations/${State.currentConvId}/bookmarks`, {
            message_id: msgId,
            branch_name: branchName,
            description: '',
        });
        bm.conversation_title = State.currentConv?.title;
        bm.conversation_mode = State.currentConv?.mode;
        State.bookmarks.push(bm);
        showToast('Bookmarked');
    }
    renderMessages();
    if (typeof refreshOpenBookmarksPanels === 'function') refreshOpenBookmarksPanels();
}

// ── Refresh Tree ──

async function refreshTree() {
    if (!State.currentConvId) return;
    try {
        State.treeData = await API.get(`/api/conversations/${State.currentConvId}/tree`);
        renderTree();
    } catch {
        // Silently fail
    }
}

// ── Permission Prompts ──

function showPermissionPrompt(data) {
    if (!streamingDiv) appendStreamingMessage();
    const contentEl = streamingDiv.querySelector('.message-content');

    const prompt = document.createElement('div');
    prompt.className = 'permission-prompt';
    prompt.dataset.requestId = data.request_id;

    const toolName = escapeHtml(data.tool_name || 'Unknown');
    const inputSummary = escapeHtml(data.input_summary || JSON.stringify(data.tool_input || {}).substring(0, 300));

    prompt.innerHTML = '<div class="permission-header">' +
        '<span class="permission-icon">&#x1f512;</span>' +
        '<span class="permission-title">Permission Request</span>' +
        '</div>' +
        '<div class="permission-body">' +
        '<div class="permission-tool">Tool: <strong>' + toolName + '</strong></div>' +
        (inputSummary ? '<div class="permission-input"><pre>' + inputSummary + '</pre></div>' : '') +
        '</div>' +
        '<div class="permission-actions">' +
        '<button class="btn-permission allow" data-perm-action="allow" data-request-id="' + data.request_id + '">Allow</button>' +
        '<button class="btn-permission deny" data-perm-action="deny" data-request-id="' + data.request_id + '">Deny</button>' +
        '<button class="btn-permission allow-all" data-perm-action="allow-all" data-request-id="' + data.request_id + '">Allow All</button>' +
        '</div>';

    // Attach button handlers
    prompt.querySelectorAll('.btn-permission').forEach(btn => {
        btn.addEventListener('click', () => {
            const action = btn.dataset.permAction;
            const requestId = btn.dataset.requestId;
            const allow = action === 'allow' || action === 'allow-all';
            const always = action === 'allow-all';

            if (State.ws && State.ws.readyState === WebSocket.OPEN) {
                State.ws.send(JSON.stringify({
                    action: 'permission_response',
                    request_id: requestId,
                    allow: allow,
                    always: always,
                }));
            }

            // Disable buttons while waiting
            prompt.querySelectorAll('.btn-permission').forEach(b => b.disabled = true);
            prompt.querySelector('.permission-title').textContent =
                allow ? 'Allowed' + (always ? ' (all future)' : '') : 'Denied';
            prompt.classList.add(allow ? 'resolved-allow' : 'resolved-deny');
        });
    });

    contentEl.appendChild(prompt);
    scrollToBottom();
}

function resolvePermissionPrompt(requestId, allowed) {
    // Update the prompt if it hasn't been updated yet (e.g., from timeout)
    const prompt = document.querySelector(`.permission-prompt[data-request-id="${requestId}"]`);
    if (prompt && !prompt.classList.contains('resolved-allow') && !prompt.classList.contains('resolved-deny')) {
        prompt.querySelectorAll('.btn-permission').forEach(b => b.disabled = true);
        prompt.querySelector('.permission-title').textContent = allowed ? 'Allowed' : 'Denied';
        prompt.classList.add(allowed ? 'resolved-allow' : 'resolved-deny');
    }
}

// ── AskUserQuestion / ExitPlanMode Rendering ──

function renderAskUserQuestion(questions, toolId) {
    if (!streamingDiv) return;
    const contentEl = streamingDiv.querySelector('.message-content');

    for (const q of questions) {
        const block = document.createElement('div');
        block.className = 'ask-question-block';
        block.innerHTML = '<div class="ask-question-header">' + escapeHtml(q.header || 'Question') + '</div>' +
            '<div class="ask-question-text">' + escapeHtml(q.question) + '</div>' +
            '<div class="ask-question-options"></div>';

        const optionsEl = block.querySelector('.ask-question-options');
        for (const opt of (q.options || [])) {
            const btn = document.createElement('button');
            btn.className = 'ask-question-option';
            btn.innerHTML = '<strong>' + escapeHtml(opt.label) + '</strong>' +
                (opt.description ? '<span>' + escapeHtml(opt.description) + '</span>' : '');
            btn.addEventListener('click', () => {
                const input = document.getElementById('user-input');
                input.value = opt.label;
                input.focus();
                // Mark selected
                optionsEl.querySelectorAll('.ask-question-option').forEach(b => b.classList.remove('selected'));
                btn.classList.add('selected');
            });
            optionsEl.appendChild(btn);
        }
        contentEl.appendChild(block);
    }
    scrollToBottom();
}

function renderPlanReady(plan, planFile, toolId) {
    if (!streamingDiv) return;
    const contentEl = streamingDiv.querySelector('.message-content');

    const block = document.createElement('div');
    block.className = 'plan-block';
    block.innerHTML = '<div class="plan-header">Plan Ready</div>' +
        (planFile ? '<div class="plan-file">' + escapeHtml(planFile) + '</div>' : '') +
        '<div class="plan-actions">' +
        '<button class="plan-action-btn approve">Approve</button>' +
        '<button class="plan-action-btn revise">Revise</button>' +
        '</div>';

    block.querySelector('.approve').addEventListener('click', () => {
        document.getElementById('user-input').value = 'Approved, proceed with the plan.';
        document.getElementById('user-input').focus();
    });
    block.querySelector('.revise').addEventListener('click', () => {
        const input = document.getElementById('user-input');
        input.value = "I'd like to revise the plan: ";
        input.focus();
    });

    contentEl.appendChild(block);
    scrollToBottom();
}

// ── Code Block Preview ──

function toggleCodePreview(wrapper, btn) {
    const existing = wrapper.querySelector('.code-preview-panel');
    if (existing) { existing.remove(); btn.textContent = 'Preview'; return; }
    btn.textContent = 'Close';
    const codeEl = wrapper.querySelector('pre code');
    if (!codeEl) return;
    const rawCode = codeEl.textContent;
    const isSvg = (codeEl.className || '').includes('language-svg');

    const panel = document.createElement('div');
    panel.className = 'code-preview-panel';
    const toolbar = document.createElement('div');
    toolbar.className = 'code-preview-toolbar';
    toolbar.innerHTML = '<span class="code-preview-label">Preview</span>' +
        '<button class="code-action-btn" data-code-action="popout" title="Open larger">Popout</button>';
    panel.appendChild(toolbar);

    const iframe = document.createElement('iframe');
    iframe.className = 'code-preview-iframe';
    iframe.sandbox = 'allow-scripts';
    panel.appendChild(iframe);
    wrapper.appendChild(panel);

    const doc = iframe.contentDocument || (iframe.contentWindow && iframe.contentWindow.document);
    if (!doc) return;
    doc.open();
    if (isSvg) {
        doc.write('<!DOCTYPE html><html><head><style>body{margin:0;background:#1a1a2e;display:flex;align-items:center;justify-content:center;min-height:100vh;}</style></head><body>' + rawCode + '</body></html>');
    } else {
        doc.write(rawCode);
    }
    doc.close();
    scrollToBottom();
}

function openPreviewModal(wrapper) {
    const codeEl = wrapper.querySelector('pre code');
    if (!codeEl) return;
    const rawCode = codeEl.textContent;
    const isSvg = (codeEl.className || '').includes('language-svg');

    const modal = document.getElementById('modal-preview');
    const body = document.getElementById('preview-modal-body');
    modal.classList.remove('hidden');
    body.innerHTML = '';

    const iframe = document.createElement('iframe');
    iframe.className = 'preview-modal-iframe';
    iframe.sandbox = 'allow-scripts';
    body.appendChild(iframe);

    const doc = iframe.contentDocument || (iframe.contentWindow && iframe.contentWindow.document);
    if (!doc) { modal.classList.add('hidden'); return; }
    doc.open();
    if (isSvg) {
        doc.write('<!DOCTYPE html><html><head><style>body{margin:0;background:#1a1a2e;display:flex;align-items:center;justify-content:center;min-height:100vh;}</style></head><body>' + rawCode + '</body></html>');
    } else {
        doc.write(rawCode);
    }
    doc.close();
}

// ── Event Delegation for Tool Blocks + Thinking + Code Actions ──
document.addEventListener('click', (e) => {
    // Code block: Copy
    const copyBtn = e.target.closest('[data-code-action="copy"]');
    if (copyBtn) {
        e.stopPropagation();
        const wrapper = copyBtn.closest('.code-block-wrapper');
        const codeEl = wrapper && wrapper.querySelector('pre code');
        if (codeEl) {
            navigator.clipboard.writeText(codeEl.textContent).then(
                () => { copyBtn.textContent = 'Copied!'; setTimeout(() => { copyBtn.textContent = 'Copy'; }, 1500); },
                () => { copyBtn.textContent = 'Failed'; }
            );
        }
        return;
    }
    // Code block: Preview toggle
    const previewBtn = e.target.closest('[data-code-action="preview"]');
    if (previewBtn) {
        e.stopPropagation();
        const wrapper = previewBtn.closest('.code-block-wrapper');
        if (wrapper) toggleCodePreview(wrapper, previewBtn);
        return;
    }
    // Code block: Popout to modal
    const popoutBtn = e.target.closest('[data-code-action="popout"]');
    if (popoutBtn) {
        e.stopPropagation();
        const wrapper = popoutBtn.closest('.code-block-wrapper');
        if (wrapper) openPreviewModal(wrapper);
        return;
    }
    // Close preview modal
    if (e.target.closest('[data-close-modal-preview]')) {
        document.getElementById('modal-preview').classList.add('hidden');
        document.getElementById('preview-modal-body').innerHTML = '';
        return;
    }
    // Tool block expand/collapse
    const header = e.target.closest('[data-tool-toggle]');
    if (header) {
        e.stopPropagation();
        const block = header.closest('.tool-block');
        if (block) {
            block.classList.toggle('expanded');
            const toggle = header.querySelector('.tool-toggle');
            if (toggle) toggle.textContent = block.classList.contains('expanded') ? '▾' : '▸';
        }
        return;
    }
    // Thinking block expand/collapse
    const thinkToggle = e.target.closest('.cc-thinking-toggle');
    if (thinkToggle) {
        const content = thinkToggle.nextElementSibling;
        if (content) content.style.display = content.style.display === 'none' ? 'block' : 'none';
    }
});
