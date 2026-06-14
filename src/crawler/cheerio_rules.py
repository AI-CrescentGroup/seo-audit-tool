"""
Site-wide SEO crawler — extracts 13 metrics across up to 500 pages.
"""
import asyncio
import logging
import time
from collections import defaultdict
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
            current = await client.send(current.next_request, follow_redirects=False)
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


# ── main entry ────────────────────────────────────────────────────────────────

async def run_full_audit(start_url: str) -> dict:
    """Crawl the site and return all 13 metrics."""
    pages = await crawl_site(start_url)
    crawled_urls = {p["url"] for p in pages}

    return {
        "pages_crawled": len(pages),
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
    }
