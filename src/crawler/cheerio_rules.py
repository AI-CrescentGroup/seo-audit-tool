"""
Site-wide SEO crawler — extracts 23 metrics across up to 500 pages.
"""
import asyncio
import json
import logging
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

MAX_PAGES = 500
CONCURRENCY = 10
HEADERS = {"User-Agent": "SEOAuditBot/1.0 (+https://seoaudit.io)"}
TIMEOUT = 20


# ── helpers ──────────────────────────────────────────────────────────────────

def _norm(url: str) -> str:
    """Normalise URL: strip fragment, trailing slash on non-root paths."""
    p = urlparse(url)
    path = p.path.rstrip("/") or "/"
    return p._replace(fragment="", path=path, query=p.query).geturl()


def _same_domain(url: str, domain: str) -> bool:
    host = urlparse(url).netloc
    return host == domain or host == f"www.{domain}" or f"www.{host}" == domain


def _metric(count: int, affected_urls: list, severity: str) -> dict:
    return {"count": count, "affected_urls": affected_urls[:200], "severity": severity}


# ── per-page fetch ────────────────────────────────────────────────────────────

async def _fetch(client: httpx.AsyncClient, url: str) -> dict:
    """Fetch a single URL and return raw page data."""
    start = time.monotonic()
    result = {
        "url": url,
        "status": None,
        "html": None,
        "final_url": url,
        "redirect_chain": [],
        "load_ms": None,
        "error": None,
    }
    try:
        # Follow redirects manually to capture chains
        resp = await client.get(url, headers=HEADERS, follow_redirects=False)
        chain = []
        current = resp
        while current.is_redirect and len(chain) < 10:
            chain.append(str(current.url))
            next_request = current.next_request
            if next_request is None:
                break
            current = await client.send(next_request, follow_redirects=False)
        result["redirect_chain"] = chain
        result["final_url"] = str(current.url)
        result["status"] = current.status_code
        result["load_ms"] = round((time.monotonic() - start) * 1000, 1)
        if current.status_code == 200:
            result["html"] = current.text
    except Exception as exc:
        result["error"] = str(exc)
    return result


# ── crawler ───────────────────────────────────────────────────────────────────

async def crawl_site(start_url: str) -> list[dict]:
    """
    BFS crawl starting from start_url.
    Returns list of page dicts (url, status, html, redirect_chain, load_ms, error).
    """
    parsed = urlparse(start_url)
    domain = parsed.netloc.lstrip("www.")
    visited: set[str] = set()
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait(_norm(start_url))
    results: list[dict] = []
    sem = asyncio.Semaphore(CONCURRENCY)

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
        while not queue.empty() and len(visited) < MAX_PAGES:
            batch = []
            while not queue.empty() and len(batch) < CONCURRENCY:
                url = await queue.get()
                if url not in visited:
                    visited.add(url)
                    batch.append(url)

            if not batch:
                break

            async def fetch_one(u):
                async with sem:
                    return await _fetch(client, u)

            pages = await asyncio.gather(*[fetch_one(u) for u in batch])

            for page in pages:
                results.append(page)
                if page["html"] and page["status"] == 200:
                    soup = BeautifulSoup(page["html"], "html.parser")
                    for a in soup.find_all("a", href=True):
                        href = a["href"].strip()
                        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
                            continue
                        full = _norm(urljoin(page["url"], href))
                        if _same_domain(full, domain) and full not in visited:
                            queue.put_nowait(full)

    logger.info("Crawled %d pages for %s", len(results), domain)
    return results


# ── 13 metric extractors ──────────────────────────────────────────────────────

def metric_http_errors(pages: list[dict]) -> dict:
    """404 and 5xx errors."""
    affected = [
        p["url"] for p in pages
        if p["status"] and (p["status"] == 404 or p["status"] >= 500)
    ]
    severity = "high" if affected else "low"
    return _metric(len(affected), affected, severity)


def metric_missing_h1(pages: list[dict]) -> dict:
    affected = []
    for p in pages:
        if not p["html"]:
            continue
        soup = BeautifulSoup(p["html"], "html.parser")
        if not soup.find("h1"):
            affected.append(p["url"])
    severity = "high" if len(affected) > 5 else "medium" if affected else "low"
    return _metric(len(affected), affected, severity)


