"""
booker.py

Reads an outreach_leads CSV, generates personalized Cal.com booking links
for each emailed lead, then polls the Cal.com API to detect who has booked.
Writes a booked_leads CSV with booking status for every lead.

Required .env additions:
    CAL_COM_API_KEY=your-cal-api-key
    CAL_COM_USERNAME=your-cal-username        # e.g. "alexsmith"
    CAL_COM_EVENT_SLUG=15min-discovery        # event type URL slug

Usage:
    python agents/booker.py                                    # auto-picks latest outreach_leads_*.csv
    python agents/booker.py --input outreach_leads_X.csv      # specific file in /data
    python agents/booker.py --dry-run                         # generate links, skip Cal.com API call
    python agents/booker.py --since-days 60                   # look back N days for bookings (default 90)
"""

import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
CONFIG_DIR = ROOT / "config"

DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

CAL_BASE_URL = "https://api.cal.com/v1"

# All outreach_leads columns + booking-specific additions
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
    # qualification
    "qualified",
    "score",
    "disqualify_reason",
    "qualified_at",
    # outreach
    "email_sent",
    "email_sent_at",
    "email_subject",
    "email_body",
    # booking
    "cal_link",
    "booked",
    "booked_at",
    "booking_id",
    "booking_status",
    "meeting_time",
    # pipeline metadata
    "industry_searched",
    "title_searched",
    "source",
    "scraped_at",
    "enriched",
    "enriched_at",
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

def find_latest_outreach_file() -> Path:
    candidates = sorted(DATA_DIR.glob("outreach_leads_*.csv"), reverse=True)
    if not candidates:
        raise FileNotFoundError("No outreach_leads_*.csv found in /data. Run outreach.py first.")
    return candidates[0]


