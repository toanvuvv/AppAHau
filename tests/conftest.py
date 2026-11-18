"""Shared fixtures: every test gets its own SQLite order store in tmp_path.

Without this, any test that reaches etsy_poller._ensure_store() would open
./etsy/etsy.db in the repo and import the developer's local JSON files.
"""
import pytest

import etsy_poller
import etsy_sqlite_order_store as store


@pytest.fixture(autouse=True)
def isolated_order_store(monkeypatch, tmp_path):
    store.close_store()
    monkeypatch.setattr(etsy_poller, "ETSY_DB_FILE", str(tmp_path / "etsy-test.db"))
    monkeypatch.setattr(etsy_poller, "ETSY_ORDERS_FILE", str(tmp_path / "no-legacy-orders.json"))
    monkeypatch.setattr(etsy_poller, "ETSY_PROCESSED_FILE", str(tmp_path / "no-legacy-processed.json"))
    yield
    store.close_store()
