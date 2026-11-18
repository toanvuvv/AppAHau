"""Etsy order polling engine -- fetches sale emails, parses orders, stores results.

Threading model (single gunicorn worker):
- One background thread runs the scheduled poll loop (start_etsy_polling).
- Manual actions (Poll Now, Sync Sheet) run as ONE background job at a time
  (start_job) so HTTP requests return immediately.
- _poll_mutex guarantees only one poll/sync touches Gmail/Sheets at a time.
- _lock guards orders_version, last-poll times and the job state; it is never
  held during network I/O. Orders and processed IDs live in SQLite
  (etsy_sqlite_order_store), which serialises its own access.
"""
import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import schedule as schedule_lib

from main import (
    ETSY_DB_FILE,
    ETSY_ORDERS_FILE,
    ETSY_PROCESSED_FILE,
    ETSY_LAST_POLL_FILE,
    load_config_safe as load_config,
    load_credentials,
    setup_logging,
)
from reader import (
    build_gmail_service,
    get_email_full_body,
    get_email_html_body,
    mark_email_processed,
    parse_email_headers,
    unmark_email_processed,
)
from etsy_parser import parse_etsy_sale_email
from etsy_json_store_with_backup import load_json_store, save_json_store
import etsy_sqlite_order_store as store
import etsy_sqlite_order_list_query as order_query
from etsy_polling_heartbeat_watchdog import mark_poll_finished, mark_poll_started, mark_tick
from writer import sync_etsy_orders_to_sheet
from sheet_write_error_explainer import OTHER as OTHER_SHEET_ERROR, describe_sheet_error

# --- Constants ---
DEFAULT_ETSY_QUERY = (
    '(subject:"You made a sale on Etsy" OR subject:"Etsy Order confirmation for") -label:processed'
)

NY_TZ = ZoneInfo("America/New_York")

# Auto poll re-scans this far before the previous poll START. Emails that arrive
# mid-poll or are indexed late by Gmail are not lost; known_ids dedupes the rest.
AUTO_POLL_OVERLAP_SECONDS = 24 * 3600
GMAIL_NUM_RETRIES = 2
LOOP_RETRY_MIN_SECONDS = 30
LOOP_RETRY_MAX_SECONDS = 300

logger = logging.getLogger(__name__)
setup_logging()

# Own scheduler to avoid conflict with main.py's schedule
etsy_scheduler = schedule_lib.Scheduler()

# Threading lock for shared state
_lock = threading.Lock()

# Mutex to prevent concurrent poll/sync cycles (auto poll vs manual jobs)
_poll_mutex = threading.Lock()

# Small in-memory state (orders + processed IDs are in SQLite)
etsy_last_poll = {}    # {email: epoch_int} -- start time of last successful Gmail listing
polling_enabled = True  # Global toggle from dashboard

# Last failed Google Sheet write per account, shown on the account card until
# a later write to that account succeeds. {email: {message, kind, sheet_id,
# sheet_tab, row, raw, at}}. In memory only: a still-broken sheet is reported
# again by the next poll/sync (its orders stay sheet_synced=0 and are retried).
sheet_errors = {}

# Bumped on every orders mutation; the UI reloads the order list only when it
# changes. Seeded with the clock so a restarted worker never repeats a version.
orders_version = int(time.time() * 1000)

# State of the single manual background job (Poll Now / Sync Sheet)
_job = {"kind": None, "target": None, "state": "idle", "started_at": None,
        "finished_at": None, "result": None, "error": None}


# --- Storage ---


def _ensure_store():
    """Open the SQLite store on first use (imports legacy JSON files once)."""
    if not store.is_open():
        store.open_store(ETSY_DB_FILE, ETSY_ORDERS_FILE, ETSY_PROCESSED_FILE)


def load_etsy_last_poll():
    """Load last poll timestamps per account. Returns {email: epoch_int}."""
    return load_json_store(ETSY_LAST_POLL_FILE)


def save_etsy_last_poll(data):
    """Persist last poll timestamps (atomic write)."""
    save_json_store(ETSY_LAST_POLL_FILE, data, keep_backup=False)


def _bump_orders_version():
    """Signal the UI that the order list changed."""
    global orders_version
    with _lock:
        orders_version += 1


# --- Helpers ---


def _find_account(email):
    for acc in load_config().get("accounts", []):
        if acc["email"] == email:
            return acc
    return None


