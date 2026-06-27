"""
qualifier.py

Reads an enriched_leads CSV, scores each lead against the ICP profile
using rule-based filters + Claude AI, and writes a qualified_leads CSV.

Usage:
    python agents/qualifier.py                                   # auto-picks latest enriched_leads_*.csv
    python agents/qualifier.py --input enriched_leads_X.csv     # specific file in /data
    python agents/qualifier.py --dry-run                        # log actions, no API calls
    python agents/qualifier.py --skip-ai                        # rule-based scoring only, no Claude
    python agents/qualifier.py --model claude-haiku-4-5         # cheaper model for large batches
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

import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
CONFIG_DIR = ROOT / "config"

DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

DEFAULT_MODEL = "claude-opus-4-8"

# Output columns: superset of enriched_leads + qualification fields
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

def find_latest_enriched_file() -> Path:
    candidates = sorted(DATA_DIR.glob("enriched_leads_*.csv"), reverse=True)
    if not candidates:
        raise FileNotFoundError("No enriched_leads_*.csv found in /data. Run enricher.py first.")
    return candidates[0]


def load_enriched_leads(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# Rule-based helpers
# ---------------------------------------------------------------------------

def parse_company_size_min(size_str: str) -> int | None:
    """Parse '100-200' → 100, '1001-5000' → 1001, '' → None."""
    if not size_str:
        return None
    try:
        return int(size_str.split("-")[0].replace(",", "").strip())
    except (ValueError, IndexError):
        return None


def has_disqualifier(lead: dict, disqualifiers: list[str]) -> str | None:
    """Return the first disqualifier phrase found in the lead's text fields, or None."""
    combined = " ".join(filter(None, [
        lead.get("job_title", ""),
        lead.get("headline", ""),
        lead.get("company", ""),
        lead.get("company_industry", ""),
        lead.get("company_type", ""),
    ])).lower()
    for phrase in disqualifiers:
        if phrase.lower() in combined:
            return phrase
    return None


def title_matches_icp(job_title: str, target_titles: list[str]) -> bool:
    """True if any significant word from a target title appears in the job title."""
    if not job_title:
        return False
    title_lower = job_title.lower()
    # Key seniority/role words that indicate a genuine ICP title match
    keywords = {"vp", "vice president", "cro", "chief revenue", "svp", "director", "sales"}
    words_in_title = set(title_lower.split())
    return bool(words_in_title & keywords)


def rule_based_qualify(lead: dict, icp: dict) -> dict | None:
    """
    Fast rule-based pre-filter for obvious disqualifications.
    Returns a result dict on a hard decision, or None if ambiguous (→ AI).
    """
    disq = has_disqualifier(lead, icp.get("disqualifiers", []))
    if disq:
        return {"qualified": False, "score": 0, "reason": f"Disqualifier keyword: '{disq}'"}

    size_min = parse_company_size_min(lead.get("company_size", ""))
    threshold = icp.get("company_size_min_employees", 100)
    if size_min is not None and size_min < threshold:
        return {
            "qualified": False,
            "score": 5,
            "reason": f"Company too small: {lead.get('company_size')} (min {threshold} employees)",
        }

    return None  # ambiguous — let Claude decide


# ---------------------------------------------------------------------------
# AI qualification via Claude
# ---------------------------------------------------------------------------

class QualificationResult(BaseModel):
    qualified: bool
    score: int   # 0–100
    reason: str  # one sentence


def qualify_with_claude(
    lead: dict,
    icp: dict,
    client: anthropic.Anthropic,
    model: str,
    logger: logging.Logger,
) -> dict:
    """Call Claude to score a lead against the ICP. Returns {qualified, score, reason}."""

    lead_block = "\n".join(filter(None, [
        f"Name:              {lead.get('full_name', 'Unknown')}",
        f"Title:             {lead.get('job_title') or lead.get('headline') or 'Unknown'}",
        f"Headline:          {lead.get('headline', '')}",
        f"Company:           {lead.get('company', 'Unknown')}",
        f"Company Size:      {lead.get('company_size', 'Unknown')}",
        f"Company Industry:  {lead.get('company_industry', 'Unknown')}",
        f"Company Type:      {lead.get('company_type', '')}",
        f"Location:          {lead.get('location', 'Unknown')}",
        f"Email Valid:       {lead.get('email_valid', 'unknown')}",
    ]))

    icp_block = (
        f"Product:        {icp.get('product', '')}\n"
        f"Target Titles:  {', '.join(icp.get('target_titles', []))}\n"
        f"Industries:     {', '.join(icp.get('target_industries', []))}\n"
        f"Min Employees:  {icp.get('company_size_min_employees', 100)}\n"
        f"Pain Point:     {icp.get('pain_point', '')}\n"
        f"Disqualifiers:  {', '.join(icp.get('disqualifiers', []))}"
    )

    prompt = (
        "You are a B2B sales qualification expert. Score the lead below against our ICP.\n\n"
        f"ICP:\n{icp_block}\n\n"
        f"LEAD:\n{lead_block}\n\n"
        "Score 0–100 on: title fit (ICP role), company size (meets minimum), "
        "industry relevance, absence of disqualifiers. "
        "Mark qualified=true only if score >= 60. "
        "Write reason as one concise sentence."
    )

    try:
        response = client.messages.parse(
            model=model,
            max_tokens=1024,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": prompt}],
            output_format=QualificationResult,
        )
        result = response.parsed_output
        return {
            "qualified": result.qualified,
            "score": max(0, min(100, result.score)),
            "reason": result.reason,
        }

    except Exception as e:
        logger.warning(f"  Claude error: {e} — falling back to title-match")
        title_ok = title_matches_icp(
            lead.get("job_title", "") or lead.get("headline", ""),
            icp.get("target_titles", []),
        )
        return {
            "qualified": title_ok,
            "score": 50 if title_ok else 20,
            "reason": f"AI scoring failed; title-match fallback used ({e})",
        }


