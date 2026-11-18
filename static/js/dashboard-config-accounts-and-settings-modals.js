// Config view of templates/index.html: Google client file upload, poll interval,
// accounts table with add / edit / delete / OAuth modals.
// Exposes DashboardConfig.load() (called by the overview router when #config opens).
// Depends on: shared-ui-theme-toast-fetch-helpers.js

const DashboardConfig = (function () {
    const $ = id => document.getElementById(id);
    let accounts = [];

    // ---------- modal helpers ----------
    function showModal(id) {
        $(id).classList.add('active');
        const first = $(id).querySelector('input:not([disabled]), button');
        if (first) setTimeout(() => first.focus(), 30);
    }
    function closeModal(id) { $(id).classList.remove('active'); }
    function setMsg(id, text, ok) { const el = $(id); el.className = ok ? 'msg-success' : 'msg-error'; el.textContent = text; }

    function tokenPill(status) {
        if (status === 'valid') return '<span class="pill ok">Đã xác thực</span>';
        if (status === 'needs_refresh') return '<span class="pill warn">Cần làm mới</span>';
        return '<span class="pill bad">Chưa xác thực</span>';
    }

    // ---------- Google client file (credentials.json) ----------
    async function loadClientFileStatus() {
        try {
            const { data } = await fetchJSON('/api/credentials/status');
            const el = $('cred-status-text');
            const btn = $('cred-upload-btn');
            if (data.exists && data.valid) { el.innerHTML = '<span class="pill ok">Hợp lệ</span>'; btn.textContent = 'Thay file'; }
            else if (data.exists) { el.innerHTML = '<span class="pill warn">File không hợp lệ</span>'; btn.textContent = 'Thay file'; }
            else { el.innerHTML = '<span class="pill bad">Chưa có file</span>'; btn.textContent = 'Tải lên credentials.json'; }
        } catch (e) { /* status is informational */ }
    }

    async function uploadClientFile(file) {
        const form = new FormData();
        form.append('file', file);
        try {
            const { data } = await fetchJSON('/api/credentials/upload', { method: 'POST', body: form });
            if (data.success) { setMsg('cred-msg', 'Đã tải lên', true); loadClientFileStatus(); }
            else setMsg('cred-msg', data.error || 'Tải lên thất bại');
        } catch (e) {
            setMsg('cred-msg', 'Lỗi kết nối, thử lại');
        }
    }

    // ---------- poll interval ----------
    async function loadPollingInterval() {
        try {
            const { data } = await fetchJSON('/api/status');
            if (data.polling_interval) $('polling-input').value = data.polling_interval;
        } catch (e) {}
    }

    async function savePolling() {
        const val = parseInt($('polling-input').value, 10);
        if (!val || val < 1) { setMsg('polling-msg', 'Nhập số phút từ 1 trở lên'); return; }
        try {
            const { data } = await fetchJSON('/api/config/polling', {
                method: 'PUT', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ polling_interval_minutes: val })
            });
            if (data.success) { setMsg('polling-msg', 'Đã lưu', true); setTimeout(() => $('polling-msg').textContent = '', 2000); }
            else setMsg('polling-msg', data.error || 'Lưu thất bại');
        } catch (e) {
            setMsg('polling-msg', 'Lỗi kết nối, thử lại');
        }
    }

    // ---------- accounts ----------
    async function loadAccounts() {
        const tbody = $('accounts-tbody');
        try {
            const { data } = await fetchJSON('/api/accounts');
            accounts = data.accounts || [];
            if (!accounts.length) {
                tbody.innerHTML = '<tr><td colspan="6" class="loading-text" style="text-align:center">Chưa có tài khoản. Bấm «Thêm tài khoản».</td></tr>';
                return;
            }
            tbody.innerHTML = accounts.map((acc, i) => `<tr data-i="${i}">
                <td class="email">${esc(acc.email)}</td>
                <td class="mono cut" title="${esc(acc.sheet_id)}">${esc(acc.sheet_id)}</td>
                <td>${esc(acc.sheet_tab)}</td>
                <td class="mono cut" title="${esc(acc.etsy_query)}">${esc(acc.etsy_query) || '<span class="helper">mặc định</span>'}</td>
                <td>${tokenPill(acc.token_status)}</td>
                <td class="row-actions">
                    <button class="btn ghost small" type="button" data-action="auth">Xác thực</button>
                    <button class="btn ghost small" type="button" data-action="edit">Sửa</button>
                    <button class="btn ghost small danger" type="button" data-action="delete">Xoá</button>
                </td></tr>`).join('');
        } catch (e) {
            tbody.innerHTML = '<tr><td colspan="6" class="msg-error">Không tải được danh sách tài khoản</td></tr>';
        }
    }

    function openAdd() {
        $('acc-email').value = '';
        $('acc-sheet-id').value = '';
        $('acc-sheet-tab').value = 'Sheet1';
        $('acc-query').value = '';
        $('add-acc-msg').textContent = '';
        showModal('add-account-modal');
    }

    async function addAccount() {
        const email = $('acc-email').value.trim();
        const sheet_id = $('acc-sheet-id').value.trim();
        if (!email || !sheet_id) { setMsg('add-acc-msg', 'Cần nhập Email và Sheet ID'); return; }
        try {
            const { data } = await fetchJSON('/api/accounts', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ email, sheet_id, sheet_tab: $('acc-sheet-tab').value.trim() || 'Sheet1', etsy_query: $('acc-query').value.trim() })
            });
            if (!data.success) { setMsg('add-acc-msg', data.error || 'Thêm thất bại'); return; }
            closeModal('add-account-modal');
            showToast(`Đã thêm ${email}. Bấm «Xác thực» để cấp quyền Gmail.`, 'success');
            loadAccounts();
        } catch (e) {
            setMsg('add-acc-msg', 'Lỗi kết nối, thử lại');
        }
    }

    function openEdit(acc) {
        $('edit-email').value = acc.email;
        $('edit-sheet-id').value = acc.sheet_id;
        $('edit-sheet-tab').value = acc.sheet_tab;
        $('edit-query').value = acc.etsy_query || '';
        $('edit-acc-msg').textContent = '';
        showModal('edit-account-modal');
    }

    async function saveEdit() {
        const email = $('edit-email').value;
        try {
            const { data } = await fetchJSON('/api/accounts/' + encodeURIComponent(email), {
                method: 'PUT', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ sheet_id: $('edit-sheet-id').value.trim(), sheet_tab: $('edit-sheet-tab').value.trim(), etsy_query: $('edit-query').value.trim() })
            });
            if (!data.success) { setMsg('edit-acc-msg', data.error || 'Lưu thất bại'); return; }
            closeModal('edit-account-modal');
            showToast('Đã lưu tài khoản ' + email, 'success');
            loadAccounts();
        } catch (e) {
            setMsg('edit-acc-msg', 'Lỗi kết nối, thử lại');
        }
    }

    async function deleteAccount(email) {
        if (!confirm(`Xoá tài khoản ${email}?\n\nToàn bộ đơn Etsy đã lưu của tài khoản này cũng bị xoá.`)) return;
        try {
            const { data } = await fetchJSON('/api/accounts/' + encodeURIComponent(email), { method: 'DELETE' });
            if (!data.success) throw new Error(data.error || 'không rõ');
            showToast(`Đã xoá ${email} (${data.deleted_orders || 0} đơn)`, 'success');
            loadAccounts();
        } catch (e) {
            showToast('Xoá thất bại: ' + e.message, 'error');
        }
    }

    let oauthTimer = null;
    async function startAuth(email) {
        try {
            const { data } = await fetchJSON('/api/auth/start', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email })
            });
            if (!data.auth_url) { showToast(data.error || 'Không tạo được link xác thực', 'error'); return; }
            $('oauth-email-display').textContent = email;
            $('oauth-url-display').textContent = data.auth_url;
            showModal('oauth-modal');
            // Watch for the token to become valid, for at most 5 minutes
            clearInterval(oauthTimer);
            const started = Date.now();
            oauthTimer = setInterval(async () => {
                if (Date.now() - started > 300000 || !$('oauth-modal').classList.contains('active')) { clearInterval(oauthTimer); return; }
                const res = await fetchJSON('/api/accounts').catch(() => null);
                const acc = res && (res.data.accounts || []).find(a => a.email === email);
                if (acc && acc.token_status === 'valid') {
                    clearInterval(oauthTimer);
                    closeModal('oauth-modal');
                    showToast('Đã xác thực ' + email, 'success');
                    loadAccounts();
                }
            }, 3000);
        } catch (e) {
            showToast('Lỗi kết nối tới server', 'error');
        }
    }

    function copyOAuthUrl() {
        const url = $('oauth-url-display').textContent;
        navigator.clipboard.writeText(url)
            .then(() => showToast('Đã sao chép link xác thực', 'success'))
            .catch(() => showToast('Không sao chép được, hãy bôi đen link rồi Ctrl+C', 'warning'));
    }

    // ---------- wiring ----------
    document.addEventListener('DOMContentLoaded', () => {
        $('btn-add-account').addEventListener('click', openAdd);
        $('add-account-form').addEventListener('submit', e => { e.preventDefault(); addAccount(); });
        $('edit-account-form').addEventListener('submit', e => { e.preventDefault(); saveEdit(); });
        $('btn-copy-oauth').addEventListener('click', copyOAuthUrl);
        $('btn-save-polling').addEventListener('click', savePolling);
        $('polling-input').addEventListener('keydown', e => { if (e.key === 'Enter') savePolling(); });
        $('cred-file-input').addEventListener('change', e => {
            const file = e.target.files[0];
            if (file) uploadClientFile(file);
            e.target.value = '';
        });
        $('accounts-tbody').addEventListener('click', e => {
            const btn = e.target.closest('[data-action]');
            if (!btn) return;
            const acc = accounts[Number(btn.closest('tr').dataset.i)];
            if (!acc) return;
            if (btn.dataset.action === 'auth') startAuth(acc.email);
            else if (btn.dataset.action === 'edit') openEdit(acc);
            else if (btn.dataset.action === 'delete') deleteAccount(acc.email);
        });
        document.querySelectorAll('[data-close]').forEach(b => b.addEventListener('click', () => closeModal(b.dataset.close)));
        document.querySelectorAll('.modal-overlay').forEach(o => o.addEventListener('click', e => { if (e.target === o) closeModal(o.id); }));
        document.addEventListener('keydown', e => {
            if (e.key === 'Escape') document.querySelectorAll('.modal-overlay.active').forEach(m => closeModal(m.id));
        });
    });

    function load() { loadClientFileStatus(); loadAccounts(); loadPollingInterval(); }
    return { load };
})();
