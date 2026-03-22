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
        // Reset streaming UI — server will keep generating and save the result.
        // On reconnect, loadMessages() will pick up the completed response.
        if (State.isStreaming) {
            State.isStreaming = false;
            document.getElementById('btn-send').disabled = false;
            removeStreamingMessage();
            showGenStatus('Reconnecting... generation continues on server');
        }
        // Only reconnect if still on the same conversation and in chat/tree view
        if (State.currentConvId === convId && State.currentView !== 'home') {
            setTimeout(() => {
                if (State.currentConvId === convId && State.currentView !== 'home') {
                    connectWebSocket(convId);
                }
            }, _wsReconnectDelay);
            // Exponential backoff, cap at 30s
            _wsReconnectDelay = Math.min(_wsReconnectDelay * 1.5, 30000);
        }
    };

    State.ws = ws;
}

// ── Generation Status ──
let _streamTokenCount = 0;
let _streamStartTime = 0;

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

function handleWSMessage(data) {
    switch (data.type) {
        case 'context_info':
            updateContextInfo(data);
            showGenStatus(`Context: ${data.total_tokens.toLocaleString()} tokens — Waiting for model...`);
            break;

        case 'stream_start':
            State.isStreaming = true;
            _streamTokenCount = 0;
            _streamStartTime = Date.now();
            _streamBuffer = '';
            _streamFlushTimer = null;
            hideGenStatus();
            hideRetryBar();
            appendStreamingMessage();
            document.getElementById('btn-send').disabled = true;
            break;

        case 'thinking_start':
            showThinkingIndicator();
            break;

        case 'thinking_end':
            hideThinkingIndicator();
            _streamStartTime = Date.now();
            _streamTokenCount = 0;
            break;

        case 'stream_chunk':
            _streamTokenCount++;
            appendStreamChunk(data.content);
            // Update token rate in status
            const elapsed = (Date.now() - _streamStartTime) / 1000;
            if (elapsed > 0.5) {
                const rate = (_streamTokenCount / elapsed).toFixed(1);
                document.getElementById('token-count').textContent =
                    `${_streamTokenCount} tok · ${rate} t/s`;
            }
            break;

        case 'tool_start':
            appendToolBlock(data.name, data.tool_id);
            break;

        case 'tool_input_chunk':
            appendToolInput(data.content, data.tool_id);
            break;

        case 'tool_result':
            finalizeToolBlock(data.content, data.tool_id, data.image_url);
            break;

        case 'thinking_chunk':
            appendThinkingChunk(data.content);
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

        case 'stream_end':
            State.isStreaming = false;
            document.getElementById('btn-send').disabled = false;
            hideGenStatus();
            if (streamingDiv) {
                finalizeStreamingMessage(data.message, data.cost);
            } else {
                // Streaming div was lost (disconnect/reconnect race) — reload from DB
                loadMessages(State.currentConvId);
            }
            refreshTree();
            break;

        case 'cancelled':
            removeStreamingMessage();
            State.isStreaming = false;
            document.getElementById('btn-send').disabled = false;
            hideGenStatus();
            showRetryBar('Generation cancelled');
            break;

        case 'error':
            removeStreamingMessage();
            State.isStreaming = false;
            document.getElementById('btn-send').disabled = false;
            hideGenStatus();
            showRetryBar(data.error || 'Generation error');
            break;

        case 'generation_active':
            // Reconnected while a generation is still running
            removeStreamingMessage();
            showGenStatus('Generation in progress...');
            State.isStreaming = true;
            document.getElementById('btn-send').disabled = true;
            appendStreamingMessage();
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
    const infoEl = document.getElementById('context-info');
    infoEl.classList.remove('hidden');
    document.getElementById('token-count').textContent = `${data.total_tokens.toLocaleString()} tok`;
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
    const isClaudeMode = State.currentConv && State.currentConv.mode === 'claude';
    const hasImages = State.pendingImages.length > 0;
    if (!content && !(hasImages && !isClaudeMode)) return;

    // Add user message via REST — send image paths as JSON array
    const imagePaths = hasImages ? State.pendingImages.map(img => img.path) : null;
    const msgData = {
        role: 'user',
        content: content,
        image_path: imagePaths,
    };

    try {
        const msg = await API.post(`/api/conversations/${State.currentConvId}/messages`, msgData);
        State.messages.push(msg);
        renderMessages();
        scrollToBottom();

        // Clear input
        input.value = '';
        autoResizeTextarea();
        clearPendingImages();

        // Request generation via WebSocket
        if (State.ws && State.ws.readyState === WebSocket.OPEN) {
            showGenStatus('Sending...');
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
}

// ── Render Messages ──

function renderMessages() {
    const container = document.getElementById('messages');
    container.innerHTML = '';

    for (const msg of State.messages) {
        if (msg.role === 'system') continue;
        container.appendChild(createMessageElement(msg));
    }

    // Show generate/retry bar if needed
    const lastMsg = State.messages[State.messages.length - 1];
    if (lastMsg && lastMsg.role === 'assistant' && !lastMsg.content?.trim()) {
        showRetryBar('Empty response — try regenerating');
    } else if (lastMsg && lastMsg.role === 'user') {
        showGenerateBar();
    }
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
        if (State.ws && State.ws.readyState === WebSocket.OPEN) {
            showGenStatus('Generating...');
            State.ws.send(JSON.stringify({ action: 'generate' }));
        }
    });
    container.appendChild(bar);
    scrollToBottom();
}

function createMessageElement(msg, cost) {
    const div = document.createElement('div');
    div.className = `message ${msg.role}`;
    div.dataset.msgId = msg.id;

    const isClaudeMode = State.currentConv && State.currentConv.mode === 'claude';
    const isLocalMode = State.currentConv && State.currentConv.mode === 'local';
    const roleLabel = msg.role === 'user' ? 'You'
        : isClaudeMode ? 'Claude'
        : isLocalMode ? (State.currentConv.local_model || 'Local')
        : getCharacterName();

    let actionsHtml = '';
    if (msg.role === 'assistant') {
        actionsHtml = '<button onclick="regenerateMessage(' + msg.id + ')" title="Regenerate">&#x21BB;</button>' +
            '<button onclick="forkFromMessage(' + msg.id + ')" title="Fork new conversation from here">&#x2325;</button>' +
            '<button onclick="copyMessage(' + msg.id + ')" title="Copy">&#x2298;</button>';
    } else {
        actionsHtml = '<button onclick="editMessage(' + msg.id + ')" title="Edit">&#x270E;</button>' +
            '<button onclick="forkFromMessage(' + msg.id + ')" title="Fork new conversation from here">&#x2325;</button>' +
            '<button onclick="copyMessage(' + msg.id + ')" title="Copy">&#x2298;</button>';
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

    if (blocks && blocks.length > 0) {
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
            imgHtml = '<div class="message-images">' +
                paths.map(p => '<img class="message-image" src="/uploads/' + p.split(/[\\/]/).pop() + '" alt="Attached image">').join('') +
                '</div>';
        }
    }
    div.innerHTML = '<div class="message-header">' +
        '<span class="message-role">' + escapeHtml(roleLabel) + '</span>' +
        '<div class="message-actions">' + branchPlaceholder + actionsHtml + '</div>' +
        '</div>' +
        '<div class="message-content">' + contentHtml + '</div>' +
        imgHtml + costHtml;

    // Load sibling info for branch indicator
    loadBranchIndicator(msg.id, div.querySelector('.branch-slot'));

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
            const input = (block.input || '').trim();
            const result = (block.result || '').trim();
            const resultDisplay = result.length > 2000 ? result.substring(0, 2000) + '\n... (truncated)' : result;

            // Special rendering for Edit tool
            const isEdit = (name === 'Edit');
            const diffHtml = isEdit ? renderEditDiff(input) : null;
            const autoExpand = isEdit || ['Write', 'Bash'].includes(name);
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
    if (!conv) return 'Assistant';
    if (conv.mode === 'local') return conv.local_model || 'Local';
    if (!conv.character_id) return 'Assistant';
    const char = State.characters.find(c => c.id === conv.character_id);
    return char ? char.name : 'Assistant';
}

// Configure marked for chat rendering
if (typeof marked !== 'undefined') {
    const renderer = new marked.Renderer();
    const origLink = renderer.link.bind(renderer);
    renderer.link = function(href, title, text) {
        const html = origLink(href, title, text);
        return html.replace('<a ', '<a target="_blank" rel="noopener" ');
    };
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
    const contentEl = streamingDiv.querySelector('.message-content');
    contentEl.innerHTML = '<span class="thinking-indicator"><span class="thinking-dots"></span> Thinking...</span>';
    scrollToBottom();
}

function hideThinkingIndicator() {
    if (!streamingDiv) return;
    const contentEl = streamingDiv.querySelector('.message-content');
    contentEl.innerHTML = '';
    contentEl.dataset.rawContent = '';
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
        '<div class="message-actions">' +
        '<button onclick="cancelGeneration()" title="Cancel">&#9632;</button>' +
        '</div></div>' +
        '<div class="message-content"></div>';
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

function appendToolBlock(name, toolId) {
    if (!streamingDiv) return;
    const contentEl = streamingDiv.querySelector('.message-content');
    const block = document.createElement('div');
    block.className = 'tool-block';
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

function finalizeToolBlock(result, toolId, imageUrl) {
    if (!streamingDiv) return;
    const block = streamingDiv.querySelector(`.tool-block[data-tool-id="${toolId}"]`)
                || streamingDiv.querySelector('.tool-block:last-child');
    if (!block) return;
    const resultEl = block.querySelector('.tool-block-result');
    // Truncate long results for display
    const display = result.length > 2000 ? result.substring(0, 2000) + '\n... (truncated)' : result;
    resultEl.textContent = display;

    // If this tool produced an image, display it inline
    if (imageUrl) {
        const imgContainer = document.createElement('div');
        imgContainer.className = 'tool-image-result';
        const img = document.createElement('img');
        img.src = imageUrl;
        img.alt = 'Generated image';
        img.className = 'generated-image';
        img.addEventListener('click', () => {
            const body = document.getElementById('preview-modal-body');
            body.innerHTML = '<img src="' + imageUrl + '" style="max-width:100%;max-height:80vh;">';
            document.getElementById('modal-preview').classList.remove('hidden');
        });
        imgContainer.appendChild(img);
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
    if (State.isStreaming) return;
    const msg = State.messages.find(m => m.id === msgId);
    if (!msg || msg.role !== 'user') return;

    const msgEl = document.querySelector(`.message[data-msg-id="${msgId}"]`);
    if (!msgEl) return;
    const contentEl = msgEl.querySelector('.message-content');

    const textarea = document.createElement('textarea');
    textarea.className = 'edit-message-input';
    textarea.value = msg.content;
    textarea.rows = Math.max(3, msg.content.split('\n').length);
    contentEl.replaceWith(textarea);
    textarea.focus();

    const btnRow = document.createElement('div');
    btnRow.className = 'edit-message-actions';
    btnRow.innerHTML = `
        <button class="btn-small edit-save">Send as new branch</button>
        <button class="btn-small edit-cancel">Cancel</button>
    `;
    textarea.after(btnRow);

    btnRow.querySelector('.edit-cancel').addEventListener('click', () => {
        const newContent = document.createElement('div');
        newContent.className = 'message-content';
        newContent.innerHTML = formatContent(msg.content);
        textarea.replaceWith(newContent);
        btnRow.remove();
    });

    btnRow.querySelector('.edit-save').addEventListener('click', async () => {
        const newText = textarea.value.trim();
        if (!newText) return;

        try {
            // Find parent of this message to branch from
            const msgIdx = State.messages.findIndex(m => m.id === msgId);
            const parentMsg = msgIdx > 0 ? State.messages[msgIdx - 1] : null;
            const parentId = parentMsg ? parentMsg.id : null;

            // Post new user message as sibling (new branch)
            const newMsg = await API.post(`/api/conversations/${State.currentConvId}/messages`, {
                role: 'user',
                content: newText,
                parent_id: parentId,
            });

            // Rebuild message list up to the edit point, then add new message
            State.messages = State.messages.slice(0, msgIdx);
            State.messages.push(newMsg);
            renderMessages();
            scrollToBottom();

            // Trigger generation
            if (State.ws && State.ws.readyState === WebSocket.OPEN) {
                showGenStatus('Generating...');
                State.ws.send(JSON.stringify({
                    action: 'generate',
                    parent_id: newMsg.id,
                }));
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
    if (!streamingDiv) return;
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
