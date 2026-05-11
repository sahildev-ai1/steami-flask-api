"""
routers/newsletter.py  —  Newsletter system (v4 — Custom Builder)
=================================================================
WHAT'S NEW IN V4:
  - POST /api/newsletter/draft/save     — mod saves newsletter draft to MongoDB
  - GET  /api/newsletter/draft          — load saved draft (mod/admin)
  - POST /api/newsletter/draft/chart    — AI chart generation via Ollama
  - POST /api/newsletter/preview-draft  — render draft → HTML (mod/admin)
  - POST /api/newsletter/send-custom    — send drafted newsletter (admin)
  - POST /api/newsletter/subscribe      — PUBLIC subscribe
  - POST /api/newsletter/unsubscribe    — PUBLIC unsubscribe
  (all original endpoints preserved below)

HTML TEMPLATE:
  Matches steami_newsletter_light_v2.html styling:
  blue-tinted light background, Syne/DM-Sans/JetBrains Mono fonts,
  section headers, compact info hierarchy.

CHART GENERATION:
  Uses Ollama (same ollama_agent.py pattern) to:
  1. Generate Python matplotlib/plotly chart code for the article
  2. Execute it in a subprocess sandbox (no arbitrary imports)
  3. Save PNG to /tmp, serve via /api/newsletter/chart/<id>
  4. Return chart URL + AI-written explanation text

ENV VARS (all existing + new):
  BREVO_API_KEY, BREVO_SENDER_EMAIL, BREVO_SENDER_NAME, SITE_URL, SITE_NAME
  OLLAMA_API_KEY  — for chart generation (reuses ollama_agent config)
  OLLAMA_HOST     — optional local ollama host
  OLLAMA_MODEL    — default gemma4:31b-cloud
"""

import os
import uuid
import logging
import re
import base64
import requests as http
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List
import tempfile

from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from mongodb_client import db
from auth import require_admin, require_mod, get_uid

# ── Import AI functions from ollama_agent ─────────────────────────────────────
from ollama_agent import (
    generate_cover_story,
    generate_newsletter_chart,
    CHART_STORE_DIR,
)

log = logging.getLogger(__name__)
router = APIRouter()

# ── Brevo config ──────────────────────────────────────────────────────────────
BREVO_API_KEY      = os.getenv("BREVO_API_KEY", "")
BREVO_SENDER_EMAIL = os.getenv("BREVO_SENDER_EMAIL", "hello@steami.com")
BREVO_SENDER_NAME  = os.getenv("BREVO_SENDER_NAME", "STEAMI Newsletter")
BREVO_API_BASE     = "https://api.brevo.com/v3"
SITE_URL           = os.getenv("SITE_URL",  "https://steami.com")
SITE_NAME          = os.getenv("SITE_NAME", "STEAMI")
# Base URL of this FastAPI backend — used to resolve relative image paths in emails.
# e.g. https://steami-flask-api.onrender.com
API_BASE_URL       = os.getenv("API_BASE_URL", "").rstrip("/")

DRAFT_DOC_ID = "current"   # We only ever keep one active draft in MongoDB


# ═════════════════════════════════════════════════════════════════════════════
# PYDANTIC MODELS
# ═════════════════════════════════════════════════════════════════════════════

class SubscribeBody(BaseModel):
    email: str
    name:  str = ""

class UnsubscribeBody(BaseModel):
    email: str

class TestEmailBody(BaseModel):
    to_email: str
    subject:  Optional[str] = None
    draft:    Optional[dict] = None   # full NewsletterDraft fields (optional)

class AiSubscribeBody(BaseModel):
    email:    str
    name:     str = ""
    source:   Optional[str] = None
    metadata: Optional[dict] = None

class ChartRequest(BaseModel):
    article_id: str
    title:      str
    summary:    str = ""
    domain:     str = "Technology"

class CoverStoryRequest(BaseModel):
    """Full article data sent by the frontend to generate cover story + chart together."""
    article_id:   str
    title:        str
    content:      str = ""
    summary:      str = ""
    domain:       str = "Technology"
    article_url:  str = ""
    fetched_at:   str = ""
    matched_domains: List[str] = []

class NewsletterDraft(BaseModel):
    # Cover signal
    cover_article_id:       str = ""
    cover_article_title:    str = ""
    cover_article_url:      str = ""
    cover_article_date:     str = ""
    cover_insight_summary:  str = ""
    cover_chart_image_url:  str = ""
    cover_chart_explanation:str = ""
    cover_chart_svg_data:   str = ""   # legacy SVG string
    # V5: Chart.js + QuickChart fields
    cover_chart_config:     str = ""   # Chart.js JSON string (for frontend canvas preview)
    cover_chart_png_b64:    str = ""   # PNG base64 data URI from QuickChart.io (for email)
    # AI cover story extras
    cover_headline:         str = ""
    cover_standfirst:       str = ""
    cover_pull_quote:       str = ""
    cover_key_stats:        List[dict] = []
    cover_closing_line:     str = ""
    # Sponsor
    sponsor_name:           str = ""
    sponsor_message:        str = ""
    sponsor_image_url:      str = ""
    sponsor_link:           str = ""
    # Content links
    explainer_id:           str = ""
    explainer_title:        str = ""
    explainer_link:         str = ""
    research_id:            str = ""
    research_title:         str = ""
    research_link:          str = ""
    blog_id:                str = ""
    blog_title:             str = ""
    blog_link:              str = ""
    # Signal briefs
    signal_brief_ids:       List[str] = []
    signal_brief_notes:     str = ""
    # On the radar
    on_the_radar:           str = ""
    radar_events:           List[dict] = []  # structured radar events from frontend
    # Meta
    frontend_url:           str = SITE_URL
    linkedin_url:           str = ""
    ad_website_url:         str = ""
    issue_number:           str = ""


# ═════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%B %d, %Y")

def _brevo_headers() -> dict:
    if not BREVO_API_KEY:
        raise HTTPException(503, detail=(
            "Brevo API key not configured. "
            "Set BREVO_API_KEY in your .env → https://app.brevo.com"
        ))
    return {
        "accept":       "application/json",
        "content-type": "application/json",
        "api-key":      BREVO_API_KEY,
    }

def _send_one_via_brevo(to_email: str, to_name: str, subject: str, html_body: str) -> bool:
    payload = {
        "sender": {"name": BREVO_SENDER_NAME, "email": BREVO_SENDER_EMAIL},
        "to":     [{"email": to_email, "name": to_name or to_email}],
        "subject": subject,
        "htmlContent": html_body,
    }
    try:
        resp = http.post(f"{BREVO_API_BASE}/smtp/email",
                         headers=_brevo_headers(), json=payload, timeout=30)
        if resp.status_code in (200, 201):
            log.info("Brevo sent to %s", to_email)
            return True
        log.error("Brevo failed %s: HTTP %d — %s", to_email, resp.status_code, resp.text[:300])
        return False
    except Exception as e:
        log.error("Brevo exception %s: %s", to_email, e)
        return False

def _brevo_sync_contact(email: str, name: str = ""):
    """Create/update contact in Brevo (best-effort)."""
    if not BREVO_API_KEY:
        return
    try:
        http.post(
            f"{BREVO_API_BASE}/contacts",
            headers=_brevo_headers(),
            json={"email": email, "attributes": {"FIRSTNAME": name}, "updateEnabled": True},
            timeout=10,
        )
    except Exception:
        pass

def _get_subscriber_emails() -> list[dict]:
    docs = (
        db.collection("newsletter_subscribers")
          .where("subscribed", "==", True)
          .stream()
    )
    return [d.to_dict() for d in docs]

def _make_deep_link(frontend_url: str, type_: str, id_: str) -> str:
    base = frontend_url.rstrip("/")
    enc  = id_.replace(" ", "%20")
    if type_ == "insight":   return f"{base}/?insight={enc}"
    if type_ == "explainer": return f"{base}/?explainer={enc}"
    if type_ == "research":  return f"{base}/research?research={enc}"
    if type_ == "blog":      return f"{base}/blog/{enc}"
    return f"{base}/"

