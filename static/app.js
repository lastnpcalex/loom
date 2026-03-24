/**
 * Loom — RP Harness
 * State management, API client, view switching, initialization
 */

const API = {
    async get(url) {
        const res = await fetch(url);
        if (!res.ok) throw new Error(`GET ${url}: ${res.status}`);
        return res.json();
    },
    async post(url, data = {}) {
        const res = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        });
        if (!res.ok) throw new Error(`POST ${url}: ${res.status}`);
        return res.json();
    },
    async del(url) {
        const res = await fetch(url, { method: 'DELETE' });
        if (!res.ok) throw new Error(`DELETE ${url}: ${res.status}`);
        return res.json();
    },
    async put(url, data) {
        const res = await fetch(url, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        });
        if (!res.ok) throw new Error(`PUT ${url}: ${res.status}`);
        return res.json();
    },
    async upload(file) {
        const form = new FormData();
        form.append('file', file);
        const res = await fetch('/api/upload', { method: 'POST', body: form });
        if (!res.ok) throw new Error('Upload failed');
        return res.json();
    },
};

// ── Application State ──
const State = {
    currentView: 'home',  // 'home', 'tree', 'chat'
    currentConvId: null,
    currentConv: null,
    conversations: [],
    characters: [],
    personas: [],
    lore: [],
    selectedCharacterId: null,
    messages: [],
    treeData: [],
    ws: null,
    isStreaming: false,
    pendingImages: [],  // [{path, url}, ...] — max 5
    config: {},
    convFilter: 'all',
    convFolderCollapsed: {},
};

// ── View Switching ──
function switchView(view) {
    State.currentView = view;
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    document.getElementById(`view-${view}`).classList.add('active');

    // Update header
    const sep = document.getElementById('header-separator');
    const title = document.getElementById('conv-title');
    const backBtn = document.getElementById('btn-back-to-tree');
    const contextInfo = document.getElementById('context-info');

    if (view === 'home') {
        sep.classList.add('hidden');
        title.classList.add('hidden');
        backBtn.classList.add('hidden');
        contextInfo.classList.add('hidden');
        // Close WebSocket when leaving a conversation
        if (State.ws) { State.ws.close(); State.ws = null; }
    } else if (view === 'tree') {
        sep.classList.remove('hidden');
        title.classList.remove('hidden');
        title.textContent = State.currentConv?.title || '—';
        backBtn.classList.add('hidden');
        contextInfo.classList.remove('hidden');
    } else if (view === 'chat') {
        sep.classList.remove('hidden');
        title.classList.remove('hidden');
        title.textContent = State.currentConv?.title || '—';
        backBtn.classList.remove('hidden');
        contextInfo.classList.remove('hidden');
        // Refresh messages when switching back to chat (picks up responses
        // that completed while on tree/home view)
        if (State.currentConvId && !State.isStreaming) {
            loadMessages(State.currentConvId);
        }
    }
}

// ── Toast Notifications ──
function showToast(message, type = 'info') {
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 3000);
}

// ── Modal Helpers ──
function openModal(id) { document.getElementById(id).classList.remove('hidden'); }
function closeModal(id) { document.getElementById(id).classList.add('hidden'); }

// ── Init ──
async function init() {
    try {
        const [chars, convs, cfg, personas, lore] = await Promise.all([
            API.get('/api/characters'),
            API.get('/api/conversations'),
            API.get('/api/config'),
            API.get('/api/personas'),
            API.get('/api/lore'),
        ]);
        State.characters = chars;
        State.conversations = convs;
        State.config = cfg;
        State.personas = personas;
        State.lore = lore;
    } catch (e) {
        showToast('Failed to connect to server', 'error');
        console.error(e);
    }

    renderConversationList();
    renderHomeCharacters();
    renderHomePersonas();
    renderHomeLore();
    setupEventListeners();
    initInlineCCControls();
    initPasteHandler();
    switchView('home');
    checkHealth();
}

async function checkHealth() {
    try {
        const health = await API.get('/api/health');
        if (health.status === 'error') {
            showToast(`Ollama not reachable: ${health.error}`, 'error');
        } else if (!health.model_available) {
            showToast(`Model "${health.target_model}" not found. Available: ${health.models.join(', ')}`, 'error');
        }
    } catch {
        showToast('Cannot reach server', 'error');
    }
}

// ── Render Conversation List ──
function renderConversationList() {
    const list = document.getElementById('conv-list');
    const empty = document.getElementById('home-empty');
    list.innerHTML = '';

    // Filter by mode
    let convs = State.conversations;
    if (State.convFilter === 'weave') {
        convs = convs.filter(c => c.mode === 'weave' || !c.mode);
    } else if (State.convFilter === 'local') {
        convs = convs.filter(c => c.mode === 'local');
    } else if (State.convFilter === 'claude') {
        convs = convs.filter(c => c.mode === 'claude');
    }

    if (convs.length === 0) {
        empty.style.display = '';
        return;
    }
    empty.style.display = 'none';

    // Group by folder
    const folders = {};  // folder name -> [conv, ...]
    const unfiled = [];
    for (const conv of convs) {
        const folder = conv.folder || '';
        if (folder) {
            if (!folders[folder]) folders[folder] = [];
            folders[folder].push(conv);
        } else {
            unfiled.push(conv);
        }
    }

    // Render folder groups first
    const folderNames = Object.keys(folders).sort();
    for (const folderName of folderNames) {
        const group = document.createElement('div');
        group.className = 'conv-folder-group';

        const collapsed = !!State.convFolderCollapsed[folderName];
        const header = document.createElement('div');
        header.className = 'conv-folder-header';
        header.innerHTML = `
            <span class="conv-folder-arrow">${collapsed ? '▸' : '▾'}</span>
            <span class="conv-folder-name">${escapeHtml(folderName)}</span>
            <span class="conv-folder-count">(${folders[folderName].length})</span>
        `;
        header.addEventListener('click', () => {
            State.convFolderCollapsed[folderName] = !State.convFolderCollapsed[folderName];
            renderConversationList();
        });
        group.appendChild(header);

        if (!collapsed) {
            for (const conv of folders[folderName]) {
                group.appendChild(buildConvItem(conv));
            }
        }
        list.appendChild(group);
    }

    // Render unfiled conversations
    for (const conv of unfiled) {
        list.appendChild(buildConvItem(conv));
    }
}

