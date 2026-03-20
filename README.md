# Recommendation Engine v3

Google Search Ads AI recommendation system — Paytm Performance Marketing.

## Quick start (local)

```bash
# 1. Clone and install (Important)
git clone https://github.com/YOUR_ORG/re-engine.git
cd re-engine
pip install -r requirements.txt

# 2. Copy and fill environment variables
cp .env.example .env
# Edit .env with your actual credentials

# 3. Set up local Postgres (skip if using Railway DB directly)
createdb re_memory

# 4. Test the pipeline with sample data (no Google Ads creds needed)
python pipeline.py --dry-run

# 5. Start the API server locally
python api.py
# Visit: http://localhost:5000/health
```

## Railway deployment

See Section 13 of the Technical Guide PDF for full step-by-step instructions.

### One-time setup sequence
1. Push this repo to GitHub (all .py files must be at root)
2. Railway → New Project → Deploy from GitHub
3. Add Postgres plugin
4. Add all env vars from .env.example to Railway Variables
5. Add Cron service: `python pipeline.py` at `0 9 * * *`
6. Add Cron service: `python outcome_tracker.py` at `0 10 * * *`
7. POST /seed to create sample campaign configs

### Verify deployment
```bash
curl https://YOUR-PROJECT.up.railway.app/health
# Expected: {"status":"ok","data":{"db":"connected",...}}

curl -X POST https://YOUR-PROJECT.up.railway.app/run \
  -H "Content-Type: application/json" \
  -d '{"dry_run": true}'
```

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | /health | Health check |
| POST | /run | Run full pipeline |
| GET | /recommendations | Latest approved recs |
| POST | /feedback | Human accept/reject |
| POST | /implement | Confirm implementation (locks T0) |
| GET | /memory | Accuracy scores + run history |
| GET | /config/{id} | Get campaign config |
| PUT | /config/{id} | Update campaign config |
| GET | /portfolio | Category caps and spend |
| GET | /outcomes | 7-day impact results |
| GET | /do-nothing | DO_NOTHING outputs |
| POST | /seed | Seed sample configs (dev only) |

## File structure

```
api.py                  Flask API — all endpoints
pipeline.py             Main orchestrator — runs all layers
memory.py               PostgreSQL — all DB operations
data_pull.py            Google Ads API + sample fallback
normalizer.py           KPI-aware schema normalisation
query_engine.py         Segmentation · n-gram · clustering · dedup
cause_diagnosis.py      CTR/CVR/CPC cause trees (deterministic)
intelligence.py         Marginal CPA/ROAS engine
copy_intelligence.py    Position-adjusted CTR · intent matrix
portfolio.py            Cannibalization · category caps
lever_scorer.py         10-lever scoring engine
agents.py               Three Claude API calls
outcome_tracker.py      7-day impact analysis cron
```

## Adding a new campaign

```bash
curl -X PUT https://YOUR-PROJECT.up.railway.app/config/YOUR_CAMPAIGN_ID \
  -H "Content-Type: application/json" \
  -d '{
    "product": "digital_gold",
    "kpi": "CPA",
    "target_value": 250,
    "active": true,
    "lever_weights": {"ad_copy": 9, "search_term_mining": 8}
  }'
```

## Environment variables

See `.env.example` for full list. All secrets go in Railway Variables — never in code.