def _svg_to_png_base64(svg_data: str) -> str:
    """
    Convert an SVG string → PNG → base64 data URI.
    PNG base64 is universally safe in all email clients (Gmail, Outlook, Apple Mail).

    Tries two methods in order:
      1. cairosvg  — fast, best quality  (pip install cairosvg)
      2. matplotlib + io  — pure Python fallback, renders SVG via svglib if available
    Returns a data:image/png;base64,... URI on success, or "" on failure.
    """
    # ── Method 1: cairosvg ────────────────────────────────────────────────────
    try:
        import cairosvg, io
        png_bytes = cairosvg.svg2png(bytestring=svg_data.encode("utf-8"), output_width=596)
        b64 = base64.b64encode(png_bytes).decode("ascii")
        return f"data:image/png;base64,{b64}"
    except Exception as e:
        log.warning("cairosvg svg→png failed: %s", e)

    # ── Method 2: svglib + reportlab + Pillow ────────────────────────────────
    try:
        import io, tempfile
        from svglib.svglib import svg2rlg
        from reportlab.graphics import renderPM

        with tempfile.NamedTemporaryFile("w", suffix=".svg", delete=False) as tmp:
            tmp.write(svg_data)
            tmp_path = tmp.name

        drawing = svg2rlg(tmp_path)
        Path(tmp_path).unlink(missing_ok=True)
        if drawing:
            buf = io.BytesIO()
            renderPM.drawToFile(drawing, buf, fmt="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return f"data:image/png;base64,{b64}"
    except Exception as e:
        log.warning("svglib svg→png failed: %s", e)

    return ""


def _linkify(text: str) -> str:
    """Convert raw URLs in free-form text to clickable <a> tags."""
    url_re = re.compile(r'(https?://[^\s<>"]+)')
    return url_re.sub(
        r'<a href="\1" target="_blank" rel="noopener" '
        r'style="color:#1d4ed8;text-decoration:underline;">\1</a>',
        text
    )

def _resolve_chart_url(chart_url: str) -> str:
    """
    Ensure chart_url is an absolute URL.

    If chart_url is already absolute (starts with http/https), return it as-is.
    If it is a relative path (e.g. /images/newsletter/charts/xxx.png), prepend
    API_BASE_URL (from the .env API_BASE_URL variable) so the email server can
    fetch it.  Falls back to SITE_URL if API_BASE_URL is not set.
    """
    if not chart_url:
        return chart_url
    if chart_url.startswith("http://") or chart_url.startswith("https://"):
        return chart_url
    base = API_BASE_URL or SITE_URL
    return f"{base}/{chart_url.lstrip('/')}"


def _chart_url_to_img_src(chart_url: str) -> str:
    """
    Convert a chart URL to an email-safe base64 data URI.

    Email clients (Gmail, Outlook, Apple Mail) either block remote images or
    proxy them through their own servers, which can cause images to appear
    broken.  The only universally-reliable approach is to embed the image as a
    base64 data URI directly in the HTML so no external fetch is required.

    Strategy:
      1. Resolve relative paths → absolute URL using API_BASE_URL from .env.
      2. If the URL points to our own /api/newsletter/chart/<id>, try to read
         the file directly from CHART_STORE_DIR on disk (fastest, no HTTP).
      3. Fetch the URL over HTTP and base64-encode the response body.
         Works for any public PNG/JPEG/GIF URL, including Render-hosted files
         like https://steami-flask-api.onrender.com/images/newsletter/charts/*.
      4. Last resort: return the (absolute) URL as-is.  PNG remote URLs still
         render in most clients; this is better than a broken image tag.

    NOTE: SVG remote URLs are silently stripped by Gmail — always convert them.
    """
    if not chart_url:
        return chart_url

    # ── 0. Ensure the URL is absolute ────────────────────────────────────────
    chart_url = _resolve_chart_url(chart_url)

    # ── 1. Try to read file directly from disk via CHART_STORE_DIR ───────────
    m = re.search(r'/api/newsletter/chart/([^/?#]+)', chart_url)
    if m:
        safe_id = re.sub(r'[^a-zA-Z0-9_\-]', '', m.group(1))
        for ext, mime in [("png", "image/png"), ("svg", "image/svg+xml")]:
            fpath = CHART_STORE_DIR / f"{safe_id}.{ext}"
            if fpath.exists():
                try:
                    raw = fpath.read_bytes()
                    b64 = base64.b64encode(raw).decode("ascii")
                    log.info("Chart: base64-encoded from disk (%s)", fpath)
                    return f"data:{mime};base64,{b64}"
                except Exception as e:
                    log.warning("Could not base64-encode chart file %s: %s", fpath, e)

    # ── 2. Fetch over HTTP and base64-encode ──────────────────────────────────
    # This handles /images/newsletter/charts/*.png served by FastAPI StaticFiles
    # as well as any other publicly accessible image URL.
    try:
        resp = http.get(chart_url, timeout=15)
        if resp.status_code == 200:
            ct = resp.headers.get("content-type", "image/png").split(";")[0].strip()
            # Normalise content-type — some servers omit it or return text/plain
            if not ct.startswith("image/"):
                ct = "image/png"
            b64 = base64.b64encode(resp.content).decode("ascii")
            log.info("Chart: fetched and base64-encoded from %s (mime: %s, %d bytes)",
                     chart_url, ct, len(resp.content))
            return f"data:{ct};base64,{b64}"
        else:
            log.warning("Chart fetch returned HTTP %d for %s", resp.status_code, chart_url)
    except Exception as e:
        log.warning("Could not fetch chart URL %s for base64 embedding: %s", chart_url, e)

    # ── 3. Last resort: return absolute URL as-is ────────────────────────────
    log.warning("Chart: falling back to raw URL (may not display in Gmail): %s", chart_url)
    return chart_url

# Month name → abbreviation map for radar parsing
_MONTH_NUMS = {
    "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
    "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
}
_MONTH_ABBR = {
    1:"Jan",2:"Feb",3:"Mar",4:"Apr",5:"May",6:"Jun",
    7:"Jul",8:"Aug",9:"Sep",10:"Oct",11:"Nov",12:"Dec",
}

def _parse_radar_events(text: str) -> list[dict]:
    """
    Parse structured On the Radar text into a list of event dicts.

    Supported formats (one event per blank-line-separated block):
      DD\nMonth\nEvent title and description   (day on first line, month on second)
      DD Month\nEvent description              (day + month on first line)
      Month DD\nEvent description
      Plain text with no date                  (day/month will be empty)

    Returns list of: { day: str, month: str, text: str }
    If no date pattern is found at all, returns [] so caller uses free-form fallback.
    """
    # Split into blocks separated by one or more blank lines
    blocks = [b.strip() for b in re.split(r'\n{2,}', text.strip()) if b.strip()]
    if not blocks:
        return []

    results = []
    date_found = False

    for block in blocks:
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if not lines:
            continue

        day = ""
        month = ""
        body_lines = lines[:]

        # Try: first line is a number (day), second line is a month name
        if len(lines) >= 2 and re.match(r'^\d{1,2}$', lines[0]):
            m = re.match(r'^([A-Za-z]{3,9})$', lines[1])
            if m and m.group(1).lower()[:3] in _MONTH_NUMS:
                day   = lines[0]
                month = m.group(1)[:3].capitalize()
                body_lines = lines[2:]
                date_found = True

        # Try: "DD Month" or "Month DD" on first line
        if not day:
            m = re.match(r'^(\d{1,2})\s+([A-Za-z]{3,9})$', lines[0])
            if m and m.group(2).lower()[:3] in _MONTH_NUMS:
                day   = m.group(1)
                month = m.group(2)[:3].capitalize()
                body_lines = lines[1:]
                date_found = True
            else:
                m2 = re.match(r'^([A-Za-z]{3,9})\s+(\d{1,2})$', lines[0])
                if m2 and m2.group(1).lower()[:3] in _MONTH_NUMS:
                    day   = m2.group(2)
                    month = m2.group(1)[:3].capitalize()
                    body_lines = lines[1:]
                    date_found = True

        body_lines_list = body_lines  # already a list
        heading_txt = body_lines_list[0] if body_lines_list else ""
        desc_txt    = "\n".join(body_lines_list[1:]) if len(body_lines_list) > 1 else ""
        results.append({"day": day, "month": month, "heading": heading_txt, "text": desc_txt})

    # Only return structured list if at least one date was found; otherwise free-form
    return results if date_found else []


# ═════════════════════════════════════════════════════════════════════════════
# HTML BUILDER  (matches steami_newsletter_light_v2.html aesthetic)
# ═════════════════════════════════════════════════════════════════════════════

def _build_custom_html(draft: dict, signal_articles: list[dict], recipient_name: str = "") -> str:
    """Build branded newsletter HTML from a draft dict + fetched signal articles."""

    frontend_url   = draft.get("frontend_url", SITE_URL).rstrip("/")
    issue_number   = draft.get("issue_number", "")
    issue_label    = f"Issue #{issue_number}" if issue_number else _today_str()
    greeting       = f"Hi {recipient_name}," if recipient_name else "Hello,"
    unsubscribe_url = f"{frontend_url}/?unsubscribe=1"  # deep-link → modal in SteamiNav
    subscribe_url   = f"{frontend_url}/?subscribe=1"
    linkedin_url    = draft.get("linkedin_url", "") or "#"
    ad_url          = draft.get("ad_website_url", "") or "#"

    # ── 1. Cover Signal ──────────────────────────────────────────────────────
    cover_chart = ""

    png_b64   = draft.get("cover_chart_png_b64", "")   # V5: pre-rendered PNG from QuickChart.io
    svg_data  = draft.get("cover_chart_svg_data", "")  # legacy SVG string
    chart_url = draft.get("cover_chart_image_url", "") # legacy URL

    img_src = ""

    # Priority 1: pre-rendered PNG base64 from QuickChart.io — best for email.
    # Self-contained in the HTML, works in Gmail/Outlook/Apple Mail with zero
    # external fetches. This should always win when available.
    if png_b64 and png_b64.startswith("data:image/png;base64,"):
        img_src = png_b64
        log.info("Chart: using pre-rendered PNG base64 (QuickChart)")

    # Priority 2: PNG/image served by our backend via FastAPI StaticFiles
    # (e.g. /images/newsletter/charts/<uuid>.png stored on disk by ollama_agent).
    #
    # We ALWAYS fetch and base64-embed the image — never use the URL directly —
    # because Brevo/Gmail/Outlook either block remote images or proxy them in
    # ways that can cause them to appear broken.
    #
    # _chart_url_to_img_src handles:
    #   • Relative paths  → resolved with API_BASE_URL from .env
    #   • Disk files      → read directly from CHART_STORE_DIR (no HTTP needed)
    #   • Any public URL  → fetched over HTTP and base64-encoded
    #   • Last resort     → absolute URL returned as-is if all else fails
    if not img_src and chart_url:
        img_src = _chart_url_to_img_src(chart_url)
        if img_src and img_src.startswith("data:"):
            log.info("Chart: base64-embedded via _chart_url_to_img_src")
        else:
            log.warning("Chart: could not embed as base64, using URL as-is")

    # Priority 3: convert stored SVG string → PNG base64
    if not img_src and svg_data and svg_data.strip().startswith("<svg"):
        img_src = _svg_to_png_base64(svg_data)
        if img_src:
            log.info("Chart: SVG string converted to PNG base64 for email")

    if img_src:
        cover_chart = f"""
        <div style="margin:20px 0;">
          <img src="{img_src}" alt="Signal Chart" width="596"
               style="width:100%;max-height:280px;object-fit:contain;border-radius:10px;
                      border:1px solid rgba(80,130,210,0.2);display:block;background:#fff;">
          {f'<p style="margin:10px 0 0;font-size:12px;color:#5a7fa8;font-style:italic;">{draft["cover_chart_explanation"]}</p>'
           if draft.get("cover_chart_explanation") else ""}
        </div>"""

    cover_section = ""
    if draft.get("cover_article_title"):
        cover_url   = draft.get("cover_article_url", frontend_url)
        cover_date  = draft.get("cover_article_date", _today_str())
        cover_body  = draft.get("cover_insight_summary", "")
        cover_section = f"""
        <table width="100%" cellpadding="0" cellspacing="0"
               style="background:#eef5ff;border-radius:12px;margin-bottom:24px;
                      border:1px solid rgba(80,130,210,0.18);overflow:hidden;">
          <tr><td style="padding:28px 32px;">
            <p style="margin:0 0 8px;font-family:'JetBrains Mono',monospace;
                      font-size:10px;letter-spacing:2px;text-transform:uppercase;
                      color:#3b72c8;font-weight:500;">📡 SIGNAL — COVER STORY</p>
            <h2 style="margin:0 0 8px;font-family:'Syne',sans-serif;font-size:22px;
                        font-weight:800;color:#0f2651;line-height:1.25;">
              {draft['cover_article_title']}
            </h2>
            {cover_chart}
            <p style="margin:0 0 20px;font-size:14px;color:#2a3f5a;line-height:1.7;">
              {cover_body}
            </p>
            <a href="{cover_url}" target="_blank" rel="noopener"
               style="display:inline-block;background:#1d4ed8;color:#fff;
                      text-decoration:none;padding:12px 24px;border-radius:8px;
                      font-family:'Syne',sans-serif;font-size:13px;font-weight:700;">
              Read Full Article →
            </a>
          </td></tr>
        </table>"""

    # ── 2. Sponsor ────────────────────────────────────────────────────────────
    sponsor_section = ""
    if draft.get("sponsor_name") or draft.get("sponsor_message"):
        sp_img = ""
        if draft.get("sponsor_image_url"):
            sp_img = f'<img src="{draft["sponsor_image_url"]}" alt="{draft.get("sponsor_name","")}" style="width:100%;max-height:160px;object-fit:cover;border-radius:8px;margin-bottom:14px;display:block;">'
        sp_link  = draft.get("sponsor_link") or ad_url
        sp_name  = draft.get("sponsor_name", "Partner")
        sp_msg   = draft.get("sponsor_message", "")
        sponsor_section = f"""
        <table width="100%" cellpadding="0" cellspacing="0"
               style="background:#fff8e6;border-radius:12px;margin-bottom:24px;
                      border:1px solid rgba(200,160,40,0.25);overflow:hidden;">
          <tr><td style="padding:22px 28px;">
            <p style="margin:0 0 10px;font-family:'JetBrains Mono',monospace;
                      font-size:10px;letter-spacing:2px;text-transform:uppercase;
                      color:#b07d10;font-weight:500;">🤝 SPONSORED · PARTNER MESSAGE</p>
            {sp_img}
            <p style="margin:0 0 14px;font-size:14px;color:#3a2c00;line-height:1.65;">{sp_msg}</p>
            <a href="{sp_link}" target="_blank" rel="noopener"
               style="display:inline-block;background:#b07d10;color:#fff;
                      text-decoration:none;padding:10px 20px;border-radius:7px;
                      font-size:13px;font-weight:600;">{sp_name} →</a>
          </td></tr>
        </table>"""

    # ── 3–5. Content modules (Explainer / Research / Blog) ───────────────────
    def _content_card(emoji: str, tag: str, title: str, link: str, color: str) -> str:
        if not title:
            return ""
        return f"""
        <table width="100%" cellpadding="0" cellspacing="0"
               style="background:#fff;border-radius:10px;margin-bottom:16px;
                      border:1px solid rgba(80,130,210,0.15);overflow:hidden;">
          <tr><td style="padding:20px 24px;">
            <p style="margin:0 0 8px;font-family:'JetBrains Mono',monospace;
                      font-size:10px;letter-spacing:2px;text-transform:uppercase;
                      color:{color};font-weight:500;">{emoji} {tag}</p>
            <h3 style="margin:0 0 12px;font-family:'Syne',sans-serif;font-size:17px;
                        font-weight:700;color:#0f2651;line-height:1.3;">{title}</h3>
            <a href="{link}" target="_blank" rel="noopener"
               style="font-family:'JetBrains Mono',monospace;font-size:12px;
                      color:#1d4ed8;text-decoration:underline;">Read more →</a>
          </td></tr>
        </table>"""

    content_section = ""
    if draft.get("explainer_title") or draft.get("research_title") or draft.get("blog_title"):
        content_section = """
        <p style="margin:0 0 14px;font-family:'JetBrains Mono',monospace;font-size:10px;
                  letter-spacing:2px;text-transform:uppercase;color:#3b72c8;font-weight:500;">
          📚 DEEP DIVES
        </p>"""
        content_section += _content_card(
            "💡", "EXPLAINER MODULE",
            draft.get("explainer_title", ""), draft.get("explainer_link", "#"), "#6366f1"
        )
        content_section += _content_card(
            "🔬", "RESEARCH",
            draft.get("research_title", ""), draft.get("research_link", "#"), "#10b981"
        )
        content_section += _content_card(
            "📝", "BLOG POST",
            draft.get("blog_title", ""), draft.get("blog_link", "#"), "#f59e0b"
        )

    # ── 6. Signal Briefs ──────────────────────────────────────────────────────
    briefs_section = ""
    if signal_articles:
        brief_notes = draft.get("signal_brief_notes", "")
        brief_items = ""
        for art in signal_articles:
            insight   = art.get("insight") or {}
            art_url   = _make_deep_link(frontend_url, "insight", art.get("id", ""))
            emoji     = insight.get("emoji", "⚡")
            domain    = insight.get("domain", "")
            summary   = (insight.get("summary") or art.get("short_summary") or "")[:200]
            fetched   = art.get("fetched_at", "")
            date_str  = ""
            if fetched:
                try:
                    date_str = datetime.fromisoformat(fetched.replace("Z", "+00:00")).strftime("%b %d")
                except Exception:
                    pass
            art_title = art.get('title', '')
            date_span = (
                f'<span style="font-family:\'JetBrains Mono\',monospace;font-size:10px;'
                f'color:#7a9dc8;margin-left:8px;">{date_str}</span>'
            ) if date_str else ""
            domain_p = (
                f'<p style="margin:4px 0 6px;font-size:12px;color:#5a7fa8;">{domain}</p>'
            ) if domain else ""
            brief_items += f"""
            <tr>
              <td style="padding:14px 0;border-bottom:1px solid rgba(80,130,210,0.12);">
                <p style="margin:0 0 4px;">
                  <span style="font-size:15px;">{emoji}</span>
                  <strong style="font-family:'Syne',sans-serif;font-size:14px;
                                 color:#0f2651;margin-left:6px;">{art_title}</strong>
                  {date_span}
                </p>
                {domain_p}
                <p style="margin:0 0 8px;font-size:13px;color:#2a3f5a;line-height:1.6;">{summary}…</p>
                <a href="{art_url}" target="_blank" rel="noopener"
                   style="font-family:'JetBrains Mono',monospace;font-size:11px;
                          color:#1d4ed8;text-decoration:underline;">Read insight →</a>
              </td>
            </tr>"""

        briefs_section = f"""
        <table width="100%" cellpadding="0" cellspacing="0"
               style="background:#f0f6ff;border-radius:12px;margin-bottom:24px;
                      border:1px solid rgba(80,130,210,0.18);overflow:hidden;">
          <tr><td style="padding:24px 28px;">
            <p style="margin:0 0 {('14px' if brief_notes else '18px')};font-family:'JetBrains Mono',monospace;
                      font-size:10px;letter-spacing:2px;text-transform:uppercase;
                      color:#3b72c8;font-weight:500;">⚡ SIGNAL BRIEFS</p>
            {f'<p style="margin:0 0 16px;font-size:13px;color:#2a3f5a;font-style:italic;">{brief_notes}</p>' if brief_notes else ""}
            <table width="100%" cellpadding="0" cellspacing="0">
              {brief_items}
            </table>
          </td></tr>
        </table>"""

    # ── 7. On the Radar ───────────────────────────────────────────────────────
    radar_section = ""
    on_the_radar = draft.get("on_the_radar", "")
    if on_the_radar:
        radar_items = _parse_radar_events(on_the_radar)
        if radar_items:
            items_html = ""
            for item in radar_items:
                day_html   = f'<div style="font-family:\'Syne\',sans-serif;font-size:26px;font-weight:800;color:#1d4ed8;line-height:1;">{item["day"]}</div>' if item.get("day") else ""
                month_html = f'<div style="font-family:\'JetBrains Mono\',monospace;font-size:9px;letter-spacing:2px;text-transform:uppercase;color:#5a7fa8;margin-top:2px;">{item["month"]}</div>' if item.get("month") else ""
                heading_txt = item.get("heading", "")
                desc_txt    = item.get("text", "")
                heading_html = (
                    f'<p style="margin:0 0 4px;font-family:\'Syne\',sans-serif;font-size:14px;'
                    f'font-weight:700;color:#0f2651;line-height:1.3;">{heading_txt}</p>'
                ) if heading_txt else ""
                desc_html = (
                    f'<p style="margin:0;font-size:13px;color:#2a3f5a;line-height:1.65;">'
                    f'{_linkify(desc_txt.replace(chr(10), "<br>"))}</p>'
                ) if desc_txt else ""
                items_html += f"""
                <tr>
                  <td style="padding:16px 0;border-bottom:1px solid rgba(80,130,210,0.10);vertical-align:top;">
                    <table width="100%" cellpadding="0" cellspacing="0"><tr>
                      <td style="width:56px;vertical-align:top;padding-right:18px;text-align:center;">
                        {day_html}{month_html}
                      </td>
                      <td style="vertical-align:middle;">
                        {heading_html}{desc_html}
                      </td>
                    </tr></table>
                  </td>
                </tr>"""
            radar_section = f"""
            <table width="100%" cellpadding="0" cellspacing="0"
                   style="background:#fff;border-radius:12px;margin-bottom:24px;
                          border:1px solid rgba(80,130,210,0.15);overflow:hidden;">
              <tr><td style="padding:24px 28px;">
                <p style="margin:0 0 16px;font-family:'JetBrains Mono',monospace;
                          font-size:10px;letter-spacing:2px;text-transform:uppercase;
                          color:#3b72c8;font-weight:500;">📻 ON THE RADAR</p>
                <table width="100%" cellpadding="0" cellspacing="0">
                  {items_html}
                </table>
              </td></tr>
            </table>"""
        else:
            # Fallback: free-form text with linkify
            radar_html = _linkify(on_the_radar.replace("\n", "<br>"))
            radar_section = f"""
            <table width="100%" cellpadding="0" cellspacing="0"
                   style="background:#fff;border-radius:12px;margin-bottom:24px;
                          border:1px solid rgba(80,130,210,0.15);overflow:hidden;">
              <tr><td style="padding:24px 28px;">
                <p style="margin:0 0 14px;font-family:'JetBrains Mono',monospace;
                          font-size:10px;letter-spacing:2px;text-transform:uppercase;
                          color:#3b72c8;font-weight:500;">📻 ON THE RADAR</p>
                <div style="font-size:14px;color:#2a3f5a;line-height:1.75;">
                  {radar_html}
                </div>
              </td></tr>
            </table>"""

    # ── Footer ────────────────────────────────────────────────────────────────
    footer = f"""
    <table width="100%" cellpadding="0" cellspacing="0"
           style="background:#e8f2fc;border-top:1px solid rgba(80,130,210,0.14);">
      <tr><td style="padding:24px 32px;text-align:center;">
        <p style="margin:0 0 10px;font-family:'JetBrains Mono',monospace;
                  font-size:12px;font-weight:700;color:#0f2651;letter-spacing:1px;">
          {SITE_NAME}
        </p>
        <p style="margin:0 0 14px;font-size:12px;color:#5a7fa8;">
          Science &amp; Technology · {_today_str()}
        </p>
        <div style="display:flex;gap:16px;justify-content:center;margin-bottom:14px;flex-wrap:wrap;">
          <a href="{frontend_url}" style="font-size:12px;color:#1d4ed8;text-decoration:none;">Visit {SITE_NAME}</a>
          {f'<a href="{linkedin_url}" style="font-size:12px;color:#1d4ed8;text-decoration:none;">LinkedIn</a>' if linkedin_url and linkedin_url != "#" else ""}
          <a href="{subscribe_url}" style="font-size:12px;color:#1d4ed8;text-decoration:none;">Subscribe</a>
        </div>
        <p style="margin:0;font-size:11px;color:#8aabcc;">
          You received this because you subscribed to {SITE_NAME} updates. &nbsp;
          <a href="{unsubscribe_url}" style="color:#8aabcc;text-decoration:underline;">Unsubscribe</a>
        </p>
      </td></tr>
    </table>"""

    # ── Assemble ──────────────────────────────────────────────────────────────
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>{SITE_NAME} Newsletter — {issue_label}</title>
  <link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600&family=Syne:wght@400;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
</head>
<body style="margin:0;padding:0;background:#d0e4f4;
             font-family:'DM Sans',system-ui,sans-serif;font-size:15px;
             line-height:1.68;color:#1a2a3a;padding:32px 16px;">
  <table width="100%" cellpadding="0" cellspacing="0">
    <tr><td align="center">
      <table width="660" cellpadding="0" cellspacing="0"
             style="max-width:660px;width:100%;background:#f5f9ff;
                    border:1px solid rgba(80,130,210,0.22);border-radius:4px;overflow:hidden;">

        <!-- Header -->
        <tr><td style="background:#f5f9ff;padding:20px 36px 24px;
                        border-bottom:2px solid rgba(80,130,210,0.14);">
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td>
                <table cellpadding="0" cellspacing="0">
                  <tr>
                    <td style="width:38px;height:38px;background:#1d4ed8;border-radius:6px;
                                text-align:center;vertical-align:middle;">
                      <span style="font-family:'Syne',sans-serif;font-weight:800;
                                   font-size:13px;color:#fff;">ST</span>
                    </td>
                    <td style="padding-left:11px;">
                      <div style="font-family:'Syne',sans-serif;font-weight:700;
                                  font-size:17px;color:#0f2651;">{SITE_NAME}</div>
                      <div style="font-family:'JetBrains Mono',monospace;font-size:10px;
                                  color:#7a9dc8;letter-spacing:2px;text-transform:uppercase;">
                        Science &amp; Technology
                      </div>
                    </td>
                  </tr>
                </table>
              </td>
              <td style="text-align:right;font-family:'JetBrains Mono',monospace;
                          font-size:11px;color:#7a9dc8;line-height:1.6;">
                {issue_label}<br>{_today_str()}
              </td>
            </tr>
          </table>
          <!-- Greeting -->
          <p style="margin:20px 0 0;font-size:15px;color:#374151;">
            {greeting} Here's your STEAMI digest — the science &amp; tech signals you need to know. ✨
          </p>
        </td></tr>

        <!-- Body -->
        <tr><td style="padding:28px 32px;">
          {cover_section}
          {sponsor_section}
          {content_section}
          {briefs_section}
          {radar_section}
        </td></tr>

        <!-- Footer -->
        <tr><td>{footer}</td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


# ═════════════════════════════════════════════════════════════════════════════
# OLLAMA CHART GENERATION
# ═════════════════════════════════════════════════════════════════════════════

_CHART_SYSTEM = """You are a data-visualization code generator for the STEAMI science newsletter.
Given an article summary, generate:
1. A short Python script using matplotlib to draw a relevant bar/line chart
2. A one-paragraph explanation of what the chart shows

Respond with ONLY valid JSON — no markdown, no extra text:
{
  "chart_code": "import matplotlib.pyplot as plt\\n...",
  "explanation": "This chart shows..."
}

Rules for chart_code:
- Use matplotlib only (no seaborn, plotly, pandas)
- Hard-code 4-6 realistic-looking data points relevant to the article
- Save to the path given in the SAVE_PATH variable (already defined)
- Use a clean light style: plt.style.use('seaborn-v0_8-whitegrid') or default
- Set figure size to (8, 4), dpi=120
- Use colors: #1d4ed8 (blue), #3b82f6 (light blue)
- Add title, xlabel, ylabel
- Call plt.tight_layout() before plt.savefig(SAVE_PATH, bbox_inches='tight')
- Do NOT call plt.show()"""

def _ollama_url() -> str:
    if OLLAMA_LOCAL_HOST:
        return f"{OLLAMA_LOCAL_HOST}/api/chat"
    return OLLAMA_CLOUD_URL

def _generate_chart_via_ollama(article_id: str, title: str, summary: str, domain: str) -> dict:
    """Call Ollama to get chart code + explanation, execute code, return image path + explanation."""

    chart_path = CHART_STORE_DIR / f"{article_id}.png"

    prompt = f"""Article title: {title}
Domain: {domain}
Summary: {summary[:600]}

Generate a matplotlib chart and explanation for this article.
Set SAVE_PATH = "{chart_path}" in your script."""

    headers = {"Content-Type": "application/json"}
    if OLLAMA_API_KEY:
        headers["Authorization"] = f"Bearer {OLLAMA_API_KEY}"

    payload = {
        "model":  OLLAMA_MODEL,
        "stream": False,
        "format": "json",
        "messages": [
            {"role": "system", "content": _CHART_SYSTEM},
            {"role": "user",   "content": prompt},
        ],
    }

    resp = http.post(_ollama_url(), headers=headers, json=payload, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"Ollama HTTP {resp.status_code}: {resp.text[:300]}")

    raw = resp.json().get("message", {}).get("content", "")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Try to extract JSON from response
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            raise RuntimeError("Ollama returned invalid JSON for chart generation")
        data = json.loads(m.group())

    chart_code  = data.get("chart_code", "")
    explanation = data.get("explanation", "")

    # ── Execute chart code in sandboxed subprocess ────────────────────────────
    if chart_code:
        # Inject SAVE_PATH if model didn't include it
        if "SAVE_PATH" not in chart_code:
            chart_code = f'SAVE_PATH = "{chart_path}"\n' + chart_code

        # Whitelist: only allow matplotlib imports
        forbidden = ["import os", "import sys", "import subprocess", "open(", "__import__",
                     "eval(", "exec(", "requests", "urllib", "socket"]
        for f in forbidden:
            if f in chart_code:
                raise RuntimeError(f"Unsafe chart code detected: {f}")

        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tmp:
            tmp.write(chart_code)
            tmp_path = tmp.name

        result = subprocess.run(
            ["python3", tmp_path],
            capture_output=True, text=True, timeout=30
        )
        Path(tmp_path).unlink(missing_ok=True)

        if result.returncode != 0:
            raise RuntimeError(f"Chart script error: {result.stderr[:300]}")

    return {
        "chart_path":  str(chart_path) if chart_path.exists() else "",
        "explanation": explanation,
    }


# ═════════════════════════════════════════════════════════════════════════════
# ENDPOINTS — DRAFT MANAGEMENT  (mod + admin)
# ═════════════════════════════════════════════════════════════════════════════

@router.post("/draft/save",
             summary="Save newsletter draft to MongoDB — MOD/ADMIN",
             tags=["Newsletter"])
def save_draft(draft: NewsletterDraft, payload: dict = Depends(require_mod)):
    """
    Saves (upserts) the single active newsletter draft.
    Old draft is replaced — only one draft is ever kept.

    IMPORTANT: cover_chart_png_b64 and cover_chart_config are large blobs that
    the frontend may not carry in its draft state (they're set by the chart
    endpoint, not by user input).  If the incoming draft has empty values for
    these fields, we restore them from the existing MongoDB document so the
    chart image is not lost on every save.
    """
    doc = draft.model_dump()

    # Preserve chart blobs that the frontend may not carry
    _PRESERVE_IF_EMPTY = ("cover_chart_png_b64", "cover_chart_config", "cover_chart_svg_data")
    preserve_needed = any(not doc.get(f) for f in _PRESERVE_IF_EMPTY)
    if preserve_needed:
        try:
            existing = db.collection("newsletter_draft").document(DRAFT_DOC_ID).get()
            if existing.exists:
                existing_data = existing.to_dict() or {}
                for field in _PRESERVE_IF_EMPTY:
                    if not doc.get(field) and existing_data.get(field):
                        doc[field] = existing_data[field]
                        log.info("save_draft: preserved %s from existing draft", field)
        except Exception as e:
            log.warning("save_draft: could not read existing draft to preserve blobs: %s", e)

    doc.update({
        "saved_at":  _now(),
        "saved_by":  get_uid(payload),
        "doc_id":    DRAFT_DOC_ID,
    })
    try:
        db.collection("newsletter_draft").document(DRAFT_DOC_ID).set(doc)
    except Exception as e:
        raise HTTPException(500, detail=f"Failed to save draft: {e}")
    return {"saved": True, "doc_id": DRAFT_DOC_ID}


@router.get("/draft",
            summary="Load the current newsletter draft — MOD/ADMIN",
            tags=["Newsletter"])
def get_draft(payload: dict = Depends(require_mod)):
    try:
        doc = db.collection("newsletter_draft").document(DRAFT_DOC_ID).get()
        if not doc.exists:
            return {"draft": None}
        return {"draft": doc.to_dict()}
    except Exception as e:
        raise HTTPException(500, detail=f"Failed to load draft: {e}")


@router.post("/draft/chart",
             summary="AI-generate chart image for a cover article — MOD/ADMIN",
             tags=["Newsletter"])
def generate_chart(req: ChartRequest, payload: dict = Depends(require_mod)):
    """
    Uses Ollama to generate a matplotlib chart for the given article,
    executes it in a sandboxed subprocess, and returns the chart URL + explanation.
    The chart image is served via GET /api/newsletter/chart/{article_id}.
    """
    if not OLLAMA_API_KEY and not OLLAMA_LOCAL_HOST:
        raise HTTPException(503, detail=(
            "Ollama not configured. Set OLLAMA_API_KEY (cloud) or "
            "OLLAMA_HOST (local) in your .env to enable chart generation."
        ))
    try:
        result = _generate_chart_via_ollama(
            article_id=req.article_id,
            title=req.title,
            summary=req.summary,
            domain=req.domain,
        )
    except Exception as e:
        raise HTTPException(500, detail=f"Chart generation failed: {e}")

    chart_url = ""
    svg_data  = ""
    chart_path_obj = Path(result.get("chart_path", ""))
    if result.get("chart_path") and chart_path_obj.exists():
        safe_id   = re.sub(r'[^a-zA-Z0-9_\-]', '', req.article_id)
        chart_url = f"{SITE_URL}/api/newsletter/chart/{safe_id}"
        try:
            svg_data = chart_path_obj.read_text(encoding="utf-8")
        except Exception:
            pass
        if svg_data:
            try:
                db.collection("newsletter_draft").document(DRAFT_DOC_ID).set(
                    {"cover_chart_svg_data": svg_data, "cover_chart_image_url": chart_url,
                     "cover_chart_explanation": result.get("explanation", "")},
                    merge=True,
                )
            except Exception as e:
                log.warning("Could not persist SVG to MongoDB draft: %s", e)

    return {
        "chart_image_url": chart_url,
        "chart_svg_data":  svg_data,
        "explanation":     result.get("explanation", ""),
        "article_id":      req.article_id,
    }


@router.get("/chart/{article_id}",
            summary="Serve a generated chart SVG",
            tags=["Newsletter"])
def serve_chart(article_id: str):
    """Serves the AI-generated chart SVG for an article."""
    safe_id = re.sub(r'[^a-zA-Z0-9_\-]', '', article_id)
    # Try SVG first (new format), fall back to PNG (legacy)
    svg_path = CHART_STORE_DIR / f"{safe_id}.svg"
    png_path = CHART_STORE_DIR / f"{safe_id}.png"
    if svg_path.exists():
        return FileResponse(str(svg_path), media_type="image/svg+xml")
    if png_path.exists():
        return FileResponse(str(png_path), media_type="image/png")
    raise HTTPException(404, detail="Chart not found. Generate it first via POST /draft/cover-story-chart.")


# ─────────────────────────────────────────────────────────────────────────────
# NEW: Cover Story endpoint  (uses generate_cover_story from ollama_agent)
# ─────────────────────────────────────────────────────────────────────────────

class CoverStoryRequest(BaseModel):
    """Full article data the frontend sends to generate a cover story."""
    article_id:      str
    title:           str
    content:         str = ""
    summary:         str = ""
    domain:          str = "Technology"
    article_url:     str = ""
    fetched_at:      str = ""
    matched_domains: list = []


@router.post("/draft/cover-story",
             summary="AI-generate long-form cover story for selected article — MOD/ADMIN",
             tags=["Newsletter"])
def generate_cover_story_endpoint(req: CoverStoryRequest, payload: dict = Depends(require_mod)):
    """
    Calls generate_cover_story() from ollama_agent.py.

    Builds a rich article dict from the request and passes it to the AI.
    Returns:
      headline, standfirst, body_paragraphs, pull_quote, key_stats,
      chart_data, closing_line, reading_time_min, domain

    The frontend should:
      1. Call this endpoint when the mod selects a cover article.
      2. Auto-fill the cover insight summary field with standfirst + body_paragraphs.
      3. Store chart_data for the next call to /draft/cover-story-chart.
    """
    from ollama_agent import generate_cover_story

    # Build article dict that ollama_agent expects
    article = {
        "id":              req.article_id,
        "title":           req.title,
        "content":         req.content or req.summary,
        "full_content":    req.content,
        "description":     req.summary,
        "article_url":     req.article_url,
        "url":             req.article_url,
        "matched_domains": req.matched_domains or [req.domain],
        "topic":           req.domain,
        "fetched_at":      req.fetched_at,
    }

    try:
        cover = generate_cover_story(article)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(502, detail=f"Ollama error: {e}")
    except Exception as e:
        log.error("cover_story error: %s", e)
        raise HTTPException(500, detail=f"Cover story generation failed: {e}")

    # Build a formatted summary string for auto-filling the draft field
    paragraphs = cover.get("body_paragraphs", [])
    formatted_summary = "\n\n".join(paragraphs) if paragraphs else cover.get("standfirst", "")

    return {
        "article_id":       req.article_id,
        "headline":         cover.get("headline", req.title),
        "standfirst":       cover.get("standfirst", ""),
        "body_paragraphs":  cover.get("body_paragraphs", []),
        "pull_quote":       cover.get("pull_quote", ""),
        "key_stats":        cover.get("key_stats", []),
        "chart_data":       cover.get("chart_data", {}),
        "chart_subject":    cover.get("chart_subject", ""),
        "closing_line":     cover.get("closing_line", ""),
        "reading_time_min": cover.get("reading_time_min", 3),
        "domain":           cover.get("domain", req.domain),
        # Convenience: pre-formatted string for the draft summary textarea
        "formatted_summary": formatted_summary,
    }


@router.post("/draft/cover-story-chart",
             summary="AI-generate chart PNG for the cover story — MOD/ADMIN",
             tags=["Newsletter"])
def generate_cover_story_chart_endpoint(req: CoverStoryRequest, payload: dict = Depends(require_mod)):
    """
    Two-step cover story chart generation:
      1. Calls generate_cover_story() to get structured chart_data.
      2. Calls generate_newsletter_chart() to produce the PNG.

    If chart_data was already generated by /draft/cover-story, the frontend
    can pass it back in the request body (content field unused for chart step,
    but the article identity is needed).

    Returns:
      chart_image_url — served via GET /api/newsletter/chart/<article_id>
      explanation     — two-sentence AI description of what the chart shows
      chart_type      — "bar" | "line" | "horizontal_bar"
      success         — bool
      error           — empty string on success
    """
    from ollama_agent import generate_cover_story, generate_newsletter_chart

    article = {
        "id":              req.article_id,
        "title":           req.title,
        "content":         req.content or req.summary,
        "full_content":    req.content,
        "description":     req.summary,
        "article_url":     req.article_url,
        "url":             req.article_url,
        "matched_domains": req.matched_domains or [req.domain],
        "topic":           req.domain,
        "fetched_at":      req.fetched_at,
    }

    try:
        cover = generate_cover_story(article)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, detail=f"Cover story step failed: {e}")

    try:
        result = generate_newsletter_chart(article, cover)
    except Exception as e:
        log.error("cover_story_chart error: %s", e)
        raise HTTPException(500, detail=f"Chart generation failed: {e}")

    chart_url  = result.get("chart_image_url", "")   # public URL built from API_BASE_URL
    svg_data   = ""
    png_b64    = result.get("chart_png_b64", "")

    # Build chart_url from legacy chart_path if the new URL field is empty
    # (old matplotlib/subprocess path — kept for backward compat)
    if not chart_url and result.get("success") and result.get("chart_path"):
        chart_path_obj = Path(result["chart_path"])
        safe_id   = re.sub(r'[^a-zA-Z0-9_\-]', '', req.article_id)
        chart_url = f"{SITE_URL}/api/newsletter/chart/{safe_id}"
        try:
            svg_data = chart_path_obj.read_text(encoding="utf-8")
        except Exception as e:
            log.warning("Could not read SVG for inline storage: %s", e)

    # ── Persist everything to MongoDB draft (always, for both paths) ──────────
    # IMPORTANT: always use merge=True so a subsequent save_draft (merge=False)
    # from the frontend still finds cover_chart_png_b64 in its own draft state.
    if png_b64 or chart_url:
        patch = {
            "cover_chart_png_b64":     png_b64,
            "cover_chart_image_url":   chart_url,
            "cover_chart_explanation": result.get("explanation", ""),
        }
        if svg_data:
            patch["cover_chart_svg_data"] = svg_data
        try:
            db.collection("newsletter_draft").document(DRAFT_DOC_ID).set(patch, merge=True)
            log.info("Chart PNG base64 persisted to MongoDB draft")
        except Exception as e:
            log.warning("Could not persist chart to MongoDB draft: %s", e)

    return {
        "article_id":      req.article_id,
        "chart_image_url": chart_url,
        "chart_svg_data":  svg_data,
        "chart_png_b64":   png_b64,
        "chartjs_config":  result.get("chartjs_config", ""),
        "explanation":     result.get("explanation", ""),
        "chart_type":      result.get("chart_type", "bar"),
        "render_ok":       result.get("render_ok", bool(png_b64)),
        "success":         result.get("success", False),
        "error":           result.get("error", ""),
    }


