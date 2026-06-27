# Lead Engine

Multi-agent lead generation engine for GNS. Automates prospecting, enrichment, qualification, and outreach booking.

## Project Structure

```
lead-engine/
├── agents/          # Individual agent scripts (scraper, enricher, qualifier, outreach, booker)
├── config/          # ICP profiles and settings (JSON)
├── data/            # Input CSVs and output/enriched lead files
├── logs/            # Run logs per agent execution
├── outreach/        # Email templates (plain text and HTML)
├── .env             # API keys (never commit this)
├── .env.example     # Template for required environment variables
├── requirements.txt # Python dependencies
└── README.md
```

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   playwright install chromium
   ```

2. Copy `.env.example` to `.env` and fill in your API keys:
   ```
   cp .env.example .env
   ```

3. Add ICP profiles to `config/` (see `icp_futuri.json` as a template).

## ICP Profiles

Each client has a JSON profile in `config/` defining:
- Target industries and job titles
- Company size and contract value minimums
- Core pain point and disqualifiers
- Price per booked meeting

## Pipeline

The five agents run in sequence. Each writes a timestamped CSV to `data/` that the next agent reads.

```
scraper → enricher → qualifier → outreach → booker
```

---

### 1. Scraper

Pulls raw leads from Proxycurl Person Search based on the ICP profile.

```bash
# Run with default ICP (config/icp_futuri.json)
python agents/scraper.py

# Use a different ICP profile
python agents/scraper.py --icp icp_custom.json

# Preview search params without hitting the API
python agents/scraper.py --dry-run
```

Output: `data/raw_leads_YYYYMMDD_HHMMSS.csv`

---

### 2. Enricher

Enriches each lead with full LinkedIn profile data via Proxycurl, finds a work email, and validates it with Debounce.

```bash
# Auto-picks the latest raw_leads_*.csv
python agents/enricher.py

# Specific input file
python agents/enricher.py --input raw_leads_20260627_120000.csv

# Skip email lookup and validation (saves API credits)
python agents/enricher.py --skip-email

# Dry run — no API calls
python agents/enricher.py --dry-run
```

Output: `data/enriched_leads_YYYYMMDD_HHMMSS.csv`

---

### 3. Qualifier

Scores each lead against the ICP using a rule-based pre-filter followed by Claude AI scoring. Adds `qualified`, `score`, and `disqualify_reason` columns.

```bash
# Auto-picks the latest enriched_leads_*.csv
python agents/qualifier.py

# Specific input file
python agents/qualifier.py --input enriched_leads_20260627_120000.csv

# Rule-based scoring only — no Claude API calls
python agents/qualifier.py --skip-ai

# Use a cheaper model for large batches
python agents/qualifier.py --model claude-haiku-4-5

# Dry run
python agents/qualifier.py --dry-run
```

Output: `data/qualified_leads_YYYYMMDD_HHMMSS.csv`

Leads with `score >= 60` are marked `qualified=true`.

---

### 4. Outreach

Generates a personalized cold email for each qualified lead via Claude and sends it via SMTP. Only sends to leads where `qualified=true` and `email_valid=true`.

```bash
# Auto-picks the latest qualified_leads_*.csv
python agents/outreach.py

# Specific input file
python agents/outreach.py --input qualified_leads_20260627_120000.csv

# Generate and log emails without sending
python agents/outreach.py --dry-run

# Send even if email_valid != true
python agents/outreach.py --force-send

# Cap emails per run (useful for domain warmup)
python agents/outreach.py --limit 20

# Use a cheaper model
python agents/outreach.py --model claude-haiku-4-5
```

Output: `data/outreach_leads_YYYYMMDD_HHMMSS.csv`

Requires `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS` in `.env`.

---

### 5. Booker

Generates a pre-filled Cal.com booking link for every emailed lead, then polls the Cal.com API to detect who has booked. Adds `cal_link`, `booked`, `booking_id`, and `meeting_time` columns.

```bash
# Auto-picks the latest outreach_leads_*.csv
python agents/booker.py

# Specific input file
python agents/booker.py --input outreach_leads_20260627_120000.csv

# Look back further for bookings (default: 90 days)
python agents/booker.py --since-days 180

# Generate Cal.com links without calling the API
python agents/booker.py --dry-run
```

Output: `data/booked_leads_YYYYMMDD_HHMMSS.csv`

Requires `CAL_COM_API_KEY`, `CAL_COM_USERNAME`, and `CAL_COM_EVENT_SLUG` in `.env`.

---

## Running the Full Pipeline

```bash
python agents/scraper.py
python agents/enricher.py
python agents/qualifier.py
python agents/outreach.py
python agents/booker.py
```

Each agent auto-picks the latest output from the previous stage, so no `--input` flags are needed for a sequential run.

## API Keys Required

| Key | Service | Used by |
|---|---|---|
| `ANTHROPIC_API_KEY` | Claude (AI reasoning) | qualifier, outreach |
| `OPENROUTER_API_KEY` | OpenRouter (model fallback) | optional |
| `PROXYCURL_API_KEY` | LinkedIn enrichment | scraper, enricher |
| `DEBOUNCE_API_KEY` | Email validation | enricher |
| `SMTP_HOST/PORT/USER/PASS` | Email sending | outreach |
| `CAL_COM_API_KEY` | Cal.com booking detection | booker |
| `CAL_COM_USERNAME` | Cal.com profile slug | booker |
| `CAL_COM_EVENT_SLUG` | Cal.com event type | booker |
| `SUPABASE_URL` / `SUPABASE_KEY` | Lead database | optional |