def _unmark_on_gmail(account_email, message_id):
    """Remove 'processed' label from a Gmail message so it can be re-polled."""
    try:
        acc = _find_account(account_email)
        if acc:
            creds = load_credentials(acc.get("token_file", ""))
            if creds:
                service = build_gmail_service(creds)
                unmark_email_processed(service, message_id)
                logger.info(f"[Etsy][{account_email}] Da go label 'processed' cho message {message_id}")
    except Exception as e:
        logger.warning(f"[Etsy][{account_email}] Khong the go label Gmail: {e}")


def _track_processed(email_addr, msg_id):
    store.add_processed_id(email_addr, msg_id)


def _record_unparsed(email_addr, msg_id, headers, reason):
    """Keep an unreadable order email visible instead of silently marking it processed."""
    subject = headers.get("subject", "")
    if store.record_unparsed(email_addr, msg_id, subject, headers.get("date", ""), reason,
                             datetime.now(NY_TZ).isoformat()):
        logger.error(f"[Etsy][{email_addr}] Khong parse duoc email '{subject}' ({msg_id}) "
                     f"-- da dua vao danh sach 'email khong doc duoc' tren dashboard")
    else:
        logger.info(f"[Etsy][{email_addr}] Van chua parse duoc email {msg_id}")


def dismiss_unparsed_email(account_email, message_id):
    """User confirmed an unreadable email is not an order: mark it processed for good."""
    _ensure_store()
    if not any(u["message_id"] == message_id for u in store.list_unparsed(account_email)):
        return False
    _track_processed(account_email, message_id)  # also removes it from the unparsed list
    try:
        acc = _find_account(account_email)
        creds = load_credentials(acc.get("token_file", "")) if acc else None
        if creds:
            mark_email_processed(build_gmail_service(creds), message_id, "processed")
    except Exception as e:
        # processed_ids already skips it; the label only keeps Gmail tidy
        logger.warning(f"[Etsy][{account_email}] Khong gan duoc label cho {message_id}: {e}")
    logger.info(f"[Etsy][{account_email}] Da bo qua email khong doc duoc {message_id}")
    return True


# --- Sheet sync ---


def _mark_orders_synced(email_addr, order_ids):
    """Set sheet_synced=True for the given orders."""
    if store.mark_orders_synced(email_addr, order_ids):
        _bump_orders_version()


def _record_sheet_result(email_addr, sheet_id, sheet_tab, result):
    """Remember (or clear) the account's Sheet write error for the dashboard card."""
    if not result.get("error"):
        with _lock:
            sheet_errors.pop(email_addr, None)
        return
    kind = result.get("error_kind", OTHER_SHEET_ERROR)
    row = result.get("error_row")
    with _lock:
        sheet_errors[email_addr] = {
            "message": describe_sheet_error(kind, email_addr, sheet_tab, row, result["error"]),
            "kind": kind, "sheet_id": sheet_id, "sheet_tab": sheet_tab, "row": row,
            "raw": result["error"], "at": time.time(),
        }


def _sync_pending_orders(email_addr, creds, sheet_id, sheet_tab):
    """Write every order still marked sheet_synced=False (new + earlier failures).

    Orders without the flag (created before it existed) count as synced.
    sync_etsy_orders_to_sheet skips Order IDs already on the Sheet, so a retry
    after a partial write never duplicates rows.
    """
    pending = store.list_unsynced_orders(email_addr)
    if not pending:
        return {"written": 0, "skipped": 0}

    result = sync_etsy_orders_to_sheet(creds, sheet_id, sheet_tab, pending)
    _record_sheet_result(email_addr, sheet_id, sheet_tab, result)
    if result.get("error"):
        logger.error(f"[Etsy][{email_addr}] Ghi Sheet {sheet_id} | tab '{sheet_tab}' loi, "
                     f"{len(pending)} don se ghi lai o chu ky sau: {result['error']}")
        return result

    _mark_orders_synced(email_addr, {o["order_id"] for o in pending})
    logger.info(f"[Etsy][{email_addr}] Da ghi {result.get('written', 0)} dong vao Sheet "
                f"(bo qua {result.get('skipped', 0)} da co)")
    return result


