import json
import logging
import os
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from src.crawler.cheerio_rules import run_full_audit
from src.database.db import get_audit, get_audit_by_domain, list_audits, save_audit, update_pdf_url
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
    key = os.environ.get("PAGESPEED_API_KEY")
    if not key:
        logger.info("PageSpeed API: no key configured")
        return None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                "https://www.googleapis.com/pagespeedonline/v5/runPagespeed",
                params={"url": url, "strategy": "mobile", "key": key},
            )
            resp.raise_for_status()
            data = resp.json()

        # Debug: log the actual response structure
        logger.debug(f"PageSpeed API response keys: {data.keys()}")

        lighthouse = data.get("lighthouseResult", {})
        if not lighthouse:
            logger.warning(f"PageSpeed API: no lighthouseResult in response for {url}")
            return None

        cats = lighthouse.get("categories", {})
        if not cats:
            logger.warning(f"PageSpeed API: no categories in lighthouse result for {url}")
            logger.debug(f"Lighthouse keys: {lighthouse.keys()}")
            return None

        logger.debug(f"PageSpeed categories found: {cats.keys()}")

        # Extract scores with detailed logging
        result = {}
        for metric_name, category_key in [
            ("performance", "performance"),
            ("seo", "seo"),
            ("accessibility", "accessibility"),
            ("best_practices", "best-practices"),
        ]:
            category = cats.get(category_key, {})
            score = category.get("score")
            if score is None:
                logger.warning(f"PageSpeed: {category_key} score is None/missing")
                result[metric_name] = 0
            else:
                result[metric_name] = round(score * 100)
                logger.debug(f"PageSpeed: {metric_name} = {score} → {result[metric_name]}")

        logger.info(f"PageSpeed API success for {url}: {result}")
        return result
    except Exception as exc:
        logger.error(f"PageSpeed API failed for {url}: {exc}", exc_info=True)
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


# ── endpoints ─────────────────────────────────────────────────────────────────

@router.post("/audit/{domain:path}", response_model=AuditResult)
async def create_audit(domain: str):
    start_url = _normalise_domain(domain)
    clean_domain = urlparse(start_url).netloc

    # Return cached result
    existing = await get_audit_by_domain(clean_domain)
    if existing:
        logger.info("Returning cached audit for %s", clean_domain)
        
        # Parse potential string records into a verified dict format
        parsed_ai_recs = _parse_ai_recommendations(existing.get("ai_insights"))
        
        return AuditResult(
            id=existing["id"],
            domain=clean_domain,
            status="cached",
            pages_crawled=existing.get("metrics", {}).get("pages_crawled", 0),
            metrics=existing.get("metrics", {}),
            pagespeed=existing.get("pagespeed"),
            ai_recommendations=parsed_ai_recs,
            pdf_url=existing.get("pdf_url"),
        )

    logger.info("Starting fresh audit for %s", start_url)

    # 1. Crawl
    try:
        metrics = await run_full_audit(start_url)
    except Exception as exc:
        logger.exception("Crawler failed for %s", start_url)
        raise HTTPException(status_code=500, detail=f"Crawler error: {exc}")

    # 2. PageSpeed
    logger.info(f"Calling PageSpeed API for {start_url}")
    pagespeed = await _run_pagespeed(start_url)
    if pagespeed:
        logger.info(f"PageSpeed results: {pagespeed}")
    else:
        logger.warning(f"PageSpeed returned None for {start_url}")

    # 3. AI analysis
    try:
        ai_recs = await analyze_seo(metrics)
    except Exception as exc:
        logger.warning("AI analysis failed: %s", exc)
        ai_recs = {"overall_score": 0, "summary": str(exc), "critical_issues": [], "quick_wins": []}

    # Ensure memory reference is a clean dictionary format before storage
    ai_recs = _parse_ai_recommendations(ai_recs)

    # 4. Save to Supabase
    saved = await save_audit({
        "domain": clean_domain,
        "metrics": metrics,
        "pagespeed": pagespeed,
        "ai_insights": ai_recs,
    })
    audit_id = saved.get("id") if saved else None

    # 5. Generate PDF + upload to Supabase Storage (non-blocking on failure)
    pdf_url: str | None = None
    if audit_id:
        try:
            pdf_url = await generate_and_store_pdf(clean_domain, audit_id, metrics, ai_recs)
            if pdf_url:
                await update_pdf_url(audit_id, pdf_url)
            else:
                logger.warning("PDF generation returned null for %s", audit_id)
        except Exception as exc:
            logger.error("PDF generation/upload raised exception: %s", exc, exc_info=True)

    return AuditResult(
        id=audit_id,
        domain=clean_domain,
        status="completed",
        pages_crawled=metrics.get("pages_crawled", 0),
        metrics=metrics,
        pagespeed=pagespeed,
        ai_recommendations=ai_recs,
        pdf_url=pdf_url,
    )


@router.get("/audit/{audit_id}", response_model=AuditResult)
async def get_audit_endpoint(audit_id: str):
    record = await get_audit(audit_id)
    if not record:
        raise HTTPException(status_code=404, detail="Audit not found")
        
    # Safeguard against string conversions on direct entry fetches
    parsed_ai_recs = _parse_ai_recommendations(record.get("ai_insights"))
    
    return AuditResult(
        id=record["id"],
        domain=record["domain"],
        status="completed",
        pages_crawled=record.get("metrics", {}).get("pages_crawled", 0),
        metrics=record.get("metrics", {}),
        pagespeed=record.get("pagespeed"),
        ai_recommendations=parsed_ai_recs,
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