def load_outreach_leads(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# Cal.com booking link generator
# ---------------------------------------------------------------------------

def make_cal_link(username: str, event_slug: str, name: str, email: str) -> str:
    """Build a pre-filled Cal.com booking URL for a specific attendee."""
    params = urlencode({"name": name, "email": email})
    return f"https://cal.com/{username}/{event_slug}?{params}"


# ---------------------------------------------------------------------------
# Cal.com API — fetch bookings
# ---------------------------------------------------------------------------

def _cal_get(endpoint: str, api_key: str, params: dict, logger: logging.Logger) -> dict | None:
    """Single Cal.com GET call with basic retry on rate limit."""
    url = f"{CAL_BASE_URL}/{endpoint.lstrip('/')}"
    all_params = {"apiKey": api_key, **params}

    for attempt in range(3):
        try:
            resp = requests.get(url, params=all_params, timeout=30)
        except requests.RequestException as e:
            logger.warning(f"Cal.com request error (attempt {attempt + 1}): {e}")
            time.sleep(5)
            continue

        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            wait = 30 * (attempt + 1)
            logger.warning(f"Cal.com rate limited — sleeping {wait}s")
            time.sleep(wait)
            continue
        logger.warning(f"Cal.com {resp.status_code} for {endpoint}: {resp.text[:200]}")
        return None

    return None


def fetch_cal_bookings(
    api_key: str,
    since_days: int,
    logger: logging.Logger,
) -> dict[str, dict]:
    """
    Fetch all Cal.com bookings created within the last `since_days` days.
    Returns {attendee_email_lowercase: booking_dict}.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    bookings_by_email: dict[str, dict] = {}
    page = 0
    page_size = 100

    logger.info(f"Fetching Cal.com bookings since {cutoff.date()} (last {since_days} days)…")

    while True:
        data = _cal_get(
            "/bookings",
            api_key,
            {"take": page_size, "skip": page * page_size},
            logger,
        )
        if data is None:
            break

        batch = data.get("bookings", [])
        if not batch:
            break

        added = 0
        for b in batch:
            start_str = b.get("startTime", "")
            try:
                start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
            if start_dt < cutoff:
                # Cal.com returns newest first; once we hit old bookings we can stop
                return bookings_by_email

            for attendee in b.get("attendees", []):
                email = attendee.get("email", "").strip().lower()
                if email and email not in bookings_by_email:
                    bookings_by_email[email] = b
                    added += 1

        logger.info(f"  Page {page + 1}: {len(batch)} bookings, {added} new attendee emails indexed")

        if len(batch) < page_size:
            break
        page += 1

    return bookings_by_email


# ---------------------------------------------------------------------------
# Booker loop
# ---------------------------------------------------------------------------

def run_booker(
    leads: list[dict],
    cal_username: str,
    cal_event_slug: str,
    cal_bookings: dict[str, dict],
    logger: logging.Logger,
    dry_run: bool = False,
) -> list[dict]:
    results = []
    booked_count = 0
    new_bookings = 0

    for i, row in enumerate(leads, 1):
        name = row.get("full_name", "unknown")
        email = row.get("email", "").strip()
        email_lower = email.lower()

        logger.info(f"[{i}/{len(leads)}] {name} — {email or '(no email)'}")

        # Idempotency: skip rows already confirmed booked
        if row.get("booked", "").lower() == "true":
            logger.info(f"  Already booked — skipping")
            booked_count += 1
            results.append(row)
            continue

        # Only process leads we actually emailed
        if row.get("email_sent", "").lower() != "true":
            logger.info(f"  Not yet emailed — skipping")
            results.append({
                **row,
                "cal_link": "",
                "booked": "false",
                "booked_at": "",
                "booking_id": "",
                "booking_status": "",
                "meeting_time": "",
            })
            continue

        if not email:
            logger.info(f"  No email address — skipping")
            results.append({
                **row,
                "cal_link": "",
                "booked": "false",
                "booked_at": "",
                "booking_id": "",
                "booking_status": "",
                "meeting_time": "",
            })
            continue

        # Generate pre-filled Cal.com link
        cal_link = make_cal_link(cal_username, cal_event_slug, name, email)

        if dry_run:
            logger.info(f"  [DRY RUN] Cal link: {cal_link}")
            results.append({
                **row,
                "cal_link": cal_link,
                "booked": "false",
                "booked_at": "",
                "booking_id": "",
                "booking_status": "",
                "meeting_time": "",
            })
            continue

        # Check Cal.com for a booking from this attendee
        booking = cal_bookings.get(email_lower)
        if booking:
            status = booking.get("status", "").upper()
            is_confirmed = status in ("ACCEPTED", "CONFIRMED", "")
            if is_confirmed:
                booked_count += 1
                new_bookings += 1
                logger.info(
                    f"  BOOKED — meeting at {booking.get('startTime', 'unknown time')} "
                    f"(booking #{booking.get('id')})"
                )
            else:
                logger.info(f"  Booking found but status={status!r} — not marking as booked")

            results.append({
                **row,
                "cal_link": cal_link,
                "booked": "true" if is_confirmed else "false",
                "booked_at": datetime.now().isoformat() if is_confirmed else "",
                "booking_id": str(booking.get("id", "")),
                "booking_status": status,
                "meeting_time": booking.get("startTime", ""),
            })
        else:
            logger.info(f"  No booking found")
            results.append({
                **row,
                "cal_link": cal_link,
                "booked": "false",
                "booked_at": "",
                "booking_id": "",
                "booking_status": "",
                "meeting_time": "",
            })

    logger.info(f"Total booked: {booked_count} | New this run: {new_bookings}")
    return results


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def save_booked(leads: list[dict], logger: logging.Logger) -> Path | None:
    if not leads:
        logger.warning("No leads to save.")
        return None

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = DATA_DIR / f"booked_leads_{ts}.csv"

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        for row in leads:
            writer.writerow({k: row.get(k, "") for k in FIELDNAMES})

    logger.info(f"Saved {len(leads)} leads → {out_path.relative_to(ROOT)}")
    return out_path


def print_booking_summary(leads: list[dict], logger: logging.Logger) -> None:
    emailed = [r for r in leads if r.get("email_sent", "").lower() == "true"]
    booked = [r for r in leads if r.get("booked", "").lower() == "true"]
    pending = [r for r in emailed if r.get("booked", "").lower() != "true"]

    logger.info("=" * 60)
    logger.info("PIPELINE SUMMARY")
    logger.info(f"  Total leads:     {len(leads)}")
    logger.info(f"  Emailed:         {len(emailed)}")
    logger.info(f"  Booked:          {len(booked)}")
    if emailed:
        rate = 100 * len(booked) // len(emailed)
        logger.info(f"  Booking rate:    {rate}%")
    logger.info(f"  Still pending:   {len(pending)}")
    if booked:
        logger.info("Booked leads:")
        for r in booked:
            logger.info(
                f"  {r.get('full_name')} ({r.get('email')}) — "
                f"{r.get('meeting_time', 'time TBD')}"
            )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lead booker — Cal.com booking tracker")
    parser.add_argument(
        "--input", default=None,
        help="outreach_leads CSV filename (in /data). Defaults to latest.",
    )
    parser.add_argument("--icp", default="icp_futuri.json", help="ICP filename inside config/")
    parser.add_argument(
        "--since-days", type=int, default=90,
        help="How many days back to check Cal.com for bookings (default: 90)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Generate Cal.com links but skip the booking API call",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logger = setup_logger("booker")
    logger.info("=" * 60)
    logger.info("Booker started")

    try:
        icp = load_icp(args.icp)
        logger.info(f"Client: {icp['client']} — Product: {icp['product']}")

        # Cal.com config
        cal_api_key = os.getenv("CAL_COM_API_KEY", "").strip()
        cal_username = os.getenv("CAL_COM_USERNAME", "").strip()
        cal_event_slug = os.getenv("CAL_COM_EVENT_SLUG", "").strip()

        if not args.dry_run:
            missing = [k for k, v in {
                "CAL_COM_API_KEY": cal_api_key,
                "CAL_COM_USERNAME": cal_username,
                "CAL_COM_EVENT_SLUG": cal_event_slug,
            }.items() if not v]
            if missing:
                raise EnvironmentError(
                    f"Missing Cal.com env vars: {', '.join(missing)}. "
                    "Add them to .env (see usage comment at top of booker.py)."
                )
        else:
            # Dry run still needs username/slug to build links
            if not cal_username or not cal_event_slug:
                logger.warning(
                    "CAL_COM_USERNAME or CAL_COM_EVENT_SLUG not set — "
                    "booking links will be placeholder URLs"
                )
                cal_username = cal_username or "your-username"
                cal_event_slug = cal_event_slug or "15min-discovery"

        if args.input:
            input_path = DATA_DIR / args.input
            if not input_path.exists():
                raise FileNotFoundError(f"Input file not found: {input_path}")
        else:
            input_path = find_latest_outreach_file()

        logger.info(f"Input:       {input_path.relative_to(ROOT)}")
        logger.info(f"Cal.com:     cal.com/{cal_username}/{cal_event_slug}")
        logger.info(f"since_days:  {args.since_days}")
        logger.info(f"dry_run:     {args.dry_run}")

        raw_leads = load_outreach_leads(input_path)
        emailed = sum(1 for r in raw_leads if r.get("email_sent", "").lower() == "true")
        already_booked = sum(1 for r in raw_leads if r.get("booked", "").lower() == "true")
        logger.info(
            f"Loaded {len(raw_leads)} leads "
            f"({emailed} emailed, {already_booked} already booked)"
        )

        # Fetch Cal.com bookings once, then cross-reference per lead
        cal_bookings: dict[str, dict] = {}
        if not args.dry_run and cal_api_key:
            cal_bookings = fetch_cal_bookings(cal_api_key, args.since_days, logger)
            logger.info(f"Indexed {len(cal_bookings)} unique attendee emails from Cal.com")

        booked_leads = run_booker(
            raw_leads,
            cal_username,
            cal_event_slug,
            cal_bookings,
            logger,
            dry_run=args.dry_run,
        )

        out_path = save_booked(booked_leads, logger)
        if out_path:
            print_booking_summary(booked_leads, logger)
            logger.info(f"Run complete. Pipeline finished → {out_path.name}")

    except (FileNotFoundError, EnvironmentError) as e:
        logger.error(str(e))
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