function buildConvItem(conv) {
    const div = document.createElement('div');
    div.className = 'conv-item';

    const isCC = conv.mode === 'claude';
    const isLocal = conv.mode === 'local';
    const charName = isCC ? 'Claude Code'
        : isLocal ? (conv.local_model || 'Local')
        : conv.character_id
        ? (State.characters.find(c => c.id === conv.character_id)?.name || conv.character_id)
        : 'Freeform';
    const modeBadge = isCC ? '<span class="mode-badge">CC</span>'
        : isLocal ? '<span class="mode-badge">L</span>'
        : '<span class="mode-badge">W</span>';
    const starred = conv.starred ? 1 : 0;
    const starChar = starred ? '★' : '☆';
    const starClass = starred ? 'conv-star-btn active' : 'conv-star-btn';

    div.innerHTML = `
        <button class="${starClass}" title="Star">${starChar}</button>
        ${modeBadge}
        <span class="conv-title-text">${escapeHtml(conv.title)}</span>
        <span class="conv-meta">${escapeHtml(charName)}</span>
        <div class="conv-actions">
            <button class="char-action-btn conv-folder-btn" title="Move to folder"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg></button>
            <button class="char-action-btn conv-edit-btn" title="Rename">✎</button>
            <button class="char-action-btn conv-export-btn" title="Export">↓</button>
            <button class="char-action-btn char-delete-btn conv-delete-btn" title="Delete">✕</button>
        </div>
    `;

    // Star toggle
    div.querySelector('.conv-star-btn').addEventListener('click', async (e) => {
        e.stopPropagation();
        const newStarred = conv.starred ? 0 : 1;
        try {
            await API.put(`/api/conversations/${conv.id}`, { starred: newStarred });
            conv.starred = newStarred;
            // Re-sort: starred first, then by updated_at
            State.conversations.sort((a, b) => {
                if ((b.starred || 0) !== (a.starred || 0)) return (b.starred || 0) - (a.starred || 0);
                return (b.updated_at || 0) - (a.updated_at || 0);
            });
            renderConversationList();
        } catch { showToast('Failed to update star', 'error'); }
    });

    // Click title/meta to open
    div.querySelector('.conv-title-text').addEventListener('click', () => loadConversation(conv.id));
    div.querySelector('.conv-meta').addEventListener('click', () => loadConversation(conv.id));

    // Folder move button
    div.querySelector('.conv-folder-btn').addEventListener('click', (e) => {
        e.stopPropagation();
        showFolderDropdown(e.currentTarget, conv);
    });

    // Rename
    div.querySelector('.conv-edit-btn').addEventListener('click', (e) => {
        e.stopPropagation();
        const titleSpan = div.querySelector('.conv-title-text');
        const currentTitle = conv.title || '';
        const input = document.createElement('input');
        input.type = 'text';
        input.value = currentTitle;
        input.className = 'conv-title-input';
        titleSpan.replaceWith(input);
        input.focus();
        input.select();

        const save = async () => {
            const newTitle = input.value.trim() || currentTitle;
            const span = document.createElement('span');
            span.className = 'conv-title-text';
            span.textContent = newTitle;
            span.addEventListener('click', () => loadConversation(conv.id));
            input.replaceWith(span);
            if (newTitle !== currentTitle) {
                try {
                    const updated = await API.put(`/api/conversations/${conv.id}`, { title: newTitle });
                    conv.title = updated.title;
                    if (State.currentConv && State.currentConv.id === conv.id) {
                        State.currentConv.title = updated.title;
                    }
                } catch { showToast('Failed to rename', 'error'); }
            }
        };
        input.addEventListener('blur', save);
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') input.blur();
            if (e.key === 'Escape') { input.value = currentTitle; input.blur(); }
        });
    });

    // Export
    div.querySelector('.conv-export-btn').addEventListener('click', (e) => {
        e.stopPropagation();
        downloadFile(`/api/conversations/${conv.id}/export`, `${conv.title || 'conversation'}.json`);
    });

    // Delete
    div.querySelector('.conv-delete-btn').addEventListener('click', async (e) => {
        e.stopPropagation();
        if (confirm('Delete this conversation?')) {
            await API.del(`/api/conversations/${conv.id}`);
            State.conversations = State.conversations.filter(c => c.id !== conv.id);
            if (State.currentConvId === conv.id) {
                State.currentConvId = null;
                State.currentConv = null;
                State.messages = [];
                switchView('home');
            }
            renderConversationList();
        }
    });

    return div;
}

function showFolderDropdown(anchorBtn, conv) {
    // Remove any existing dropdown
    document.querySelectorAll('.conv-move-dropdown').forEach(d => d.remove());

    const dropdown = document.createElement('div');
    dropdown.className = 'conv-move-dropdown';

    // Collect existing folder names
    const existingFolders = [...new Set(
        State.conversations.map(c => c.folder).filter(f => f)
    )].sort();

    // "No folder" option
    let html = `<div class="conv-move-option" data-folder="">— No folder —</div>`;
    for (const f of existingFolders) {
        const active = conv.folder === f ? ' active' : '';
        html += `<div class="conv-move-option${active}" data-folder="${escapeHtml(f)}">${escapeHtml(f)}</div>`;
    }
    html += `<div class="conv-move-option conv-move-new">+ New folder...</div>`;
    dropdown.innerHTML = html;

    // Position relative to anchor
    anchorBtn.appendChild(dropdown);

    // Handle clicks
    dropdown.querySelectorAll('.conv-move-option:not(.conv-move-new)').forEach(opt => {
        opt.addEventListener('click', async (e) => {
            e.stopPropagation();
            const folder = opt.dataset.folder;
            try {
                await API.put(`/api/conversations/${conv.id}`, { folder });
                conv.folder = folder;
                renderConversationList();
            } catch { showToast('Failed to move', 'error'); }
            dropdown.remove();
        });
    });

    dropdown.querySelector('.conv-move-new').addEventListener('click', (e) => {
        e.stopPropagation();
        const name = prompt('Folder name:');
        if (name && name.trim()) {
            const folder = name.trim();
            API.put(`/api/conversations/${conv.id}`, { folder }).then(() => {
                conv.folder = folder;
                renderConversationList();
            }).catch(() => showToast('Failed to move', 'error'));
        }
        dropdown.remove();
    });

    // Close on outside click
    const closeDropdown = (e) => {
        if (!dropdown.contains(e.target)) {
            dropdown.remove();
            document.removeEventListener('click', closeDropdown);
        }
    };
    setTimeout(() => document.addEventListener('click', closeDropdown), 0);
}

