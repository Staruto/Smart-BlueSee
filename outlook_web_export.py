"""
Export Outlook Web mailbox content to local .txt files.

This script does not store credentials. You log in manually in the browser.
The exported files can be ingested using kb_importer.py via --docs-path.
"""

from __future__ import annotations

import argparse
import os
import re
import time


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def safe_name(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_")
    return (text or "mail")[:100]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export Outlook Web inbox messages to local txt")
    p.add_argument("--url", default="https://outlook.office.com/mail/", help="Outlook web mail URL")
    p.add_argument(
        "--output-dir",
        default=os.path.join("knowledge", "import", "raw", "email_web_txt"),
        help="Output folder for exported txt files",
    )
    p.add_argument("--max-mails", type=int, default=80, help="Max messages to export")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    ensure_dir(args.output_dir)

    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception:
        print("Playwright is required. Install with:")
        print("  pip install playwright")
        print("  python -m playwright install chromium")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        page.goto(args.url, wait_until="domcontentloaded")
        input("[Action Required] Login in browser, open Inbox, then press ENTER...")

        # Try to load more mails by scrolling message list panel.
        for _ in range(6):
            page.mouse.wheel(0, 2000)
            time.sleep(0.4)

        # Common message-row selectors in Outlook Web.
        row_selectors = [
            'div[role="option"]',
            'div[aria-selected][role="option"]',
            'div[draggable="true"][role="option"]',
        ]

        rows = []
        for sel in row_selectors:
            rows = page.query_selector_all(sel)
            if rows:
                break

        if not rows:
            print("[Outlook] Could not find message rows. Keep Inbox visible and retry.")
            browser.close()
            return

        exported = 0
        for i, row in enumerate(rows[: args.max_mails], 1):
            try:
                row.click(timeout=5000)
                page.wait_for_timeout(600)

                subject = "(No Subject)"
                for s in [
                    'h1[role="heading"]',
                    'div[role="heading"]',
                    'span[title]'
                ]:
                    node = page.query_selector(s)
                    if node:
                        txt = (node.inner_text() or "").strip()
                        if txt:
                            subject = txt
                            break

                body = ""
                for s in [
                    'div[aria-label*="Message body"]',
                    'div[role="document"]',
                    'div[data-app-section="MailReadCompose"]'
                ]:
                    node = page.query_selector(s)
                    if node:
                        txt = (node.inner_text() or "").strip()
                        if txt:
                            body = txt
                            break

                if len(body) < 20:
                    continue

                fname = os.path.join(args.output_dir, f"mail_{i:04d}_{safe_name(subject)}.txt")
                with open(fname, "w", encoding="utf-8") as f:
                    f.write(f"Subject: {subject}\n")
                    f.write("Source: Outlook Web Inbox\n\n")
                    f.write(body)
                    f.write("\n")
                exported += 1
                print(f"[Saved] {fname}")
            except Exception:
                continue

        browser.close()

    print(f"[Outlook] Export finished. Saved {exported} message text files.")
    print(f"[Outlook] Output: {args.output_dir}")
    print("[Next] Run kb_importer.py --docs-path knowledge/import/raw/email_web_txt")


if __name__ == "__main__":
    main()
