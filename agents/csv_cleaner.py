"""
csv_cleaner.py

Reads data/futuri_seamless_batch1_clean.csv and outputs data/futuri_ready.csv
with a normalised column set. Skips rows with no email address.

Usage:
    python agents/csv_cleaner.py
    python agents/csv_cleaner.py --input futuri_seamless_batch2_clean.csv
    python agents/csv_cleaner.py --output futuri_ready_v2.csv
"""

import argparse
import csv
import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = ROOT / "data"
LOG_DIR  = ROOT / "logs"

DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# Input column → output column mapping
COLUMN_MAP = {
    "First Name":               "first_name",
    "Last Name":                "last_name",
    "Title":                    "title",
    "Company Name - Cleaned":   "company",
    "Company Website Domain":   "domain",
    "Email 1":                  "contact_email",
    "Contact LI Profile URL":   "linkedin_url",
    "Company Industry":         "company_industry",
    "Company Staff Count":      "company_size",
}

OUTPUT_FIELDS = [
    "first_name",
    "last_name",
    "title",
    "company",
    "domain",
    "contact_email",
    "linkedin_url",
    "company_industry",
    "company_size",
    "location",
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
# Clean
# ---------------------------------------------------------------------------

def build_location(row: dict) -> str:
    city  = row.get("Contact City", "").strip()
    state = row.get("Contact State", "").strip()
    parts = [p for p in (city, state) if p]
    return ", ".join(parts)


def clean(input_path: Path, output_path: Path, logger: logging.Logger) -> None:
    with open(input_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    logger.info(f"Read {len(rows)} rows from {input_path.relative_to(ROOT)}")

    # Warn about any expected columns that are missing
    if rows:
        headers = set(rows[0].keys())
        for src in list(COLUMN_MAP) + ["Contact City", "Contact State"]:
            if src not in headers:
                logger.warning(f"  Column not found in input: {src!r}")

    kept, skipped = [], 0
    for row in rows:
        email = row.get("Email 1", "").strip()
        if not email:
            skipped += 1
            continue

        out = {dst: row.get(src, "").strip() for src, dst in COLUMN_MAP.items()}
        out["location"] = build_location(row)
        kept.append(out)

    logger.info(f"Kept {len(kept)} rows, skipped {skipped} (no email)")

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(kept)

    logger.info(f"Wrote {len(kept)} rows → {output_path.relative_to(ROOT)}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CSV cleaner — normalise Seamless.AI export")
    parser.add_argument(
        "--input", default="futuri_seamless_batch1_clean.csv",
        help="Input filename inside data/ (default: futuri_seamless_batch1_clean.csv)",
    )
    parser.add_argument(
        "--output", default="futuri_ready.csv",
        help="Output filename inside data/ (default: futuri_ready.csv)",
    )
    return parser.parse_args()


def main() -> None:
    args   = parse_args()
    logger = setup_logger("csv_cleaner")
    logger.info("=" * 60)
    logger.info("CSV cleaner started")

    input_path  = DATA_DIR / args.input
    output_path = DATA_DIR / args.output

    if not input_path.exists():
        logger.error(f"Input file not found: {input_path}")
        sys.exit(1)

    try:
        clean(input_path, output_path, logger)
        logger.info("Done.")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