def metric_missing_meta_title(pages: list[dict]) -> dict:
    affected = []
    for p in pages:
        if not p["html"]:
            continue
        soup = BeautifulSoup(p["html"], "html.parser")
        tag = soup.find("title")
        if not tag or not tag.get_text(strip=True):
            affected.append(p["url"])
    severity = "high" if affected else "low"
    return _metric(len(affected), affected, severity)


def metric_duplicate_meta_titles(pages: list[dict]) -> dict:
    title_map: dict[str, list[str]] = defaultdict(list)
    for p in pages:
        if not p["html"]:
            continue
        soup = BeautifulSoup(p["html"], "html.parser")
        tag = soup.find("title")
        if tag and tag.get_text(strip=True):
            title_map[tag.get_text(strip=True).lower()].append(p["url"])
    dupes = {t: urls for t, urls in title_map.items() if len(urls) > 1}
    affected = [url for urls in dupes.values() for url in urls]
    severity = "medium" if dupes else "low"
    return {
        "count": len(dupes),
        "affected_urls": affected[:200],
        "duplicate_groups": {t: urls for t, urls in list(dupes.items())[:50]},
        "severity": severity,
    }


def metric_missing_meta_description(pages: list[dict]) -> dict:
    affected = []
    for p in pages:
        if not p["html"]:
            continue
        soup = BeautifulSoup(p["html"], "html.parser")
        tag = soup.find("meta", attrs={"name": "description"})
        if not tag or not (tag.get("content") or "").strip():
            affected.append(p["url"])
    severity = "high" if len(affected) > 5 else "medium" if affected else "low"
    return _metric(len(affected), affected, severity)


def metric_duplicate_meta_descriptions(pages: list[dict]) -> dict:
    desc_map: dict[str, list[str]] = defaultdict(list)
    for p in pages:
        if not p["html"]:
            continue
        soup = BeautifulSoup(p["html"], "html.parser")
        tag = soup.find("meta", attrs={"name": "description"})
        content = (tag.get("content") or "").strip().lower() if tag else ""
        if content:
            desc_map[content].append(p["url"])
    dupes = {d: urls for d, urls in desc_map.items() if len(urls) > 1}
    affected = [url for urls in dupes.values() for url in urls]
    severity = "medium" if dupes else "low"
    return {
        "count": len(dupes),
        "affected_urls": affected[:200],
        "duplicate_groups": {d[:80]: urls for d, urls in list(dupes.items())[:50]},
        "severity": severity,
    }


def metric_missing_canonical(pages: list[dict]) -> dict:
    affected = []
    for p in pages:
        if not p["html"]:
            continue
        soup = BeautifulSoup(p["html"], "html.parser")
        if not soup.find("link", attrs={"rel": "canonical"}):
            affected.append(p["url"])
    severity = "medium" if len(affected) > 10 else "low" if not affected else "low"
    return _metric(len(affected), affected, severity)


def metric_image_alt_gaps(pages: list[dict]) -> dict:
    missing_alts: list[dict] = []
    for p in pages:
        if not p["html"]:
            continue
        soup = BeautifulSoup(p["html"], "html.parser")
        for img in soup.find_all("img"):
            if not (img.get("alt") or "").strip():
                missing_alts.append({
                    "page": p["url"],
                    "src": img.get("src", "")[:200],
                })
    severity = "medium" if len(missing_alts) > 10 else "low" if not missing_alts else "low"
    return {
        "count": len(missing_alts),
        "affected_urls": list({m["page"] for m in missing_alts})[:200],
        "images": missing_alts[:100],
        "severity": severity,
    }


def metric_broken_internal_links(pages: list[dict], crawled_urls: set[str]) -> dict:
    broken: list[dict] = []
    error_statuses = {p["url"]: p["status"] for p in pages if p["status"]}

    for p in pages:
        if not p["html"]:
            continue
        soup = BeautifulSoup(p["html"], "html.parser")
        parsed = urlparse(p["url"])
        domain = parsed.netloc.lstrip("www.")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            full = _norm(urljoin(p["url"], href))
            if _same_domain(full, domain):
                status = error_statuses.get(full)
                if status and (status == 404 or status >= 500):
                    broken.append({"source": p["url"], "target": full, "status": status})

    affected = list({b["source"] for b in broken})
    severity = "high" if broken else "low"
    return {
        "count": len(broken),
        "affected_urls": affected[:200],
        "broken_links": broken[:100],
        "severity": severity,
    }


