"""Tests for Etsy polling: pagination, sheet retry, overlap window, jobs, storage."""
import json
import threading
import time
from unittest.mock import MagicMock

import pytest

import etsy_poller
import etsy_sqlite_order_store as store
import etsy_polling_heartbeat_watchdog as watchdog
from etsy_json_store_with_backup import load_json_store, save_json_store

ACCOUNT = {
    "email": "seller@example.com",
    "token_file": "tokens/test.json",
    "sheet_id": "sheet-123",
    "sheet_tab": "Orders",
    "etsy_query": "from:transaction@etsy.com",
    "etsy_polling_enabled": True,
}


@pytest.fixture
def poller(monkeypatch, tmp_path):
    """Isolate etsy_poller from disk, Gmail and Sheets."""
    monkeypatch.setattr(etsy_poller, "load_config", lambda: {"accounts": [dict(ACCOUNT)]})
    monkeypatch.setattr(etsy_poller, "load_credentials", lambda _: MagicMock(valid=True, expired=False))
    monkeypatch.setattr(etsy_poller, "parse_email_headers", lambda _: {"subject": "Sale", "date": "2026-03-26"})
    monkeypatch.setattr(etsy_poller, "get_email_full_body", lambda _: "plain body")
    monkeypatch.setattr(etsy_poller, "get_email_html_body", lambda _: "<html></html>")
    monkeypatch.setattr(etsy_poller, "mark_email_processed", lambda *args, **kwargs: None)
    monkeypatch.setattr(etsy_poller, "save_etsy_last_poll", lambda data: None)

    counter = {"value": 0}

    def fake_parse(plain_text, html_body):
        counter["value"] += 1
        return {"order_id": f"order-{counter['value']}", "address_1": "1 Main St", "items": []}

    monkeypatch.setattr(etsy_poller, "parse_etsy_sale_email", fake_parse)
    store.open_store(str(tmp_path / "etsy.db"))
    etsy_poller.etsy_last_poll = {}
    yield etsy_poller
    store.close_store()


def _gmail(monkeypatch, pages):
    """Mock Gmail service returning the given list pages; records list kwargs."""
    calls = []

    def list_side_effect(**kwargs):
        calls.append(kwargs)
        request = MagicMock()
        request.execute.return_value = pages[len(calls) - 1]
        return request

    def get_side_effect(**kwargs):
        request = MagicMock()
        request.execute.return_value = {"id": kwargs["id"], "payload": {"headers": []}}
        return request

    messages = MagicMock()
    messages.list.side_effect = list_side_effect
    messages.get.side_effect = get_side_effect
    service = MagicMock()
    service.users.return_value.messages.return_value = messages
    monkeypatch.setattr(etsy_poller, "build_gmail_service", lambda creds: service)
    return calls


class TestPollEtsyPagination:
    def test_poll_fetches_all_gmail_pages_until_exhausted(self, poller, monkeypatch):
        page_calls = _gmail(monkeypatch, [
            {"messages": [{"id": "msg-1"}, {"id": "msg-2"}], "nextPageToken": "page-2"},
            {"messages": [{"id": "msg-3"}]},
        ])
        written = []
        monkeypatch.setattr(poller, "sync_etsy_orders_to_sheet",
                            lambda creds, sid, tab, orders: written.extend(orders) or {"written": len(orders), "skipped": 0})

        poller.poll_etsy(force=True)

        assert len(page_calls) == 2
        assert page_calls[0] == {"userId": "me", "q": "from:transaction@etsy.com", "maxResults": 20}
        assert page_calls[1]["pageToken"] == "page-2"
        assert sorted(o["message_id"] for o in written) == ["msg-1", "msg-2", "msg-3"]
        assert store.get_processed_ids("seller@example.com") == {"msg-1", "msg-2", "msg-3"}
        assert all(o["sheet_synced"] is True for o in poller.get_etsy_orders("seller@example.com"))


