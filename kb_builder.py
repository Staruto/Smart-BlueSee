"""
Knowledge Base Builder — Web Scraper for UNNC

Fetches content from the UNNC website and other configured URLs,
extracts text, and converts it into [Section]-formatted .txt files
for the knowledge base.

Usage:
    python kb_builder.py                    # Scrape all configured pages
    python kb_builder.py --url URL          # Scrape a single URL
    python kb_builder.py --list             # List configured sources
    python kb_builder.py --update           # Re-scrape and update existing KB files

Requirements:
    pip install requests beautifulsoup4
"""

import os
import re
import sys
import argparse
import datetime
from typing import List, Tuple
from urllib.parse import urljoin, urlparse

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing dependencies. Install with:")
    print("  pip install requests beautifulsoup4")
    sys.exit(1)

from config import KNOWLEDGE_DIR, BASE_DIR

# ═══════════════════════════════════════════════════════════
#  Configuration: pages to scrape
# ═══════════════════════════════════════════════════════════
# Each entry: (output_filename, section_title, url)
# Output files go into KNOWLEDGE_DIR/web/

SCRAPE_SOURCES: List[Tuple[str, str, str]] = [
    # Key dates / academic calendar
    (
        "web_key_dates.txt",
        "Official Key Dates",
        "https://www.nottingham.edu.cn/en/about/key-dates/key-dates.aspx",
    ),
    # Fees and scholarships
    (
        "web_fees_scholarships.txt",
        "Fees and Scholarships",
        "https://www.nottingham.edu.cn/en/study-with-us/fees-and-scholarships.aspx",
    ),
    # Accommodation
    (
        "web_accommodation.txt",
        "Accommodation",
        "https://www.nottingham.edu.cn/en/accommodation/home.aspx",
    ),
    # Careers
    (
        "web_careers.txt",
        "Careers and Employability",
        "https://www.nottingham.edu.cn/en/careers/",
    ),
    # IT Services
    (
        "web_it_services.txt",
        "IT Services (Official)",
        "https://www.nottingham.edu.cn/en/it-services/",
    ),
    # The Hub
    (
        "web_the_hub.txt",
        "The Hub (Official)",
        "https://www.nottingham.edu.cn/en/the-hub/",
    ),
    # Sports
    (
        "web_sports.txt",
        "Sports (Official)",
        "https://www.nottingham.edu.cn/en/sport/",
    ),
    # Health and wellbeing
    (
        "web_health_wellbeing.txt",
        "Health and Wellbeing (Official)",
        "https://www.nottingham.edu.cn/en/health-and-wellbeing-centre/home.aspx",
    ),
    # Library
    (
        "web_library.txt",
        "Library (Official)",
        "https://www.nottingham.edu.cn/en/library/",
    ),
    # Graduate school
    (
        "web_graduate_school.txt",
        "Graduate School",
        "https://www.nottingham.edu.cn/en/graduateschool/",
    ),
    # Teaching and learning
    (
        "web_teaching_learning.txt",
        "Teaching and Learning",
        "https://www.nottingham.edu.cn/en/teaching-learning/home.aspx",
    ),
    # Global / exchange
    (
        "web_global_exchange.txt",
        "Global Exchange Programs",
        "https://www.nottingham.edu.cn/en/global/exchange-and-study-abroad/outbound/outbound-exchange.aspx",
    ),
    # International student support
    (
        "web_intl_support.txt",
        "International Student Support",
        "https://www.nottingham.edu.cn/en/global/student-support/student-support.aspx",
    ),
    # Visa and immigration
    (
        "web_visa.txt",
        "Immigration and Visa",
        "https://www.nottingham.edu.cn/en/global/student-support/immigration-and-visa.aspx",
    ),
    # Course search (undergraduate)
    (
        "web_courses_ug.txt",
        "Undergraduate Programs (Official)",
        "https://www.nottingham.edu.cn/en/study-with-us/undergraduate/home.aspx",
    ),
    # Course search (postgraduate)
    (
        "web_courses_pg.txt",
        "Postgraduate Programs (Official)",
        "https://www.nottingham.edu.cn/en/study-with-us/postgraduate-taught/home.aspx",
    ),
    # Graduation
    (
        "web_graduation.txt",
        "Graduation",
        "https://www.nottingham.edu.cn/en/graduation/graduation.aspx",
    ),
]


# ═══════════════════════════════════════════════════════════
#  Scraping helpers
# ═══════════════════════════════════════════════════════════
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Tags to remove entirely (nav, footer, scripts, etc.)
REMOVE_TAGS = {"script", "style", "nav", "footer", "noscript", "svg", "img"}


