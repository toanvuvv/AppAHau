"""
One-time OAuth2 authentication per Gmail account.
Usage: python auth.py --email user@gmail.com --token tokens/token_user.json
"""
import argparse
import json
import os
import sys

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets",
]

CREDENTIALS_FILE = "credentials.json"


def load_existing_token(token_file):
    """Load and return existing credentials if valid, or refresh if expired."""
    if not os.path.exists(token_file):
        return None

    creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if creds.valid:
        print(f"[OK] Token '{token_file}' con han su dung. Khong can xac thuc lai.")
        return creds

    if creds.expired and creds.refresh_token:
        print(f"[INFO] Token het han, dang refresh...")
        creds.refresh(Request())
        save_token(creds, token_file)
        print(f"[OK] Da refresh token thanh cong: {token_file}")
        return creds

    return None


def run_oauth_flow(email, token_file):
    """Run OAuth2 flow to get new credentials."""
    if not os.path.exists(CREDENTIALS_FILE):
        print(f"[LOI] Khong tim thay file '{CREDENTIALS_FILE}'.")
        print("Hay tai file nay tu Google Cloud Console va dat vao thu muc project.")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(
        CREDENTIALS_FILE,
        scopes=SCOPES,
    )

    print("\n" + "=" * 60)
    print(f"XAC THUC CHO TAI KHOAN: {email}")
    print("=" * 60)
    print("\nHuong dan:")
    print("1. Trinh duyet se tu dong mo trang xac thuc")
    print(f"2. Dang nhap bang DUNG tai khoan: {email}")
    print("3. Bam 'Cho phep' (Allow)")
    print("4. Quay lai terminal khi hoan tat\n")

    creds = flow.run_local_server(port=0)

    os.makedirs(os.path.dirname(token_file) or ".", exist_ok=True)
    save_token(creds, token_file)
    print(f"\n[OK] Xac thuc thanh cong! Token da luu tai: {token_file}")
    return creds


def save_token(creds, token_file):
    """Save credentials to JSON file."""
    dirname = os.path.dirname(token_file)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    with open(token_file, "w") as f:
        f.write(creds.to_json())


def main():
    parser = argparse.ArgumentParser(description="Xac thuc Gmail OAuth2")
    parser.add_argument("--email", required=True, help="Dia chi Gmail can xac thuc")
    parser.add_argument("--token", required=True, help="Duong dan file token output")
    args = parser.parse_args()

    existing = load_existing_token(args.token)
    if existing:
        return

    run_oauth_flow(args.email, args.token)


if __name__ == "__main__":
    main()
