"""
scraper.py

Pulls raw leads from Proxycurl Person Search based on a loaded ICP profile.
Outputs a timestamped CSV to /data and logs progress to /logs.

Usage:
    python agents/scraper.py                        # uses config/icp_futuri.json
    python agents/scraper.py --icp icp_custom.json  # uses config/icp_custom.json
    python agents/scraper.py --dry-run              # prints params, makes no API calls
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

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
CONFIG_DIR = ROOT / "config"

DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)


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
# Proxycurl search
# ---------------------------------------------------------------------------

PROXYCURL_SEARCH_URL = "https://nubela.co/proxycurl/api/v2/search/person/"

# Maps our plain-English industry labels to Proxycurl's industry filter values.
# Extend as needed: https://nubela.co/proxycurl/docs#people-api-person-search-endpoint
INDUSTRY_MAP = {
    "sports teams": "sports",
    "enterprise companies": "information technology and services",
    "large sales organizations": "marketing and advertising",
}

def build_search_params(title: str, industry: str, page_size: int = 25) -> dict:
    params = {
        "role": title,
        "enrich_profile": "skip",
        "page_size": page_size,
    }
    mapped = INDUSTRY_MAP.get(industry.lower())
    if mapped:
        params["industries"] = mapped
    return params


def fetch_page(api_key: str, params: dict, logger: logging.Logger) -> list[dict]:
    resp = requests.get(
        PROXYCURL_SEARCH_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        params=params,
        timeout=30,
    )

    if resp.status_code == 429:
        logger.warning("Rate limited — sleeping 60s")
        time.sleep(60)
        return fetch_page(api_key, params, logger)

    if resp.status_code != 200:
        logger.warning(f"Search returned {resp.status_code}: {resp.text[:200]}")
        return []

    return resp.json().get("results", [])


def search_leads(icp: dict, logger: logging.Logger, dry_run: bool = False) -> list[dict]:
    api_key = os.getenv("PROXYCURL_API_KEY", "").strip()
    if not api_key:
        raise EnvironmentError("PROXYCURL_API_KEY is not set in .env")

    leads = []
    seen_urls: set[str] = set()

    for title in icp["target_titles"]:
        for industry in icp["target_industries"]:
            params = build_search_params(title, industry)

            if dry_run:
                logger.info(f"[DRY RUN] Would search — title={title!r}, industry={industry!r}, params={params}")
                continue

            logger.info(f"Searching: {title!r} | {industry!r}")
            results = fetch_page(api_key, params, logger)
            logger.info(f"  → {len(results)} results")

            for r in results:
                url = r.get("linkedin_profile_url", "")
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                leads.append({
                    "full_name": r.get("full_name", ""),
                    "headline": r.get("headline", ""),
                    "linkedin_url": url,
                    "company": "",          # populated by enricher.py
                    "email": "",            # populated by enricher.py
                    "industry_searched": industry,
                    "title_searched": title,
                    "source": "proxycurl_search",
                    "scraped_at": datetime.now().isoformat(),
                    "enriched": "false",
                    "qualified": "",
                })

            # Proxycurl recommends >=1s between requests
            time.sleep(1.2)

    return leads


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

FIELDNAMES = [
    "full_name", "headline", "linkedin_url", "company", "email",
    "industry_searched", "title_searched", "source",
    "scraped_at", "enriched", "qualified",
]


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
    parser = argparse.ArgumentParser(description="Lead scraper — Proxycurl Person Search")
    parser.add_argument("--icp", default="icp_futuri.json", help="ICP filename inside config/")
    parser.add_argument("--dry-run", action="store_true", help="Print search params without hitting the API")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logger = setup_logger("scraper")
    logger.info("=" * 60)
    logger.info("Scraper started")
    logger.info(f"ICP: {args.icp} | dry_run={args.dry_run}")

    try:
        icp = load_icp(args.icp)
        logger.info(f"Client: {icp['client']} — Product: {icp['product']}")
        logger.info(f"Titles:     {icp['target_titles']}")
        logger.info(f"Industries: {icp['target_industries']}")

        leads = search_leads(icp, logger, dry_run=args.dry_run)
        logger.info(f"Total unique leads collected: {len(leads)}")

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
