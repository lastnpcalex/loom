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
        ws._lastActivity = Date.now();
        // Web Worker keepalive — runs at full speed even in background tabs
        // (setInterval gets throttled to ~60s by Chrome in background). The
        // worker fires every 15s; we use it to both ping the server AND check
        // whether we've gone silent for too long (half-open TCP on mobile
        // Safari / NAT timeouts typically present this way — readyState stays
        // OPEN forever but no traffic flows).
        const workerBlob = new Blob([`setInterval(() => postMessage('tick'), 15000)`], {type: 'text/javascript'});
        ws._pingWorker = new Worker(URL.createObjectURL(workerBlob));
        ws._pingWorker.onmessage = () => {
            if (ws.readyState !== WebSocket.OPEN) return;
            // Staleness check BEFORE sending — if the socket's already silent
            // for 35s, force-close so onclose can trigger a reconnect. Don't
            // send a ping into the void; it'd reset nothing.
            const since = Date.now() - (ws._lastActivity || 0);
            if (since > 35000) {
                console.warn(`[WS] No activity for ${Math.round(since/1000)}s — forcing reconnect`);
                try { ws.close(); } catch {}
                return;
            }
            ws.send(JSON.stringify({ action: 'ping' }));
        };
        // Server immediately sends generation_active (with snapshot) or generation_idle.
        // We let those handlers trigger loadMessages — no more racy setTimeout.
        ws._needsSync = true;
        // If the user hit Send while the old socket was stale, replay now.
        _flushPendingGenerate();
    };

    ws.onmessage = (event) => {
        ws._lastActivity = Date.now();
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
            showGenStatus('Reconnecting... generation continues on server', true);
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

// DOM-authoritative streaming check. State.isStreaming is a flag we set and
// try to clear cleanly, but it leaks any time stream_end fires on a dropped
// WS, or when a permission prompt auto-creates a streamingDiv between turns
// and we lose track of whose turn it belonged to. Symptom: user sends a new
// message, it "disappears" (queued behind a stream that already ended), or
// stream chunks land in a detached div. The only ground truth is whether
// streamingDiv is (a) set and (b) still attached to #messages.
function _isActuallyStreaming() {
    if (!State.isStreaming) return false;
    if (!streamingDiv) return false;
    return !!streamingDiv.isConnected;
}

// Force-clear streaming state. Called before starting a new turn, to recover
// from any leaked flags / stale streamingDiv references.
function _resetStreamState() {
    if (typeof _streamFlushTimer !== 'undefined' && _streamFlushTimer) {
        clearTimeout(_streamFlushTimer);
        _streamFlushTimer = null;
    }
    if (typeof _streamBuffer !== 'undefined') _streamBuffer = '';
    State.isStreaming = false;
    State._streamIsOurBranch = undefined;
    State._followingGenId = null;
    State._parallelCount = 0;
    if (streamingDiv && !streamingDiv.isConnected) {
        streamingDiv = null;  // detached; drop the stale ref
    }
}

// Probe the WS when the tab returns to foreground. readyState alone is not
// enough on mobile — the socket often reports OPEN while actually being
// TCP-dead. If we've had no inbound message recently, force-close so the
// normal reconnect path can replace it.
function _probeWSOnResume(source) {
    if (!State.currentConvId) return;
    const ws = State.ws;
    if (!ws || ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) {
        console.log(`[WS] ${source} — no live socket, reconnecting`);
        connectWebSocket(State.currentConvId);
        return;
    }
    const since = Date.now() - (ws._lastActivity || 0);
    if (since > 15000) {
        console.log(`[WS] ${source} — ${Math.round(since/1000)}s since last activity, forcing reconnect`);
        try { ws.close(); } catch {}
        // onclose will trigger reconnect. Don't loadMessages here — the new
        // socket's generation_active / generation_idle will handle sync.
        return;
    }
    // Socket looks fresh. A light resync covers anything we missed while blurred.
    if (!State.isStreaming && !document.querySelector('.edit-message-input')) {
        loadMessages(State.currentConvId);
    }
}

document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') _probeWSOnResume('visibilitychange');
});
// iOS Safari back/forward cache restores the page without firing
// visibilitychange. pageshow with persisted=true is the reliable signal.
window.addEventListener('pageshow', (e) => {
    if (e.persisted) _probeWSOnResume('pageshow(bfcache)');
});

async function loadMessages(convId) {
    try {
        // NOTE: compactify banner is NOT cleared here — it's cleared in
        // loadConversation() when switching conversations, not on message reload

        const prevCount = State.messages.length;
        // Signature includes empty/non-empty content state so an empty draft
        // filling in (same id, "" → real text) forces a re-render instead of
        // being skipped by the cheap-diff bail below.
        const _msgSig = m => `${m.id}:${(m.content || '').length > 0 ? 1 : 0}`;
        const prevIdSig = State.messages.map(_msgSig).join(',');
        const treeData = await API.get(`/api/conversations/${convId}/tree`);
        State.treeData = treeData;  // keep branch indicators in sync
        hideRetryBar();
        const activeNodes = treeData.filter(n => n.is_active);
        if (activeNodes.length > 0) {
            // The leaf is the active node whose id is no other active node's
            // parent_id. Picking by max(id) breaks when a system message
            // (e.g. mid-stream auto-compact marker) is inserted with a higher
            // id than its assistant child — the marker would be misidentified
            // as the leaf, and /branch/<marker> walks UP to root, dropping
            // the actual reply from State.messages.
            const activeIds = new Set(activeNodes.map(n => n.id));
            const activeParents = new Set(
                activeNodes.map(n => n.parent_id).filter(p => p != null && activeIds.has(p))
            );
            const leaves = activeNodes.filter(n => !activeParents.has(n.id));
            // In a clean tree there's exactly one leaf. If somehow multiple,
            // prefer the deepest by created_at as a tiebreaker.
            const leaf = leaves.length === 1
                ? leaves[0]
                : leaves.sort((a, b) => (b.created_at || 0) - (a.created_at || 0))[0];
            const leafId = leaf ? leaf.id : activeNodes[activeNodes.length - 1].id;
            State.messages = await API.get(`/api/conversations/${convId}/branch/${leafId}`);
        } else {
            State.messages = [];
        }
        // Cheap change detection: if the message id list is identical to what
        // we already rendered, skip the container.innerHTML='' + full rebuild.
        // Heartbeat-driven WS reconnects used to call loadMessages on every
        // generation_idle and wipe the DOM even when nothing actually changed
        // — that's what the user was seeing as flicker.
        const newIdSig = State.messages.map(_msgSig).join(',');
        if (newIdSig === prevIdSig && prevIdSig !== '' && !State.isStreaming) {
            // Same chain. Refresh tree decorations only and bail.
            if (typeof refreshTree === 'function') refreshTree();
            return;
        }
        renderMessages();
        // If still streaming, renderMessages just destroyed the streaming div.
        // Drop the stale ref and ask the server for a snapshot — the reply
        // will rebuild the div with prior text intact, instead of an empty
        // box that loses everything streamed before this reload.
        if (streamingDiv && !streamingDiv.isConnected) streamingDiv = null;
        if (State.isStreaming && !streamingDiv) {
            if (State._streamIsOurBranch !== false) {
                _requestSnapshotIfStreaming();
            }
        }
        scrollToBottom();
        if (State.messages.length > prevCount && prevCount > 0 && !State.isStreaming) {
            showToast('Response loaded');
        }
    } catch (err) {
        console.error('loadMessages failed:', err);
    }
}

let _genStatusSpinnerTicker = null;
let _genStatusSpinnerFrame = 0;
const _GEN_STATUS_SPINNER_FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];

function _startGenStatusSpinner() {
    const spinner = document.getElementById('gen-status-spinner');
    const dot = document.querySelector('#generation-status .gen-status-dot');
    if (!spinner) return;
    spinner.classList.remove('hidden');
    if (dot) dot.classList.add('hidden');
    if (_genStatusSpinnerTicker) clearInterval(_genStatusSpinnerTicker);
    _genStatusSpinnerFrame = 0;
    _genStatusSpinnerTicker = setInterval(() => {
        spinner.textContent = _GEN_STATUS_SPINNER_FRAMES[_genStatusSpinnerFrame];
        _genStatusSpinnerFrame = (_genStatusSpinnerFrame + 1) % _GEN_STATUS_SPINNER_FRAMES.length;
    }, 80);
}

function _stopGenStatusSpinner() {
    const spinner = document.getElementById('gen-status-spinner');
    const dot = document.querySelector('#generation-status .gen-status-dot');
    if (_genStatusSpinnerTicker) { clearInterval(_genStatusSpinnerTicker); _genStatusSpinnerTicker = null; }
    if (spinner) { spinner.classList.add('hidden'); spinner.textContent = ''; }
    if (dot) dot.classList.remove('hidden');
}

