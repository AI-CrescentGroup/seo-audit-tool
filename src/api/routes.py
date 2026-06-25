import json
import logging
import os
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from src.crawler.cheerio_rules import run_full_audit
from src.database.db import get_audit, get_audit_by_domain, list_audits, save_audit, update_audit, update_pdf_url
from src.utils.ai_analyzer import analyze_seo, rewrite_for_geo
from src.utils.pdf_generator import generate_and_store_pdf

logger = logging.getLogger(__name__)
router = APIRouter()


# ── helpers ───────────────────────────────────────────────────────────────────

def _normalise_domain(domain: str) -> str:
    if not domain.startswith(("http://", "https://")):
        domain = f"https://{domain}"
    parsed = urlparse(domain)
    return f"{parsed.scheme}://{parsed.netloc}/"


def _parse_ai_recommendations(ai_insights) -> dict:
    """Safely converts stringified or raw database text into a clean Python dictionary structure."""
    if not ai_insights:
        return {}
    if isinstance(ai_insights, str):
        try:
            return json.loads(ai_insights)
        except Exception as e:
            logger.warning("Failed to parse stringified ai_insights: %s", e)
            return {"overall_score": 0, "summary": ai_insights, "critical_issues": [], "quick_wins": []}
    if isinstance(ai_insights, dict):
        return ai_insights
    return {}


async def _run_pagespeed(url: str) -> dict | None:
    """
    Fetch PageSpeed Insights scores for all 4 metrics.
    Uses strategy=desktop to ensure all metrics are returned.
    """
    key = os.environ.get("PAGESPEED_API_KEY")
    if not key:
        logger.info("PageSpeed API: PAGESPEED_API_KEY not configured")
        return None

    try:
        import httpx

        # Use desktop strategy to get all metrics (mobile may not return SEO/Accessibility/Best Practices)
        strategy = "desktop"
        api_url = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
        params = {
            "url": url,
            "strategy": strategy,
            "key": key,
            "category": ["performance", "seo", "accessibility", "best-practices"],  # Explicitly request all categories
        }

        logger.info(f"PageSpeed API: calling {api_url} with strategy={strategy} for {url}")

        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.get(api_url, params=params)
            resp.raise_for_status()
            response_json = resp.json()

        # Log full response for debugging
        logger.debug(f"PageSpeed full response: {json.dumps(response_json, indent=2)}")

        # Navigate to lighthouseResult
        lighthouse = response_json.get("lighthouseResult")
        if not lighthouse:
            logger.error(f"PageSpeed: no lighthouseResult in response for {url}")
            logger.error(f"Response keys: {response_json.keys()}")
            return None

        # Get categories
        categories = lighthouse.get("categories", {})
        if not categories:
            logger.error(f"PageSpeed: no categories found in lighthouseResult for {url}")
            logger.error(f"Lighthouse keys: {lighthouse.keys()}")
            return None

        logger.info(f"PageSpeed: categories found: {list(categories.keys())}")

        # Parse all 4 metrics with detailed logging
        pagespeed_scores = {
            "performance": 0,
            "seo": 0,
            "accessibility": 0,
            "best_practices": 0,
        }

        # Map API category names to our output keys
        category_mappings = [
            ("performance", "performance"),
            ("seo", "seo"),
            ("accessibility", "accessibility"),
            ("best-practices", "best_practices"),  # API uses hyphen, we use underscore
        ]

        for api_category_name, output_key in category_mappings:
            category_data = categories.get(api_category_name)

            if not category_data:
                logger.warning(f"PageSpeed: category '{api_category_name}' not found in response")
                pagespeed_scores[output_key] = 0
                continue

            score = category_data.get("score")

            if score is None:
                logger.warning(f"PageSpeed: category '{api_category_name}' has no score")
                pagespeed_scores[output_key] = 0
            else:
                # Score is 0-1, multiply by 100 for 0-100 scale
                pagespeed_scores[output_key] = round(score * 100)
                logger.info(f"PageSpeed: {output_key} = {score} → {pagespeed_scores[output_key]}/100")

        logger.info(f"PageSpeed final scores: {pagespeed_scores}")
        return pagespeed_scores

    except Exception as exc:
        logger.error(f"PageSpeed API error for {url}: {exc}", exc_info=True)
        return None


