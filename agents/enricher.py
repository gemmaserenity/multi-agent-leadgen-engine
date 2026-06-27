"""
enricher.py

Reads a raw_leads CSV, enriches each row via Proxycurl (LinkedIn profile + work
email lookup), validates emails with Debounce, and writes an enriched CSV.

Usage:
    python agents/enricher.py                          # auto-picks latest raw_leads_*.csv
    python agents/enricher.py --input raw_leads_X.csv  # specific file in /data
    python agents/enricher.py --dry-run                # logs what would be enriched, no API calls
    python agents/enricher.py --skip-email             # skip email lookup (saves credits)
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

DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# Proxycurl endpoints
PROXYCURL_PROFILE_URL = "https://nubela.co/proxycurl/api/v2/linkedin"
PROXYCURL_EMAIL_URL = "https://nubela.co/proxycurl/api/linkedin/profile/email"

# Debounce endpoint
DEBOUNCE_URL = "https://api.debounce.io/v1/"

# Output columns (superset of scraper columns + enriched fields)
FIELDNAMES = [
    # identity
    "full_name",
    "first_name",
    "last_name",
    "linkedin_url",
    # confirmed profile data
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
# Proxycurl — profile lookup
# ---------------------------------------------------------------------------

def _proxycurl_get(url: str, params: dict, api_key: str, logger: logging.Logger, retries: int = 2) -> dict | None:
    headers = {"Authorization": f"Bearer {api_key}"}
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
        except requests.RequestException as e:
            logger.warning(f"Request error (attempt {attempt + 1}): {e}")
            time.sleep(3)
            continue

        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            wait = 60 * (attempt + 1)
            logger.warning(f"Rate limited — sleeping {wait}s")
            time.sleep(wait)
            continue
        if resp.status_code == 404:
            return None
        logger.warning(f"Proxycurl {resp.status_code} for {params}: {resp.text[:120]}")
        return None

    return None


def enrich_profile(linkedin_url: str, api_key: str, logger: logging.Logger) -> dict:
    data = _proxycurl_get(
        PROXYCURL_PROFILE_URL,
        {"linkedin_profile_url": linkedin_url, "use_cache": "if-present"},
        api_key,
        logger,
    )

    if not data:
        return {}

    # Pull current experience (first entry is most recent)
    experiences = data.get("experiences") or []
    current_exp = next(
        (e for e in experiences if e.get("ends_at") is None),
        experiences[0] if experiences else {},
    )

    # Company details live in current experience or top-level company
    company_name = (
        current_exp.get("company")
        or data.get("company", "")
    )
    company_linkedin = current_exp.get("company_linkedin_profile_url", "")

    # Proxycurl nests company employee count under company_obj if profile was fetched
    # with extra fields; fall back gracefully
    company_obj = data.get("company_obj") or {}
    company_size_raw = company_obj.get("company_size") or current_exp.get("company_size") or []
    if isinstance(company_size_raw, list) and len(company_size_raw) == 2:
        company_size = f"{company_size_raw[0]}-{company_size_raw[1]}"
    else:
        company_size = str(company_size_raw) if company_size_raw else ""

    location_parts = filter(None, [
        data.get("city"), data.get("state"), data.get("country_full_name")
    ])
    location = ", ".join(location_parts)

    return {
        "first_name": data.get("first_name", ""),
        "last_name": data.get("last_name", ""),
        "job_title": current_exp.get("title") or data.get("occupation", ""),
        "location": location,
        "company": company_name,
        "company_linkedin_url": company_linkedin,
        "company_size": company_size,
        "company_industry": data.get("industry", ""),
        "company_type": company_obj.get("company_type", ""),
    }


# ---------------------------------------------------------------------------
# Proxycurl — work email lookup
# ---------------------------------------------------------------------------

def lookup_email(linkedin_url: str, api_key: str, logger: logging.Logger) -> str:
    data = _proxycurl_get(
        PROXYCURL_EMAIL_URL,
        {"linkedin_profile_url": linkedin_url},
        api_key,
        logger,
    )
    if not data:
        return ""
    return data.get("email", "")


# ---------------------------------------------------------------------------
# Debounce — email validation
# ---------------------------------------------------------------------------

def validate_email(email: str, api_key: str, logger: logging.Logger) -> tuple[bool, str]:
    """Returns (is_valid, status_string)."""
    if not email or not api_key:
        return False, "not_checked"

    try:
        resp = requests.get(
            DEBOUNCE_URL,
            params={"api": api_key, "email": email},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning(f"Debounce {resp.status_code} for {email}")
            return False, "error"

        result = resp.json()
        debounce = result.get("debounce", {})
        code = debounce.get("code", "")
        send_transactional = debounce.get("send_transactional", "0")
        is_valid = send_transactional == "1"
        return is_valid, code

    except requests.RequestException as e:
        logger.warning(f"Debounce request error for {email}: {e}")
        return False, "error"


# ---------------------------------------------------------------------------
# Enrichment loop
# ---------------------------------------------------------------------------

def enrich_leads(
    leads: list[dict],
    logger: logging.Logger,
    dry_run: bool = False,
    skip_email: bool = False,
) -> list[dict]:
    proxycurl_key = os.getenv("PROXYCURL_API_KEY", "").strip()
    debounce_key = os.getenv("DEBOUNCE_API_KEY", "").strip()

    if not proxycurl_key and not dry_run:
        raise EnvironmentError("PROXYCURL_API_KEY is not set in .env")

    enriched = []
    total = len(leads)

    for i, row in enumerate(leads, 1):
        linkedin_url = row.get("linkedin_url", "").strip()
        name = row.get("full_name", "unknown")

        logger.info(f"[{i}/{total}] {name} — {linkedin_url or '(no URL)'}")

        if not linkedin_url:
            logger.warning(f"  Skipping — no LinkedIn URL")
            enriched.append({**row, "enriched": "false", "enriched_at": ""})
            continue

        if row.get("enriched", "").lower() == "true":
            logger.info(f"  Already enriched — skipping")
            enriched.append(row)
            continue

        if dry_run:
            logger.info(f"  [DRY RUN] Would call Proxycurl profile + email lookup")
            enriched.append({**row, "enriched": "false", "enriched_at": ""})
            continue

        # --- Profile ---
        profile_data = enrich_profile(linkedin_url, proxycurl_key, logger)
        if profile_data:
            logger.info(f"  Profile: {profile_data.get('job_title')} @ {profile_data.get('company')}")
        else:
            logger.warning(f"  Profile lookup returned nothing")

        # --- Email ---
        email = row.get("email", "").strip()
        email_valid = False
        email_status = "not_checked"

        if not skip_email:
            if not email:
                email = lookup_email(linkedin_url, proxycurl_key, logger)
                if email:
                    logger.info(f"  Email found: {email}")
                else:
                    logger.info(f"  No work email found")

            if email and debounce_key:
                email_valid, email_status = validate_email(email, debounce_key, logger)
                logger.info(f"  Email valid={email_valid} status={email_status}")

        merged = {
            **row,
            **profile_data,
            "email": email,
            "email_valid": str(email_valid).lower(),
            "email_status": email_status,
            "enriched": "true",
            "enriched_at": datetime.now().isoformat(),
        }
        enriched.append(merged)

        # Proxycurl recommends >=1s between requests
        time.sleep(1.5)

    return enriched


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def save_enriched(leads: list[dict], logger: logging.Logger) -> Path | None:
    if not leads:
        logger.warning("No leads to save.")
        return None

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = DATA_DIR / f"enriched_leads_{ts}.csv"

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        for row in leads:
            # Fill any missing FIELDNAMES keys with empty string
            writer.writerow({k: row.get(k, "") for k in FIELDNAMES})

    logger.info(f"Saved {len(leads)} leads → {out_path.relative_to(ROOT)}")
    return out_path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lead enricher — Proxycurl + Debounce")
    parser.add_argument("--input", default=None, help="raw_leads CSV filename (in /data). Defaults to latest.")
    parser.add_argument("--dry-run", action="store_true", help="Log actions without hitting APIs")
    parser.add_argument("--skip-email", action="store_true", help="Skip email lookup and validation")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logger = setup_logger("enricher")
    logger.info("=" * 60)
    logger.info("Enricher started")

    try:
        if args.input:
            input_path = DATA_DIR / args.input
            if not input_path.exists():
                raise FileNotFoundError(f"Input file not found: {input_path}")
        else:
            input_path = find_latest_raw_file()

        logger.info(f"Input:      {input_path.relative_to(ROOT)}")
        logger.info(f"dry_run:    {args.dry_run}")
        logger.info(f"skip_email: {args.skip_email}")

        raw_leads = load_raw_leads(input_path)
        already_done = sum(1 for r in raw_leads if r.get("enriched", "").lower() == "true")
        logger.info(f"Loaded {len(raw_leads)} leads ({already_done} already enriched, {len(raw_leads) - already_done} to process)")

        enriched = enrich_leads(raw_leads, logger, dry_run=args.dry_run, skip_email=args.skip_email)

        out_path = save_enriched(enriched, logger)
        if out_path:
            valid_emails = sum(1 for r in enriched if r.get("email_valid") == "true")
            logger.info(f"Leads with valid email: {valid_emails}/{len(enriched)}")
            logger.info(f"Run complete. Next step: python agents/qualifier.py --input {out_path.name}")

    except (FileNotFoundError, EnvironmentError) as e:
        logger.error(str(e))
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
