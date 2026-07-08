// ── Loom Admin dashboard ────────────────────────────────────────────
// Single-page client over admin_server.py's JSON API. No build step.

let META = null;          // /api/meta payload
let cronArchiveTab = false;
let refreshTimer = null;
let specsLoaded = false;

function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

function showToast(msg) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.style.display = 'block';
    clearTimeout(showToast._timer);
    showToast._timer = setTimeout(() => t.style.display = 'none', 3000);
}

function ensureConfirmModal() {
    let modal = document.getElementById('confirm-modal');
    if (modal) return modal;
    modal = document.createElement('div');
    modal.id = 'confirm-modal';
    modal.className = 'confirm-modal';
    modal.innerHTML = `
        <div class="confirm-backdrop" data-confirm-cancel></div>
        <div class="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
            <div class="confirm-kicker">Confirm Action</div>
            <h2 id="confirm-title">Are you sure?</h2>
            <p id="confirm-message"></p>
            <div class="confirm-actions">
                <button class="btn btn-ghost" data-confirm-cancel>Cancel</button>
                <button class="btn btn-warn" data-confirm-ok>OK</button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
    return modal;
}

function confirmDialog(message, title = 'Are you sure?') {
    const modal = ensureConfirmModal();
    const titleEl = modal.querySelector('#confirm-title');
    const msgEl = modal.querySelector('#confirm-message');
    const ok = modal.querySelector('[data-confirm-ok]');
    const cancelEls = modal.querySelectorAll('[data-confirm-cancel]');
    titleEl.textContent = title;
    msgEl.textContent = message;
    modal.classList.add('open');
    ok.focus();

    return new Promise(resolve => {
        const cleanup = (value) => {
            modal.classList.remove('open');
            ok.removeEventListener('click', onOk);
            cancelEls.forEach(el => el.removeEventListener('click', onCancel));
            document.removeEventListener('keydown', onKey);
            resolve(value);
        };
        const onOk = () => cleanup(true);
        const onCancel = () => cleanup(false);
        const onKey = (e) => {
            if (e.key === 'Escape') cleanup(false);
            if (e.key === 'Enter') cleanup(true);
        };
        ok.addEventListener('click', onOk);
        cancelEls.forEach(el => el.addEventListener('click', onCancel));
        document.addEventListener('keydown', onKey);
    });
}

// ── View switching ──────────────────────────────────────────────────
const VIEW_TITLES = { overview: 'Overview', servers: 'Servers', terminal: 'Terminal', tools: 'Tools', cron: 'Cron Jobs', guide: 'User Guide' };

function setView(name) {
    document.querySelectorAll('.nav-item').forEach(b => b.classList.toggle('active', b.dataset.view === name));
    document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + name));
    document.getElementById('view-title').textContent = VIEW_TITLES[name] || name;
    localStorage.setItem('loom-admin-view', name);
    if (name === 'overview') {
        if (!specsLoaded) loadSpecs();
        loadLlamaModels({ force: true });
        renderMemoryServices();
        loadDbStorage();
    }
    if (name === 'terminal') refreshTtyd();
}

// ── Llama switch-model control ──────────────────────────────────────
function _llamaModelKey(name) {
    return String(name || '')
        .split(/[\\/]/).pop()
        .replace(/\.gguf$/i, '')
        .toLowerCase()
        .replace(/[^a-z0-9]/g, '');
}

function _matchLlamaModelName(name, models) {
    if (!name) return '';
    const base = String(name).split(/[\\/]/).pop();
    if (models.includes(base)) return base;
    const key = _llamaModelKey(base);
    return models.find(m => _llamaModelKey(m) === key) || base;
}

async function loadLlamaModels(opts = {}) {
    const sel = document.getElementById('llama-model-switch');
    if (!sel || (!opts.force && sel.dataset.loaded === '1')) return;
    try {
        const r = await fetch('/api/llama-models', { cache: 'no-store' });
        if (!r.ok) return;
        const d = await r.json();
        const models = d.models || [];
        const loadedNow = (d.loaded || []).map(m => _matchLlamaModelName(m, models)).filter(Boolean);
        const configured = _matchLlamaModelName(d.configured, models);
        const options = models.slice();
        for (const name of loadedNow) {
            if (name && !options.includes(name)) options.unshift(name);
        }
        if (configured && !options.includes(configured)) options.push(configured);
        const selected = loadedNow[0] || configured || '';
        sel.innerHTML = options.map(m => {
            const marks = [];
            if (loadedNow.some(l => l === m)) marks.push('loaded');
            if (m === configured) marks.push('default');
            return `<option value="${esc(m)}"${m === selected ? ' selected' : ''}>${esc(m)}${marks.length ? ' - ' + marks.join(', ') : ''}</option>`;
        }).join('') || '<option value="">(no .gguf files found)</option>';
        sel.dataset.loaded = '1';
        sel.dataset.loadedModel = loadedNow[0] || '';
        return;
        /*
        const loaded = (d.loaded || []).map(m => m.split(/[\\/]/).pop());
        sel.innerHTML = (d.models || []).map(m => {
            const marks = [];
            if (loaded.some(l => l === m)) marks.push('loaded');
            if (m === d.configured) marks.push('default');
            return `<option value="${esc(m)}"${m === d.configured ? ' selected' : ''}>${esc(m)}${marks.length ? ' — ' + marks.join(', ') : ''}</option>`;
        }).join('') || '<option value="">(no .gguf files found)</option>';
        sel.dataset.loaded = '1';
        */
    } catch (e) { /* admin down or no models dir */ }
}

// Memory services: shared visibility for local processes that intentionally
// park GPU or system RAM (Llama, Dream/DiffusionGemma, ComfyUI).
function fmtMb(mb) {
    if (mb === null || mb === undefined) return '-';
    const n = Number(mb) || 0;
    return n >= 1024 ? (n / 1024).toFixed(n >= 10240 ? 1 : 2) + ' GB' : Math.round(n) + ' MB';
}

function memoryStateLabel(state) {
    return {
        warm: 'Warm',
        ready: 'Ready',
        orphan: 'Orphan',
        off: 'Off',
    }[state] || state || 'Unknown';
}

function memoryStateHint(s) {
    if (s.state === 'warm') return 'loaded and intentionally parked for reuse';
    if (s.state === 'ready') return 'process is running; model loads on demand';
    if (s.state === 'orphan') return 'process exists but the service probe failed';
    return 'not running';
}

function renderMemoryPanel(s) {
    const loaded = (s.loaded || []).filter(Boolean);
    const loadedText = loaded.length ? loaded.join(', ') : (s.state === 'ready' ? 'none loaded yet' : 'none');
    const pidText = (s.pids || []).length ? (s.pids || []).join(', ') : '-';
    const idle = s.idle_secs != null
        ? Math.floor(s.idle_secs / 60) + 'm ' + (s.idle_secs % 60) + 's'
        : '-';
    const timeout = s.idle_timeout_min > 0 ? 'auto-unloads at ' + s.idle_timeout_min + 'm idle' : 'auto-unload off';
    const extra = s.key === 'dream'
        ? `<div class="memory-row memory-muted">Idle: ${esc(idle)} (${esc(timeout)})</div>`
        : '';
    return `
        <div class="memory-top">
            <span class="memory-state memory-${esc(s.state)}">${esc(memoryStateLabel(s.state))}</span>
            <span class="memory-muted">${esc(memoryStateHint(s))}</span>
        </div>
        <div class="memory-row"><b>Loaded:</b> <span class="memory-loaded">${esc(loadedText)}</span></div>
        <div class="memory-grid">
            <div><span>GPU</span><b>${esc(fmtMb(s.gpu_mb))}</b></div>
            <div><span>Active RAM</span><b>${esc(fmtMb(s.active_ram_mb))}</b></div>
            <div><span>Reserved RAM</span><b>${esc(fmtMb(s.reserved_ram_mb))}</b></div>
            <div><span>Reclaimable Cache</span><b>${esc(fmtMb(s.cache_mb))}</b></div>
        </div>
        ${extra}
        <div class="memory-row memory-muted">PID: ${esc(pidText)} - ${esc(s.release_action || '')}</div>
    `;
}

async function renderMemoryServices() {
    let data;
    try {
        const r = await fetch('/api/memory-services', { cache: 'no-store' });
        if (!r.ok) return;
        data = await r.json();
    } catch (e) { return; }
    const byKey = {};
    (data.services || []).forEach(s => { byKey[s.key] = s; });
    for (const key of ['llama', 'dream', 'comfy']) {
        const panel = document.getElementById(key + '-memory-panel');
        if (!panel) continue;
        const svc = byKey[key];
        panel.innerHTML = svc
            ? renderMemoryPanel(svc)
            : '<span class="memory-muted">Memory state unavailable.</span>';
    }
}

async function renderDreamModels() {
    await renderMemoryServices();
}
async function llamaSwitchModel() {
    const sel = document.getElementById('llama-model-switch');
    const model = sel ? sel.value : '';
    if (!model) { showToast('Pick a model first'); return; }
    if (!await confirmDialog('Restart llama-server with ' + model + '? Cold load takes ~30-90s.', 'Restart Llama')) return;
    const btn = document.getElementById('btn-llama-switch');
    btn.disabled = true;
    await runTool('llama-restart?model=' + encodeURIComponent(model), { target: 'out-llama' });
    await loadLlamaModels({ force: true });
    btn.disabled = false;
}

async function llamaUnloadForComfy() {
    if (!await confirmDialog('Unload Llama Server and release its model memory? This stops llama-server until you reload it.', 'Unload Llama')) return;
    const btn = document.getElementById('btn-llama-unload');
    if (btn) btn.disabled = true;
    await runTool('llama-unload', { target: 'out-llama' });
    await loadLlamaModels({ force: true });
    if (btn) btn.disabled = false;
}

async function llamaReloadSelected() {
    const sel = document.getElementById('llama-model-switch');
    const model = sel ? sel.value : '';
    const suffix = model ? '?model=' + encodeURIComponent(model) : '';
    const label = model || 'the configured default model';
    if (!await confirmDialog('Reload llama-server with ' + label + '? Cold load takes ~30-90s.', 'Reload Llama')) return;
    const btn = document.getElementById('btn-llama-reload');
    if (btn) btn.disabled = true;
    await runTool('llama-reload' + suffix, { target: 'out-llama' });
    await loadLlamaModels({ force: true });
    if (btn) btn.disabled = false;
}

// ── Hermes runtime management (Prometheus + attendants) ──────────────────
// Hits the admin /api/hermes/* relay endpoints, which forward to the main
// Loom server's _RUNTIMES. Three runtimes: Prometheus (incognito, always-warm,
// cloud-fallback) + llama/dream attendants (ensouled, model-bound).
async function fetchHermesStatus() {
    try {
        const r = await fetch('/api/hermes/status', { cache: 'no-store' });
        if (!r.ok) return null;
        return await r.json();
    } catch (e) { return null; }
}

function renderHermesRuntimePanel(status) {
    const panel = document.getElementById('hermes-runtime-panel');
    if (!panel) return;
    if (!status || !status.hermes_available) {
        panel.innerHTML = '<span class="memory-muted">Hermes adapter unavailable' +
            (status && status.import_error ? ' (' + esc(String(status.import_error)) + ')' : '') + '.</span>';
        return;
    }
    const dot = (up) => up ? '🟢' : '⚫';
    const m = status.models || {};
    const llamaUp = m.llama && m.llama.up;
    const dreamUp = m.dream && m.dream.up;
    const llamaAtt = m.llama && m.llama.attendant;
    const dreamAtt = m.dream && m.dream.attendant;
    const prom = status.prometheus || {};
    const parts = [];
    parts.push('<div class="rt-row"><b>Models:</b> ' +
        `llama ${dot(llamaUp)} ${llamaUp ? 'up' : 'down'} · ` +
        `dream ${dot(dreamUp)} ${dreamUp ? 'up' : 'down'}</div>`);
    parts.push('<div class="rt-row"><b>Llama attendant:</b> ' +
        (llamaAtt && llamaAtt.alive ? `warm (PID ${llamaAtt.pid})` : 'cold (re-inits on next turn)') + '</div>');
    parts.push('<div class="rt-row"><b>Dream attendant:</b> ' +
        (dreamAtt && dreamAtt.alive ? `warm (PID ${dreamAtt.pid})` : 'cold (re-inits on next turn)') + '</div>');
    parts.push('<div class="rt-row"><b>Prometheus:</b> ' +
        (prom.alive ? `warm (PID ${prom.pid})` : 'cold — will init on next incognito turn') +
        (prom.held ? '' : ' · not held') + '</div>');
    panel.innerHTML = parts.join('');
}

async function hermesStatus() {
    const out = document.getElementById('out-hermes');
    if (out) { out.classList.remove('error'); out.textContent = 'Probing Hermes runtimes…'; }
    const s = await fetchHermesStatus();
    renderHermesRuntimePanel(s);
    if (out) {
        if (!s) { out.textContent = 'Main server unreachable.'; out.classList.add('error'); }
        else if (!s.hermes_available) { out.textContent = 'Hermes adapter unavailable: ' + (s.import_error || '?'); out.classList.add('error'); }
        else { out.textContent = JSON.stringify(s, null, 2); }
    }
}

async function prometheusRestart() {
    if (!await confirmDialog('Restart Prometheus? Re-routes the backend (rewrites config) and clears the warm runtime; it re-inits on the next incognito turn.', 'Restart Prometheus')) return;
    const btn = document.getElementById('btn-prometheus-restart');
    if (btn) btn.disabled = true;
    try {
        const r = await fetch('/api/hermes/prometheus/restart', { method: 'POST' });
        const d = await r.json();
        const out = document.getElementById('out-hermes');
        if (out) out.textContent = d.status === 'ok'
            ? `Prometheus re-routed → backend: ${d.backend}, model: ${d.model}\nWarm runtime cleared; re-inits on next incognito turn.`
            : ('Error: ' + (d.error || JSON.stringify(d)));
    } catch (e) {
        const out = document.getElementById('out-hermes');
        if (out) { out.textContent = 'Request failed: ' + e; out.classList.add('error'); }
    }
    await hermesStatus();
    if (btn) btn.disabled = false;
}

async function prometheusStop() {
    if (!await confirmDialog('Stop Prometheus\' warm runtime? It stays down until the next incognito turn re-inits it.', 'Stop Prometheus')) return;
    const btn = document.getElementById('btn-prometheus-stop');
    if (btn) btn.disabled = true;
    try {
        const r = await fetch('/api/hermes/prometheus/stop', { method: 'POST' });
        const d = await r.json();
        const out = document.getElementById('out-hermes');
        if (out) out.textContent = d.status === 'ok'
            ? `Prometheus stopped (cleared: ${d.cleared}).`
            : ('Error: ' + (d.error || JSON.stringify(d)));
    } catch (e) {
        const out = document.getElementById('out-hermes');
        if (out) { out.textContent = 'Request failed: ' + e; out.classList.add('error'); }
    }
    await hermesStatus();
    if (btn) btn.disabled = false;
}

async function attendantStop(backend) {
    if (!await confirmDialog(`Force-clear the ${backend} attendant's warm process? The soul (home/state.db) survives; it re-inits on the next turn.`, `Clear ${backend} attendant`)) return;
    try {
        const r = await fetch('/api/hermes/attendant/stop?backend=' + backend, { method: 'POST' });
        const d = await r.json();
        const out = document.getElementById('out-hermes');
        if (out) out.textContent = d.status === 'ok'
            ? `${backend} attendant cleared (cleared: ${d.cleared}).`
            : ('Error: ' + (d.error || JSON.stringify(d)));
    } catch (e) {
        const out = document.getElementById('out-hermes');
        if (out) { out.textContent = 'Request failed: ' + e; out.classList.add('error'); }
    }
    await hermesStatus();
}