def metric_orphan_pages(pages: list[dict]) -> dict:
    """Pages that are not linked to by any other crawled page."""
    linked_to: set[str] = set()
    for p in pages:
        if not p["html"]:
            continue
        soup = BeautifulSoup(p["html"], "html.parser")
        parsed = urlparse(p["url"])
        domain = parsed.netloc.lstrip("www.")
        for a in soup.find_all("a", href=True):
            full = _norm(urljoin(p["url"], a["href"].strip()))
            if _same_domain(full, domain):
                linked_to.add(full)

    all_urls = {p["url"] for p in pages if p["html"]}
    # Exclude start page (it's the entry point, expected to have 0 inbound)
    start = min(pages, key=lambda x: len(x["url"]), default=None)
    start_url = start["url"] if start else ""
    orphans = [u for u in all_urls if u not in linked_to and u != start_url]
    severity = "medium" if len(orphans) > 5 else "low" if not orphans else "low"
    return _metric(len(orphans), orphans, severity)


def metric_mobile_viewport(pages: list[dict]) -> dict:
    """Check if every page has a viewport meta tag."""
    missing = []
    for p in pages:
        if not p["html"]:
            continue
        soup = BeautifulSoup(p["html"], "html.parser")
        if not soup.find("meta", attrs={"name": "viewport"}):
            missing.append(p["url"])
    severity = "high" if missing else "low"
    return {
        "count": len(missing),
        "affected_urls": missing[:200],
        "all_pages_have_viewport": len(missing) == 0,
        "severity": severity,
    }


def metric_https(pages: list[dict]) -> dict:
    """Percentage of pages served over HTTPS."""
    total = len(pages)
    https_count = sum(1 for p in pages if p["url"].startswith("https://"))
    pct = round(https_count / total * 100, 1) if total else 0
    non_https = [p["url"] for p in pages if not p["url"].startswith("https://")]
    severity = "high" if pct < 100 else "low"
    return {
        "count": len(non_https),
        "affected_urls": non_https[:200],
        "https_percentage": pct,
        "severity": severity,
    }


def metric_redirect_chains(pages: list[dict]) -> dict:
    """Pages whose URL caused a redirect chain (2+ hops)."""
    chains = [
        {"url": p["url"], "chain": p["redirect_chain"], "final": p["final_url"]}
        for p in pages
        if len(p.get("redirect_chain") or []) >= 2
    ]
    severity = "medium" if chains else "low"
    return {
        "count": len(chains),
        "affected_urls": [c["url"] for c in chains][:200],
        "chains": chains[:50],
        "severity": severity,
    }


# ── 10 new metrics ────────────────────────────────────────────────────────────

def metric_multiple_h1_tags(pages: list[dict]) -> dict:
    """Pages with more than one <h1> tag."""
    affected = []
    for p in pages:
        if not p["html"]:
            continue
        soup = BeautifulSoup(p["html"], "html.parser")
        h1_count = len(soup.find_all("h1"))
        if h1_count > 1:
            affected.append(p["url"])
    severity = "medium" if len(affected) > 5 else "low" if affected else "low"
    return _metric(len(affected), affected, severity)


def metric_title_length_issues(pages: list[dict]) -> dict:
    """Pages with title tag length outside 50-60 char range."""
    affected = []
    for p in pages:
        if not p["html"]:
            continue
        soup = BeautifulSoup(p["html"], "html.parser")
        tag = soup.find("title")
        if tag and tag.get_text(strip=True):
            length = len(tag.get_text(strip=True))
            if length < 50 or length > 60:
                affected.append(p["url"])
    severity = "medium" if len(affected) > 5 else "low" if affected else "low"
    return _metric(len(affected), affected, severity)