// ── Load Conversation → Tree View ──
async function loadConversation(convId) {
    State.currentConvId = convId;

    const [conv, treeData] = await Promise.all([
        API.get(`/api/conversations/${convId}`),
        API.get(`/api/conversations/${convId}/tree`),
    ]);

    State.currentConv = conv;
    State.treeData = treeData;

    // Also load the active branch for chat
    const activeNodes = treeData.filter(n => n.is_active);
    if (activeNodes.length > 0) {
        const leafId = activeNodes[activeNodes.length - 1].id;
        State.messages = await API.get(`/api/conversations/${convId}/branch/${leafId}`);
    } else {
        State.messages = [];
    }

    connectWebSocket(convId);
    renderTree();
    renderMessages();
    updateInlineCCControls(conv);

    // If only a linear conversation (no forks), go straight to chat
    const hasForks = treeData.some(n => {
        const siblings = treeData.filter(s => s.parent_id === n.parent_id);
        return siblings.length > 1;
    });

    if (hasForks) {
        switchView('tree');
    } else {
        switchView('chat');
    }
}

// ── Create Conversation ──
async function createConversation() {
    const mode = document.querySelector('#mode-toggle .toggle-btn.active')?.dataset.value || 'weave';
    const title = document.getElementById('new-conv-title').value.trim() || 'New Conversation';

    if (mode === 'claude') {
        const projectDir = document.getElementById('project-dir').value.trim();
        if (!projectDir) {
            showToast('Working directory is required for Claude mode', 'error');
            return;
        }
        const ccModel = document.getElementById('cc-model').value;
        const ccEffort = document.getElementById('cc-effort').value;
        const conv = await API.post('/api/conversations', {
            title,
            mode: 'claude',
            project_dir: projectDir,
            cc_model: ccModel,
            cc_effort: ccEffort,
        });
        State.conversations.unshift(conv);
        closeModal('modal-new-conv');
        document.getElementById('new-conv-title').value = '';
        document.getElementById('project-dir').value = '';
        renderConversationList();
        await loadConversation(conv.id);
        switchView('chat');
        return;
    }

    if (mode === 'local') {
        const localModel = document.getElementById('local-model').value;
        if (!localModel) {
            showToast('Select an Ollama model', 'error');
            return;
        }
        const localProjectDir = document.getElementById('project-dir').value.trim();
        const conv = await API.post('/api/conversations', {
            title,
            mode: 'local',
            local_model: localModel,
            project_dir: localProjectDir || undefined,
        });
        State.conversations.unshift(conv);
        closeModal('modal-new-conv');
        document.getElementById('new-conv-title').value = '';
        renderConversationList();
        await loadConversation(conv.id);
        switchView('chat');
        return;
    }

    // Weave mode (unchanged)
    const charId = State.selectedCharacterId;
    const firstTurn = document.querySelector('#first-turn-toggle .toggle-btn.active')?.dataset.value || 'character';
    const customScene = document.getElementById('custom-scene').value.trim();
    const personaId = document.getElementById('persona-select').value || null;
    const styleNudge = 'Natural';

    const loreIds = [];
    document.querySelectorAll('#lore-checklist input[type="checkbox"]:checked').forEach(cb => {
        loreIds.push(cb.value);
    });

    const conv = await API.post('/api/conversations', {
        title,
        character_id: charId,
        persona_id: personaId,
        lore_ids: loreIds,
        style_nudge: styleNudge,
        first_turn: firstTurn,
        custom_scene: customScene || null,
    });

    State.conversations.unshift(conv);
    closeModal('modal-new-conv');
    document.getElementById('new-conv-title').value = '';
    document.getElementById('custom-scene').value = '';
    State.selectedCharacterId = null;

    await loadConversation(conv.id);
    // New conversation has no forks, go to chat
    switchView('chat');

    // Trigger generation if character goes first and no static greeting was used
    if (firstTurn === 'character' && charId) {
        const lastMsg = State.messages[State.messages.length - 1];
        if (!lastMsg || lastMsg.role !== 'assistant') {
            // WebSocket may still be connecting — wait for it
            showGenStatus('Generating first response...');
            const sendGenerate = () => {
                if (State.ws && State.ws.readyState === WebSocket.OPEN) {
                    State.ws.send(JSON.stringify({ action: 'generate' }));
                } else {
                    hideGenStatus();
                    showRetryBar('WebSocket not connected');
                }
            };
            if (State.ws && State.ws.readyState === WebSocket.OPEN) {
                sendGenerate();
            } else if (State.ws) {
                State.ws.addEventListener('open', sendGenerate, { once: true });
                // Safety timeout — if WS doesn't connect in 10s, show error
                setTimeout(() => {
                    if (State.ws && State.ws.readyState !== WebSocket.OPEN) {
                        hideGenStatus();
                        showRetryBar('Connection timed out — try again');
                    }
                }, 10000);
            }
        }
    }
}

// ── Open New Conversation Modal ──
function openNewConvModal() {
    renderCharacterGrid();
    renderPersonaSelect();
    renderLoreChecklist();
    document.querySelectorAll('#first-turn-toggle .toggle-btn').forEach(b => b.classList.remove('active'));
    document.querySelector('#first-turn-toggle .toggle-btn[data-value="character"]').classList.add('active');
    document.getElementById('custom-scene').value = '';
    // Reset mode toggle to Weave
    document.querySelectorAll('#mode-toggle .toggle-btn').forEach(b => b.classList.remove('active'));
    document.querySelector('#mode-toggle .toggle-btn[data-value="weave"]').classList.add('active');
    document.getElementById('project-dir-group').classList.add('hidden');
    document.getElementById('project-dir').value = '';
    document.getElementById('cc-model-group').classList.add('hidden');
    document.getElementById('cc-effort-group').classList.add('hidden');
    document.getElementById('cc-model').value = 'sonnet';
    document.getElementById('cc-effort').value = 'high';
    document.getElementById('local-model-group').classList.add('hidden');
    showWeaveFields(true);
    openModal('modal-new-conv');
}

