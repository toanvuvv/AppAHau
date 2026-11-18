"""Read new emails from Gmail API for one account."""
import base64
import logging
import re
from email.header import decode_header as email_decode_header

from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

# Cache for Gmail label IDs to avoid repeated API calls
_label_id_cache = {}


def parse_email_headers(headers):
    """Extract subject, from, date from Gmail message headers."""
    result = {
        "subject": "(Khong co tieu de)",
        "from": "(Khong ro nguoi gui)",
        "date": "",
    }
    for h in headers:
        name = h.get("name", "").lower()
        value = h.get("value", "")
        if name == "subject":
            result["subject"] = _decode_header_value(value)
        elif name == "from":
            result["from"] = _decode_header_value(value)
        elif name == "date":
            result["date"] = value
    return result


def _decode_header_value(value):
    """Decode RFC 2047 encoded header values like =?UTF-8?B?...?=."""
    if "=?" not in value:
        return value
    parts = email_decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded)


def _strip_html(html):
    """Convert HTML to readable plain text."""
    # Remove <style> and <script> blocks entirely
    text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Replace <br> and block tags with newlines
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|tr|li|h[1-6])>", "\n", text, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode common HTML entities
    import html as html_mod
    text = html_mod.unescape(text)
    # Collapse whitespace per line, then collapse blank lines
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    return text


def decode_body(data, max_length=1000, is_html=False):
    """Decode base64url-encoded email body. Strip HTML if needed. Truncate."""
    if not data:
        return ""
    body_bytes = base64.urlsafe_b64decode(data)
    text = body_bytes.decode("utf-8", errors="replace")

    if is_html:
        text = _strip_html(text)

    if len(text) > max_length:
        text = text[:max_length]
    return text


def _extract_body(payload):
    """Extract body text from Gmail message payload (handles multipart)."""
    # Try plain text first (cleanest), then HTML as fallback
    plain_data = _find_part_data(payload, "text/plain")
    if plain_data:
        return decode_body(plain_data)

    html_data = _find_part_data(payload, "text/html")
    if html_data:
        return decode_body(html_data, is_html=True)

    # Single non-multipart part — decode whatever is there
    body_data = payload.get("body", {}).get("data", "")
    if body_data:
        is_html = "html" in payload.get("mimeType", "")
        return decode_body(body_data, is_html=is_html)

    return ""


def get_new_emails(service, gmail_filter, processed_ids):
    """
    Fetch new emails from Gmail API.

    Args:
        service: Gmail API service object (already built)
        gmail_filter: Gmail search query string
        processed_ids: set of message IDs already processed

    Returns:
        list of dicts with keys: message_id, date, from, subject, body
    """
    logger.info(f"Gmail API messages.list — filter: '{gmail_filter}'")
    messages = []
    next_page_token = None
    try:
        while True:
            list_kwargs = {"userId": "me", "q": gmail_filter, "maxResults": 100}
            if next_page_token:
                list_kwargs["pageToken"] = next_page_token
            response = service.users().messages().list(**list_kwargs).execute()
            messages.extend(response.get("messages", []))
            next_page_token = response.get("nextPageToken")
            if not next_page_token:
                break
    except Exception as e:
        logger.error(f"Gmail API messages.list FAILED: {e}")
        raise RuntimeError(f"Loi khi goi Gmail API messages.list: {e}") from e

    total_returned = len(messages)
    logger.info(f"Gmail API tra ve {total_returned} messages, processed_ids hien co {len(processed_ids)} IDs")

    if not messages:
        return []

    results = []
    skipped = 0
    for msg_info in messages:
        msg_id = msg_info["id"]
        if msg_id in processed_ids:
            skipped += 1
            continue

        try:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=msg_id, format="full")
                .execute()
            )
        except Exception as e:
            logger.warning(f"Gmail API messages.get FAILED cho msg_id={msg_id}: {e}")
            continue

        payload = msg.get("payload", {})
        headers = payload.get("headers", [])
        parsed = parse_email_headers(headers)
        body = _extract_body(payload)

        logger.info(f"  Email moi: id={msg_id}, from={parsed['from']}, subject={parsed['subject'][:60]}")

        results.append(
            {
                "message_id": msg_id,
                "date": parsed["date"],
                "from": parsed["from"],
                "subject": parsed["subject"],
                "body": body,
            }
        )

    logger.info(f"Ket qua: {len(results)} email moi, {skipped} da xu ly truoc do (skip)")

    return results


def build_gmail_service(creds):
    """Build Gmail API service from credentials."""
    return build("gmail", "v1", credentials=creds)


# --- Etsy support functions ---


def _decode_raw(data):
    """Decode base64url data to UTF-8 string without truncation."""
    if not data:
        return ""
    body_bytes = base64.urlsafe_b64decode(data)
    return body_bytes.decode("utf-8", errors="replace")


def _find_part_data(payload, target_mime):
    """Recursively find the first part matching target_mime and return raw data."""
    mime_type = payload.get("mimeType", "")

    if not mime_type.startswith("multipart/"):
        if mime_type == target_mime:
            return payload.get("body", {}).get("data", "")
        return ""

    for part in payload.get("parts", []):
        result = _find_part_data(part, target_mime)
        if result:
            return result
    return ""


def get_email_full_body(payload):
    """Get full plain text body without truncation (for Etsy parsing)."""
    data = _find_part_data(payload, "text/plain")
    return _decode_raw(data)


def get_email_html_body(payload):
    """Get raw HTML body without stripping tags (for thumbnail extraction)."""
    data = _find_part_data(payload, "text/html")
    return _decode_raw(data)


def get_or_create_label(service, label_name):
    """Find a Gmail label by name, or create it if it doesn't exist.

    Returns the label ID. Caches results to avoid repeated API calls.
    """
    cache_key = label_name.lower()
    if cache_key in _label_id_cache:
        return _label_id_cache[cache_key]

    results = service.users().labels().list(userId="me").execute()
    for label in results.get("labels", []):
        if label["name"].lower() == cache_key:
            _label_id_cache[cache_key] = label["id"]
            return label["id"]

    # Create the label
    body = {
        "name": label_name,
        "labelListVisibility": "labelShow",
        "messageListVisibility": "show",
    }
    created = service.users().labels().create(userId="me", body=body).execute()
    _label_id_cache[cache_key] = created["id"]
    return created["id"]


def mark_email_processed(service, message_id, label_name="processed"):
    """Add a label to an email message to mark it as processed.

    Uses Gmail API to add the label (like GmailApp.addLabel in Apps Script).
    """
    label_id = get_or_create_label(service, label_name)
    service.users().messages().modify(
        userId="me",
        id=message_id,
        body={"addLabelIds": [label_id]},
    ).execute()


def unmark_email_processed(service, message_id, label_name="processed"):
    """Remove the processed label from an email so it can be re-polled."""
    try:
        label_id = get_or_create_label(service, label_name)
        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"removeLabelIds": [label_id]},
        ).execute()
    except Exception:
        pass