# ═════════════════════════════════════════════════════════════════════════════
# ENDPOINTS — PREVIEW & SEND  (mod preview, admin send)
# ═════════════════════════════════════════════════════════════════════════════

@router.post("/preview-draft",
             summary="Render draft newsletter to HTML — MOD/ADMIN",
             tags=["Newsletter"])
def preview_draft(draft: NewsletterDraft, payload: dict = Depends(require_mod)):
    """Renders the draft to HTML for in-browser preview (no email sent)."""
    signal_articles = _fetch_signal_articles(draft.signal_brief_ids)
    html = _build_custom_html(draft.model_dump(), signal_articles)
    return {
        "html":    html,
        "subject": _build_subject(draft),
        "articles_included": len(signal_articles),
    }


@router.post("/send-custom",
             summary="Send the custom drafted newsletter to all subscribers — ADMIN ONLY",
             tags=["Newsletter"])
def send_custom_newsletter(draft: NewsletterDraft, payload: dict = Depends(require_admin)):
    """
    Send the mod-drafted newsletter to all active subscribers.
    Replaces the old send-daily endpoint for custom newsletters.
    Old draft is deleted after successful send.
    """
    signal_articles = _fetch_signal_articles(draft.signal_brief_ids)
    subscribers     = _get_subscriber_emails()
    if not subscribers:
        return {"sent": 0, "failed": 0, "total_subscribers": 0, "message": "No subscribers found."}

    subject       = _build_subject(draft)
    sent          = 0
    failed        = 0
    failed_emails = []

    for sub in subscribers:
        email     = sub["email"]
        name      = sub.get("name", "")
        html_body = _build_custom_html(draft.model_dump(), signal_articles, recipient_name=name)

        if _send_one_via_brevo(email, name, subject, html_body):
            sent += 1
        else:
            failed += 1
            failed_emails.append(email)

    # Log the send
    log_id = str(uuid.uuid4())
    try:
        db.collection("newsletter_logs").document(log_id).set({
            "log_id":            log_id,
            "sent_at":           _now(),
            "sent_by":           get_uid(payload),
            "type":              "custom",
            "issue_number":      draft.issue_number,
            "total_subscribers": len(subscribers),
            "emails_sent":       sent,
            "emails_failed":     failed,
            "cover_article":     draft.cover_article_title,
            "failed_emails":     failed_emails,
        })
        # Delete draft after send (no old newsletters stored)
        db.collection("newsletter_draft").document(DRAFT_DOC_ID).delete()
    except Exception as e:
        log.warning("Post-send cleanup error: %s", e)

    log.info("send_custom: sent=%d failed=%d subscribers=%d", sent, failed, len(subscribers))
    return {
        "sent":              sent,
        "failed":            failed,
        "total_subscribers": len(subscribers),
        "articles_included": len(signal_articles),
        "log_id":            log_id,
    }


