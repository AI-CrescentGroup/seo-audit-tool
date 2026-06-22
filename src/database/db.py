import logging
import os
from typing import Optional

from supabase import Client, create_client

logger = logging.getLogger(__name__)

_client: Optional[Client] = None


def get_client() -> Client:
    global _client
    if _client is None:
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_SERVICE_KEY"]
        _client = create_client(url, key)
    return _client


async def save_audit(data: dict) -> dict:
    try:
        resp = get_client().table("audits").insert(data).execute()
        return resp.data[0] if resp.data else {}
    except Exception as exc:
        logger.error("save_audit failed: %s", exc)
        return {}


async def get_audit(audit_id: str) -> Optional[dict]:
    try:
        resp = get_client().table("audits").select("*").eq("id", audit_id).single().execute()
        return resp.data
    except Exception:
        return None


async def get_audit_by_domain(domain: str) -> Optional[dict]:
    try:
        resp = (
            get_client()
            .table("audits")
            .select("*")
            .eq("domain", domain)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None
    except Exception:
        return None


async def update_audit(audit_id: str, data: dict) -> None:
    try:
        get_client().table("audits").update(data).eq("id", audit_id).execute()
    except Exception as exc:
        logger.error("update_audit failed: %s", exc)


async def update_pdf_url(audit_id: str, pdf_url: str) -> None:
    try:
        get_client().table("audits").update({"pdf_url": pdf_url}).eq("id", audit_id).execute()
    except Exception as exc:
        logger.error("update_pdf_url failed: %s", exc)


async def list_audits(limit: int = 20, offset: int = 0) -> list[dict]:
    try:
        resp = (
            get_client()
            .table("audits")
            .select("id, domain, created_at, metrics->pages_crawled, ai_insights->overall_score")
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        return resp.data or []
    except Exception as exc:
        logger.error("list_audits failed: %s", exc)
        return []
