/**
 * Loom — Chat rendering, WebSocket, image upload, streaming, branch nav
 */

// ── WebSocket ──

function connectWebSocket(convId) {
    if (State.ws) {
        State.ws.close();
        State.ws = null;
    }

    const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${protocol}://${location.host}/ws/chat/${convId}`);

    ws.onopen = () => {
        console.log('WebSocket connected');
    };

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        handleWSMessage(data);
    };

    ws.onerror = (err) => {
        console.error('WebSocket error:', err);
        showToast('Connection error', 'error');
    };

    ws.onclose = () => {
        console.log('WebSocket closed');
        // Auto-reconnect after 2s if still on same conversation
        if (State.currentConvId === convId) {
            setTimeout(() => {
                if (State.currentConvId === convId) {
                    connectWebSocket(convId);
                }
            }, 2000);
        }
    };

    State.ws = ws;
}

function handleWSMessage(data) {
    switch (data.type) {
        case 'context_info':
            updateContextInfo(data);
            break;

        case 'stream_start':
            State.isStreaming = true;
            // Add placeholder assistant message
            appendStreamingMessage();
            document.getElementById('btn-send').disabled = true;
            break;

        case 'stream_chunk':
            appendStreamChunk(data.content);
            break;

        case 'stream_end':
            finalizeStreamingMessage(data.message);
            State.isStreaming = false;
            document.getElementById('btn-send').disabled = false;
            // Refresh tree
            refreshTree();
            break;

        case 'cancelled':
            removeStreamingMessage();
            State.isStreaming = false;
            document.getElementById('btn-send').disabled = false;
            showToast('Generation cancelled');
            break;

        case 'error':
            removeStreamingMessage();
            State.isStreaming = false;
            document.getElementById('btn-send').disabled = false;
            showToast(data.error || 'Generation error', 'error');
            break;
    }
}

function updateContextInfo(data) {
    const infoEl = document.getElementById('context-info');
    infoEl.classList.remove('hidden');
    document.getElementById('token-count').textContent = `${data.total_tokens} tok`;

    // Update the style selector to reflect what was actually used
    const sel = document.getElementById('style-badge-select');
    if (data.style_nudge && sel) {
        sel.value = data.style_nudge;
    }

    const repBadge = document.getElementById('rep-badge');
    if (data.repetition_alerts > 0) {
        repBadge.classList.remove('hidden');
        repBadge.title = `${data.repetition_alerts} repetition alert(s)`;
    } else {
        repBadge.classList.add('hidden');
    }
}

// ── Send Message ──

async function sendMessage() {
    if (State.isStreaming) return;
    if (!State.currentConvId) {
        showToast('Create or select a conversation first', 'error');
        return;
    }

    const input = document.getElementById('user-input');
    const content = input.value.trim();
    if (!content && !State.pendingImagePath) return;

    // Add user message via REST
    const msgData = {
        role: 'user',
        content: content,
        image_path: State.pendingImagePath || null,
    };

    try {
        const msg = await API.post(`/api/conversations/${State.currentConvId}/messages`, msgData);
        State.messages.push(msg);
        renderMessages();
        scrollToBottom();

        // Clear input
        input.value = '';
        autoResizeTextarea();
        State.pendingImagePath = null;
        State.pendingImageUrl = null;
        document.getElementById('image-preview').classList.add('hidden');

        // Request generation via WebSocket
        if (State.ws && State.ws.readyState === WebSocket.OPEN) {
            State.ws.send(JSON.stringify({
                action: 'generate',
                parent_id: msg.id,
            }));
        }
    } catch (err) {
        showToast('Failed to send message', 'error');
    }
}

// ── Regenerate ──

