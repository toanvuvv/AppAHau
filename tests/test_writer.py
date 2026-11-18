"""Tests for writer.py Etsy sheet writing: row 1 untouched, data only in A..S."""
from unittest.mock import MagicMock, patch

import gspread
import pytest

from sheet_write_error_explainer import classify_sheet_error, describe_sheet_error
from writer import (
    ETSY_HEADER_ROW,
    _ensure_header_row,
    _flatten_order_to_rows,
    sync_etsy_orders_to_sheet,
    write_etsy_orders_to_sheet,
)

# Seller's customised header: renamed/extra columns from T onward (42 cols total)
SELLER_HEADER = ETSY_HEADER_ROW + ["Fulfilment", "Giá thành", "Ngày"] + [f"Col{i}" for i in range(20)]


def _order(order_id="111", items=2):
    return {
        "order_id": order_id,
        "email_date": "Mon, 26 Oct 2026 10:00:00 -0400",
        "buyer_name": "Jane",
        "address_1": "1 Main St",
        "phone": "555",
        "items": [{"title": f"Item {i}", "shop": "S", "quantity": 1} for i in range(items)],
    }


def _fake_batch_get(all_values):
    """Mimic Worksheet.batch_get(["D2:D", ...]): one column per range, rows from 2,
    empty cells as [] and trailing empty rows trimmed (like the Sheets API)."""
    def batch_get(ranges):
        out = []
        for rng in ranges:
            idx = ord(rng[0]) - ord("A")
            col = [[row[idx]] if idx < len(row) and row[idx] != "" else [] for row in all_values[1:]]
            while col and not col[-1]:
                col.pop()
            out.append(col)
        return out
    return batch_get


def _mock_worksheet(row1, all_values=None):
    ws = MagicMock()
    ws.row_values.return_value = row1
    ws.col_values.return_value = [row1[0] if row1 else ""]
    values = all_values if all_values is not None else [row1]
    ws.batch_get.side_effect = _fake_batch_get(values)
    ws.get_all_values.side_effect = AssertionError("must not read the whole sheet")
    ws.id = 0
    return ws


def _patch_client(ws):
    client = MagicMock()
    client.open_by_key.return_value.worksheet.return_value = ws
    return patch("writer.gspread.authorize", return_value=client)


class TestHeaderRow:
    def test_header_only_covers_a_to_s(self):
        assert len(ETSY_HEADER_ROW) == 19
        assert ETSY_HEADER_ROW[0] == "Date"
        assert ETSY_HEADER_ROW[-1] == "Phone Number"

    def test_does_not_touch_seller_header(self):
        ws = _mock_worksheet(SELLER_HEADER)
        _ensure_header_row(ws)
        ws.update.assert_not_called()

    def test_does_not_touch_partially_filled_header(self):
        ws = _mock_worksheet(["", "", "Custom"])
        _ensure_header_row(ws)
        ws.update.assert_not_called()

    def test_writes_a_to_s_header_only_on_empty_row1(self):
        ws = _mock_worksheet(["", "  "])
        _ensure_header_row(ws)
        ws.update.assert_called_once_with([ETSY_HEADER_ROW], "A1")

    def test_read_error_does_not_write_header(self):
        ws = MagicMock()
        ws.row_values.side_effect = Exception("API down")
        with pytest.raises(Exception):
            _ensure_header_row(ws)
        ws.update.assert_not_called()


class TestFlattenRows:
    def test_rows_have_exactly_19_cells(self):
        rows = _flatten_order_to_rows(_order(items=3))
        assert len(rows) == 3
        assert all(len(r) == len(ETSY_HEADER_ROW) for r in rows)
        assert rows[0][3] == "111"
        assert rows[0][18] == "555"


