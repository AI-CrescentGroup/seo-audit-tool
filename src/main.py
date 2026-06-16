import os
from dotenv import load_dotenv

# Load .env — check project root first, then config/
_base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _candidate in (os.path.join(_base, ".env"), os.path.join(_base, "config", ".env")):
    if os.path.isfile(_candidate):
        load_dotenv(dotenv_path=_candidate, override=True)
        break
else:
    load_dotenv(override=True)

# Fail fast if required keys are missing
_REQUIRED = ["SUPABASE_URL", "SUPABASE_SERVICE_KEY", "OPENROUTER_API_KEY"]
_missing = [k for k in _REQUIRED if not os.getenv(k)]
if _missing:
    raise ValueError(
        f"Missing required environment variables: {', '.join(_missing)}\n"
        "Copy .env.example to .env and fill in the values."
    )

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api.routes import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


# ── startup validation ─────────────────────────────────────────────────────

async def _validate_apis() -> None:
    # Supabase
    if os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_KEY"):
        logger.info("✅ Supabase credentials: present")
    else:
        logger.error("❌ Supabase credentials missing")

    # OpenRouter
    if os.getenv("OPENROUTER_API_KEY"):
        logger.info("✅ OpenRouter API key: present")
    else:
        logger.error("❌ OpenRouter API key missing")

    # PageSpeed (optional — validate with a real call)
    key = os.getenv("PAGESPEED_API_KEY")
    if key:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    "https://www.googleapis.com/pagespeedonline/v5/runPagespeed",
                    params={"url": "https://google.com", "strategy": "mobile", "key": key},
                )
            if r.status_code == 200:
                logger.info("✅ PageSpeed API: valid")
            else:
                logger.warning("⚠️  PageSpeed API returned %d — check your key", r.status_code)
        except Exception as exc:
            logger.warning("⚠️  PageSpeed API validation failed: %s", exc)
    else:
        logger.warning("⚠️  PAGESPEED_API_KEY not set (PageSpeed scores will be skipped)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _validate_apis()
    yield


# ── app ────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="SEO Audit Tool",
    description="Automated site-wide SEO analysis with AI recommendations",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:8080",
        "https://*.vercel.app",
    ],
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s", request.url)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "error": str(exc)},
    )


app.include_router(router, prefix="/api")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "env": {
            "supabase": bool(os.getenv("SUPABASE_URL")),
            "openrouter": bool(os.getenv("OPENROUTER_API_KEY")),
            "pagespeed": bool(os.getenv("PAGESPEED_API_KEY")),
        },
    }
