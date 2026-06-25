"""
Google Search Console OAuth integration + per-URL metrics.
Handles: auth flow, token storage, GSC API calls, per-URL data fetching.
"""
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/gsc", tags=["gsc"])

# Temporary cache for PKCE code verifiers (state -> {verifier, timestamp})
# In production, use Redis or database instead
_code_verifier_cache: dict = {}

# Import after logging setup
try:
    from google_auth_oauthlib.flow import Flow
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    GOOGLE_IMPORTS_OK = True
except ImportError as e:
    logger.warning(f"Google API imports failed (install google-auth-oauthlib): {e}")
    GOOGLE_IMPORTS_OK = False


# ── helpers ────────────────────────────────────────────────────────────────

def _get_db():
    """Get Supabase client."""
    from src.database.db import get_client
    return get_client()


def _build_flow():
    """Build OAuth flow object."""
    if not GOOGLE_IMPORTS_OK:
        raise RuntimeError("Google API libraries not installed")

    config = {
        "web": {
            "client_id": os.getenv("GOOGLE_CLIENT_ID"),
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
            "redirect_uris": [os.getenv("GOOGLE_REDIRECT_URI")],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }

    return Flow.from_client_config(
        config,
        scopes=["https://www.googleapis.com/auth/webmasters.readonly"],
        redirect_uri=os.getenv("GOOGLE_REDIRECT_URI")
    )


def _get_credentials():
    """Retrieve stored Google credentials from Supabase."""
    db = _get_db()
    try:
        result = db.table("gsc_connections").select("*").eq("domain", "default").execute()
        if not result.data:
            return None

        cred_data = result.data[0]
        credentials = Credentials(
            token=cred_data["access_token"],
            refresh_token=cred_data["refresh_token"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=os.getenv("GOOGLE_CLIENT_ID"),
            client_secret=os.getenv("GOOGLE_CLIENT_SECRET")
        )

        # Check if expired and refresh if needed
        if credentials.expired and credentials.refresh_token:
            logger.info("Refreshing expired GSC credentials")
            credentials.refresh(Request())

            # Update token in Supabase
            db.table("gsc_connections").update({
                "access_token": credentials.token,
                "token_expiry": credentials.expiry.isoformat() if credentials.expiry else None
            }).eq("domain", "default").execute()
            logger.info("GSC credentials refreshed and stored")

        return credentials
    except Exception as e:
        logger.error(f"Failed to retrieve GSC credentials: {e}")
        return None


# ── endpoints ──────────────────────────────────────────────────────────────

@router.get("/auth")
async def gsc_auth():
    """Generate Google OAuth URL and initiate auth flow."""
    if not GOOGLE_IMPORTS_OK:
        raise HTTPException(status_code=503, detail="Google API not available")

    try:
        flow = _build_flow()
        auth_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent"
        )

        # Store code_verifier for PKCE in callback (temporary cache - 10 min validity)
        # For production, use Redis or DB. For now, store in memory with expiration.
        import time
        _code_verifier_cache[state] = {
            "verifier": flow.code_verifier,
            "timestamp": time.time()
        }

        logger.info("GSC auth URL generated")
        return {"auth_url": auth_url}
    except Exception as e:
        logger.error(f"GSC auth generation failed: {e}")
        raise HTTPException(status_code=500, detail=f"Auth failed: {str(e)[:100]}")


@router.get("/callback")
async def gsc_callback(code: str, state: Optional[str] = None):
    """Handle OAuth callback from Google, store tokens."""
    if not GOOGLE_IMPORTS_OK:
        raise HTTPException(status_code=503, detail="Google API not available")

    try:
        # Retrieve stored code_verifier for PKCE
        code_verifier = None
        if state and state in _code_verifier_cache:
            cached = _code_verifier_cache.pop(state)
            code_verifier = cached.get("verifier")

        flow = _build_flow()

        # Set the code_verifier before fetching token (for PKCE)
        if code_verifier:
            flow.code_verifier = code_verifier

        flow.fetch_token(code=code)
        credentials = flow.credentials

        db = _get_db()
        db.table("gsc_connections").upsert({
            "domain": "default",
            "access_token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "token_expiry": credentials.expiry.isoformat() if credentials.expiry else None,
            "connected_at": datetime.utcnow().isoformat()
        }).execute()

        logger.info("GSC tokens stored in Supabase")

        frontend_url = os.getenv("FRONTEND_URL", "https://seo-audit-tool-frontend.vercel.app")
        return RedirectResponse(url=f"{frontend_url}/gsc-connected")

    except Exception as e:
        logger.error(f"GSC callback failed: {e}")
        raise HTTPException(status_code=500, detail=f"Callback failed: {str(e)[:100]}")


@router.get("/status")
async def gsc_status():
    """Check if GSC is connected."""
    db = _get_db()
    try:
        result = db.table("gsc_connections").select("*").eq("domain", "default").execute()

        if result.data:
            return {
                "connected": True,
                "connected_at": result.data[0]["connected_at"]
            }
        return {"connected": False}
    except Exception as e:
        logger.error(f"GSC status check failed: {e}")
        raise HTTPException(status_code=500, detail="Status check failed")


@router.get("/data/{domain}")
async def get_gsc_data(domain: str):
    """Fetch GSC data (site totals + per-URL) for last 30 days."""
    credentials = _get_credentials()
    if not credentials:
        raise HTTPException(status_code=404, detail="GSC not connected")

    try:
        service = build("webmasters", "v3", credentials=credentials)
        site_url = f"https://{domain}/"

        end_date = datetime.utcnow().strftime("%Y-%m-%d")
        start_date = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")

        logger.info(f"Fetching GSC data for {domain} ({start_date} to {end_date})")

        # Site-level totals
        site_response = service.searchanalytics().query(
            siteUrl=site_url,
            body={
                "startDate": start_date,
                "endDate": end_date,
                "dimensions": [],
                "rowLimit": 1
            }
        ).execute()

        # Per-URL breakdown (top 1000 URLs by clicks)
        url_response = service.searchanalytics().query(
            siteUrl=site_url,
            body={
                "startDate": start_date,
                "endDate": end_date,
                "dimensions": ["page"],
                "rowLimit": 1000
            }
        ).execute()

        # Format per-URL data
        per_url = {}
        for row in url_response.get("rows", []):
            url = row["keys"][0]
            per_url[url] = {
                "impressions": row.get("impressions", 0),
                "clicks": row.get("clicks", 0),
                "ctr": round(row.get("ctr", 0) * 100, 2),
                "position": round(row.get("position", 0), 1)
            }

        # Format site totals
        totals_row = site_response.get("rows", [{}])[0]
        site_totals = {
            "impressions": totals_row.get("impressions", 0),
            "clicks": totals_row.get("clicks", 0),
            "ctr": round(totals_row.get("ctr", 0) * 100, 2),
            "avg_position": round(totals_row.get("position", 0), 1)
        }

        logger.info(f"GSC data fetched: {len(per_url)} URLs, {site_totals['impressions']} impressions")

        return {
            "domain": domain,
            "date_range": f"{start_date} to {end_date}",
            "site_totals": site_totals,
            "per_url": per_url
        }

    except Exception as e:
        logger.error(f"GSC data fetch failed for {domain}: {e}")
        raise HTTPException(status_code=500, detail=f"GSC fetch failed: {str(e)[:100]}")


@router.post("/attach/{audit_id}")
async def attach_gsc_to_audit(audit_id: str):
    """Fetch GSC data for audit's domain and store in audit.gsc_metrics."""
    db = _get_db()

    try:
        # Get audit domain
        audit_result = db.table("audits").select("domain").eq("id", audit_id).execute()
        if not audit_result.data:
            raise HTTPException(status_code=404, detail="Audit not found")

        domain = audit_result.data[0]["domain"]

        # Check if GSC is connected (no error, just return early)
        creds_check = db.table("gsc_connections").select("id").eq("domain", "default").execute()
        if not creds_check.data:
            logger.info(f"GSC not connected; skipping attach for audit {audit_id}")
            return {"success": True, "skipped": True, "reason": "GSC not connected"}

        # Fetch GSC data
        gsc_data = await get_gsc_data(domain)

        # Store in audit.gsc_metrics
        db.table("audits").update({
            "gsc_metrics": gsc_data
        }).eq("id", audit_id).execute()

        logger.info(f"GSC data attached to audit {audit_id} ({len(gsc_data['per_url'])} URLs)")
        return {
            "success": True,
            "urls_tracked": len(gsc_data["per_url"]),
            "site_impressions": gsc_data["site_totals"]["impressions"]
        }

    except HTTPException:
        # Non-blocking — if GSC fails, audit still completes
        logger.warning(f"GSC attach skipped for audit {audit_id} (API error)")
        return {"success": True, "skipped": True, "reason": "GSC API error"}
    except Exception as e:
        logger.error(f"GSC attach failed for audit {audit_id}: {e}")
        return {"success": True, "skipped": True, "reason": str(e)[:100]}