async function loadDirBrowser(path) {
    const browser = document.getElementById('dir-browser');
    browser.classList.remove('hidden');
    browser.innerHTML = '<div class="dir-loading">Loading...</div>';

    try {
        const data = await API.get(`/api/browse-dirs?path=${encodeURIComponent(path)}`);
        let html = '';

        // Current path display (clickable to refresh)
        if (data.current) {
            html += `<div class="dir-current" data-dir-path="${escapeHtml(data.current)}" title="Click to refresh">${escapeHtml(data.current)}</div>`;
        }

        // Navigation
        html += '<div class="dir-list">';

        // Select current directory button (prominent)
        if (data.current) {
            html += `<div class="dir-entry dir-select" data-dir-select="${escapeHtml(data.current)}">&#x2714; Use this folder</div>`;
        }

        // Parent directory link
        if (data.parent !== null && data.parent !== undefined) {
            html += `<div class="dir-entry dir-parent" data-dir-path="${escapeHtml(data.parent)}"><svg class="dir-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"/></svg> ..</div>`;
        }

        // Subdirectories
        for (const dir of data.dirs) {
            html += `<div class="dir-entry" data-dir-path="${escapeHtml(dir.path)}"><svg class="dir-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"/><path d="M3 9h18"/></svg> ${escapeHtml(dir.name)}</div>`;
        }

        if (data.dirs.length === 0 && !data.error) {
            html += '<div class="dir-empty">No subdirectories</div>';
        }
        if (data.error) {
            html += `<div class="dir-error">${escapeHtml(data.error)}</div>`;
        }

        html += '</div>';
        browser.innerHTML = html;

        // Event delegation for directory entries
        browser.querySelectorAll('[data-dir-path]').forEach(el => {
            el.addEventListener('click', () => loadDirBrowser(el.dataset.dirPath));
        });
        browser.querySelectorAll('[data-dir-select]').forEach(el => {
            el.addEventListener('click', () => {
                document.getElementById('project-dir').value = el.dataset.dirSelect;
                browser.classList.add('hidden');
            });
        });
    } catch (err) {
        browser.innerHTML = `<div class="dir-error">Failed to browse: ${err.message}</div>`;
    }
}

// ── Inline CC Model/Effort Controls ──

function updateInlineCCControls(conv) {
    const controls = document.getElementById('cc-inline-controls');
    if (!controls) return;

    if (conv && (conv.mode === 'claude' || conv.mode === 'local')) {
        controls.classList.remove('hidden');
        const modelSel = document.getElementById('cc-model-inline');
        const effortSel = document.getElementById('cc-effort-inline');
        const permSel = document.getElementById('cc-permission-mode-inline');
        modelSel.value = conv.cc_model || 'sonnet';
        effortSel.value = conv.cc_effort || 'high';
        if (permSel) permSel.value = conv.cc_permission_mode || 'default';
        // Hide model/effort for local mode (model is set at creation)
        modelSel.style.display = conv.mode === 'local' ? 'none' : '';
        effortSel.style.display = conv.mode === 'local' ? 'none' : '';
    } else {
        controls.classList.add('hidden');
    }
}

function initInlineCCControls() {
    const modelSel = document.getElementById('cc-model-inline');
    const effortSel = document.getElementById('cc-effort-inline');
    if (!modelSel || !effortSel) return;

    async function saveCC(field, value) {
        if (!State.currentConvId || !State.currentConv) return;
        try {
            await API.put(`/api/conversations/${State.currentConvId}`, { [field]: value });
            State.currentConv[field] = value;
        } catch { showToast('Failed to update', 'error'); }
    }

    modelSel.addEventListener('change', () => saveCC('cc_model', modelSel.value));
    effortSel.addEventListener('change', () => saveCC('cc_effort', effortSel.value));

    const permSel = document.getElementById('cc-permission-mode-inline');
    if (permSel) permSel.addEventListener('change', () => saveCC('cc_permission_mode', permSel.value));
}

function showWeaveFields(show) {
    const weaveFields = ['character-grid', 'persona-select', 'lore-checklist',
                         'first-turn-toggle', 'custom-scene-group'];
    for (const id of weaveFields) {
        const el = document.getElementById(id);
        if (el) {
            const fg = el.closest('.form-group') || el;
            if (show) fg.classList.remove('hidden');
            else fg.classList.add('hidden');
        }
    }
}

async function fetchOllamaModels() {
    const select = document.getElementById('local-model');
    select.innerHTML = '<option value="">Loading models...</option>';
    try {
        const data = await API.get('/api/ollama/models');
        select.innerHTML = '';
        if (data.models && data.models.length > 0) {
            for (const model of data.models) {
                const opt = document.createElement('option');
                opt.value = model;
                opt.textContent = model;
                select.appendChild(opt);
            }
        } else {
            select.innerHTML = '<option value="">No models found</option>';
        }
    } catch {
        select.innerHTML = '<option value="">Failed to load models</option>';
    }
}