def sync_orders_to_sheet(account_email=None):
    """Manual 'Sync Sheet': push ALL stored orders (dedupe by Order ID).

    Returns {"written", "skipped", "errors"}. Caller must hold _poll_mutex
    (run it through start_job) so it never races an auto poll.
    """
    _ensure_store()
    accounts = [a for a in load_config().get("accounts", [])
                if not account_email or a["email"] == account_email]
    if not accounts:
        raise ValueError("Khong tim thay tai khoan")

    total_written, total_skipped, errors = 0, 0, []
    for acc in accounts:
        email = acc["email"]
        sheet_id = acc.get("sheet_id", "")
        if not sheet_id:
            errors.append(f"{email}: chua co sheet_id")
            continue
        account_orders = store.list_orders(email)
        if not account_orders:
            continue
        creds = load_credentials(acc.get("token_file", ""))
        if not creds:
            errors.append(f"{email}: token khong hop le")
            continue

        sheet_tab = acc.get("sheet_tab", "Sheet1")
        logger.info(f"[Sync][{email}] Bat dau sync {len(account_orders)} don -> Sheet {sheet_id} | tab '{sheet_tab}'")
        result = sync_etsy_orders_to_sheet(creds, sheet_id, sheet_tab, account_orders)
        _record_sheet_result(email, sheet_id, sheet_tab, result)
        total_written += result.get("written", 0)
        total_skipped += result.get("skipped", 0)
        if result.get("error"):
            errors.append(f"{email}: {result['error']}")
            logger.error(f"[Sync][{email}] Ghi Sheet {sheet_id} | tab '{sheet_tab}' LOI "
                         f"(written={result.get('written')}): {result['error']}")
        else:
            _mark_orders_synced(email, {o["order_id"] for o in account_orders})
        logger.info(f"[Sync][{email}] written={result.get('written')}, skipped={result.get('skipped')}")

    return {"written": total_written, "skipped": total_skipped, "errors": errors}


# --- Polling ---


def _build_query(account, force, after_date, before_date):
    """Gmail query with optional date filters (dates are UTC+7 Vietnam days)."""
    email_addr = account["email"]
    query = account.get("etsy_query") or DEFAULT_ETSY_QUERY
    vn_tz = timezone(timedelta(hours=7))
    if after_date:
        # Manual date from Poll Now UI
        try:
            dt = datetime.strptime(after_date, "%Y-%m-%d").replace(tzinfo=vn_tz)
            query += f" after:{int(dt.timestamp())}"
        except ValueError:
            logger.warning(f"[Etsy][{email_addr}] Invalid after_date: {after_date}")
    elif not force:
        with _lock:
            last_ts = etsy_last_poll.get(email_addr)
        if last_ts:
            since = int(last_ts) - AUTO_POLL_OVERLAP_SECONDS
            query += f" after:{since}"
            logger.info(f"[Etsy][{email_addr}] Auto poll tu moc "
                        f"{datetime.fromtimestamp(since, tz=vn_tz).strftime('%Y-%m-%d %H:%M')} (lui 1 ngay)")
    if before_date:
        try:
            dt = datetime.strptime(before_date, "%Y-%m-%d").replace(tzinfo=vn_tz)
            dt += timedelta(days=1)  # Gmail before: is exclusive, +1 day to make inclusive
            query += f" before:{int(dt.timestamp())}"
        except ValueError:
            logger.warning(f"[Etsy][{email_addr}] Invalid before_date: {before_date}")
    return query


