/**
 * MCP Gateway - Frontend Application
 * Manages CRUD operations, live status, metrics, tool search, backup/restore.
 */
class MCPGatewayApp {
    constructor() {
        this.backends = [];
        this.tools = [];
        this.health = null;
        this.metrics = null;
        this.currentView = 'dashboard';
        this.deleteTarget = null;
        this.ws = null;
        this.init();
    }

    init() {
        this.bindEvents();
        this.initDarkMode();
        this.loadData();
        this.connectWebSocket();
        setInterval(() => this.loadData(), 30000);
    }

    // ─── Events ─────────────────────────────────────────────────────────

    bindEvents() {
        // Navigation
        document.querySelectorAll('.nav-item[data-view]').forEach(item => {
            item.addEventListener('click', (e) => { e.preventDefault(); this.switchView(item.dataset.view); });
        });

        // Header
        document.getElementById('refresh-all-btn').addEventListener('click', () => this.refreshAll());
        document.getElementById('dark-mode-toggle').addEventListener('click', () => this.toggleDarkMode());

        // Backends
        document.getElementById('add-backend-btn').addEventListener('click', () => this.openModal());
        document.getElementById('backup-btn').addEventListener('click', () => this.downloadBackup());
        document.getElementById('restore-btn').addEventListener('click', () => document.getElementById('restore-file-input').click());
        document.getElementById('restore-file-input').addEventListener('change', (e) => this.restoreFromFile(e));

        // Modal
        document.getElementById('modal-close').addEventListener('click', () => this.closeModal());
        document.getElementById('modal-cancel').addEventListener('click', () => this.closeModal());
        document.getElementById('modal-save').addEventListener('click', () => this.saveBackend());
        document.getElementById('backend-modal-overlay').addEventListener('click', (e) => { if (e.target === e.currentTarget) this.closeModal(); });
        document.getElementById('form-transport').addEventListener('change', (e) => this.toggleTransportFields(e.target.value));

        // Confirm
        document.getElementById('confirm-cancel').addEventListener('click', () => this.closeConfirm());
        document.getElementById('confirm-delete').addEventListener('click', () => this.executeDelete());
        document.getElementById('confirm-overlay').addEventListener('click', (e) => { if (e.target === e.currentTarget) this.closeConfirm(); });

        // Tool search
        document.getElementById('tool-search').addEventListener('input', (e) => this.filterTools(e.target.value));

        // Logs refresh
        document.getElementById('refresh-logs-btn').addEventListener('click', () => this.loadLogs());

        // Keyboard
        document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { this.closeModal(); this.closeConfirm(); } });
    }

    // ─── WebSocket Live Status ──────────────────────────────────────────

    connectWebSocket() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws/status`;
        
        try {
            this.ws = new WebSocket(wsUrl);
            this.ws.onopen = () => {
                document.getElementById('live-dot').classList.add('active');
            };
            this.ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                this.handleStatusEvent(data);
            };
            this.ws.onclose = () => {
                document.getElementById('live-dot').classList.remove('active');
                // Reconnect after 5s
                setTimeout(() => this.connectWebSocket(), 5000);
            };
            this.ws.onerror = () => {
                document.getElementById('live-dot').classList.remove('active');
            };
        } catch (e) {
            console.warn('WebSocket not available:', e);
        }
    }

    handleStatusEvent(data) {
        if (data.type === 'status_change') {
            const name = data.backend;
            const connected = data.connected;
            if (connected === null) {
                // Backend removed
                this.showToast(`Backend "${name}" removed`, 'info');
            } else if (connected) {
                this.showToast(`Backend "${name}" connected`, 'success');
            } else {
                this.showToast(`Backend "${name}" disconnected`, 'warning');
            }
            // Refresh data
            this.loadData();
        } else if (data.type === 'init') {
            // Initial state from WebSocket
        }
    }

    // ─── Dark Mode ──────────────────────────────────────────────────────

    initDarkMode() {
        if (localStorage.getItem('mcp-gateway-theme') === 'dark') {
            document.documentElement.setAttribute('data-theme', 'dark');
            this.updateDarkModeIcon(true);
        }
    }

    toggleDarkMode() {
        const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
        document.documentElement.toggleAttribute('data-theme', !isDark);
        if (!isDark) { document.documentElement.setAttribute('data-theme', 'dark'); }
        else { document.documentElement.removeAttribute('data-theme'); }
        localStorage.setItem('mcp-gateway-theme', isDark ? 'light' : 'dark');
        this.updateDarkModeIcon(!isDark);
    }

    updateDarkModeIcon(isDark) {
        document.querySelector('#dark-mode-toggle i').className = isDark ? 'fas fa-sun' : 'fas fa-moon';
    }

    // ─── Navigation ─────────────────────────────────────────────────────

    switchView(view) {
        this.currentView = view;
        document.querySelectorAll('.nav-item[data-view]').forEach(item => item.classList.toggle('active', item.dataset.view === view));
        ['dashboard', 'backends', 'tools', 'logs', 'settings'].forEach(v => {
            const el = document.getElementById(`view-${v}`);
            if (el) el.style.display = v === view ? '' : 'none';
        });
        if (view === 'tools') this.renderTools();
        if (view === 'logs') this.loadLogs();
    }

    // ─── API Calls ──────────────────────────────────────────────────────

    async loadData() {
        try {
            const [healthRes, backendsRes, metricsRes, toolsRes] = await Promise.all([
                fetch('/health'), fetch('/admin/backends'), fetch('/admin/metrics'), fetch('/admin/tools'),
            ]);
            this.health = await healthRes.json();
            this.backends = (await backendsRes.json()).backends || [];
            this.metrics = await metricsRes.json();
            this.tools = (await toolsRes.json()).tools || [];
            this.render();
        } catch (err) {
            console.error('Load data failed:', err);
        }
    }

    async addBackend(payload) {
        const res = await fetch('/admin/backends', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        const data = await res.json();
        if (!res.ok && !data.status) throw new Error(data.error || `HTTP ${res.status}`);
        return data;
    }

    async removeBackend(name) {
        const res = await fetch(`/admin/backends/${encodeURIComponent(name)}`, { method: 'DELETE' });
        if (!res.ok) { const d = await res.json(); throw new Error(d.error || `HTTP ${res.status}`); }
        return await res.json();
    }

    async refreshAll() {
        const btn = document.getElementById('refresh-all-btn');
        btn.disabled = true; btn.innerHTML = '<span class="loading-spinner"></span> Refreshing...';
        try { await fetch('/admin/refresh', { method: 'POST' }); await this.loadData(); this.showToast('All backends refreshed', 'success'); }
        catch (err) { this.showToast('Refresh failed', 'error'); }
        finally { btn.disabled = false; btn.innerHTML = '<i class="fas fa-sync-alt"></i> Refresh'; }
    }

    async loadLogs() {
        try {
            const res = await fetch('/admin/logs?limit=100');
            const data = await res.json();
            this.renderLogs(data.logs || []);
        } catch (err) { console.error(err); }
    }

    // ─── Backup & Restore ───────────────────────────────────────────────

    async downloadBackup() {
        try {
            const res = await fetch('/admin/backup');
            const data = await res.json();
            const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a'); a.href = url;
            a.download = `mcp-gateway-backup-${new Date().toISOString().slice(0,10)}.json`;
            a.click(); URL.revokeObjectURL(url);
            this.showToast('Backup downloaded', 'success');
        } catch (err) { this.showToast('Backup failed', 'error'); }
    }

    async restoreFromFile(event) {
        const file = event.target.files[0];
        if (!file) return;
        try {
            const text = await file.text();
            const backup = JSON.parse(text);
            const res = await fetch('/admin/restore', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(backup) });
            const result = await res.json();
            if (res.ok) { await this.loadData(); this.showToast(`Restored ${result.backends} backends`, 'success'); }
            else { this.showToast(result.error || 'Restore failed', 'error'); }
        } catch (err) { this.showToast(`Restore error: ${err.message}`, 'error'); }
        event.target.value = '';
    }

    // ─── Rendering ──────────────────────────────────────────────────────

    render() {
        this.renderStats();
        this.renderDashboardBackends();
        this.renderBackendsList();
        if (this.currentView === 'tools') this.renderTools();
    }

    renderStats() {
        const total = this.backends.length;
        const connected = this.backends.filter(b => b.connected).length;
        const tools = this.health ? this.health.tools : 0;
        const calls = this.metrics ? this.metrics.total_calls : 0;
        document.getElementById('stat-backends').textContent = total;
        document.getElementById('stat-connected').textContent = connected;
        document.getElementById('stat-tools').textContent = tools;
        document.getElementById('stat-calls').textContent = calls;
    }

    renderDashboardBackends() {
        const container = document.getElementById('dashboard-backends');
        if (!this.backends.length) {
            container.innerHTML = `<div class="empty-state" style="grid-column: 1/-1;"><i class="fas fa-server"></i><h3>No backends configured</h3><p>Add your first MCP server to get started.</p><button type="button" class="btn btn-primary" style="margin-top:1rem;" onclick="app.switchView('backends');app.openModal();"><i class="fas fa-plus"></i> Add Backend</button></div>`;
            return;
        }
        container.innerHTML = this.backends.map(b => this.renderBackendCard(b, false)).join('');
    }

    renderBackendsList() {
        const container = document.getElementById('backends-list');
        if (!this.backends.length) {
            container.innerHTML = `<div class="empty-state"><i class="fas fa-server"></i><h3>No backends configured</h3><p>Click "Add Backend" to register your first MCP server.</p></div>`;
            return;
        }
        container.innerHTML = this.backends.map(b => this.renderBackendCard(b, true)).join('');
    }

    renderBackendCard(backend, showActions = true) {
        const statusClass = backend.connected ? 'connected' : 'disconnected';
        const transportBadge = backend.transport === 'http' ? '<span class="badge badge-http">HTTP</span>'
            : backend.transport === 'sse' ? '<span class="badge badge-sse">SSE</span>'
            : '<span class="badge badge-stdio">STDIO</span>';
        const lazyBadge = backend.lazy ? '<span class="badge badge-lazy">Lazy</span>' : '<span class="badge badge-eager">Eager</span>';
        const endpoint = backend.url || `${backend.command || ''} ${(backend.args || []).join(' ')}`.trim();
        const m = backend.metrics || {};
        const metricsHtml = m.total_calls > 0 ? `<span class="tools-count"><i class="fas fa-exchange-alt"></i> ${m.total_calls} calls, ${m.avg_latency_ms}ms avg</span>` : '';
        const filterHtml = backend.tool_filter ? '<span class="badge badge-filter"><i class="fas fa-filter"></i> Filtered</span>' : '';
        const authHtml = backend.headers && Object.keys(backend.headers).length ? '<span class="badge badge-auth"><i class="fas fa-lock"></i></span>' : '';

        const actionsHtml = showActions ? `<div class="backend-card-actions"><button type="button" class="btn-icon" onclick="app.openEditModal('${backend.name}')" title="Edit"><i class="fas fa-pen"></i></button><button type="button" class="btn-icon danger" onclick="app.confirmDelete('${backend.name}')" title="Remove"><i class="fas fa-trash"></i></button></div>` : '';

        return `<div class="backend-card"><div class="backend-card-header"><div class="backend-card-title"><span class="backend-status-dot ${statusClass}"></span><h3>${this.esc(backend.name)}</h3>${authHtml}</div>${actionsHtml}</div><div class="backend-card-meta">${backend.description ? `<div class="meta-row"><i class="fas fa-info-circle"></i> ${this.esc(backend.description)}</div>` : ''}<div class="meta-row"><i class="fas fa-link"></i> ${this.esc(endpoint)}</div></div><div class="backend-card-footer"><div>${transportBadge} ${lazyBadge} ${filterHtml}</div><span class="tools-count"><i class="fas fa-wrench"></i> ${backend.tools || 0} tools</span></div>${metricsHtml ? `<div class="backend-card-metrics">${metricsHtml}</div>` : ''}</div>`;
    }

    // ─── Tools View with Search ─────────────────────────────────────────

    renderTools() {
        const query = (document.getElementById('tool-search').value || '').toLowerCase();
        this.filterTools(query);
    }

    filterTools(query) {
        query = query.toLowerCase();
        const container = document.getElementById('tools-list');
        const filtered = query ? this.tools.filter(t => t.name.toLowerCase().includes(query) || (t.description || '').toLowerCase().includes(query) || (t.backend || '').toLowerCase().includes(query)) : this.tools;

        if (!filtered.length) {
            container.innerHTML = `<div class="empty-state"><i class="fas fa-wrench"></i><h3>${query ? 'No matching tools' : 'No tools registered'}</h3><p>${query ? 'Try a different search term.' : 'Tools will appear once backends are connected.'}</p></div>`;
            return;
        }

        // Group by backend
        const groups = {};
        filtered.forEach(t => { (groups[t.backend] = groups[t.backend] || []).push(t); });

        let html = '';
        for (const [backend, tools] of Object.entries(groups)) {
            const b = this.backends.find(x => x.name === backend);
            const dot = b && b.connected ? 'connected' : 'disconnected';
            html += `<div class="tool-group"><div class="tool-group-header"><span class="backend-status-dot ${dot}"></span><strong>${this.esc(backend)}</strong><span class="tools-count">${tools.length} tools</span></div>`;
            html += '<div class="tool-list">';
            tools.forEach(t => {
                html += `<div class="tool-item"><span class="tool-name">${this.esc(t.name)}</span><span class="tool-desc">${this.esc(t.description || '')}</span></div>`;
            });
            html += '</div></div>';
        }
        container.innerHTML = html;
    }

    // ─── Logs View ──────────────────────────────────────────────────────

    renderLogs(logs) {
        const container = document.getElementById('logs-container');
        if (!logs.length) {
            container.innerHTML = `<div class="empty-state"><i class="fas fa-list-alt"></i><h3>No activity yet</h3><p>Tool calls will appear here as they happen.</p></div>`;
            return;
        }

        let html = '<div class="log-table"><div class="log-header"><span>Time</span><span>Tool</span><span>Backend</span><span>Latency</span><span>Status</span></div>';
        logs.forEach(log => {
            const time = new Date(log.timestamp * 1000).toLocaleTimeString();
            const statusCls = log.success ? 'log-success' : 'log-error';
            const statusIcon = log.success ? '<i class="fas fa-check-circle"></i>' : '<i class="fas fa-times-circle"></i>';
            html += `<div class="log-row ${statusCls}"><span>${time}</span><span class="log-tool">${this.esc(log.tool)}</span><span>${this.esc(log.backend)}</span><span>${log.latency_ms}ms</span><span>${statusIcon}</span></div>`;
        });
        html += '</div>';
        container.innerHTML = html;
    }

    // ─── Modal ──────────────────────────────────────────────────────────

    openModal() {
        this.resetForm();
        document.getElementById('modal-title').textContent = 'Add Backend';
        document.getElementById('backend-modal-overlay').classList.add('active');
        document.getElementById('form-name').focus();
    }

    openEditModal(name) {
        const backend = this.backends.find(b => b.name === name);
        if (!backend) return;
        this.resetForm();
        document.getElementById('modal-title').textContent = 'Edit Backend';
        document.getElementById('form-editing-name').value = name;
        document.getElementById('form-name').value = backend.name;
        document.getElementById('form-name').disabled = true;
        document.getElementById('form-description').value = backend.description || '';
        document.getElementById('form-lazy').checked = backend.lazy !== false;
        document.getElementById('form-transport').value = backend.transport || 'http';
        this.toggleTransportFields(backend.transport || 'http');

        if (backend.transport === 'stdio') {
            document.getElementById('form-command').value = backend.command || '';
        } else {
            document.getElementById('form-url').value = backend.url || '';
        }

        // Auth
        if (backend.headers && backend.headers.Authorization) {
            document.getElementById('form-auth-header').value = backend.headers.Authorization;
        }

        // Tool filter
        if (backend.tool_filter) {
            document.getElementById('form-tools-include').value = (backend.tool_filter.include || []).join(', ');
            document.getElementById('form-tools-exclude').value = (backend.tool_filter.exclude || []).join(', ');
        }

        document.getElementById('backend-modal-overlay').classList.add('active');
    }

    closeModal() { document.getElementById('backend-modal-overlay').classList.remove('active'); this.resetForm(); }

    resetForm() {
        document.getElementById('backend-form').reset();
        document.getElementById('form-editing-name').value = '';
        document.getElementById('form-name').disabled = false;
        document.getElementById('form-lazy').checked = true;
        this.toggleTransportFields('http');
    }

    toggleTransportFields(transport) {
        document.getElementById('http-fields').style.display = (transport === 'http' || transport === 'sse') ? '' : 'none';
        document.getElementById('stdio-fields').style.display = transport === 'stdio' ? '' : 'none';
    }

    async saveBackend() {
        const editingName = document.getElementById('form-editing-name').value;
        const name = document.getElementById('form-name').value.trim();
        const transport = document.getElementById('form-transport').value;
        const description = document.getElementById('form-description').value.trim();
        const lazy = document.getElementById('form-lazy').checked;

        if (!name) { this.showToast('Name is required', 'warning'); return; }

        const payload = { name, description, lazy, transport };

        if (transport === 'http' || transport === 'sse') {
            const url = document.getElementById('form-url').value.trim();
            if (!url) { this.showToast('URL is required', 'warning'); return; }
            payload.url = url;
        } else {
            const command = document.getElementById('form-command').value.trim();
            if (!command) { this.showToast('Command is required', 'warning'); return; }
            payload.command = command;
            const argsStr = document.getElementById('form-args').value.trim();
            payload.args = argsStr ? argsStr.split(',').map(s => s.trim()).filter(Boolean) : [];
        }

        // Auth header
        const authHeader = document.getElementById('form-auth-header').value.trim();
        if (authHeader) { payload.headers = { Authorization: authHeader }; }

        // Tool filter
        const includeStr = document.getElementById('form-tools-include').value.trim();
        const excludeStr = document.getElementById('form-tools-exclude').value.trim();
        if (includeStr || excludeStr) {
            payload.tools = {};
            if (includeStr) payload.tools.include = includeStr.split(',').map(s => s.trim()).filter(Boolean);
            if (excludeStr && !includeStr) payload.tools.exclude = excludeStr.split(',').map(s => s.trim()).filter(Boolean);
        }

        const saveBtn = document.getElementById('modal-save');
        saveBtn.disabled = true; saveBtn.innerHTML = '<span class="loading-spinner"></span> Saving...';

        try {
            if (editingName) await this.removeBackend(editingName);
            const result = await this.addBackend(payload);
            this.closeModal(); await this.loadData();
            if (result.status === 'connected') { this.showToast(`"${name}" ${editingName ? 'updated' : 'added'} — ${result.tools_discovered} tools`, 'success'); }
            else { this.showToast(`"${name}" added but failed: ${result.error || 'unknown'}`, 'warning'); }
        } catch (err) { this.showToast(`Error: ${err.message}`, 'error'); }
        finally { saveBtn.disabled = false; saveBtn.innerHTML = '<i class="fas fa-save"></i> Save'; }
    }

    // ─── Delete ─────────────────────────────────────────────────────────

    confirmDelete(name) {
        this.deleteTarget = name;
        document.getElementById('confirm-message').textContent = `Remove "${name}"? All its tools will be unregistered.`;
        document.getElementById('confirm-overlay').classList.add('active');
    }

    closeConfirm() { document.getElementById('confirm-overlay').classList.remove('active'); this.deleteTarget = null; }

    async executeDelete() {
        if (!this.deleteTarget) return;
        const name = this.deleteTarget; this.closeConfirm();
        try { await this.removeBackend(name); await this.loadData(); this.showToast(`"${name}" removed`, 'success'); }
        catch (err) { this.showToast(`Failed: ${err.message}`, 'error'); }
    }

    // ─── Toast ──────────────────────────────────────────────────────────

    showToast(message, type = 'info') {
        const container = document.getElementById('toast-container');
        const icons = { success: 'fa-check-circle', error: 'fa-times-circle', warning: 'fa-exclamation-circle', info: 'fa-info-circle' };
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        toast.innerHTML = `<i class="fas ${icons[type] || icons.info}"></i> <span>${this.esc(message)}</span>`;
        container.appendChild(toast);
        setTimeout(() => { toast.classList.add('removing'); setTimeout(() => toast.remove(), 300); }, 4000);
    }

    // ─── Utility ────────────────────────────────────────────────────────

    esc(str) { const d = document.createElement('div'); d.textContent = str || ''; return d.innerHTML; }
}

const app = new MCPGatewayApp();
