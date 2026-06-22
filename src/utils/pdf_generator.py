"""
PDF report generator — builds the PDF in memory, uploads to Supabase Storage,
returns a public URL. Falls back to /tmp on upload failure.
"""
import io
import logging
import os
import re
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

logger = logging.getLogger(__name__)


def _sanitize_recommendation_text(html_text: str) -> str:
    """Remove all HTML tags and escape content for safe PDF rendering."""
    if not html_text:
        return ""
    # Remove all HTML tags
    text = re.sub(r'<[^>]+>', '', html_text)
    # Unescape common HTML entities
    text = text.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
    return text.strip()

METRIC_LABELS = {
    "http_errors":                      "4xx / 5xx Errors",
    "missing_h1":                       "Missing H1 Tags",
    "missing_meta_title":               "Missing Meta Titles",
    "duplicate_meta_titles":            "Duplicate Meta Titles",
    "missing_meta_description":         "Missing Meta Descriptions",
    "duplicate_meta_descriptions":      "Duplicate Meta Descriptions",
    "missing_canonical":                "Missing Canonical Tags",
    "image_alt_gaps":                   "Images Without Alt Text",
    "broken_internal_links":            "Broken Internal Links",
    "orphan_pages":                     "Orphan Pages",
    "mobile_viewport":                  "Missing Viewport Tag",
    "https_check":                      "Non-HTTPS Pages",
    "redirect_chains":                  "Redirect Chains",
    "multiple_h1_tags":                 "Multiple H1 Tags",
    "title_length_issues":              "Title Length Issues",
    "meta_description_length_issues":   "Meta Description Length Issues",
    "mixed_content":                    "Mixed Content (HTTP/HTTPS)",
    "broken_external_links":            "Broken External Links",
    "redirect_loops":                   "Redirect Loops",
    "hreflang_errors":                  "Hreflang Errors",
    "xml_sitemap_issues":               "XML Sitemap Issues",
    "schema_markup_errors":             "Schema Markup Errors",
    "image_file_size_issues":           "Large Uncompressed Images",
}


# ── helpers ────────────────────────────────────────────────────────────────

def _score_color(score: int) -> colors.Color:
    if score >= 70:
        return colors.HexColor("#22c55e")
    if score >= 40:
        return colors.HexColor("#f59e0b")
    return colors.HexColor("#ef4444")


def _extract_score(ai_insights: dict | None) -> int:
    if not ai_insights:
        return 0
    score = ai_insights.get("overall_score")
    if isinstance(score, (int, float)):
        return int(score)
    # Fallback: scan raw text
    raw = str(ai_insights)
    match = re.search(r"(\d{1,3})\s*/\s*100", raw)
    return int(match.group(1)) if match else 0


# ── PDF builder ────────────────────────────────────────────────────────────