def fetch_page(url: str, timeout: int = 15) -> str | None:
    """Fetch a URL and return raw HTML, or None on failure."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        print(f"  [Error] Failed to fetch {url}: {e}")
        return None


def _clean_lines(raw_text: str) -> list[str]:
    """Split text, strip, deduplicate consecutive identical lines."""
    lines = [l.strip() for l in raw_text.split("\n") if l.strip()]
    deduped: list[str] = []
    prev = None
    for line in lines:
        if line != prev:
            deduped.append(line)
            prev = line
    return deduped


def _filter_nav_lines(lines: list[str]) -> list[str]:
    """Remove site-wide navigation, UI chrome, and footer lines."""

    # Exact nav labels (lower-cased, trailing period stripped)
    nav_exact = {
        "students", "staff", "alumni", "china", "study with us", "uk", "malaysia",
        "search", "research", "global", "about", "more", "university life",
        "schools and departments", "faculties", "courses & admission",
        "courses & programmes", "undergraduate", "postgraduate taught",
        "postgraduate research", "admission", "entry requirements",
        "fees and scholarships", "how to apply", "global recruitment",
        "exchange & study abroad", "student services", "faculties and schools",
        "schools & departments", "activities and wellbeing",
        "nottinghamhub", "staff/student portal", "job opportunities",
        "business development", "education foundation", "key dates",
        "open days", "visitor information", "quick links",
        "global engagement", "research strength", "menu", "courses",
        "discover our research", "university strategy", "university leadership",
        "facts & accreditations", "sustainability", "our brand",
        "information disclosure", "annual quality report",
        "360° virtual campus tour", "video hub", "confucius institute",
        "estates", "library", "it services",
        "academic services", "department of campus life", "the hub", "sport",
        "health and wellbeing centre", "careers and employability service",
        "teaching and learning", "personal tutorials", "arts centre",
        "accommodation", "graduation",
        "research integrity & ethics", "research database",
        "commercial initiative", "inspiring people", "sustainable development",
        "environment", "health", "transport", "beacons of excellence",
        "centre for english language education", "graduate school",
        "china beacons institute", "professional service departments",
        "research centres", "make an enquiry", "course search",
        "training & summer programmes",
        "international student support", "immigration and visa",
        "hk, macao and taiwan affairs", "international partners",
        "overseas summer programme",
        "nottingham university business school china",
        "faculty of humanities and social sciences",
        "faculty of science and engineering",
        "master of business administration (mba)",
        "for international applicants",
        "for chinese applicants",
    }

    ui_re = re.compile(
        r"^(Read more|Learn more|Apply now|Click here|Back to top|Download|"
        r"Find out more|Find more|Come and visit|Explore what|Buy your|"
        r"Link arrow|Link button|linkArrowAlt|"
        r"Downward arrow|Black arrow|White arrow|"
        r"Banner Image|Accreditation image|Logo of University.*|"
        r"Get in touch|Contact us|Copyright .*|"
        r"Chat with .*ambassador|Browse all .*|"
        r".*Globe shaped.*|.*icon-.*|.*Thumbnail.*|"
        r"T\. \+86.*|E\. .*@nottingham\.edu\.cn)$",
        re.I,
    )

    footer_re = re.compile(
        r"^(University of Nottingham Ningbo China|"
        r"199 Taikang East Road.*|"
        r"Ningbo.*315100.*|"
        r"Postcode: 315100|"
        r"\+86 \(0\)574.*|"
        r"\+86 \(0\) 574.*|"
        r"Directions.*arrow|"
        r"©.*Nottingham.*)$",
        re.I,
    )

    filtered = []
    for line in lines:
        if len(line) <= 3:
            continue
        low = line.lower().rstrip(".").strip()
        if low in nav_exact:
            continue
        if ui_re.match(line):
            continue
        if footer_re.match(line):
            continue
        if line.startswith("Browser does not support"):
            continue
        if line.count("|") > 2:
            continue
        filtered.append(line)
    return filtered


def extract_text(html: str, url: str = "") -> str:
    """
    Parse HTML and extract meaningful text content from UNNC pages.

    Strategy (v3 — DOM-aware):
      1. Remove script/style/nav/footer/noscript/svg/img tags globally.
      2. Find `#main` (exists on all UNNC pages).
      3. Remove noise sub-elements: .side-nav, <header>, <footer> within #main.
      4. Prefer rich-content containers: `.rte` and `.accordion__panel`.
         If they yield ≥100 chars of text, use that.
      5. Otherwise fall back to the full cleaned #main text.
      6. Apply nav-fingerprint line filter.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Remove unwanted tags globally
    for tag_name in REMOVE_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    # ── Find #main ──
    main_node = soup.find(id="main")
    if not main_node:
        # Fallback: try role="main", then <body>
        main_node = soup.find(attrs={"role": "main"})
    if not main_node:
        main_node = soup.body or soup

    # ── Remove known noise sub-elements inside #main ──
    for sel in [
        {"class_": "side-nav"},
        {"class_": "breadcrumb"},
        {"class_": "breadcrumbs"},
        {"class_": "social-share"},
        {"class_": "cookie-banner"},
    ]:
        for tag in main_node.find_all(**sel):
            tag.decompose()
    for tag in main_node.find_all("header"):
        tag.decompose()
    # Note: we already removed <footer> globally via REMOVE_TAGS

    # ── Strategy A: Use .rte + .accordion__panel (rich content containers) ──
    rich_parts: list[str] = []
    for cls in ("rte", "accordion__panel"):
        for elem in main_node.find_all(class_=cls):
            text = elem.get_text(separator="\n", strip=True)
            if text:
                rich_parts.append(text)

    if rich_parts:
        combined = "\n".join(rich_parts)
        if len(combined) >= 100:
            lines = _clean_lines(combined)
            lines = _filter_nav_lines(lines)
            if lines:
                return "\n".join(lines)

    # ── Strategy B: Use cleaned #main text ──
    raw_text = main_node.get_text(separator="\n", strip=True)
    lines = _clean_lines(raw_text)
    lines = _filter_nav_lines(lines)
    return "\n".join(lines)


