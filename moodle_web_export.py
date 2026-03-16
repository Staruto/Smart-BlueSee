"""
Export Moodle pages to local HTML files for offline KB ingestion.

Workflow:
1) Launches browser to Moodle login page.
2) You log in manually in the opened browser window.
3) Script crawls discovered course and module links and saves HTML.
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
    key = (p.path + "?" + (p.query or "")).strip("?")
    key = re.sub(r"[^a-zA-Z0-9]+", "_", key).strip("_")
    return (key or "page")[:120]


def same_origin(base: str, candidate: str) -> bool:
    b = urlparse(base)
    c = urlparse(candidate)
    return b.scheme == c.scheme and b.netloc == c.netloc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export Moodle HTML after manual login")
    parser.add_argument("--base-url", default="https://moodle.nottingham.ac.uk/", help="Moodle base URL")
    parser.add_argument(
        "--output-dir",
        default=os.path.join("knowledge", "import", "raw", "moodle_html"),
        help="Directory to save exported html",
    )
    parser.add_argument("--max-pages", type=int, default=120, help="Maximum pages to save")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    ensure_dir(args.output_dir)

    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception:
        print("Playwright is required. Install:")
        print("  pip install playwright")
        print("  python -m playwright install chromium")
        raise

    visited: set[str] = set()
    queue: list[str] = [args.base_url]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        print(f"[Moodle] Opening: {args.base_url}")
        page.goto(args.base_url, wait_until="domcontentloaded")
        input("[Action Required] Login manually in browser, then press ENTER here to continue...")

        while queue and len(visited) < args.max_pages:
            url = queue.pop(0)
            if url in visited:
                continue
            if not same_origin(args.base_url, url):
                continue

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                continue

            visited.add(url)
            html = page.content()
            fname = os.path.join(args.output_dir, f"{len(visited):04d}_{safe_name(url)}.html")
            with open(fname, "w", encoding="utf-8") as f:
                f.write(html)
            print(f"[Saved] {fname}")

            hrefs = page.eval_on_selector_all(
                "a[href]",
                "els => els.map(e => e.getAttribute('href')).filter(Boolean)",
            )

            for href in hrefs:
                full = urljoin(url, href)
                if not same_origin(args.base_url, full):
                    continue
                if "/course/view.php" in full or "/mod/" in full or "/course/" in full:
                    if full not in visited and full not in queue:
                        queue.append(full)

        browser.close()

    print(f"[Moodle] Export done. Pages saved: {len(visited)}")
    print(f"[Moodle] Output directory: {args.output_dir}")
    print("[Next] Run kb_importer.py with --moodle-path pointing to this folder.")


if __name__ == "__main__":
    main()
