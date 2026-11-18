"""SQLite store for Etsy orders and processed Gmail message IDs.

Why SQLite: with ~100k orders the old JSON file (~90 MB) was rewritten in full
on every save and held entirely in RAM. Here each change touches only its rows
and every write is its own transaction (WAL + synchronous=FULL: a crash or a
killed Docker VM never leaves a half-written database).

Layout (DATA_DIR/etsy/etsy.db):
- orders(seq, account, order_id, message_id, sheet_synced, + derived columns)
  Narrow on purpose: counts, buyer grouping and search scan this table, so
  the big JSON lives elsewhere. `sheet_synced` mirrors the dict flag
  (NULL = order created before the flag existed, counts as synced).
  ts/date_key/buyer_key/buyer_label/item_count/search are computed at insert
  (etsy_order_derived_fields) and drive the paginated list query.
- order_payload(seq, data): the full order dict as JSON, 1:1 with orders
  (ON DELETE CASCADE); only read for the rows actually returned.
- processed_ids(account, message_id)  -- no size cap any more
- unparsed_messages(account, message_id, ...) -- order emails the parser could
  not read. NOT in processed_ids and not labelled on Gmail, so every poll that
  sees them tries again; shown on the dashboard until parsed or dismissed.
- meta(key, value)                     -- schema / migration markers

First start with an existing JSON store: orders + processed IDs are imported
in one transaction, then the JSON files are renamed to *.migrated (kept for
manual rollback, never read again).

All access goes through one connection guarded by a lock, so callers from
several threads (poll loop, background job, HTTP handlers) are safe.
"""
import json
import logging
import os
import sqlite3
import threading

from etsy_json_store_with_backup import load_json_store
from etsy_order_derived_fields import derive_order_fields

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    account      TEXT NOT NULL,
    order_id     TEXT NOT NULL,
    message_id   TEXT,
    sheet_synced INTEGER,
    ts           INTEGER NOT NULL DEFAULT 0,
    date_key     INTEGER,
    buyer_key    TEXT NOT NULL DEFAULT '',
    buyer_label  TEXT NOT NULL DEFAULT '',
    item_count   INTEGER NOT NULL DEFAULT 1,
    search       TEXT NOT NULL DEFAULT '',
    UNIQUE (account, order_id)
);
CREATE INDEX IF NOT EXISTS idx_orders_account_seq ON orders (account, seq);
-- Sort indexes also cover account/order_id: a page or "select all" never reads table rows
CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders (ts DESC, seq DESC, account, order_id);
CREATE INDEX IF NOT EXISTS idx_orders_account_ts ON orders (account, ts DESC, seq DESC, order_id);
-- Covers the buyer list GROUP BY (incl. its account/date filter) and the buyer filter
CREATE INDEX IF NOT EXISTS idx_orders_buyer
    ON orders (buyer_key, buyer_label, account, date_key, ts, seq);
CREATE INDEX IF NOT EXISTS idx_orders_unsynced ON orders (account, seq) WHERE sheet_synced = 0;
-- Covering index: totals + buyer list never touch the table rows
CREATE INDEX IF NOT EXISTS idx_orders_stats
    ON orders (account, date_key, buyer_key, buyer_label, item_count, sheet_synced);