// ── Setup Event Listeners ──
function setupEventListeners() {
    // Home button
    document.getElementById('btn-home').addEventListener('click', () => {
        renderConversationList();
        renderHomeCharacters();
        renderHomePersonas();
        renderHomeLore();
        switchView('home');
    });

    // Rename conversation (click title in header)
    document.getElementById('conv-title').addEventListener('click', () => {
        if (!State.currentConvId || !State.currentConv) return;
        const titleEl = document.getElementById('conv-title');
        const currentTitle = State.currentConv.title || '';

        const input = document.createElement('input');
        input.type = 'text';
        input.value = currentTitle;
        input.className = 'conv-title-input';
        titleEl.replaceWith(input);
        input.focus();
        input.select();

        const save = async () => {
            const newTitle = input.value.trim() || currentTitle;
            const span = document.createElement('span');
            span.id = 'conv-title';
            span.textContent = newTitle;
            input.replaceWith(span);
            // Re-attach click listener
            span.addEventListener('click', () => document.getElementById('conv-title').click());

            if (newTitle !== currentTitle) {
                try {
                    const updated = await API.put(`/api/conversations/${State.currentConvId}`, { title: newTitle });
                    State.currentConv.title = updated.title;
                    const convInList = State.conversations.find(c => c.id === State.currentConvId);
                    if (convInList) convInList.title = updated.title;
                } catch {
                    showToast('Failed to rename', 'error');
                    span.textContent = currentTitle;
                }
            }
        };

        input.addEventListener('blur', save);
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') input.blur();
            if (e.key === 'Escape') { input.value = currentTitle; input.blur(); }
        });
    });

    // Back to tree
    document.getElementById('btn-back-to-tree').addEventListener('click', async () => {
        // Refresh tree data first, then switch view, then render
        try {
            if (State.currentConvId) {
                State.treeData = await API.get(`/api/conversations/${State.currentConvId}/tree`);
            }
        } catch (e) {
            console.error('Failed to refresh tree:', e);
        }
        switchView('tree');
        try {
            renderTree();
        } catch (e) {
            console.error('renderTree error:', e);
            document.getElementById('tree-nodes').innerHTML =
                '<div style="color:var(--text-muted);padding:40px;">Error rendering tree. Check console.</div>';
        }
    });

    // Conversation filter tabs
    document.querySelectorAll('.conv-filter-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.conv-filter-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            State.convFilter = btn.dataset.filter;
            renderConversationList();
        });
    });

    // New conversation
    document.getElementById('btn-new-conv-home')?.addEventListener('click', openNewConvModal);
    document.getElementById('btn-create-conv').addEventListener('click', createConversation);

    // Character creator + import
    document.getElementById('btn-create-char')?.addEventListener('click', () => openCharacterModal());
    document.getElementById('btn-save-char')?.addEventListener('click', saveCharacter);
    document.getElementById('btn-import-char')?.addEventListener('click', () => {
        importFile('/api/characters/import', '.md', async () => {
            State.characters = await API.get('/api/characters');
            renderHomeCharacters();
        });
    });

    // Persona creator + import
    document.getElementById('btn-create-persona')?.addEventListener('click', () => openPersonaModal());
    document.getElementById('btn-save-persona')?.addEventListener('click', savePersona);
    document.getElementById('btn-import-persona')?.addEventListener('click', () => {
        importFile('/api/personas/import', '.md', async () => {
            State.personas = await API.get('/api/personas');
            renderHomePersonas();
        });
    });

    // Lore creator + import
    document.getElementById('btn-create-lore')?.addEventListener('click', () => openLoreModal());
    document.getElementById('btn-save-lore')?.addEventListener('click', saveLore);
    document.getElementById('btn-import-lore')?.addEventListener('click', () => {
        importFile('/api/lore/import', '.md', async () => {
            State.lore = await API.get('/api/lore');
            renderHomeLore();
        });
    });

    // Conversation import
    document.getElementById('btn-import-conv')?.addEventListener('click', () => {
        importFile('/api/conversations/import', '.json', async () => {
            State.conversations = await API.get('/api/conversations');
            renderConversationList();
        });
    });

    // Mode toggle (Weave / Local / Claude)
    document.querySelectorAll('#mode-toggle .toggle-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('#mode-toggle .toggle-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            const mode = btn.dataset.value;
            // Hide all mode-specific fields first
            document.getElementById('project-dir-group').classList.add('hidden');
            document.getElementById('cc-model-group').classList.add('hidden');
            document.getElementById('cc-effort-group').classList.add('hidden');
            document.getElementById('local-model-group').classList.add('hidden');
            if (mode === 'claude') {
                document.getElementById('project-dir-group').classList.remove('hidden');
                document.getElementById('cc-model-group').classList.remove('hidden');
                document.getElementById('cc-effort-group').classList.remove('hidden');
                showWeaveFields(false);
            } else if (mode === 'local') {
                document.getElementById('local-model-group').classList.remove('hidden');
                document.getElementById('project-dir-group').classList.remove('hidden');
                showWeaveFields(false);
                fetchOllamaModels();
            } else {
                showWeaveFields(true);
            }
        });
    });

    // Model selection — disable "max" effort when not opus
    document.getElementById('cc-model').addEventListener('change', () => {
        const model = document.getElementById('cc-model').value;
        const maxOpt = document.querySelector('#cc-effort option[value="max"]');
        if (model !== 'opus') {
            maxOpt.disabled = true;
            if (document.getElementById('cc-effort').value === 'max') {
                document.getElementById('cc-effort').value = 'high';
            }
        } else {
            maxOpt.disabled = false;
        }
    });

    // Browse directory button
    document.getElementById('btn-browse-dir').addEventListener('click', () => {
        const browser = document.getElementById('dir-browser');
        if (!browser.classList.contains('hidden')) {
            browser.classList.add('hidden');
            return;
        }
        const currentPath = document.getElementById('project-dir').value.trim();
        loadDirBrowser(currentPath || '');
    });

    // First-turn toggle
    document.querySelectorAll('#first-turn-toggle .toggle-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('#first-turn-toggle .toggle-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
        });
    });

    // Settings
    document.getElementById('btn-settings').addEventListener('click', async () => {
        const cfg = await API.get('/api/config');
        State.config = cfg;
        document.getElementById('cfg-host').value = cfg.ollama_host || '';
        document.getElementById('cfg-model').value = cfg.ollama_model || '';
        document.getElementById('cfg-temp').value = cfg.temperature || 0.85;
        document.getElementById('cfg-top-p').value = cfg.top_p || 0.92;
        document.getElementById('cfg-max-tokens').value = cfg.max_tokens || 1024;
        document.getElementById('cfg-repeat-penalty').value = cfg.repeat_penalty || 1.12;
        document.getElementById('cfg-context').value = cfg.max_context_tokens || 28000;
        document.getElementById('cfg-verbatim').value = cfg.verbatim_window || 8;
        openModal('modal-settings');
    });

    document.getElementById('btn-save-settings').addEventListener('click', async () => {
        const cfg = {
            ollama_host: document.getElementById('cfg-host').value,
            ollama_model: document.getElementById('cfg-model').value,
            temperature: parseFloat(document.getElementById('cfg-temp').value),
            top_p: parseFloat(document.getElementById('cfg-top-p').value),
            max_tokens: parseInt(document.getElementById('cfg-max-tokens').value),
            repeat_penalty: parseFloat(document.getElementById('cfg-repeat-penalty').value),
            max_context_tokens: parseInt(document.getElementById('cfg-context').value),
            verbatim_window: parseInt(document.getElementById('cfg-verbatim').value),
        };
        await API.put('/api/config', cfg);
        State.config = cfg;
        closeModal('modal-settings');
        showToast('Settings saved');
    });

    // Close modals
    document.querySelectorAll('[data-close-modal]').forEach(btn => {
        btn.addEventListener('click', () => btn.closest('.modal').classList.add('hidden'));
    });
    document.querySelectorAll('.modal-backdrop').forEach(bd => {
        bd.addEventListener('click', () => bd.closest('.modal').classList.add('hidden'));
    });

    // Send message
    document.getElementById('btn-send').addEventListener('click', sendMessage);
    document.getElementById('user-input').addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });

    // Auto-resize textarea
    document.getElementById('user-input').addEventListener('input', autoResizeTextarea);

    // Image upload
    document.getElementById('file-input').addEventListener('change', handleImageSelect);

    // Paste image from clipboard
    document.getElementById('user-input').addEventListener('paste', handleImagePaste);
}