def _build_subject(draft: NewsletterDraft) -> str:
    base = f"🔬 {SITE_NAME} Newsletter"
    if draft.issue_number:
        base += f" #{draft.issue_number}"
    base += f" — {_today_str()}"
    return base

def _fetch_signal_articles(ids: list[str]) -> list[dict]:
    """Fetch article docs from MongoDB for the chosen signal brief IDs."""
    articles = []
    for art_id in ids[:5]:  # cap at 5
        try:
            doc = db.collection("articles").document(art_id).get()
            if doc.exists:
                articles.append(doc.to_dict())
        except Exception as e:
            log.warning("Could not fetch signal article %s: %s", art_id, e)
    return articles


# ═════════════════════════════════════════════════════════════════════════════
# ENDPOINTS — SUBSCRIBE / UNSUBSCRIBE  (PUBLIC)
# ═════════════════════════════════════════════════════════════════════════════

@router.post("/subscribe",
             summary="Subscribe an email to the newsletter — PUBLIC",
             tags=["Newsletter"])
def subscribe(body: SubscribeBody):
    email = body.email.lower().strip()
    if not email or "@" not in email:
        raise HTTPException(400, detail="Valid email is required.")

    name = (body.name or "").strip()

    existing = list(
        db.collection("newsletter_subscribers")
          .where("email", "==", email)
          .limit(1)
          .stream()
    )
    if existing:
        doc = existing[0].to_dict()
        if doc.get("subscribed"):
            return f"Already subscribed: {email}"
        db.collection("newsletter_subscribers").document(existing[0].id).update({
            "subscribed": True, "updated_at": _now(), "name": name or doc.get("name", ""),
        })
        _brevo_sync_contact(email, name)
        return f"Resubscribed: {email}"

    sub_id = str(uuid.uuid4())
    db.collection("newsletter_subscribers").document(sub_id).set({
        "uid": sub_id, "id": sub_id,
        "email": email, "name": name,
        "subscribed": True, "source": "web_modal",
        "created_at": _now(), "is_active": True,
    })
    _brevo_sync_contact(email, name)
    log.info("newsletter subscribe: %s", email)
    return f"Subscribed: {email}"


