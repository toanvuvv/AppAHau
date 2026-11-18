// Shared UI helpers for every dashboard page: theme toggle (SVG icons),
// non-blocking toasts (replace alert()), fetch with timeout, HTML escaping.

(function () {
    const SUN_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
    const MOON_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>';

    /** Saved choice wins; otherwise follow the OS setting. */
    function readSavedTheme() {
        let saved = null;
        try { saved = localStorage.getItem('theme'); } catch (e) {}
        if (saved === 'light' || saved === 'dark') return saved;
        return window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }

    function applyTheme(theme) {
        document.documentElement.setAttribute('data-theme', theme);
        // Every toggle (sidebar + mobile bar); icon shows the theme you will switch TO
        document.querySelectorAll('.theme-toggle').forEach(btn => {
            btn.innerHTML = theme === 'dark' ? SUN_SVG : MOON_SVG;
            btn.setAttribute('aria-label', theme === 'dark' ? 'Chuyển giao diện sáng' : 'Chuyển giao diện tối');
        });
    }

    window.toggleTheme = function () {
        const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
        try { localStorage.setItem('theme', next); } catch (e) {}
        applyTheme(next);
    };

    // Apply as early as possible to avoid a light->dark flash
    document.documentElement.setAttribute('data-theme', readSavedTheme());
    document.addEventListener('DOMContentLoaded', () => applyTheme(readSavedTheme()));

    /** Escape text for safe insertion into HTML text AND attribute values. */
    window.esc = function (value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    };

    /** fetch + JSON with a timeout so a stuck request never blocks the UI. */
    window.fetchJSON = async function (url, options = {}, timeoutMs = 15000) {
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), timeoutMs);
        try {
            const res = await fetch(url, { ...options, signal: ctrl.signal });
            const data = await res.json().catch(() => ({}));
            return { status: res.status, ok: res.ok, data };
        } finally {
            clearTimeout(timer);
        }
    };

    /** Non-blocking notification. type: info | success | error | warning. */
    window.showToast = function (message, type = 'info', durationMs) {
        let region = document.getElementById('toast-region');
        if (!region) {
            region = document.createElement('div');
            region.id = 'toast-region';
            region.className = 'toast-region';
            region.setAttribute('role', 'status');
            region.setAttribute('aria-live', 'polite');
            document.body.appendChild(region);
        }
        const toast = document.createElement('div');
        toast.className = 'toast ' + type;
        toast.textContent = message;
        region.appendChild(toast);
        const ttl = durationMs || (type === 'error' ? 8000 : 4000);
        setTimeout(() => {
            toast.classList.add('leaving');
            setTimeout(() => toast.remove(), 200);
        }, ttl);
    };
})();
