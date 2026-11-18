// Etsy orders data layer: normalize one page of API orders (date label, keys)
// for rendering. Filtering, sorting and pagination happen on the server
// (/api/etsy/orders?page=...), so only the visible page is ever downloaded.

const EtsyOrdersData = (function () {
    // Cached formatter: much faster than date.toLocaleString({timeZone}) per call
    const NY_PARTS_FORMATTER = new Intl.DateTimeFormat('en-US', {
        timeZone: 'America/New_York',
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
    });

    /** Return the America/New_York wall-clock parts of a Date. */
    function nyParts(date) {
        const out = {};
        for (const p of NY_PARTS_FORMATTER.formatToParts(date)) {
            if (p.type !== 'literal') out[p.type] = p.value;
        }
        return out; // {year, month, day, hour, minute} as zero-padded strings
    }

    /** Parse the best available order date (Gmail header first, parser date fallback). */
    function parseOrderDate(order) {
        for (const rawDate of [order.email_date, order.date]) {
            const raw = (rawDate || '').trim();
            if (!raw) continue;

            // Vietnamese forwarded header format: "22 thg 3, 2026"
            const vn = raw.match(/^(\d{1,2})\s+thg\s+(\d{1,2}),\s*(\d{4})$/i);
            if (vn) {
                const d = new Date(Number(vn[3]), Number(vn[2]) - 1, Number(vn[1]));
                if (!Number.isNaN(d.getTime())) return d;
                continue;
            }
            // Avoid JS defaulting missing-year dates (e.g. "Mar 3" -> 2001)
            if (!/\b\d{4}\b/.test(raw)) continue;
            const parsed = new Date(raw);
            if (!Number.isNaN(parsed.getTime())) return parsed;
        }
        return null;
    }

    function normalizeText(value) {
        return (value || '').trim().toLowerCase();
    }

    /** Attach precomputed fields (prefixed with _) to one order. */
    function normalizeOrder(order, account) {
        const date = parseOrderDate(order);
        let ts = 0, dateKey = null, dateLabel = '';
        if (date) {
            const p = nyParts(date);
            ts = date.getTime();
            dateKey = Number(p.year) * 10000 + Number(p.month) * 100 + Number(p.day);
            dateLabel = `${p.month}/${p.day}/${p.year} ${p.hour}:${p.minute} ET`;
        }
        const items = (order.items && order.items.length) ? order.items
            : [{ title: '', sku: '', thumbnail: '', variation_1: '', variation_2: '', quantity: 1, personalized: '' }];

        return Object.assign({}, order, {
            items,
            _account: account,
            _key: account + '::' + order.order_id,
            _ts: ts,
            _dateKey: dateKey,
            _dateLabel: dateLabel,
            _buyerKey: normalizeText(order.buyer_name),
            _buyerLabel: (order.buyer_name || '').trim() || '(Không có tên buyer)'
        });
    }

    /** Normalize one server page (already filtered + sorted newest first; each order has _account). */
    function normalizePage(orders) {
        return (orders || []).map(o => normalizeOrder(o, o._account || ''));
    }

    return { normalizePage };
})();
