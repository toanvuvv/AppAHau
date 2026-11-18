"""Flask web dashboard for monitoring Gmail → Sheets Sync."""
import html as html_mod
import json
import logging
import os
import re
import threading
import time as _time
from datetime import datetime

import secrets

import yaml
from functools import wraps
from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from google_auth_oauthlib.flow import Flow

from main import (
    DATA_DIR, CONFIG_FILE, PROCESSED_IDS_FILE, LOG_FILE, CREDENTIALS_FILE,
    ConfigReadError, atomic_text_write, load_config_for_update, load_config_safe, save_token_file,
)


app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]  # Required — fail fast if missing


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# Every read-modify-write of config.yaml runs under this lock so two requests
# never interleave and lose each other's change (RLock: helpers may nest).
_config_lock = threading.RLock()


def config_write(f):
    """Serialise a route that loads, changes and saves config.yaml."""
    @wraps(f)
    def decorated(*args, **kwargs):
        with _config_lock:
            return f(*args, **kwargs)
    return decorated


@app.errorhandler(ConfigReadError)
def handle_config_read_error(e):
    # Refuse to save over a config we could not read (would wipe all accounts)
    logging.critical(f"Tu choi ghi config: {e}")
    return jsonify({"error": f"config.yaml loi, khong luu de tranh mat du lieu: {e}"}), 500


# Store OAuth flows in memory, keyed by state parameter
oauth_flows = {}


def _prune_oauth_flows():
    """Remove OAuth flow entries older than 10 minutes."""
    now = _time.time()
    expired = [k for k, v in oauth_flows.items() if now - v.get("created_at", 0) > 600]
    for k in expired:
        del oauth_flows[k]

START_TIME = datetime.now()