def _poll_single_account(account, force=False, after_date=None, before_date=None):
    """Poll a single account for new Etsy orders. Returns number of new orders found.

    Caller holds _poll_mutex. after_date/before_date: optional "YYYY-MM-DD".
    """
    _ensure_store()
    email_addr = account["email"]
    sheet_id = account.get("sheet_id", "")
    sheet_tab = account.get("sheet_tab", "Sheet1")
    poll_started_ts = int(time.time())

    etsy_query = _build_query(account, force, after_date, before_date)
    logger.info(f"[Etsy][{email_addr}] query='{etsy_query}'")
    creds = load_credentials(account.get("token_file", ""))
    if not creds:
        logger.error(f"[Etsy][{email_addr}] Khong tim thay token: {account.get('token_file', '')}")
        return 0

    logger.info(f"[Etsy][{email_addr}] Token OK -- valid={creds.valid}, expired={creds.expired}")
    service = build_gmail_service(creds)

    known_ids = store.get_processed_ids(email_addr)
    logger.info(f"[Etsy][{email_addr}] Da co {len(known_ids)} processed IDs")

    # Fetch all result pages (NO lock needed -- external API call)
    messages = []
    next_page_token = None
    try:
        while True:
            list_kwargs = {"userId": "me", "q": etsy_query, "maxResults": 20}
            if next_page_token:
                list_kwargs["pageToken"] = next_page_token
            response = service.users().messages().list(**list_kwargs).execute(num_retries=GMAIL_NUM_RETRIES)
            messages.extend(response.get("messages", []))
            next_page_token = response.get("nextPageToken")
            if not next_page_token:
                break
    except Exception as e:
        logger.error(f"[Etsy][{email_addr}] Gmail API messages.list FAILED: {e}")
        return 0

    logger.info(f"[Etsy][{email_addr}] Gmail API tra ve {len(messages)} messages")

    new_orders = 0
    fetch_failed = 0  # messages we could not download this cycle
    for msg_info in messages:
        msg_id = msg_info["id"]
        if msg_id in known_ids:
            continue

        try:
            msg = service.users().messages().get(
                userId="me", id=msg_id, format="full").execute(num_retries=GMAIL_NUM_RETRIES)
        except Exception as e:
            logger.warning(f"[Etsy][{email_addr}] Khong tai duoc message {msg_id}, thu lai lan sau: {e}")
            fetch_failed += 1
            continue

        payload = msg.get("payload", {})
        parsed_headers = parse_email_headers(payload.get("headers", []))
        plain_text = get_email_full_body(payload)
        html_body = get_email_html_body(payload)
        full_text = f"Subject: {parsed_headers['subject']}\n\n{plain_text}"

        order = parse_etsy_sale_email(full_text, html_body)

        if not (order and order.get("order_id")):
            # Not processed, not labelled: every later poll that sees it parses it
            # again, and the dashboard lists it until it parses or is dismissed.
            _record_unparsed(email_addr, msg_id, parsed_headers, "Khong doc duoc Order ID")
            continue

        # Skip orders without shipping address
        if not order.get("address_1", "").strip():
            logger.warning(f"[Etsy][{email_addr}] Bo qua order #{order['order_id']} -- khong co shipping address")
            _track_processed(email_addr, msg_id)
            continue

        order["message_id"] = msg_id
        order["email_date"] = parsed_headers["date"]
        order["processed_at"] = datetime.now(NY_TZ).isoformat()
        # No sheet configured -> nothing to sync (same as before the flag existed)
        order["sheet_synced"] = not sheet_id

        # Committed before the Gmail label is set: a crash here can at worst
        # re-read the email (deduped by order_id), never lose the order.
        if store.insert_order(email_addr, order):
            new_orders += 1
            _bump_orders_version()
            logger.info(f"[Etsy][{email_addr}] Don moi: #{order['order_id']}")
        else:
            logger.info(f"[Etsy][{email_addr}] Bo qua order trung: {order['order_id']}")

        # Mark processed on Gmail (outside lock -- I/O)
        try:
            mark_email_processed(service, msg_id, "processed")
        except Exception as e:
            logger.warning(f"[Etsy][{email_addr}] Khong the gan label: {e}")
        _track_processed(email_addr, msg_id)

    if new_orders:
        logger.info(f"[Etsy][{email_addr}] Da xu ly {new_orders} don moi")

    # Write new orders + any earlier failed writes; failures stay pending for the next cycle
    if sheet_id:
        _sync_pending_orders(email_addr, creds, sheet_id, sheet_tab)
    elif new_orders:
        logger.info(f"[Etsy][{email_addr}] Chua co sheet_id, bo qua ghi Sheet")

    # Every message downloaded -> next auto poll starts from this poll's START
    # (minus overlap). A failed download keeps the old mark, so the auto poll
    # window never slides past a message we have not read yet.
    if fetch_failed:
        logger.warning(f"[Etsy][{email_addr}] {fetch_failed} message tai loi -- giu moc poll cu de lan sau quet lai")
    else:
        with _lock:
            etsy_last_poll[email_addr] = poll_started_ts
            save_etsy_last_poll(etsy_last_poll)

    return new_orders


