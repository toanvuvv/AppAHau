"""Write Etsy order data to Google Sheet via gspread."""
import logging
import zlib
from collections import defaultdict
from datetime import datetime
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import gspread

NY_TZ = ZoneInfo("America/New_York")

# (connect, read) seconds. gspread has no timeout by default, so a hung
# connection used to block the poll thread forever while holding the poll mutex.
SHEETS_TIMEOUT = (10, 60)

logger = logging.getLogger(__name__)

# Tool owns only columns A..S (Date -> Phone Number). Column T onward belongs to
# the seller (manual input / formulas) and must never be written, not even blanks.
ETSY_HEADER_ROW = [
    "Date", "Mã Shop", "Kí Hiệu", "Order ID", "Title", "SKU", "Thumbnail",
    "Variation 1", "Variation 2", "Quantity", "Personalized note",
    "Buyer Name", "Address 1", "Address 2", "City", "State",
    "Zipcode", "Country", "Phone Number",
]


def _ensure_header_row(worksheet):
    """Write the A..S header only when row 1 is completely empty (new sheet).

    Row 1 is owned by the seller once it has any content: the tool never
    overwrites or renames existing header cells.
    """
    row1 = worksheet.row_values(1)
    if any(str(cell).strip() for cell in row1):
        return

    worksheet.update([ETSY_HEADER_ROW], "A1")
    logger.info("Sheet trong dong 1 -- da ghi header A:S")


def _col_letter(idx):
    """0-based column index -> A1 letter (A..Z is all the tool needs)."""
    return chr(ord("A") + idx)


def _read_data_columns(worksheet, col_indices):
    """Read only some columns of the data rows (row 2 down) in ONE request.

    Returns rows shaped like get_all_values()[1:] restricted to A..S: each row
    has len(ETSY_HEADER_ROW) cells, filled only at col_indices. Reading 2-4
    columns instead of the whole sheet (incl. the seller's columns from T on)
    keeps a 100k-row sheet within the request timeout.
    """
    ranges = [f"{_col_letter(i)}2:{_col_letter(i)}" for i in col_indices]
    columns = worksheet.batch_get(ranges)
    n_rows = max((len(c) for c in columns), default=0)
    rows = [[""] * len(ETSY_HEADER_ROW) for _ in range(n_rows)]
    for idx, column in zip(col_indices, columns):
        for r, cell in enumerate(column):
            rows[r][idx] = str(cell[0]) if cell else ""
    return rows


def _get_order_timestamp(order):
    """Get timestamp from order for date-based sorting/positioning."""
    email_date = order.get("email_date", "")
    if email_date:
        try:
            return parsedate_to_datetime(email_date).timestamp()
        except Exception:
            pass
    processed_at = order.get("processed_at", "")
    if processed_at:
        try:
            return datetime.fromisoformat(processed_at).timestamp()
        except Exception:
            pass
    return 0


def _sort_orders_newest_first(orders):
    """Sort orders by date descending (newest first)."""
    return sorted(orders, key=_get_order_timestamp, reverse=True)


def _parse_sheet_date(date_str):
    """Parse MM/DD/YYYY HH:MM from sheet column A to timestamp."""
    try:
        dt = datetime.strptime(date_str.strip(), "%m/%d/%Y %H:%M")
        return dt.replace(tzinfo=NY_TZ).timestamp()
    except Exception:
        return 0


def _find_insert_row(existing_ts, new_ts):
    """Find 1-based row for insertion in a descending-date sheet.

    Sheet layout: row 1 = header, row 2 = newest, row N = oldest.
    Returns the row where the new order should be inserted to maintain order.
    """
    for i, ts in enumerate(existing_ts):
        if new_ts >= ts:
            return i + 2  # +2: row 1 = header, i is 0-based
    return len(existing_ts) + 2


def _open_worksheet(creds, sheet_id, sheet_tab):
    """Authorize with a request timeout and open the target tab. Raises on failure."""
    client = gspread.authorize(creds)
    client.set_timeout(SHEETS_TIMEOUT)
    return client.open_by_key(sheet_id).worksheet(sheet_tab)


