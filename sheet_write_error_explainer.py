"""Turn Google Sheets write errors into a kind + a plain Vietnamese fix-it message.

writer.py classifies the exception (classify_sheet_error); etsy_poller.py turns
the kind into the text shown on the account card (describe_sheet_error), so the
seller sees WHICH sheet/tab/row is blocked and what to change, not a raw API error.
"""
import gspread

PROTECTED = "protected"          # write hit a protected range / sheet
NO_PERMISSION = "no_permission"  # the Gmail account is not an editor of the sheet
NOT_FOUND = "not_found"          # wrong sheet_id, sheet deleted, or tab renamed
OTHER = "other"

# Google localises the message (VI UI: "...ô hoặc đối tượng được bảo vệ...")
_PROTECTED_MARKERS = ("protected", "được bảo vệ")


def classify_sheet_error(exc):
    """Return one of PROTECTED / NO_PERMISSION / NOT_FOUND / OTHER for an exception."""
    if isinstance(exc, (gspread.exceptions.SpreadsheetNotFound, gspread.exceptions.WorksheetNotFound)):
        return NOT_FOUND
    text = str(exc).lower()
    if any(m in text for m in _PROTECTED_MARKERS):
        return PROTECTED
    code = getattr(exc, "code", None)
    if code == 403 or "permission" in text:
        return NO_PERMISSION
    if code == 404:
        return NOT_FOUND
    return OTHER


def describe_sheet_error(kind, email, sheet_tab, row=None, raw=""):
    """Plain message for the account card: what is wrong + what to do."""
    where = f"tab «{sheet_tab}»" + (f", dòng {row}" if row else "")
    if kind == PROTECTED:
        return (f"Sheet đang bị khóa (vùng được bảo vệ) ở {where}. "
                f"Vào Dữ liệu → Trang tính và dải ô được bảo vệ: gỡ khóa, "
                f"hoặc thêm {email} vào danh sách được sửa.")
    if kind == NO_PERMISSION:
        return (f"{email} không có quyền sửa Sheet này. "
                f"Bấm Chia sẻ trên Sheet, thêm {email} với quyền Người chỉnh sửa.")
    if kind == NOT_FOUND:
        return (f"Không tìm thấy Sheet hoặc tab «{sheet_tab}». "
                f"Kiểm tra lại Sheet ID và tên tab trong Cấu hình.")
    return f"Ghi Sheet lỗi ở {where}: {raw[:200]}"