@router.post("/unsubscribe",
             summary="Unsubscribe from the newsletter — PUBLIC",
             tags=["Newsletter"])
def unsubscribe(body: UnsubscribeBody):
    email = body.email.lower().strip()
    if not email or "@" not in email:
        raise HTTPException(400, detail="Valid email is required.")

    docs = list(
        db.collection("newsletter_subscribers")
          .where("email", "==", email)
          .limit(1)
          .stream()
    )
    if not docs:
        return f"Not found: {email}"

    db.collection("newsletter_subscribers").document(docs[0].id).update({
        "subscribed": False, "is_active": False, "unsubscribed_at": _now(),
    })
    # Unsubscribe from Brevo list too (best-effort)
    if BREVO_API_KEY:
        try:
            http.put(
                f"{BREVO_API_BASE}/contacts/{email}",
                headers=_brevo_headers(),
                json={"emailBlacklisted": True},
                timeout=10,
            )
        except Exception:
            pass

    log.info("newsletter unsubscribe: %s", email)
    return f"Unsubscribed: {email}"


# ═════════════════════════════════════════════════════════════════════════════
# ENDPOINTS — ORIGINAL (preserved)
# ═════════════════════════════════════════════════════════════════════════════

@router.get("/recipients",
            summary="List all newsletter subscribers — ADMIN ONLY",
            tags=["Newsletter"])
