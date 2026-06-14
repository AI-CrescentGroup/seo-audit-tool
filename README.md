# SEO Audit Tool — Backend

FastAPI backend: crawls up to 500 pages, extracts 13 SEO metrics, calls Claude via OpenRouter for AI recommendations, stores PDFs in Supabase Storage.

## Local Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your credentials
uvicorn src.main:app --reload --port 8000
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `SUPABASE_URL` | ✅ | Your Supabase project URL |
| `SUPABASE_SERVICE_KEY` | ✅ | Supabase service role key |
| `OPENROUTER_API_KEY` | ✅ | OpenRouter API key (Claude) |
| `PAGESPEED_API_KEY` | optional | Google PageSpeed Insights key |

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/audit/{domain}` | Start or return cached audit |
| `GET` | `/api/audit/{id}` | Fetch audit by ID |
| `GET` | `/api/audits` | List recent audits |
| `GET` | `/health` | Health + env check |

## Deploy to Railway

1. Push this repo to GitHub
2. railway.app → New Project → Deploy from GitHub
3. Add all env vars from `.env.example`
4. Railway auto-deploys on every push