// ── Meta / quick links ──────────────────────────────────────────────
async function loadMeta() {
    try {
        const r = await fetch('/api/meta', { cache: 'no-store' });
        if (!r.ok) return;
        META = await r.json();
    } catch (e) { return; }
    document.getElementById('admin-port').textContent = META.admin_port;
    const llamaTag = document.getElementById('llama-port-tag');
    if (llamaTag) llamaTag.textContent = ':' + META.llama_port;
    const nrolTag = document.getElementById('nrol-port-tag');
    if (nrolTag) nrolTag.textContent = ':' + META.nrol_port;
    const dreamTag = document.getElementById('dream-port-tag');
    if (dreamTag) dreamTag.textContent = ':' + META.dream_port;
    const ttydTag = document.getElementById('ttyd-port-tag');
    if (ttydTag) ttydTag.textContent = ':' + META.ttyd.port;

    // Links use the page's own hostname so they work over Tailscale.
    const h = location.hostname;
    const links = [
        ['🌐 Main Loom', `https://${h}:${META.main_port}`],
        ['🧪 Test', `http://${h}:${META.test_port}`],
        ['🦙 Llama', `http://${h}:${META.llama_port}`],
        ['📈 NROL-AO', `http://${h}:${META.nrol_port}`],
        ['🎨 ComfyUI', `http://${h}:${META.comfy_port}`],
    ];
    document.getElementById('quick-links').innerHTML = links
        .map(([label, url]) => `<a class="quick-link" href="${url}" target="_blank" rel="noopener">${label}</a>`)
        .join('');
}

