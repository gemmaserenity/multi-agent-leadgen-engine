"""
enricher.py

Reads a raw_leads CSV (company-centric, from scraper.py), searches Apollo.io
for contacts at each company whose titles match the ICP, and writes one row
per contact to data/enriched_leads_{timestamp}.csv.

Usage:
    python agents/enricher.py                            # auto-picks latest raw_leads_*.csv
    python agents/enricher.py --input raw_leads_X.csv   # specific file in /data
    python agents/enricher.py --per-company 25           # max contacts per company (default: 10)
    python agents/enricher.py --dry-run                  # log Apollo calls, no API requests
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

APOLLO_SEARCH_URL = "https://api.apollo.io/v1/mixed_people/search"

# Apollo email statuses that indicate a sendable address
VALID_EMAIL_STATUSES = {"verified", "likely to engage"}

# Output columns — compatible with qualifier.py
FIELDNAMES = [
    # identity
    "full_name",
    "first_name",
    "last_name",
    "linkedin_url",
    # profile
    "job_title",
    "headline",
    "location",
    # company
    "company",
    "company_linkedin_url",
    "company_size",
    "company_industry",
    "company_type",
    # contact
    "email",
    "email_valid",
    "email_status",
    # pipeline metadata
    "industry_searched",
    "title_searched",
    "source",
    "scraped_at",
    "enriched",
    "enriched_at",
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

def load_icp(filename: str = "icp_futuri.json") -> dict:
    path = CONFIG_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"ICP profile not found: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------

def find_latest_raw_file() -> Path:
    candidates = sorted(DATA_DIR.glob("raw_leads_*.csv"), reverse=True)
    if not candidates:
        raise FileNotFoundError("No raw_leads_*.csv found in /data. Run scraper.py first.")
    return candidates[0]


def load_raw_leads(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# Apollo.io
# ---------------------------------------------------------------------------

def _apollo_post(
    payload: dict,
    api_key: str,
    logger: logging.Logger,
    retries: int = 2,
) -> dict | None:
    headers = {"Content-Type": "application/json", "Cache-Control": "no-cache", "X-Api-Key": api_key}

    for attempt in range(retries + 1):
        try:
            resp = requests.post(APOLLO_SEARCH_URL, json=payload, headers=headers, timeout=20)
        except requests.RequestException as e:
            logger.warning(f"  Request error (attempt {attempt + 1}): {e}")
            time.sleep(5)
            continue

        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            wait = 30 * (attempt + 1)
            logger.warning(f"  Apollo rate limited — sleeping {wait}s")
            time.sleep(wait)
            continue
        logger.warning(f"  Apollo {resp.status_code}: {resp.text[:200]}")
        return None

    return None


def search_apollo(
    domain: str,
    titles: list[str],
    api_key: str,
    logger: logging.Logger,
    per_page: int = 10,
) -> list[dict]:
    """Search Apollo for people at a domain with matching titles. Returns raw person dicts."""
    payload = {
        "q_organization_domains": [domain],
        "person_titles": titles,
        "per_page": per_page,
        "page": 1,
    }
    data = _apollo_post(payload, api_key, logger)
    if not data:
        return []
    return data.get("people", [])


# ---------------------------------------------------------------------------
# Contact mapping
# ---------------------------------------------------------------------------

def format_location(person: dict) -> str:
    return ", ".join(filter(None, [
        person.get("city", ""),
        person.get("state", ""),
        person.get("country", ""),
    ]))


def map_person(person: dict, company_row: dict) -> dict:
    """Map an Apollo person object to our enriched FIELDNAMES schema."""
    org = person.get("organization") or {}
    email = (person.get("email") or "").strip()
    email_status = (person.get("email_status") or "").strip().lower()
    size_raw = org.get("estimated_num_employees") or ""

    return {
        "full_name": person.get("name", ""),
        "first_name": person.get("first_name", ""),
        "last_name": person.get("last_name", ""),
        "linkedin_url": person.get("linkedin_url") or "",
        "job_title": person.get("title") or "",
        "headline": person.get("headline") or person.get("title") or "",
        "location": format_location(person),
        "company": org.get("name") or company_row.get("company", ""),
        "company_linkedin_url": org.get("linkedin_url") or "",
        "company_size": str(size_raw) if size_raw else "",
        "company_industry": org.get("industry") or company_row.get("industry_searched", ""),
        "company_type": org.get("type") or "",
        "email": email,
        "email_valid": str(email_status in VALID_EMAIL_STATUSES).lower() if email else "false",
        "email_status": email_status,
        "industry_searched": company_row.get("industry_searched", ""),
        "title_searched": company_row.get("title_searched", ""),
        "source": "apollo",
        "scraped_at": company_row.get("scraped_at", ""),
        "enriched": "true",
        "enriched_at": datetime.now().isoformat(),
        "qualified": "",
    }


# ---------------------------------------------------------------------------
# Enrichment loop
# ---------------------------------------------------------------------------

def enrich_leads(
    companies: list[dict],
    icp: dict,
    logger: logging.Logger,
    dry_run: bool = False,
    per_company: int = 10,
) -> list[dict]:
    api_key = os.getenv("APOLLO_API_KEY", "").strip()
    if not api_key and not dry_run:
        raise EnvironmentError("APOLLO_API_KEY is not set in .env")

    titles = icp.get("target_titles", [])
    contacts: list[dict] = []
    seen_ids: set[str] = set()
    processed_domains: set[str] = set()
    total = len(companies)

    for i, row in enumerate(companies, 1):
        domain = (row.get("domain") or "").strip()
        company = row.get("company", "unknown")

        logger.info(f"[{i}/{total}] {company} ({domain or 'no domain'})")

        if not domain:
            logger.warning("  No domain — skipping")
            continue

        # Skip duplicate domains within this run
        if domain in processed_domains:
            logger.info("  Already processed this domain — skipping")
            continue
        processed_domains.add(domain)

        # Skip rows already marked enriched in the input (manual override / re-run guard)
        if row.get("enriched", "").lower() == "true":
            logger.info("  Marked enriched in input — skipping")
            continue

        if dry_run:
            logger.info(f"  [DRY RUN] Would search Apollo: domain={domain!r}, titles={titles}")
            continue

        people = search_apollo(domain, titles, api_key, logger, per_page=per_company)

        if not people:
            logger.info("  No contacts found")
            time.sleep(0.5)
            continue

        added = 0
        for person in people:
            # Deduplicate by Apollo person ID, falling back to email
            dedup_key = person.get("id") or (person.get("email") or "").lower()
            if dedup_key and dedup_key in seen_ids:
                continue
            if dedup_key:
                seen_ids.add(dedup_key)

            contacts.append(map_person(person, row))
            added += 1

        logger.info(f"  {len(people)} people returned, {added} contacts added")
        time.sleep(0.5)  # stay within Apollo rate limits

    return contacts


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def save_enriched(contacts: list[dict], logger: logging.Logger) -> Path | None:
    if not contacts:
        logger.warning("No contacts to save.")
        return None

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = DATA_DIR / f"enriched_leads_{ts}.csv"

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        for row in contacts:
            writer.writerow({k: row.get(k, "") for k in FIELDNAMES})

    logger.info(f"Saved {len(contacts)} contacts → {out_path.relative_to(ROOT)}")
    return out_path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lead enricher — Apollo.io contact search")
    parser.add_argument(
        "--input", default=None,
        help="raw_leads CSV filename (in /data). Defaults to latest.",
    )
    parser.add_argument("--icp", default="icp_futuri.json", help="ICP filename inside config/")
    parser.add_argument(
        "--per-company", type=int, default=10,
        help="Max contacts to retrieve per company from Apollo (default: 10)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Log Apollo calls without making them")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logger = setup_logger("enricher")
    logger.info("=" * 60)
    logger.info("Enricher started")

    try:
        icp = load_icp(args.icp)
        logger.info(f"Client: {icp['client']} — Product: {icp['product']}")

        if args.input:
            input_path = DATA_DIR / args.input
            if not input_path.exists():
                raise FileNotFoundError(f"Input file not found: {input_path}")
        else:
            input_path = find_latest_raw_file()

        logger.info(f"Input:        {input_path.relative_to(ROOT)}")
        logger.info(f"Titles:       {icp['target_titles']}")
        logger.info(f"per_company:  {args.per_company}")
        logger.info(f"dry_run:      {args.dry_run}")

        companies = load_raw_leads(input_path)
        logger.info(f"Loaded {len(companies)} companies to enrich")

        contacts = enrich_leads(
            companies,
            icp,
            logger,
            dry_run=args.dry_run,
            per_company=args.per_company,
        )

        out_path = save_enriched(contacts, logger)
        if out_path:
            valid_emails = sum(1 for c in contacts if c.get("email_valid") == "true")
            logger.info(f"Contacts with valid email: {valid_emails}/{len(contacts)}")
            logger.info(f"Run complete. Next step: python agents/qualifier.py --input {out_path.name}")

    except (FileNotFoundError, EnvironmentError) as e:
        logger.error(str(e))
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