def metric_meta_description_length_issues(pages: list[dict]) -> dict:
    """Pages with meta description length outside 120-158 char range."""
    affected = []
    for p in pages:
        if not p["html"]:
            continue
        soup = BeautifulSoup(p["html"], "html.parser")
        tag = soup.find("meta", attrs={"name": "description"})
        if tag and (tag.get("content") or "").strip():
            length = len((tag.get("content") or "").strip())
            if length < 120 or length > 158:
                affected.append(p["url"])
    severity = "medium" if len(affected) > 5 else "low" if affected else "low"
    return _metric(len(affected), affected, severity)


def metric_mixed_content(pages: list[dict]) -> dict:
    """HTTPS pages with HTTP resources (img/script/link/css)."""
    affected = []
    for p in pages:
        if not p["html"] or not p["url"].startswith("https://"):
            continue
        soup = BeautifulSoup(p["html"], "html.parser")
        has_http_resource = False
        # Check img src
        for img in soup.find_all("img"):
            src = (img.get("src") or "").strip()
            if src.startswith("http://"):
                has_http_resource = True
                break
        # Check script src
        if not has_http_resource:
            for script in soup.find_all("script"):
                src = (script.get("src") or "").strip()
                if src.startswith("http://"):
                    has_http_resource = True
                    break
        # Check link href (stylesheet, etc.)
        if not has_http_resource:
            for link in soup.find_all("link"):
                href = (link.get("href") or "").strip()
                if href.startswith("http://"):
                    has_http_resource = True
                    break
        if has_http_resource:
            affected.append(p["url"])
    severity = "high" if affected else "low"
    return _metric(len(affected), affected, severity)


async def metric_broken_external_links(pages: list[dict]) -> dict:
    """External links (different domain) returning 4xx/5xx."""
    broken = []
    seen_urls = set()

    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        for p in pages:
            if not p["html"]:
                continue
            soup = BeautifulSoup(p["html"], "html.parser")
            parsed = urlparse(p["url"])
            domain = parsed.netloc.lstrip("www.")

            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if href.startswith(("mailto:", "tel:", "javascript:", "#")):
                    continue
                full = urljoin(p["url"], href)
                if not _same_domain(full, domain) and full not in seen_urls:
                    seen_urls.add(full)
                    try:
                        resp = await client.get(full, headers=HEADERS)
                        if resp.status_code >= 400:
                            broken.append({"source": p["url"], "target": full, "status": resp.status_code})
                    except Exception:
                        pass

    affected = list({b["source"] for b in broken})
    severity = "medium" if broken else "low"
    return {
        "count": len(broken),
        "affected_urls": affected[:200],
        "broken_links": broken[:100],
        "severity": severity,
    }


def metric_redirect_loops(pages: list[dict]) -> dict:
    """Detect redirect loops where URL chain points back to itself."""
    loops = []
    for p in pages:
        chain = p.get("redirect_chain") or []
        if not chain:
            continue
        urls_in_chain = [p["url"]] + chain
        # Check if any URL appears twice
        if len(urls_in_chain) != len(set(urls_in_chain)):
            loops.append({"url": p["url"], "chain": chain})
    severity = "high" if loops else "low"
    return {
        "count": len(loops),
        "affected_urls": [l["url"] for l in loops][:200],
        "loops": loops[:50],
        "severity": severity,
    }


def metric_hreflang_errors(pages: list[dict]) -> dict:
    """Hreflang tag issues: missing reciprocal, invalid codes, conflicts."""
    affected = []
    for p in pages:
        if not p["html"]:
            continue
        soup = BeautifulSoup(p["html"], "html.parser")
        hreflangs = soup.find_all("link", attrs={"rel": "hreflang"})

        # Check if hreflang tags exist but have issues
        has_issues = False
        if hreflangs:
            for hl in hreflangs:
                hreflang = (hl.get("hreflang") or "").strip()
                # Basic validation: should be xx or xx-XX format
                if hreflang and not (len(hreflang) == 2 or (len(hreflang) == 5 and hreflang[2] == "-")):
                    has_issues = True
                    break

        if has_issues:
            affected.append(p["url"])

    severity = "medium" if len(affected) > 2 else "low" if affected else "low"
    return _metric(len(affected), affected, severity)