// ── Polling ─────────────────────────────────────────────────────────
function scheduleRefresh(delay = 10000) {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refreshAll, delay);
}

async function refreshAll() {
    try {
        await Promise.all([refreshInstances(), refreshGenerations(), refreshCronJobs(), refreshPorts(), loadDbStorage()]);
        const overview = document.getElementById('view-overview');
        if (overview?.classList.contains('active') && document.activeElement?.id !== 'llama-model-switch') {
            await loadLlamaModels({ force: true });
            await renderDreamModels();
        }
        // Keep the Hermes runtime panel fresh without blocking the periodic loop
        // — a slow/unreachable main server must not stall refreshAll.
        fetchHermesStatus().then(renderHermesRuntimePanel).catch(() => {});
    } catch (e) { /* keep ticking */ }
    scheduleRefresh();
}

// Maps each server-card head-actions slot to the buttons that should render
// in each state. Keeps the up/down conditional logic — and the "no Stop button
// when nothing is running" rule — in one place.
const SERVER_HEAD_ACTIONS = {
    llama: {
        on: [
            { cls: 'btn btn-cyan', label: 'Restart', tool: 'llama-restart' },
            { cls: 'btn btn-warn', label: 'Unload', tool: 'llama-unload', confirm: 'Unload Llama Server and release its model memory?' },
        ],
        off: [
            { cls: 'btn btn-green', label: 'Start',  tool: 'llama-start' },
        ],
        target: 'out-llama',
    },
    comfy: {
        on: [
            { cls: 'btn btn-warn', label: 'Unload', tool: 'comfyui-stop', confirm: 'Unload ComfyUI by stopping its process?' },
        ],
        off: [
            { cls: 'btn btn-green', label: 'Start',  tool: 'comfyui-start' },
        ],
        target: 'out-comfy',
    },
    nrol: {
        on: [
            { cls: 'btn btn-warn', label: '⏹ Stop',    tool: 'nrol-dashboard-stop', confirm: 'Stop NROL-AO dashboard if Loom admin launched it?' },
        ],
        off: [
            { cls: 'btn btn-green', label: 'Start',  tool: 'nrol-dashboard-start' },
        ],
        target: 'out-nrol',
    },
    dream: {
        on: [
            { cls: 'btn btn-cyan', label: 'Restart', tool: 'dream-restart', confirm: 'Restart Dream Engine? This unloads Dream, releases cache, then starts it again.' },
            { cls: 'btn btn-warn', label: 'Unload', tool: 'dream-unload', confirm: 'Unload Dream Engine and release reclaimable model cache?' },
        ],
        off: [
            { cls: 'btn btn-green', label: 'Start',  tool: 'dream-start' },
        ],
        target: 'out-dream',
    },
};