class TestWriteOrders:
    def _assert_inserted_at_row2_within_a_to_s(self, ws):
        ws.update.assert_not_called()
        ws.insert_rows.assert_called_once()
        rows = ws.insert_rows.call_args[0][0]
        assert ws.insert_rows.call_args[1]["row"] == 2
        assert all(len(r) == 19 for r in rows)

    def test_write_keeps_header_and_columns_after_s(self):
        ws = _mock_worksheet(SELLER_HEADER)
        with _patch_client(ws):
            count = write_etsy_orders_to_sheet(MagicMock(), "sid", "Order", [_order()])
        assert count == 2
        self._assert_inserted_at_row2_within_a_to_s(ws)

    def test_sync_keeps_header_and_columns_after_s(self):
        ws = _mock_worksheet(SELLER_HEADER)
        with _patch_client(ws):
            result = sync_etsy_orders_to_sheet(MagicMock(), "sid", "Order", [_order()])
        assert result["written"] == 2
        self._assert_inserted_at_row2_within_a_to_s(ws)

    def test_sync_skips_existing_order_id(self):
        existing = ["10/25/2026 10:00", "S", "", "111"] + [""] * 15
        ws = _mock_worksheet(SELLER_HEADER, all_values=[SELLER_HEADER, existing])
        with _patch_client(ws):
            result = sync_etsy_orders_to_sheet(MagicMock(), "sid", "Order", [_order("111")])
        assert result == {"written": 0, "skipped": 1}
        ws.insert_rows.assert_not_called()

    def test_highlight_limited_to_a_to_s(self):
        # Sheet after insert: the just-written order 111 shares an address with an old row
        new_row = ["d", "S", "", "111"] + [""] * 7 + ["Jane", "1 Main", ""] + [""] * 5
        old_row = ["d", "S", "", "1"] + [""] * 7 + ["Jane", "1 Main", ""] + [""] * 5
        ws = _mock_worksheet(SELLER_HEADER, all_values=[SELLER_HEADER, new_row, old_row])
        with _patch_client(ws):
            write_etsy_orders_to_sheet(MagicMock(), "sid", "Order", [_order()])
        requests = ws.spreadsheet.batch_update.call_args[0][0]["requests"]
        for req in requests:
            rng = req["repeatCell"]["range"]
            assert rng["endColumnIndex"] == 19
            assert rng["startRowIndex"] >= 1  # never row 1

    def test_returns_zero_on_empty_orders(self):
        assert write_etsy_orders_to_sheet(MagicMock(), "sid", "Order", []) == 0

    def test_handles_sheet_not_found_gracefully(self):
        client = MagicMock()
        client.open_by_key.side_effect = Exception("Sheet not found")
        with patch("writer.gspread.authorize", return_value=client):
            assert write_etsy_orders_to_sheet(MagicMock(), "bad", "Order", [_order()]) == 0

    def test_client_gets_request_timeout(self):
        ws = _mock_worksheet(SELLER_HEADER)
        with _patch_client(ws) as authorize:
            write_etsy_orders_to_sheet(MagicMock(), "sid", "Order", [_order()])
        authorize.return_value.set_timeout.assert_called_once()

    def test_sync_reports_error_when_insert_fails(self):
        ws = _mock_worksheet(SELLER_HEADER)
        ws.insert_rows.side_effect = Exception("quota")
        with _patch_client(ws):
            result = sync_etsy_orders_to_sheet(MagicMock(), "sid", "Order", [_order()])
        assert result["written"] == 0
        assert "error" in result

    def test_insert_error_log_names_sheet_tab_and_row(self, caplog):
        ws = _mock_worksheet(SELLER_HEADER)
        ws.insert_rows.side_effect = Exception("protected cell")
        with _patch_client(ws), caplog.at_level("ERROR", logger="writer"):
            sync_etsy_orders_to_sheet(MagicMock(), "sid-123", "Order", [_order()])
        msg = caplog.records[-1].getMessage()
        assert "sid-123" in msg and "'Order'" in msg and "dong 2" in msg and "protected cell" in msg

    def test_insert_error_result_has_kind_and_row(self):
        ws = _mock_worksheet(SELLER_HEADER)
        ws.insert_rows.side_effect = _api_error(400, "Bạn đang cố gắng chỉnh sửa ô hoặc đối tượng được bảo vệ.")
        with _patch_client(ws):
            result = sync_etsy_orders_to_sheet(MagicMock(), "sid", "Order", [_order()])
        assert result["error_kind"] == "protected"
        assert result["error_row"] == 2


def _api_error(code, message):
    response = MagicMock()
    response.json.return_value = {"error": {"code": code, "message": message}}
    return gspread.exceptions.APIError(response)