def load_processed_ids_safe():
    """Load processed IDs, return empty dict on error."""
    try:
        with open(PROCESSED_IDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def read_recent_logs(n=20):
    """Read last n lines from log file."""
    if not os.path.exists(LOG_FILE):
        return []
    try:
        from collections import deque
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            last_lines = deque(f, maxlen=n)
        return [line.strip() for line in last_lines]
    except Exception:
        return []


def get_today_count(processed_ids):
    """Count emails processed today (approximation from log)."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    count = 0
    if not os.path.exists(LOG_FILE):
        return 0
    try:
        from collections import deque
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            recent_lines = deque(f, maxlen=500)
        for line in recent_lines:
            if today_str in line and "Da ghi" in line:
                match = re.search(r"Da ghi (\d+) (?:email|dong)", line)
                if match:
                    count += int(match.group(1))
    except Exception:
        pass
    return count


def save_config(config):
    """Save config dict to YAML file."""
    # Serialise first: a yaml error must not leave a half-written file behind
    text = yaml.dump(config, default_flow_style=False, allow_unicode=True)
    atomic_text_write(CONFIG_FILE, text)


def check_token_status(token_file):
    """Check if token file exists and is non-empty."""
    full_path = os.path.join(DATA_DIR, token_file)
    if not os.path.exists(full_path):
        return "missing"
    try:
        with open(full_path, "r") as f:
            data = json.load(f)
        if data.get("refresh_token"):
            return "valid"
        return "needs_refresh"
    except Exception:
        return "error"


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    username = request.form.get("username", "")
    password = request.form.get("password", "")
    admin_user = os.environ.get("ADMIN_USER", "admin")
    admin_pass = os.environ.get("ADMIN_PASS", "")

    if (secrets.compare_digest(username, admin_user)
            and secrets.compare_digest(password, admin_pass)):
        session["logged_in"] = True
        return redirect("/")

    return render_template("login.html", error="Sai username hoặc password"), 200


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/api/status")
@login_required
def api_status():
    config = load_config_safe()
    processed = load_processed_ids_safe()
    logs = read_recent_logs(20)
    today_count = get_today_count(processed)

    total_all_time = sum(len(ids) for ids in processed.values())
    uptime_seconds = int((datetime.now() - START_TIME).total_seconds())

    accounts = []
    for acc in config.get("accounts", []):
        email = acc["email"]
        token_status = check_token_status(acc.get("token_file", ""))
        email_count = len(processed.get(email, []))
        accounts.append({
            "email": email,
            "total_emails": email_count,
            "token_status": token_status,
            "sheet_id": acc.get("sheet_id", ""),
            "polling_enabled": acc.get("polling_enabled", True),
        })

    parsed_logs = []
    for line in logs:
        level = "INFO"
        if "[ERROR]" in line:
            level = "ERROR"
        elif "[WARNING]" in line:
            level = "WARNING"
        elif "Da ghi" in line:
            level = "SUCCESS"
        parsed_logs.append({"text": line, "level": level})

    return jsonify({
        "running": etsy_poller.polling_enabled,
        "accounts": accounts,
        "logs": parsed_logs,
        "today_count": today_count,
        "total_all_time": total_all_time,
        "uptime_seconds": uptime_seconds,
        "polling_interval": config.get("etsy_polling_interval_minutes", 5),
    })


@app.route("/api/credentials/status")
@login_required
def api_credentials_status():
    exists = os.path.exists(CREDENTIALS_FILE)
    valid = False
    if exists:
        try:
            with open(CREDENTIALS_FILE, "r") as f:
                data = json.load(f)
            valid = "installed" in data or "web" in data
        except Exception:
            pass
    return jsonify({"exists": exists, "valid": valid})


@app.route("/api/credentials/upload", methods=["POST"])
@login_required
def api_credentials_upload():
    if "file" not in request.files:
        return jsonify({"error": "Không có file"}), 400
    file = request.files["file"]
    try:
        content = file.read()
        data = json.loads(content)
        if "installed" not in data and "web" not in data:
            return jsonify({"error": "File không hợp lệ: thiếu key 'installed' hoặc 'web'"}), 400
        atomic_text_write(CREDENTIALS_FILE, content.decode("utf-8"))
        return jsonify({"success": True})
    except (json.JSONDecodeError, UnicodeDecodeError):
        return jsonify({"error": "File không phải JSON hợp lệ"}), 400


@app.route("/api/accounts")
@login_required
def api_accounts():
    config = load_config_safe()
    accounts = []
    for acc in config.get("accounts", []):
        token_status = check_token_status(acc.get("token_file", ""))
        accounts.append({
            "email": acc["email"],
            "sheet_id": acc.get("sheet_id", ""),
            "sheet_tab": acc.get("sheet_tab", "Sheet1"),
            "etsy_query": acc.get("etsy_query", ""),
            "token_file": acc.get("token_file", ""),
            "token_status": token_status,
        })
    return jsonify({"accounts": accounts})


@app.route("/api/accounts", methods=["POST"])
@login_required
@config_write
def api_accounts_add():
    data = request.get_json()
    if not data or not data.get("email") or not data.get("sheet_id"):
        return jsonify({"error": "Email và Sheet ID là bắt buộc"}), 400

    email = data["email"]
    # Auto-generate token_file from email
    safe_name = email.split("@")[0].replace(".", "_")
    token_file = f"tokens/token_{safe_name}.json"

    config = load_config_for_update()
    if "accounts" not in config:
        config["accounts"] = []

    # Check duplicate
    for acc in config["accounts"]:
        if acc["email"] == email:
            return jsonify({"error": f"Tài khoản {email} đã tồn tại"}), 400

    config["accounts"].append({
        "email": email,
        "token_file": token_file,
        "sheet_id": data["sheet_id"],
        "sheet_tab": data.get("sheet_tab", "Sheet1"),
        "etsy_query": data.get("etsy_query", ""),
        "etsy_polling_enabled": True,
    })
    save_config(config)
    return jsonify({"success": True})


@app.route("/api/accounts/<path:email>", methods=["PUT"])
@login_required
@config_write
def api_accounts_edit(email):
    data = request.get_json()
    if not data:
        return jsonify({"error": "Không có dữ liệu"}), 400

    config = load_config_for_update()
    found = False
    for acc in config.get("accounts", []):
        if acc["email"] == email:
            if "sheet_id" in data:
                acc["sheet_id"] = data["sheet_id"]
            if "sheet_tab" in data:
                acc["sheet_tab"] = data["sheet_tab"]
            if "etsy_query" in data:
                acc["etsy_query"] = data["etsy_query"]
            found = True
            break

    if not found:
        return jsonify({"error": f"Không tìm thấy tài khoản {email}"}), 404

    save_config(config)
    return jsonify({"success": True})


@app.route("/api/accounts/<path:email>", methods=["DELETE"])
@login_required
@config_write
def api_accounts_delete(email):
    config = load_config_for_update()
    accounts = config.get("accounts", [])
    new_accounts = [a for a in accounts if a["email"] != email]

    if len(new_accounts) == len(accounts):
        return jsonify({"error": f"Không tìm thấy tài khoản {email}"}), 404

    config["accounts"] = new_accounts
    save_config(config)

    # Clean up all Etsy data for this account (orders, processed IDs, last poll)
    deleted_orders = etsy_poller.delete_account_etsy_data(email)

    return jsonify({"success": True, "deleted_orders": deleted_orders})


SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets",
]


def _get_email_from_credentials(creds):
    """Get the Gmail address from OAuth credentials using Gmail API."""
    from googleapiclient.discovery import build
    service = build("gmail", "v1", credentials=creds)
    profile = service.users().getProfile(userId="me").execute()
    return profile["emailAddress"]


def _build_oauth_flow(redirect_uri):
    """Create an OAuth Flow from credentials.json."""
    return Flow.from_client_secrets_file(
        CREDENTIALS_FILE,
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )


def _get_redirect_uri():
    """Build the OAuth redirect URI from PUBLIC_URL."""
    public_url = os.environ.get("PUBLIC_URL", "")
    if not public_url:
        return None
    return f"{public_url.rstrip('/')}/oauth/callback"


def _save_token_for_email(email, creds):
    """Save OAuth token and ensure account exists in config. Returns token_file path."""
    safe_name = email.split("@")[0].replace(".", "_")
    token_file = f"tokens/token_{safe_name}.json"

    with _config_lock:
        token_file = _ensure_account_for_token(email, token_file)

    save_token_file(token_file, creds.to_json())
    return token_file


def _ensure_account_for_token(email, token_file):
    """Add the account to config if missing; return the token_file to use."""
    config = load_config_for_update()
    if "accounts" not in config:
        config["accounts"] = []

    # Check if account already exists
    found = False
    for acc in config["accounts"]:
        if acc["email"] == email:
            token_file = acc.get("token_file", token_file)
            found = True
            break

    if not found:
        config["accounts"].append({
            "email": email,
            "token_file": token_file,
            "sheet_id": "",
            "sheet_tab": "Sheet1",
            "etsy_query": "",
            "etsy_polling_enabled": True,
        })
        save_config(config)
    return token_file


@app.route("/api/auth/new")
@login_required
def api_auth_new():
    """Redirect directly to Google OAuth for a new account (no email needed)."""
    _prune_oauth_flows()

    if not os.path.exists(CREDENTIALS_FILE):
        return "<h2>Lỗi: Chưa có credentials.json. Hãy tải lên trong tab Cấu hình.</h2>", 400

    redirect_uri = _get_redirect_uri()
    if not redirect_uri:
        return "<h2>Lỗi: Chưa cấu hình PUBLIC_URL.</h2>", 400

    try:
        flow = _build_oauth_flow(redirect_uri)
        auth_url, state = flow.authorization_url(
            access_type="offline",
            prompt="consent",
        )
        oauth_flows[state] = {"flow": flow, "email": None, "created_at": _time.time()}
        return redirect(auth_url)
    except Exception as e:
        return f"<h2>Lỗi tạo URL xác thực: {str(e)}</h2>", 500


@app.route("/api/auth/start", methods=["POST"])
@login_required
def api_auth_start():
    """Start OAuth for an existing account (email required)."""
    _prune_oauth_flows()
    data = request.get_json()
    if not data or not data.get("email"):
        return jsonify({"error": "Email là bắt buộc"}), 400

    email = data["email"]

    if not os.path.exists(CREDENTIALS_FILE):
        return jsonify({"error": "Chưa có credentials.json. Hãy tải lên trước."}), 400

    redirect_uri = _get_redirect_uri()
    if not redirect_uri:
        return jsonify({"error": "Chưa cấu hình PUBLIC_URL environment variable"}), 400

    try:
        flow = _build_oauth_flow(redirect_uri)
        auth_url, state = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            login_hint=email,
        )
        oauth_flows[state] = {"flow": flow, "email": email, "created_at": _time.time()}
        return jsonify({"auth_url": auth_url, "state": state})
    except Exception as e:
        return jsonify({"error": f"Lỗi tạo URL xác thực: {str(e)}"}), 500


@app.route("/oauth/callback")
def oauth_callback():
    code = request.args.get("code")
    state = request.args.get("state")

    if not code or not state:
        return "<h2>Lỗi: Thiếu code hoặc state</h2>", 400

    flow_data = oauth_flows.pop(state, None)
    if not flow_data:
        return "<h2>Lỗi: Phiên xác thực không hợp lệ hoặc đã hết hạn</h2>", 400

    flow = flow_data["flow"]
    email = flow_data["email"]

    try:
        flow.fetch_token(code=code)
        creds = flow.credentials

        # If email not known (new account flow), get it from Gmail API
        if not email:
            email = _get_email_from_credentials(creds)

        _save_token_for_email(email, creds)

        return """
        <!DOCTYPE html>
        <html>
        <head><meta charset="UTF-8"><title>Xác thực thành công</title>
        <script>setTimeout(function(){{ window.location.href = '/'; }}, 2000);</script>
        </head>
        <body style="background:#2B2783;color:#fff;font-family:Poppins,sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;">
            <div style="text-align:center;background:#fff;padding:40px;border-radius:12px;box-shadow:0 4px 6px rgba(0,0,0,0.07);color:#1e293b;">
                <h1 style="color:#00B4D8;">Xác thực thành công!</h1>
                <p style="margin-top:8px;">Tài khoản: {email}</p>
                <p style="color:#64748b;">Đang chuyển về Dashboard...</p>
            </div>
        </body>
        </html>
        """.format(email=html_mod.escape(email))
    except Exception as e:
        return f"<h2>Lỗi xác thực: {str(e)}</h2>", 500


@app.route("/api/config/polling", methods=["PUT"])
@login_required
@config_write
def api_config_polling():
    data = request.get_json()
    interval_val = data.get("polling_interval_minutes") or data.get("etsy_polling_interval_minutes") if data else None
    if not interval_val:
        return jsonify({"error": "polling_interval_minutes là bắt buộc"}), 400

    try:
        interval = int(interval_val)
        if interval < 1:
            raise ValueError()
    except (ValueError, TypeError):
        return jsonify({"error": "Giá trị phải là số nguyên dương"}), 400

    config = load_config_for_update()
    config["etsy_polling_interval_minutes"] = interval
    save_config(config)
    return jsonify({"success": True})


@app.route("/api/polling/toggle", methods=["POST"])
@login_required
def api_polling_toggle():
    """Toggle polling on/off."""
    try:
        etsy_poller.polling_enabled = not etsy_poller.polling_enabled
        state = "enabled" if etsy_poller.polling_enabled else "disabled"
        logging.info(f"Polling {state} by user")
        return jsonify({"success": True, "polling_enabled": etsy_poller.polling_enabled})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/polling/toggle/<path:email>", methods=["POST"])
@login_required
@config_write
def api_account_polling_toggle(email):
    """Toggle polling for a specific account."""
    config = load_config_for_update()
    found = False
    new_state = False

    for acc in config.get("accounts", []):
        if acc["email"] == email:
            current = acc.get("polling_enabled", True)
            acc["polling_enabled"] = not current
            acc["etsy_polling_enabled"] = not current
            new_state = not current
            found = True
            break

    if not found:
        return jsonify({"error": f"Khong tim thay tai khoan {email}"}), 404

    save_config(config)
    return jsonify({"success": True, "polling_enabled": new_state})


## --- Etsy Routes ---


@app.route("/etsy")
@login_required
def etsy_page():
    return render_template("etsy_orders.html")


def _order_filters_from_args(args):
    """Shared filter params of the paginated order endpoints."""
    return {
        "account": args.get("account") or None,
        "search": args.get("q", ""),
        "date_from": args.get("from", ""),
        "date_to": args.get("to", ""),
        "buyer_key": args.get("buyer") or None,  # "" = all buyers (as in the UI)
    }


def _int_arg(args, name, default):
    try:
        return int(args.get(name, default))
    except (TypeError, ValueError):
        return default


@app.route("/api/etsy/orders")
@login_required
def api_etsy_orders():
    """Get Etsy orders.

    With ?page= (or ?size=): one filtered page + totals (filters: account, q,
    from, to, buyer). Without: legacy full list, optionally for one account.
    """
    account = request.args.get("account", "")
    version = etsy_poller.get_orders_version()

    if "page" in request.args or "size" in request.args:
        result = etsy_poller.query_etsy_orders(
            **_order_filters_from_args(request.args),
            page=_int_arg(request.args, "page", 1),
            page_size=_int_arg(request.args, "size", 25))
        result["version"] = version
        return jsonify(result)

    if account:
        account_orders = etsy_poller.get_etsy_orders(account)
        return jsonify({"orders": account_orders, "account": account, "version": version})

    # Return all orders grouped by account
    return jsonify({"orders": etsy_poller.get_etsy_orders(), "version": version})


@app.route("/api/etsy/orders/keys")
@login_required
def api_etsy_order_keys():
    """[[account, order_id], ...] of every order matching the filters ("select all")."""
    return jsonify({"keys": etsy_poller.query_etsy_order_keys(**_order_filters_from_args(request.args))})


@app.route("/api/etsy/status")
@login_required
def api_etsy_status():
    """Get Etsy polling status per account."""
    config = load_config_safe()
    statuses = etsy_poller.get_etsy_status(config)
    interval = config.get("etsy_polling_interval_minutes", 5)
    return jsonify({
        "accounts": statuses,
        "polling_interval": interval,
        "orders_version": etsy_poller.get_orders_version(),
        "poll_running": etsy_poller.is_poll_running(),
        "job": etsy_poller.get_job(),
        "last_poll_finished_at": heartbeat["last_poll_finished_at"],
    })


@app.route("/api/etsy/polling/toggle/<path:email>", methods=["POST"])
@login_required
@config_write
def api_etsy_polling_toggle(email):
    """Toggle Etsy polling for a specific account."""
    config = load_config_for_update()
    found = False
    new_state = False

    for acc in config.get("accounts", []):
        if acc["email"] == email:
            current = acc.get("etsy_polling_enabled", False)
            acc["polling_enabled"] = not current
            acc["etsy_polling_enabled"] = not current
            new_state = not current
            found = True
            break

    if not found:
        return jsonify({"error": f"Khong tim thay tai khoan {email}"}), 404

    save_config(config)
    return jsonify({"success": True, "etsy_polling_enabled": new_state})


@app.route("/api/etsy/orders/all", methods=["DELETE"])
@login_required
def api_etsy_delete_all_orders():
    """Delete all Etsy orders, optionally filtered by account."""
    account = request.args.get("account", "")
    clear_processed = request.args.get("clear_processed", "0") == "1"
    count = etsy_poller.delete_all_etsy_orders(account or None, clear_processed)
    return jsonify({"success": True, "deleted": count})


@app.route("/api/etsy/orders/<order_id>", methods=["DELETE"])
@login_required
def api_etsy_delete_order(order_id):
    """Delete a single Etsy order by order_id."""
    account = request.args.get("account", "")
    if not account:
        return jsonify({"error": "account parameter la bat buoc"}), 400

    clear_processed = request.args.get("clear_processed", "0") == "1"
    deleted = etsy_poller.delete_etsy_order(account, order_id, clear_processed)
    if deleted:
        return jsonify({"success": True})
    return jsonify({"error": "Khong tim thay order"}), 404


@app.route("/api/etsy/unparsed/dismiss", methods=["POST"])
@login_required
def api_etsy_dismiss_unparsed():
    """Mark an unreadable order email as 'not an order' so it stops being retried/listed."""
    data = request.get_json(silent=True) or {}
    account, message_id = data.get("account", ""), data.get("message_id", "")
    if not account or not message_id:
        return jsonify({"error": "account va message_id la bat buoc"}), 400
    if etsy_poller.dismiss_unparsed_email(account, message_id):
        return jsonify({"success": True})
    return jsonify({"error": "Khong tim thay email"}), 404


@app.route("/api/etsy/orders/bulk", methods=["DELETE"])
@login_required
def api_etsy_delete_orders_bulk():
    """Delete multiple Etsy orders by list of {account: [order_id, ...]}."""
    data = request.get_json(silent=True) or {}
    orders_by_account = data.get("orders", {})
    clear_processed = data.get("clear_processed", False)

    if not orders_by_account:
        return jsonify({"error": "orders parameter is required"}), 400

    count = etsy_poller.delete_etsy_orders_bulk(orders_by_account, clear_processed)
    return jsonify({"success": True, "deleted": count})


JOB_BUSY_MESSAGE = "Dang co poll/sync khac chay, vui long doi"


def _job_response(started):
    """202 when the background job started, 409 when another poll/sync is running."""
    if started:
        return jsonify({"success": True, "started": True}), 202
    return jsonify({"error": JOB_BUSY_MESSAGE}), 409


def _account_exists(email):
    return any(acc["email"] == email for acc in load_config_safe().get("accounts", []))


@app.route("/api/etsy/sync-sheet", methods=["POST"])
@login_required
def api_etsy_sync_sheet():
    """Start a background sync of stored Etsy orders to Google Sheet (dedupe by Order ID).

    Result is reported via /api/etsy/status -> job.
    """
    account_email = request.args.get("account", "")
    if account_email and not _account_exists(account_email):
        return jsonify({"error": "Khong tim thay tai khoan"}), 404
    started = etsy_poller.start_job("sync_sheet", etsy_poller.sync_orders_to_sheet,
                                    account_email=account_email or None)
    return _job_response(started)


@app.route("/api/etsy/poll-now", methods=["POST"])
@login_required
def api_etsy_poll_now():
    """Start an immediate background poll of all accounts."""
    data = request.get_json(silent=True) or {}
    started = etsy_poller.start_job("poll_all", etsy_poller.poll_all_accounts_job,
                                    after_date=data.get("after_date"), before_date=data.get("before_date"))
    return _job_response(started)


@app.route("/api/etsy/poll-now/<path:email>", methods=["POST"])
@login_required
def api_etsy_poll_now_account(email):
    """Start an immediate background poll of one account."""
    if not _account_exists(email):
        return jsonify({"error": f"Khong tim thay tai khoan {email}"}), 404
    data = request.get_json(silent=True) or {}
    started = etsy_poller.start_job("poll_account", etsy_poller.poll_one_account, email=email,
                                    after_date=data.get("after_date"), before_date=data.get("before_date"))
    return _job_response(started)


@app.route("/api/gmail/messages")
@login_required
def api_gmail_messages():
    """Proxy Gmail messages.list — returns raw Google API response."""
    from reader import build_gmail_service
    from main import load_credentials

    query = request.args.get("q", 'subject:"You made a sale on Etsy"')
    max_results = min(int(request.args.get("maxResults", 5)), 50)
    email_param = request.args.get("email", "")

    config = load_config_safe()
    account = None
    for acc in config.get("accounts", []):
        if email_param and acc["email"] != email_param:
            continue
        account = acc
        break

    if not account:
        return jsonify({"error": "Khong tim thay tai khoan"}), 404

    creds = load_credentials(account.get("token_file", ""))
    if not creds:
        return jsonify({"error": f"Token khong hop le cho {account['email']}"}), 400

    service = build_gmail_service(creds)
    response = service.users().messages().list(
        userId="me", q=query, maxResults=max_results
    ).execute()
    return jsonify(response)


@app.route("/api/gmail/messages/<message_id>")
@login_required
def api_gmail_message_detail(message_id):
    """Proxy Gmail messages.get — returns raw Google API response (format=full)."""
    from reader import build_gmail_service
    from main import load_credentials

    fmt = request.args.get("format", "full")
    email_param = request.args.get("email", "")

    config = load_config_safe()
    account = None
    for acc in config.get("accounts", []):
        if email_param and acc["email"] != email_param:
            continue
        account = acc
        break

    if not account:
        return jsonify({"error": "Khong tim thay tai khoan"}), 404

    creds = load_credentials(account.get("token_file", ""))
    if not creds:
        return jsonify({"error": f"Token khong hop le cho {account['email']}"}), 400

    service = build_gmail_service(creds)
    response = service.users().messages().get(
        userId="me", id=message_id, format=fmt
    ).execute()
    return jsonify(response)


@app.route("/api/etsy/config/polling", methods=["PUT"])
@login_required
@config_write
def api_etsy_config_polling():
    """Update Etsy polling interval."""
    data = request.get_json()
    if not data or "etsy_polling_interval_minutes" not in data:
        return jsonify({"error": "etsy_polling_interval_minutes la bat buoc"}), 400

    try:
        interval = int(data["etsy_polling_interval_minutes"])
        if interval < 1:
            raise ValueError()
    except (ValueError, TypeError):
        return jsonify({"error": "Gia tri phai la so nguyen duong"}), 400

    config = load_config_for_update()
    config["etsy_polling_interval_minutes"] = interval
    save_config(config)
    return jsonify({"success": True})


@app.route("/api/logs/clear", methods=["POST"])
@login_required
def api_logs_clear():
    """Clear the log file."""
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.truncate(0)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


import etsy_poller
from etsy_polling_heartbeat_watchdog import heartbeat, start_watchdog


@app.route("/healthz")
def healthz():
    """Liveness for the Docker healthcheck (no login). Process up -> 200."""
    now = _time.time()
    last_tick = heartbeat["last_tick"]
    return jsonify({
        "ok": True,
        "poll_running": etsy_poller.is_poll_running(),
        "last_tick_age_seconds": int(now - last_tick) if last_tick else None,
    })

_polling_started = False
_polling_lock = threading.Lock()


def start_polling_thread():
    """Start Etsy polling in background thread. Thread-safe, idempotent."""
    global _polling_started
    with _polling_lock:
        if _polling_started:
            return
        _polling_started = True

    def run():
        global _polling_started
        import traceback
        try:
            logging.info("Polling thread starting...")
            etsy_poller.start_etsy_polling()
        except Exception as e:
            logging.error(f"Polling thread CRASHED: {e}\n{traceback.format_exc()}")
            # Reset flag so thread can be restarted
            with _polling_lock:
                _polling_started = False

    thread = threading.Thread(target=run, daemon=True, name="etsy-poller")
    thread.start()
    start_watchdog()
    logging.info("Polling thread launched")


# Start polling when module is imported (Gunicorn imports dashboard)
# Skip in test environment to avoid noisy background errors
if not os.environ.get("TESTING"):
    start_polling_thread()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