function renderServerHeadActions(key, on) {
    const slot = document.getElementById('head-actions-' + key);
    if (!slot) return;
    const cfg = SERVER_HEAD_ACTIONS[key];
    if (!cfg) return;
    const buttons = on ? cfg.on : cfg.off;
    slot.innerHTML = buttons.map((b, i) =>
        `<button class="${b.cls}" data-srv="${key}" data-srv-i="${i}">${b.label}</button>`
    ).join('');
    slot.querySelectorAll('button').forEach(btn => {
        const b = buttons[+btn.dataset.srvI];
        btn.addEventListener('click', () => {
            const opts = { target: cfg.target };
            if (b.confirm) confirmTool(b.tool, b.confirm, opts);
            else runTool(b.tool, opts);
        });
    });
}

async function refreshPorts() {
    let d = {};
    try {
        const r = await fetch('/api/ports-status', { cache: 'no-store' });
        if (r.ok) d = await r.json();
    } catch (e) { return; }
    const order = [['main', 'Loom'], ['test', 'Test'], ['llama', 'Llama'], ['dream', 'Dream Engine'], ['nrol', 'NROL'], ['comfy', 'Comfy'], ['ttyd', 'ttyd']];
    document.getElementById('pulse-grid').innerHTML = order
        .map(([k, label]) => `<span class="pulse-item"><span class="dot ${d[k] ? 'on' : 'off'}"></span>${label}</span>`)
        .join('');
    for (const k of ['llama', 'comfy', 'nrol', 'ttyd', 'dream']) {
        const dot = document.getElementById('dot-' + k);
        if (dot) dot.className = 'dot ' + (d[k] ? 'on' : 'off');
    }
    for (const k of ['llama', 'comfy', 'nrol', 'dream']) renderServerHeadActions(k, !!d[k]);
    // Keep the terminal iframe in sync if ttyd died or came up elsewhere
    if (document.getElementById('view-terminal').classList.contains('active')) {
        const frame = document.getElementById('ttyd-frame');
        if (!d.ttyd && frame.classList.contains('live')) refreshTtyd();
        if (d.ttyd && !frame.classList.contains('live')) refreshTtyd();
    }
}

