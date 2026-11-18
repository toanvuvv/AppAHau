"""Paginated, filtered order listing on top of etsy_sqlite_order_store.

The orders page asks for ONE page at a time (plus totals and the buyer list)
instead of downloading every order: at ~100k orders the full list was ~80 MB
of JSON per page load.

Filters (all optional, combined with AND):
- account   : one account email
- date_from / date_to : "YYYY-MM-DD", compared to the America/New_York day;
                a from > to range is reported as invalid and ignored
- buyer_key : exact (lower-cased) buyer name; dropped automatically when that
                buyer has no order inside the account + date filter
- search    : whitespace-separated terms, every term must appear (substring,
                case-insensitive) in the order's search text
Sort: newest order date first, then newest inserted.
"""
import json
import math

import etsy_sqlite_order_store as store

MAX_PAGE_SIZE = 500
BUYER_LIST_LIMIT = 300


def _date_key(value):
    """'2026-03-22' -> 20260322, anything else -> None."""
    try:
        y, m, d = (int(p) for p in (value or "").split("-"))
        return y * 10000 + m * 100 + d
    except ValueError:
        return None


def _base_where(account, date_from, date_to):
    """WHERE clause for account + date range. Returns (sql, args, invalid_range)."""
    clauses, args = [], []
    if account:
        clauses.append("account = ?")
        args.append(account)
    from_key, to_key = _date_key(date_from), _date_key(date_to)
    invalid = from_key is not None and to_key is not None and from_key > to_key
    if not invalid and (from_key is not None or to_key is not None):
        clauses.append("date_key IS NOT NULL")
        if from_key is not None:
            clauses.append("date_key >= ?")
            args.append(from_key)
        if to_key is not None:
            clauses.append("date_key <= ?")
            args.append(to_key)
    return clauses, args, invalid


def _add_text_filters(clauses, args, buyer_key, search):
    terms = (search or "").lower().split()
    # With search terms every row's text must be read anyway: '+account'
    # makes that one sequential scan instead of an index walk + random lookups.
    clauses = ["+account = ?" if (terms and c == "account = ?") else c for c in clauses]
    args = list(args)
    if buyer_key is not None:
        clauses.append("buyer_key = ?")
        args.append(buyer_key)
    for term in terms:
        clauses.append("instr(search, ?) > 0")
        args.append(term)
    return clauses, args


def _order_by(filtered):
    """Newest first.

    Unfiltered (or account only): walk the covering date index, stop after
    one page. Filtered by search/date/buyer: '+' stops SQLite from walking the
    date index with one random row lookup per order (seconds at 100k); it
    scans the narrow table (or the buyer index) and sorts only the matches.
    """
    return "+ts DESC, +seq DESC" if filtered else "ts DESC, seq DESC"


def _is_filtered(search, date_from, date_to, buyer_key):
    return bool((search or "").split() or date_from or date_to or buyer_key is not None)


def _sql_where(clauses):
    return ("WHERE " + " AND ".join(clauses)) if clauses else ""


def query_orders(account=None, search="", date_from="", date_to="", buyer_key=None,
                 page=1, page_size=25):
    """One page of orders plus everything the orders page header needs.

    Returns {orders, total, items, unsynced, total_all, page, pages, page_size,
    buyers, buyer_count, buyer_key, invalid_range}. Each order dict carries
    `_account`. `buyer_key` is the filter actually applied (None if dropped).
    """
    page_size = max(1, min(int(page_size or 25), MAX_PAGE_SIZE))
    base, base_args, invalid = _base_where(account, date_from, date_to)
    db = store._db()

    with store._db_lock:
        # Buyer dropdown: account + date filter only (same as before)
        # One pass: top buyers + number of distinct buyers (window count before LIMIT)
        rows = db.execute(
            f"SELECT buyer_key, MAX(buyer_label), COUNT(*) AS n, COUNT(*) OVER () "
            f"FROM orders {_sql_where(base)} "
            f"GROUP BY buyer_key ORDER BY n DESC, MAX(buyer_label) LIMIT ?",
            base_args + [BUYER_LIST_LIMIT]).fetchall()
        buyers = [r[:3] for r in rows]
        buyer_count = rows[0][3] if rows else 0

        if buyer_key is not None:
            hit = db.execute(
                f"SELECT MAX(buyer_label), COUNT(*) FROM orders {_sql_where(base + ['buyer_key = ?'])}",
                base_args + [buyer_key]).fetchone()
            if not hit[1]:
                buyer_key = None  # buyer vanished after a date/account change
            elif all(b[0] != buyer_key for b in buyers):
                buyers.append((buyer_key, hit[0], hit[1]))  # keep selected buyer visible

        where, args = _add_text_filters(base, base_args, buyer_key, search)
        total, items, unsynced = db.execute(
            f"SELECT COUNT(*), COALESCE(SUM(item_count), 0), "
            f"COALESCE(SUM(sheet_synced = 0), 0) FROM orders {_sql_where(where)}", args).fetchone()
        total_all = db.execute(
            "SELECT COUNT(*) FROM orders" + (" WHERE account = ?" if account else ""),
            [account] if account else []).fetchone()[0]

        pages = max(1, math.ceil(total / page_size))
        page = min(max(1, int(page or 1)), pages)
        rows = db.execute(
            f"SELECT o.account, p.data FROM (SELECT seq, account, ts FROM orders {_sql_where(where)} "
            f"ORDER BY {_order_by(_is_filtered(search, date_from, date_to, buyer_key))} "
            f"LIMIT ? OFFSET ?) o "
            f"JOIN order_payload p USING (seq) ORDER BY o.ts DESC, o.seq DESC",
            args + [page_size, (page - 1) * page_size]).fetchall()

    orders = []
    for acc, data in rows:
        order = json.loads(data)
        order["_account"] = acc
        orders.append(order)

    return {
        "orders": orders,
        "total": total,
        "items": items,
        "unsynced": unsynced,
        "total_all": total_all,
        "page": page,
        "pages": pages,
        "page_size": page_size,
        "buyers": [{"key": k, "label": label, "count": n} for k, label, n in buyers],
        "buyer_count": buyer_count,
        "buyer_key": buyer_key,
        "invalid_range": invalid,
    }


def query_order_keys(account=None, search="", date_from="", date_to="", buyer_key=None):
    """[[account, order_id], ...] of every order matching the filters (for 'select all')."""
    base, base_args, _ = _base_where(account, date_from, date_to)
    where, args = _add_text_filters(base, base_args, buyer_key, search)
    with store._db_lock:
        rows = store._db().execute(
            f"SELECT account, order_id FROM orders {_sql_where(where)} "
            f"ORDER BY {_order_by(_is_filtered(search, date_from, date_to, buyer_key))}",
            args).fetchall()
    return [[a, o] for a, o in rows]