async function regenerateMessage(msgId) {
    if (State.isStreaming) return;

    try {
        const result = await API.post(`/api/conversations/${State.currentConvId}/regenerate/${msgId}`);

        if (State.ws && State.ws.readyState === WebSocket.OPEN) {
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
}

// ── Render Messages ──

function renderMessages() {
    const container = document.getElementById('messages');
    container.innerHTML = '';

    for (const msg of State.messages) {
        if (msg.role === 'system') continue;
        container.appendChild(createMessageElement(msg));
    }
}

function createMessageElement(msg) {
    const div = document.createElement('div');
    div.className = `message ${msg.role}`;
    div.dataset.msgId = msg.id;

    const roleLabel = msg.role === 'user' ? 'You' : getCharacterName();

    let actionsHtml = '';
    if (msg.role === 'assistant') {
        actionsHtml = `
            <button onclick="regenerateMessage(${msg.id})" title="Regenerate">↻</button>
            <button onclick="copyMessage(${msg.id})" title="Copy">⊘</button>
        `;
    } else {
        actionsHtml = `
            <button onclick="copyMessage(${msg.id})" title="Copy">⊘</button>
        `;
    }

    // Branch indicator (async - will fill in after render)
    const branchPlaceholder = `<span class="branch-slot" data-msg-id="${msg.id}"></span>`;

    div.innerHTML = `
        <div class="message-header">
            <span class="message-role">${escapeHtml(roleLabel)}</span>
            <div class="message-actions">
                ${branchPlaceholder}
                ${actionsHtml}
            </div>
        </div>
        <div class="message-content">${formatContent(msg.content)}</div>
        ${msg.image_path ? `<img class="message-image" src="/uploads/${msg.image_path.split(/[\\/]/).pop()}" alt="Attached image">` : ''}
    `;

    // Load sibling info for branch indicator
    loadBranchIndicator(msg.id, div.querySelector('.branch-slot'));

    return div;
}

async function loadBranchIndicator(msgId, slot) {
    try {
        const siblings = await API.get(`/api/conversations/${State.currentConvId}/messages/${msgId}/siblings`);
        if (siblings.length <= 1) return;

        const currentIndex = siblings.findIndex(s => s.id === msgId);
        const total = siblings.length;

        const indicator = document.createElement('span');
        indicator.className = 'branch-indicator';
        indicator.innerHTML = `
            <button ${currentIndex === 0 ? 'disabled' : ''} data-sibling-nav="prev" data-siblings='${JSON.stringify(siblings.map(s => s.id))}' data-current="${currentIndex}">‹</button>
            <span>${currentIndex + 1}/${total}</span>
            <button ${currentIndex === total - 1 ? 'disabled' : ''} data-sibling-nav="next" data-siblings='${JSON.stringify(siblings.map(s => s.id))}' data-current="${currentIndex}">›</button>
        `;

        indicator.querySelectorAll('button').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                const siblingIds = JSON.parse(btn.dataset.siblings);
                const current = parseInt(btn.dataset.current);
                const direction = btn.dataset.siblingNav;
                const newIndex = direction === 'prev' ? current - 1 : current + 1;
                if (newIndex < 0 || newIndex >= siblingIds.length) return;

                // Find the deepest active descendant of this sibling
                const targetId = siblingIds[newIndex];
                await switchToBranch(targetId);
            });
        });

        slot.replaceWith(indicator);
    } catch {
        // No siblings or error — leave blank
    }
}

async function switchToBranch(leafId) {
    try {
        // First try to find the deepest leaf along the active path from this node
        const branch = await API.post(`/api/conversations/${State.currentConvId}/switch-branch/${leafId}`);
        State.messages = branch;
        renderMessages();
        renderTree();
        scrollToBottom();
    } catch (err) {
        showToast('Failed to switch branch', 'error');
    }
}

function getCharacterName() {
    if (!State.currentConvId) return 'Assistant';
    const conv = State.conversations.find(c => c.id === State.currentConvId);
    if (!conv || !conv.character_id) return 'Assistant';
    const char = State.characters.find(c => c.id === conv.character_id);
    return char ? char.name : 'Assistant';
}

function formatContent(text) {
    if (!text) return '';
    // Basic markdown-like formatting
    let html = escapeHtml(text);
    // Bold: **text**
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    // Italic: *text* (but not inside bold)
    html = html.replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g, '<em>$1</em>');
    // Paragraphs
    html = html.replace(/\n\n/g, '</p><p>');
    html = '<p>' + html + '</p>';
    return html;
}

// ── Streaming Message ──

let streamingDiv = null;

function appendStreamingMessage() {
    const container = document.getElementById('messages');
    streamingDiv = document.createElement('div');
    streamingDiv.className = 'message assistant streaming';
    streamingDiv.innerHTML = `
        <div class="message-header">
            <span class="message-role">${escapeHtml(getCharacterName())}</span>
            <div class="message-actions">
                <button onclick="cancelGeneration()" title="Cancel">■</button>
            </div>
        </div>
        <div class="message-content"></div>
    `;
    container.appendChild(streamingDiv);
    scrollToBottom();
}

function appendStreamChunk(content) {
    if (!streamingDiv) return;
    const contentEl = streamingDiv.querySelector('.message-content');
    // Append raw text, format at the end
    const existing = contentEl.dataset.rawContent || '';
    const updated = existing + content;
    contentEl.dataset.rawContent = updated;
    contentEl.innerHTML = formatContent(updated) + '<span class="typing-cursor"></span>';
    scrollToBottom();
}

function finalizeStreamingMessage(msg) {
    if (!streamingDiv) return;

    // Replace the streaming div with a proper message element
    State.messages.push(msg);
    const newEl = createMessageElement(msg);
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