def format_as_section(title: str, content: str, url: str) -> str:
    """Format extracted content as a KB section."""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    header = f"[{title}]"
    source_line = f"Source: {url}"
    updated_line = f"Last updated: {timestamp}"
    return f"{header}\n{source_line}\n{updated_line}\n\n{content}\n"


# ═══════════════════════════════════════════════════════════
#  Main operations
# ═══════════════════════════════════════════════════════════
def scrape_single(url: str, title: str, output_path: str) -> bool:
    """Scrape one URL and save to file. Returns True on success."""
    print(f"  Fetching: {url}")
    html = fetch_page(url)
    if not html:
        return False

    content = extract_text(html, url)
    if not content or len(content) < 50:
        print(f"  [Warning] Extracted content too short ({len(content)} chars), skipping.")
        return False

    formatted = format_as_section(title, content, url)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(formatted)

    print(f"  Saved: {os.path.basename(output_path)} ({len(content)} chars)")
    return True


def scrape_all():
    """Scrape all configured sources."""
    web_dir = os.path.join(KNOWLEDGE_DIR, "web")
    os.makedirs(web_dir, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  UNNC Knowledge Base Builder")
    print(f"  Output: {web_dir}")
    print(f"  Sources: {len(SCRAPE_SOURCES)} pages")
    print(f"{'='*60}\n")

    success = 0
    failed = 0

    for filename, title, url in SCRAPE_SOURCES:
        output_path = os.path.join(web_dir, filename)
        if scrape_single(url, title, output_path):
            success += 1
        else:
            failed += 1

    print(f"\nDone: {success} succeeded, {failed} failed.")
    print(f"Knowledge files saved to: {web_dir}")
    print(f"\nRestart the assistant or use /reload to load the new content.")


def scrape_url(url: str, title: str | None = None):
    """Scrape a single user-specified URL."""
    web_dir = os.path.join(KNOWLEDGE_DIR, "web")
    os.makedirs(web_dir, exist_ok=True)

    if not title:
        # Generate title from URL path
        parsed = urlparse(url)
        title = parsed.path.rstrip("/").split("/")[-1].replace("-", " ").replace("_", " ").title()
        if not title:
            title = parsed.netloc

    # Generate filename from title
    safe_name = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
    filename = f"web_custom_{safe_name}.txt"
    output_path = os.path.join(web_dir, filename)

    print(f"\nScraping: {url}")
    print(f"Title: {title}")
    scrape_single(url, title, output_path)


def list_sources():
    """Print all configured scrape sources."""
    print(f"\nConfigured scrape sources ({len(SCRAPE_SOURCES)}):\n")
    for i, (filename, title, url) in enumerate(SCRAPE_SOURCES, 1):
        web_path = os.path.join(KNOWLEDGE_DIR, "web", filename)
        exists = "✓" if os.path.exists(web_path) else "✗"
        print(f"  {exists} {i:2d}. [{title}]")
        print(f"       {url}")
        print(f"       → {filename}")
        print()


# ═══════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(
        description="UNNC Knowledge Base Builder — scrape web content into KB files"
    )
    parser.add_argument("--url", type=str, help="Scrape a specific URL")
    parser.add_argument("--title", type=str, help="Section title for --url")
    parser.add_argument("--list", action="store_true", help="List configured sources")
    parser.add_argument("--update", action="store_true",
                        help="Re-scrape all sources (same as running without args)")

    args = parser.parse_args()

    if args.list:
        list_sources()
    elif args.url:
        scrape_url(args.url, args.title)
    else:
        scrape_all()


if __name__ == "__main__":
    main()
