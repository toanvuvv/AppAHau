"""Shared helpers — config, credentials, processed IDs, logging."""
import json
import logging
import logging.handlers
import os
import sys
import tempfile
import threading

import yaml
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

# --- Constants ---
DATA_DIR = os.environ.get("DATA_DIR", ".")
CONFIG_FILE = os.path.join(DATA_DIR, "config.yaml")
PROCESSED_IDS_FILE = os.path.join(DATA_DIR, "processed", "processed_ids.json")
LOG_FILE = os.path.join(DATA_DIR, "logs", "app.log")
CREDENTIALS_FILE = os.path.join(DATA_DIR, "credentials.json")
ETSY_ORDERS_FILE = os.path.join(DATA_DIR, "etsy", "etsy_orders.json")
ETSY_PROCESSED_FILE = os.path.join(DATA_DIR, "etsy", "etsy_processed_ids.json")
ETSY_LAST_POLL_FILE = os.path.join(DATA_DIR, "etsy", "etsy_last_poll.json")
ETSY_DB_FILE = os.path.join(DATA_DIR, "etsy", "etsy.db")
MAX_IDS_PER_ACCOUNT = 10000
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 3

# Serialises OAuth token refresh + write so two threads never refresh/write
# the same token file at once (a half-written token kills polling).
_token_lock = threading.Lock()


# --- Helpers ---


def load_config(path=CONFIG_FILE):
    """Load and return config from YAML file."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Khong tim thay file config: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_config_safe(path=CONFIG_FILE):
    """Load config, return empty dict on error."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


class ConfigReadError(Exception):
    """config.yaml exists but cannot be parsed — callers must not overwrite it."""


def load_config_for_update(path=CONFIG_FILE):
    """Load config for a read-modify-write cycle.

    Missing file -> {} (first run). Existing but empty/corrupt/non-dict file
    -> ConfigReadError, so a route never saves {} over the real accounts.
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as e:
        raise ConfigReadError(f"Khong doc duoc config: {e}") from e
    if data is None and os.path.getsize(path) == 0:
        # Zero-byte file (e.g. leftover from an old bind mount) holds nothing to protect
        return {}
    if not isinstance(data, dict):
        raise ConfigReadError("config.yaml khong hop le (khong phai mapping)")
    return data


def load_processed_ids(path=PROCESSED_IDS_FILE):
    """Load processed message IDs from JSON file. Returns empty dict if missing."""
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def atomic_text_write(filepath, text):
    """Write text atomically: temp file in same dir -> fsync -> os.replace.

    Readers never see a half-written file, and the data survives a crash or a
    killed Docker VM right after the write returns.
    """
    dirpath = os.path.dirname(filepath) or "."
    os.makedirs(dirpath, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dirpath, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, filepath)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_json_write(filepath, data):
    """Serialise to JSON first, then write atomically (see atomic_text_write)."""
    atomic_text_write(filepath, json.dumps(data, ensure_ascii=False, indent=2))


def save_processed_ids(path, data):
    """Save processed message IDs to JSON file (atomic write)."""
    atomic_json_write(path, data)


def trim_processed_ids(data, max_per_account=MAX_IDS_PER_ACCOUNT):
    """Trim processed IDs to max_per_account per account (keep newest)."""
    for email_addr in data:
        if len(data[email_addr]) > max_per_account:
            data[email_addr] = data[email_addr][-max_per_account:]
    return data


def load_credentials(token_file):
    """Load OAuth2 credentials from token file, refresh if needed.

    Refresh + write happen under a lock and the file is replaced atomically.
    """
    full_path = os.path.join(DATA_DIR, token_file)
    if not os.path.exists(full_path):
        return None

    with _token_lock:
        creds = Credentials.from_authorized_user_file(full_path)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            atomic_text_write(full_path, creds.to_json())

    return creds


def save_token_file(token_file, token_json):
    """Write a token file atomically, serialised with load_credentials refreshes."""
    with _token_lock:
        atomic_text_write(os.path.join(DATA_DIR, token_file), token_json)


def setup_logging():
    """Setup logging to both file and stdout. Idempotent — safe to call multiple times."""
    os.makedirs(os.path.join(DATA_DIR, "logs"), exist_ok=True)

    root = logging.getLogger()
    if root.handlers:
        return root

    root.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotate so the log (read by /api/status) never grows unbounded
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    return root


