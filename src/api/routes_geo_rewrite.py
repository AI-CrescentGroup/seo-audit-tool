"""
GEO (Generative Engine Optimization) rewrite endpoint.
Rewrites page content for better LLM citation and AI consumption.
"""
import json
import logging
import os
from typing import Optional

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ── models ────────────────────────────────────────────────────────────────────

class GeoRewriteRequest(BaseModel):
    url: str
    current_content: str
    geo_issues: list[str] = []


class GeoRewriteResponse(BaseModel):
    url: str
    original_content: str
    rewritten_content: str
    diff_summary: str
    faq_suggestions: Optional[list[dict]] = None


# ── constants ─────────────────────────────────────────────────────────────────

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "google/gemini-2.5-flash"

GEO_SYSTEM_PROMPT = """You are an SEO expert specializing in Generative Engine Optimization (GEO).
Your task is to rewrite web page content to maximize citation by LLMs (ChatGPT, Perplexity, Gemini).

Focus on these 5 optimization signals:

1. **FAQ Schema**: Structure key Q&A pairs that LLMs commonly cite. Format:
   Q: [Common question about the topic]
   A: [Direct, concise answer with entities and data]

2. **Entity Density**: Mention 5+ named entities (companies, people, locations, products) naturally in the text.
   Example: "TensorFlow, released by Google in 2015, powers..."

3. **Answer Density**: In the first 100 words, directly answer the main query without fluff.
   Bad: "Project management is important..."
   Good: "Asana is a project management tool that helps teams track tasks in real-time."

4. **Freshness Signals**: Add recency indicators.
   Example: "As of 2024, the market leader is..." or "In the latest industry report..."

5. **Readability for LLMs**: Use simple language (Flesch-Kincaid grade 8 or lower).
   Avoid: "Utilize the aforementioned methodology..."
   Use: "Use this method..."

---

Your output MUST follow this exact format:

[REWRITTEN_CONTENT]
(Plain text, 200-400 words, optimized for LLM citation)

---SEPARATOR---

[FAQ_SUGGESTIONS]
Q: [Question 1]
A: [Answer 1]

Q: [Question 2]
A: [Answer 2]

Q: [Question 3]
A: [Answer 3]

Return ONLY this format. No markdown, no explanations, no additional text."""


# ── helpers ────────────────────────────────────────────────────────────────────

async def call_gemini_rewrite(
    url: str, current_content: str, geo_issues: list[str]
) -> tuple[str, str, list[dict]]:
    """
    Call Gemini via OpenRouter to rewrite content for GEO.
    Returns (rewritten_content, diff_summary, faq_suggestions).
    """
    api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("No API key found (OPENROUTER_API_KEY or ANTHROPIC_API_KEY)")

    # Build user prompt with context
    issues_text = "\n".join(f"- {issue}" for issue in geo_issues) if geo_issues else "- General GEO optimization"

    user_prompt = f"""URL: {url}

Current GEO Issues:
{issues_text}

Original Content:
{current_content[:2000]}

Rewrite this content to address the GEO issues above. Focus on making it highly citable by LLMs."""

    payload = {
        "model": MODEL,
        "max_tokens": 2000,
        "temperature": 0.7,
        "messages": [
            {"role": "system", "content": GEO_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://seo-audit-tool-frontend.vercel.app",
        "X-Title": "SEO GEO Rewrite Engine",
    }

    logger.info(f"Calling Gemini for GEO rewrite: {url}")

    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(OPENROUTER_API_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.error(f"Gemini API call failed: {exc}", exc_info=True)
        raise

    if "choices" not in data or not data["choices"]:
        logger.error(f"Gemini returned no choices: {data}")
        raise RuntimeError("Gemini API returned empty response")

    raw_response = data["choices"][0]["message"]["content"].strip()
    logger.debug(f"Gemini raw response length: {len(raw_response)} chars")

    # Parse response: split by separator
    if "---SEPARATOR---" not in raw_response:
        logger.warning(f"Gemini response missing separator, using entire response as content")
        rewritten_content = raw_response
        faq_block = ""
    else:
        parts = raw_response.split("---SEPARATOR---")
        rewritten_content = parts[0].strip()
        faq_block = parts[1].strip() if len(parts) > 1 else ""

    # Parse FAQ suggestions
    faq_suggestions = []
    if faq_block:
        current_q = None
        for line in faq_block.split("\n"):
            line = line.strip()
            if line.startswith("Q:"):
                if current_q:
                    faq_suggestions.append(current_q)
                current_q = {"question": line[2:].strip(), "answer": ""}
            elif line.startswith("A:") and current_q:
                current_q["answer"] = line[2:].strip()
        if current_q:
            faq_suggestions.append(current_q)

    # Generate diff summary
    original_entity_count = len([w for w in current_content.split() if w and w[0].isupper()])
    rewritten_entity_count = len([w for w in rewritten_content.split() if w and w[0].isupper()])
    entity_delta = rewritten_entity_count - original_entity_count

    diff_summary = f"Rewritten for GEO optimization: {len(faq_suggestions)} FAQ items added, +{entity_delta} entity mentions, improved answer density"

    logger.info(f"GEO rewrite complete: {len(rewritten_content)} chars, {len(faq_suggestions)} FAQs")

    return rewritten_content, diff_summary, faq_suggestions


# ── router setup ──────────────────────────────────────────────────────────────

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api", tags=["geo-rewrite"])


@router.post("/rewrite-for-geo", response_model=GeoRewriteResponse)
async def rewrite_for_geo(req: GeoRewriteRequest):
    """
    Rewrite page content for GEO (Generative Engine Optimization).
    Optimizes content for LLM citation and AI consumption.
    """
    # Validate input
    if not req.url:
        raise HTTPException(status_code=400, detail="URL is required")
    if not req.current_content or len(req.current_content.strip()) < 50:
        raise HTTPException(status_code=400, detail="Content is required (minimum 50 characters)")

    logger.info(f"GEO rewrite request for {req.url}")

    try:
        rewritten_content, diff_summary, faq_suggestions = await call_gemini_rewrite(
            url=req.url,
            current_content=req.current_content,
            geo_issues=req.geo_issues,
        )
    except RuntimeError as exc:
        logger.error(f"GEO rewrite failed for {req.url}: {exc}")
        raise HTTPException(status_code=500, detail=f"Rewrite failed: {str(exc)[:100]}")
    except Exception as exc:
        logger.exception(f"Unexpected error during GEO rewrite for {req.url}")
        raise HTTPException(status_code=500, detail="Internal error during content rewriting")

    return GeoRewriteResponse(
        url=req.url,
        original_content=req.current_content[:1000],  # Keep original for reference
        rewritten_content=rewritten_content,
        diff_summary=diff_summary,
        faq_suggestions=faq_suggestions,
    )