def write_etsy_orders_to_sheet(creds, sheet_id, sheet_tab, orders):
    """
    Write Etsy orders to a Google Sheet. One row per item.

    Args:
        creds: google.oauth2.credentials.Credentials
        sheet_id: Google Sheet ID from URL
        sheet_tab: Worksheet tab name
        orders: list of order dicts from etsy_parser

    Returns:
        int: number of rows successfully written
    """
    if not orders:
        return 0

    try:
        worksheet = _open_worksheet(creds, sheet_id, sheet_tab)
    except Exception as e:
        logger.error(f"Loi mo Google Sheet '{sheet_id}' tab '{sheet_tab}': {e}")
        return 0

    try:
        _ensure_header_row(worksheet)
    except Exception as e:
        logger.error(f"Loi doc Sheet: {e}")
        return 0

    # Read existing dates for position-aware insertion
    try:
        existing_dates = worksheet.col_values(1)[1:]  # skip header
    except Exception:
        existing_dates = []
    existing_ts = [_parse_sheet_date(d) for d in existing_dates]

    sorted_orders = _sort_orders_newest_first(orders)

    # Group orders by their target insert position
    position_groups = {}
    for order in sorted_orders:
        order_ts = _get_order_timestamp(order)
        insert_row = _find_insert_row(existing_ts, order_ts)
        if insert_row not in position_groups:
            position_groups[insert_row] = []
        position_groups[insert_row].extend(_flatten_order_to_rows(order))

    if not position_groups:
        return 0

    count = 0
    try:
        # Insert from bottom to top to avoid row offset issues
        for row_num in sorted(position_groups.keys(), reverse=True):
            rows = position_groups[row_num]
            worksheet.insert_rows(rows, row=row_num, value_input_option="USER_ENTERED")
            count += len(rows)
    except Exception as e:
        logger.error(f"Loi ghi orders: {e}")

    if count > 0:
        highlight_duplicate_addresses(worksheet, {str(o.get("order_id", "")).strip() for o in orders})

    return count


def sync_etsy_orders_to_sheet(creds, sheet_id, sheet_tab, orders):
    """
    Sync Etsy orders to Google Sheet — skip orders already on the Sheet.

    Checks existing Order IDs in column D, only writes orders not yet present.
    Safe to call repeatedly (used as the retry path for failed writes).

    Returns:
        dict: {"written": int, "skipped": int} plus "error": str when the
        sheet could not be opened/read or an insert failed. Callers must treat
        any "error" as "not fully synced" and retry later.
    """
    if not orders:
        return {"written": 0, "skipped": 0}

    try:
        worksheet = _open_worksheet(creds, sheet_id, sheet_tab)
    except Exception as e:
        logger.error(f"Loi mo Google Sheet '{sheet_id}' tab '{sheet_tab}': {e}")
        return {"written": 0, "skipped": 0, "error": str(e)}

    try:
        _ensure_header_row(worksheet)
        # Only Date (A) and Order ID (D) are needed: dedupe + insert position
        existing = _read_data_columns(worksheet, (0, 3))
    except Exception as e:
        logger.error(f"Loi doc Sheet: {e}")
        return {"written": 0, "skipped": 0, "error": str(e)}

    existing_ids = {row[3].strip() for row in existing if row[3].strip()}
    logger.info(f"Sheet co {len(existing_ids)} order IDs")

    # Read existing dates for position-aware insertion
    existing_ts = [_parse_sheet_date(row[0]) for row in existing]

    skipped = 0
    sorted_orders = _sort_orders_newest_first(orders)
    new_order_ids = set()

    # Group new orders by their target insert position
    position_groups = {}
    for order in sorted_orders:
        order_id = str(order.get("order_id", "")).strip()
        if order_id in existing_ids:
            skipped += 1
            continue

        order_ts = _get_order_timestamp(order)
        insert_row = _find_insert_row(existing_ts, order_ts)
        if insert_row not in position_groups:
            position_groups[insert_row] = []
        position_groups[insert_row].extend(_flatten_order_to_rows(order))
        existing_ids.add(order_id)
        new_order_ids.add(order_id)

    written = 0
    insert_error = None
    if position_groups:
        try:
            for row_num in sorted(position_groups.keys(), reverse=True):
                rows = position_groups[row_num]
                worksheet.insert_rows(rows, row=row_num, value_input_option="USER_ENTERED")
                written += len(rows)
        except Exception as e:
            insert_error = str(e)
            logger.error(f"Loi ghi orders: {e}")

    if written > 0:
        highlight_duplicate_addresses(worksheet, new_order_ids)

    result = {"written": written, "skipped": skipped}
    if insert_error:
        result["error"] = insert_error
    return result


PASTEL_COLORS = [
    {"red": 1.0, "green": 0.95, "blue": 0.8},    # light yellow
    {"red": 0.82, "green": 0.88, "blue": 1.0},    # light blue
    {"red": 0.82, "green": 1.0, "blue": 0.88},    # light green
    {"red": 1.0, "green": 0.85, "blue": 0.85},    # light pink
    {"red": 0.9, "green": 0.85, "blue": 1.0},     # light purple
    {"red": 1.0, "green": 0.9, "blue": 0.8},      # light orange
]

WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}

# Column indices for matching key: Buyer Name (L=11), Address 1 (M=12), Address 2 (N=13)
_KEY_COLS = (11, 12, 13)


def _address_key(row):
    """Build a grouping key from Buyer Name + Address 1 + Address 2."""
    parts = []
    for col in _KEY_COLS:
        val = row[col].strip().lower() if col < len(row) else ""
        parts.append(val)
    return tuple(parts)


