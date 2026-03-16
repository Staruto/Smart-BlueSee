"""
Fetch school emails locally via IMAP and save them as .eml files.

Security notes:
- Credentials are read at runtime via prompt/getpass only.
- No credentials are written to disk.
"""

from __future__ import annotations

import argparse
import datetime as dt
import imaplib
import os
from getpass import getpass


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch emails via IMAP and save as .eml")
    parser.add_argument("--host", default="outlook.office365.com", help="IMAP host")
    parser.add_argument("--port", type=int, default=993, help="IMAP SSL port")
    parser.add_argument("--username", required=True, help="Email username")
    parser.add_argument("--folder", default="INBOX", help="Mailbox folder name")
    parser.add_argument("--limit", type=int, default=200, help="Max latest emails to fetch")
    parser.add_argument(
        "--output-dir",
        default=os.path.join("knowledge", "import", "raw", "email_eml"),
        help="Directory to store fetched .eml files",
    )
    parser.add_argument("--since", default="", help="Optional IMAP date filter DD-MMM-YYYY")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    ensure_dir(args.output_dir)

    password = getpass("Email password (input hidden): ")

    print(f"[IMAP] Connecting to {args.host}:{args.port} ...")
    with imaplib.IMAP4_SSL(args.host, args.port) as imap:
        imap.login(args.username, password)
        status, _ = imap.select(args.folder)
        if status != "OK":
            raise RuntimeError(f"Failed to open folder: {args.folder}")

        if args.since:
            search_query = f'(SINCE "{args.since}")'
        else:
            search_query = "ALL"

        status, data = imap.search(None, search_query)
        if status != "OK":
            raise RuntimeError("IMAP search failed")

        ids = data[0].split()
        if not ids:
            print("[IMAP] No emails found with current filter.")
            return

        ids = ids[-args.limit :]
        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        saved = 0

        for idx, msg_id in enumerate(ids, 1):
            status, msg_data = imap.fetch(msg_id, "(RFC822)")
            if status != "OK" or not msg_data:
                continue

            raw_bytes = None
            for part in msg_data:
                if isinstance(part, tuple) and len(part) >= 2:
                    raw_bytes = part[1]
                    break

            if not raw_bytes:
                continue

            fname = os.path.join(args.output_dir, f"mail_{ts}_{idx:05d}.eml")
            with open(fname, "wb") as f:
                f.write(raw_bytes)
            saved += 1

        print(f"[IMAP] Done. Saved {saved} emails to: {args.output_dir}")
        print("[Next] Run kb_importer.py with --email-path pointing to this folder.")


if __name__ == "__main__":
    main()