def list_recipients(payload: dict = Depends(require_admin)):
    docs = db.collection("newsletter_subscribers").stream()
    return [d.to_dict() for d in docs]


@router.post("/send-daily",
             summary="Send auto digest (top 5 insight articles) — ADMIN ONLY",
             tags=["Newsletter"])
def send_daily_digest(
    limit:   int  = Query(5, ge=1, le=20),
    payload: dict = Depends(require_admin),
):
    """Original auto-digest endpoint — still works, uses AI-insight articles directly."""
    docs = (
        db.collection("articles")
          .where("has_insight", "==", True)
          .order_by("fetched_at", direction="DESCENDING")
          .limit(limit)
          .stream()
    )
    articles = [d.to_dict() for d in docs]
    if not articles:
        raise HTTPException(404, detail="No articles with AI insights found.")

    subscribers = _get_subscriber_emails()
    if not subscribers:
        return {"sent": 0, "failed": 0, "total_subscribers": 0}

    subject = f"🔬 {SITE_NAME} Daily Digest — {_today_str()}"
    sent = failed = 0
    failed_emails = []

    for sub in subscribers:
        html_body = _build_old_digest_html(articles, recipient_name=sub.get("name", ""))
        if _send_one_via_brevo(sub["email"], sub.get("name", ""), subject, html_body):
            sent += 1
        else:
            failed += 1
            failed_emails.append(sub["email"])

    log_id = str(uuid.uuid4())
    try:
        db.collection("newsletter_logs").document(log_id).set({
            "log_id": log_id, "sent_at": _now(), "type": "auto_digest",
            "sent_by": get_uid(payload),
            "total_subscribers": len(subscribers),
            "emails_sent": sent, "emails_failed": failed,
            "articles_included": len(articles), "failed_emails": failed_emails,
        })
    except Exception as e:
        log.warning("Failed to log newsletter send: %s", e)

    return {"sent": sent, "failed": failed, "total_subscribers": len(subscribers),
            "articles_included": len(articles), "log_id": log_id}