async def metric_xml_sitemap_issues(start_url: str, pages: list[dict]) -> dict:
    """Check for sitemap.xml issues: missing, unreachable, or contains 404 URLs."""
    parsed = urlparse(start_url)
    domain = parsed.netloc.lstrip("www.")
    base = f"{parsed.scheme}://{parsed.netloc}"
    sitemap_url = f"{base}/sitemap.xml"

    issues = []

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(sitemap_url, headers=HEADERS)
            if resp.status_code >= 400:
                issues.append(f"sitemap.xml unreachable (HTTP {resp.status_code})")
            else:
                # Parse sitemap and check if URLs are 404
                try:
                    from xml.etree import ElementTree as ET
                    root = ET.fromstring(resp.content)
                    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
                    urls_in_sitemap = [elem.text for elem in root.findall(".//sm:loc", ns)]

                    crawled_urls = {p["url"] for p in pages}
                    for url in urls_in_sitemap[:20]:  # Check first 20
                        if url not in crawled_urls:
                            for p in pages:
                                if p["url"] == url and p["status"] == 404:
                                    issues.append(f"Sitemap contains 404: {url}")
                                    break
                except Exception:
                    issues.append("sitemap.xml malformed or unreadable")
    except Exception:
        issues.append("sitemap.xml missing or unreachable")

    severity = "medium" if issues else "low"
    return {
        "count": len(issues),
        "affected_urls": [],
        "issues": issues[:10],
        "severity": severity,
    }


def metric_schema_markup_errors(pages: list[dict]) -> dict:
    """JSON-LD structured data that's malformed or invalid."""
    affected = []
    for p in pages:
        if not p["html"]:
            continue
        soup = BeautifulSoup(p["html"], "html.parser")
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                json.loads(script.string or "")
            except Exception:
                affected.append(p["url"])
                break
    severity = "medium" if len(affected) > 5 else "low" if affected else "low"
    return _metric(len(affected), affected, severity)


async def metric_image_file_size_issues(pages: list[dict]) -> dict:
    """Images over 200KB not in next-gen format (WebP/AVIF)."""
    issues = []
    seen_urls = set()

    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        for p in pages:
            if not p["html"]:
                continue
            soup = BeautifulSoup(p["html"], "html.parser")
            for img in soup.find_all("img"):
                src = (img.get("src") or "").strip()
                if not src or src in seen_urls:
                    continue
                seen_urls.add(src)

                full_url = urljoin(p["url"], src)
                try:
                    resp = await client.head(full_url, headers=HEADERS)
                    size_bytes = int(resp.headers.get("content-length", 0))
                    content_type = resp.headers.get("content-type", "").lower()

                    if size_bytes > 200 * 1024:  # Over 200KB
                        if "webp" not in content_type and "avif" not in content_type:
                            issues.append({
                                "page": p["url"],
                                "src": src[:100],
                                "size_kb": round(size_bytes / 1024, 1),
                            })
                except Exception:
                    pass

    affected = list({issue["page"] for issue in issues})
    severity = "low" if len(affected) > 10 else "low"
    return {
        "count": len(issues),
        "affected_urls": affected[:200],
        "large_images": issues[:50],
        "severity": severity,
    }


# ── GEO (Generative Engine Optimization) scoring ────────────────────────────────

def _calculate_readability_score(text: str) -> int:
    """
    Flesch-Kincaid grade level. Returns 0–2 points.
    Grade 8 or lower = 2, 8–12 = 1, >12 = 0 (simpler is better for LLMs).
    """
    if not text or len(text) < 50:
        return 0

    words = text.split()
    sentences = len(re.split(r'[.!?]+', text)) - 1
    syllables = sum(_count_syllables(word) for word in words)

    if sentences == 0 or len(words) == 0:
        return 0

    grade = (0.39 * (len(words) / max(sentences, 1))) + (11.8 * (syllables / max(len(words), 1))) - 15.59
    grade = max(0, grade)

    if grade <= 8:
        return 2
    elif grade <= 12:
        return 1
    else:
        return 0