def _poll_all_accounts(force=False, after_date=None, before_date=None):
    """One poll cycle over all enabled accounts. Caller holds _poll_mutex."""
    logger.info("=== Bat dau poll ===")
    config = load_config()
    total_new = 0
    accounts = config.get("accounts", [])
    logger.info(f"[Etsy] {len(accounts)} accounts trong config")
    for account in accounts:
        if not force and not account.get("etsy_polling_enabled", False):
            logger.info(f"[Etsy][{account['email']}] etsy_polling_enabled=False, bo qua")
            continue
        try:
            total_new += _poll_single_account(account, force=force, after_date=after_date, before_date=before_date)
        except Exception as e:
            logger.error(f"[Etsy][{account['email']}] Loi: {e}")
    logger.info(f"=== Ket thuc poll -- {total_new} don moi ===")
    return total_new


def poll_etsy(force=False, after_date=None, before_date=None):
    """Run one Etsy polling cycle across all enabled accounts.

    Returns the number of new orders, or None when skipped (polling disabled
    or another poll/sync is running).
    """
    if not force and not polling_enabled:
        logger.info("Polling disabled, bo qua")
        return None

    if not _poll_mutex.acquire(blocking=False):
        logger.warning("=== Poll SKIPPED -- another poll cycle is running ===")
        return None
    mark_poll_started()
    try:
        return _poll_all_accounts(force=force, after_date=after_date, before_date=before_date)
    finally:
        mark_poll_finished()
        _poll_mutex.release()


def poll_one_account(email, after_date=None, before_date=None):
    """Force-poll one account. Caller holds _poll_mutex (use via start_job)."""
    account = _find_account(email)
    if not account:
        raise ValueError(f"Khong tim thay tai khoan {email}")
    return {"new_orders": _poll_single_account(account, force=True, after_date=after_date, before_date=before_date)}


def poll_all_accounts_job(after_date=None, before_date=None):
    """Job wrapper: force-poll all accounts. Caller holds _poll_mutex."""
    return {"new_orders": _poll_all_accounts(force=True, after_date=after_date, before_date=before_date)}


def poll_etsy_account(email, after_date=None, before_date=None):
    """Synchronous single-account poll (kept for scripts/tests). Returns dict."""
    if not _poll_mutex.acquire(blocking=False):
        return {"error": "Dang co poll cycle khac chay, vui long doi"}
    mark_poll_started()
    try:
        return poll_one_account(email, after_date=after_date, before_date=before_date)
    except Exception as e:
        logger.error(f"[Etsy][{email}] Loi: {e}")
        return {"error": str(e)}
    finally:
        mark_poll_finished()
        _poll_mutex.release()


# --- Background jobs (Poll Now / Sync Sheet) ---


def start_job(kind, target, **kwargs):
    """Run target(**kwargs) in a background thread while holding _poll_mutex.

    Returns False immediately if a poll or another job is running (no queue).
    Only one job exists at a time; its state is exposed via get_job().
    """
    if not _poll_mutex.acquire(blocking=False):
        return False
    with _lock:
        _job.update(kind=kind, target=kwargs.get("email"), state="running",
                    started_at=time.time(), finished_at=None, result=None, error=None)
    mark_poll_started()

    def run():
        try:
            result = target(**kwargs)
            with _lock:
                _job.update(state="done", result=result)
        except Exception as e:
            logger.error(f"Job {kind} loi: {e}")
            with _lock:
                _job.update(state="error", error=str(e))
        finally:
            with _lock:
                _job["finished_at"] = time.time()
            mark_poll_finished()
            _poll_mutex.release()

    try:
        threading.Thread(target=run, daemon=True, name=f"job-{kind}").start()
    except Exception:
        mark_poll_finished()
        _poll_mutex.release()
        raise
    return True


def get_job():
    with _lock:
        return dict(_job)


def is_poll_running():
    return _poll_mutex.locked()


# --- Entry points ---


def _reschedule(interval):
    etsy_scheduler.clear("etsy")
    etsy_scheduler.every(interval).minutes.do(poll_etsy).tag("etsy")


