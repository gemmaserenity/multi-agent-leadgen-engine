"""
scraper.py

Searches SerpApi (Google engine) for companies matching the ICP profile.
Extracts company name, website URL, and description from results.
Deduplicates by domain and saves to data/raw_leads_{timestamp}.csv.

Usage:
    python agents/scraper.py                        # uses config/icp_futuri.json
    python agents/scraper.py --icp icp_custom.json
    python agents/scraper.py --pages 3              # fetch up to 3 pages per query (default: 1)
    python agents/scraper.py --limit 5              # stop after N unique companies
    python agents/scraper.py --dry-run              # print queries, no API calls
"""

import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
CONFIG_DIR = ROOT / "config"

DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

SERPAPI_URL = "https://serpapi.com/search"


# Domains that produce noise — job boards, social, reference sites
BLOCKED_DOMAINS = {
    "indeed.com", "glassdoor.com", "ziprecruiter.com", "monster.com",
    "careerbuilder.com", "simplyhired.com", "lever.co", "greenhouse.io",
    "workday.com", "jobs.com", "twitter.com", "x.com", "facebook.com",
    "instagram.com", "youtube.com", "tiktok.com", "wikipedia.org",
    "wikihow.com", "quora.com", "reddit.com",
}

FIELDNAMES = [
    "company",
    "website",
    "domain",
    "description",
    "industry_searched",
    "title_searched",
    "source",
    "scraped_at",
    "enriched",
    "qualified",
]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logger(name: str) -> logging.Logger:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    fh = logging.FileHandler(LOG_DIR / f"{name}_{ts}.log", encoding="utf-8")
    fh.setFormatter(fmt)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


# ---------------------------------------------------------------------------
# ICP
# ---------------------------------------------------------------------------

def load_icp(filename: str) -> dict:
    path = CONFIG_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"ICP profile not found: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# URL / domain helpers
# ---------------------------------------------------------------------------

def extract_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def is_blocked(url: str) -> bool:
    domain = extract_domain(url)
    if not domain:
        return True
    if domain in BLOCKED_DOMAINS:
        return True
    # Skip LinkedIn individual profiles (/in/); keep company pages (/company/)
    if "linkedin.com" in domain and "/in/" in urlparse(url).path:
        return True
    return False


def extract_company_name(item: dict) -> str:
    """
    Best-effort company name from a CSE result.
    Tries the first segment of the page title before a separator,
    then falls back to a humanised domain name.
    """
    title = item.get("title", "").strip()
    for sep in (" | ", " - ", " – ", " — "):
        if sep in title:
            candidate = title.split(sep)[0].strip()
            if 2 <= len(candidate) <= 80:
                return candidate
    # Fall back: capitalise the SLD of the domain
    domain = extract_domain(item.get("link", ""))
    sld = domain.split(".")[0] if domain else ""
    return sld.replace("-", " ").title() if sld else title[:80]


# ---------------------------------------------------------------------------
# SerpApi
# ---------------------------------------------------------------------------

def build_query(title: str, industry: str) -> str:
    return f'"{title}" "{industry}" enterprise'


def fetch_serpapi_page(
    query: str,
    api_key: str,
    page: int,
    logger: logging.Logger,
) -> list[dict]:
    """Fetch one page (up to 10 results) from SerpApi."""
    params = {
        "api_key": api_key,
        "engine": "google",
        "q": query,
        "num": 10,
        "start": (page - 1) * 10,
    }
    try:
        resp = requests.get(SERPAPI_URL, params=params, timeout=15)
    except requests.RequestException as e:
        logger.warning(f"  Request error: {e}")
        return []

    if resp.status_code == 429:
        logger.warning("  SerpApi rate limited — sleeping 60s")
        time.sleep(60)
        return fetch_serpapi_page(query, api_key, page, logger)

    if resp.status_code != 200:
        logger.warning(f"  SerpApi {resp.status_code}: {resp.text[:200]}")
        return []

    return resp.json().get("organic_results", [])


def search_leads(
    icp: dict,
    logger: logging.Logger,
    pages: int = 1,
    limit: int | None = None,
    dry_run: bool = False,
) -> list[dict]:
    api_key = os.getenv("SERPAPI_KEY", "").strip()

    if not dry_run and not api_key:
        raise EnvironmentError("SERPAPI_KEY is not set in .env")

    leads: list[dict] = []
    seen_domains: set[str] = set()

    for title in icp["target_titles"]:
        for industry in icp["target_industries"]:
            query = build_query(title, industry)

            if dry_run:
                logger.info(f"[DRY RUN] Would search: {query!r}")
                continue

            logger.info(f"Searching: {query!r}")

            for page in range(1, pages + 1):
                items = fetch_serpapi_page(query, api_key, page, logger)
                if not items:
                    break

                added = 0
                for item in items:
                    if limit is not None and len(leads) >= limit:
                        break

                    url = item.get("link", "")
                    if not url or is_blocked(url):
                        continue

                    domain = extract_domain(url)
                    if domain in seen_domains:
                        continue
                    seen_domains.add(domain)

                    leads.append({
                        "company": extract_company_name(item),
                        "website": url,
                        "domain": domain,
                        "description": item.get("snippet", "").replace("\n", " ").strip(),
                        "industry_searched": industry,
                        "title_searched": title,
                        "source": "serpapi",
                        "scraped_at": datetime.now().isoformat(),
                        "enriched": "false",
                        "qualified": "",
                    })
                    added += 1

                logger.info(f"  Page {page}: {len(items)} results, {added} new companies added")

                if limit is not None and len(leads) >= limit:
                    break
                if len(items) < 10:
                    break  # fewer than a full page means no more results

                time.sleep(1)  # stay within SerpApi rate limits

            if limit is not None and len(leads) >= limit:
                break
        if limit is not None and len(leads) >= limit:
            break

    return leads


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def save_leads(leads: list[dict], logger: logging.Logger) -> Path | None:
    if not leads:
        logger.warning("No leads to save.")
        return None

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = DATA_DIR / f"raw_leads_{ts}.csv"

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(leads)

    logger.info(f"Saved {len(leads)} companies → {out_path.relative_to(ROOT)}")
    return out_path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lead scraper — SerpApi Google Search")
    parser.add_argument("--icp", default="icp_futuri.json", help="ICP filename inside config/")
    parser.add_argument(
        "--pages", type=int, default=1,
        help="Pages of results to fetch per query, 10 results each (default: 1)",
    )
    parser.add_argument("--limit", type=int, default=None, help="Stop after collecting this many unique companies")
    parser.add_argument("--dry-run", action="store_true", help="Print queries without hitting the API")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logger = setup_logger("scraper")
    logger.info("=" * 60)
    logger.info("Scraper started")
    logger.info(f"ICP: {args.icp} | pages={args.pages} | dry_run={args.dry_run}")

    try:
        icp = load_icp(args.icp)
        logger.info(f"Client: {icp['client']} — Product: {icp['product']}")
        logger.info(f"Titles:     {icp['target_titles']}")
        logger.info(f"Industries: {icp['target_industries']}")

        leads = search_leads(icp, logger, pages=args.pages, limit=args.limit, dry_run=args.dry_run)
        logger.info(f"Total unique companies collected: {len(leads)}")

        out_path = save_leads(leads, logger)
        if out_path:
            logger.info(f"Run complete. Next step: python agents/enricher.py --input {out_path.name}")

    except (FileNotFoundError, EnvironmentError) as e:
        logger.error(str(e))
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
