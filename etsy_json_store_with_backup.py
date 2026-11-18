"""Crash-safe JSON stores for Etsy data (orders, processed IDs, last poll).

Guarantees:
- Writes are atomic + fsynced (main.atomic_json_write).
- Before overwriting, the previous good file is kept as <file>.bak.
- A corrupt file is never silently treated as "empty" and then overwritten:
  it is renamed to <file>.corrupt-<ts> and the .bak copy is loaded instead.
"""
import json
import logging
import os
import shutil
import time

from main import atomic_json_write

logger = logging.getLogger(__name__)


def _read_dict(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object, got {type(data).__name__}")
    return data


def load_json_store(path):
    """Load a {key: value} JSON store. Missing file -> {}.

    Corrupt file -> quarantined, then fall back to <path>.bak (or {} if that
    is missing/corrupt too). The quarantined file is kept for manual recovery.
    """
    if not os.path.exists(path):
        return {}
    try:
        return _read_dict(path)
    except Exception as e:
        quarantine = f"{path}.corrupt-{int(time.time())}"
        try:
            os.replace(path, quarantine)
        except OSError:
            quarantine = "(khong doi ten duoc)"
        logger.critical(f"File du lieu hong: {path} ({e}) -- da chuyen sang {quarantine}")

    bak = path + ".bak"
    if os.path.exists(bak):
        try:
            data = _read_dict(bak)
            logger.warning(f"Da khoi phuc {path} tu ban backup {bak}")
            return data
        except Exception as e:
            logger.critical(f"Ban backup cung hong: {bak} ({e})")
    return {}


def save_json_store(path, data, keep_backup=True):
    """Atomically write data; keep the previous version as <path>.bak."""
    if keep_backup and os.path.exists(path):
        try:
            shutil.copy2(path, path + ".bak")
        except OSError as e:
            logger.warning(f"Khong tao duoc backup cho {path}: {e}")
    atomic_json_write(path, data)