def _make_color_request(sheet_id, row_idx, num_cols, color):
    """Create a Sheets API repeatCell request to set background color for a row."""
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": row_idx,
                "endRowIndex": row_idx + 1,
                "startColumnIndex": 0,
                "endColumnIndex": num_cols,
            },
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": color,
                }
            },
            "fields": "userEnteredFormat.backgroundColor",
        }
    }


def _color_for_key(key):
    """Stable pastel colour per address group (same group keeps its colour)."""
    return PASTEL_COLORS[zlib.crc32("|".join(key).encode("utf-8")) % len(PASTEL_COLORS)]


def highlight_duplicate_addresses(worksheet, new_order_ids=None):
    """Highlight rows that share the same Buyer Name + Address 1 + Address 2.

    new_order_ids given (normal path): only groups that contain a just-written
    order are recoloured, so the request count stays small as the sheet grows.
    Groups with >1 row get a stable pastel colour; a new single row is reset to
    white (inserted rows can inherit a neighbour's colour).
    new_order_ids None: recolour the whole sheet (legacy full pass).
    """
    try:
        # Order ID (D) + Buyer Name / Address 1 / Address 2 (L, M, N) only
        data_rows = _read_data_columns(worksheet, (3,) + _KEY_COLS)
    except Exception as e:
        logger.error(f"Loi doc Sheet khi highlight: {e}")
        return

    if not data_rows:
        return

    num_cols = len(ETSY_HEADER_ROW)  # color A..S only; seller columns keep their formatting
    ws_sheet_id = worksheet.id

    # Group row indices by address key; remember which keys hold a new order
    groups = defaultdict(list)
    touched_keys = set()
    for i, row in enumerate(data_rows):
        key = _address_key(row)
        groups[key].append(i + 1)  # +1 because row 0 is header
        if new_order_ids is not None and len(row) > 3 and str(row[3]).strip() in new_order_ids:
            touched_keys.add(key)

    requests = []
    dup_groups = 0
    for key, row_indices in groups.items():
        if new_order_ids is not None and key not in touched_keys:
            continue
        if len(row_indices) > 1:
            color = _color_for_key(key)
            dup_groups += 1
        else:
            color = WHITE
        for ri in row_indices:
            requests.append(_make_color_request(ws_sheet_id, ri, num_cols, color))

    if not requests:
        return

    try:
        worksheet.spreadsheet.batch_update({"requests": requests})
        logger.info(f"Highlight xong: {dup_groups} nhom trung dia chi ({len(requests)} dong)")
    except Exception as e:
        logger.error(f"Loi highlight duplicate addresses: {e}")


def _convert_date_to_ny(order):
    """Convert order date to New York time formatted string.

    Tries email_date (RFC 2822 from Gmail header) first, then parser date.
    Returns MM/DD/YYYY HH:MM format in America/New_York timezone.
    """
    # Try RFC 2822 email header date first (has timezone info)
    email_date = order.get("email_date", "")
    if email_date:
        try:
            dt = parsedate_to_datetime(email_date)
            ny_dt = dt.astimezone(NY_TZ)
            return ny_dt.strftime("%m/%d/%Y %H:%M")
        except Exception:
            pass

    # Try processed_at (ISO format with timezone)
    processed_at = order.get("processed_at", "")
    if processed_at:
        try:
            dt = datetime.fromisoformat(processed_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=NY_TZ)
            ny_dt = dt.astimezone(NY_TZ)
            return ny_dt.strftime("%m/%d/%Y %H:%M")
        except Exception:
            pass

    # Fallback: use current NY time so the Sheet always gets a consistent timezone
    return datetime.now(NY_TZ).strftime("%m/%d/%Y %H:%M")


def _flatten_order_to_rows(order):
    """Convert an order dict to a list of Sheet rows (one per item).

    Each row has exactly len(ETSY_HEADER_ROW) cells (A..S) so nothing is
    written to the seller-owned columns from T onward.
    """
    items = order.get("items", [])
    if not items:
        items = [{}]

    ny_date = _convert_date_to_ny(order)

    rows = []
    for item in items:
        rows.append([
            ny_date,
            item.get("shop", ""),
            "",  # Kí Hiệu
            order.get("order_id", ""),
            item.get("title", ""),
            item.get("sku", ""),
            f'=IMAGE("{item.get("thumbnail", "")}")' if item.get("thumbnail") else "",
            item.get("variation_1", ""),
            item.get("variation_2", ""),
            item.get("quantity", 1),
            item.get("personalized", ""),
            order.get("buyer_name", ""),
            order.get("address_1", ""),
            order.get("address_2", ""),
            order.get("city", ""),
            order.get("state", ""),
            order.get("zipcode", ""),
            order.get("country", ""),
            order.get("phone", ""),
        ])
    return rows