@router.get("/preview",
            summary="Preview auto-digest HTML — ADMIN ONLY",
            tags=["Newsletter"])
def preview_auto_digest(limit: int = Query(5, ge=1, le=20), payload: dict = Depends(require_admin)):
    docs = (
        db.collection("articles")
          .where("has_insight", "==", True)
          .order_by("fetched_at", direction="DESCENDING")
          .limit(limit)
          .stream()
    )
    articles = [d.to_dict() for d in docs]
    return {
        "articles_included": len(articles),
        "subject": f"🔬 {SITE_NAME} Daily Digest — {_today_str()}",
        "html": _build_old_digest_html(articles),
    }


@router.post("/test",
             summary="Send a test newsletter to one address — ADMIN ONLY",
             tags=["Newsletter"])
def send_test_email(body: TestEmailBody, payload: dict = Depends(require_admin)):
    """
    Sends the full custom draft newsletter (same HTML as the real send) to a
    single test address.  If no draft is passed in the request body, it loads
    the current saved draft from MongoDB.  Falls back to the old digest only
    if neither is available.
    """
    # 1. Use draft from request body if provided
    draft_dict = body.draft or {}

    # 2. If none provided, load saved draft from MongoDB
    if not draft_dict:
        try:
            doc = db.collection("newsletter_draft").document(DRAFT_DOC_ID).get()
            if doc.exists:
                draft_dict = doc.to_dict() or {}
        except Exception as e:
            log.warning("Could not load draft for test email: %s", e)

    subject = body.subject or f"[TEST] {SITE_NAME} Newsletter"

    if draft_dict:
        # Build full custom HTML exactly as the real send would
        signal_articles = _fetch_signal_articles(draft_dict.get("signal_brief_ids", []))
        html_body = _build_custom_html(draft_dict, signal_articles)
    else:
        # Absolute fallback: old digest with 3 recent insight articles
        docs = (
            db.collection("articles")
              .where("has_insight", "==", True)
              .order_by("fetched_at", direction="DESCENDING")
              .limit(3)
              .stream()
        )
        articles  = [d.to_dict() for d in docs]
        html_body = _build_old_digest_html(articles)

    success = _send_one_via_brevo(body.to_email, "", subject, html_body)
    return {"sent": 1 if success else 0, "failed": 0 if success else 1,
            "to_email": body.to_email, "success": success}