class TestSheetRetry:
    def test_failed_sheet_write_is_retried_next_cycle(self, poller, monkeypatch):
        _gmail(monkeypatch, [{"messages": [{"id": "msg-1"}]}, {"messages": []}])
        results = [{"written": 0, "skipped": 0, "error": "quota"}, {"written": 1, "skipped": 0}]
        sent = []

        def fake_sync(creds, sid, tab, orders):
            sent.append([o["order_id"] for o in orders])
            return results[len(sent) - 1]

        monkeypatch.setattr(poller, "sync_etsy_orders_to_sheet", fake_sync)

        poller.poll_etsy(force=True)
        assert poller.get_etsy_orders("seller@example.com")[0]["sheet_synced"] is False

        poller.poll_etsy(force=True)  # no new mail, but the pending order is retried
        assert sent == [["order-1"], ["order-1"]]
        assert poller.get_etsy_orders("seller@example.com")[0]["sheet_synced"] is True

    def test_legacy_orders_without_flag_are_not_resent(self, poller, monkeypatch):
        _gmail(monkeypatch, [{"messages": []}])
        store.insert_order("seller@example.com", {"order_id": "old"})
        sync = MagicMock()
        monkeypatch.setattr(poller, "sync_etsy_orders_to_sheet", sync)
        poller.poll_etsy(force=True)
        sync.assert_not_called()


class TestAutoPollOverlap:
    def test_auto_poll_rescans_one_day_before_last_poll(self, poller, monkeypatch):
        calls = _gmail(monkeypatch, [{"messages": []}])
        poller.etsy_last_poll = {"seller@example.com": 1_000_000}
        poller.poll_etsy()
        assert calls[0]["q"].endswith(f"after:{1_000_000 - 86400}")

    def test_last_poll_records_poll_start_time(self, poller, monkeypatch):
        _gmail(monkeypatch, [{"messages": []}])
        before = int(time.time())
        poller.poll_etsy()
        assert poller.etsy_last_poll["seller@example.com"] >= before


class TestUnparsedEmails:
    """An order email the parser cannot read must never be silently dropped."""

    def test_unparsed_email_is_listed_and_retried_until_it_parses(self, poller, monkeypatch):
        _gmail(monkeypatch, [{"messages": [{"id": "msg-1"}]}, {"messages": [{"id": "msg-1"}]}])
        monkeypatch.setattr(poller, "sync_etsy_orders_to_sheet",
                            lambda *a: {"written": 1, "skipped": 0})
        labelled = []
        monkeypatch.setattr(poller, "mark_email_processed", lambda svc, mid, label: labelled.append(mid))
        parsed = [None, {"order_id": "o-1", "address_1": "1 Main St", "items": []}]
        monkeypatch.setattr(poller, "parse_etsy_sale_email", lambda *a: parsed.pop(0))

        poller.poll_etsy(force=True)
        assert store.get_processed_ids("seller@example.com") == set()
        assert labelled == []
        assert [u["message_id"] for u in store.list_unparsed("seller@example.com")] == ["msg-1"]
        assert poller.get_etsy_status()[0]["unparsed"][0]["subject"] == "Sale"

        poller.poll_etsy(force=True)  # parser fixed / email readable now
        assert store.list_unparsed("seller@example.com") == []
        assert store.get_processed_ids("seller@example.com") == {"msg-1"}
        assert [o["order_id"] for o in poller.get_etsy_orders("seller@example.com")] == ["o-1"]

    def test_dismiss_marks_email_processed(self, poller, monkeypatch):
        _gmail(monkeypatch, [{"messages": [{"id": "msg-1"}]}, {"messages": [{"id": "msg-1"}]}])
        monkeypatch.setattr(poller, "parse_etsy_sale_email", lambda *a: None)
        poller.poll_etsy(force=True)

        assert poller.dismiss_unparsed_email("seller@example.com", "msg-1") is True
        assert store.list_unparsed("seller@example.com") == []
        assert store.get_processed_ids("seller@example.com") == {"msg-1"}
        assert poller.dismiss_unparsed_email("seller@example.com", "msg-1") is False

        poller.poll_etsy(force=True)  # dismissed email is skipped, not re-listed
        assert store.list_unparsed("seller@example.com") == []