function _instanceActionsHtml(s) {
    const on = s.status === 'online';
    if (on) {
        return `<button onclick="doAction('${s.name}', 'restart')" class="btn btn-cyan">↻ Restart</button>
                <button onclick="doAction('${s.name}', 'shutdown')" class="btn btn-warn">⏹ Shutdown</button>`;
    }
    return `<button onclick="doAction('${s.name}', 'start')" class="btn btn-green">▶ Start</button>`;
}

function _instanceDbHtml(s) {
    if (s.name === 'main') {
        const dbs = (window.availableDbs || []).slice();
        if (!dbs.includes(s.db)) dbs.push(s.db);
        return `<select class="select" onchange="switchDb(this.value)" style="padding: 1px 4px; font-size: 0.9em;">
            ${dbs.map(db => `<option value="${esc(db)}"${db === s.db ? ' selected' : ''}>${esc(db)}</option>`).join('')}
        </select>`;
    }
    return `<span>${esc(s.db)}</span>`;
}

async function refreshInstances() {
    const r = await fetch('/api/status', { cache: 'no-store' });
    if (!r.ok) return;
    const d = await r.json();
    const instances = d.instances || [];

    // Mirror state onto the matching server-cards, so the
    // start/stop/restart controls and PID/DB readouts live there.
    for (const s of instances) {
        const on = s.status === 'online';
        const dot = document.getElementById('dot-' + s.name);
        if (dot) dot.className = 'dot ' + (on ? 'on' : 'off');
        const portTag = document.getElementById(s.name + '-port-tag');
        if (portTag) portTag.textContent = ':' + s.port;
        const managedSlot = document.getElementById(s.name + '-managed-tag');
        if (managedSlot) managedSlot.innerHTML = s.managed ? '<span class="tag">managed</span>' : '';
        const head = document.getElementById('head-actions-' + s.name);
        if (head) head.innerHTML = _instanceActionsHtml(s);
        const dbSlot = document.getElementById(s.name + '-db-label');
        if (dbSlot) dbSlot.innerHTML = _instanceDbHtml(s);
        const pidSlot = document.getElementById(s.name + '-pid-label');
        if (pidSlot) pidSlot.textContent = s.pid ? 'PID ' + s.pid : '—';
    }
}

async function refreshGenerations() {
    let gens = [];
    try {
        const r = await fetch('/api/generations-proxy', { cache: 'no-store' });
        if (r.ok) gens = await r.json();
    } catch (e) { /* ignore */ }
    const empty = document.getElementById('generations-empty');
    const table = document.getElementById('generations-table');
    const body = document.getElementById('generations-body');
    const count = document.getElementById('gens-count');
    if (!gens.length) {
        empty.style.display = 'block';
        table.style.display = 'none';
        count.textContent = '';
        return;
    }
    empty.style.display = 'none';
    table.style.display = 'table';
    count.textContent = '(' + gens.length + ')';
    body.innerHTML = gens.map(g => {
        const status = g.in_memory ? 'running' : (g.pid_alive ? 'orphan' : 'dead');
        const color = status === 'running' ? 'var(--green)' : (status === 'orphan' ? 'var(--amber)' : 'var(--text-mute)');
        const age = g.started_at ? Math.round(Date.now() / 1000 - g.started_at) + 's' : '—';
        return `<tr>
            <td>${g.conv_id}</td><td>#${g.draft_msg_id}</td><td>${g.pid || '—'}</td>
            <td><span style="color:${color}">${status}</span></td>
            <td>${esc(g.mode || '—')}</td><td>${age}</td>
            <td><button class="btn btn-warn" onclick="killGen(${g.draft_msg_id})">Kill</button></td>
        </tr>`;
    }).join('');
}

function setCronTab(archive) {
    cronArchiveTab = archive;
    document.getElementById('cron-active-tab').classList.toggle('active', !archive);
    document.getElementById('cron-archive-tab').classList.toggle('active', archive);
    refreshCronJobs();
}

async function refreshCronJobs() {
    let jobs = [];
    try {
        const r = await fetch('/api/cron-proxy?include_archived=' + (cronArchiveTab ? 'true' : 'false'), { cache: 'no-store' });
        if (r.ok) jobs = await r.json();
    } catch (e) { /* ignore */ }
    jobs = jobs.filter(j => cronArchiveTab ? j.archived : !j.archived);
    const empty = document.getElementById('cron-empty');
    const table = document.getElementById('cron-table');
    const body = document.getElementById('cron-body');
    const count = document.getElementById('cron-count');
    count.textContent = jobs.length ? '(' + jobs.length + ')' : '';
    if (!jobs.length) {
        empty.style.display = 'block';
        table.style.display = 'none';
        body.innerHTML = '';
        return;
    }
    empty.style.display = 'none';
    table.style.display = 'table';
    body.innerHTML = jobs.map(j => {
        const desc = esc(j.description || 'No description');
        const output = esc([j.last_output, j.last_error].filter(Boolean).join('\n'));
        const status = j.archived ? 'archived' : (j.enabled ? (j.last_status || 'enabled') : 'disabled');
        const actions = j.archived ? '' : `
            <button class="btn btn-cyan" onclick="toggleCron(${j.id}, ${j.enabled ? 'false' : 'true'})">${j.enabled ? 'Disable' : 'Enable'}</button>
            <button class="btn btn-warn" onclick="archiveCron(${j.id})">Archive</button>`;
        return `<tr>
            <td>#${j.id}</td>
            <td>${j.conv_id}</td>
            <td><code>${esc(j.script)}</code></td>
            <td>${Math.round((j.every_seconds || 0) / 60)} min</td>
            <td>${esc(j.last_run_at_display || 'never')}</td>
            <td>${esc(j.next_run_at_display || '-')}</td>
            <td>${esc(status)}${j.last_exit_code !== null && j.last_exit_code !== undefined ? ' (' + j.last_exit_code + ')' : ''}</td>
            <td><details class="cron-detail"><summary>Description</summary><pre>${desc}</pre>${output ? '<pre>' + output + '</pre>' : ''}</details></td>
            <td>${actions}</td>
        </tr>`;
    }).join('');
}