function autoResizeTextarea() {
    const ta = document.getElementById('user-input');
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, 200) + 'px';
}

// ── Character Grid ──
function renderCharacterGrid() {
    const grid = document.getElementById('character-grid');
    grid.innerHTML = '';

    const noChar = document.createElement('div');
    noChar.className = `char-card${!State.selectedCharacterId ? ' selected' : ''}`;
    noChar.innerHTML = `
        <div class="char-avatar">○</div>
        <div class="char-name">Freeform</div>
        <div class="char-tags">No character</div>
    `;
    noChar.addEventListener('click', () => { State.selectedCharacterId = null; renderCharacterGrid(); });
    grid.appendChild(noChar);

    for (const char of State.characters) {
        const card = document.createElement('div');
        card.className = `char-card${State.selectedCharacterId === char.id ? ' selected' : ''}`;
        const initial = char.name ? char.name[0].toUpperCase() : '?';
        const tags = Array.isArray(char.tags) ? char.tags.join(', ') : '';
        card.innerHTML = `
            <div class="char-avatar">${initial}</div>
            <div class="char-name">${escapeHtml(char.name)}</div>
            <div class="char-tags">${escapeHtml(tags)}</div>
        `;
        card.addEventListener('click', () => { State.selectedCharacterId = char.id; renderCharacterGrid(); });
        grid.appendChild(card);
    }
}

// ── Image Upload (multi-image, max 5) ──
const MAX_PENDING_IMAGES = 5;

function clearPendingImages() {
    State.pendingImages = [];
    document.getElementById('file-input').value = '';
    renderImagePreviews();
}

function removePendingImage(index) {
    State.pendingImages.splice(index, 1);
    renderImagePreviews();
}

function renderImagePreviews() {
    const container = document.getElementById('image-preview');
    if (State.pendingImages.length === 0) {
        container.classList.add('hidden');
        container.innerHTML = '';
        return;
    }
    container.classList.remove('hidden');
    container.innerHTML = State.pendingImages.map((img, i) => `
        <div class="preview-thumb">
            <img src="${img.url}" alt="Preview ${i + 1}">
            <button class="btn-remove-thumb" data-idx="${i}">✕</button>
        </div>
    `).join('') + `<span class="preview-count">${State.pendingImages.length}/${MAX_PENDING_IMAGES}</span>`;

    container.querySelectorAll('.btn-remove-thumb').forEach(btn => {
        btn.addEventListener('click', () => removePendingImage(parseInt(btn.dataset.idx)));
    });
}

async function attachImage(file) {
    if (State.pendingImages.length >= MAX_PENDING_IMAGES) {
        showToast(`Max ${MAX_PENDING_IMAGES} images allowed`, 'error');
        return;
    }
    try {
        const result = await API.upload(file);
        State.pendingImages.push({ path: result.path, url: result.url });
        renderImagePreviews();
    } catch (err) {
        showToast('Image upload failed', 'error');
    }
}

function initPasteHandler() {
    const input = document.getElementById('user-input');
    if (!input) return;
    input.addEventListener('paste', async (e) => {
        const items = e.clipboardData?.items;
        if (!items) return;
        for (const item of items) {
            if (item.type.startsWith('image/')) {
                e.preventDefault();
                const file = item.getAsFile();
                if (file) await attachImage(file);
                return;
            }
        }
    });
}

async function handleImageSelect(e) {
    const file = e.target.files[0];
    if (!file) return;
    await attachImage(file);
    e.target.value = '';
}

async function handleImagePaste(e) {
    const items = e.clipboardData && e.clipboardData.items;
    if (!items) return;
    const imageFiles = [];
    for (const item of items) {
        if (item.type.startsWith('image/')) {
            const file = item.getAsFile();
            if (file) imageFiles.push(file);
        }
    }
    if (imageFiles.length > 0) {
        e.preventDefault();
        for (const file of imageFiles) {
            await attachImage(file);
        }
    }
}

// ── Persona Select ──
function renderPersonaSelect() {
    const select = document.getElementById('persona-select');
    select.innerHTML = '<option value="">None</option>';
    for (const p of State.personas) {
        const opt = document.createElement('option');
        opt.value = p.id;
        opt.textContent = p.name;
        select.appendChild(opt);
    }
}

// ── Lore Checklist ──
function renderLoreChecklist() {
    const container = document.getElementById('lore-checklist');
    container.innerHTML = '';
    for (const entry of State.lore) {
        const label = document.createElement('label');
        label.className = 'lore-check';
        label.innerHTML = `
            <input type="checkbox" value="${escapeHtml(entry.id)}">
            <span>${escapeHtml(entry.name)}</span>
        `;
        container.appendChild(label);
    }
}

