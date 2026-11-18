"""Tests for reader.py email parsing logic."""
import base64
from unittest.mock import MagicMock

from reader import parse_email_headers, decode_body, get_new_emails


class TestParseEmailHeaders:
    def test_extracts_subject_from_and_date(self):
        headers = [
            {"name": "Subject", "value": "Test Subject"},
            {"name": "From", "value": "sender@example.com"},
            {"name": "Date", "value": "Thu, 20 Mar 2025 14:30:00 +0700"},
        ]
        result = parse_email_headers(headers)
        assert result["subject"] == "Test Subject"
        assert result["from"] == "sender@example.com"
        assert "2025" in result["date"]

    def test_missing_headers_return_defaults(self):
        result = parse_email_headers([])
        assert result["subject"] == "(Khong co tieu de)"
        assert result["from"] == "(Khong ro nguoi gui)"
        assert result["date"] == ""

    def test_encoded_utf8_subject(self):
        headers = [
            {"name": "Subject", "value": "=?UTF-8?B?VGnDqm5nIFZp4buHdA==?="},
            {"name": "From", "value": "test@test.com"},
            {"name": "Date", "value": "Thu, 20 Mar 2025 14:30:00 +0700"},
        ]
        result = parse_email_headers(headers)
        assert "Ti" in result["subject"]


class TestDecodeBody:
    def test_decodes_base64url_text(self):
        original = "Hello World"
        encoded = base64.urlsafe_b64encode(original.encode()).decode()
        result = decode_body(encoded)
        assert result == "Hello World"

    def test_truncates_long_body(self):
        original = "A" * 2000
        encoded = base64.urlsafe_b64encode(original.encode()).decode()
        result = decode_body(encoded, max_length=1000)
        assert len(result) == 1000

    def test_empty_body(self):
        result = decode_body("")
        assert result == ""

    def test_strips_html_tags(self):
        html = "<p>Hello <b>World</b></p>"
        encoded = base64.urlsafe_b64encode(html.encode()).decode()
        result = decode_body(encoded, is_html=True)
        assert "<p>" not in result
        assert "Hello" in result
        assert "World" in result


class TestGetNewEmails:
    def test_skips_already_processed_ids(self):
        mock_service = MagicMock()

        mock_list = MagicMock()
        mock_list.execute.return_value = {
            "messages": [{"id": "msg1"}, {"id": "msg2"}]
        }

        mock_msg2_payload = {
            "id": "msg2",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Test"},
                    {"name": "From", "value": "a@b.com"},
                    {"name": "Date", "value": "Thu, 20 Mar 2025 14:30:00 +0700"},
                ],
                "mimeType": "text/plain",
                "body": {
                    "data": base64.urlsafe_b64encode(b"body text").decode()
                },
            },
        }
        mock_get = MagicMock()
        mock_get.execute.return_value = mock_msg2_payload

        mock_messages = MagicMock()
        mock_messages.list.return_value = mock_list
        mock_messages.get.return_value = mock_get
        mock_service.users.return_value.messages.return_value = mock_messages

        processed_ids = {"msg1"}
        result = get_new_emails(mock_service, "is:unread", processed_ids)
        assert len(result) == 1
        assert result[0]["message_id"] == "msg2"

    def test_returns_empty_when_no_messages(self):
        mock_service = MagicMock()
        mock_list = MagicMock()
        mock_list.execute.return_value = {}
        mock_service.users.return_value.messages.return_value.list.return_value = mock_list

        result = get_new_emails(mock_service, "is:unread", set())
        assert result == []