async function toggleCron(id, enabled) {
    try {
        const r = await fetch('/api/cron-proxy/' + id, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled }),
        });
        showToast(r.ok ? 'cron updated' : 'cron update failed');
    } catch (e) { showToast('cron update failed: ' + e); }
    setTimeout(refreshCronJobs, 300);
}

async function archiveCron(id) {
    if (!await confirmDialog('Archive cron job #' + id + '?', 'Archive Cron Job')) return;
    try {
        const r = await fetch('/api/cron-proxy/' + id, { method: 'DELETE' });
        showToast(r.ok ? 'cron archived' : 'cron archive failed');
    } catch (e) { showToast('cron archive failed: ' + e); }
    setTimeout(refreshCronJobs, 300);
}

async function killGen(draftId) {
    if (!await confirmDialog('Kill generation #' + draftId + '?', 'Kill Generation')) return;
    showToast('killing #' + draftId);
    try {
        const r = await fetch('/api/generations-proxy/' + draftId + '/kill', { method: 'POST' });
        if (r.ok) { const d = await r.json(); showToast(d.status || 'killed'); }
        else showToast('kill failed');
    } catch (e) { showToast('kill failed: ' + e); }
    setTimeout(refreshAll, 500);
}

// ── Instance + admin actions ────────────────────────────────────────
function _writeInstanceStatus(name, text, isError) {
    // Show progress + completion on both the compact Overview note and the
    // full Servers-page server-out, so the same "starting, started" widget
    // feedback the llama/comfy/nrol cards already gave you is mirrored here.
    const stamp = new Date().toLocaleTimeString();
    const line = '[' + stamp + '] ' + text;
    const note = document.getElementById('status-' + name);
    if (note) {
        note.textContent = text;
        note.style.color = isError ? 'var(--red)' : (text.endsWith('...') ? 'var(--amber)' : 'var(--green)');
    }
    const out = document.getElementById('out-' + name);
    if (out) {
        const existing = out.textContent ? out.textContent + '\n' : '';
        out.textContent = existing + line;
        out.classList.toggle('error', !!isError);
        out.scrollTop = out.scrollHeight;
    }
}

async function doAction(name, action) {
    const verb = { start: 'Starting', restart: 'Restarting', shutdown: 'Shutting down' }[action] || action;
    _writeInstanceStatus(name, verb + ' ' + name + '...', false);
    try {
        const r = await fetch('/action/' + name + '/' + action, { method: 'POST' });
        const d = await r.json();
        const msg = d.status || d.error || 'done';
        const isError = !!d.error;
        _writeInstanceStatus(name, msg, isError);
    } catch (e) {
        _writeInstanceStatus(name, 'failed: ' + e, true);
    }
    setTimeout(refreshAll, 1500);
}

async function adminAction(action) {
    showToast('admin ' + action + '...');
    const url = action === 'shutdown' ? '/shutdown' : '/admin/restart';
    try {
        const r = await fetch(url, { method: 'POST' });
        const d = await r.json();
        showToast(d.status || 'done');
    } catch (e) { /* connection drops — expected */ }
    if (action === 'restart') {
        showToast('admin restarting — waiting for it to come back...');
        const start = Date.now();
        const poll = async () => {
            try {
                const p = await fetch('/api/status', { cache: 'no-store' });
                if (p.ok) { location.reload(); return; }
            } catch (e) { /* still down */ }
            if (Date.now() - start < 20000) setTimeout(poll, 1000);
            else showToast('admin did not come back — check logs');
        };
        setTimeout(poll, 2500);
    }
}

// ── Tools ───────────────────────────────────────────────────────────
async function confirmTool(name, msg, opts) {
    if (await confirmDialog(msg)) runTool(name, opts);
}

