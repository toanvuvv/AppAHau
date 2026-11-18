// Overview view of templates/index.html: hash router (#overview / #config), stats,
// Gmail account cards (with per-account auto-poll switch), activity log, sidebar accounts.
// Depends on: shared-ui-theme-toast-fetch-helpers.js, app-sidebar-accounts-and-mobile-menu.js
// The config view lives in dashboard-config-accounts-and-settings-modals.js (DashboardConfig).

(function () {
    const $ = id => document.getElementById(id);
    const LEVEL_LABEL = { INFO: 'INFO', SUCCESS: 'OK', WARNING: 'WARN', ERROR: 'LỖI' };

    // ---------- views (hash routing so the sidebar can link to /#config) ----------
    function currentView() { return location.hash === '#config' ? 'config' : 'overview'; }

    function showView() {
        const view = currentView();
        $('view-overview').hidden = view !== 'overview';
        $('view-config').hidden = view !== 'config';
        document.title = (view === 'config' ? 'Cấu hình' : 'Tổng quan') + ' · AppMail';
        document.querySelectorAll('.side-nav a[data-nav]').forEach(a => {
            const on = (a.dataset.nav === 'cfg' && view === 'config') || (a.dataset.nav === 'dash' && view === 'overview');
            if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
        });
        if (view === 'config' && typeof DashboardConfig !== 'undefined') DashboardConfig.load();
    }
    window.addEventListener('hashchange', showView);

    // ---------- formatting ----------
    function formatUptime(seconds) {
        const d = Math.floor(seconds / 86400);
        const h = Math.floor((seconds % 86400) / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        return d ? `${d} ngày ${h} giờ` : `${h} giờ ${m} phút`;
    }
    const num = n => Number(n || 0).toLocaleString('vi-VN');

    function tokenPill(status) {
        if (status === 'valid') return '<span class="pill ok">Token hợp lệ</span>';
        if (status === 'needs_refresh') return '<span class="pill warn">Cần làm mới</span>';
        return '<span class="pill bad">Chưa xác thực</span>';
    }

    // ---------- rendering ----------
    // Last failed Google Sheet write of an account (etsy status accounts[].sheet_error)
    function sheetErrorBox(err) {
        if (!err) return '';
        const when = err.at ? new Date(err.at * 1000).toLocaleString('vi-VN') : '';
        return `<div class="sheet-error" role="alert">
            <div class="sheet-error-title">Không ghi được vào Sheet</div>
            <div>${esc(err.message)}</div>
            <div class="sheet-error-meta" title="${esc(err.raw)}">Tab «${esc(err.sheet_tab)}»${err.row ? ` · dòng ${esc(err.row)}` : ''}${when ? ` · ${esc(when)}` : ''}</div>
        </div>`;
    }

    let accountsSig = '';
    function renderAccounts(accounts, ordersByEmail, sheetErrorByEmail) {
        const sig = JSON.stringify([accounts, ordersByEmail, sheetErrorByEmail]);
        if (sig === accountsSig) return; // a re-render would reset a switch mid-click
        accountsSig = sig;
        const grid = $('accounts-grid');
        if (!accounts.length) {
            grid.innerHTML = '<div class="loading-text">Chưa có tài khoản. Bấm «Thêm tài khoản Gmail» để bắt đầu.</div>';
            return;
        }
        grid.innerHTML = accounts.map(acc => {
            const orders = ordersByEmail[acc.email];
            return `<article class="acc-card" data-email="${esc(acc.email)}">
                <div class="row"><span class="email" title="${esc(acc.email)}">${esc(acc.email)}</span>${tokenPill(acc.token_status)}</div>
                <div class="nums">
                    <div><div class="big">${num(acc.total_emails)}</div><div class="muted">email đã xử lý</div></div>
                    ${orders == null ? '' : `<div><div class="big">${num(orders)}</div><div class="muted">đơn Etsy</div></div>`}
                </div>
                ${sheetErrorBox(sheetErrorByEmail[acc.email])}
                <div class="row">
                    <label class="switch"><input type="checkbox" data-action="toggle-poll" ${acc.polling_enabled ? 'checked' : ''}><span class="track" aria-hidden="true"></span>Tự poll</label>
                    <span class="links">
                        <a class="link-btn" href="/etsy?account=${encodeURIComponent(acc.email)}">Xem đơn</a>
                        ${acc.sheet_id ? `<a class="link-btn" href="https://docs.google.com/spreadsheets/d/${encodeURIComponent(acc.sheet_id)}" target="_blank" rel="noopener">Mở Sheet ↗</a>` : ''}
                    </span>
                </div>
            </article>`;
        }).join('');
    }

    // Order emails the parser could not read (from /api/etsy/status accounts[].unparsed)
    let unparsedSig = '';
    function renderUnparsed(accounts) {
        const items = [];
        accounts.forEach(a => (a.unparsed || []).forEach(u => items.push({ ...u, account: a.email })));
        const sig = JSON.stringify(items);
        if (sig === unparsedSig) return;
        unparsedSig = sig;
        $('unparsed-box').hidden = !items.length;
        if (!items.length) return;
        $('unparsed-count').textContent = items.length;
        $('unparsed-list').innerHTML = items.map(u => {
            const gmail = `https://mail.google.com/mail/?authuser=${encodeURIComponent(u.account)}#all/${encodeURIComponent(u.message_id)}`;
            return `<div class="item" data-account="${esc(u.account)}" data-id="${esc(u.message_id)}">
                <div class="info">
                    <div class="subj">${esc(u.subject || '(không có tiêu đề)')}</div>
                    <div class="meta">${esc(u.account)} · ${esc(u.email_date)} · ${esc(u.reason)}</div>
                </div>
                <span class="acts">
                    <a class="link-btn" href="${gmail}" target="_blank" rel="noopener">Mở Gmail ↗</a>
                    <button class="link-btn" type="button" data-action="dismiss-unparsed">Bỏ qua</button>
                </span>
            </div>`;
        }).join('');
    }

    async function dismissUnparsed(item) {
        if (!confirm('Email này không phải đơn hàng? Sẽ không đọc lại nữa.')) return;
        try {
            const { data } = await fetchJSON('/api/etsy/unparsed/dismiss', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ account: item.dataset.account, message_id: item.dataset.id })
            });
            if (!data.success) throw new Error(data.error || 'không rõ');
            showToast('Đã bỏ qua email', 'success');
            refresh();
        } catch (e) {
            showToast('Bỏ qua thất bại: ' + e.message, 'error');
        }
    }

    let lastLogsSig = '';
    function renderLogs(logs) {
        const sig = logs.map(l => l.text).join('\n');
        if (sig === lastLogsSig) return; // skip DOM work when nothing changed
        lastLogsSig = sig;
        const box = $('log-container');
        if (!logs.length) { box.innerHTML = '<div class="empty">Chưa có nhật ký.</div>'; return; }
        // Newest first: the latest event is what you look for
        box.innerHTML = [...logs].reverse().map(l =>
            `<div class="ln"><span class="lv ${esc(l.level)}">${esc(LEVEL_LABEL[l.level] || l.level)}</span><span class="msg">${esc(l.text)}</span></div>`).join('');
    }

    // ---------- data ----------
    let refreshing = false;
    async function refresh() {
        if (refreshing) return; // never stack requests on a slow server
        refreshing = true;
        try {
            const [statusRes, etsyRes] = await Promise.all([
                fetchJSON('/api/status'),
                fetchJSON('/api/etsy/status').catch(() => null)
            ]);
            const data = statusRes.data || {};
            const etsy = (etsyRes && etsyRes.ok) ? etsyRes.data : null;

            $('today-count').textContent = num(data.today_count);
            $('total-count').textContent = num(data.total_all_time);
            $('uptime').textContent = formatUptime(data.uptime_seconds || 0);
            $('interval-sub').textContent = `tự poll mỗi ${data.polling_interval} phút`;
            $('run-dot').classList.toggle('off', !data.running);
            $('run-text').textContent = data.running
                ? `Đang chạy · tự poll mỗi ${data.polling_interval} phút`
                : 'Đang tạm dừng poll';
            AppSidebar.setPollInterval(data.polling_interval);

            const ordersByEmail = {};
            const sheetErrorByEmail = {};
            if (etsy) {
                let total = 0;
                (etsy.accounts || []).forEach(a => {
                    ordersByEmail[a.email] = a.order_count;
                    total += a.order_count || 0;
                    if (a.sheet_error) sheetErrorByEmail[a.email] = a.sheet_error;
                });
                $('orders-count').textContent = num(total);
                $('orders-sub').textContent = `trên ${(etsy.accounts || []).length} tài khoản`;
                AppSidebar.renderAccounts((etsy.accounts || []).map(a => ({ email: a.email, on: a.etsy_polling_enabled, count: a.order_count })));
                renderUnparsed(etsy.accounts || []);
            } else {
                AppSidebar.renderAccounts((data.accounts || []).map(a => ({ email: a.email, on: a.polling_enabled })));
            }
            renderAccounts(data.accounts || [], ordersByEmail, sheetErrorByEmail);
            renderLogs(data.logs || []);
        } catch (e) {
            $('run-text').textContent = 'Không kết nối được tới server';
            $('run-dot').classList.add('off');
        } finally {
            refreshing = false;
        }
    }

    async function togglePolling(email, input) {
        input.disabled = true;
        try {
            const { data } = await fetchJSON(`/api/polling/toggle/${encodeURIComponent(email)}`, { method: 'POST' });
            if (!data.success) throw new Error(data.error || 'không rõ');
            showToast(`Tự poll ${data.polling_enabled ? 'đã bật' : 'đã tắt'} cho ${email}`, 'success');
            accountsSig = '';
            await refresh();
        } catch (e) {
            input.checked = !input.checked; // revert optimistic switch
            showToast('Không đổi được tự poll: ' + e.message, 'error');
        } finally {
            input.disabled = false;
        }
    }

    async function clearLogs() {
        if (!confirm('Xoá toàn bộ nhật ký?')) return;
        try {
            const { data } = await fetchJSON('/api/logs/clear', { method: 'POST' });
            if (!data.success) throw new Error(data.error || 'không rõ');
            lastLogsSig = '';
            showToast('Đã xoá nhật ký', 'success');
            refresh();
        } catch (e) {
            showToast('Xoá nhật ký thất bại: ' + e.message, 'error');
        }
    }

    // ---------- wiring ----------
    document.addEventListener('DOMContentLoaded', () => {
        $('accounts-grid').addEventListener('change', e => {
            if (e.target.dataset.action === 'toggle-poll') togglePolling(e.target.closest('.acc-card').dataset.email, e.target);
        });
        $('btn-clear-logs').addEventListener('click', clearLogs);
        $('unparsed-list').addEventListener('click', e => {
            if (e.target.dataset.action === 'dismiss-unparsed') dismissUnparsed(e.target.closest('.item'));
        });
        showView();
        refresh();
        setInterval(() => { if (!document.hidden) refresh(); }, 30000);
        document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
    });
})();