// ── Home Characters Grid ──
function renderHomeCharacters() {
    const grid = document.getElementById('home-char-grid');
    if (!grid) return;
    grid.innerHTML = '';

    for (const char of State.characters) {
        const card = document.createElement('div');
        card.className = 'char-card home-char-card';
        const initial = char.name ? char.name[0].toUpperCase() : '?';
        const tags = Array.isArray(char.tags) ? char.tags.join(', ') : '';
        card.innerHTML = `
            <div class="char-card-main">
                <div class="char-avatar">${initial}</div>
                <div class="char-name">${escapeHtml(char.name)}</div>
                <div class="char-tags">${escapeHtml(tags)}</div>
            </div>
            <div class="char-card-actions">
                <button class="char-action-btn char-edit-btn" title="Edit">✎</button>
                <button class="char-action-btn char-export-btn" title="Export">↓</button>
                <button class="char-action-btn char-delete-btn" title="Delete">✕</button>
            </div>
        `;
        // Click main area → start new conversation with this character
        card.querySelector('.char-card-main').addEventListener('click', () => {
            State.selectedCharacterId = char.id;
            openNewConvModal();
        });
        // Edit button
        card.querySelector('.char-edit-btn').addEventListener('click', (e) => {
            e.stopPropagation();
            openCharacterModal(char.id);
        });
        // Export button
        card.querySelector('.char-export-btn').addEventListener('click', (e) => {
            e.stopPropagation();
            downloadFile(`/api/characters/${char.id}/export`, `${char.id}.md`);
        });
        // Delete button
        card.querySelector('.char-delete-btn').addEventListener('click', async (e) => {
            e.stopPropagation();
            if (!confirm(`Delete character "${char.name}"? This cannot be undone.`)) return;
            try {
                await API.del(`/api/characters/${char.id}`);
                State.characters = State.characters.filter(c => c.id !== char.id);
                renderHomeCharacters();
                showToast('Character deleted');
            } catch (err) {
                showToast('Failed to delete character', 'error');
            }
        });
        grid.appendChild(card);
    }

    if (State.characters.length === 0) {
        grid.innerHTML = '<div class="home-empty-hint">No characters yet. Create one to get started.</div>';
    }
}


// ── Character Creator/Editor Modal ──
function openCharacterModal(charId) {
    const isEdit = !!charId;
    document.getElementById('char-modal-title').textContent = isEdit ? 'Edit Character' : 'Create Character';
    document.getElementById('char-edit-id').value = charId || '';

    if (isEdit) {
        const char = State.characters.find(c => c.id === charId);
        if (!char) return;
        document.getElementById('char-name').value = char.name || '';
        document.getElementById('char-tags').value = Array.isArray(char.tags) ? char.tags.join(', ') : '';
        document.getElementById('char-personality').value = char.personality || '';
        document.getElementById('char-scenario').value = char.scenario || '';
        document.getElementById('char-greeting').value = char.greeting || '';
        // Reconstruct example messages text
        const examples = char.example_messages || [];
        let exText = '';
        let exNum = 0;
        for (let i = 0; i < examples.length; i++) {
            if (examples[i].role === 'user') {
                exNum++;
                if (exNum > 1) exText += '\n';
                exText += `## Example ${exNum}\n`;
            }
            exText += `${examples[i].role}: ${examples[i].content}\n`;
        }
        document.getElementById('char-examples').value = exText.trim();
    } else {
        document.getElementById('char-name').value = '';
        document.getElementById('char-tags').value = '';
        document.getElementById('char-personality').value = '';
        document.getElementById('char-scenario').value = '';
        document.getElementById('char-greeting').value = '';
        document.getElementById('char-examples').value = '';
    }

    openModal('modal-char-edit');
}


async function saveCharacter() {
    const editId = document.getElementById('char-edit-id').value;
    const name = document.getElementById('char-name').value.trim();
    if (!name) {
        showToast('Character name is required', 'error');
        return;
    }

    const data = {
        name,
        tags: document.getElementById('char-tags').value,
        personality: document.getElementById('char-personality').value,
        scenario: document.getElementById('char-scenario').value,
        greeting: document.getElementById('char-greeting').value,
        example_messages_raw: document.getElementById('char-examples').value,
    };

    try {
        let saved;
        if (editId) {
            saved = await API.put(`/api/characters/${editId}`, data);
        } else {
            saved = await API.post('/api/characters', data);
        }

        // Refresh character list
        State.characters = await API.get('/api/characters');
        renderHomeCharacters();
        closeModal('modal-char-edit');
        showToast(editId ? 'Character updated' : 'Character created');
    } catch (err) {
        showToast('Failed to save character', 'error');
        console.error(err);
    }
}


// ── Home Personas Grid ──
function renderHomePersonas() {
    const grid = document.getElementById('home-persona-grid');
    if (!grid) return;
    grid.innerHTML = '';

    for (const persona of State.personas) {
        const card = document.createElement('div');
        card.className = 'char-card home-persona-card';
        const initial = persona.name ? persona.name[0].toUpperCase() : '?';
        const tags = Array.isArray(persona.tags) ? persona.tags.join(', ') : '';
        card.innerHTML = `
            <div class="char-card-main">
                <div class="char-avatar persona-avatar">${initial}</div>
                <div class="char-name">${escapeHtml(persona.name)}</div>
                <div class="char-tags">${escapeHtml(tags)}</div>
            </div>
            <div class="char-card-actions">
                <button class="char-action-btn char-edit-btn" title="Edit">✎</button>
                <button class="char-action-btn char-export-btn" title="Export">↓</button>
                <button class="char-action-btn char-delete-btn" title="Delete">✕</button>
            </div>
        `;
        card.querySelector('.char-edit-btn').addEventListener('click', (e) => {
            e.stopPropagation();
            openPersonaModal(persona.id);
        });
        card.querySelector('.char-export-btn').addEventListener('click', (e) => {
            e.stopPropagation();
            downloadFile(`/api/personas/${persona.id}/export`, `${persona.id}.md`);
        });
        card.querySelector('.char-delete-btn').addEventListener('click', async (e) => {
            e.stopPropagation();
            if (!confirm(`Delete persona "${persona.name}"? This cannot be undone.`)) return;
            try {
                await API.del(`/api/personas/${persona.id}`);
                State.personas = State.personas.filter(p => p.id !== persona.id);
                renderHomePersonas();
                showToast('Persona deleted');
            } catch (err) {
                showToast('Failed to delete persona', 'error');
            }
        });
        grid.appendChild(card);
    }

    if (State.personas.length === 0) {
        grid.innerHTML = '<div class="home-empty-hint">No personas yet. Create one to define who you are in the RP.</div>';
    }
}