function showGenStatus(text, reconnecting = false) {
    const el = document.getElementById('generation-status');
    document.getElementById('gen-status-text').textContent = text;
    el.classList.toggle('reconnecting', reconnecting);
    el.classList.remove('hidden');
    // Use the braille spinner for vision-describe status; the dot for everything else.
    if (/^describing\b/i.test(text)) _startGenStatusSpinner();
    else _stopGenStatusSpinner();
    scrollToBottom();
}

function hideGenStatus() {
    const el = document.getElementById('generation-status');
    el.classList.add('hidden');
    el.classList.remove('reconnecting');
    _stopGenStatusSpinner();
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
            const retryMsg = { action: 'generate' };
            _attachCCSettings(retryMsg);
            State.ws.send(JSON.stringify(retryMsg));
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

// Event types that indicate the model/runtime is actively producing output
// or doing tool work — any of these should clear the pre-first-token
// "still working" indicator so it never lingers when activity is visible.
const _ACTIVITY_EVENT_TYPES = new Set([
    'stream_chunk', 'thinking_chunk',
    'tool_start', 'tool_input_chunk', 'tool_result', 'tool_use',
    'permission_request', 'ask_user_question',
    'state_update', 'compact_boundary', 'compact_summary_ready', 'compact_done',
    'plan_ready', 'plan_landed', 'canvas_updated', 'image_describe',
]);

function handleWSMessage(data) {
    if (_ACTIVITY_EVENT_TYPES.has(data.type)) _removeStreamWaiting();
    switch (data.type) {
        case 'context_info':
            if (!_isOurBranch(data)) break;
            updateContextInfo(data);
            if (data.was_compactified) {
                const summ = data.summarized_count || '?';
                const verb = data.verbatim_count || '?';
                showGenStatus(`Context: ${data.total_tokens.toLocaleString()} tokens (${summ} summarized, ${verb} verbatim) — Waiting for model...`);
            } else {
                showGenStatus(`Context: ${data.total_tokens.toLocaleString()} tokens — Waiting for model...`);
            }
            break;

        case 'compact_boundary':
            // Claude Code compactified its context — forked into a new branch.
            // When streaming, we CAN'T call switchToBranch — it rebuilds the
            // DOM and detaches the streamingDiv, so the incoming stream chunks
            // pile into an orphan div. Instead, splice the marker into State
            // and insert it DIRECTLY BEFORE the streaming div so the visual
            // order matches the tree (parent → [compact marker] → draft).
            State._compactedThisGen = true;
            State._compactData = data;
            showGenStatus('Context compactified — continuing generation...');
            if (data.marker_id) {
                const trigger = data.trigger || 'auto';
                const preTokens = data.pre_tokens;
                const tokenInfo = preTokens ? ` — ${preTokens.toLocaleString()} tokens before` : '';
                const markerContent = `[CC context compactified (${trigger})${tokenInfo}]`;
                // Find the draft (streaming) message — it should be the active leaf.
                const draftMsg = State.messages.find(m => m.role === 'assistant' && !m.content?.trim());
                const markerMsg = {
                    id: data.marker_id,
                    role: 'system',
                    content: markerContent,
                    parent_id: draftMsg ? draftMsg.parent_id : null,
                };
                // Re-parent the draft under the marker in local state so ordering is stable.
                if (draftMsg) draftMsg.parent_id = data.marker_id;

                // Insert into State.messages right before the draft (or at end)
                if (draftMsg) {
                    const draftIdx = State.messages.indexOf(draftMsg);
                    if (draftIdx >= 0) {
                        State.messages.splice(draftIdx, 0, markerMsg);
                    } else {
                        State.messages.push(markerMsg);
                    }
                } else {
                    State.messages.push(markerMsg);
                }

                // DOM insert: put the marker right before streamingDiv if it
                // exists and is attached, else just append to messages container.
                const container = document.getElementById('messages');
                const markerEl = createMessageElement(markerMsg);
                if (streamingDiv && streamingDiv.parentNode === container) {
                    container.insertBefore(markerEl, streamingDiv);
                } else if (container) {
                    container.appendChild(markerEl);
                }
                refreshTree();
            } else {
                loadMessages(State.currentConvId);
            }
            break;

        case 'compact_done':
            // Compaction finished — clear status and reload to show the new branch
            hideGenStatus();
            loadMessages(State.currentConvId);
            break;

        case 'compact_summary_ready':
            // Background task patched the marker row with the narrative summary.
            // Update the marker element in place (if it's visible) so the user
            // can expand "Previously" without a reload.
            if (data.marker_id) {
                const markerEl = document.querySelector(`.compact-marker[data-msg-id="${data.marker_id}"]`);
                if (markerEl) {
                    const summaryBox = markerEl.querySelector('.compact-marker-summary');
                    if (summaryBox && data.summary) {
                        summaryBox.innerHTML = formatContent(data.summary);
                    }
                }
                // Sync State.messages so re-renders keep the summary
                const m = State.messages?.find(x => x.id === data.marker_id);
                if (m && data.summary) {
                    const header = (m.content || '').split('\n', 1)[0];
                    m.content = header + '\n\n---\nPreviously:\n' + data.summary;
                }
            }
            break;

        case 'status':
            if (!_isOurBranch(data)) break;
            showGenStatus(data.text || 'Looming...');
            break;

        case 'image_describe': {
            const msg = State.messages.find(m => m.id === data.message_id);
            if (msg) {
                let existing = {};
                if (msg.image_alt) {
                    try { existing = JSON.parse(msg.image_alt) || {}; } catch { existing = {}; }
                }
                msg.image_alt = JSON.stringify({ ...existing, ...(data.descriptions || {}) });
            }
            const msgEl = document.querySelector(`.message[data-msg-id="${data.message_id}"]`);
            if (msgEl && data.descriptions) {
                for (const [filename, desc] of Object.entries(data.descriptions)) {
                    const figs = msgEl.querySelectorAll(`.message-image-figure[data-img-name="${CSS.escape(filename)}"]`);
                    figs.forEach(fig => {
                        let cap = fig.querySelector('.image-description');
                        if (!cap) {
                            cap = document.createElement('figcaption');
                            cap.className = 'image-description';
                            cap.title = 'From vision model';
                            fig.appendChild(cap);
                        }
                        cap.textContent = desc;
                    });
                }
            }
            break;
        }

        case 'stream_start': {
            // Check if this generation is for our current branch
            const parentId = data.parent_id;
            const myMsgIds = new Set(State.messages.map(m => m.id));
            const isOnOurBranch = parentId == null || myMsgIds.has(parentId);
            // If the isStreaming flag leaked from a previous turn (no live
            // streamingDiv attached) clear it now so we actually create a
            // fresh div for this stream. Without this, stream chunks fall
            // into the void and the user has to refresh to see anything.
            if (State.isStreaming && !_isActuallyStreaming()) {
                console.log('[WS] stream_start: clearing leaked isStreaming flag');
                _resetStreamState();
            }
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
            _removeStreamWaiting();
            appendToolInput(data.content, data.tool_id);
            break;

        case 'tool_result':
            if (!State._streamIsOurBranch) break;
            _removeStreamWaiting();
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
            // If output tokens registered, work is happening — drop the
            // pre-first-token indicator even if no chunks/tools fired yet.
            if ((data.output_tokens || 0) > 0) _removeStreamWaiting();
            // Compaction banner removed — branch fork is the visual cue now
            break;
        }

        case 'ask_user_question':
            renderAskUserQuestion(data.questions, data.tool_id);
            break;

        case 'plan_ready':
            // Plan display is handled by the ExitPlanMode permission prompt.
            // Just fire a browser push if tab is hidden.
            if (document.hidden && typeof Notification !== 'undefined' && Notification.permission === 'granted') {
                new Notification('A Shadow Loom — Plan Ready', {
                    body: 'Plan awaiting review' + (data.plan_file ? ': ' + data.plan_file : ''),
                    icon: '/static/img/loom-ico-transparent.png',
                });
            }
            break;

        case 'permission_request':
            // Permission request landing means tool work is happening — drop the
            // "still waiting for first token" indicator.
            _removeStreamWaiting();
            // Always add to notification bell (works from any conversation)
            addPermissionNotification(data);
            // Also render inline if we're viewing the right conversation.
            // If a snapshot reconstruction is in flight, queue the prompt —
            // rendering it now would attach it to a streamingDiv that's about
            // to be destroyed by _reconstructFromSnapshot's remove+re-append.
            if (!data.conv_id || data.conv_id === State.currentConvId) {
                if (State._reconstructing) {
                    (State._pendingPermPrompts = State._pendingPermPrompts || []).push(data);
                } else {
                    showPermissionPrompt(data);
                }
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

        case 'canvas_updated':
            refreshCanvasIframe();
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
            State._compactedThisGen = false;
            State._compactData = null;
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

        case 'warning':
            // Server-side non-fatal warning (e.g. CC silently downgraded model).
            // Show as a sticky toast so the user actually reads it.
            if (data.text) showToast(data.text, 8000);
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
                        if (State.ws !== activeWs) return;
                        // If the draft message has already landed from the DB
                        // (loadMessages picked up the committed final response),
                        // we're done — nothing to reconstruct.
                        const draftLanded = snap.draft_msg_id && State.messages.some(
                            m => m.id === snap.draft_msg_id && (m.content || '').trim()
                        );
                        if (draftLanded) {
                            _drainPendingPermPrompts();
                            return;
                        }
                        // Otherwise reconstruct from snapshot — covers the race
                        // where stream_end fired during loadMessages but the
                        // assistant row wasn't yet committed to the DB. Don't
                        // gate on State.isStreaming: a fast/rate-limited turn
                        // can finish before we get here, and we still need to
                        // render the partial output until the final lands.
                        const freshIds = new Set(State.messages.map(m => m.id));
                        const stillOurs = !snap.parent_id || freshIds.has(snap.parent_id);
                        State._streamIsOurBranch = stillOurs;
                        if (stillOurs) {
                            _reconstructFromSnapshot(snap);
                        }
                        _drainPendingPermPrompts();
                    }).catch(() => { State._reconstructing = false; _drainPendingPermPrompts(); });
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
            // Server confirms no generation running — reset any stuck streaming state.
            // If we were mid-reconstruction, force a reload: a fast turn can finish
            // (and commit its final assistant message) in the gap between
            // generation_active and generation_idle, leaving the new row unrendered.
            const wasReconstructing = State._reconstructing;
            State._reconstructing = false;
            // If a generate is stashed and about to fire, skip the reload —
            // an in-flight renderMessages() races with the upcoming stream_start
            // and wipes the streamingDiv mid-stream. The new stream will paint
            // the response itself; no reload needed.
            if (_pendingGenerate) {
                if (State.ws) State.ws._needsSync = false;
                break;
            }
            if (State.isStreaming || (State.ws && State.ws._needsSync) || wasReconstructing) {
                State.isStreaming = false;
                State._streamIsOurBranch = undefined;
                State._followingGenId = null;
                document.getElementById('btn-send').disabled = false;
                removeStreamingMessage();
                hideGenStatus();
                // Don't clobber an in-progress edit or typed draft on reconnect.
                const editing = !!document.querySelector('.edit-message-input');
                const ta = document.getElementById('message-input');
                const typing = !!(ta && ta.value && ta.value.trim().length > 0);
                if (!editing && !typing) {
                    loadMessages(State.currentConvId);
                }
            }
            if (State.ws) State.ws._needsSync = false;
            break;

        case 'pong':
            // Heartbeat ack — no UI action needed; onmessage already updated _lastActivity.
            break;
    }
}

