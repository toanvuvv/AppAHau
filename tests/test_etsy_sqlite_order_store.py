"""Tests for the SQLite order store: legacy JSON import, dedupe, sync flag, deletes, snapshot."""
import json
import sqlite3
import threading

import pytest

import etsy_sqlite_order_store as store

A = "seller@example.com"


@pytest.fixture
def db(tmp_path):
    store.open_store(str(tmp_path / "etsy.db"))
    yield tmp_path
    store.close_store()


class TestLegacyImport:
    def test_json_files_imported_once_then_renamed(self, tmp_path):
        orders = tmp_path / "etsy_orders.json"
        processed = tmp_path / "etsy_processed_ids.json"
        orders.write_text(json.dumps({A: [
            {"order_id": "1", "message_id": "m1", "buyer": "Ánh"},          # no flag (legacy)
            {"order_id": "2", "message_id": "m2", "sheet_synced": False},
            {"order_id": "2", "message_id": "dup"},                          # duplicate id
        ]}), encoding="utf-8")
        processed.write_text(json.dumps({A: ["m1", "m2", "m9"]}))

        db_path = str(tmp_path / "etsy.db")
        store.open_store(db_path, str(orders), str(processed))
        try:
            got = store.list_orders(A)
            assert [o["order_id"] for o in got] == ["1", "2"]
            assert got[0]["buyer"] == "Ánh" and "sheet_synced" not in got[0]
            assert [o["order_id"] for o in store.list_unsynced_orders(A)] == ["2"]
            assert store.get_processed_ids(A) == {"m1", "m2", "m9"}
            assert not orders.exists() and (tmp_path / "etsy_orders.json.migrated").exists()
            assert (tmp_path / "etsy_processed_ids.json.migrated").exists()
        finally:
            store.close_store()

        # A JSON file appearing later (e.g. restored by mistake) is not re-imported
        orders.write_text(json.dumps({A: [{"order_id": "99"}]}))
        store.open_store(db_path, str(orders), str(processed))
        try:
            assert store.count_orders(A) == 2
        finally:
            store.close_store()

    def test_fresh_install_without_json(self, db):
        assert store.list_all_orders() == {}


class TestOrders:
    def test_insert_dedupes_by_account_and_order_id(self, db):
        assert store.insert_order(A, {"order_id": "1"}) is True
        assert store.insert_order(A, {"order_id": "1"}) is False
        assert store.insert_order("b@x.com", {"order_id": "1"}) is True
        assert store.count_orders(A) == 1

    def test_mark_synced_updates_flag_in_returned_dict(self, db):
        store.insert_order(A, {"order_id": "1", "sheet_synced": False})
        store.insert_order(A, {"order_id": "2", "sheet_synced": False})
        assert store.mark_orders_synced(A, {"1"}) == 1
        assert store.mark_orders_synced(A, {"1"}) == 0
        assert [o["sheet_synced"] for o in store.list_orders(A)] == [True, False]

    def test_delete_returns_message_ids(self, db):
        store.insert_order(A, {"order_id": "1", "message_id": "m1"})
        store.insert_order(A, {"order_id": "2", "message_id": "m2"})
        assert store.delete_orders(A, ["1", "missing"]) == ["m1"]
        assert store.delete_all_orders() == {A: ["m2"]}
        assert store.list_all_orders() == {}

    def test_delete_account_removes_orders_and_processed_ids(self, db):
        store.insert_order(A, {"order_id": "1"})
        store.add_processed_id(A, "m1")
        store.add_processed_id("b@x.com", "m1")
        assert store.delete_account(A) == 1
        assert store.get_processed_ids(A) == set()
        assert store.get_processed_ids("b@x.com") == {"m1"}

    def test_concurrent_inserts_from_threads(self, db):
        def worker(start):
            for i in range(start, start + 200):
                store.insert_order(A, {"order_id": str(i)})

        threads = [threading.Thread(target=worker, args=(k * 100,)) for k in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert store.count_orders(A) == 500  # ids 0..499, overlaps deduped


class TestSnapshot:
    def test_snapshot_is_consistent_copy(self, db):
        store.insert_order(A, {"order_id": "1"})
        dest = db / "backup" / "copy.db"
        store.snapshot(str(db / "etsy.db"), str(dest))
        conn = sqlite3.connect(dest)
        try:
            assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        finally:
            conn.close()


def test_unparsed_messages_lifecycle(tmp_path):
    store.open_store(str(tmp_path / "u.db"))
    try:
        assert store.record_unparsed("a", "m1", "Subj", "Mon", "why", "2026-01-01") is True
        assert store.record_unparsed("a", "m1", "Subj", "Mon", "why", "2026-01-02") is False
        assert [u["message_id"] for u in store.list_unparsed("a")] == ["m1"]
        store.add_processed_id("a", "m1")
        assert store.list_unparsed("a") == []
        store.record_unparsed("a", "m2", "", "", "", "2026-01-03")
        store.delete_account("a")
        assert store.list_unparsed("a") == []
    finally:
        store.close_store()