// ── Persona Creator/Editor Modal ──
function openPersonaModal(personaId) {
    const isEdit = !!personaId;
    document.getElementById('persona-modal-title').textContent = isEdit ? 'Edit Persona' : 'Create Persona';
    document.getElementById('persona-edit-id').value = personaId || '';

    if (isEdit) {
        const persona = State.personas.find(p => p.id === personaId);
        if (!persona) return;
        document.getElementById('persona-name').value = persona.name || '';
        document.getElementById('persona-tags').value = Array.isArray(persona.tags) ? persona.tags.join(', ') : '';
        document.getElementById('persona-content').value = persona.content || '';
    } else {
        document.getElementById('persona-name').value = '';
        document.getElementById('persona-tags').value = '';
        document.getElementById('persona-content').value = '';
    }

    openModal('modal-persona-edit');
}


async function savePersona() {
    const editId = document.getElementById('persona-edit-id').value;
    const name = document.getElementById('persona-name').value.trim();
    if (!name) {
        showToast('Persona name is required', 'error');
        return;
    }

    const data = {
        name,
        tags: document.getElementById('persona-tags').value,
        content: document.getElementById('persona-content').value,
    };

    try {
        if (editId) {
            await API.put(`/api/personas/${editId}`, data);
        } else {
            await API.post('/api/personas', data);
        }

        State.personas = await API.get('/api/personas');
        renderHomePersonas();
        closeModal('modal-persona-edit');
        showToast(editId ? 'Persona updated' : 'Persona created');
    } catch (err) {
        showToast('Failed to save persona', 'error');
        console.error(err);
    }
}


// ── Home Lore Grid ──
function renderHomeLore() {
    const grid = document.getElementById('home-lore-grid');
    if (!grid) return;
    grid.innerHTML = '';

    for (const entry of State.lore) {
        const card = document.createElement('div');
        card.className = 'char-card home-lore-card';
        const initial = entry.name ? entry.name[0].toUpperCase() : '?';
        const tags = Array.isArray(entry.tags) ? entry.tags.join(', ') : '';
        card.innerHTML = `
            <div class="char-card-main">
                <div class="char-avatar lore-avatar">${initial}</div>
                <div class="char-name">${escapeHtml(entry.name)}</div>
                <div class="char-tags">${escapeHtml(tags)}</div>
            </div>
            <div class="char-card-actions">
                <button class="char-action-btn char-edit-btn" title="Edit">✎</button>
                <button class="char-action-btn char-export-btn" title="Export">↓</button>
                <button class="char-action-btn char-delete-btn" title="Delete">✕</button>
            </div>
        `;
        card.querySelector('.char-edit-btn').addEventListener('click', (e) => {
            e.stopPropagation();
            openLoreModal(entry.id);
        });
        card.querySelector('.char-export-btn').addEventListener('click', (e) => {
            e.stopPropagation();
            downloadFile(`/api/lore/${entry.id}/export`, `${entry.id}.md`);
        });
        card.querySelector('.char-delete-btn').addEventListener('click', async (e) => {
            e.stopPropagation();
            if (!confirm(`Delete lore entry "${entry.name}"? This cannot be undone.`)) return;
            try {
                await API.del(`/api/lore/${entry.id}`);
                State.lore = State.lore.filter(l => l.id !== entry.id);
                renderHomeLore();
                showToast('Lore entry deleted');
            } catch (err) {
                showToast('Failed to delete lore entry', 'error');
            }
        });
        grid.appendChild(card);
    }

    if (State.lore.length === 0) {
        grid.innerHTML = '<div class="home-empty-hint">No lore entries yet. Add world history, locations, or factions.</div>';
    }
}


// ── Lore Creator/Editor Modal ──
function openLoreModal(loreId) {
    const isEdit = !!loreId;
    document.getElementById('lore-modal-title').textContent = isEdit ? 'Edit Lore Entry' : 'Create Lore Entry';
    document.getElementById('lore-edit-id').value = loreId || '';

    if (isEdit) {
        const entry = State.lore.find(l => l.id === loreId);
        if (!entry) return;
        document.getElementById('lore-name').value = entry.name || '';
        document.getElementById('lore-tags').value = Array.isArray(entry.tags) ? entry.tags.join(', ') : '';
        document.getElementById('lore-content').value = entry.content || '';
    } else {
        document.getElementById('lore-name').value = '';
        document.getElementById('lore-tags').value = '';
        document.getElementById('lore-content').value = '';
    }

    openModal('modal-lore-edit');
}


async function saveLore() {
    const editId = document.getElementById('lore-edit-id').value;
    const name = document.getElementById('lore-name').value.trim();
    if (!name) {
        showToast('Lore entry name is required', 'error');
        return;
    }

    const data = {
        name,
        tags: document.getElementById('lore-tags').value,
        content: document.getElementById('lore-content').value,
    };

    try {
        if (editId) {
            await API.put(`/api/lore/${editId}`, data);
        } else {
            await API.post('/api/lore', data);
        }

        State.lore = await API.get('/api/lore');
        renderHomeLore();
        closeModal('modal-lore-edit');
        showToast(editId ? 'Lore entry updated' : 'Lore entry created');
    } catch (err) {
        showToast('Failed to save lore entry', 'error');
        console.error(err);
    }
}


// ── Download / Import Helpers ──

async function downloadFile(url, filename) {
    try {
        const res = await fetch(url);
        if (!res.ok) throw new Error(`Download failed: ${res.status}`);
        const blob = await res.blob();
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(a.href);
    } catch (err) {
        showToast('Download failed', 'error');
        console.error(err);
    }
}

function importFile(url, accept, refreshFn) {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = accept;
    input.addEventListener('change', async () => {
        const file = input.files[0];
        if (!file) return;
        const form = new FormData();
        form.append('file', file);
        try {
            const res = await fetch(url, { method: 'POST', body: form });
            if (!res.ok) throw new Error(`Import failed: ${res.status}`);
            await refreshFn();
            showToast('Imported successfully');
        } catch (err) {
            showToast('Import failed', 'error');
            console.error(err);
        }
    });
    input.click();
}

// ── Helpers ──
function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function scrollToBottom() {
    const container = document.getElementById('messages-container');
    requestAnimationFrame(() => { container.scrollTop = container.scrollHeight; });
}

// ── Start ──
document.addEventListener('DOMContentLoaded', init);