function updateContextInfo(data) {
    let banner = document.getElementById('compactify-banner');
    if (data.was_compactified) {
        if (!banner) {
            banner = document.createElement('div');
            banner.id = 'compactify-banner';
            // Place above input area so it's visible without scrolling
            const inputArea = document.getElementById('input-area');
            inputArea.parentNode.insertBefore(banner, inputArea);
        }
        // Build summary line and expanded details per source
        let summaryLine, details = [];
        if (data.pre_tokens != null) {
            // Claude Code compaction
            const trigger = data.trigger === 'manual' ? 'manual' : 'auto';
            summaryLine = `Context compactified (${trigger})`;
            details.push(`Before: ${data.pre_tokens.toLocaleString()} tokens`);
            if (data.post_tokens) {
                details.push(`After: ${data.post_tokens.toLocaleString()} tokens`);
            }
        } else if (data.summarized_count != null) {
            // Local (Ollama) compaction
            summaryLine = `Context compactified — ${data.summarized_count} of ${data.total_messages || '?'} messages summarized`;
            details.push(`${data.verbatim_count || '?'} messages sent verbatim`);
            if (data.total_tokens) details.push(`Post-compaction context: ${data.total_tokens.toLocaleString()} tokens`);
            if (data.summary_text) {
                details.push(`--- Rolling summary ---\n${data.summary_text}`);
            }
        } else {
            summaryLine = 'Context compactified';
        }

        const detailsHtml = details.length
            ? `<div class="compactify-details">${details.map(d => `<pre>${escapeHtml(d)}</pre>`).join('')}</div>`
            : '';
        // Preserve open/closed state of existing <details> element
        const existingDetails = banner.querySelector('details.compactify-collapse');
        const wasOpen = existingDetails ? existingDetails.open : false;
        banner.innerHTML = `<details class="compactify-collapse"${wasOpen ? ' open' : ''}><summary><span class="compactify-icon">⧈</span> ${escapeHtml(summaryLine)}</summary>${detailsHtml}</details>`;
        banner.classList.remove('hidden');
    } else if (banner) {
        banner.classList.add('hidden');
    }
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

    // Re-render pending permission prompts that were lost when we rebuilt the
    // streaming div — but ONLY for the current conversation. Without this
    // filter, a page reload that replays every globally-pending perm inlines
    // prompts from other loom conversations into this branch's stream.
    for (const n of _notifications) {
        if (n.type !== 'permission' || n.resolved) continue;
        if (n.convId && n.convId !== State.currentConvId) continue;
        showPermissionPrompt({
            request_id: n.requestId,
            conv_id: n.convId,
            tool_name: n.toolName,
            input_summary: n.inputSummary,
        });
    }

    showGenStatus('Reconnected — streaming in progress', true);
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
        case 'help':
            // Alias for /skills — show all available commands
            _handleMetaCommand('skills', args);
            break;
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
        case 'hooks':
            API.get('/api/cc-hooks').then(data => {
                const container = document.getElementById('messages-container');
                if (!container) { showToast('Could not display hooks'); return; }
                const el = document.createElement('div');
                el.className = 'system-message';
                const hookEntries = data.hooks || {};
                if (Object.keys(hookEntries).length === 0) {
                    el.innerHTML = `<pre style="font-size:0.85em;color:var(--text-dim);white-space:pre-wrap">No CC hooks configured.\n\nSearched:\n${(data.paths || []).map(p => '  ' + p).join('\n')}</pre>`;
                } else {
                    let lines = 'CC Hooks:\n\n';
                    for (const [file, hooks] of Object.entries(hookEntries)) {
                        lines += `── ${file} ──\n`;
                        for (const [event, rules] of Object.entries(hooks)) {
                            lines += `  ${event}:\n`;
                            const ruleList = Array.isArray(rules) ? rules : [rules];
                            for (const rule of ruleList) {
                                const cmd = rule.command || rule.cmd || JSON.stringify(rule);
                                const matcher = rule.matcher ? ` (${rule.matcher})` : '';
                                lines += `    → ${cmd}${matcher}\n`;
                            }
                        }
                        lines += '\n';
                    }
                    el.innerHTML = `<pre style="font-size:0.85em;color:var(--text-dim);white-space:pre-wrap">${escapeHtml(lines)}</pre>`;
                }
                container.appendChild(el);
                el.scrollIntoView({ behavior: 'smooth' });
            }).catch(() => showToast('Failed to read hooks config'));
            break;
        case 'compact': {
            // CC compacts automatically — manual /compact just informs the user
            showToast('Context compacts automatically when needed. Use /clear to start a fresh branch.', 5000);
            break;
        }
        case 'clear': {
            // Start a fresh branch from the current point
            if (!State.currentConvId) { showToast('No conversation selected'); break; }
            const leaf = State.messages[State.messages.length - 1];
            if (!leaf) { showToast('No messages to branch from'); break; }
            API.post(`/api/conversations/${State.currentConvId}/messages`, {
                role: 'system',
                content: '[Fresh branch started by user]',
                parent_id: leaf.id,
            }).then(marker => {
                switchToBranch(marker.id, marker.id);
                showToast('Fresh branch started');
            }).catch(() => showToast('Failed to create branch', 'error'));
            break;
        }
        case 'compact-test': {
            // Simulate full compact branching flow — creates a real system message
            if (!State.currentConvId) { showToast('No conversation selected'); break; }
            const ctLeaf = State.messages[State.messages.length - 1];
            if (!ctLeaf) { showToast('No messages to compact from'); break; }
            API.post(`/api/conversations/${State.currentConvId}/messages`, {
                role: 'system',
                content: '[CC context compactified (test) — 185,000 tokens before]',
                parent_id: ctLeaf.id,
            }).then(marker => {
                showToast('Compact test — switching to new branch', 3000);
                switchToBranch(marker.id, marker.id);
            }).catch(() => showToast('Failed to create test compact marker', 'error'));
            break;
        }
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
    let _currentMatches = [];

    input.addEventListener('input', async () => {
        const val = input.value;
        if (!val.startsWith('/') || val.includes('\n')) {
            dropdown.classList.add('hidden');
            _currentMatches = [];
            return;
        }
        const query = val.slice(1).toLowerCase();
        // Show loading spinner on first fetch
        if (!_cachedSkills) {
            dropdown.innerHTML = '<div class="slash-item slash-loading"><span class="slash-desc">Loading commands...</span></div>';
            dropdown.classList.remove('hidden');
        }
        const skills = await _loadSkills();
        // Filter by prefix first (for Tab completion), then fuzzy
        const prefixMatches = skills.filter(s =>
            s.name.toLowerCase().startsWith(query) ||
            (s.command || '').toLowerCase().startsWith('/' + query)
        );
        const fuzzyMatches = query
            ? skills.filter(s =>
                !prefixMatches.includes(s) && (
                    s.name.toLowerCase().includes(query) ||
                    (s.description || '').toLowerCase().includes(query)
                )
              )
            : [];
        const matches = [...prefixMatches, ...fuzzyMatches].slice(0, 15);
        if (!query) matches.splice(0, matches.length, ...skills.slice(0, 31));
        _currentMatches = matches;

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
        } else if (e.key === 'Tab') {
            // CLI-style tab completion: fill to longest common prefix
            e.preventDefault();
            if (_currentMatches.length === 0) return;
            if (_selectedIdx >= 0) {
                // Item highlighted — select it directly
                items[_selectedIdx].click();
                return;
            }
            // Find longest common prefix among match names
            const names = _currentMatches.map(s => s.name.toLowerCase());
            let prefix = names[0];
            for (let i = 1; i < names.length; i++) {
                while (!names[i].startsWith(prefix)) {
                    prefix = prefix.slice(0, -1);
                }
                if (!prefix) break;
            }
            if (_currentMatches.length === 1) {
                // Single match — complete fully with trailing space
                input.value = '/' + _currentMatches[0].name + ' ';
                dropdown.classList.add('hidden');
            } else if (prefix.length > input.value.length - 1) {
                // Extend to common prefix
                input.value = '/' + prefix;
                input.dispatchEvent(new Event('input'));
            }
        } else if (e.key === 'Enter' && _selectedIdx >= 0) {
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

    // Handle slash commands — meta commands work in all modes
    if (content.startsWith('/')) {
        const skills = await _loadSkills();
        const translated = _translateSlashCommand(content, skills);
        if (translated && translated.meta) {
            _handleMetaCommand(translated.skillName, translated.args);
            input.value = '';
            return;
        }
        // Headless commands: substitute the NL prompt template for the raw /slash text
        if (translated) {
            content = translated.prompt;
            showToast(`Running: /${translated.skillName}`);
        }
    }

    // Add user message via REST — send image paths as JSON array
    const imagePaths = hasImages ? State.pendingImages.map(img => img.path) : null;
    const describeInput = document.getElementById('describe-context');
    const describeContext = describeInput ? describeInput.value.trim() : '';
    const isFromTree = State.currentView === 'tree';
    const msgData = {
        role: 'user',
        content: content,
        image_path: imagePaths,
    };
    if (describeContext) msgData.describe_context = describeContext;
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

        // Ground-truth streaming check — State.isStreaming alone leaks after
        // disconnect/reconnect or between turns when perm prompts create
        // stray streamingDivs. If the flag is set but no streamingDiv is
        // attached to the DOM, reset and treat as idle.
        const reallyStreaming = _isActuallyStreaming();
        if (!reallyStreaming && State.isStreaming) {
            console.log('[SEND] stale isStreaming flag — resetting before new turn');
            _resetStreamState();
        }
        if (reallyStreaming) {
            // Queue it — will fire when current stream ends
            _queuedGeneration = msg;
            State.messages.push(msg);
            // Show queued message immediately in chat
            const container = document.getElementById('messages');
            const el = createMessageElement(msg);
            el.classList.add('queued-message');
            // Add cancel/edit bar to the queued message
            const queueBar = document.createElement('div');
            queueBar.className = 'queue-actions';
            queueBar.innerHTML = `<button class="btn-small queue-edit-btn" title="Edit queued message">Edit</button>`
                + `<button class="btn-small queue-cancel-btn" title="Cancel queued message">Cancel</button>`;
            queueBar.querySelector('.queue-cancel-btn').addEventListener('click', () => cancelQueuedMessage(msg));
            queueBar.querySelector('.queue-edit-btn').addEventListener('click', () => editQueuedMessage(msg));
            el.appendChild(queueBar);
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

async function cancelQueuedMessage(msg) {
    _queuedGeneration = null;
    // Remove from DB
    try {
        await API.del(`/api/conversations/${State.currentConvId}/messages/${msg.id}`);
    } catch { /* already gone */ }
    // Remove from local state
    State.messages = State.messages.filter(m => m.id !== msg.id);
    const el = document.querySelector(`.message[data-msg-id="${msg.id}"]`);
    if (el) el.remove();
    showToast('Queued message cancelled');
}

async function editQueuedMessage(msg) {
    const msgEl = document.querySelector(`.message[data-msg-id="${msg.id}"]`);
    if (!msgEl) return;
    const contentEl = msgEl.querySelector('.message-content');
    const queueBar = msgEl.querySelector('.queue-actions');

    const textarea = document.createElement('textarea');
    textarea.className = 'edit-message-input';
    textarea.value = msg.content;
    textarea.rows = Math.max(3, msg.content.split('\n').length);
    contentEl.replaceWith(textarea);
    textarea.focus();
    const _resizeQueuedEdit = () => {
        const clone = textarea.cloneNode(true);
        clone.style.position = 'absolute';
        clone.style.visibility = 'hidden';
        clone.style.height = 'auto';
        clone.style.width = textarea.offsetWidth + 'px';
        textarea.parentNode.appendChild(clone);
        textarea.style.height = Math.min(clone.scrollHeight, 600) + 'px';
        clone.remove();
    };
    textarea.addEventListener('input', _resizeQueuedEdit);
    requestAnimationFrame(_resizeQueuedEdit);

    const btnRow = document.createElement('div');
    btnRow.className = 'edit-message-actions';
    btnRow.innerHTML = `<button class="btn-small edit-save">Save</button>`
        + `<button class="btn-small edit-cancel">Cancel edit</button>`;
    if (queueBar) queueBar.replaceWith(btnRow);
    else textarea.after(btnRow);

    btnRow.querySelector('.edit-cancel').addEventListener('click', () => {
        const newContent = document.createElement('div');
        newContent.className = 'message-content';
        newContent.innerHTML = formatContent(msg.content);
        textarea.replaceWith(newContent);
        // Restore queue bar
        const bar = document.createElement('div');
        bar.className = 'queue-actions';
        bar.innerHTML = `<button class="btn-small queue-edit-btn" title="Edit queued message">Edit</button>`
            + `<button class="btn-small queue-cancel-btn" title="Cancel queued message">Cancel</button>`;
        bar.querySelector('.queue-cancel-btn').addEventListener('click', () => cancelQueuedMessage(msg));
        bar.querySelector('.queue-edit-btn').addEventListener('click', () => editQueuedMessage(msg));
        btnRow.replaceWith(bar);
    });

    btnRow.querySelector('.edit-save').addEventListener('click', async () => {
        const newText = textarea.value.trim();
        if (!newText) return;
        try {
            const updated = await API.put(
                `/api/conversations/${State.currentConvId}/messages/${msg.id}`,
                { content: newText }
            );
            // Update local references
            msg.content = updated.content || newText;
            if (_queuedGeneration && _queuedGeneration.id === msg.id) {
                _queuedGeneration = msg;
            }
            const stateMsg = State.messages.find(m => m.id === msg.id);
            if (stateMsg) stateMsg.content = msg.content;

            // Replace textarea with rendered content
            const newContent = document.createElement('div');
            newContent.className = 'message-content';
            newContent.innerHTML = formatContent(msg.content);
            textarea.replaceWith(newContent);
            // Restore queue bar
            const bar = document.createElement('div');
            bar.className = 'queue-actions';
            bar.innerHTML = `<button class="btn-small queue-edit-btn" title="Edit queued message">Edit</button>`
                + `<button class="btn-small queue-cancel-btn" title="Cancel queued message">Cancel</button>`;
            bar.querySelector('.queue-cancel-btn').addEventListener('click', () => cancelQueuedMessage(msg));
            bar.querySelector('.queue-edit-btn').addEventListener('click', () => editQueuedMessage(msg));
            btnRow.replaceWith(bar);
            showToast('Queued message updated');
        } catch {
            showToast('Failed to update message', 'error');
        }
    });
}

function _flushQueuedGeneration() {
    if (!_queuedGeneration) return;
    const msg = _queuedGeneration;
    _queuedGeneration = null;
    // Message already in State.messages and rendered — just trigger generation
    // Remove queued styling and action bar
    const queuedEl = document.querySelector('.queued-message');
    if (queuedEl) {
        queuedEl.classList.remove('queued-message');
        const bar = queuedEl.querySelector('.queue-actions');
        if (bar) bar.remove();
    }
    if (State.ws && State.ws.readyState === WebSocket.OPEN) {
        showGenStatus('Sending queued message...');
        const queueMsg = { action: 'generate', parent_id: msg.id };
        _attachCCSettings(queueMsg);
        State.ws.send(JSON.stringify(queueMsg));
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
            const regenMsg = { action: 'regenerate', parent_id: result.parent_id };
            _attachCCSettings(regenMsg);
            State.ws.send(JSON.stringify(regenMsg));
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

    const isPlanNotif = n.toolName === 'ExitPlanMode' || n.toolName === 'exit_plan_mode';
    if (isPlanNotif) {
        item.classList.add('notif-plan');
        item.innerHTML =
            `<div class="notif-perm-header">` +
            `<span class="notif-time">${timeStr}</span>` +
            `<span class="notif-perm-tool">&#x1F9F5; Plan Ready</span>` +
            `</div>` +
            `<div class="notif-perm-actions">` +
            `<button class="notif-perm-btn plan-approve" data-action="allow">Approve</button>` +
            `<button class="notif-perm-btn plan-revise" data-action="deny">Revise</button>` +
            `</div>`;
    } else {
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
    }

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
            // If plan was approved, flip dropdown back to Act
            if (isPlanNotif && allow) {
                const permSel = document.getElementById('cc-permission-mode-inline');
                if (permSel) permSel.value = 'default';
            }
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
    document.getElementById('notif-refresh').addEventListener('click', (e) => {
        e.stopPropagation();
        const before = _notifications.length;
        _notifications.splice(0, _notifications.length, ..._notifications.filter(n => !(n.type === 'permission' && n.resolved)));
        if (_notifications.length !== before) _renderNotifBell();
        else _renderNotifDropdown();
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

    const renderMsgs = State.messages;

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

        // Anchor to an existing DOM element rather than scrollHeight deltas.
        // scrollHeight can keep growing after layout (images, fonts, code
        // blocks) so the delta math was under-adjusting and the view would
        // jump. Remember the first-rendered old message's screen position,
        // then scroll it back to the same spot after insert.
        const sentinel = document.getElementById('scroll-sentinel');
        const anchorEl = sentinel ? sentinel.nextElementSibling : container.firstElementChild;
        const anchorTopBefore = anchorEl ? anchorEl.getBoundingClientRect().top : null;

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

        // Restore scroll position: after paint, anchor element has shifted
        // down by (batch height). Offset scrollTop so its getBoundingClientRect
        // is back where it was. Double rAF covers fonts/images that resize
        // between layout and paint.
        const restoreAnchor = () => {
            if (anchorEl && anchorTopBefore != null) {
                const anchorTopAfter = anchorEl.getBoundingClientRect().top;
                const shift = anchorTopAfter - anchorTopBefore;
                if (shift) scrollParent.scrollTop += shift;
            }
            VIRTUAL_SCROLL.isLoadingOlder = false;
        };
        requestAnimationFrame(() => requestAnimationFrame(restoreAnchor));
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

// Pending generate stashed while we reconnect a dead/stale WS on mobile.
// Flushed by connectWebSocket's onopen handler.
let _pendingGenerate = null;

function _triggerParallelGenerate(count, parentId) {
    const isWeave = State.currentConv && State.currentConv.mode === 'weave';
    const n = isWeave ? Math.max(1, Math.min(5, count)) : 1;

    // Mobile safari / NAT can leave the socket reporting OPEN while it's
    // actually TCP-dead. send() goes into the void and no stream_start ever
    // comes back — the user sees their own message disappear into the grey.
    // Detect staleness (no inbound traffic in 10s) or closed socket, force
    // a reconnect, and replay the generate on the new socket's onopen.
    const ws = State.ws;
    const stale = !ws
        || ws.readyState === WebSocket.CLOSED
        || ws.readyState === WebSocket.CLOSING
        || (ws.readyState === WebSocket.OPEN && Date.now() - (ws._lastActivity || 0) > 30000)
        || ws.readyState === WebSocket.CONNECTING;
    if (stale) {
        console.log('[WS] generate: socket stale or not open — reconnecting and stashing');
        _pendingGenerate = { count: n, parentId };
        showGenStatus('Reconnecting...');
        if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
            try { ws.close(); } catch {}
        } else if (!ws || ws.readyState === WebSocket.CLOSED) {
            connectWebSocket(State.currentConvId);
        }
        return;
    }

    for (let i = 0; i < n; i++) {
        const msg = { action: 'generate' };
        if (parentId) msg.parent_id = parentId;
        _attachCCSettings(msg);
        ws.send(JSON.stringify(msg));
    }
    showGenStatus(n > 1 ? `Generating ${n} branches...` : 'Sending...');
}

// Fired by connectWebSocket onopen — replays a stashed generate so the user
// doesn't have to resend after a silent mobile reconnect.
function _flushPendingGenerate() {
    if (!_pendingGenerate) return;
    const { count, parentId } = _pendingGenerate;
    _pendingGenerate = null;
    // Defer one tick so server has processed our ws_chat setup (generation_idle etc.)
    setTimeout(() => _triggerParallelGenerate(count, parentId), 150);
}

/** Attach cc_model/effort/permission to a WS message from current UI state */
function _attachCCSettings(msg) {
    const conv = State.currentConv;
    if (!conv || (conv.mode !== 'claude' && conv.mode !== 'local')) return;
    const modelSel = document.getElementById('cc-model-inline');
    const effortSel = document.getElementById('cc-effort-inline');
    const permSel = document.getElementById('cc-permission-mode-inline');
    if (modelSel && modelSel.value) msg.cc_model = modelSel.value;
    if (effortSel && effortSel.value) msg.cc_effort = effortSel.value;
    if (permSel && permSel.value) msg.cc_permission_mode = permSel.value;
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
    const msgModel = msg.cc_model_used || State.currentConv?.cc_model || '';
    const isGemini = isClaudeMode && msgModel.startsWith('gemini');
    const isLocalMode = State.currentConv && State.currentConv.mode === 'local';
    const roleLabel = msg.role === 'system' ? 'System'
        : msg.role === 'user' ? 'You'
        : isGemini ? 'Gemini'
        : isClaudeMode ? 'Claude'
        : isLocalMode ? (State.currentConv.local_model || 'Local')
        : getCharacterName();
    // Local model tag to show alongside Claude label
    const localModelTag = isLocalMode ? `<span class="local-model-tag">({${escapeHtml(State.currentConv.local_model || 'local')} model})</span>` : '';
    const branchLabelFull = State.branchNames?.[msg.id] || '';
    // Middle-truncate long branch labels so they don't push action buttons off
    // the row (matches tree nodes + breadcrumb).
    const branchLabel = branchLabelFull.length > 14
        ? branchLabelFull.slice(0, Math.ceil(14 / 2) - 1) + '…' + branchLabelFull.slice(-Math.floor(14 / 2))
        : branchLabelFull;

    const isBm = State.bookmarks?.some(b => b.message_id === msg.id);
    const bmBtn = `<button onclick="toggleChatBookmark(${msg.id})" title="${isBm ? 'Remove bookmark' : 'Bookmark'}" class="chat-bookmark-btn${isBm ? ' active' : ''}">${isBm ? '⏣' : '⬡'}</button>`;
    let actionsHtml = '';
    if (msg.role === 'system') {
        // No actions for system messages (compact markers, etc.)
        actionsHtml = '';
    } else if (msg.role === 'assistant') {
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
    } else if (msg.role === 'system' && typeof msg.content === 'string' && msg.content.trimStart().startsWith('[CC context compactified')) {
        contentHtml = renderCompactMarker(msg.content, msg.id);
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
            let descMap = {};
            if (msg.image_alt) {
                try { descMap = JSON.parse(msg.image_alt) || {}; } catch { descMap = {}; }
            }
            imgHtml = '<div class="message-images">' +
                paths.map(p => {
                    const filename = p.split(/[\\/]/).pop();
                    const ext = '.' + filename.split('.').pop().toLowerCase();
                    if (imageExts.includes(ext)) {
                        const desc = descMap[filename];
                        const descHtml = desc
                            ? `<figcaption class="image-description" title="From vision model">${escapeHtml(desc)}</figcaption>`
                            : '';
                        return `<figure class="message-image-figure" data-img-name="${escapeHtml(filename)}"><img class="message-image" src="/uploads/${escapeHtml(filename)}" alt="Attached image">${descHtml}</figure>`;
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
            (localModelTag ? `<span class="local-model-label">${localModelTag}</span>` : '') + (branchLabel ? '<span class="message-branch-label" title="' + escapeHtml(branchLabelFull) + ' — click to copy branch path">' + escapeHtml(branchLabel) + '</span>' : '') +
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
        const renderMsgs = State.messages;
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

function renderCompactMarker(content, msgId) {
    // Marker body looks like:
    //   [CC context compactified (handoff → <target>) — N tokens before]
    //   \n\n---\nPreviously:\n<summary>
    // Header is the first line. Everything after the "Previously:" label (or
    // an empty fallback until the summary is patched in) goes in a default-
    // collapsed block.
    const text = (content || '').trim();
    const firstNl = text.indexOf('\n');
    const header = firstNl === -1 ? text : text.slice(0, firstNl);
    let summary = '';
    const marker = text.indexOf('Previously:');
    if (marker !== -1) {
        summary = text.slice(marker + 'Previously:'.length).trim();
    } else if (firstNl !== -1) {
        const rest = text.slice(firstNl + 1).replace(/^[-\s]+/, '').trim();
        if (rest) summary = rest;
    }
    const summaryHtml = summary ? formatContent(summary) : '<em class="compact-summary-pending">Summary pending…</em>';
    return (
        '<div class="compact-marker" data-msg-id="' + msgId + '">' +
            '<div class="compact-marker-header">' +
                '<span class="compact-marker-text">' + escapeHtml(header) + '</span>' +
            '</div>' +
            '<div class="compact-marker-previously">' +
                '<button class="compact-marker-toggle" onclick="this.parentElement.classList.toggle(\'expanded\')">' +
                    '<span class="compact-marker-arrow">▸</span> Previously' +
                '</button>' +
                '<div class="compact-marker-summary">' + summaryHtml + '</div>' +
            '</div>' +
        '</div>'
    );
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
let _lastSnapshotRequestAt = 0;

function _requestSnapshotIfStreaming() {
    // Throttle: at most one request every 750ms — server reply ('generation_active')
    // takes one round-trip and triggers _reconstructFromSnapshot which rebuilds the div.
    const now = Date.now();
    if (now - _lastSnapshotRequestAt < 750) return;
    if (!State.ws || State.ws.readyState !== WebSocket.OPEN) return;
    _lastSnapshotRequestAt = now;
    try {
        State.ws.send(JSON.stringify({ action: 'request_snapshot' }));
    } catch {}
}

let _genTimerInterval = null;
let _loomAnimInterval = null;
const _loomFrames = [
    "╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴",
    "╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴",
    "╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴",
    "╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴",
    "─╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌",
    "─╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌",
    "─╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌",
    "◆╼╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌",
    "╾◆╼╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌",
    "═╾◆╼╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌",
    "══╾◆╼╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌",
    "═══╾◆╼╌╌╌╌╌╌╌╌╌╌╌╌╌╌",
    "════╾◆╼╌╌╌╌╌╌╌╌╌╌╌╌╌",
    "═════╾◆╼╌╌╌╌╌╌╌╌╌╌╌╌",
    "══════╾◆╼╌╌╌╌╌╌╌╌╌╌╌",
    "═══════╾◆╼╌╌╌╌╌╌╌╌╌╌",
    "════════╾◆╼╌╌╌╌╌╌╌╌╌",
    "═════════╾◆╼╌╌╌╌╌╌╌╌",
    "══════════╾◆╼╌╌╌╌╌╌╌",
    "═══════════╾◆╼╌╌╌╌╌╌",
    "════════════╾◆╼╌╌╌╌╌",
    "═════════════╾◆╼╌╌╌╌",
    "══════════════╾◆╼╌╌╌",
    "═══════════════╾◆╼╌╌",
    "════════════════╾◆╼╌",
    "═════════════════╾◆╼",
    "══════════════════╾◆",
    "════════════════════",
    "════════════════════",
    "════════════════════",
    "══════════✂═════════",
    "══════════✂═════════",
    "══════════✂═════════",
    "≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼",
    "∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈",
    "≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼",
    "∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈",
    "≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼",
    "∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈",
    "────────────────────",
    "╌──────────────────╌",
    "╌╌────────────────╌╌",
    "╌╌╌──────────────╌╌╌",
    "╌╌╌╌────────────╌╌╌╌",
    "╌╌╌╌╌──────────╌╌╌╌╌",
    "╌╌╌╌╌╌────────╌╌╌╌╌╌",
    "╌╌╌╌╌╌╌──────╌╌╌╌╌╌╌",
    "╌╌╌╌╌╌╌╌────╌╌╌╌╌╌╌╌",
    "╌╌╌╌╌╌╌╌╌──╌╌╌╌╌╌╌╌╌",
    "╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌",
    "╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴",
    "╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴",
    "╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴",
];

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
        const tokEl = streamingDiv.querySelector('.gen-token-info');
        if (tokEl && _streamTokenCount > 0 && !tokEl.dataset.hasUsage) {
            tokEl.textContent = '↓' + _fmtTok(_streamTokenCount) + ' · ';
        }
    }, 1000);
    // Start loom animation
    if (_loomAnimInterval) clearInterval(_loomAnimInterval);
    let frame = 0;
    _loomAnimInterval = setInterval(() => {
        if (!streamingDiv) return;
        const el = streamingDiv.querySelector('.loom-anim');
        if (el) el.textContent = _loomFrames[frame];
        frame = (frame + 1) % _loomFrames.length;
    }, 120);
}

function _stopGenTimer() {
    if (_genTimerInterval) { clearInterval(_genTimerInterval); _genTimerInterval = null; }
    if (_loomAnimInterval) { clearInterval(_loomAnimInterval); _loomAnimInterval = null; }
}

function appendStreamingMessage() {
    const container = document.getElementById('messages');
    streamingDiv = document.createElement('div');
    streamingDiv.className = 'message assistant streaming';
    const isClaudeMode = State.currentConv && State.currentConv.mode === 'claude';
    const isGemini = isClaudeMode && State.currentConv.cc_model && State.currentConv.cc_model.startsWith('gemini');
    const isLocalMode = State.currentConv && State.currentConv.mode === 'local';
    const label = isGemini ? 'Gemini'
        : isClaudeMode ? 'Claude'
        : isLocalMode ? (State.currentConv.local_model || 'Local')
        : getCharacterName();
    streamingDiv.innerHTML = '<div class="message-header">' +
        '<span class="message-role">' + escapeHtml(label) + '</span>' +
        '</div>' +
        '<div class="message-content">' +
            '<span class="stream-waiting">' +
                '<span class="stream-waiting-anim"></span>' +
                '<span class="stream-waiting-text">waiting for first token...</span>' +
            '</span>' +
        '</div>' +
        '<div class="stream-thinking-footer">' +
        '<button onclick="cancelGeneration()" title="Cancel generation" class="cancel-draft-btn">&#x2298;</button>' +
        '<span class="loom-anim"></span><span class="looming-text"> Looming...</span>' +
        '<span class="gen-stats"><span class="gen-token-info"></span><span class="gen-timer">0:00</span></span>' +
        '</div>';
    container.appendChild(streamingDiv);
    _startGenTimer();
    _startStreamWaitingTicker();
    scrollToBottom();
}

let _streamWaitingTicker = null;
let _streamWaitingFrame = 0;
const _streamWaitingFrames = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];
function _startStreamWaitingTicker() {
    if (_streamWaitingTicker) clearInterval(_streamWaitingTicker);
    _streamWaitingFrame = 0;
    const start = Date.now();
    _streamWaitingTicker = setInterval(() => {
        if (!streamingDiv) { _stopStreamWaitingTicker(); return; }
        const animEl = streamingDiv.querySelector('.stream-waiting-anim');
        const textEl = streamingDiv.querySelector('.stream-waiting-text');
        if (!animEl || !textEl) { _stopStreamWaitingTicker(); return; }
        animEl.textContent = _streamWaitingFrames[_streamWaitingFrame];
        _streamWaitingFrame = (_streamWaitingFrame + 1) % _streamWaitingFrames.length;
        const secs = Math.floor((Date.now() - start) / 1000);
        if (secs > 10) {
            textEl.textContent = `still working… (${secs}s — large context can take 30–90s before first token)`;
        } else if (secs > 4) {
            textEl.textContent = `waiting for first token… (${secs}s)`;
        }
    }, 80);
}
function _stopStreamWaitingTicker() {
    if (_streamWaitingTicker) { clearInterval(_streamWaitingTicker); _streamWaitingTicker = null; }
}
function _removeStreamWaiting() {
    _stopStreamWaitingTicker();
    if (!streamingDiv) return;
    const el = streamingDiv.querySelector('.stream-waiting');
    if (el) el.remove();
}

let _streamBuffer = '';
let _streamFlushTimer = null;

function appendStreamChunk(content) {
    // If the streamingDiv got detached (e.g. by a mid-stream renderMessages
    // that happens when loadMessages or reconstruct races with chunks),
    // drop the stale ref and ask the server for a snapshot so we can rebuild
    // without forcing the user to refresh the page.
    if (streamingDiv && !streamingDiv.isConnected) {
        streamingDiv = null;
    }
    if (!streamingDiv) {
        // Drop this chunk (server snapshot already contains it) and ask for
        // a fresh snapshot so the reconstruction path can rebuild the UI.
        if (State.isStreaming && State._streamIsOurBranch !== false) {
            _requestSnapshotIfStreaming();
        }
        _streamBuffer = '';
        return;
    }
    _streamBuffer += content;
    // Throttle DOM updates to max every 50ms
    if (!_streamFlushTimer) {
        _streamFlushTimer = setTimeout(_flushStreamBuffer, 50);
    }
}

// Throttle scroll during streaming so rapid chunk flushes don't thrash layout.
let _lastStreamScrollAt = 0;
function _scrollDuringStream() {
    const now = Date.now();
    if (now - _lastStreamScrollAt < 250) return;
    _lastStreamScrollAt = now;
    scrollToBottom();
}

function _flushStreamBuffer() {
    _streamFlushTimer = null;
    if (streamingDiv && !streamingDiv.isConnected) {
        streamingDiv = null;
        if (State.isStreaming && State._streamIsOurBranch !== false) {
            _requestSnapshotIfStreaming();
        }
        _streamBuffer = '';
        return;
    }
    if (!streamingDiv || !_streamBuffer) return;
    _removeStreamWaiting();
    const contentEl = streamingDiv.querySelector('.message-content');
    // Find or create a text span to stream into (keeps text separate from tool blocks)
    let textSpan = contentEl.querySelector('.streaming-text:last-of-type');
    // If last child is a tool/thinking block, start a new text span after it
    const lastChild = contentEl.lastElementChild;
    if (!textSpan || (lastChild && !lastChild.classList.contains('streaming-text'))) {
        textSpan = document.createElement('span');
        textSpan.className = 'streaming-text';
        // Cursor lives as a sibling of the text, so we can append text nodes
        // directly without touching cursor markup on every flush.
        contentEl.appendChild(textSpan);
        // Ensure a single cursor span exists on the contentEl as last child
        if (!contentEl.querySelector('.typing-cursor')) {
            const cur = document.createElement('span');
            cur.className = 'typing-cursor';
            contentEl.appendChild(cur);
        }
    }
    // Append-only: just tack on a text node with the new delta. O(1) per flush
    // regardless of total message size. We accept raw markdown chars visible
    // during the stream (no `**bold**` formatting until finalize) in exchange
    // for flushes that don't re-parse the entire message every 50ms.
    const delta = _streamBuffer;
    _streamBuffer = '';
    const prev = textSpan.dataset.rawContent || '';
    textSpan.dataset.rawContent = prev + delta;
    textSpan.appendChild(document.createTextNode(delta));
    // Keep cursor at the end
    const cursor = contentEl.querySelector('.typing-cursor');
    if (cursor && cursor !== contentEl.lastChild) contentEl.appendChild(cursor);
    _scrollDuringStream();
}

function finalizeStreamingMessage(msg, cost) {
    if (!streamingDiv) return;
    _stopGenTimer();
    _stopStreamWaitingTicker();

    // Cancel any pending incremental flush — we're about to do a full markdown
    // render in one pass, so a trailing setTimeout would just waste work.
    if (_streamFlushTimer) {
        clearTimeout(_streamFlushTimer);
        _streamFlushTimer = null;
    }
    _streamBuffer = '';

    // Replace the streaming div (which holds append-only raw text) with a
    // fully rendered message element. This is the ONE markdown parse pass per
    // turn — O(N) instead of O(N²) from per-chunk reparsing.
    State.messages.push(msg);
    const newEl = createMessageElement(msg, cost);
    streamingDiv.replaceWith(newEl);
    streamingDiv = null;
    scrollToBottom();
}

function removeStreamingMessage() {
    _stopGenTimer();
    _stopStreamWaitingTicker();
    if (streamingDiv) {
        streamingDiv.remove();
        streamingDiv = null;
    }
}

// ── Claude Code: Tool + Thinking Blocks ──

function appendToolBlock(name, toolId, isOoda) {
    if (!streamingDiv) return;
    _removeStreamWaiting();
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
    _removeStreamWaiting();
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
    // Auto-grow as the user types/pastes — clone-measure to avoid flex flicker.
    const _resizeEditTextarea = () => {
        const clone = textarea.cloneNode(true);
        clone.style.position = 'absolute';
        clone.style.visibility = 'hidden';
        clone.style.height = 'auto';
        clone.style.width = textarea.offsetWidth + 'px';
        textarea.parentNode.appendChild(clone);
        textarea.style.height = Math.min(clone.scrollHeight, 600) + 'px';
        clone.remove();
    };
    textarea.addEventListener('input', _resizeEditTextarea);
    // Defer once so the textarea is laid out before we measure.
    requestAnimationFrame(_resizeEditTextarea);

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

    // Describe focus input for image descriptions (only shown when images attached)
    const describeRow = document.createElement('div');
    describeRow.className = 'edit-describe-context hidden';
    describeRow.innerHTML = `
        <label title="Extra focus for vision model image description — not sent to chat model">Describe focus</label>
        <textarea class="edit-describe-input" rows="2" placeholder="e.g. focus on body language and composition" autocomplete="off" spellcheck="false">${escapeHtml(msg.describe_context || '')}</textarea>
    `;
    filePreview.after(describeRow);

    // Show/hide describe row based on whether images are attached
    function updateDescribeVisibility() {
        const hasImg = editFiles.some(f => {
            const ext = f.path.split('.').pop().toLowerCase();
            return ['jpg','jpeg','png','gif','webp','bmp','svg'].includes(ext);
        });
        describeRow.classList.toggle('hidden', !hasImg);
    }
    updateDescribeVisibility();
    // Patch renderEditFiles to also update describe visibility
    const _origRenderEditFiles = renderEditFiles;
    renderEditFiles = function() { _origRenderEditFiles(); updateDescribeVisibility(); };

    const isWeave = State.currentConv && State.currentConv.mode === 'weave';
    const btnRow = document.createElement('div');
    btnRow.className = 'edit-message-actions';
    const pillHtml = isWeave ? `<div class="branch-count-pill pill-compact" title="Branches to generate (click to cycle)"><span class="branch-count-icon">⑂</span><span class="branch-count-value">${State.branchCount}</span></div>` : '';
    btnRow.innerHTML = `
        <label class="btn-small edit-attach" title="Attach file(s) — you can also paste or drop files onto the textarea">
            <input type="file" class="edit-file-input" multiple hidden>
            📎
        </label>
        <button class="btn-small edit-save" title="Send as new branch (Shift+Enter)">Send as new branch</button>
        ${pillHtml}
        <button class="btn-small edit-cancel">Cancel</button>
    `;
    if (isWeave) _setupBranchPillClick(btnRow.querySelector('.branch-count-pill'));
    describeRow.after(btnRow);

    // Shared uploader: takes a File, uploads it, appends to editFiles, refreshes chips.
    async function _uploadEditFile(file) {
        if (!file) return;
        try {
            const result = await API.upload(file);
            editFiles.push({ path: result.path, original: false });
            renderEditFiles();
        } catch { showToast('File upload failed', 'error'); }
    }
    async function _uploadEditFiles(fileList) {
        const files = Array.from(fileList || []);
        if (!files.length) return;
        const editAttachLabel = btnRow.querySelector('.edit-attach');
        const orig = editAttachLabel.textContent;
        editAttachLabel.classList.add('uploading');
        editAttachLabel.textContent = '⏳';
        for (const f of files) await _uploadEditFile(f);
        editAttachLabel.textContent = orig || '📎';
        editAttachLabel.classList.remove('uploading');
    }

    // File picker (now accepts multiple)
    const editFileInput = btnRow.querySelector('.edit-file-input');
    editFileInput.addEventListener('change', async (e) => {
        await _uploadEditFiles(e.target.files);
        e.target.value = '';
    });

    // Paste clipboard images / files directly into the textarea
    textarea.addEventListener('paste', async (e) => {
        const items = e.clipboardData && e.clipboardData.items;
        if (!items) return;
        const files = [];
        for (const item of items) {
            // Image from clipboard or a regular File
            if (item.kind === 'file') {
                const f = item.getAsFile();
                if (f) files.push(f);
            }
        }
        if (files.length > 0) {
            e.preventDefault();
            await _uploadEditFiles(files);
        }
    });

    // Drop files onto the textarea
    textarea.addEventListener('dragover', (e) => {
        if (e.dataTransfer && e.dataTransfer.types && e.dataTransfer.types.indexOf('Files') !== -1) {
            e.preventDefault();
            textarea.classList.add('drag-over');
        }
    });
    textarea.addEventListener('dragleave', () => textarea.classList.remove('drag-over'));
    textarea.addEventListener('drop', async (e) => {
        if (!(e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length)) return;
        e.preventDefault();
        textarea.classList.remove('drag-over');
        await _uploadEditFiles(e.dataTransfer.files);
    });

    // Shift+Enter = send (matches main #user-input). Plain Enter = newline.
    textarea.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && e.shiftKey) {
            e.preventDefault();
            btnRow.querySelector('.edit-save').click();
        }
    });

    btnRow.querySelector('.edit-cancel').addEventListener('click', () => {
        const newContent = document.createElement('div');
        newContent.className = 'message-content';
        newContent.innerHTML = formatContent(msg.content);
        textarea.replaceWith(newContent);
        filePreview.remove();
        describeRow.remove();
        btnRow.remove();
    });

    btnRow.querySelector('.edit-save').addEventListener('click', async () => {
        const newText = textarea.value.trim();
        if (!newText) return;

        try {
            const parentId = msg.parent_id || null;
            const allPaths = editFiles.map(f => f.path);

            const editDescInput = describeRow.querySelector('.edit-describe-input');
            const editDescCtx = editDescInput ? editDescInput.value.trim() : '';
            const editMsgData = {
                role: 'user',
                content: newText,
                parent_id: parentId,
                image_path: allPaths.length > 0 ? allPaths : null,
            };
            if (editDescCtx) editMsgData.describe_context = editDescCtx;
            const newMsg = await API.post(`/api/conversations/${State.currentConvId}/messages`, editMsgData);

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

// Queued permission prompts that arrived while _reconstructFromSnapshot
// was in flight. Rendering them immediately would attach them to a
// streamingDiv that's about to be destroyed and replaced.
function _drainPendingPermPrompts() {
    const queue = State._pendingPermPrompts;
    if (!queue || !queue.length) return;
    State._pendingPermPrompts = [];
    for (const data of queue) {
        showPermissionPrompt(data);
    }
}

function showPermissionPrompt(data) {
    // Detect and drop stale streamingDiv ref before attaching a prompt to it.
    if (streamingDiv && !streamingDiv.isConnected) {
        streamingDiv = null;
    }
    // Dedup: reconstruction loop + queued-during-reconstruct replay both fire
    // showPermissionPrompt for the same request_id. Skip if already rendered
    // anywhere in the document (stream div, draft message, etc.).
    if (data.request_id && document.querySelector(`.permission-prompt[data-request-id="${data.request_id}"]`)) {
        return;
    }
    if (!streamingDiv) appendStreamingMessage();
    const contentEl = streamingDiv.querySelector('.message-content');

    const prompt = document.createElement('div');
    prompt.dataset.requestId = data.request_id;

    const toolName = escapeHtml(data.tool_name || 'Unknown');
    const isPlan = data.tool_name === 'ExitPlanMode' || data.tool_name === 'exit_plan_mode';

    if (isPlan) {
        prompt.className = 'permission-prompt plan-prompt';
        let planInput = data.tool_input || {};
        if (typeof planInput === 'string') try { planInput = JSON.parse(planInput); } catch {}
        const planRaw = planInput.plan || planInput.planFilePath || data.input_summary || '';
        const planFileName = planInput.planFilePath ? planInput.planFilePath.split(/[/\\]/).pop() : '';
        // Render plan as markdown if marked is available, otherwise fall back to escaped pre
        const planHtml = (typeof marked !== 'undefined' && planRaw.length > 10)
            ? DOMPurify.sanitize(marked.parse(planRaw))
            : '<pre>' + escapeHtml(planRaw) + '</pre>';
        prompt.innerHTML = '<div class="permission-header">' +
            '<span class="permission-icon">&#x1F9F5;</span>' +
            '<span class="permission-title">Plan Ready for Review</span>' +
            (planFileName ? '<span class="plan-filename">' + escapeHtml(planFileName) + '</span>' : '') +
            '</div>' +
            (planRaw ? '<div class="permission-body"><div class="permission-input plan-content">' + planHtml + '</div></div>' : '') +
            '<div class="permission-actions">' +
            '<button class="btn-permission allow plan-approve" data-perm-action="allow" data-request-id="' + data.request_id + '">Approve Plan</button>' +
            '<button class="btn-permission deny plan-revise" data-perm-action="deny" data-request-id="' + data.request_id + '">Revise</button>' +
            '</div>';
    } else {
        prompt.className = 'permission-prompt';
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
    }

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

            // If this was a plan approval, flip dropdown back to Act
            if (prompt.classList.contains('plan-prompt') && allow) {
                const permSel = document.getElementById('cc-permission-mode-inline');
                if (permSel) permSel.value = 'default';
            }
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

        // If this was a plan approval, flip dropdown back to Act
        if (prompt.classList.contains('plan-prompt') && allowed) {
            const permSel = document.getElementById('cc-permission-mode-inline');
            if (permSel) permSel.value = 'default';
        }
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

// ── Canvas ──

function refreshCanvasIframe() {
    if (!State.currentConvId) return;
    const src = `/api/canvas/${State.currentConvId}/index.html?t=${Date.now()}`;
    // Refresh whichever canvas iframe is visible
    const fullview = document.getElementById('canvas-fullview-iframe');
    if (fullview && !document.getElementById('canvas-fullview')?.classList.contains('hidden')) {
        fullview.src = src;
    }
    // Also refresh the tree node thumbnail
    const thumb = document.querySelector('.canvas-node-iframe');
    if (thumb) thumb.src = src;
}

function openCanvasFullview() {
    const fullview = document.getElementById('canvas-fullview');
    if (!fullview || !State.currentConvId) return;
    fullview.classList.remove('hidden');
    const iframe = document.getElementById('canvas-fullview-iframe');
    iframe.src = `/api/canvas/${State.currentConvId}/index.html?t=${Date.now()}`;
    // Show tree button so user can navigate back
    const treeBtn = document.getElementById('btn-to-tree');
    if (treeBtn) treeBtn.classList.remove('hidden');
    // Hide canvas toggle — no purpose inside the canvas
    const canvasBtn = document.getElementById('btn-canvas-toggle');
    if (canvasBtn) canvasBtn.classList.add('hidden');
}

function closeCanvasFullview() {
    const fullview = document.getElementById('canvas-fullview');
    if (fullview) fullview.classList.add('hidden');
    // Hide tree button again if we're in tree view
    if (State.currentView === 'tree') {
        const treeBtn = document.getElementById('btn-to-tree');
        if (treeBtn) treeBtn.classList.add('hidden');
    }
    // Restore canvas toggle button
    const canvasBtn = document.getElementById('btn-canvas-toggle');
    if (canvasBtn && State.currentConv?.mode === 'claude') canvasBtn.classList.remove('hidden');
}

async function toggleCanvas() {
    if (!State.currentConvId || !State.currentConv) return;
    const isEnabled = State.currentConv.canvas_enabled;
    const newState = !isEnabled;
    try {
        await API.post(`/api/conversations/${State.currentConvId}/canvas`, { enabled: newState });
        State.currentConv.canvas_enabled = newState ? 1 : 0;
        const btn = document.getElementById('btn-canvas-toggle');
        if (btn) btn.classList.toggle('active', newState);
        // Re-render tree without resetting camera position
        if (typeof TREE !== 'undefined') TREE._skipCenter = true;
        if (typeof renderTree === 'function') renderTree();
    } catch (e) {
        console.error('Canvas toggle failed:', e);
    }
}

function updateCanvasVisibility() {
    const btn = document.getElementById('btn-canvas-toggle');
    if (!btn) return;
    const hasConv = !!State.currentConv;
    const showBtn = hasConv && State.currentView === 'tree';
    btn.classList.toggle('hidden', !showBtn);
    if (btn) btn.classList.toggle('active', !!(hasConv && State.currentConv?.canvas_enabled));
    // Show/hide canvas focus button in tree toolbar
    const focusBtn = document.getElementById('btn-canvas-focus');
    if (focusBtn) focusBtn.classList.toggle('hidden', !(showBtn && State.currentConv?.canvas_enabled));
    // Close fullview if canvas was disabled
    if (!State.currentConv?.canvas_enabled) closeCanvasFullview();
}

document.getElementById('btn-canvas-toggle')?.addEventListener('click', toggleCanvas);

// ── Canvas postMessage Bridge ──
// Allows canvas iframes to trigger Loom actions via window.parent.postMessage
window.addEventListener('message', async (e) => {
    // Only accept messages from our own canvas iframes
    if (!e.data || e.data.source !== 'loom-canvas') return;
    const convId = State.currentConvId;
    if (!convId) return;

    if (e.data.type === 'send-message') {
        // Create user message and trigger generation
        try {
            const payload = { role: 'user', content: e.data.content || '' };
            if (e.data.image_paths) payload.image_path = e.data.image_paths;
            if (e.data.parent_id !== undefined) payload.parent_id = e.data.parent_id;
            const resp = await fetch(`/api/conversations/${convId}/messages`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const msg = await resp.json();
            if (msg.id) {
                _triggerParallelGenerate(1, msg.id);
                // Notify canvas of the message ID
                e.source?.postMessage({ source: 'loom-host', type: 'message-sent', id: msg.id }, '*');
            }
        } catch (err) {
            e.source?.postMessage({ source: 'loom-host', type: 'error', error: err.message }, '*');
        }
    } else if (e.data.type === 'get-conv-id') {
        e.source?.postMessage({ source: 'loom-host', type: 'conv-id', convId }, '*');
    }
});

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