def start_etsy_polling():
    """Scheduled polling loop (called from dashboard background thread).

    Never gives up: an unexpected error restarts the loop with backoff
    (30s doubling to 5 min). Each iteration updates the watchdog heartbeat.
    """
    global etsy_last_poll

    _ensure_store()
    with _lock:
        etsy_last_poll = load_etsy_last_poll()
    _bump_orders_version()

    retry_delay = LOOP_RETRY_MIN_SECONDS
    attempt = 0
    while True:
        attempt += 1
        try:
            mark_tick()
            config = load_config()
            current_interval = config.get("etsy_polling_interval_minutes", 5)
            num_accounts = len(config.get("accounts", []))
            logger.info(f"Polling thread started (lan {attempt}) -- {num_accounts} accounts, interval {current_interval}m")

            poll_etsy()  # first poll immediately
            _reschedule(current_interval)

            while True:
                mark_tick()
                try:
                    etsy_scheduler.run_pending()
                except Exception as e:
                    logger.error(f"Scheduler run_pending error: {e}")
                retry_delay = LOOP_RETRY_MIN_SECONDS  # loop is healthy again
                time.sleep(30)

                # Apply interval changes from the dashboard without a restart
                new_interval = load_config().get("etsy_polling_interval_minutes", 5)
                if new_interval != current_interval:
                    logger.info(f"Polling interval changed: {current_interval}m -> {new_interval}m")
                    _reschedule(new_interval)
                    current_interval = new_interval

        except Exception as e:
            logger.error(f"Polling loop CRASHED (lan {attempt}): {e} -- thu lai sau {retry_delay}s")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, LOOP_RETRY_MAX_SECONDS)


def get_etsy_orders(account=None):
    """Return orders for the API: {email: [order, ...]} or one account's list."""
    _ensure_store()
    if account is not None:
        return store.list_orders(account)
    return store.list_all_orders()


def query_etsy_orders(**filters):
    """One filtered page of orders + totals (see etsy_sqlite_order_list_query.query_orders)."""
    _ensure_store()
    return order_query.query_orders(**filters)


def query_etsy_order_keys(**filters):
    """[[account, order_id], ...] matching the filters (page 'select all')."""
    _ensure_store()
    return order_query.query_order_keys(**filters)


def get_orders_version():
    with _lock:
        return orders_version


def _delete_and_unmark(account, order_ids, clear_processed):
    """Delete orders of one account; optionally make their emails re-pollable."""
    removed_msg_ids = store.delete_orders(account, order_ids)
    if not removed_msg_ids:
        return 0
    _bump_orders_version()
    msg_ids = [m for m in removed_msg_ids if m]
    if clear_processed and msg_ids:
        store.remove_processed_ids(account, msg_ids)
        for mid in msg_ids:  # Gmail I/O, outside any lock
            _unmark_on_gmail(account, mid)
    return len(removed_msg_ids)


def delete_etsy_order(account, order_id, clear_processed=False):
    """Delete a single order by order_id from an account.

    Args:
        clear_processed: If True, also remove the message_id from processed IDs
                         and the Gmail label so the email can be re-polled.

    Returns True if found and deleted, False otherwise.
    """
    _ensure_store()
    return _delete_and_unmark(account, [order_id], clear_processed) > 0


def delete_etsy_orders_bulk(order_ids_by_account, clear_processed=False):
    """Delete multiple orders by {account: [order_id, ...]}.

    Returns total number of orders deleted.
    """
    _ensure_store()
    return sum(_delete_and_unmark(account, order_ids, clear_processed)
               for account, order_ids in order_ids_by_account.items() if order_ids)


def delete_account_etsy_data(account):
    """Delete all Etsy data for an account: orders, processed IDs, last poll.

    Used when an account is removed from the system entirely.
    """
    _ensure_store()
    deleted_count = store.delete_account(account)
    _bump_orders_version()
    with _lock:
        etsy_last_poll.pop(account, None)
        save_etsy_last_poll(etsy_last_poll)
    return deleted_count


def delete_all_etsy_orders(account=None, clear_processed=False):
    """Delete all orders, optionally filtered by account.

    Returns number of orders deleted.
    """
    _ensure_store()
    removed = store.delete_all_orders(account)
    if clear_processed:
        store.clear_processed_ids(account)
    _bump_orders_version()

    if clear_processed:
        for acc, msg_ids in removed.items():
            for mid in msg_ids:
                if mid:
                    _unmark_on_gmail(acc, mid)

    return sum(len(v) for v in removed.values())


def get_etsy_status(config=None):
    """Return Etsy polling status per account."""
    if config is None:
        config = load_config()

    _ensure_store()
    statuses = []
    for acc in config.get("accounts", []):
        email = acc["email"]
        statuses.append({
            "email": email,
            "etsy_polling_enabled": acc.get("etsy_polling_enabled", False),
            "order_count": store.count_orders(email),
            "unparsed": store.list_unparsed(email),
            "sheet_error": sheet_errors.get(email),
            "etsy_query": acc.get("etsy_query", DEFAULT_ETSY_QUERY),
        })
    return statuses
