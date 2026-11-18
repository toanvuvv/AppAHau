"""Tests for server-side order listing: derived fields, filters, pagination, API."""
import pytest

import etsy_sqlite_order_store as store
from etsy_order_derived_fields import derive_order_fields
from etsy_sqlite_order_list_query import query_order_keys, query_orders

A, B = "a@x.com", "b@x.com"


def _order(oid, day, buyer="Jane", city="Austin", items=1, synced=True, hour=10):
    return {
        "order_id": oid,
        "email_date": f"Mon, {day:02d} Oct 2026 {hour:02d}:00:00 -0400",
        "buyer_name": buyer,
        "city": city,
        "items": [{"title": f"Mug {i}", "sku": f"SKU-{oid}-{i}"} for i in range(items)],
        "sheet_synced": synced,
    }


@pytest.fixture
def db(tmp_path):
    store.open_store(str(tmp_path / "etsy.db"))
    store.insert_order(A, _order("1", 1, buyer="Jane"))
    store.insert_order(A, _order("2", 3, buyer="Bob", items=3, synced=False))
    store.insert_order(A, _order("3", 2, buyer=" jane ", city="Đà Nẵng"))
    store.insert_order(B, _order("4", 5, buyer="Ann"))
    yield
    store.close_store()


class TestDerivedFields:
    def test_email_date_converted_to_new_york_day(self):
        # 02:00 UTC+7 on Oct 2 is still Oct 1 in New York
        d = derive_order_fields({"order_id": "x", "email_date": "Fri, 02 Oct 2026 02:00:00 +0700"}, A)
        assert d["date_key"] == 20261001

    def test_parser_date_fallback_and_missing_year_ignored(self):
        assert derive_order_fields({"date": "Oct 02, 2026"}, A)["date_key"] == 20261002
        assert derive_order_fields({"date": "Mar 3"}, A)["date_key"] is None
        assert derive_order_fields({"date": "22 thg 3, 2026"}, A)["date_key"] == 20260322

    def test_search_text_and_counts(self):
        d = derive_order_fields(_order("9", 1, buyer="  Mary Ann ", items=0), A)
        assert d["buyer_key"] == "mary ann" and d["buyer_label"] == "Mary Ann"
        assert d["item_count"] == 1
        assert "austin" in d["search"] and A in d["search"]


class TestQueryOrders:
    def test_newest_first_with_totals(self, db):
        r = query_orders(page_size=2)
        assert [o["order_id"] for o in r["orders"]] == ["4", "2"]
        assert r["orders"][0]["_account"] == B
        assert (r["total"], r["total_all"], r["pages"], r["items"], r["unsynced"]) == (4, 4, 2, 6, 1)

    def test_page_is_clamped(self, db):
        r = query_orders(page=99, page_size=3)
        assert r["page"] == 2 and [o["order_id"] for o in r["orders"]] == ["1"]

    def test_account_date_and_search_filters(self, db):
        r = query_orders(account=A, date_from="2026-10-02", date_to="2026-10-03")
        assert [o["order_id"] for o in r["orders"]] == ["2", "3"]
        assert r["total_all"] == 3
        assert [o["order_id"] for o in query_orders(search="ĐÀ nẵng")["orders"]] == ["3"]
        assert [o["order_id"] for o in query_orders(search="sku-2 mug")["orders"]] == ["2"]
        assert query_orders(search="sku-2 austinx")["total"] == 0

    def test_invalid_range_is_ignored(self, db):
        r = query_orders(date_from="2026-10-05", date_to="2026-10-01")
        assert r["invalid_range"] is True and r["total"] == 4

    def test_buyer_filter_groups_case_and_spaces(self, db):
        r = query_orders(account=A, buyer_key="jane")
        assert sorted(o["order_id"] for o in r["orders"]) == ["1", "3"]
        assert {b["key"]: b["count"] for b in r["buyers"]} == {"jane": 2, "bob": 1}

    def test_buyer_dropped_when_not_in_date_range(self, db):
        r = query_orders(account=A, buyer_key="bob", date_from="2026-10-01", date_to="2026-10-02")
        assert r["buyer_key"] is None and r["total"] == 2

    def test_keys_for_select_all(self, db):
        assert query_order_keys(account=A, search="jane") == [[A, "3"], [A, "1"]]


class TestOrdersApi:
    @pytest.fixture
    def client(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TESTING", "1")
        import dashboard
        dashboard.app.config["TESTING"] = True
        c = dashboard.app.test_client()
        with c.session_transaction() as s:
            s["logged_in"] = True
        return c

    def test_paginated_endpoint(self, db, client):
        r = client.get("/api/etsy/orders?page=1&size=2&account=a@x.com&buyer=jane").get_json()
        assert [o["order_id"] for o in r["orders"]] == ["3", "1"]
        assert r["total"] == 2 and "version" in r

    def test_keys_endpoint(self, db, client):
        r = client.get("/api/etsy/orders/keys?q=ann").get_json()
        assert r["keys"] == [[B, "4"]]
