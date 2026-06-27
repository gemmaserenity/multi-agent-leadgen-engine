"""
outreach.py

Reads a qualified_leads CSV, generates personalized cold emails via Claude,
and sends them via SMTP. Only processes leads where qualified=true
and email_valid=true (override with --force-send).

Required .env additions:
    SMTP_HOST=smtp.gmail.com        # or smtp.sendgrid.net, etc.
    SMTP_PORT=587                   # 587 for STARTTLS, 465 for SSL
    SMTP_USER=you@yourdomain.com
    SMTP_PASS=your-app-password
    SMTP_FROM=Your Name <you@yourdomain.com>   # optional display name

Usage:
    python agents/outreach.py                                   # auto-picks latest qualified_leads_*.csv
    python agents/outreach.py --input qualified_leads_X.csv    # specific file in /data
    python agents/outreach.py --dry-run                        # generate emails, log them, don't send
    python agents/outreach.py --force-send                     # send even if email_valid != true
    python agents/outreach.py --limit 20                       # cap emails per run (domain warmup)
    python agents/outreach.py --model claude-haiku-4-5         # cheaper model for large batches
"""

import argparse
import csv
import logging
import json
import os
import smtplib
import sys
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
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

# All qualified_leads columns + outreach-specific additions
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

def find_latest_qualified_file() -> Path:
    candidates = sorted(DATA_DIR.glob("qualified_leads_*.csv"), reverse=True)
    if not candidates:
        raise FileNotFoundError("No qualified_leads_*.csv found in /data. Run qualifier.py first.")
    return candidates[0]


def load_qualified_leads(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# Email generation via Claude
# ---------------------------------------------------------------------------

class OutreachEmail(BaseModel):
    subject: str
    body: str   # plain text, ~100–150 words, no markdown, no bullet points


def generate_email(
    lead: dict,
    icp: dict,
    client: anthropic.Anthropic,
    model: str,
    logger: logging.Logger,
) -> OutreachEmail | None:
    """Generate a personalized cold email for the lead. Returns None on failure."""

    first_name = lead.get("first_name") or lead.get("full_name", "").split()[0] or "there"
    title = lead.get("job_title") or lead.get("headline") or "sales leader"
    company = lead.get("company") or "your company"
    industry = lead.get("company_industry") or ""
    size = lead.get("company_size") or ""

    product = icp.get("product", "TopLine Enterprise")
    pain_point = icp.get("pain_point", "sales teams stuck in admin tools, not enough time selling")
    sender_context = (
        "I'm reaching out from GNS on behalf of Futuri's TopLine Enterprise team. "
        "TopLine Enterprise helps sales organizations eliminate admin overhead so reps "
        "spend more time actually selling."
    )

    size_context = f", a {size}-person company" if size else ""
    industry_context = f" in {industry}" if industry else ""

    prompt = f"""Write a short, personalized B2B cold email from a sales development rep.

Context about the sender:
{sender_context}

Prospect details:
- First name: {first_name}
- Title: {title}
- Company: {company}{size_context}{industry_context}
- Pain point to address: {pain_point}

Requirements:
- Subject line: punchy, specific, <8 words, no clickbait
- Body: 3 short paragraphs, ~100–130 words total, plain text only
  - Para 1: one sentence that shows you know their world (reference title/company/industry)
  - Para 2: connect that to the pain point and what {product} does about it
  - Para 3: soft CTA — ask for a 15-minute call to explore if it's a fit
- Sign off with: "Best, Alex"
- No markdown, no bullet points, no ALL CAPS, no exclamation marks
- Sound human, not templated — avoid phrases like "I hope this finds you well"

Return only the email subject and body."""

    try:
        response = client.messages.parse(
            model=model,
            max_tokens=1024,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": prompt}],
            output_format=OutreachEmail,
        )
        return response.parsed_output

    except Exception as e:
        logger.warning(f"  Claude error generating email: {e}")
        return None


# ---------------------------------------------------------------------------
# SMTP sending
# ---------------------------------------------------------------------------

class SMTPConfig:
    def __init__(self) -> None:
        self.host = os.getenv("SMTP_HOST", "").strip()
        self.port = int(os.getenv("SMTP_PORT", "587"))
        self.user = os.getenv("SMTP_USER", "").strip()
        self.password = os.getenv("SMTP_PASS", "").strip()
        self.from_addr = os.getenv("SMTP_FROM", self.user).strip()

    def validate(self) -> None:
        missing = [k for k, v in {
            "SMTP_HOST": self.host,
            "SMTP_USER": self.user,
            "SMTP_PASS": self.password,
        }.items() if not v]
        if missing:
            raise EnvironmentError(
                f"Missing SMTP env vars: {', '.join(missing)}. "
                "Add them to .env (see usage comment at top of outreach.py)."
            )


def send_email(
    cfg: SMTPConfig,
    to_addr: str,
    subject: str,
    body: str,
    logger: logging.Logger,
) -> bool:
    """Send a plain-text email. Returns True on success."""
    msg = MIMEMultipart("alternative")
    msg["From"] = cfg.from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        if cfg.port == 465:
            with smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=30) as server:
                server.login(cfg.user, cfg.password)
                server.sendmail(cfg.from_addr, to_addr, msg.as_string())
        else:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.login(cfg.user, cfg.password)
                server.sendmail(cfg.from_addr, to_addr, msg.as_string())
        return True

    except smtplib.SMTPRecipientsRefused:
        logger.warning(f"  SMTP rejected recipient: {to_addr}")
        return False
    except smtplib.SMTPException as e:
        logger.warning(f"  SMTP error for {to_addr}: {e}")
        return False
    except OSError as e:
        logger.warning(f"  Network error sending to {to_addr}: {e}")
        return False