# ── models ────────────────────────────────────────────────────────────────────

class AuditResult(BaseModel):
    id: str | None = None
    domain: str
    status: str
    pages_crawled: int = 0
    metrics: dict = {}
    pagespeed: dict | None = None
    ai_recommendations: dict = {}
    gsc_metrics: dict | None = None
    pdf_url: str | None = None


class RewriteRequest(BaseModel):
    page_url: str
    target_keyword: str
    page_content: str


class RewriteResponse(BaseModel):
    original_content: str
    rewritten_content: str
    faq_block: str
    json_ld_schema: dict
    diff_summary: str


# ── background audit runner ───────────────────────────────────────────────────

async def _run_audit_background(audit_id: str, start_url: str, clean_domain: str) -> None:
    """Full audit pipeline — runs after the POST response is sent."""
    logger.info("Background audit started for %s (id=%s)", clean_domain, audit_id)
    try:
        # 1. Crawl
        metrics = await run_full_audit(start_url)
        logger.info("Crawl done for %s: %d pages", clean_domain, metrics.get("pages_crawled", 0))

        # 2. PageSpeed
        pagespeed = await _run_pagespeed(start_url)

        # 3. AI analysis
        try:
            ai_recs = await analyze_seo(metrics)
        except Exception as exc:
            logger.warning("AI analysis failed: %s", exc)
            ai_recs = {"overall_score": 0, "summary": str(exc), "critical_issues": [], "quick_wins": []}
        ai_recs = _parse_ai_recommendations(ai_recs)

        # 4. Write results to Supabase (overwrite the placeholder)
        await update_audit(audit_id, {
            "metrics": metrics,
            "pagespeed": pagespeed,
            "ai_insights": ai_recs,
        })
        logger.info("Audit %s saved to Supabase", audit_id)

        # 5. GSC data (non-blocking)
        gsc_metrics = None
        try:
            from src.api.routes_gsc import attach_gsc_to_audit
            result = await attach_gsc_to_audit(audit_id)
            if result.get("success"):
                if result.get("urls_tracked"):
                    logger.info(f"GSC data attached to audit {audit_id}: {result['urls_tracked']} URLs")
                    # Fetch the gsc_metrics from database for PDF inclusion
                    audit_record = await get_audit(audit_id)
                    if audit_record:
                        gsc_metrics = audit_record.get("gsc_metrics")
                elif result.get("skipped"):
                    logger.info(f"GSC attach skipped: {result.get('reason')}")
        except Exception as exc:
            logger.warning(f"GSC attach failed (non-blocking) for {audit_id}: {exc}")

        # 6. PDF (non-blocking, now includes GSC metrics if available)
        try:
            pdf_url = await generate_and_store_pdf(clean_domain, audit_id, metrics, ai_recs, gsc_metrics)
            if pdf_url:
                await update_pdf_url(audit_id, pdf_url)
        except Exception as exc:
            logger.error("PDF generation failed for %s: %s", audit_id, exc, exc_info=True)

        logger.info("Background audit complete for %s", clean_domain)

    except Exception as exc:
        logger.exception("Background audit crashed for %s", clean_domain)
        # Mark as errored so the frontend stops polling
        await update_audit(audit_id, {
            "metrics": {"pages_crawled": 0, "_error": str(exc)[:200]},
            "ai_insights": {"overall_score": 0, "summary": f"Audit failed: {str(exc)[:200]}", "critical_issues": [], "quick_wins": []},
        })


# ── endpoints ─────────────────────────────────────────────────────────────────