async function runTool(name, opts = {}) {
    clearTimeout(refreshTimer);
    const out = document.getElementById(opts.target || 'tool-output');
    const isPre = out.tagName === 'PRE';
    out.classList.remove('error', 'html-mode');
    out.innerHTML = '<span class="spinner"></span> Running ' + esc(name) + '...';
    try {
        const r = await fetch('/tools/' + name, { method: opts.method || 'POST' });
        const d = await r.json();
        if (d.status === 'login_started' && d.url) {
            // Auth login flow always renders in the Tools output panel
            const panel = document.getElementById('tool-output');
            setView('tools');
            panel.classList.remove('error', 'html-mode');
            panel.innerHTML = esc(d.output).replace(/\n/g, '<br>') +
                '<br><br><a href="' + esc(d.url) + '" target="_blank" style="color:var(--cyan); word-break:break-all;">' + esc(d.url) + '</a>' +
                '<br><br><div style="margin-top:8px;">After authenticating, paste the code from the callback page:<br>' +
                '<input id="auth-code-input" type="text" placeholder="paste authorization code" style="width:60%; min-width:280px; margin-top:6px;">' +
                ' <button class="btn btn-cyan" onclick="submitAuthCode()" style="margin-left:6px;">Submit</button></div>' +
                '<br><span id="login-poll" style="color:var(--text-mute);">Waiting for login to complete...</span>';
            if (out !== panel) out.textContent = 'See Tools tab for the login flow.';
            pollLoginStatus();
            const inp = document.getElementById('auth-code-input');
            if (inp) {
                inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') submitAuthCode(); });
                inp.focus();
            }
            return;
        }
        if (d.status === 'ok_html' && !isPre) {
            out.innerHTML = d.output;
            out.classList.add('html-mode');
        } else {
            out.textContent = d.output || '(no output)';
            if (d.status === 'error') out.classList.add('error');
        }
    } catch (e) {
        out.textContent = 'Request failed: ' + e;
        out.classList.add('error');
    }
    if (/^llama-/.test(name)) {
        await loadLlamaModels({ force: true });
        await renderMemoryServices();
    }
    if (/^dream-/.test(name)) {
        await renderMemoryServices();
    }
    if (/^comfyui-|^clear-vram$|^memory-status$/.test(name)) {
        await renderMemoryServices();
    }
    scheduleRefresh(30000);
}

async function submitAuthCode() {
    const inp = document.getElementById('auth-code-input');
    if (!inp) return;
    const code = inp.value.trim();
    if (!code) { showToast('Paste the authorization code first'); return; }
    inp.disabled = true;
    try {
        const r = await fetch('/tools/auth-submit-code', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code }),
        });
        const d = await r.json();
        showToast(d.output || (d.status === 'ok' ? 'submitted' : 'failed'));
        if (d.status !== 'ok') inp.disabled = false;
    } catch (e) {
        showToast('submit failed: ' + e);
        inp.disabled = false;
    }
}

async function pollLoginStatus() {
    const poll = document.getElementById('login-poll');
    if (!poll) return;
    try {
        const r = await fetch('/tools/auth-login-status');
        const d = await r.json();
        if (d.status === 'waiting') {
            poll.innerHTML = '<span class="spinner"></span> ' + esc(d.output);
            setTimeout(pollLoginStatus, 3000);
        } else if (d.status === 'ok') {
            poll.style.color = 'var(--green)';
            poll.textContent = d.output;
            scheduleRefresh(5000);
        } else {
            poll.style.color = 'var(--red)';
            poll.textContent = d.output;
            scheduleRefresh(10000);
        }
    } catch (e) {
        poll.textContent = 'Poll failed: ' + e;
        scheduleRefresh(10000);
    }
}

// ── System specs ────────────────────────────────────────────────────
async function loadSpecs() {
    specsLoaded = true;
    const panel = document.getElementById('specs-panel');
    panel.innerHTML = '<div class="empty-note"><span class="spinner"></span> Querying CPU / RAM / GPU…</div>';
    try {
        const r = await fetch('/tools/system-specs', { method: 'POST' });
        const d = await r.json();
        panel.innerHTML = d.status === 'ok_html' ? d.output : '<pre class="server-out">' + esc(d.output || 'no output') + '</pre>';
    } catch (e) {
        panel.innerHTML = '<div class="spec-error">Specs query failed: ' + esc(e) + '</div>';
    }
}

// ── ttyd terminal ───────────────────────────────────────────────────
function ttydUrl(status) {
    const scheme = status.ssl ? 'https' : 'http';
    return `${scheme}://${location.hostname}:${status.port}/`;
}

async function refreshTtyd() {
    let s = null;
    try {
        const r = await fetch('/api/ttyd-status', { cache: 'no-store' });
        if (r.ok) s = await r.json();
    } catch (e) { /* admin down */ }
    if (!s) return;
    const dot = document.getElementById('dot-ttyd');
    if (dot) dot.className = 'dot ' + (s.running ? 'on' : 'off');
    const frame = document.getElementById('ttyd-frame');
    const open = document.getElementById('ttyd-open');
    const hint = document.getElementById('ttyd-hint');
    const url = ttydUrl(s);
    open.href = url;
    open.style.display = s.running ? '' : 'none';

    const hints = [];
    if (!s.exe) {
        hints.push('ttyd.exe not found — drop it at <code>bin/ttyd.exe</code> (<a href="https://github.com/tsl0922/ttyd/releases" target="_blank" rel="noopener">releases</a>) or set <code>TTYD_EXE</code>.');
    }
    if (s.running && s.ssl) {
        hints.push('Terminal stays black? Open it in a tab once to accept the self-signed certificate, then reload.');
    }
    const remote = !['localhost', '127.0.0.1'].includes(location.hostname);
    if (s.host === '127.0.0.1' && remote) {
        hints.push('ttyd is bound to 127.0.0.1 — for remote/Tailscale access set <code>TTYD_HOST=0.0.0.0</code> and <code>TTYD_CRED=user:pass</code>, then restart admin.');
    }
    hint.innerHTML = hints.join('<br>');

    if (s.running) {
        if (!frame.classList.contains('live') || frame.dataset.url !== url) {
            frame.src = url;
            frame.dataset.url = url;
            frame.classList.add('live');
        }
    } else {
        frame.classList.remove('live');
        frame.removeAttribute('src');
        delete frame.dataset.url;
    }
}

async function ttydStart() {
    const shell = document.getElementById('ttyd-shell').value;
    showToast('starting ttyd (' + shell + ')...');
    try {
        const r = await fetch('/tools/ttyd-start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ shell }),
        });
        const d = await r.json();
        showToast(d.output || d.status);
    } catch (e) { showToast('ttyd start failed: ' + e); }
    setTimeout(refreshTtyd, 800);
}