CREATE TABLE IF NOT EXISTS order_payload (
    seq  INTEGER PRIMARY KEY REFERENCES orders (seq) ON DELETE CASCADE,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processed_ids (
    account    TEXT NOT NULL,
    message_id TEXT NOT NULL,
    PRIMARY KEY (account, message_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS unparsed_messages (
    account    TEXT NOT NULL,
    message_id TEXT NOT NULL,
    subject    TEXT NOT NULL DEFAULT '',
    email_date TEXT NOT NULL DEFAULT '',
    reason     TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL,
    PRIMARY KEY (account, message_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

_conn = None
_db_path = None
_db_lock = threading.RLock()


# --- Connection / lifecycle ---


def open_store(db_path, legacy_orders_path=None, legacy_processed_path=None):
    """Open (or create) the database and import legacy JSON once.

    Safe to call again: re-opening the same path is a no-op, a different path
    (tests) closes the old connection first.
    """
    global _conn, _db_path
    with _db_lock:
        if _conn is not None and _db_path == db_path:
            return
        close_store()
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")  # order_payload rows follow their order
        conn.executescript(SCHEMA)
        _conn, _db_path = conn, db_path
        _migrate_legacy_json(legacy_orders_path, legacy_processed_path)


def is_open():
    with _db_lock:
        return _conn is not None


def close_store():
    global _conn, _db_path
    with _db_lock:
        if _conn is not None:
            _conn.close()
        _conn, _db_path = None, None


def _db():
    if _conn is None:
        raise RuntimeError("Etsy store chua mo (open_store)")
    return _conn


class _tx:
    """BEGIN IMMEDIATE ... COMMIT/ROLLBACK under the store lock."""

    def __enter__(self):
        _db_lock.acquire()
        try:
            _db().execute("BEGIN IMMEDIATE")
        except BaseException:
            _db_lock.release()
            raise
        return _db()

    def __exit__(self, exc_type, exc, tb):
        try:
            _db().execute("COMMIT" if exc_type is None else "ROLLBACK")
        finally:
            _db_lock.release()
        return False


def _migrate_legacy_json(orders_path, processed_path):
    """Import etsy_orders.json / etsy_processed_ids.json once, then rename them."""
    done = _db().execute("SELECT value FROM meta WHERE key='legacy_json_imported'").fetchone()
    if done:
        return
    orders = load_json_store(orders_path) if orders_path and os.path.exists(orders_path) else {}
    processed = load_json_store(processed_path) if processed_path and os.path.exists(processed_path) else {}

    n_orders = n_ids = 0
    with _tx() as db:
        for account, account_orders in orders.items():
            for order in account_orders or []:
                if isinstance(order, dict) and order.get("order_id"):
                    n_orders += _insert(db, account, order)
        for account, ids in processed.items():
            for mid in ids or []:
                cur = db.execute("INSERT OR IGNORE INTO processed_ids VALUES (?, ?)", (account, mid))
                n_ids += cur.rowcount
        db.execute("INSERT OR REPLACE INTO meta VALUES ('legacy_json_imported', datetime('now'))")

    for path in (orders_path, processed_path):
        if path and os.path.exists(path):
            os.replace(path, path + ".migrated")
    if orders or processed:
        logger.warning(f"Da chuyen du lieu JSON sang SQLite: {n_orders} don, {n_ids} processed IDs "
                       f"(file cu doi ten thanh *.migrated)")


# --- Orders ---


def _flag_to_db(order):
    flag = order.get("sheet_synced")
    return None if flag is None else int(bool(flag))


def _insert(db, account, order):
    d = derive_order_fields(order, account)
    cur = db.execute(
        "INSERT OR IGNORE INTO orders (account, order_id, message_id, sheet_synced, "
        "ts, date_key, buyer_key, buyer_label, item_count, search) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (account, str(order["order_id"]), order.get("message_id"), _flag_to_db(order),
         d["ts"], d["date_key"], d["buyer_key"], d["buyer_label"], d["item_count"], d["search"]),
    )
    if cur.rowcount != 1:
        return 0
    db.execute("INSERT INTO order_payload (seq, data) VALUES (?, ?)",
               (cur.lastrowid, json.dumps(order, ensure_ascii=False)))
    return 1


def insert_order(account, order):
    """Insert one order. Returns False if (account, order_id) already exists."""
    with _tx() as db:
        return _insert(db, account, order) == 1


def _rows_to_orders(rows):
    return [json.loads(r[0]) for r in rows]


def list_orders(account):
    """One account's orders, oldest first (insertion order)."""
    with _db_lock:
        rows = _db().execute(
            "SELECT p.data FROM orders o JOIN order_payload p USING (seq) "
            "WHERE o.account=? ORDER BY o.seq", (account,)).fetchall()
    return _rows_to_orders(rows)


def list_all_orders():
    """{account: [order, ...]} for every account that has orders."""
    with _db_lock:
        rows = _db().execute(
            "SELECT o.account, p.data FROM orders o JOIN order_payload p USING (seq) ORDER BY o.seq").fetchall()
    result = {}
    for account, data in rows:
        result.setdefault(account, []).append(json.loads(data))
    return result


def count_orders(account):
    with _db_lock:
        return _db().execute("SELECT COUNT(*) FROM orders WHERE account=?", (account,)).fetchone()[0]


def list_unsynced_orders(account):
    """Orders explicitly marked sheet_synced=False (new or failed Sheet writes)."""
    with _db_lock:
        rows = _db().execute(
            "SELECT p.data FROM orders o JOIN order_payload p USING (seq) "
            "WHERE o.account=? AND o.sheet_synced=0 ORDER BY o.seq", (account,)).fetchall()
    return _rows_to_orders(rows)


def mark_orders_synced(account, order_ids):
    """Set sheet_synced=True (column + JSON). Returns number of rows changed."""
    if not order_ids:
        return 0
    changed = 0
    with _tx() as db:
        for oid in order_ids:
            row = db.execute(
                "SELECT o.seq, p.data FROM orders o JOIN order_payload p USING (seq) "
                "WHERE o.account=? AND o.order_id=? AND (o.sheet_synced IS NULL OR o.sheet_synced=0)",
                (account, str(oid))).fetchone()
            if not row:
                continue
            order = json.loads(row[1])
            if order.get("sheet_synced") is True:
                continue
            order["sheet_synced"] = True
            db.execute("UPDATE orders SET sheet_synced=1 WHERE seq=?", (row[0],))
            db.execute("UPDATE order_payload SET data=? WHERE seq=?",
                       (json.dumps(order, ensure_ascii=False), row[0]))
            changed += 1
    return changed


def delete_orders(account, order_ids):
    """Delete orders by id. Returns [message_id, ...] of the removed orders (None kept)."""
    removed = []
    with _tx() as db:
        for oid in order_ids:
            row = db.execute("SELECT seq, message_id FROM orders WHERE account=? AND order_id=?",
                             (account, str(oid))).fetchone()
            if row:
                db.execute("DELETE FROM orders WHERE seq=?", (row[0],))
                removed.append(row[1])
    return removed


def delete_all_orders(account=None):
    """Delete every order (of one account, or all). Returns {account: [message_id, ...]}."""
    where, args = ("WHERE account=?", (account,)) if account else ("", ())
    with _tx() as db:
        rows = db.execute(f"SELECT account, message_id FROM orders {where}", args).fetchall()
        db.execute(f"DELETE FROM orders {where}", args)
    removed = {}
    for acc, mid in rows:
        removed.setdefault(acc, []).append(mid)
    return removed


# --- Processed message IDs ---


def get_processed_ids(account):
    with _db_lock:
        rows = _db().execute("SELECT message_id FROM processed_ids WHERE account=?", (account,)).fetchall()
    return {r[0] for r in rows}


def add_processed_id(account, message_id):
    """Mark a message handled; it also leaves the unparsed list (parsed later or dismissed)."""
    with _tx() as db:
        db.execute("INSERT OR IGNORE INTO processed_ids VALUES (?, ?)", (account, message_id))
        db.execute("DELETE FROM unparsed_messages WHERE account=? AND message_id=?", (account, message_id))


def remove_processed_ids(account, message_ids):
    with _tx() as db:
        db.executemany("DELETE FROM processed_ids WHERE account=? AND message_id=?",
                       [(account, m) for m in message_ids if m])


def clear_processed_ids(account=None):
    with _tx() as db:
        if account:
            db.execute("DELETE FROM processed_ids WHERE account=?", (account,))
        else:
            db.execute("DELETE FROM processed_ids")


def delete_account(account):
    """Remove all orders, processed IDs and unparsed emails of an account. Returns orders deleted."""
    with _tx() as db:
        n = db.execute("DELETE FROM orders WHERE account=?", (account,)).rowcount
        db.execute("DELETE FROM processed_ids WHERE account=?", (account,))
        db.execute("DELETE FROM unparsed_messages WHERE account=?", (account,))
    return n


# --- Unparsed order emails ---


def record_unparsed(account, message_id, subject, email_date, reason, first_seen):
    """Remember an order email the parser could not read. Returns True if it is new."""
    with _tx() as db:
        cur = db.execute(
            "INSERT OR IGNORE INTO unparsed_messages "
            "(account, message_id, subject, email_date, reason, first_seen) VALUES (?, ?, ?, ?, ?, ?)",
            (account, message_id, subject or "", email_date or "", reason or "", first_seen))
    return cur.rowcount == 1


def list_unparsed(account):
    """Unparsed emails of one account, oldest first, as dicts."""
    with _db_lock:
        rows = _db().execute(
            "SELECT message_id, subject, email_date, reason, first_seen FROM unparsed_messages "
            "WHERE account=? ORDER BY first_seen", (account,)).fetchall()
    keys = ("message_id", "subject", "email_date", "reason", "first_seen")
    return [dict(zip(keys, r)) for r in rows]



# --- Backup ---


def snapshot(src_path, dest_path):
    """Write a consistent copy of the database at src_path to dest_path.

    Uses SQLite's online backup API, so it is safe while the app is writing.
    Do not copy etsy.db with cp/tar instead: pages still in the WAL file
    would be missing. Opens the source read-only and never runs migration.
    """
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    tmp = dest_path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dest = sqlite3.connect(tmp)
    try:
        src.backup(dest)
    finally:
        dest.close()
        src.close()
    os.replace(tmp, dest_path)


if __name__ == "__main__":
    # CLI used by the host backup script: python etsy_sqlite_order_store.py snapshot <dest>
    import sys
    from main import ETSY_DB_FILE

    if len(sys.argv) != 3 or sys.argv[1] != "snapshot":
        sys.exit("usage: python etsy_sqlite_order_store.py snapshot <dest.db>")
    if not os.path.exists(ETSY_DB_FILE):
        sys.exit(f"chua co database: {ETSY_DB_FILE}")
    snapshot(ETSY_DB_FILE, sys.argv[2])
    print(f"snapshot -> {sys.argv[2]}")
