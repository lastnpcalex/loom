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
    pendingImagePath: null,
    pendingImageUrl: null,
    config: {},
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

    renderHomeCharacters();
    renderConversationList();
    renderHomePersonas();
    renderHomeLore();
    setupEventListeners();
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

    if (State.conversations.length === 0) {
        empty.style.display = '';
        return;
    }
    empty.style.display = 'none';

    for (const conv of State.conversations) {
        const div = document.createElement('div');
        div.className = 'conv-item';

        const charName = conv.character_id
            ? (State.characters.find(c => c.id === conv.character_id)?.name || conv.character_id)
            : 'Freeform';

        div.innerHTML = `
            <span class="conv-title-text">${escapeHtml(conv.title)}</span>
            <span class="conv-meta">${escapeHtml(charName)}</span>
            <button class="conv-delete" title="Delete">✕</button>
        `;
        div.querySelector('.conv-title-text').addEventListener('click', () => loadConversation(conv.id));
        div.querySelector('.conv-meta').addEventListener('click', () => loadConversation(conv.id));
        div.querySelector('.conv-delete').addEventListener('click', async (e) => {
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
        list.appendChild(div);
    }
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
    const title = document.getElementById('new-conv-title').value.trim() || 'New Conversation';
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
            const sendGenerate = () => {
                if (State.ws && State.ws.readyState === WebSocket.OPEN) {
                    State.ws.send(JSON.stringify({ action: 'generate' }));
                }
            };
            if (State.ws && State.ws.readyState === WebSocket.OPEN) {
                sendGenerate();
            } else if (State.ws) {
                State.ws.addEventListener('open', sendGenerate, { once: true });
            }
        }
    }
}

// ── Setup Event Listeners ──
function setupEventListeners() {
    // Home button
    document.getElementById('btn-home').addEventListener('click', () => {
        renderHomeCharacters();
        renderConversationList();
        renderHomePersonas();
        renderHomeLore();
        switchView('home');
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

    // New conversation (both header and home button)
    const openNewConvModal = () => {
        renderCharacterGrid();
        renderPersonaSelect();
        renderLoreChecklist();
        document.querySelectorAll('#first-turn-toggle .toggle-btn').forEach(b => b.classList.remove('active'));
        document.querySelector('#first-turn-toggle .toggle-btn[data-value="character"]').classList.add('active');
        document.getElementById('custom-scene').value = '';
        openModal('modal-new-conv');
    };
    document.getElementById('btn-new-conv-home')?.addEventListener('click', openNewConvModal);
    document.getElementById('btn-create-conv').addEventListener('click', createConversation);

    // Character creator
    document.getElementById('btn-create-char')?.addEventListener('click', () => openCharacterModal());
    document.getElementById('btn-save-char')?.addEventListener('click', saveCharacter);

    // Persona creator
    document.getElementById('btn-create-persona')?.addEventListener('click', () => openPersonaModal());
    document.getElementById('btn-save-persona')?.addEventListener('click', savePersona);

    // Lore creator
    document.getElementById('btn-create-lore')?.addEventListener('click', () => openLoreModal());
    document.getElementById('btn-save-lore')?.addEventListener('click', saveLore);

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
    document.getElementById('btn-remove-image').addEventListener('click', () => {
        State.pendingImagePath = null;
        State.pendingImageUrl = null;
        document.getElementById('image-preview').classList.add('hidden');
    });
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

// ── Image Upload ──
async function handleImageSelect(e) {
    const file = e.target.files[0];
    if (!file) return;
    try {
        const result = await API.upload(file);
        State.pendingImagePath = result.path;
        State.pendingImageUrl = result.url;
        document.getElementById('preview-img').src = result.url;
        document.getElementById('image-preview').classList.remove('hidden');
    } catch (err) {
        showToast('Image upload failed', 'error');
    }
    e.target.value = '';
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
                <button class="char-action-btn char-delete-btn" title="Delete">✕</button>
            </div>
        `;
        card.querySelector('.char-edit-btn').addEventListener('click', (e) => {
            e.stopPropagation();
            openPersonaModal(persona.id);
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
                <button class="char-action-btn char-delete-btn" title="Delete">✕</button>
            </div>
        `;
        card.querySelector('.char-edit-btn').addEventListener('click', (e) => {
            e.stopPropagation();
            openLoreModal(entry.id);
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
