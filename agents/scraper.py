"""
scraper.py

Searches Apollo.io People Search API for contacts matching the ICP profile.
Finds real people at real companies — name, title, email, LinkedIn URL.
Deduplicates by domain and saves to data/raw_leads_{timestamp}.csv.

Usage:
    python agents/scraper.py                        # uses config/icp_futuri.json
    python agents/scraper.py --icp icp_custom.json
    python agents/scraper.py --pages 3              # fetch up to 3 pages, 25 results each (default: 1)
    python agents/scraper.py --limit 50             # stop after N results
    python agents/scraper.py --dry-run              # log search params, no API calls
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

APOLLO_SEARCH_URL = "https://api.apollo.io/v1/mixed_people/search"

FIELDNAMES = [
    "contact_name",
    "contact_title",
    "contact_email",
    "linkedin_url",
    "company",
    "website",
    "domain",
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
# Domain helper
# ---------------------------------------------------------------------------

def extract_domain(url: str) -> str:
    if not url:
        return ""
    try:
        netloc = urlparse(url).netloc.lower()
        return netloc.removeprefix("www.")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Apollo.io People Search
# ---------------------------------------------------------------------------

def fetch_apollo_page(
    api_key: str,
    titles: list[str],
    page: int,
    logger: logging.Logger,
) -> list[dict]:
    """POST one page of Apollo People Search results (up to 25 per page)."""
    payload = {
        "api_key": api_key,
        "person_titles": titles,
        "organization_industry_tag_ids": [],
        "organization_num_employees_ranges": ["100,10000"],
        "page": page,
        "per_page": 25,
    }
    headers = {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
    }
    try:
        resp = requests.post(APOLLO_SEARCH_URL, json=payload, headers=headers, timeout=20)
    except requests.RequestException as e:
        logger.warning(f"  Request error: {e}")
        return []

    if resp.status_code == 429:
        logger.warning("  Apollo rate limited — sleeping 30s")
        time.sleep(30)
        return fetch_apollo_page(api_key, titles, page, logger)

    if resp.status_code != 200:
        logger.warning(f"  Apollo {resp.status_code}: {resp.text[:200]}")
        return []

    return resp.json().get("people", [])


def search_leads(
    icp: dict,
    logger: logging.Logger,
    pages: int = 1,
    limit: int | None = None,
    dry_run: bool = False,
) -> list[dict]:
    api_key = os.getenv("APOLLO_API_KEY", "").strip()

    if not dry_run and not api_key:
        raise EnvironmentError("APOLLO_API_KEY is not set in .env")

    titles = icp.get("target_titles", [])

    if dry_run:
        logger.info(f"[DRY RUN] Would search Apollo with:")
        logger.info(f"  person_titles: {titles}")
        logger.info(f"  organization_num_employees_ranges: ['100,10000']")
        logger.info(f"  pages: {pages}, per_page: 25")
        return []

    leads: list[dict] = []
    seen_domains: set[str] = set()

    for page in range(1, pages + 1):
        logger.info(f"Fetching page {page} of {pages}…")

        people = fetch_apollo_page(api_key, titles, page, logger)
        if not people:
            logger.info("  No results — stopping pagination")
            break

        added = 0
        for person in people:
            if limit is not None and len(leads) >= limit:
                break

            org = person.get("organization") or {}
            domain = (
                org.get("primary_domain")
                or extract_domain(org.get("website_url", ""))
            ).strip().lower()

            if not domain or domain in seen_domains:
                continue
            seen_domains.add(domain)

            leads.append({
                "contact_name":  person.get("name", ""),
                "contact_title": person.get("title", ""),
                "contact_email": person.get("email", "") or "",
                "linkedin_url":  person.get("linkedin_url", "") or "",
                "company":       org.get("name", ""),
                "website":       org.get("website_url", "") or "",
                "domain":        domain,
                "source":        "apollo",
                "scraped_at":    datetime.now().isoformat(),
                "enriched":      "false",
                "qualified":     "",
            })
            added += 1

        logger.info(f"  Page {page}: {len(people)} people returned, {added} new companies added")

        if limit is not None and len(leads) >= limit:
            break
        if len(people) < 25:
            break  # fewer than a full page — no more results

        time.sleep(1)

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

    logger.info(f"Saved {len(leads)} leads → {out_path.relative_to(ROOT)}")
    return out_path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lead scraper — Apollo.io People Search")
    parser.add_argument("--icp", default="icp_futuri.json", help="ICP filename inside config/")
    parser.add_argument(
        "--pages", type=int, default=1,
        help="Pages of Apollo results to fetch, 25 results each (default: 1)",
    )
    parser.add_argument("--limit", type=int, default=None, help="Stop after collecting this many leads")
    parser.add_argument("--dry-run", action="store_true", help="Log search params without hitting the API")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logger = setup_logger("scraper")
    logger.info("=" * 60)
    logger.info("Scraper started")
    logger.info(f"ICP: {args.icp} | pages={args.pages} | dry_run={args.dry_run}")

    try:
        icp = load_icp(args.icp)
        logger.info(f"Client:  {icp['client']} — Product: {icp['product']}")
        logger.info(f"Titles:  {icp['target_titles']}")
        logger.info(f"Min employees: {icp.get('company_size_min_employees', 100)}")

        leads = search_leads(icp, logger, pages=args.pages, limit=args.limit, dry_run=args.dry_run)
        logger.info(f"Total leads collected: {len(leads)}")

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
