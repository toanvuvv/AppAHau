// Right-hand detail drawer for one Etsy order: every item (thumb, SKU, variations,
// personalization), ship-to address with a copy button, phone, account and Sheet state.
// The page controller owns state; this module only renders and copies.
// Depends on: shared-ui-theme-toast-fetch-helpers.js, etsy-orders-table-html-builders.js

const EtsyOrderDrawer = (function () {
    const $ = id => document.getElementById(id);
    const CLOSE_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"/></svg>';
    const COPY_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>';
    const TRASH_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/></svg>';

    /** Address as plain lines, ready to paste into a shipping label tool. */
    function addressText(o) {
        const cityLine = [o.city, [o.state, o.zipcode].filter(Boolean).join(' ')].filter(Boolean).join(', ');
        return [o.buyer_name, o.address_1, o.address_2, cityLine, o.country].filter(Boolean).join('\n');
    }

    function itemHtml(it) {
        const thumb = it.thumbnail
            ? `<img class="th lg" src="${esc(it.thumbnail)}" alt="" width="56" height="56" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'th lg blank'}))">`
            : '<span class="th lg blank"></span>';
        const variations = [it.variation_1, it.variation_2].filter(Boolean).map(esc).join(' · ');
        return `<div class="item">${thumb}<div>
                <p class="t">${esc(it.title || '(không có tên sản phẩm)')}</p>
                <p class="m">${it.sku ? `<span class="mono">${esc(it.sku)}</span>` : ''}${it.sku && variations ? ' · ' : ''}${variations}</p>
                ${it.personalized ? `<p class="m p">${esc(it.personalized)}</p>` : ''}
                ${it.shop ? `<p class="m">${esc(it.shop)}</p>` : ''}
            </div><span class="q">×${esc(it.quantity || 1)}</span></div>`;
    }

    /** Render the order into #order-drawer, or close it when order is null. */
    function render(order) {
        const d = $('order-drawer');
        if (!order) {
            d.classList.remove('open');
            d.setAttribute('aria-hidden', 'true');
            d.inert = true;
            return;
        }
        const addr = addressText(order);
        d.innerHTML = `<div class="dh">
                <div><h2 class="mono">#${esc(order.order_id)}</h2><p>${esc(order._dateLabel)} · ${esc(AppSidebar.shortEmail(order._account))}</p></div>
                <button type="button" class="icon-btn" data-drawer="close" aria-label="Đóng chi tiết">${CLOSE_SVG}</button>
            </div>
            <div class="db">
                <section><h3>${order.items.length} sản phẩm · ${EtsyOrdersTable.totalQty(order)} cái</h3>${order.items.map(itemHtml).join('')}</section>
                <section><h3>Giao tới</h3>
                    <div class="addr"><button type="button" class="btn ghost small copy" data-drawer="copy">${COPY_SVG}Sao chép</button>
                    ${esc(addr).replace(/\n/g, '<br>')}</div>
                    <pre class="sr-only" id="drawer-addr-text">${esc(addr)}</pre>
                </section>
                <section><h3>Thông tin</h3><dl class="kv">
                    <dt>Điện thoại</dt><dd>${esc(order.phone) || '—'}</dd>
                    <dt>Google Sheet</dt><dd>${EtsyOrdersTable.sheetPill(order)}</dd>
                    <dt>Tài khoản</dt><dd>${esc(order._account)}</dd>
                </dl></section>
            </div>
            <div class="df">
                <button type="button" class="btn danger" data-drawer="delete">${TRASH_SVG}Xoá đơn</button>
                <button type="button" class="btn" data-drawer="buyer">Lọc đơn của buyer này</button>
            </div>`;
        d.classList.add('open');
        d.setAttribute('aria-hidden', 'false');
        d.inert = false;
    }

    function copyAddress() {
        const text = $('drawer-addr-text') ? $('drawer-addr-text').textContent : '';
        navigator.clipboard.writeText(text)
            .then(() => showToast('Đã sao chép địa chỉ', 'success'))
            .catch(() => showToast('Trình duyệt chặn sao chép, hãy bôi đen địa chỉ rồi Ctrl+C', 'warning'));
    }

    return { render, copyAddress };
})();