async function ttydStop() {
    if (!await confirmDialog('Stop the web terminal?', 'Stop Terminal')) return;
    try {
        const r = await fetch('/tools/ttyd-stop', { method: 'POST' });
        const d = await r.json();
        showToast(d.output ? d.output.split('\n')[0] : 'stopped');
    } catch (e) { showToast('ttyd stop failed: ' + e); }
    setTimeout(refreshTtyd, 500);
}

async function switchDb(dbName) {
    showToast('switching database to ' + dbName + '...');
    try {
        const r = await fetch('/api/change-db', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ db_name: dbName }),
        });
        const d = await r.json();
        if (r.ok) {
            showToast('database changed successfully' + (d.restarted ? ' (restarting Loom)' : ''));
        } else {
            showToast('failed to change database: ' + (d.error || 'unknown error'));
        }
    } catch (e) {
        showToast('error switching database: ' + e);
    }
    setTimeout(refreshAll, 1000);
}

window.availableDbs = [];
async function loadDatabases() {
    try {
        const r = await fetch('/api/databases', { cache: 'no-store' });
        if (r.ok) {
            const d = await r.json();
            window.availableDbs = d.databases || [];
        }
    } catch (e) { /* ignore */ }
}

async function loadDbStorage() {
    const folder = document.getElementById('db-folder');
    const filename = document.getElementById('db-filename');
    const current = document.getElementById('db-current');
    if (!folder || !filename || !current) return;
    try {
        const r = await fetch('/api/db-storage', { cache: 'no-store' });
        if (!r.ok) return;
        const d = await r.json();
        if (document.activeElement !== folder) folder.value = d.folder || '';
        if (document.activeElement !== filename) filename.value = d.filename || 'loom.db';
        const size = d.size_bytes ? ` (${(d.size_bytes / 1048576).toFixed(1)} MB)` : '';
        current.classList.toggle('warn', !!d.onedrive);
        current.textContent = `Current: ${d.resolved || d.configured || 'unknown'}${size}${d.onedrive ? ' - OneDrive path' : ''}`;
    } catch (e) {
        current.textContent = 'Database storage unavailable: ' + e;
        current.classList.add('warn');
    }
}

async function saveDbStorage() {
    const folder = document.getElementById('db-folder')?.value.trim();
    const filename = document.getElementById('db-filename')?.value.trim();
    const copyCurrent = !!document.getElementById('db-copy-current')?.checked;
    const restart = !!document.getElementById('db-restart-main')?.checked;
    if (!folder || !filename) {
        showToast('folder and file are required');
        return;
    }
    const msg = copyCurrent
        ? 'Save this DB location, stop Main Loom, copy the current SQLite files, then restart Main Loom?'
        : 'Save this DB location and restart Main Loom? If the DB does not exist, a new empty DB will be created.';
    if (!await confirmDialog(msg, 'Change Database Location')) return;
    const btn = document.getElementById('btn-db-save');
    if (btn) btn.disabled = true;
    try {
        const r = await fetch('/api/db-storage', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ folder, filename, copy_current: copyCurrent, restart }),
        });
        const d = await r.json();
        if (r.ok) {
            showToast('database location saved' + (d.restarted ? ' and Main Loom restarted' : ''));
            await loadDatabases();
            await loadDbStorage();
            setTimeout(refreshAll, 1500);
        } else {
            showToast('database change failed: ' + (d.error || 'unknown error'));
        }
    } catch (e) {
        showToast('database change failed: ' + e);
    } finally {
        if (btn) btn.disabled = false;
    }
}

// ── Boot ────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
    document.querySelectorAll('.nav-item').forEach(b =>
        b.addEventListener('click', () => setView(b.dataset.view)));
    document.getElementById('btn-admin-restart').addEventListener('click', () => adminAction('restart'));
    document.getElementById('btn-admin-shutdown').addEventListener('click', async () => {
        if (await confirmDialog('Shut down the admin server?', 'Shutdown Admin')) adminAction('shutdown');
    });
    document.getElementById('btn-specs-refresh').addEventListener('click', loadSpecs);
    document.getElementById('btn-ttyd-start').addEventListener('click', ttydStart);
    document.getElementById('btn-ttyd-stop').addEventListener('click', ttydStop);
    document.getElementById('btn-llama-switch').addEventListener('click', llamaSwitchModel);
    document.getElementById('btn-llama-unload').addEventListener('click', llamaUnloadForComfy);
    document.getElementById('btn-llama-reload').addEventListener('click', llamaReloadSelected);
    document.getElementById('btn-hermes-status').addEventListener('click', hermesStatus);
    document.getElementById('btn-prometheus-restart').addEventListener('click', prometheusRestart);
    document.getElementById('btn-prometheus-stop').addEventListener('click', prometheusStop);
    document.getElementById('btn-attendant-llama-stop').addEventListener('click', () => attendantStop('llama'));
    document.getElementById('btn-attendant-dream-stop').addEventListener('click', () => attendantStop('dream'));
    document.getElementById('btn-db-refresh').addEventListener('click', loadDbStorage);
    document.getElementById('btn-db-save').addEventListener('click', saveDbStorage);

    await loadDatabases();
    await loadDbStorage();
    await loadMeta();
    const saved = localStorage.getItem('loom-admin-view');
    if (saved && document.getElementById('view-' + saved)) setView(saved);
    else setView('overview');  // default view is overview
    refreshAll();
});
