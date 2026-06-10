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

// ── View switching ──────────────────────────────────────────────────
const VIEW_TITLES = { overview: 'Overview', servers: 'Servers', terminal: 'Terminal', tools: 'Tools', cron: 'Cron Jobs' };

function setView(name) {
    document.querySelectorAll('.nav-item').forEach(b => b.classList.toggle('active', b.dataset.view === name));
    document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + name));
    document.getElementById('view-title').textContent = VIEW_TITLES[name] || name;
    localStorage.setItem('loom-admin-view', name);
    if (name === 'overview' && !specsLoaded) loadSpecs();
    if (name === 'terminal') refreshTtyd();
    if (name === 'servers') loadLlamaModels();
}

// ── Llama switch-model control ──────────────────────────────────────
async function loadLlamaModels() {
    const sel = document.getElementById('llama-model-switch');
    if (!sel || sel.dataset.loaded === '1') return;
    try {
        const r = await fetch('/api/llama-models', { cache: 'no-store' });
        if (!r.ok) return;
        const d = await r.json();
        const loaded = (d.loaded || []).map(m => m.split(/[\\/]/).pop());
        sel.innerHTML = (d.models || []).map(m => {
            const marks = [];
            if (loaded.some(l => l === m)) marks.push('loaded');
            if (m === d.configured) marks.push('default');
            return `<option value="${esc(m)}"${m === d.configured ? ' selected' : ''}>${esc(m)}${marks.length ? ' — ' + marks.join(', ') : ''}</option>`;
        }).join('') || '<option value="">(no .gguf files found)</option>';
        sel.dataset.loaded = '1';
    } catch (e) { /* admin down or no models dir */ }
}

async function llamaSwitchModel() {
    const sel = document.getElementById('llama-model-switch');
    const model = sel ? sel.value : '';
    if (!model) { showToast('Pick a model first'); return; }
    if (!confirm('Restart llama-server with ' + model + '? Cold load takes ~30-90s.')) return;
    const btn = document.getElementById('btn-llama-switch');
    btn.disabled = true;
    await runTool('llama-restart?model=' + encodeURIComponent(model), { target: 'out-llama' });
    btn.disabled = false;
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
        await Promise.all([refreshInstances(), refreshGenerations(), refreshCronJobs(), refreshPorts()]);
    } catch (e) { /* keep ticking */ }
    scheduleRefresh();
}

async function refreshPorts() {
    let d = {};
    try {
        const r = await fetch('/api/ports-status', { cache: 'no-store' });
        if (r.ok) d = await r.json();
    } catch (e) { return; }
    const order = [['main', 'Loom'], ['test', 'Test'], ['llama', 'Llama'], ['nrol', 'NROL'], ['comfy', 'Comfy'], ['ttyd', 'ttyd']];
    document.getElementById('pulse-grid').innerHTML = order
        .map(([k, label]) => `<span class="pulse-item"><span class="dot ${d[k] ? 'on' : 'off'}"></span>${label}</span>`)
        .join('');
    for (const k of ['llama', 'comfy', 'nrol', 'ttyd']) {
        const dot = document.getElementById('dot-' + k);
        if (dot) dot.className = 'dot ' + (d[k] ? 'on' : 'off');
    }
    // Keep the terminal iframe in sync if ttyd died or came up elsewhere
    if (document.getElementById('view-terminal').classList.contains('active')) {
        const frame = document.getElementById('ttyd-frame');
        if (!d.ttyd && frame.classList.contains('live')) refreshTtyd();
        if (d.ttyd && !frame.classList.contains('live')) refreshTtyd();
    }
}

async function refreshInstances() {
    const r = await fetch('/api/status', { cache: 'no-store' });
    if (!r.ok) return;
    const d = await r.json();
    const host = document.getElementById('instance-cards');
    host.innerHTML = (d.instances || []).map(s => {
        const on = s.status === 'online';
        const managedTag = s.managed ? ' <span class="tag">managed</span>' : '';
        let actions = '';
        if (on) {
            actions = `<button onclick="doAction('${s.name}', 'restart')" class="btn btn-cyan">↻ Restart</button>
                       <button onclick="doAction('${s.name}', 'shutdown')" class="btn btn-warn">⏹ Shutdown</button>`;
        } else {
            actions = `<button onclick="doAction('${s.name}', 'start')" class="btn btn-green">▶ Start</button>`;
        }
        return `<div class="instance-card">
            <div class="instance-top"><span class="dot ${on ? 'on' : 'off'}"></span>
                <span class="instance-name">${esc(s.label)}</span>${managedTag}</div>
            <div class="instance-meta"><span>:${s.port}</span><span>${esc(s.db)}</span><span>${s.pid ? 'PID ' + s.pid : '—'}</span></div>
            <div class="instance-actions">${actions}</div>
        </div>`;
    }).join('');
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
    if (!confirm('Archive cron job #' + id + '?')) return;
    try {
        const r = await fetch('/api/cron-proxy/' + id, { method: 'DELETE' });
        showToast(r.ok ? 'cron archived' : 'cron archive failed');
    } catch (e) { showToast('cron archive failed: ' + e); }
    setTimeout(refreshCronJobs, 300);
}

async function killGen(draftId) {
    if (!confirm('Kill generation #' + draftId + '?')) return;
    showToast('killing #' + draftId);
    try {
        const r = await fetch('/api/generations-proxy/' + draftId + '/kill', { method: 'POST' });
        if (r.ok) { const d = await r.json(); showToast(d.status || 'killed'); }
        else showToast('kill failed');
    } catch (e) { showToast('kill failed: ' + e); }
    setTimeout(refreshAll, 500);
}

// ── Instance + admin actions ────────────────────────────────────────
async function doAction(name, action) {
    showToast(action + 'ing ' + name + '...');
    try {
        const r = await fetch('/action/' + name + '/' + action, { method: 'POST' });
        const d = await r.json();
        showToast(d.status || d.error || 'done');
    } catch (e) { showToast('failed: ' + e); }
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
function confirmTool(name, msg, opts) {
    if (confirm(msg)) runTool(name, opts);
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
    if (!confirm('Stop the web terminal?')) return;
    try {
        const r = await fetch('/tools/ttyd-stop', { method: 'POST' });
        const d = await r.json();
        showToast(d.output ? d.output.split('\n')[0] : 'stopped');
    } catch (e) { showToast('ttyd stop failed: ' + e); }
    setTimeout(refreshTtyd, 500);
}

// ── Boot ────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
    document.querySelectorAll('.nav-item').forEach(b =>
        b.addEventListener('click', () => setView(b.dataset.view)));
    document.getElementById('btn-admin-restart').addEventListener('click', () => adminAction('restart'));
    document.getElementById('btn-admin-shutdown').addEventListener('click', () => {
        if (confirm('Shut down the admin server?')) adminAction('shutdown');
    });
    document.getElementById('btn-specs-refresh').addEventListener('click', loadSpecs);
    document.getElementById('btn-ttyd-start').addEventListener('click', ttydStart);
    document.getElementById('btn-ttyd-stop').addEventListener('click', ttydStop);
    document.getElementById('btn-llama-switch').addEventListener('click', llamaSwitchModel);

    await loadMeta();
    const saved = localStorage.getItem('loom-admin-view');
    if (saved && document.getElementById('view-' + saved)) setView(saved);
    else loadSpecs();  // default view is overview
    refreshAll();
});