@router.post("/audit/{domain:path}", response_model=AuditResult)
async def create_audit(domain: str, background_tasks: BackgroundTasks):
    start_url = _normalise_domain(domain)
    clean_domain = urlparse(start_url).netloc

    # Return cached result if one exists
    existing = await get_audit_by_domain(clean_domain)
    if existing:
        logger.info("Returning cached audit for %s", clean_domain)
        parsed_ai_recs = _parse_ai_recommendations(existing.get("ai_insights"))
        return AuditResult(
            id=existing["id"],
            domain=clean_domain,
            status="cached",
            pages_crawled=existing.get("metrics", {}).get("pages_crawled", 0),
            metrics=existing.get("metrics", {}),
            pagespeed=existing.get("pagespeed"),
            ai_recommendations=parsed_ai_recs,
            gsc_metrics=existing.get("gsc_metrics"),
            pdf_url=existing.get("pdf_url"),
        )

    # Create a "processing" placeholder — returns immediately to the client
    saved = await save_audit({
        "domain": clean_domain,
        "metrics": {"pages_crawled": 0, "_processing": True},
        "pagespeed": None,
        "ai_insights": {},
    })
    audit_id = saved.get("id")
    if not audit_id:
        raise HTTPException(status_code=500, detail="Failed to create audit record")

    logger.info("Created processing placeholder %s for %s", audit_id, clean_domain)

    # Queue full audit to run after this response is sent
    background_tasks.add_task(_run_audit_background, audit_id, start_url, clean_domain)

    return AuditResult(
        id=audit_id,
        domain=clean_domain,
        status="processing",
        pages_crawled=0,
        metrics={},
        pagespeed=None,
        ai_recommendations={},
    )


@router.get("/audit/{audit_id}", response_model=AuditResult)
async def get_audit_endpoint(audit_id: str):
    record = await get_audit(audit_id)
    if not record:
        raise HTTPException(status_code=404, detail="Audit not found")

    metrics = record.get("metrics", {})
    # _processing flag means background task hasn't finished yet
    if metrics.get("_processing"):
        return AuditResult(
            id=record["id"],
            domain=record["domain"],
            status="processing",
            pages_crawled=0,
            metrics={},
            pagespeed=None,
            ai_recommendations={},
            gsc_metrics=None,
        )

    # _error flag means background task crashed
    if metrics.get("_error"):
        parsed_ai_recs = _parse_ai_recommendations(record.get("ai_insights"))
        return AuditResult(
            id=record["id"],
            domain=record["domain"],
            status="error",
            pages_crawled=0,
            metrics={},
            pagespeed=None,
            ai_recommendations=parsed_ai_recs,
            gsc_metrics=None,
        )

    parsed_ai_recs = _parse_ai_recommendations(record.get("ai_insights"))
    return AuditResult(
        id=record["id"],
        domain=record["domain"],
        status="completed",
        pages_crawled=metrics.get("pages_crawled", 0),
        metrics=metrics,
        pagespeed=record.get("pagespeed"),
        ai_recommendations=parsed_ai_recs,
        gsc_metrics=record.get("gsc_metrics"),
        pdf_url=record.get("pdf_url"),
    )


@router.get("/audit/{audit_id}/pdf")
async def get_pdf(audit_id: str):
    """Return the PDF file directly with proper headers for browser download."""
    record = await get_audit(audit_id)
    if not record:
        raise HTTPException(status_code=404, detail="Audit not found")

    pdf_url = record.get("pdf_url")
    if not pdf_url:
        raise HTTPException(status_code=404, detail="PDF not yet generated for this audit")

    # Fetch the PDF from Supabase Storage
    try:
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(pdf_url)
            resp.raise_for_status()
            pdf_bytes = resp.content
    except Exception as exc:
        logger.warning("Failed to fetch PDF from Supabase: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to retrieve PDF")

    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        iter([pdf_bytes]),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=seo-audit-{audit_id}.pdf"}
    )


@router.get("/audits")
async def list_audits_endpoint(limit: int = 20, offset: int = 0):
    return await list_audits(limit=limit, offset=offset)


@router.post("/audit/{audit_id}/rewrite", response_model=RewriteResponse)
async def rewrite_page(audit_id: str, req: RewriteRequest):
    """Rewrite page content for GEO (Generative Engine Optimization)."""
    record = await get_audit(audit_id)
    if not record:
        raise HTTPException(status_code=404, detail="Audit not found")

    try:
        result = await rewrite_for_geo(
            page_url=req.page_url,
            target_keyword=req.target_keyword,
            page_content=req.page_content,
        )
        return result
    except Exception as exc:
        logger.exception("Rewrite failed for %s", req.page_url)
        raise HTTPException(status_code=500, detail=f"Rewrite error: {exc}")
