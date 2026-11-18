// Etsy Orders page controller: state, filters (synced to URL), account toolbar,
// Poll Now / Sync Sheet background jobs, deletes, and a non-overlapping refresh loop.
// Depends on: shared-ui-theme-toast-fetch-helpers.js, app-sidebar-accounts-and-mobile-menu.js,
// etsy-orders-data-normalize-and-filter.js, etsy-orders-table-html-builders.js, etsy-order-detail-drawer.js

(function () {
    const DEFAULT_PAGE_SIZE = 25;
    const PAGE_SIZE_KEY = 'etsy-page-size';
    const REFRESH_IDLE_MS = 30000;   // normal auto-refresh
    const REFRESH_JOB_MS = 2000;     // while a Poll/Sync job runs

    const $ = id => document.getElementById(id);

    /** Valid page size from any source, else null. */
    function parsePageSize(value) {
        const n = parseInt(value, 10);
        return EtsyOrdersTable.PAGE_SIZES.includes(n) ? n : null;
    }
    function savedPageSize() {
        try { return parsePageSize(localStorage.getItem(PAGE_SIZE_KEY)); } catch (e) { return null; }
    }

    const state = {
        account: '',
        accounts: [],
        accountsSig: '',
        orders: [],                  // current page only (server filters, sorts, paginates)
        total: 0,                    // orders matching the filters
        totalAll: 0,                 // orders of the account (or all) without filters
        pages: 1,
        ordersVersion: null,
        loadedAccount: null,         // account the current `orders` belong to
        loadSeq: 0,                  // ignores responses of superseded requests
        filters: { search: '', from: '', to: '', buyerKey: '' },
        page: 1,
        pageSize: DEFAULT_PAGE_SIZE,
        openKey: null,               // order shown in the detail drawer
        openOrder: null,             // its data (kept while it is off the current page)
        selected: new Set(),
        job: null,                   // last seen job object from /api/etsy/status
        logsSig: '',
        loadError: false
    };

    // ---------- URL state (deep link + survives reload) ----------
    function readUrlState() {
        const q = new URLSearchParams(location.search);
        state.account = q.get('account') || '';
        state.filters.search = q.get('q') || '';
        state.filters.from = q.get('from') || '';
        state.filters.to = q.get('to') || '';
        state.filters.buyerKey = q.get('buyer') || '';
        state.page = Math.max(1, parseInt(q.get('page'), 10) || 1);
        // URL wins (shared link), then this browser's last choice, then the default
        state.pageSize = parsePageSize(q.get('size')) || savedPageSize() || DEFAULT_PAGE_SIZE;
    }

    function writeUrlState() {
        const q = new URLSearchParams();
        if (state.account) q.set('account', state.account);
        if (state.filters.search) q.set('q', state.filters.search);
        if (state.filters.from) q.set('from', state.filters.from);
        if (state.filters.to) q.set('to', state.filters.to);
        if (state.filters.buyerKey) q.set('buyer', state.filters.buyerKey);
        if (state.page > 1) q.set('page', state.page);
        if (state.pageSize !== DEFAULT_PAGE_SIZE) q.set('size', state.pageSize);
        const qs = q.toString();
        history.replaceState(null, '', qs ? '?' + qs : location.pathname);
    }

    // ---------- Accounts toolbar + manage panel ----------
    function shortEmail(email) { return email.split('@')[0]; }

    function renderAccounts() {
        const sig = JSON.stringify([state.accounts, state.account]);
        if (sig === state.accountsSig) return; // nothing changed: skip DOM work
        state.accountsSig = sig;

        // Sidebar: accounts act as the account filter on this page
        AppSidebar.renderAccounts(
            state.accounts.map(a => ({ email: a.email, on: a.etsy_polling_enabled, count: a.order_count })),
            { current: state.account, asButtons: true });

        $('acc-list').innerHTML = state.accounts.map(acc => `
            <div class="acc-row" data-email="${esc(acc.email)}">
                <span class="email" title="${esc(acc.email)}">${esc(acc.email)}</span>
                <span class="meta">${acc.order_count} đơn</span>
                <div class="actions">
                    <label class="switch" title="Tự động poll theo chu kỳ">
                        <input type="checkbox" data-action="toggle-auto" ${acc.etsy_polling_enabled ? 'checked' : ''}>
                        <span class="track" aria-hidden="true"></span>Tự poll
                    </label>
                    <button type="button" class="btn small ghost danger" data-action="delete-account-orders">Xoá hết đơn</button>
                </div>
            </div>`).join('') || '<p class="helper">Chưa có tài khoản. Thêm ở Cấu hình.</p>';

        updateActionLabels();
        updateQueryRow();
    }

    function updateActionLabels() {
        const who = state.account ? shortEmail(state.account) : 'tất cả';
        $('btn-poll').title = `Lấy đơn mới từ Gmail ngay (${who})`;
        $('btn-sync').title = `Ghi đơn chưa có lên Google Sheet (${who})`;
    }

    function updateQueryRow() {
        const acc = state.accounts.find(a => a.email === state.account);
        $('query-row').hidden = !acc;
        // Never overwrite what the user is typing
        if (acc && document.activeElement !== $('query-input')) $('query-input').value = acc.etsy_query || '';
    }

    async function selectAccount(email) {
        if (email === state.account) return;
        state.account = email;
        state.page = 1;
        state.filters.buyerKey = '';
        state.openKey = null;
        state.openOrder = null;
        EtsyOrderDrawer.render(null);
        renderAccounts();
        writeUrlState();
        await loadOrders(true);
    }

    // ---------- Data loading ----------
    async function loadStatus() {
        try {
            const { data, ok } = await fetchJSON('/api/etsy/status');
            if (!ok) return null;
            state.accounts = data.accounts || [];
            AppSidebar.setPollInterval(data.polling_interval);
            renderAccounts();
            handleJob(data.job);
            return data;
        } catch (e) {
            return null;
        }
    }

    /** Filter params shared by the page query and the "select all" keys query. */
    function filterParams() {
        const q = new URLSearchParams();
        if (state.account) q.set('account', state.account);
        if (state.filters.search) q.set('q', state.filters.search);
        if (state.filters.from) q.set('from', state.filters.from);
        if (state.filters.to) q.set('to', state.filters.to);
        if (state.filters.buyerKey) q.set('buyer', state.filters.buyerKey);
        return q;
    }

    /** Fetch the current page (filters + page + size) from the server and render it. */
    async function loadOrders(showSkeleton) {
        if (showSkeleton) {
            $('orders-tbody').innerHTML = EtsyOrdersTable.skeletonHtml();
            $('empty-state').hidden = true;
        }
        const seq = ++state.loadSeq;
        const account = state.account;
        const q = filterParams();
        q.set('page', state.page);
        q.set('size', state.pageSize);
        try {
            const { data, ok } = await fetchJSON('/api/etsy/orders?' + q.toString(), {}, 30000);
            if (!ok) throw new Error('HTTP error');
            if (seq !== state.loadSeq) return; // a newer request (filter/page change) superseded this one
            state.orders = EtsyOrdersData.normalizePage(data.orders);
            state.total = data.total;
            state.totalAll = data.total_all;
            state.page = data.page;          // server clamps out-of-range pages
            state.pages = data.pages;
            state.loadedAccount = account;
            if (data.version !== undefined) state.ordersVersion = data.version;
            // Selected buyer has no order in the new account/date range: server dropped it
            if (state.filters.buyerKey && data.buyer_key == null) state.filters.buyerKey = '';
            state.loadError = false;
            renderBuyerSelect(data.buyers || [], data.buyer_count || 0);
            renderSummary(data);
        } catch (e) {
            if (seq !== state.loadSeq) return;
            state.loadError = true;
            if (!state.orders.length) showToast('Không tải được đơn hàng', 'error');
        }
        renderTable();
        writeUrlState();
    }

    /** Filters changed: back to page 1 and refetch. */
    function reloadFirstPage() {
        state.page = 1;
        loadOrders(false);
    }

    async function loadLogs() {
        try {
            const { data } = await fetchJSON('/api/status');
            const logs = data.logs || [];
            const sig = logs.map(l => l.text).join('\n');
            if (sig === state.logsSig) return;
            state.logsSig = sig;
            const box = $('log-box');
            const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
            box.innerHTML = logs.map(l => `<div class="${esc(l.level)}">${esc(l.text)}</div>`).join('');
            if (nearBottom) box.scrollTop = box.scrollHeight;
            const lastErr = [...logs].reverse().find(l => l.level === 'ERROR');
            $('log-last-error').textContent = lastErr ? '· Lỗi gần nhất: ' + lastErr.text : '';
        } catch (e) { /* logs are non-critical */ }
    }

    // ---------- Rendering (data comes pre-filtered from the server) ----------
    /** Buyer dropdown: server sends the top buyers (by order count) + the selected one. */
    function renderBuyerSelect(buyers, buyerCount) {
        const sel = $('buyer-select');
        const more = buyerCount > buyers.length ? ` · top ${buyers.length}` : '';
        sel.innerHTML = `<option value="">Tất cả buyer (${buyerCount}${more})</option>`
            + buyers.filter(b => b.key).map(b => `<option value="${esc(b.key)}">${esc(b.label)} (${b.count})</option>`).join('');
        sel.value = state.filters.buyerKey;
    }

    function hasActiveFilters() {
        const f = state.filters;
        return !!(f.search || f.from || f.to || f.buyerKey);
    }

    function renderSummary(result) {
        const total = result.total_all;
        const shown = result.total;
        const unsynced = result.unsynced;
        const who = state.account ? shortEmail(state.account) : 'tất cả tài khoản';
        let head = `<span class="live"></span><b>${shown}</b> đơn · <b>${result.items}</b> sản phẩm`;
        if (shown !== total) head += ` (trên tổng ${total})`;
        head += ` · ${esc(who)}`;
        if (unsynced) head += ` <span class="pill warn">${unsynced} chưa ghi Sheet</span>`;
        $('head-summary').innerHTML = head;

        let html = '';
        if (result.invalid_range) html += '<span class="warn">Khoảng ngày không hợp lệ (Từ sau Đến), đang bỏ qua lọc ngày.</span>';
        if (hasActiveFilters()) html += '<button type="button" class="link-btn" data-action="reset-filters">Xoá bộ lọc</button>';
        $('filter-summary').innerHTML = html;
    }

    function renderTable() {
        const tbody = $('orders-tbody');
        const empty = $('empty-state');
        const list = state.orders;

        if (!list.length) {
            tbody.innerHTML = '';
            empty.innerHTML = EtsyOrdersTable.emptyHtml(
                state.loadError && !state.totalAll ? 'error' : (state.totalAll ? 'no-match' : 'none'));
            empty.hidden = false;
        } else {
            empty.hidden = true;
            tbody.innerHTML = EtsyOrdersTable.rowsHtml(list, state.selected, state.openKey);
        }

        // Pager stays visible whenever there are orders, so the page size can always be changed
        const pager = $('pager');
        pager.hidden = !state.total;
        pager.innerHTML = state.total ? EtsyOrdersTable.pagerHtml(state.page, state.pages, state.total, state.pageSize) : '';
        updateSelectBar();
        syncDrawer();
    }

    async function goToPage(page) {
        state.page = Math.min(Math.max(1, page), state.pages);
        await loadOrders(false);
        $('table-wrap').scrollTop = 0;
        $('table-wrap').scrollIntoView({ block: 'nearest' });
    }

    function setPageSize(size) {
        const valid = parsePageSize(size);
        if (!valid) return;
        // Keep the first visible order on screen after the change
        const firstIndex = (state.page - 1) * state.pageSize;
        state.pageSize = valid;
        state.pages = Math.max(1, Math.ceil(state.total / valid));
        try { localStorage.setItem(PAGE_SIZE_KEY, String(valid)); } catch (e) {}
        goToPage(Math.floor(firstIndex / valid) + 1);
    }

    // ---------- Detail drawer ----------
    function openOrder(key) {
        state.openKey = state.openKey === key ? null : key;
        state.openOrder = state.openKey ? state.orders.find(o => o._key === key) || null : null;
        document.querySelectorAll('#orders-tbody tr[data-key]').forEach(tr => tr.classList.toggle('open', tr.dataset.key === state.openKey));
        syncDrawer();
    }

    function closeDrawer() {
        if (!state.openKey) return;
        const key = state.openKey;
        state.openKey = null;
        state.openOrder = null;
        document.querySelectorAll('#orders-tbody tr.open').forEach(tr => tr.classList.remove('open'));
        syncDrawer();
        const row = document.querySelector(`#orders-tbody tr[data-key="${CSS.escape(key)}"] .order-id`);
        if (row) row.focus({ preventScroll: true });
    }

    /** Re-render the drawer: fresh data when the order is on this page, else the cached copy. */
    function syncDrawer() {
        if (state.openKey) {
            const fresh = state.orders.find(o => o._key === state.openKey);
            if (fresh) state.openOrder = fresh;
        }
        if (!state.openOrder) state.openKey = null;
        EtsyOrderDrawer.render(state.openOrder);
    }

    function resetFilters() {
        state.filters = { search: '', from: '', to: '', buyerKey: '' };
        syncFilterInputs();
        reloadFirstPage();
    }

    function syncFilterInputs() {
        $('search-input').value = state.filters.search;
        $('from-date').value = state.filters.from;
        $('to-date').value = state.filters.to;
    }

    // ---------- Selection ----------
    function updateSelectBar() {
        const n = state.selected.size;
        $('select-bar').classList.toggle('visible', n > 0);
        $('select-count').textContent = n + ' đơn đã chọn';
        $('btn-select-all').textContent = `Chọn tất cả ${state.total} đơn đang lọc`;
        // Header checkbox reflects (and toggles) the visible page
        const allCb = $('select-all-cb');
        const selInView = state.orders.filter(o => state.selected.has(o._key)).length;
        allCb.checked = state.orders.length > 0 && selInView === state.orders.length;
        allCb.indeterminate = selInView > 0 && selInView < state.orders.length;
    }

    function setSelected(key, checked) {
        if (checked) state.selected.add(key); else state.selected.delete(key);
        document.querySelectorAll(`#orders-tbody tr[data-key="${CSS.escape(key)}"]`)
            .forEach(tr => tr.classList.toggle('selected', checked));
        updateSelectBar();
    }

    function markRowsSelected() {
        document.querySelectorAll('#orders-tbody tr[data-key]').forEach(tr => {
            const on = state.selected.has(tr.dataset.key);
            tr.classList.toggle('selected', on);
            const cb = tr.querySelector('input.cb');
            if (cb) cb.checked = on;
        });
        updateSelectBar();
    }

    /** Header checkbox: select / unselect the orders on the current page. */
    function setPageSelected(checked) {
        state.orders.forEach(o => { if (checked) state.selected.add(o._key); else state.selected.delete(o._key); });
        markRowsSelected();
    }

    /** "Select all N filtered": the keys of every matching order come from the server. */
    async function selectAllFiltered() {
        const btn = $('btn-select-all');
        btn.disabled = true;
        try {
            const { data, ok } = await fetchJSON('/api/etsy/orders/keys?' + filterParams().toString(), {}, 30000);
            if (!ok) throw new Error('HTTP error');
            (data.keys || []).forEach(([acc, id]) => state.selected.add(acc + '::' + id));
            markRowsSelected();
        } catch (e) {
            showToast('Không chọn được tất cả đơn: ' + e.message, 'error');
        } finally {
            btn.disabled = false;
        }
    }

    function clearSelection() { state.selected.clear(); renderTable(); }

    // ---------- Background jobs (Poll Now / Sync Sheet) ----------
    function jobLabel(job) {
        const what = job.kind === 'sync_sheet' ? 'Đang sync Sheet' : 'Đang poll Gmail';
        const who = job.target ? ' · ' + shortEmail(job.target) : '';
        const secs = job.started_at ? Math.max(0, Math.round(Date.now() / 1000 - job.started_at)) : 0;
        return `${what}${who}… ${secs}s`;
    }

    /** Update UI from the server job state; announce when a running job finishes. */
    function handleJob(job) {
        const prev = state.job;
        state.job = job || null;
        const running = !!(job && job.state === 'running');
        $('btn-poll').disabled = running;
        $('btn-sync').disabled = running;
        const status = $('job-status');
        status.classList.toggle('running', running);
        status.textContent = running ? jobLabel(job) : '';

        if (prev && prev.state === 'running' && job && job.state !== 'running') {
            announceJobResult(job);
            loadOrders(false);
            loadLogs();
        }
    }

    function announceJobResult(job) {
        if (job.state === 'error') {
            showToast('Lỗi: ' + (job.error || 'không rõ'), 'error');
            return;
        }
        const r = job.result || {};
        if (r.error) { showToast('Lỗi: ' + r.error, 'error'); return; }
        if (job.kind === 'sync_sheet') {
            const msg = `Sync xong: ghi ${r.written || 0} dòng mới, bỏ qua ${r.skipped || 0} đơn đã có.`;
            if (r.errors && r.errors.length) showToast(msg + '\nLỗi:\n' + r.errors.join('\n'), 'warning', 12000);
            else showToast(msg, 'success');
        } else {
            const n = r.new_orders || 0;
            showToast(n ? `Poll xong: +${n} đơn mới.` : 'Poll xong: không có đơn mới.', n ? 'success' : 'info');
        }
    }

    async function startJob(kind) {
        let url, options = { method: 'POST' };
        if (kind === 'poll') {
            const body = {};
            if ($('poll-from-date').value) body.after_date = $('poll-from-date').value;
            if ($('poll-to-date').value) body.before_date = $('poll-to-date').value;
            url = state.account ? '/api/etsy/poll-now/' + encodeURIComponent(state.account) : '/api/etsy/poll-now';
            options = { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
        } else {
            url = '/api/etsy/sync-sheet' + (state.account ? '?account=' + encodeURIComponent(state.account) : '');
        }
        $('btn-poll').disabled = true;
        $('btn-sync').disabled = true;
        try {
            const res = await fetchJSON(url, options);
            if (res.status === 202) {
                // Remember we started it, so even a job that finishes before the next
                // status check is announced (handleJob sees running -> done)
                state.job = { kind: kind === 'poll' ? 'poll' : 'sync_sheet', state: 'running',
                    target: state.account || null, started_at: Date.now() / 1000 };
            } else if (res.status === 409) showToast('Đang có poll/sync khác chạy, vui lòng đợi.', 'warning');
            else showToast('Lỗi: ' + (res.data.error || 'không bắt đầu được'), 'error');
        } catch (e) {
            showToast('Lỗi kết nối tới server', 'error');
        }
        refreshNow(); // picks up the running job and switches to fast refresh
    }

    // ---------- Mutations ----------
    async function deleteOrders(ordersByAccount, count) {
        try {
            const { data } = await fetchJSON('/api/etsy/orders/bulk', {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ orders: ordersByAccount, clear_processed: true })
            });
            if (!data.success) throw new Error(data.error || 'không rõ');
            Object.entries(ordersByAccount).forEach(([acc, ids]) => ids.forEach(id => {
                state.selected.delete(acc + '::' + id);
                if (state.openKey === acc + '::' + id) { state.openKey = null; state.openOrder = null; }
            }));
            showToast(`Đã xoá ${data.deleted ?? count} đơn. Có thể poll lại từ Gmail.`, 'success');
            await reloadAfterMutation();
        } catch (e) {
            showToast('Xoá thất bại: ' + e.message, 'error');
        }
    }

    function deleteOneOrder(key) {
        const order = state.orders.find(o => o._key === key) || (state.openOrder && state.openOrder._key === key ? state.openOrder : null);
        if (!order || !confirm(`Xoá đơn #${order.order_id}?\n\nĐơn sẽ được poll lại từ Gmail ở lần sau.`)) return;
        deleteOrders({ [order._account]: [order.order_id] }, 1);
    }

    function deleteSelected() {
        const n = state.selected.size;
        if (!n || !confirm(`Xoá ${n} đơn đã chọn?\n\nĐơn sẽ được poll lại từ Gmail ở lần sau.`)) return;
        const byAccount = {};
        state.selected.forEach(key => {
            const i = key.indexOf('::');
            (byAccount[key.slice(0, i)] ||= []).push(key.slice(i + 2));
        });
        deleteOrders(byAccount, n);
    }

    async function deleteAccountOrders(email) {
        if (!confirm(`Xoá TẤT CẢ đơn của ${email}?\n\nĐơn sẽ được poll lại từ Gmail.`)) return;
        try {
            const { data } = await fetchJSON('/api/etsy/orders/all?clear_processed=1&account=' + encodeURIComponent(email), { method: 'DELETE' });
            if (!data.success) throw new Error(data.error || 'không rõ');
            showToast(`Đã xoá ${data.deleted} đơn của ${email}.`, 'success');
            await reloadAfterMutation();
        } catch (e) {
            showToast('Xoá thất bại: ' + e.message, 'error');
        }
    }

    async function toggleAutoPoll(email, input) {
        input.disabled = true;
        try {
            const { data } = await fetchJSON('/api/etsy/polling/toggle/' + encodeURIComponent(email), { method: 'POST' });
            if (!data.success) throw new Error(data.error || 'không rõ');
            await loadStatus();
        } catch (e) {
            input.checked = !input.checked; // revert optimistic switch
            showToast('Không đổi được auto poll: ' + e.message, 'error');
        } finally {
            input.disabled = false;
        }
    }

    async function saveQuery() {
        if (!state.account) return;
        const btn = $('btn-save-query');
        btn.disabled = true;
        try {
            const { data } = await fetchJSON('/api/accounts/' + encodeURIComponent(state.account), {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ etsy_query: $('query-input').value.trim() })
            });
            if (!data.success) throw new Error(data.error || 'không rõ');
            showToast('Đã lưu Gmail query.', 'success');
            $('query-input').blur();
            await loadStatus();
        } catch (e) {
            showToast('Lưu query thất bại: ' + e.message, 'error');
        } finally {
            btn.disabled = false;
        }
    }

    async function reloadAfterMutation() {
        await loadStatus();
        await loadOrders(false);
    }

    // ---------- Auto-refresh (never overlapping, paused when hidden) ----------
    let refreshTimer = null;
    let refreshing = false;

    async function refreshNow() {
        if (refreshing) return;
        refreshing = true;
        clearTimeout(refreshTimer);
        try {
            const status = await loadStatus();
            const versionChanged = status && status.orders_version !== state.ordersVersion;
            if (versionChanged || state.loadedAccount !== state.account) await loadOrders(false);
            await loadLogs();
        } finally {
            refreshing = false;
            scheduleRefresh();
        }
    }

    function scheduleRefresh() {
        clearTimeout(refreshTimer);
        const running = state.job && state.job.state === 'running';
        refreshTimer = setTimeout(() => {
            if (document.hidden) { scheduleRefresh(); return; }
            refreshNow();
        }, running ? REFRESH_JOB_MS : REFRESH_IDLE_MS);
    }

    // ---------- Event wiring ----------
    function debounce(fn, ms) {
        let t;
        return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
    }

    function wireEvents() {
        $('side-accounts').addEventListener('click', e => {
            const chip = e.target.closest('[data-account]');
            if (chip) selectAccount(chip.dataset.account);
        });

        $('btn-poll').addEventListener('click', () => startJob('poll'));
        $('btn-sync').addEventListener('click', () => startJob('sync'));
        $('btn-manage').addEventListener('click', () => {
            const panel = $('manage-panel');
            panel.hidden = !panel.hidden;
            $('btn-manage').setAttribute('aria-expanded', String(!panel.hidden));
        });
        $('btn-save-query').addEventListener('click', saveQuery);
        $('query-input').addEventListener('keydown', e => { if (e.key === 'Enter') saveQuery(); });

        $('acc-list').addEventListener('click', e => {
            const btn = e.target.closest('[data-action="delete-account-orders"]');
            if (btn) deleteAccountOrders(btn.closest('.acc-row').dataset.email);
        });
        $('acc-list').addEventListener('change', e => {
            if (e.target.dataset.action === 'toggle-auto') toggleAutoPoll(e.target.closest('.acc-row').dataset.email, e.target);
        });

        // Filters: apply instantly (search debounced) — no "Lọc" button needed
        const onSearch = debounce(() => { state.filters.search = $('search-input').value; reloadFirstPage(); }, 180);
        $('search-input').addEventListener('input', onSearch);
        const onDate = () => {
            state.filters.from = $('from-date').value;
            state.filters.to = $('to-date').value;
            reloadFirstPage();
        };
        $('from-date').addEventListener('change', onDate);
        $('to-date').addEventListener('change', onDate);
        $('buyer-select').addEventListener('change', e => { state.filters.buyerKey = e.target.value; reloadFirstPage(); });

        // "/" focuses search; Esc clears the search, else closes the drawer
        document.addEventListener('keydown', e => {
            const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
            if (e.key === '/' && !typing) { e.preventDefault(); $('search-input').focus(); }
            if (e.key === 'Escape') {
                if (document.activeElement === $('search-input') && $('search-input').value) {
                    $('search-input').value = '';
                    onSearch();
                } else {
                    closeDrawer();
                }
            }
        });

        // Table: event delegation
        $('orders-tbody').addEventListener('change', e => {
            if (e.target.dataset.action === 'select') setSelected(e.target.closest('tr').dataset.key, e.target.checked);
        });
        $('orders-tbody').addEventListener('click', e => {
            const tr = e.target.closest('tr[data-key]');
            if (!tr) return;
            const btn = e.target.closest('[data-action]');
            if (btn && btn.dataset.action === 'buyer') {
                state.filters.buyerKey = btn.dataset.buyer;
                reloadFirstPage();
            } else if (btn && btn.dataset.action === 'select') {
                // checkbox: handled on change
            } else if (!e.target.closest('.col-check')) {
                // anywhere else on the row (incl. the Order ID button) opens the detail drawer
                openOrder(tr.dataset.key);
            }
        });

        // Detail drawer actions
        $('order-drawer').addEventListener('click', e => {
            const btn = e.target.closest('[data-drawer]');
            if (!btn) return;
            const order = state.orders.find(o => o._key === state.openKey);
            if (btn.dataset.drawer === 'close') closeDrawer();
            else if (btn.dataset.drawer === 'copy') EtsyOrderDrawer.copyAddress();
            else if (order && btn.dataset.drawer === 'delete') deleteOneOrder(order._key);
            else if (order && btn.dataset.drawer === 'buyer') {
                state.filters.buyerKey = order._buyerKey;
                reloadFirstPage();
            }
        });

        document.body.addEventListener('click', e => {
            const btn = e.target.closest('[data-action="reset-filters"], [data-action="retry"]');
            if (!btn) return;
            if (btn.dataset.action === 'reset-filters') resetFilters();
            else loadOrders(true);
        });

        // Pager: numbered pages, page size, go-to-page
        $('pager').addEventListener('click', e => {
            const btn = e.target.closest('[data-page]');
            if (!btn || btn.disabled) return;
            goToPage(Number(btn.dataset.page));
        });
        $('pager').addEventListener('change', e => {
            if (e.target.dataset.action === 'page-size') setPageSize(e.target.value);
            else if (e.target.dataset.action === 'goto') {
                const n = parseInt(e.target.value, 10);
                if (n) goToPage(n); // renderTable clamps to 1..totalPages
            }
        });
        $('pager').addEventListener('keydown', e => {
            if (e.key === 'Enter' && e.target.dataset.action === 'goto') {
                e.preventDefault();
                const n = parseInt(e.target.value, 10);
                if (n) goToPage(n);
            }
        });

        $('select-all-cb').addEventListener('change', e => setPageSelected(e.target.checked));
        $('btn-select-all').addEventListener('click', selectAllFiltered);
        $('btn-clear-selection').addEventListener('click', clearSelection);
        $('btn-delete-selected').addEventListener('click', deleteSelected);

        $('btn-clear-logs').addEventListener('click', async e => {
            e.preventDefault(); // don't toggle <details>
            if (!confirm('Xoá toàn bộ nhật ký?')) return;
            await fetchJSON('/api/logs/clear', { method: 'POST' }).catch(() => {});
            state.logsSig = '';
            loadLogs();
        });

        document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshNow(); });
    }

    // ---------- Init ----------
    async function init() {
        readUrlState();
        syncFilterInputs();
        wireEvents();
        $('orders-tbody').innerHTML = EtsyOrdersTable.skeletonHtml();
        // Status and orders in parallel: first paint doesn't wait on two round-trips
        await Promise.all([loadStatus(), loadOrders(false)]);
        loadLogs();
        scheduleRefresh();
    }

    init();
})();
