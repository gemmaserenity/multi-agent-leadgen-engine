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
├── requirements.txt # Python dependencies
└── README.md
```

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   playwright install chromium
   ```

2. Fill in `.env` with your API keys.

3. Add ICP profiles to `config/` (see `icp_futuri.json` as a template).

## ICP Profiles

Each client has a JSON profile in `config/` defining:
- Target industries and job titles
- Company size and contract value minimums
- Core pain point and disqualifiers
- Price per booked meeting

## Agents

Agents are standalone scripts in `agents/`. Each reads from `data/`, writes results back to `data/`, and logs to `logs/`.

| Agent | Purpose |
|---|---|
| `scraper.py` | Pull raw leads from LinkedIn / Apollo / etc. |
| `enricher.py` | Enrich leads via Proxycurl |
| `qualifier.py` | Score leads against ICP profile |
| `outreach.py` | Generate and send personalized emails |
| `booker.py` | Book meetings via Cal.com API |

## API Keys Required

| Key | Service |
|---|---|
| `ANTHROPIC_API_KEY` | Claude (AI reasoning) |
| `OPENROUTER_API_KEY` | OpenRouter (model fallback) |
| `PROXYCURL_API_KEY` | LinkedIn enrichment |
| `DEBOUNCE_API_KEY` | Email validation |
| `SUPABASE_URL` / `SUPABASE_KEY` | Lead database |
| `CAL_COM_API_KEY` | Meeting booking |