def _count_syllables(word: str) -> int:
    """Rough syllable counter."""
    word = word.lower()
    vowels = "aeiouy"
    syl_count = 0
    previous_was_vowel = False
    for char in word:
        is_vowel = char in vowels
        if is_vowel and not previous_was_vowel:
            syl_count += 1
        previous_was_vowel = is_vowel
    if word.endswith("e"):
        syl_count -= 1
    if word.endswith("le") and len(word) > 2 and word[-3] not in vowels:
        syl_count += 1
    return max(1, syl_count)


def _extract_entity_names(text: str) -> int:
    """Count capitalized proper nouns (basic entity detection)."""
    words = text.split()
    entity_count = 0
    for word in words:
        # Basic heuristic: all-caps or starts with capital and has 2+ chars
        if word and (word[0].isupper() and len(word) > 2):
            if not word.endswith((".", ",", "!", "?")):
                entity_count += 1
    return entity_count


def _score_heading_structure(soup: BeautifulSoup) -> int:
    """Score heading hierarchy: 0–3 points."""
    h1_count = len(soup.find_all("h1"))
    h2_count = len(soup.find_all("h2"))
    h3_count = len(soup.find_all("h3"))

    score = 0
    if h1_count == 1:
        score += 1
    if h2_count > 0 and h1_count == 1:
        score += 1
    if h3_count > 0 and h2_count > 0:
        score += 1

    return min(3, score)


def _extract_content_freshness_score(html: str, response_headers: dict) -> int:
    """Extract last-modified date. Returns 0–2 points."""
    score = 0

    # Check HTTP Last-Modified header
    if "last-modified" in response_headers:
        try:
            from email.utils import parsedate_to_datetime
            mod_date = parsedate_to_datetime(response_headers["last-modified"])
            days_old = (datetime.utcnow() - mod_date.replace(tzinfo=None)).days
            if days_old <= 90:
                return 2
            elif days_old <= 365:
                return 1
        except Exception:
            pass

    # Check <meta name="date-published"> or <meta name="last-modified">
    soup = BeautifulSoup(html, "html.parser")
    for meta_name in ["date-published", "last-modified", "article:published_time"]:
        meta = soup.find("meta", attrs={"name": meta_name})
        if not meta:
            meta = soup.find("meta", attrs={"property": meta_name})
        if meta:
            try:
                date_str = meta.get("content", "")
                # Try parsing ISO format
                date_obj = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                days_old = (datetime.utcnow() - date_obj.replace(tzinfo=None)).days
                if days_old <= 90:
                    return 2
                elif days_old <= 365:
                    return 1
            except Exception:
                pass

    return 0


def _score_answer_density(text: str, first_n_words: int = 100) -> int:
    """
    Check if first N words answer common queries.
    Count how many question words (who/what/when/where/why/how) are answered.
    Returns 0–5 points.
    """
    words = text.split()[:first_n_words]
    first_100 = " ".join(words).lower()

    # Look for direct answers: "is", "are", "provides", "includes", "offers", "contains"
    direct_answer_patterns = [
        r"\b(is|are|provides|includes|offers|contains|explains|shows|demonstrates|enables|allows)\b",
    ]

    score = 0
    if any(re.search(pattern, first_100) for pattern in direct_answer_patterns):
        score += 2

    # Check for question words being addressed
    question_words = ["who", "what", "when", "where", "why", "how"]
    for qword in question_words:
        if qword in first_100:
            score += 0.5

    return min(5, int(score))


