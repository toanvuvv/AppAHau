// Pure HTML builders for the Etsy orders table: compact rows (one row per order),
// skeleton, empty states and the numbered pager with page-size + go-to-page.
// No inline handlers with interpolated strings: actions use data-* attributes and
// event delegation in the page script (safe for any quote/apostrophe in data).

const EtsyOrdersTable = (function () {
    const PAGE_SIZES = [25, 50, 100, 200];
    const COLS = 9;
    const PREV_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg>';
    const NEXT_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>';

    /** Up to 3 overlapping product thumbnails (blank tile when an item has none). */
    function thumbsHtml(items) {
        return '<span class="thumbs">' + items.slice(0, 3).map(it => it.thumbnail
            ? `<img class="th" src="${esc(it.thumbnail)}" alt="" width="34" height="34" loading="lazy" decoding="async" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'th blank'}))">`
            : '<span class="th blank"></span>').join('') + '</span>';
    }

    /** Sheet state: true = written, false = waiting for retry, missing = older order (unknown). */
    function sheetPill(order) {
        if (order.sheet_synced === true) return '<span class="pill ok">Đã ghi</span>';
        if (order.sheet_synced === false) return '<span class="pill warn">Chưa ghi</span>';
        return '<span class="pill muted">Không rõ</span>';
    }

    function totalQty(order) {
        return order.items.reduce((n, it) => n + (parseInt(it.quantity, 10) || 1), 0);
    }

    /** Build <tr> rows for one page of orders. */
    function rowsHtml(pageOrders, selected, openKey) {
        return pageOrders.map(order => {
            const isSel = selected.has(order._key);
            const first = order.items[0] || {};
            const more = order.items.length > 1 ? `<span class="more">+${order.items.length - 1}</span>` : '';
            const place = [order.city, order.country].filter(Boolean).join(', ');
            const cls = (isSel ? 'selected ' : '') + (order._key === openKey ? 'open' : '');
            return `<tr class="${cls}" data-key="${esc(order._key)}">`
                + `<td class="col-check"><input type="checkbox" class="cb" data-action="select" ${isSel ? 'checked' : ''} aria-label="Chọn đơn ${esc(order.order_id)}"></td>`
                + `<td>${thumbsHtml(order.items)}</td>`
                + `<td><button type="button" class="order-id" data-action="open" aria-label="Xem chi tiết đơn ${esc(order.order_id)}">${esc(order.order_id)}</button></td>`
                + `<td class="date">${esc(order._dateLabel)}</td>`
                + `<td><button type="button" class="buyer-link" data-action="buyer" data-buyer="${esc(order._buyerKey)}" title="Lọc theo buyer này">${esc(order._buyerLabel)}</button></td>`
                + `<td class="title"><span>${esc(first.title || '')}</span>${more}</td>`
                + `<td class="num">${totalQty(order)}</td>`
                + `<td class="place" title="${esc(place)}">${esc(place)}</td>`
                + `<td>${sheetPill(order)}</td>`
                + '</tr>';
        }).join('');
    }

    /** Fixed-height placeholder rows shown during the first load. */
    function skeletonHtml(rowCount = 8) {
        const cell = '<td><div class="skeleton-bar"></div></td>';
        return Array.from({ length: rowCount }, () => `<tr class="skeleton-row">${cell.repeat(COLS)}</tr>`).join('');
    }

    function emptyHtml(kind) {
        if (kind === 'no-match') {
            return '<h3>Không có đơn nào khớp bộ lọc</h3><p>Thử đổi từ khoá, khoảng ngày hoặc buyer.</p>'
                + '<button type="button" class="btn primary" data-action="reset-filters">Xoá bộ lọc</button>';
        }
        if (kind === 'error') {
            return '<h3>Không tải được đơn hàng</h3><p>Kiểm tra kết nối tới server rồi thử lại.</p>'
                + '<button type="button" class="btn primary" data-action="retry">Thử lại</button>';
        }
        return '<h3>Chưa có đơn hàng nào</h3><p>Bấm <strong>Poll Gmail</strong> để lấy đơn, hoặc bật tự poll trong <strong>Tuỳ chọn</strong>.</p>';
    }

    /** Page numbers with gaps: 1 … 4 5 6 … 20 (always first, last, and the current ±1). */
    function pageNumbers(page, totalPages) {
        if (totalPages <= 7) return Array.from({ length: totalPages }, (_, i) => i + 1);
        const set = new Set([1, totalPages, page - 1, page, page + 1]);
        if (page <= 4) [2, 3, 4, 5].forEach(n => set.add(n));
        if (page >= totalPages - 3) [1, 2, 3, 4].forEach(k => set.add(totalPages - k));
        const sorted = [...set].filter(n => n >= 1 && n <= totalPages).sort((a, b) => a - b);
        const out = [];
        sorted.forEach((n, i) => { if (i && n - sorted[i - 1] > 1) out.push('…'); out.push(n); });
        return out;
    }

    function pagerHtml(page, totalPages, totalOrders, pageSize) {
        const from = totalOrders ? (page - 1) * pageSize + 1 : 0;
        const to = Math.min(page * pageSize, totalOrders);
        const nums = pageNumbers(page, totalPages).map(n => n === '…'
            ? '<span class="gap" aria-hidden="true">…</span>'
            : `<button type="button" data-page="${n}" ${n === page ? 'aria-current="page"' : ''} aria-label="Trang ${n}">${n}</button>`).join('');
        return `<span class="range">Hiển thị <b>${from}–${to}</b> trong <b>${totalOrders}</b> đơn</span>`
            + `<label class="pp">Số đơn/trang <select class="field" data-action="page-size" aria-label="Số đơn mỗi trang">`
            + PAGE_SIZES.map(n => `<option value="${n}" ${n === pageSize ? 'selected' : ''}>${n}</option>`).join('')
            + '</select></label>'
            + (totalPages > 1 ? `<label class="goto">Tới trang <input class="field" type="number" min="1" max="${totalPages}" data-action="goto" placeholder="${page}" aria-label="Tới trang số (1 đến ${totalPages})"></label>` : '')
            + '<nav class="pages" aria-label="Phân trang">'
            + `<button type="button" data-page="${page - 1}" ${page <= 1 ? 'disabled' : ''} aria-label="Trang trước">${PREV_SVG}</button>`
            + nums
            + `<button type="button" data-page="${page + 1}" ${page >= totalPages ? 'disabled' : ''} aria-label="Trang sau">${NEXT_SVG}</button>`
            + '</nav>';
    }

    return { PAGE_SIZES, rowsHtml, skeletonHtml, emptyHtml, pagerHtml, sheetPill, totalQty };
})();
