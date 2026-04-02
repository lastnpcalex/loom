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
let _wsReconnectTimer = null;

function connectWebSocket(convId, _attempt) {
    // Cancel any pending reconnect timer
    if (_wsReconnectTimer) { clearTimeout(_wsReconnectTimer); _wsReconnectTimer = null; }

    // Close previous WS without triggering its onclose reconnect
    if (State.ws) {
        State.ws._replaced = true;  // flag so onclose skips reconnect
        State.ws.close();
        State.ws = null;
    }

    const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${protocol}://${location.host}/ws/chat/${convId}`);
    ws._reconnectAttempt = _attempt || 0;

    ws.onopen = () => {
        console.log('WebSocket connected');
        ws._reconnectAttempt = 0;
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
        // Server immediately sends generation_active (with snapshot) or generation_idle.
        // We let those handlers trigger loadMessages — no more racy setTimeout.
        ws._needsSync = true;
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
        // If this WS was intentionally replaced, don't reconnect
        if (ws._replaced || State.ws !== ws) return;
        // Reset streaming UI — server will keep generating and save the result.
        if (State.isStreaming) {
            State.isStreaming = false;
            document.getElementById('btn-send').disabled = false;
            removeStreamingMessage();
            showGenStatus('Reconnecting... generation continues on server');
        }
        // Reconnect: instant first try, then back off (cap at 8s)
        if (State.currentConvId === convId && State.currentView !== 'home') {
            const attempt = (ws._reconnectAttempt || 0) + 1;
            const delay = attempt <= 1 ? 100 : Math.min(500 * Math.pow(2, attempt - 2), 8000);
            console.log(`[WS] Reconnecting in ${Math.round(delay)}ms (attempt ${attempt})`);
            _wsReconnectTimer = setTimeout(() => {
                _wsReconnectTimer = null;
                if (State.currentConvId === convId && State.currentView !== 'home') {
                    connectWebSocket(convId, attempt);
                }
            }, delay);
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
        if (!State.ws || State.ws.readyState === WebSocket.CLOSED) {
            console.log('Tab visible — reconnecting WebSocket');
            connectWebSocket(State.currentConvId);
        } else if (State.ws && State.ws.readyState === WebSocket.OPEN) {
            // WS is open but we may have missed events while backgrounded — resync
            loadMessages(State.currentConvId);
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
        // If still streaming, re-create the streaming div (renderMessages destroyed it)
        if (State.isStreaming && !streamingDiv) {
            appendStreamingMessage();
        }
        scrollToBottom();
        if (State.messages.length > prevCount && prevCount > 0 && !State.isStreaming) {
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
    // If we're not streaming AND we've already established branch tracking, reject stale
    // parallel sibling events. But don't reject if tracking hasn't been set up yet —
    // that means we're in the pre-stream window (generate sent, stream_start not yet received).
    // Rejecting there silently drops error/status events for a generation that just failed fast.
    if (!State.isStreaming && data.gen_id != null &&
        (State._streamIsOurBranch !== undefined || State._followingGenId != null)) {
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
                hidePlanBar();
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

        case 'usage': {
            if (!State._streamIsOurBranch || !streamingDiv) break;
            const tokEl = streamingDiv.querySelector('.gen-token-info');
            if (tokEl) {
                tokEl.textContent = '↑' + _fmtTok(data.input_tokens) + ' ↓' + _fmtTok(data.output_tokens) + ' · ';
                tokEl.dataset.hasUsage = '1';  // stop timer from overwriting with chunk count
            }
            break;
        }

        case 'ask_user_question':
            renderAskUserQuestion(data.questions, data.tool_id);
            break;

        case 'plan_ready':
            renderPlanReady(data.plan, data.plan_file, data.tool_id);
            // Browser push if tab hidden (bell handled by plan_landed broadcast)
            if (document.hidden && typeof Notification !== 'undefined' && Notification.permission === 'granted') {
                new Notification('A Shadow Loom — Plan Ready', {
                    body: 'Plan awaiting review' + (data.plan_file ? ': ' + data.plan_file : ''),
                    icon: '/static/img/loom-ico-transparent.png',
                });
            }
            break;

        case 'permission_request':
            // Always add to notification bell (works from any conversation)
            addPermissionNotification(data);
            // Also render inline if we're viewing the right conversation and streaming
            if ((!data.conv_id || data.conv_id === State.currentConvId) && streamingDiv) {
                showPermissionPrompt(data);
            }
            // Push notification if tab is hidden
            if (document.hidden && typeof Notification !== 'undefined' && Notification.permission === 'granted') {
                const n = new Notification('A Shadow Loom — Permission Request', {
                    body: `${data.tool_name}: ${(data.input_summary || '').substring(0, 100)}`,
                    icon: '/static/img/loom-ico-transparent.png',
                    tag: 'perm-' + data.request_id,
                    requireInteraction: true,
                });
                n.onclick = () => { window.focus(); n.close(); };
            }
            break;

        case 'permission_resolved':
            resolvePermissionPrompt(data.request_id, data.allowed);
            resolvePermissionNotification(data.request_id, data.allowed);
            break;

        case 'cc_debug_event':
            console.log('[CC debug]', data.event_type, data.data);
            break;

        case 'branch_landed': {
            // Global notification — a generation completed somewhere (maybe another conversation)
            const isCurrentConv = data.conv_id === State.currentConvId;
            const isWatching = isCurrentConv && State.currentView === 'chat' && !document.hidden;
            if (!isWatching) {
                _notifications.push({
                    type: 'branch',
                    id: data.message_id,
                    convId: data.conv_id,
                    convTitle: data.conv_title || 'Conversation',
                    parentId: null,
                    preview: (data.preview || '').slice(0, 120),
                    time: new Date(),
                });
                _renderNotifBell();
            }
            // Browser push if tab hidden
            if (document.hidden && typeof Notification !== 'undefined' && Notification.permission === 'granted') {
                new Notification('A Shadow Loom', {
                    body: `${data.conv_title || 'Conversation'} — response complete`,
                    icon: '/static/img/loom-ico-transparent.png',
                });
            }
            break;
        }

        case 'plan_landed': {
            const isCurrentConv = data.conv_id === State.currentConvId;
            const isWatching = isCurrentConv && State.currentView === 'chat' && !document.hidden;
            if (!isWatching) {
                _notifications.push({
                    type: 'branch',
                    id: Date.now(),
                    convId: data.conv_id,
                    convTitle: data.conv_title || 'Conversation',
                    parentId: null,
                    preview: 'Plan ready' + (data.plan_file ? ' — ' + data.plan_file : ''),
                    time: new Date(),
                });
                _renderNotifBell();
            }
            if (document.hidden && typeof Notification !== 'undefined' && Notification.permission === 'granted') {
                new Notification('A Shadow Loom — Plan Ready', {
                    body: `${data.conv_title || 'Conversation'} — plan awaiting review`,
                    icon: '/static/img/loom-ico-transparent.png',
                });
            }
            break;
        }

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
                // Parallel sibling finished — refresh tree (bell handled by branch_landed)
                refreshTree();
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
            // Bell + browser push now handled by branch_landed (global broadcast)
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
            State._reconstructing = false;
            removeStreamingMessage();
            State.isStreaming = false;
            State._streamIsOurBranch = undefined;
            State._followingGenId = null;
            document.getElementById('btn-send').disabled = false;
            hideGenStatus();
            showRetryBar('Generation cancelled');
            refreshTree();
            // Reload messages to pick up any partial draft saved on cancel
            loadMessages(State.currentConvId);
            _flushQueuedGeneration();
            break;

        case 'error':
            if (!_isOurBranch(data)) {
                hideGenStatus();
                refreshTree();
                if (data.message_id) {
                    const viewedIds = new Set(State.messages.map(m => m.id));
                    if (viewedIds.has(data.message_id)) loadMessages(State.currentConvId);
                }
                break;
            }
            removeStreamingMessage();
            State.isStreaming = false;
            State._streamIsOurBranch = undefined;
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

        case 'generation_active': {
            // Reconnected while a generation is still running — use snapshot to rebuild UI
            hideRetryBar();
            hidePlanBar();
            State.isStreaming = true;
            const snapshots = data.snapshots || [];
            if (snapshots.length > 0) {
                const snap = snapshots[0];
                // Check if this generation is on our current branch
                const myMsgIds = new Set(State.messages.map(m => m.id));
                const isOurBranch = !snap.parent_id || myMsgIds.has(snap.parent_id);
                State._streamIsOurBranch = isOurBranch;
                State._followingGenId = snap.gen_id ?? null;
                _streamStartTime = (snap.started_at || 0) * 1000;
                _streamTokenCount = 0;
                if (!State._reconstructing) {
                    State._reconstructing = true;
                    const activeWs = State.ws;
                    loadMessages(State.currentConvId).then(() => {
                        State._reconstructing = false;
                        if (State.ws !== activeWs || !State.isStreaming) return;
                        // Re-check branch after messages loaded (State.messages is now fresh)
                        const freshIds = new Set(State.messages.map(m => m.id));
                        const stillOurs = !snap.parent_id || freshIds.has(snap.parent_id);
                        State._streamIsOurBranch = stillOurs;
                        if (stillOurs) {
                            _reconstructFromSnapshot(snap);
                        }
                    }).catch(() => { State._reconstructing = false; });
                }
            } else {
                // No snapshot — just load messages
                State._streamIsOurBranch = true;  // assume ours, will be corrected by stream_start
                if (!State._reconstructing) {
                    loadMessages(State.currentConvId);
                }
            }
            if (State.ws) State.ws._needsSync = false;
            break;
        }

        case 'generation_idle':
            // Server confirms no generation running — reset any stuck streaming state
            State._reconstructing = false;  // clear any stale reconstruction lock
            if (State.isStreaming || (State.ws && State.ws._needsSync)) {
                State.isStreaming = false;
                State._streamIsOurBranch = undefined;
                State._followingGenId = null;
                document.getElementById('btn-send').disabled = false;
                removeStreamingMessage();
                hideGenStatus();
                loadMessages(State.currentConvId);
            }
            if (State.ws) State.ws._needsSync = false;
            break;
    }
}

function updateContextInfo(data) {
    // Context token info now shown via gen status bar, not header
}

/**
 * Reconstruct streaming UI from a server-side generation snapshot.
 * Called on WS reconnect when a generation is mid-flight.
 */
function _reconstructFromSnapshot(snap) {
    removeStreamingMessage();
    appendStreamingMessage();
    if (!streamingDiv) return;

    const contentEl = streamingDiv.querySelector('.message-content');
    const blocks = snap.content_blocks || [];

    if (blocks.length > 0) {
        // Replay content_blocks: render completed blocks as static HTML,
        // and the last block as a live streaming element
        for (let i = 0; i < blocks.length; i++) {
            const block = blocks[i];
            const isLast = i === blocks.length - 1;

            if (block.type === 'text') {
                if (isLast) {
                    // Last text block — render as streaming text span (cursor shows it's live)
                    const textSpan = document.createElement('span');
                    textSpan.className = 'streaming-text';
                    textSpan.dataset.rawContent = block.text || '';
                    textSpan.innerHTML = formatContent(block.text || '') + '<span class="typing-cursor"></span>';
                    contentEl.appendChild(textSpan);
                } else {
                    // Completed text block
                    const div = document.createElement('div');
                    div.innerHTML = formatContent(block.text || '');
                    contentEl.appendChild(div);
                }
            } else if (block.type === 'tool_use') {
                // Render tool block — if it has a result, it's finalized; otherwise still in progress
                if (block.result) {
                    // Completed tool — render as collapsed block
                    const toolDiv = document.createElement('div');
                    toolDiv.className = 'tool-block expanded';
                    const inputPreview = (block.input || '').substring(0, 3000);
                    const resultPreview = (block.result || '').substring(0, 2000);
                    toolDiv.innerHTML =
                        '<div class="tool-header" onclick="this.parentElement.classList.toggle(\'expanded\')">' +
                        '<span class="tool-toggle">&#9656;</span> ' +
                        '<span class="tool-name">' + escapeHtml(block.name || 'Tool') + '</span>' +
                        '</div>' +
                        '<div class="tool-body">' +
                        (inputPreview ? '<pre class="tool-input">' + escapeHtml(inputPreview) + '</pre>' : '') +
                        '<div class="tool-result"><pre>' + escapeHtml(resultPreview) + '</pre></div>' +
                        '</div>';
                    contentEl.appendChild(toolDiv);
                } else {
                    // In-progress tool — show as active
                    appendToolBlock(block.name, block.tool_id, false);
                    if (block.input) {
                        appendToolInput(block.input, block.tool_id);
                    }
                }
            } else if (block.type === 'thinking') {
                // Thinking blocks — show as collapsed
                const thinkDiv = document.createElement('div');
                thinkDiv.className = 'thinking-block';
                thinkDiv.innerHTML =
                    '<div class="thinking-header" onclick="this.parentElement.classList.toggle(\'expanded\')">' +
                    '<span class="thinking-toggle">&#9656;</span> Thinking</div>' +
                    '<div class="thinking-content"><pre>' + escapeHtml(block.text || '') + '</pre></div>';
                contentEl.appendChild(thinkDiv);
            }
        }
    } else if (snap.full_text) {
        // No structured blocks — just raw text (Weave mode)
        const textSpan = document.createElement('span');
        textSpan.className = 'streaming-text';
        textSpan.dataset.rawContent = snap.full_text;
        textSpan.innerHTML = formatContent(snap.full_text) + '<span class="typing-cursor"></span>';
        contentEl.appendChild(textSpan);
    }

    // Reset stream buffer so new chunks append cleanly
    _streamBuffer = '';
    _streamFlushTimer = null;
    showGenStatus('Reconnected — streaming in progress');
    scrollToBottom();
    console.log('[WS] Reconstructed streaming UI from snapshot:', blocks.length, 'blocks,', (snap.full_text || '').length, 'chars');
}

// ── Skills / Slash Commands ──

let _cachedSkills = null;  // cached from /api/skills

async function _loadSkills() {
    if (_cachedSkills) return _cachedSkills;
    try {
        const convParam = State.currentConvId ? `?conv_id=${State.currentConvId}` : '';
        _cachedSkills = await API.get(`/api/skills${convParam}`);
    } catch {
        _cachedSkills = [];
    }
    return _cachedSkills;
}

function _invalidateSkillsCache() { _cachedSkills = null; }

/**
 * Translate a /slash command into a natural language prompt for CC.
 * Returns null if the input is not a slash command.
 */
function _translateSlashCommand(content, skills) {
    if (!content.startsWith('/')) return null;
    const match = content.match(/^\/(\S+)\s*(.*)?$/);
    if (!match) return null;

    const cmdName = match[1].toLowerCase();
    const args = (match[2] || '').trim();

    const skill = skills.find(s =>
        s.command === `/${cmdName}` || s.name === cmdName
    );

    if (!skill) return null;

    // Meta commands are handled by Loom natively, not sent to CC
    if (skill.mode === 'meta') {
        return { meta: true, skillName: skill.name, args };
    }

    let prompt = skill.prompt_template || `Run the ${skill.name} skill.`;
    prompt = prompt.replace('{args}', args || '');

    // If user provided args and template didn't have {args}, append them
    if (args && !skill.prompt_template?.includes('{args}')) {
        prompt += `\n\nAdditional context: ${args}`;
    }

    return { prompt, skillName: skill.name };
}

/**
 * Handle meta commands that Loom processes natively (not sent to CC).
 */
function _handleMetaCommand(name, args) {
    switch (name) {
        case 'skills':
            _loadSkills().then(skills => {
                const lines = skills.map(s =>
                    `${s.command}  [${s.source}/${s.mode || 'headless'}]  ${s.description || ''}`
                );
                showToast(`${skills.length} commands available`, 3000);
                // Show as a system message in chat
                const container = document.getElementById('messages-container');
                if (container) {
                    const el = document.createElement('div');
                    el.className = 'system-message';
                    el.innerHTML = `<pre style="font-size:0.85em;color:var(--text-dim);white-space:pre-wrap">`
                        + `Available commands (${skills.length}):\n\n`
                        + escapeHtml(lines.join('\n'))
                        + `</pre>`;
                    container.appendChild(el);
                    el.scrollIntoView({ behavior: 'smooth' });
                }
            });
            break;
        case 'status':
            showToast(State.isGenerating ? 'Generation in progress...' : 'Idle — no active generation');
            break;
        case 'stats':
        case 'usage':
            API.get('/api/health').then(h => {
                showToast(`Uptime: ${Math.round((h.uptime || 0) / 60)}m | Conversations: ${h.conversations || '?'}`, 4000);
            }).catch(() => showToast('Could not fetch stats'));
            break;
        case 'permissions':
            showToast('Permissions are managed via the notification bell', 3000);
            break;
        case 'export':
            if (State.currentConvId) {
                window.open(`/api/conversations/${State.currentConvId}/export`, '_blank');
                showToast('Exporting conversation...');
            } else {
                showToast('No conversation selected');
            }
            break;
        case 'settings':
            // Open settings panel if it exists
            const settingsBtn = document.querySelector('[data-action="settings"]') || document.getElementById('settings-btn');
            if (settingsBtn) settingsBtn.click();
            else showToast('Settings panel not available');
            break;
        case 'fast':
            showToast('Fast mode toggle — configure in conversation settings', 3000);
            break;
        case 'passes':
            showToast('Review passes — configure in conversation settings', 3000);
            break;
        case 'privacy':
            showToast('Privacy settings — configure in Claude Code directly', 3000);
            break;
        default:
            showToast(`/${name} is handled locally but not yet implemented`, 3000);
    }
}

/**
 * Initialize slash command autocomplete on the input textarea.
 */
function _initSlashAutocomplete() {
    const input = document.getElementById('user-input');
    const container = document.getElementById('input-area') || input.parentElement;

    // Create autocomplete dropdown
    const dropdown = document.createElement('div');
    dropdown.id = 'slash-autocomplete';
    dropdown.className = 'slash-autocomplete hidden';
    container.style.position = 'relative';
    container.appendChild(dropdown);

    let _selectedIdx = -1;

    input.addEventListener('input', async () => {
        const val = input.value;
        if (!val.startsWith('/') || val.includes('\n')) {
            dropdown.classList.add('hidden');
            return;
        }
        const query = val.slice(1).toLowerCase();
        // Show loading spinner on first fetch
        if (!_cachedSkills) {
            dropdown.innerHTML = '<div class="slash-item slash-loading"><span class="slash-desc">Loading commands...</span></div>';
            dropdown.classList.remove('hidden');
        }
        const skills = await _loadSkills();
        const matches = query
            ? skills.filter(s =>
                s.name.toLowerCase().includes(query) ||
                (s.command || '').toLowerCase().includes('/' + query) ||
                (s.description || '').toLowerCase().includes(query)
              ).slice(0, 15)
            : skills.slice(0, 31);  // Show all on bare "/"

        if (matches.length === 0) {
            dropdown.classList.add('hidden');
            return;
        }

        _selectedIdx = -1;
        dropdown.innerHTML = '';
        matches.forEach((skill, i) => {
            const item = document.createElement('div');
            item.className = 'slash-item';
            const sourceClass = skill.source === 'user' ? 'user' : 'system';
            const modeClass = skill.mode === 'meta' ? 'meta' : '';
            const sourceLabel = skill.source === 'user' ? 'user'
                : skill.mode === 'meta' ? 'loom' : 'system';
            const sourceTag = `<span class="slash-source ${sourceClass} ${modeClass}">${sourceLabel}</span>`;
            item.innerHTML =
                `<span class="slash-cmd">${escapeHtml(skill.command || '/' + skill.name)}</span>` +
                sourceTag +
                `<span class="slash-desc">${escapeHtml(skill.description || '').substring(0, 80)}</span>`;
            item.addEventListener('click', () => {
                input.value = (skill.command || '/' + skill.name) + ' ';
                dropdown.classList.add('hidden');
                input.focus();
            });
            item.addEventListener('mouseenter', () => {
                dropdown.querySelectorAll('.slash-item').forEach(el => el.classList.remove('selected'));
                item.classList.add('selected');
                _selectedIdx = i;
            });
            dropdown.appendChild(item);
        });
        dropdown.classList.remove('hidden');
    });

    // Keyboard navigation
    input.addEventListener('keydown', (e) => {
        if (dropdown.classList.contains('hidden')) return;
        const items = dropdown.querySelectorAll('.slash-item');
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            _selectedIdx = Math.min(_selectedIdx + 1, items.length - 1);
            items.forEach((el, i) => el.classList.toggle('selected', i === _selectedIdx));
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            _selectedIdx = Math.max(_selectedIdx - 1, 0);
            items.forEach((el, i) => el.classList.toggle('selected', i === _selectedIdx));
        } else if ((e.key === 'Tab' || e.key === 'Enter') && _selectedIdx >= 0) {
            e.preventDefault();
            items[_selectedIdx].click();
        } else if (e.key === 'Escape') {
            dropdown.classList.add('hidden');
        }
    });

    // Close on outside click
    document.addEventListener('click', (e) => {
        if (!container.contains(e.target)) dropdown.classList.add('hidden');
    });
}


// ── Send Message ──

let _queuedGeneration = null;  // queued message to generate after current stream ends

let _sendInFlight = false;
async function sendMessage() {
    if (_sendInFlight) return;
    if (!State.currentConvId) {
        showToast('Create or select a conversation first', 'error');
        return;
    }

    const input = document.getElementById('user-input');
    let content = input.value.trim();
    const isClaudeMode = State.currentConv && State.currentConv.mode === 'claude';
    const hasImages = State.pendingImages.length > 0;
    if (!content && !(hasImages && !isClaudeMode)) return;

    // Handle slash commands for CC mode
    if (isClaudeMode && content.startsWith('/')) {
        const skills = await _loadSkills();
        const translated = _translateSlashCommand(content, skills);
        if (translated && translated.meta) {
            _handleMetaCommand(translated.skillName, translated.args);
            input.value = '';
            return;
        }
        // Pass slash commands directly to CC — it handles skills natively
        if (translated) {
            showToast(`Running skill: ${translated.skillName}`);
        }
    }

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

    _sendInFlight = true;
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
            scrollToBottom(true);
            showToast('Message queued — will send after current turn');
        } else {
            State.messages.push(msg);
            renderMessages();
            scrollToBottom(true);

            // Request generation via WebSocket
            if (State.ws && State.ws.readyState === WebSocket.OPEN) {
                const count = State.branchCount || 1;
                showGenStatus(count > 1 ? `Generating ${count} branches...` : 'Sending...');
                _triggerParallelGenerate(count, msg.id);
            }
        }
    } catch (err) {
        showToast('Failed to send message', 'error');
    } finally {
        _sendInFlight = false;
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

// ── Notifications (background generation landings + permission requests) ──

const _notifications = [];

function addNotification(message) {
    const preview = (message.content || '').replace(/[#*_`>\[\]]/g, '').trim();
    _notifications.push({
        type: 'branch',
        id: message.id,
        convId: State.currentConvId,
        convTitle: State.currentConv?.title || 'Conversation',
        parentId: message.parent_id,
        preview: preview.slice(0, 120) + (preview.length > 120 ? '…' : ''),
        time: new Date(),
    });
    _renderNotifBell();
}

function addPermissionNotification(data) {
    // Don't duplicate — check if we already have this request_id
    if (_notifications.some(n => n.type === 'permission' && n.requestId === data.request_id)) return;
    _notifications.unshift({  // permissions go to the top
        type: 'permission',
        requestId: data.request_id,
        convId: data.conv_id || State.currentConvId,
        toolName: data.tool_name || 'Unknown',
        inputSummary: (data.input_summary || '').substring(0, 200),
        resolved: false,
        time: new Date(),
    });
    _renderNotifBell();
    // Auto-open the dropdown for urgent permission requests
    const dropdown = document.getElementById('notif-dropdown');
    if (dropdown && dropdown.classList.contains('hidden')) {
        dropdown.classList.remove('hidden');
        _renderNotifDropdown();
    }
}

function resolvePermissionNotification(requestId, allowed) {
    const n = _notifications.find(n => n.type === 'permission' && n.requestId === requestId);
    if (n) {
        n.resolved = true;
        n.allowed = allowed;
        // Re-render if dropdown is visible
        const dropdown = document.getElementById('notif-dropdown');
        if (dropdown && !dropdown.classList.contains('hidden')) _renderNotifDropdown();
        // Remove after a short delay
        setTimeout(() => {
            const idx = _notifications.indexOf(n);
            if (idx !== -1) _notifications.splice(idx, 1);
            _renderNotifBell();
        }, 2000);
    }
}

function _renderNotifBell() {
    const bell = document.getElementById('notif-bell');
    const badge = document.getElementById('notif-badge');
    if (!bell) return;
    bell.classList.remove('hidden');
    const pendingPerms = _notifications.filter(n => n.type === 'permission' && !n.resolved).length;
    const total = _notifications.length;
    if (total > 0) {
        badge.textContent = total;
        badge.classList.remove('hidden');
        bell.classList.add('notif-active');
        // Pulse the bell for pending permissions
        if (pendingPerms > 0) {
            bell.classList.add('notif-urgent');
        } else {
            bell.classList.remove('notif-urgent');
        }
    } else {
        badge.classList.add('hidden');
        bell.classList.remove('notif-active', 'notif-urgent');
        document.getElementById('notif-dropdown').classList.add('hidden');
    }
}

function _renderNotifDropdown() {
    const list = document.getElementById('notif-list');
    list.innerHTML = '';
    if (_notifications.length === 0) {
        list.innerHTML = '<div class="notif-empty">No notifications</div>';
        return;
    }
    for (const n of _notifications) {
        if (n.type === 'permission') {
            list.appendChild(_renderPermissionNotifItem(n));
        } else {
            list.appendChild(_renderBranchNotifItem(n));
        }
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

function _renderBranchNotifItem(n) {
    const item = document.createElement('div');
    item.className = 'notif-item notif-branch';
    const timeStr = n.time.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    item.innerHTML = `<span class="notif-time">${timeStr}</span>`
        + `<span class="notif-preview">${escapeHtml(n.preview || '(empty)')}</span>`;
    item.addEventListener('click', async () => {
        document.getElementById('notif-dropdown').classList.add('hidden');
        const idx = _notifications.indexOf(n);
        if (idx !== -1) _notifications.splice(idx, 1);
        _renderNotifBell();
        if (State.currentConvId !== n.convId) {
            State._skipLoadOnChat = true;
            await loadConversation(n.convId);
        }
        await switchToBranch(n.id);
        switchView('chat');
    });
    return item;
}

function _renderPermissionNotifItem(n) {
    const item = document.createElement('div');
    item.className = 'notif-item notif-permission' + (n.resolved ? ' notif-resolved' : '');
    const timeStr = n.time.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

    if (n.resolved) {
        item.innerHTML =
            `<span class="notif-time">${timeStr}</span>` +
            `<span class="notif-perm-status ${n.allowed ? 'perm-allowed' : 'perm-denied'}">${n.allowed ? 'Allowed' : 'Denied'}</span> ` +
            `<span class="notif-perm-tool">${escapeHtml(n.toolName)}</span>`;
        return item;
    }

    item.innerHTML =
        `<div class="notif-perm-header">` +
        `<span class="notif-time">${timeStr}</span>` +
        `<span class="notif-perm-tool">${escapeHtml(n.toolName)}</span>` +
        `</div>` +
        `<div class="notif-perm-summary">${escapeHtml(n.inputSummary)}</div>` +
        `<div class="notif-perm-actions">` +
        `<button class="notif-perm-btn allow" data-action="allow">Allow</button>` +
        `<button class="notif-perm-btn deny" data-action="deny">Deny</button>` +
        `<button class="notif-perm-btn allow-all" data-action="allow-all">Allow All</button>` +
        `</div>`;

    item.querySelectorAll('.notif-perm-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const action = btn.dataset.action;
            const allow = action === 'allow' || action === 'allow-all';
            const always = action === 'allow-all';
            if (State.ws && State.ws.readyState === WebSocket.OPEN) {
                State.ws.send(JSON.stringify({
                    action: 'permission_response',
                    request_id: n.requestId,
                    allow, always,
                }));
            }
            n.resolved = true;
            n.allowed = allow;
            _renderNotifDropdown();
            _renderNotifBell();
            // Also update the inline prompt if it exists
            resolvePermissionPrompt(n.requestId, allow);
            // Auto-remove after delay
            setTimeout(() => {
                const idx = _notifications.indexOf(n);
                if (idx !== -1) _notifications.splice(idx, 1);
                _renderNotifBell();
            }, 2000);
        });
    });

    // Click on the item body (not buttons) navigates to the conversation
    item.addEventListener('click', async (e) => {
        if (e.target.closest('.notif-perm-btn')) return;
        if (State.currentConvId !== n.convId) {
            State._skipLoadOnChat = true;
            await loadConversation(n.convId);
            switchView('chat');
        }
    });

    return item;
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
        dropdown.classList.add('hidden');
    });
    // Close dropdown on outside click
    document.addEventListener('click', (e) => {
        if (!dropdown.contains(e.target) && e.target !== bell) {
            dropdown.classList.add('hidden');
        }
    });
    // Request notification permission early for push notifs
    if (typeof Notification !== 'undefined' && Notification.permission === 'default') {
        // Don't request immediately — wait for user interaction
        document.addEventListener('click', function _reqPerm() {
            Notification.requestPermission();
            document.removeEventListener('click', _reqPerm);
        }, { once: true });
    }
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
    // Clear streamingDiv reference — innerHTML='' detaches it from DOM
    streamingDiv = null;

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
            if (entries[0].isIntersecting && !VIRTUAL_SCROLL.isLoadingOlder) {
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
    if (currentStart <= 0 || VIRTUAL_SCROLL.isLoadingOlder) return;
    VIRTUAL_SCROLL.isLoadingOlder = true;

    try {
        // Calculate new range
        const newStart = Math.max(0, currentStart - VIRTUAL_SCROLL.batchSize);
        const batch = renderMsgs.slice(newStart, currentStart);

        // Snapshot scroll position before DOM mutation
        const scrollHeightBefore = scrollParent.scrollHeight;
        const scrollTopBefore = scrollParent.scrollTop;

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

        // Restore scroll position after layout (rAF ensures reflow is done)
        requestAnimationFrame(() => {
            const added = scrollParent.scrollHeight - scrollHeightBefore;
            scrollParent.scrollTop = scrollTopBefore + added;
            VIRTUAL_SCROLL.isLoadingOlder = false;
        });
    } catch (e) {
        VIRTUAL_SCROLL.isLoadingOlder = false;
        throw e;
    }
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

        // Double rAF: first frame triggers reflow, second frame has correct layout
        requestAnimationFrame(() => requestAnimationFrame(() => {
            const targetEl = document.querySelector(`.message[data-msg-id="${targetId}"]`);
            if (targetEl) {
                targetEl.scrollIntoView({ behavior: 'instant', block: 'center' });
                targetEl.classList.add('message-highlight');
                setTimeout(() => targetEl.classList.remove('message-highlight'), 2000);
            } else {
                scrollToBottom();
            }
        }));
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
let _genTimerInterval = null;

function _fmtTok(n) {
    if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k';
    return String(n);
}

function _startGenTimer() {
    if (_genTimerInterval) clearInterval(_genTimerInterval);
    _genTimerInterval = setInterval(() => {
        if (!streamingDiv || !_streamStartTime) return;
        const secs = Math.floor((Date.now() - _streamStartTime) / 1000);
        const timerEl = streamingDiv.querySelector('.gen-timer');
        if (timerEl) timerEl.textContent = Math.floor(secs / 60) + ':' + String(secs % 60).padStart(2, '0');
        // In Ollama/weave mode each stream_chunk is one token — show live output count
        // until a real usage event (CC mode) overwrites it with accurate input+output
        const tokEl = streamingDiv.querySelector('.gen-token-info');
        if (tokEl && _streamTokenCount > 0 && !tokEl.dataset.hasUsage) {
            tokEl.textContent = '↓' + _fmtTok(_streamTokenCount) + ' · ';
        }
    }, 1000);
}

function _stopGenTimer() {
    if (_genTimerInterval) { clearInterval(_genTimerInterval); _genTimerInterval = null; }
}

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
        ' <button onclick="cancelGeneration()" title="Cancel generation" class="cancel-draft-btn">&#x2298;</button>' +
        '<span class="gen-stats"><span class="gen-token-info"></span><span class="gen-timer">0:00</span></span>' +
        '</div>';
    container.appendChild(streamingDiv);
    _startGenTimer();
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
    _stopGenTimer();

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
    _stopGenTimer();
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

            // Switch to and scroll to the new branch, then kick off generation
            await switchToBranch(newMsg.id, newMsg.id);
            hideRetryBar();  // loadMessages inside switchToBranch re-creates generate bar; hide it
            hidePlanBar();

            if (State.ws && State.ws.readyState === WebSocket.OPEN) {
                const count = State.branchCount || 1;
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

            // Also clear the notification bell
            resolvePermissionNotification(requestId, allow);
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
    // Show as a persistent bar in the messages container (not inside streamingDiv which
    // gets destroyed on stream_end — by then the user hasn't had a chance to click anything).
    hidePlanBar();
    const container = document.getElementById('messages');
    const bar = document.createElement('div');
    bar.id = 'plan-bar';
    bar.className = 'retry-bar plan-bar';
    bar.innerHTML =
        '<span class="plan-bar-label">Plan ready' +
        (planFile ? ' — <code>' + escapeHtml(planFile) + '</code>' : '') + '</span>' +
        '<button class="btn-small plan-action-btn approve" id="btn-plan-approve">Approve</button>' +
        '<button class="btn-small plan-action-btn revise" id="btn-plan-revise">Revise</button>';

    bar.querySelector('#btn-plan-approve').addEventListener('click', () => {
        hidePlanBar();
        const input = document.getElementById('user-input');
        input.value = 'Approved, proceed with the plan.';
        sendMessage();
    });
    bar.querySelector('#btn-plan-revise').addEventListener('click', () => {
        hidePlanBar();
        const input = document.getElementById('user-input');
        input.value = "I'd like to revise the plan: ";
        input.focus();
        // Move cursor to end
        input.selectionStart = input.selectionEnd = input.value.length;
    });

    container.appendChild(bar);
    scrollToBottom();
}

function hidePlanBar() {
    const existing = document.getElementById('plan-bar');
    if (existing) existing.remove();
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
