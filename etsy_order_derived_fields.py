"""Fields derived from an order dict once, at insert time, so the SQLite store
can sort, filter and paginate without parsing every order on each request.

Mirrors the browser's former client-side logic (etsy-orders-data-normalize-and-filter.js):
- date: Gmail `email_date` (RFC 2822) first, then parser `date`; a date without
  a year is ignored (JS used to turn "Mar 3" into 2001). date_key is the
  America/New_York calendar day as YYYYMMDD (what the date filter compares).
- buyer_key: trimmed lower-case buyer name ("" groups orders without a name).
- search: lower-cased text of order + item fields + account, joined by \\x01 so
  a search term never matches across two fields.
- item_count: number of items, at least 1 (an order without items shows one row).
"""
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

NY_TZ = ZoneInfo("America/New_York")

_VN_DATE = re.compile(r"^(\d{1,2})\s+thg\s+(\d{1,2}),\s*(\d{4})$", re.IGNORECASE)
_YEAR = re.compile(r"\b\d{4}\b")
_PLAIN_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%Y-%m-%d", "%m/%d/%Y")

ORDER_SEARCH_FIELDS = ("order_id", "buyer_name", "address_1", "address_2", "city",
                       "state", "zipcode", "country", "phone")
ITEM_SEARCH_FIELDS = ("title", "sku", "shop", "variation_1", "variation_2", "personalized")


def _parse_one(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    m = _VN_DATE.match(raw)
    if m:
        try:
            return datetime(int(m[3]), int(m[2]), int(m[1]), tzinfo=NY_TZ)
        except ValueError:
            return None
    if not _YEAR.search(raw):
        return None
    try:
        dt = parsedate_to_datetime(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=NY_TZ)
    except (TypeError, ValueError, IndexError):
        pass
    for fmt in _PLAIN_FORMATS:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=NY_TZ)
        except ValueError:
            continue
    return None


def parse_order_datetime(order):
    """Best order date as an aware datetime, or None."""
    for raw in (order.get("email_date"), order.get("date")):
        dt = _parse_one(raw)
        if dt:
            return dt
    return None


def derive_order_fields(order, account):
    """Return {ts, date_key, buyer_key, buyer_label, item_count, search} for one order."""
    dt = parse_order_datetime(order)
    ts, date_key = 0, None
    if dt:
        ts = int(dt.timestamp())
        ny = dt.astimezone(NY_TZ)
        date_key = ny.year * 10000 + ny.month * 100 + ny.day

    buyer_name = str(order.get("buyer_name") or "")
    items = order.get("items") or []
    parts = [str(order.get(f) or "") for f in ORDER_SEARCH_FIELDS] + [account]
    for item in items:
        if isinstance(item, dict):
            parts += [str(item.get(f) or "") for f in ITEM_SEARCH_FIELDS]

    return {
        "ts": ts,
        "date_key": date_key,
        "buyer_key": buyer_name.strip().lower(),
        "buyer_label": buyer_name.strip() or "(Không có tên buyer)",
        "item_count": max(1, len(items)),
        "search": "\x01".join(p for p in parts if p).lower(),
    }
