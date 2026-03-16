"""
Generic authenticated site exporter (manual login) for dynamic portals.

Use this for sites like timetabling portal where direct requests scraping returns no text.
Exports pages as HTML for later ingestion via kb_importer --docs-path.
"""

from __future__ import annotations

import argparse
import os
import re
from urllib.parse import urljoin, urlparse


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def safe_name(url: str) -> str:
    p = urlparse(url)
    text = (p.path + ("_" + p.query if p.query else "")).strip("_")
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_")
    return (text or "page")[:120]


def same_origin(base: str, u: str) -> bool:
    b = urlparse(base)
    c = urlparse(u)
    return (b.scheme, b.netloc) == (c.scheme, c.netloc)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export authenticated website pages to html")
    p.add_argument("--base-url", required=True, help="Portal base URL")
    p.add_argument(
        "--output-dir",
        default=os.path.join("knowledge", "import", "raw", "site_html"),
        help="Directory for html output",
    )
    p.add_argument("--max-pages", type=int, default=100, help="Maximum pages to export")
    p.add_argument(
        "--include-regex",
        default="",
        help="Optional regex to keep only matching URLs (example: 'sws|timetable|student')",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    ensure_dir(args.output_dir)

    include_re = re.compile(args.include_regex, re.IGNORECASE) if args.include_regex else None

    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception:
        print("Playwright is required. Install:")
        print("  pip install playwright")
        print("  python -m playwright install chromium")
        return

    visited: set[str] = set()
    queue: list[str] = [args.base_url]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(args.base_url, wait_until="domcontentloaded")
        input("[Action Required] Login and navigate to a representative page, then press ENTER...")

        while queue and len(visited) < args.max_pages:
            url = queue.pop(0)
            if url in visited:
                continue
            if not same_origin(args.base_url, url):
                continue
            if include_re and not include_re.search(url):
                continue

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                continue

            html = page.content()
            visited.add(url)
            out = os.path.join(args.output_dir, f"{len(visited):04d}_{safe_name(url)}.html")
            with open(out, "w", encoding="utf-8") as f:
                f.write(html)
            print(f"[Saved] {out}")

            hrefs = page.eval_on_selector_all(
                "a[href]",
                "els => els.map(e => e.getAttribute('href')).filter(Boolean)",
            )
            for href in hrefs:
                full = urljoin(url, href)
                if full not in visited and full not in queue and same_origin(args.base_url, full):
                    queue.append(full)

        browser.close()

    print(f"[Site] Export complete. Saved {len(visited)} pages to {args.output_dir}")
    print("[Next] Run kb_importer.py --docs-path knowledge/import/raw/site_html")


if __name__ == "__main__":
    main()