class TestFetchFailureKeepsPollMark:
    def test_failed_download_does_not_advance_last_poll(self, poller, monkeypatch):
        _gmail(monkeypatch, [{"messages": [{"id": "msg-1"}]}])
        service = poller.build_gmail_service(None)
        service.users.return_value.messages.return_value.get.side_effect = RuntimeError("503")
        poller.etsy_last_poll = {"seller@example.com": 1_000_000}

        poller.poll_etsy()

        assert poller.etsy_last_poll["seller@example.com"] == 1_000_000
        assert store.get_processed_ids("seller@example.com") == set()


class TestJobs:
    def test_second_job_rejected_while_first_runs(self, poller):
        release = threading.Event()
        assert poller.start_job("poll_all", lambda: release.wait(2)) is True
        try:
            assert poller.start_job("sync_sheet", lambda: None) is False
            assert poller.poll_etsy(force=True) is None  # auto poll skips too
        finally:
            release.set()
        for _ in range(50):
            if poller.get_job()["state"] != "running":
                break
            time.sleep(0.02)
        assert poller.get_job()["state"] == "done"
        assert not poller.is_poll_running()

    def test_job_error_is_reported_and_mutex_released(self, poller):
        def boom():
            raise RuntimeError("bad")

        assert poller.start_job("poll_all", boom)
        for _ in range(50):
            if poller.get_job()["state"] != "running":
                break
            time.sleep(0.02)
        job = poller.get_job()
        assert job["state"] == "error" and "bad" in job["error"]
        assert not poller.is_poll_running()


class TestOrdersVersion:
    def test_delete_bumps_version_and_copies_are_isolated(self, poller):
        store.insert_order("a@x.com", {"order_id": "1"})
        store.insert_order("a@x.com", {"order_id": "2"})
        snapshot = poller.get_etsy_orders()
        v0 = poller.get_orders_version()
        assert poller.delete_etsy_order("a@x.com", "1")
        assert poller.get_orders_version() > v0
        assert len(snapshot["a@x.com"]) == 2  # earlier API copy unaffected
        assert poller.get_etsy_orders("a@x.com") == [{"order_id": "2"}]


class TestJsonStore:
    def test_corrupt_file_is_quarantined_and_backup_used(self, tmp_path):
        path = str(tmp_path / "orders.json")
        save_json_store(path, {"a": [1]})
        save_json_store(path, {"a": [1, 2]})  # .bak now holds {"a": [1]}
        with open(path, "w") as f:
            f.write("{broken")
        assert load_json_store(path) == {"a": [1]}
        assert any(p.name.startswith("orders.json.corrupt-") for p in tmp_path.iterdir())

    def test_corrupt_without_backup_returns_empty_but_keeps_file(self, tmp_path):
        path = tmp_path / "orders.json"
        path.write_text("not json")
        assert load_json_store(str(path)) == {}
        assert not path.exists()
        assert any(p.name.startswith("orders.json.corrupt-") for p in tmp_path.iterdir())


class TestWatchdog:
    def test_detects_hung_poll(self, monkeypatch):
        monkeypatch.setitem(watchdog.heartbeat, "poll_started_at", 1000)
        monkeypatch.setitem(watchdog.heartbeat, "last_tick", 1000)
        assert watchdog.find_stuck_reason(1000 + watchdog.STUCK_POLL_SECONDS + 1)
        assert watchdog.find_stuck_reason(1000 + 60) is None

    def test_detects_dead_loop_only_when_idle(self, monkeypatch):
        monkeypatch.setitem(watchdog.heartbeat, "poll_started_at", None)
        monkeypatch.setitem(watchdog.heartbeat, "last_tick", 1000)
        assert watchdog.find_stuck_reason(1000 + watchdog.STALE_TICK_SECONDS + 1)

    def test_suspend_shifts_heartbeat(self, monkeypatch):
        monkeypatch.setitem(watchdog.heartbeat, "poll_started_at", None)
        monkeypatch.setitem(watchdog.heartbeat, "last_tick", 1000)
        watchdog._shift_for_suspend(3600)
        assert watchdog.heartbeat["last_tick"] == 4600
