"""
AI-powered SEO recommendations via OpenRouter (Claude model).
"""
import json
import logging
import os
import re

import httpx

logger = logging.getLogger(__name__)

_FALLBACK: dict = {
    "overall_score": 0,
    "summary": "AI analysis could not be completed.",
    "critical_issues": [],
    "quick_wins": [],
}


def _strip_fences(text: str) -> str:
    """Remove markdown code fences Claude sometimes adds despite instructions."""
    # ```json ... ``` or ``` ... ```
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```$", "", text, flags=re.MULTILINE)
    return text.strip()


def _extract_json_block(text: str) -> str:
    """Pull the first {...} block from text as a last-resort fallback."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return match.group(0) if match else text


def _parse_json_response(raw: str) -> dict:
    """
    Try increasingly aggressive strategies to extract valid JSON from the
    model response, then validate it has the required keys.
    """
    candidates = [
        raw,                          # 1. as-is
        _strip_fences(raw),           # 2. strip markdown fences
        _extract_json_block(raw),     # 3. pull first {...} block
    ]

    for attempt in candidates:
        try:
            parsed = json.loads(attempt)
            if not isinstance(parsed, dict):
                continue
            # Ensure required keys exist with safe defaults
            parsed.setdefault("overall_score", 0)
            parsed.setdefault("summary", "")
            parsed.setdefault("critical_issues", [])
            parsed.setdefault("quick_wins", [])
            logger.info("AI response parsed as JSON (overall_score=%s)", parsed["overall_score"])
            return parsed
        except json.JSONDecodeError:
            continue

    logger.warning("All JSON parse attempts failed. Raw response:\n%s", raw[:500])
    return {**_FALLBACK, "raw_response": raw}

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "google/gemini-2.5-flash"

SYSTEM_PROMPT = """You are a senior SEO consultant. Analyse the provided SEO audit metrics and return a JSON object with this exact structure:

{
  "overall_score": <integer 0-100>,
  "summary": "<2-3 sentence executive summary>",
  "critical_issues": [
    {
      "rank": 1,
      "issue": "<issue name>",
      "why_it_matters": "<1-2 sentences on SEO impact>",
      "fix": "<exact, copy-paste-ready fix or step-by-step instructions>",
      "priority_score": <integer 1-100>,
      "estimated_impact": "<e.g. '+15% organic traffic in 60 days'>"
    }
    // ... up to 5 issues
  ],
  "quick_wins": [
    "<action that takes <1 hour and has immediate impact>"
  ]
}

Base ranking on: severity field, count of affected URLs, and typical SEO impact.
IMPORTANT: Return ONLY the raw JSON object. No markdown, no code fences, no explanation before or after. The very first character of your response must be '{' and the last must be '}'."""


def _summarise_metrics(metrics: dict) -> str:
    """Trim metrics to a size safe for the LLM context window."""
    slim = {}
    for key, val in metrics.items():
        if key == "pages_crawled":
            slim[key] = val
            continue
        if isinstance(val, dict):
            slim[key] = {
                "count": val.get("count", 0),
                "severity": val.get("severity", ""),
                "affected_urls_sample": (val.get("affected_urls") or [])[:5],
            }
            # Include extra detail for a few important metrics
            if key == "duplicate_meta_titles" and val.get("duplicate_groups"):
                slim[key]["sample_groups"] = dict(list(val["duplicate_groups"].items())[:3])
            if key == "redirect_chains" and val.get("chains"):
                slim[key]["sample_chains"] = val["chains"][:3]
    return json.dumps(slim, indent=2)


async def analyze_seo(metrics: dict) -> dict:
    api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("No API key found (OPENROUTER_API_KEY or ANTHROPIC_API_KEY)")

    payload = {
        "model": MODEL,
        "max_tokens": 2048,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"SEO audit metrics:\n\n{_summarise_metrics(metrics)}",
            },
        ],
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://seoaudit.io",
        "X-Title": "SEO Audit Tool",
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(OPENROUTER_API_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    raw = data["choices"][0]["message"]["content"].strip()

    return _parse_json_response(raw)