def _build_pdf_bytes(domain: str, metrics: dict, ai_insights: dict | None) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=2 * cm, bottomMargin=2 * cm)
    styles = getSampleStyleSheet()
    story = []

    # Header
    story.append(Paragraph("SEO Audit Report", styles["Title"]))
    story.append(Paragraph(f"Domain: {domain}", styles["Normal"]))
    story.append(Paragraph(
        f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        styles["Normal"],
    ))
    story.append(Spacer(1, 0.5 * cm))

    # Score
    score = _extract_score(ai_insights)
    score_style = ParagraphStyle("Score", parent=styles["Heading1"], textColor=_score_color(score))
    story.append(Paragraph(f"Overall SEO Score: {score}/100", score_style))

    # GEO Score (if available)
    geo_score = metrics.get("geo_score", {})
    if isinstance(geo_score, dict) and "average" in geo_score:
        geo_avg = geo_score.get("average", 0)
        geo_color = _score_color(int(geo_avg * 10))  # Scale 0-10 to 0-100
        geo_style = ParagraphStyle("GEOScore", parent=styles["Heading2"], textColor=geo_color)
        story.append(Paragraph(f"GEO Score (LLM Optimization): {geo_avg}/10", geo_style))
        story.append(Spacer(1, 0.3 * cm))

    # AI summary
    if ai_insights and ai_insights.get("summary"):
        story.append(Paragraph(ai_insights["summary"], styles["Normal"]))
    story.append(Spacer(1, 0.5 * cm))

    # Metrics table
    story.append(Paragraph("Audit Metrics", styles["Heading2"]))
    rows = [["Metric", "Issues Found", "Severity"]]
    for key, label in METRIC_LABELS.items():
        m = metrics.get(key, {})
        count = m.get("count", 0)
        sev = m.get("severity", "-")
        rows.append([label, str(count), sev])

    table = Table(rows, colWidths=[7 * cm, 4 * cm, 3 * cm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e40af")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("PADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(table)
    story.append(Spacer(1, 0.5 * cm))

    # Critical issues
    if ai_insights and ai_insights.get("critical_issues"):
        story.append(Paragraph("Critical Issues & Fixes", styles["Heading2"]))
        for issue in ai_insights["critical_issues"][:5]:
            try:
                issue_title = _sanitize_recommendation_text(issue.get('issue', ''))
                priority = issue.get('priority_score', '?')
                story.append(Paragraph(
                    f"<b>#{issue.get('rank', '?')} {issue_title}</b> "
                    f"(Priority: {priority})",
                    styles["Normal"],
                ))
                if issue.get("why_it_matters"):
                    why_text = _sanitize_recommendation_text(issue['why_it_matters'])
                    story.append(Paragraph(f"Why: {why_text}", styles["Normal"]))
                if issue.get("fix"):
                    fix_text = _sanitize_recommendation_text(issue['fix'])
                    story.append(Paragraph(f"Fix: {fix_text}", styles["Normal"]))
                if issue.get("estimated_impact"):
                    impact_text = _sanitize_recommendation_text(issue['estimated_impact'])
                    story.append(Paragraph(f"Impact: {impact_text}", styles["Normal"]))
                story.append(Spacer(1, 0.3 * cm))
            except Exception as e:
                logger.warning(f"Failed to add issue to PDF: {e}")
                continue

    # Quick wins
    if ai_insights and ai_insights.get("quick_wins"):
        story.append(Paragraph("Quick Wins", styles["Heading2"]))
        for win in ai_insights["quick_wins"]:
            story.append(Paragraph(f"• {win}", styles["Normal"]))

    doc.build(story)
    return buf.getvalue()


# ── Supabase upload ────────────────────────────────────────────────────────

def _upload_to_supabase(pdf_bytes: bytes, domain: str, audit_id: str) -> str:
    from supabase import create_client

    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]
    sb = create_client(url, key)

    bucket = "seo-reports"
    try:
        sb.storage.create_bucket(bucket, options={"public": True})
    except Exception:
        pass  # Already exists

    file_path = f"{domain}/{audit_id}/report.pdf"
    sb.storage.from_(bucket).upload(
        file_path,
        pdf_bytes,
        file_options={"content-type": "application/pdf", "upsert": "true"},
    )

    public_url = f"{url}/storage/v1/object/public/{bucket}/{file_path}"
    logger.info("PDF uploaded to Supabase: %s", public_url)
    return public_url


def _save_to_tmp(pdf_bytes: bytes, audit_id: str) -> str:
    out_dir = os.environ.get("PDF_OUTPUT_DIR", "/tmp/seo-reports")
    os.makedirs(out_dir, exist_ok=True)
    path = f"{out_dir}/{audit_id}.pdf"
    with open(path, "wb") as f:
        f.write(pdf_bytes)
    return path


# ── public entry point ─────────────────────────────────────────────────────

async def generate_and_store_pdf(domain: str, audit_id: str, metrics: dict, ai_insights: dict | None):
    """
    Build PDF in memory, upload to Supabase Storage, return public URL.
    Falls back to /tmp path if Supabase upload fails.
    Returns None if PDF generation fails (audit continues without PDF).
    """
    try:
        pdf_bytes = _build_pdf_bytes(domain, metrics, ai_insights)
    except Exception as exc:
        logger.error("PDF generation failed for %s (%s): %s", audit_id, domain, exc, exc_info=True)
        return None

    try:
        return _upload_to_supabase(pdf_bytes, domain, audit_id)
    except Exception as exc:
        logger.warning("Supabase PDF upload failed (%s), falling back to /tmp", exc)
        try:
            return _save_to_tmp(pdf_bytes, audit_id)
        except Exception as fallback_exc:
            logger.error("PDF fallback storage also failed for %s: %s", audit_id, fallback_exc)
            return None