async def _calculate_geo_score_for_page(
    page: dict, response_headers: dict = None
) -> dict:
    """Calculate GEO score (0–10) and signals for a single page."""
    page_url = page.get("url", "unknown")

    if not page["html"]:
        logger.debug(f"GEO: skipping {page_url} — no HTML content")
        return {
            "url": page_url,
            "geo_score": 0,
            "geo_signals": {},
            "geo_issues": ["No HTML content to analyze"],
        }

    html = page["html"]

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as e:
        logger.warning(f"GEO: BeautifulSoup parse failed for {page_url}: {e}")
        return {
            "url": page_url,
            "geo_score": 0,
            "geo_signals": {},
            "geo_issues": ["Failed to parse HTML"],
        }

    # 1. FAQ Schema Present
    faq_present = False
    try:
        faq_script = soup.find("script", attrs={"type": "application/ld+json"})
        if faq_script:
            try:
                data = json.loads(faq_script.string or "{}")
                faq_present = data.get("@type") == "FAQPage" or "FAQPage" in str(data)
            except json.JSONDecodeError as e:
                logger.debug(f"GEO: FAQ schema parse failed for {page_url}: {e}")
                faq_present = False
    except Exception as e:
        logger.error(f"GEO: FAQ schema detection failed for {page_url}: {e}")
        faq_present = False

    # Extract body text
    try:
        for tag in soup(["script", "style"]):
            tag.decompose()
        body_text = soup.get_text(separator=" ", strip=True)
        body_text = re.sub(r"\s+", " ", body_text)
    except Exception as e:
        logger.warning(f"GEO: text extraction failed for {page_url}: {e}")
        body_text = ""

    # 2. Entity Density
    entity_density = 0
    try:
        entity_count = _extract_entity_names(body_text)
        word_count = len(body_text.split())
        entity_density = (entity_count / word_count * 100) if word_count > 0 else 0
    except Exception as e:
        logger.error(f"GEO: entity density calculation failed for {page_url}: {e}")
        entity_density = 0

    # 3. Answer Density
    answer_density_score = 0
    try:
        answer_density_score = _score_answer_density(body_text, first_n_words=100)
    except Exception as e:
        logger.error(f"GEO: answer density scoring failed for {page_url}: {e}")
        answer_density_score = 0

    # 4. Heading Structure
    heading_structure_score = 0
    try:
        heading_structure_score = _score_heading_structure(soup)
    except Exception as e:
        logger.error(f"GEO: heading structure scoring failed for {page_url}: {e}")
        heading_structure_score = 0

    # 5. Content Freshness
    content_freshness_score = 0
    try:
        headers = response_headers or {}
        content_freshness_score = _extract_content_freshness_score(html, headers)
    except Exception as e:
        logger.error(f"GEO: content freshness scoring failed for {page_url}: {e}")
        content_freshness_score = 0

    # 6. Readability
    readability_score = 0
    try:
        readability_score = _calculate_readability_score(body_text)
    except Exception as e:
        logger.error(f"GEO: readability scoring failed for {page_url}: {e}")
        readability_score = 0

    # Combine into GEO Score (0–10)
    try:
        geo_score = round(
            (faq_present * 2)
            + min(2, entity_density / 3)
            + (answer_density_score / 2.5)
            + heading_structure_score
            + content_freshness_score
            + readability_score,
            1,
        )
    except Exception as e:
        logger.error(f"GEO: score aggregation failed for {page_url}: {e}")
        geo_score = 0

    # Generate issues
    geo_issues = []
    try:
        if not faq_present:
            geo_issues.append("Missing FAQ schema — add FAQPage JSON-LD to improve LLM citability")
        if entity_density < 3:
            geo_issues.append(
                f"Entity density low ({entity_density:.1f} per 100 words) — mention more named entities"
            )
        if answer_density_score < 3:
            geo_issues.append(
                "Answer density low — provide direct answers to target queries in opening 100 words"
            )
        if heading_structure_score < 2:
            geo_issues.append("Heading structure needs improvement — use logical H1/H2/H3 hierarchy")
        if content_freshness_score == 0:
            geo_issues.append("Content appears outdated — update last-modified date or publish metadata")
        if readability_score < 2:
            geo_issues.append("Readability too high (grade >12) — simplify language for LLM consumption")
    except Exception as e:
        logger.error(f"GEO: issue generation failed for {page_url}: {e}")

    logger.debug(f"GEO: {page_url} scored {geo_score}/10 (FAQ:{faq_present}, entity:{entity_density:.1f}, answer:{answer_density_score}, heading:{heading_structure_score}, freshness:{content_freshness_score}, readability:{readability_score})")

    return {
        "url": page_url,
        "geo_score": geo_score,
        "geo_signals": {
            "faq_schema_present": faq_present,
            "entity_density": round(entity_density, 2),
            "answer_density_score": answer_density_score,
            "heading_structure_score": heading_structure_score,
            "content_freshness_score": content_freshness_score,
            "readability_score": readability_score,
        },
        "geo_issues": geo_issues[:5],
    }


# ── main entry ────────────────────────────────────────────────────────────────

