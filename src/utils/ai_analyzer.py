"""
AI-powered SEO recommendations via OpenRouter using optimized Gemini 2.5 Flash constraints.
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
    """Remove markdown code fences Gemini sometimes adds despite instructions."""
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```$", "", text, flags=re.MULTILINE)
    return text.strip()


def _extract_json_block(text: str) -> str:
    """Pull the first {...} block from text as a last-resort fallback."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return match.group(0) if match else text


def _repair_truncated_json(text: str) -> str:
    """
    Attempts to structurally close a truncated JSON block string cut off by token limit bounds
    by matching bracket weights back down to balance point zero.
    """
    text = text.strip()
    if not text.startswith("{"):
        return text

    # Remove trailing commas or incomplete string properties right at the truncation border
    text = re.sub(r",\s*$", "", text)
    text = re.sub(r'"[^"\n]*$', "", text)
    text = re.sub(r':\s*$', ' : null', text)
    
    open_braces = text.count("{")
    close_braces = text.count("}")
    open_brackets = text.count("[")
    close_brackets = text.count("]")

    # Append structural balance padding closes
    if open_brackets > close_brackets:
        text += "]" * (open_brackets - close_brackets)
    if open_braces > close_braces:
        text += "}" * (open_braces - close_braces)
        
    return text


def _parse_json_response(raw: str) -> dict:
    """
    Try increasingly aggressive strategies to extract valid JSON from the
    model response, then validate it has the required keys.
    """
    candidates = [
        raw,                                  # 1. as-is
        _strip_fences(raw),                   # 2. strip markdown fences
        _extract_json_block(raw),             # 3. pull first {...} block
        _repair_truncated_json(_strip_fences(raw)), # 4. repair stripped code
        _repair_truncated_json(_extract_json_block(raw)) # 5. repair fallback block
    ]

    for attempt in candidates:
        try:
            parsed = json.loads(attempt)
            if not isinstance(parsed, dict):
                continue
            # Ensure required keys exist with safe defaults
            parsed.setdefault("overall_score", 50)
            parsed.setdefault("summary", "Technical SEO analysis completed.")
            parsed.setdefault("critical_issues", [])
            parsed.setdefault("quick_wins", [])
            
            # Format and normalize validation items inside arrays
            for idx, issue in enumerate(parsed.get("critical_issues", [])):
                if isinstance(issue, dict):
                    issue.setdefault("rank", idx + 1)
                    issue.setdefault("issue", "Technical SEO Defect")
                    issue.setdefault("why_it_matters", "Negatively impacts crawl budget and structural performance.")
                    issue.setdefault("fix", "Review source implementation files.")
                    issue.setdefault("priority_score", 50)
                    issue.setdefault("estimated_impact", "Improves organic crawl efficiency.")
                    
            logger.info("AI response parsed as JSON (overall_score=%s)", parsed["overall_score"])
            return parsed
        except json.JSONDecodeError:
            continue

    logger.warning("All JSON parse attempts failed. Raw response:\n%s", raw[:750])
    return {**_FALLBACK, "raw_response": raw}


OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "google/gemini-2.5-flash"

SYSTEM_PROMPT = """You are an elite, brutally practical Technical SEO Systems Architect. Your job is to analyze site crawl metrics and return a single valid JSON object.

CRITICAL RULES:
1. NO GENERIC FLUFF OR TEXTBOOK DEFINITIONS. Never write sentences like "Meta titles are important for search engines." Assume the user knows SEO definitions perfectly.
2. DO NOT EXPLAIN THE METRIC. Focus 100% on the localized data payload provided. Diagnose *where* and *what* is failing based strictly on the sample URLs.
3. CODE-LEVEL WORKFLOW FIXES: The "fix" field must contain exact, developer-ready actionable instructions, explicit script/HTML configuration rules, or concrete structural resolution workflows referencing the affected paths.
4. QUANTIFIABLE PROJECTIONS: The "estimated_impact" field must contain specific, logical organic growth or structural crawl-budget recovery projections.

Return a JSON object with this exact structure:
{
  "overall_score": <integer 0-100 based on severity and total unique url density faults>,
  "summary": "<2-3 sentence strategic executive summary focusing exclusively on site architecture weaknesses>",
  "critical_issues": [
    {
      "rank": 1,
      "issue": "<issue name matching the technical metric field>",
      "why_it_matters": "<1-2 sentences stating the immediate technical damage to crawl efficiency, core web vitals, or indexing context>",
      "fix": "<precise, developer-focused fix or specific step-by-step technical recovery commands tailored to the metrics data>",
      "priority_score": <integer 1-100 matching data fault frequency and severity>,
      "estimated_impact": "<measurable structural, crawl budget, or organic traffic optimization metric target>"
    }
  ],
  "quick_wins": [
    "<highly tactical, immediate engineering deployment item resolving a high-severity technical error block in under 60 minutes>"
  ]
}

IMPORTANT: Return ONLY the raw JSON object. No markdown backticks, no code fences, no introductory or concluding statements. The first character must be '{' and the last character must be '}'."""


def _summarise_metrics(metrics: dict) -> str:
    """Trim metrics and expand localized target arrays passed to the LLM."""
    slim = {}
    for key, val in metrics.items():
        if key in ["pages_crawled", "total_pages", "crawl_duration"]:
            slim[key] = val
            continue
        if isinstance(val, dict):
            # Pass up to 8 localized sample paths to provide the AI engine exact data context
            slim[key] = {
                "count": val.get("count", 0),
                "severity": val.get("severity", "medium"),
                "affected_urls_sample": (val.get("affected_urls") or [])[:8],
            }
            # Include extra localized breakdown matrices for advanced parameters
            if key == "duplicate_meta_titles" and val.get("duplicate_groups"):
                slim[key]["sample_groups"] = dict(list(val["duplicate_groups"].items())[:4])
            if key == "redirect_chains" and val.get("chains"):
                slim[key]["sample_chains"] = val["chains"][:4]
    return json.dumps(slim, indent=2)


async def analyze_seo(metrics: dict) -> dict:
    api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("No API key found (OPENROUTER_API_KEY or ANTHROPIC_API_KEY)")

    payload = {
        "model": MODEL,
        "max_tokens": 4000,           # Higher headroom protects detailed audit scopes from cutting off
        "temperature": 0.1,          # Minimal value guarantees maximum schema contract adherence
        "response_format": {"type": "json_object"}, # Native structural JSON parsing mode execution
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Execute architecture diagnostic optimization based on this localized site metrics dataset:\n\n{_summarise_metrics(metrics)}",
            },
        ],
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://seo-audit-tool-frontend.vercel.app",
        "X-Title": "SEO Technical Audit Engine",
    }

    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(OPENROUTER_API_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    if "choices" not in data or not data["choices"]:
        logger.error("No valid choices payload structured from OpenRouter: %s", data)
        return _FALLBACK

    raw = data["choices"][0]["message"]["content"].strip()

    return _parse_json_response(raw)