# ---------------------------------------------------------------------------
# Outreach loop
# ---------------------------------------------------------------------------

def run_outreach(
    leads: list[dict],
    icp: dict,
    logger: logging.Logger,
    dry_run: bool = False,
    force_send: bool = False,
    limit: int | None = None,
    model: str = DEFAULT_MODEL,
) -> list[dict]:
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY is not set in .env")

    smtp_cfg = SMTPConfig()
    if not dry_run:
        smtp_cfg.validate()

    client = anthropic.Anthropic(api_key=api_key)

    results = []
    sent_count = 0
    skipped_count = 0

    for i, row in enumerate(leads, 1):
        name = row.get("full_name", "unknown")
        email = row.get("email", "").strip()
        email_valid = row.get("email_valid", "").lower()
        qualified = row.get("qualified", "").lower()

        logger.info(f"[{i}/{len(leads)}] {name} — {email or '(no email)'}")

        # Skip already-sent
        if row.get("email_sent", "").lower() == "true":
            logger.info(f"  Already sent — skipping")
            results.append(row)
            skipped_count += 1
            continue

        # Skip non-qualified
        if qualified != "true":
            logger.info(f"  Not qualified (qualified={qualified!r}) — skipping")
            results.append({
                **row,
                "email_sent": "false",
                "email_sent_at": "",
                "email_subject": "",
                "email_body": "",
            })
            skipped_count += 1
            continue

        # Skip invalid email unless --force-send
        if not email:
            logger.info(f"  No email address — skipping")
            results.append({
                **row,
                "email_sent": "false",
                "email_sent_at": "",
                "email_subject": "",
                "email_body": "",
            })
            skipped_count += 1
            continue

        if email_valid != "true" and not force_send:
            logger.info(f"  email_valid={email_valid!r} — skipping (use --force-send to override)")
            results.append({
                **row,
                "email_sent": "false",
                "email_sent_at": "",
                "email_subject": "",
                "email_body": "",
            })
            skipped_count += 1
            continue

        # Honour --limit
        if limit is not None and sent_count >= limit:
            logger.info(f"  Limit of {limit} reached — skipping remaining")
            results.append({
                **row,
                "email_sent": "false",
                "email_sent_at": "",
                "email_subject": "",
                "email_body": "",
            })
            continue

        # Generate email
        outreach = generate_email(row, icp, client, model, logger)
        if not outreach:
            logger.warning(f"  Email generation failed — skipping")
            results.append({
                **row,
                "email_sent": "false",
                "email_sent_at": "",
                "email_subject": "",
                "email_body": "",
            })
            continue

        logger.info(f"  Subject: {outreach.subject}")
        logger.info(f"  Body preview: {outreach.body[:120].replace(chr(10), ' ')}…")

        if dry_run:
            logger.info(f"  [DRY RUN] Would send to {email}")
            results.append({
                **row,
                "email_sent": "false",
                "email_sent_at": "",
                "email_subject": outreach.subject,
                "email_body": outreach.body,
            })
            sent_count += 1  # count generated for dry-run reporting
            time.sleep(0.3)
            continue

        # Send
        success = send_email(smtp_cfg, email, outreach.subject, outreach.body, logger)
        if success:
            logger.info(f"  Sent to {email}")
            sent_count += 1
            results.append({
                **row,
                "email_sent": "true",
                "email_sent_at": datetime.now().isoformat(),
                "email_subject": outreach.subject,
                "email_body": outreach.body,
            })
        else:
            results.append({
                **row,
                "email_sent": "false",
                "email_sent_at": "",
                "email_subject": outreach.subject,
                "email_body": outreach.body,
            })

        # ~3s between sends to avoid triggering spam filters
        time.sleep(3)

    verb = "generated" if dry_run else "sent"
    logger.info(f"Emails {verb}: {sent_count} | Skipped: {skipped_count}")
    return results


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def save_outreach(leads: list[dict], logger: logging.Logger) -> Path | None:
    if not leads:
        logger.warning("No leads to save.")
        return None

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = DATA_DIR / f"outreach_leads_{ts}.csv"

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
    parser = argparse.ArgumentParser(description="Lead outreach — AI email generation + SMTP send")
    parser.add_argument(
        "--input", default=None,
        help="qualified_leads CSV filename (in /data). Defaults to latest.",
    )
    parser.add_argument("--icp", default="icp_futuri.json", help="ICP filename inside config/")
    parser.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"Claude model to use (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Generate emails and log them but do not send",
    )
    parser.add_argument(
        "--force-send", action="store_true",
        help="Send even if email_valid != true",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Maximum emails to send this run (for domain warmup)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logger = setup_logger("outreach")
    logger.info("=" * 60)
    logger.info("Outreach started")

    try:
        icp = load_icp(args.icp)
        logger.info(f"Client: {icp['client']} — Product: {icp['product']}")

        if args.input:
            input_path = DATA_DIR / args.input
            if not input_path.exists():
                raise FileNotFoundError(f"Input file not found: {input_path}")
        else:
            input_path = find_latest_qualified_file()

        logger.info(f"Input:       {input_path.relative_to(ROOT)}")
        logger.info(f"Model:       {args.model}")
        logger.info(f"dry_run:     {args.dry_run}")
        logger.info(f"force_send:  {args.force_send}")
        logger.info(f"limit:       {args.limit if args.limit is not None else 'unlimited'}")

        raw_leads = load_qualified_leads(input_path)
        already_sent = sum(1 for r in raw_leads if r.get("email_sent", "").lower() == "true")
        eligible = sum(
            1 for r in raw_leads
            if r.get("qualified", "").lower() == "true"
            and r.get("email_sent", "").lower() != "true"
            and r.get("email", "")
            and (r.get("email_valid", "").lower() == "true" or args.force_send)
        )
        logger.info(
            f"Loaded {len(raw_leads)} leads "
            f"({already_sent} already sent, {eligible} eligible to send)"
        )

        outreach_leads = run_outreach(
            raw_leads,
            icp,
            logger,
            dry_run=args.dry_run,
            force_send=args.force_send,
            limit=args.limit,
            model=args.model,
        )

        out_path = save_outreach(outreach_leads, logger)
        if out_path:
            logger.info(f"Run complete. Next step: python agents/booker.py --input {out_path.name}")

    except (FileNotFoundError, EnvironmentError) as e:
        logger.error(str(e))
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