class TestSheetErrorExplainer:
    @pytest.mark.parametrize("exc, kind", [
        (_api_error(400, "Invalid requests[0].insertDimension: You are trying to edit a protected cell"), "protected"),
        (_api_error(400, "Bạn đang cố gắng chỉnh sửa ô hoặc đối tượng được bảo vệ."), "protected"),
        (_api_error(403, "The caller does not have permission"), "no_permission"),
        (gspread.exceptions.WorksheetNotFound("Order"), "not_found"),
        (Exception("quota"), "other"),
    ])
    def test_classify(self, exc, kind):
        assert classify_sheet_error(exc) == kind

    def test_protected_message_names_tab_row_and_email(self):
        msg = describe_sheet_error("protected", "a@gmail.com", "Order", 1234)
        assert "«Order»" in msg and "dòng 1234" in msg and "a@gmail.com" in msg


class TestColumnReads:
    def test_sync_reads_only_date_and_order_id_columns(self):
        existing = ["10/25/2026 10:00", "S", "", "111"] + [""] * 15
        ws = _mock_worksheet(SELLER_HEADER, all_values=[SELLER_HEADER, existing])
        with _patch_client(ws):
            sync_etsy_orders_to_sheet(MagicMock(), "sid", "Order", [_order("222")])
        assert ws.batch_get.call_args_list[0][0][0] == ["A2:A", "D2:D"]
        assert ws.batch_get.call_args_list[1][0][0] == ["D2:D", "L2:L", "M2:M", "N2:N"]

    def test_newer_order_inserted_above_older_rows(self):
        old = ["10/25/2026 10:00", "S", "", "111"] + [""] * 15
        ws = _mock_worksheet(SELLER_HEADER, all_values=[SELLER_HEADER, old])
        with _patch_client(ws):
            sync_etsy_orders_to_sheet(MagicMock(), "sid", "Order", [_order("222")])  # Oct 26
        assert ws.insert_rows.call_args[1]["row"] == 2

    def test_row_count_follows_longest_column(self):
        # Column D is longer than column A (last row has a blank date): that row
        # still counts (blank date = oldest, same as the old get_all_values path)
        rows = [["10/27/2026 10:00", "", "", "1"] + [""] * 15, ["", "", "", "2"] + [""] * 15]
        ws = _mock_worksheet(SELLER_HEADER, all_values=[SELLER_HEADER] + rows)
        order = dict(_order("333"), email_date="Mon, 01 Jan 2024 10:00:00 -0500")
        with _patch_client(ws):
            result = sync_etsy_orders_to_sheet(MagicMock(), "sid", "Order", [order])
        assert ws.insert_rows.call_args[1]["row"] == 3
        assert result["written"] == 2


class TestIncrementalHighlight:
    def _row(self, order_id, name, addr):
        return ["d", "S", "", order_id] + [""] * 7 + [name, addr, ""] + [""] * 5

    def test_only_groups_with_new_orders_are_recoloured(self):
        from writer import highlight_duplicate_addresses
        rows = [
            self._row("new", "Jane", "1 Main"),
            self._row("old1", "Jane", "1 Main"),
            self._row("old2", "Bob", "2 Side"),
            self._row("old3", "Bob", "2 Side"),
        ]
        ws = _mock_worksheet(SELLER_HEADER, all_values=[SELLER_HEADER] + rows)
        highlight_duplicate_addresses(ws, {"new"})
        reqs = ws.spreadsheet.batch_update.call_args[0][0]["requests"]
        touched = sorted(r["repeatCell"]["range"]["startRowIndex"] for r in reqs)
        assert touched == [1, 2]  # Bob's group untouched

    def test_new_single_row_reset_to_white(self):
        from writer import WHITE, highlight_duplicate_addresses
        ws = _mock_worksheet(SELLER_HEADER, all_values=[SELLER_HEADER, self._row("new", "Ann", "9 Road")])
        highlight_duplicate_addresses(ws, {"new"})
        req = ws.spreadsheet.batch_update.call_args[0][0]["requests"][0]
        assert req["repeatCell"]["cell"]["userEnteredFormat"]["backgroundColor"] == WHITE