@router.post("/ai-subscribe",
             summary="AI agent subscription endpoint — PUBLIC",
             tags=["Newsletter"])
def ai_agent_subscribe(body: AiSubscribeBody):
    email  = body.email.lower().strip()
    if not email or "@" not in email:
        raise HTTPException(400, detail="Valid email is required")
    name   = (body.name or "").strip()
    source = (body.source or "ai_agent").strip()[:100]

    existing_list = list(
        db.collection("newsletter_subscribers")
          .where("email", "==", email)
          .limit(1)
          .stream()
    )
    if existing_list:
        sub = existing_list[0].to_dict()
        if not sub.get("subscribed"):
            db.collection("newsletter_subscribers").document(existing_list[0].id).update({
                "subscribed": True, "source": source, "updated_at": _now(),
            })
            _brevo_sync_contact(email, name)
        return {"subscribed": True, "email": email, "already_existed": True,
                "message": f"This email is already subscribed to {SITE_NAME}."}

    sub_id = str(uuid.uuid4())
    db.collection("newsletter_subscribers").document(sub_id).set({
        "uid": sub_id, "id": sub_id, "email": email, "name": name,
        "subscribed": True, "source": source,
        "metadata": body.metadata or {}, "created_at": _now(),
    })
    _brevo_sync_contact(email, name)
    return {"subscribed": True, "email": email, "already_existed": False,
            "message": f"Successfully subscribed to {SITE_NAME} daily newsletter."}


# ── Legacy auto-digest HTML builder (original, untouched) ────────────────────

def _build_old_digest_html(articles: list[dict], recipient_name: str = "") -> str:
    greeting = f"Hi {recipient_name}," if recipient_name else "Hello,"
    article_blocks = ""
    for art in articles:
        title   = art.get("title", "Untitled")
        summary = art.get("short_summary") or art.get("content", "")[:220]
        url     = art.get("article_url") or art.get("url") or SITE_URL
        image   = art.get("image_url", "")
        domains = ", ".join(art.get("matched_domains", []))
        img_block = (
            f'<img src="{image}" alt="" width="560" '
            f'style="width:100%;max-height:220px;object-fit:cover;'
            f'border-radius:10px;margin-bottom:14px;display:block;">'
        ) if image else ""
        domain_badge = (
            f'<p style="margin:0 0 6px;font-size:11px;color:#6366f1;'
            f'font-weight:700;text-transform:uppercase;letter-spacing:0.6px;">'
            f'{domains}</p>'
        ) if domains else ""
        article_blocks += f"""
        <table width="100%" cellpadding="0" cellspacing="0"
               style="background:#ffffff;border-radius:12px;margin-bottom:20px;
                      border:1px solid #e5e7eb;overflow:hidden;">
          <tr><td style="padding:24px;">
            {img_block}{domain_badge}
            <h2 style="margin:0 0 10px;font-size:19px;font-weight:700;color:#111827;">{title}</h2>
            <p style="margin:0 0 18px;font-size:14px;color:#6b7280;line-height:1.65;">{summary}</p>
            <a href="{url}" target="_blank" rel="noopener"
               style="display:inline-block;background:#6366f1;color:#ffffff;
                      text-decoration:none;padding:11px 22px;border-radius:8px;
                      font-size:14px;font-weight:600;">Read Full Article →</a>
          </td></tr>
        </table>"""

    unsubscribe = f"{SITE_URL}/?unsubscribe=1"
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{SITE_NAME} Daily Digest</title></head>
<body style="margin:0;padding:0;background:#f3f4f6;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:32px 16px;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">
        <tr><td style="background:linear-gradient(135deg,#6366f1 0%,#8b5cf6 100%);
                        border-radius:14px 14px 0 0;padding:36px 32px;text-align:center;">
          <h1 style="margin:0;color:#ffffff;font-size:30px;font-weight:800;">🔬 {SITE_NAME}</h1>
          <p style="margin:10px 0 0;color:rgba(255,255,255,0.85);font-size:14px;">
            Your daily STEM digest &nbsp;·&nbsp; {_today_str()}</p>
        </td></tr>
        <tr><td style="padding:24px 0 4px;">
          <p style="margin:0 0 20px;font-size:15px;color:#374151;text-align:center;">
            {greeting} Here's what's new in science and technology today. ✨</p>
          {article_blocks}
        </td></tr>
        <tr><td style="background:#ffffff;border-radius:12px;padding:28px;text-align:center;
                        border:1px solid #e5e7eb;">
          <h3 style="margin:0 0 8px;font-size:17px;color:#111827;">Explore more on {SITE_NAME}</h3>
          <a href="{SITE_URL}" style="display:inline-block;margin-top:12px;background:#6366f1;
             color:#fff;text-decoration:none;padding:12px 28px;border-radius:8px;
             font-size:14px;font-weight:600;">Visit {SITE_NAME} →</a>
        </td></tr>
        <tr><td style="padding:20px;text-align:center;">
          <p style="font-size:12px;color:#9ca3af;">
            <a href="{unsubscribe}" style="color:#9ca3af;text-decoration:underline;">Unsubscribe</a>
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""