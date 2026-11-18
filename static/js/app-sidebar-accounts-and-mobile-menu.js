// Shared sidebar behaviour for every logged-in page (markup: templates/_app_sidebar.html):
// - off-canvas open/close on narrow screens
// - AppSidebar.renderAccounts(): the account list, as filter buttons (Etsy page)
//   or as links to the Etsy page (overview / config)
// Depends on: shared-ui-theme-toast-fetch-helpers.js (esc)

const AppSidebar = (function () {
    let lastSig = '';

    function shortEmail(email) { return String(email).split('@')[0]; }

    /**
     * accounts: [{ email, on, count }]
     * opts.current: selected email ('' = all), opts.asButtons: true -> <button data-account>, else links
     */
    function renderAccounts(accounts, opts = {}) {
        const box = document.getElementById('side-accounts');
        if (!box) return;
        const sig = JSON.stringify([accounts, opts]);
        if (sig === lastSig) return; // nothing changed: skip DOM work
        lastSig = sig;

        if (!accounts.length) {
            box.innerHTML = '<span class="side-muted">Chưa có tài khoản</span>';
            return;
        }
        const total = accounts.reduce((n, a) => n + (a.count || 0), 0);
        const item = (email, label, dotClass, count, title) => {
            const inner = `<span class="dot ${dotClass}" aria-hidden="true"></span><span class="name">${esc(label)}</span>`
                + (count == null ? '' : `<span class="n">${count}</span>`);
            if (opts.asButtons) {
                return `<button type="button" data-account="${esc(email)}" aria-pressed="${(opts.current || '') === email}" title="${esc(title)}">${inner}</button>`;
            }
            const href = '/etsy' + (email ? '?account=' + encodeURIComponent(email) : '');
            return `<a href="${href}" title="${esc(title)}">${inner}</a>`;
        };
        const hasCounts = accounts.some(a => a.count != null);
        box.innerHTML = item('', 'Tất cả đơn', 'blank', hasCounts ? total : null, 'Tất cả tài khoản')
            + accounts.map(a => item(a.email, shortEmail(a.email), a.on ? '' : 'off', a.count,
                `${a.email} · tự poll ${a.on ? 'bật' : 'tắt'}`)).join('');
    }

    function setPollInterval(minutes) {
        const el = document.getElementById('side-interval');
        if (el && minutes) el.textContent = `Tự poll mỗi ${minutes} phút`;
    }

    // ---------- off-canvas menu (narrow screens) ----------
    function setOpen(open) {
        const side = document.getElementById('app-side');
        const scrim = document.getElementById('side-scrim');
        const btn = document.getElementById('side-open');
        if (!side) return;
        side.classList.toggle('open', open);
        if (scrim) scrim.hidden = !open;
        if (btn) btn.setAttribute('aria-expanded', String(open));
    }

    document.addEventListener('DOMContentLoaded', () => {
        const btn = document.getElementById('side-open');
        const scrim = document.getElementById('side-scrim');
        if (btn) btn.addEventListener('click', () => setOpen(!document.getElementById('app-side').classList.contains('open')));
        if (scrim) scrim.addEventListener('click', () => setOpen(false));
        document.addEventListener('keydown', e => { if (e.key === 'Escape') setOpen(false); });
        // Close after choosing a destination on a phone
        const side = document.getElementById('app-side');
        if (side) side.addEventListener('click', e => { if (e.target.closest('a, [data-account]')) setOpen(false); });
    });

    return { renderAccounts, setPollInterval, shortEmail };
})();
