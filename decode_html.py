"""Decode Gmail API JSON response to readable HTML.

Usage:
    python decode_html.py toan.txt -o output.html    # Gmail API JSON -> HTML file
    python decode_html.py toan.txt                   # print to stdout
    python decode_html.py toan.txt --plain            # get plain text instead of HTML
    python decode_html.py -                          # read JSON from stdin

Input: Gmail API message JSON (format=full) or raw base64url string.
Output: Decoded HTML (or plain text with --plain).
"""

import argparse
import base64
import json
import sys


def decode_base64url(data: str) -> str:
    """Decode base64url-encoded string (Gmail API body format)."""
    if not data:
        return ""
    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")


def find_part_data(payload, target_mime):
    """Recursively find body data for a given MIME type in Gmail payload."""
    mime_type = payload.get("mimeType", "")

    if not mime_type.startswith("multipart/"):
        if mime_type == target_mime:
            return payload.get("body", {}).get("data", "")
        return ""

    for part in payload.get("parts", []):
        result = find_part_data(part, target_mime)
        if result:
            return result
    return ""


def extract_from_gmail_json(data: dict, prefer_html=True) -> str:
    """Extract and decode body from Gmail API message JSON."""
    payload = data.get("payload", {})

    if prefer_html:
        # Try HTML first, fallback to plain
        html_data = find_part_data(payload, "text/html")
        if html_data:
            return decode_base64url(html_data)
        plain_data = find_part_data(payload, "text/plain")
        if plain_data:
            return decode_base64url(plain_data)
    else:
        # Try plain first, fallback to HTML
        plain_data = find_part_data(payload, "text/plain")
        if plain_data:
            return decode_base64url(plain_data)
        html_data = find_part_data(payload, "text/html")
        if html_data:
            return decode_base64url(html_data)

    # Single part (no multipart)
    body_data = payload.get("body", {}).get("data", "")
    if body_data:
        return decode_base64url(body_data)

    return "(Khong tim thay body trong email)"


def main():
    parser = argparse.ArgumentParser(
        description="Decode Gmail API JSON response to readable HTML"
    )
    parser.add_argument(
        "input",
        nargs="?",
        default="-",
        help="Input file (Gmail API JSON) or '-' for stdin",
    )
    parser.add_argument("-o", "--output", help="Output file path (default: stdout)")
    parser.add_argument(
        "--plain",
        action="store_true",
        help="Prefer plain text over HTML",
    )
    args = parser.parse_args()

    if args.input == "-":
        raw = sys.stdin.read()
    else:
        with open(args.input, "r", encoding="utf-8") as f:
            raw = f.read()

    raw = raw.strip()

    # Try parsing as Gmail API JSON
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "payload" in data:
            result = extract_from_gmail_json(data, prefer_html=not args.plain)
        else:
            # Not a Gmail message, try as raw base64url
            result = decode_base64url(raw)
    except json.JSONDecodeError:
        # Not JSON — treat as raw base64url string
        result = decode_base64url(raw)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(result)
        print(f"Da ghi ket qua vao: {args.output}")
    else:
        print(result)


if __name__ == "__main__":
    main()