async def run_full_audit(start_url: str) -> dict:
    """Crawl the site and return all 23 metrics + GEO scores per page."""
    pages = await crawl_site(start_url)
    crawled_urls = {p["url"] for p in pages}

    # Run async metrics concurrently
    async_results = await asyncio.gather(
        metric_broken_external_links(pages),
        metric_xml_sitemap_issues(start_url, pages),
        metric_image_file_size_issues(pages),
    )

    # TEMPORARILY DISABLED FOR DEBUGGING
    # Calculate GEO scores for each page with error handling
    # logger.info(f"Starting GEO scoring for {len(pages)} pages")
    # geo_scores = []
    # geo_failed_count = 0
    #
    # try:
    #     geo_results = await asyncio.gather(
    #         *[_calculate_geo_score_for_page(p, {}) for p in pages],
    #         return_exceptions=True,  # Don't fail on individual page errors
    #     )
    #
    #     for idx, result in enumerate(geo_results):
    #         if isinstance(result, Exception):
    #             page_url = pages[idx].get("url", f"page_{idx}") if idx < len(pages) else f"page_{idx}"
    #             logger.error(f"GEO scoring crashed for {page_url}: {result}")
    #             geo_failed_count += 1
    #             # Create a fallback score for this page
    #             geo_scores.append({
    #                 "url": page_url,
    #                 "geo_score": 0,
    #                 "geo_signals": {},
    #                 "geo_issues": [f"GEO scoring error: {str(result)[:100]}"],
    #             })
    #         else:
    #             geo_scores.append(result)
    # except Exception as e:
    #     logger.exception(f"CRITICAL: GEO scoring batch processing failed: {e}")
    #     geo_scores = []
    #     geo_failed_count = len(pages)
    #
    # # Aggregate GEO scores
    # avg_geo_score = 0
    # try:
    #     if geo_scores:
    #         valid_scores = [g.get("geo_score", 0) for g in geo_scores if isinstance(g, dict)]
    #         avg_geo_score = round(sum(valid_scores) / len(valid_scores), 1) if valid_scores else 0
    # except Exception as e:
    #     logger.error(f"GEO score aggregation failed: {e}")
    #     avg_geo_score = 0
    #
    # logger.info(f"GEO scoring complete: {len(geo_scores) - geo_failed_count} pages scored, {geo_failed_count} failed")

    # Stub values for disabled GEO scoring
    geo_scores = []
    avg_geo_score = 0
    logger.info("GEO scoring DISABLED for debugging")

    return {
        "pages_crawled": len(pages),
        # Original 13 metrics
        "http_errors": metric_http_errors(pages),
        "missing_h1": metric_missing_h1(pages),
        "missing_meta_title": metric_missing_meta_title(pages),
        "duplicate_meta_titles": metric_duplicate_meta_titles(pages),
        "missing_meta_description": metric_missing_meta_description(pages),
        "duplicate_meta_descriptions": metric_duplicate_meta_descriptions(pages),
        "missing_canonical": metric_missing_canonical(pages),
        "image_alt_gaps": metric_image_alt_gaps(pages),
        "broken_internal_links": metric_broken_internal_links(pages, crawled_urls),
        "orphan_pages": metric_orphan_pages(pages),
        "mobile_viewport": metric_mobile_viewport(pages),
        "https_check": metric_https(pages),
        "redirect_chains": metric_redirect_chains(pages),
        # New 10 metrics
        "multiple_h1_tags": metric_multiple_h1_tags(pages),
        "title_length_issues": metric_title_length_issues(pages),
        "meta_description_length_issues": metric_meta_description_length_issues(pages),
        "mixed_content": metric_mixed_content(pages),
        "broken_external_links": async_results[0],
        "redirect_loops": metric_redirect_loops(pages),
        "hreflang_errors": metric_hreflang_errors(pages),
        "xml_sitemap_issues": async_results[1],
        "schema_markup_errors": metric_schema_markup_errors(pages),
        "image_file_size_issues": async_results[2],
        # GEO Score (Phase 2.1)
        "geo_score": {
            "average": avg_geo_score,
            "per_page": geo_scores[:100],  # Limit to first 100 for response size
        },
    }