# ---------------------------------------------------------------------------
# Qualification loop
# ---------------------------------------------------------------------------

def qualify_leads(
    leads: list[dict],
    icp: dict,
    logger: logging.Logger,
    dry_run: bool = False,
    skip_ai: bool = False,
    model: str = DEFAULT_MODEL,
) -> list[dict]:
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key and not dry_run and not skip_ai:
        raise EnvironmentError("ANTHROPIC_API_KEY is not set in .env")

    client = anthropic.Anthropic(api_key=api_key) if api_key else None

    results = []
    total = len(leads)
    qualified_count = 0

    for i, row in enumerate(leads, 1):
        name = row.get("full_name", "unknown")
        title = row.get("job_title") or row.get("headline") or ""

        logger.info(f"[{i}/{total}] {name} — {title or '(no title)'}")

        # Idempotency: skip rows already decided in a prior run
        if row.get("qualified", "").lower() in ("true", "false"):
            logger.info(f"  Already qualified — skipping")
            if row.get("qualified", "").lower() == "true":
                qualified_count += 1
            results.append(row)
            continue

        if dry_run:
            logger.info(f"  [DRY RUN] Would qualify")
            results.append({
                **row,
                "qualified": "",
                "score": "",
                "disqualify_reason": "",
                "qualified_at": "",
            })
            continue

        # Rule-based pre-filter
        rule_result = rule_based_qualify(row, icp)

        if rule_result:
            qualified = rule_result["qualified"]
            score = rule_result["score"]
            reason = rule_result["reason"]
            logger.info(f"  Rule: {'PASS' if qualified else 'FAIL'} (score={score}) — {reason}")

        elif skip_ai:
            title_ok = title_matches_icp(title, icp.get("target_titles", []))
            qualified = title_ok
            score = 65 if title_ok else 30
            reason = "Title keyword match" if title_ok else "No ICP title match"
            logger.info(f"  Title-only: {'PASS' if qualified else 'FAIL'} (score={score})")

        else:
            ai_result = qualify_with_claude(row, icp, client, model, logger)
            qualified = ai_result["qualified"]
            score = ai_result["score"]
            reason = ai_result["reason"]
            logger.info(f"  AI: {'PASS' if qualified else 'FAIL'} (score={score}) — {reason}")
            time.sleep(0.5)  # light rate limiting between API calls

        if qualified:
            qualified_count += 1

        results.append({
            **row,
            "qualified": str(qualified).lower(),
            "score": score,
            "disqualify_reason": "" if qualified else reason,
            "qualified_at": datetime.now().isoformat(),
        })

    pct = (100 * qualified_count // total) if total else 0
    logger.info(f"Qualified: {qualified_count}/{total} leads ({pct}%)")
    return results


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def save_qualified(leads: list[dict], logger: logging.Logger) -> Path | None:
    if not leads:
        logger.warning("No leads to save.")
        return None

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = DATA_DIR / f"qualified_leads_{ts}.csv"

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        for row in leads:
            writer.writerow({k: row.get(k, "") for k in FIELDNAMES})

    logger.info(f"Saved {len(leads)} leads → {out_path.relative_to(ROOT)}")
    return out_path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lead qualifier — ICP scoring with Claude")
    parser.add_argument(
        "--input", default=None,
        help="enriched_leads CSV filename (in /data). Defaults to latest.",
    )
    parser.add_argument("--icp", default="icp_futuri.json", help="ICP filename inside config/")
    parser.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"Claude model to use (default: {DEFAULT_MODEL})",
    )
    parser.add_argument("--dry-run", action="store_true", help="Log actions without hitting APIs")
    parser.add_argument("--skip-ai", action="store_true", help="Rule-based scoring only, no Claude calls")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logger = setup_logger("qualifier")
    logger.info("=" * 60)
    logger.info("Qualifier started")

    try:
        icp = load_icp(args.icp)
        logger.info(f"Client: {icp['client']} — Product: {icp['product']}")

        if args.input:
            input_path = DATA_DIR / args.input
            if not input_path.exists():
                raise FileNotFoundError(f"Input file not found: {input_path}")
        else:
            input_path = find_latest_enriched_file()

        logger.info(f"Input:    {input_path.relative_to(ROOT)}")
        logger.info(f"Model:    {args.model}")
        logger.info(f"dry_run:  {args.dry_run}")
        logger.info(f"skip_ai:  {args.skip_ai}")

        raw_leads = load_enriched_leads(input_path)
        already_done = sum(
            1 for r in raw_leads if r.get("qualified", "").lower() in ("true", "false")
        )
        logger.info(
            f"Loaded {len(raw_leads)} leads "
            f"({already_done} already qualified, {len(raw_leads) - already_done} to process)"
        )

        qualified_leads = qualify_leads(
            raw_leads,
            icp,
            logger,
            dry_run=args.dry_run,
            skip_ai=args.skip_ai,
            model=args.model,
        )

        out_path = save_qualified(qualified_leads, logger)
        if out_path:
            q_count = sum(1 for r in qualified_leads if r.get("qualified") == "true")
            logger.info(f"Qualified leads: {q_count}/{len(qualified_leads)}")
            logger.info(f"Run complete. Next step: python agents/outreach.py --input {out_path.name}")

    except (FileNotFoundError, EnvironmentError) as e:
        logger.error(str(e))
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
