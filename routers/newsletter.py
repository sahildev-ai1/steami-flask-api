"""
routers/newsletter.py  —  Newsletter & Daily Digest Mailer (Brevo / Sendinblue)
================================================================================

HOW BREVO WORKS (in 3 steps):
  1. Create a free account at https://app.brevo.com
  2. Verify your sending domain (Brevo walks you through DNS records).
  3. Generate an API key under Settings → SMTP & API → API Keys.
  4. Set BREVO_API_KEY in your .env — done. No SMTP config needed.

IMPORTANT — SENDER EMAIL:
  BREVO_SENDER_EMAIL must be a verified domain in Brevo.
  ❌ onboarding@resend.dev  — that's a Resend domain, not valid for Brevo.
  ✅ hello@steami.com        — after you verify steami.com in Brevo.
  ✅ noreply@steami.dev      — after you verify steami.dev in Brevo.
  To verify: Brevo dashboard → Senders & IPs → Domains → Add a domain.

ENV VARS (add to .env):
  BREVO_API_KEY      — from https://app.brevo.com → Settings → SMTP & API → API Keys
  BREVO_SENDER_EMAIL — a domain you've verified inside Brevo, e.g. hello@steami.com
  BREVO_SENDER_NAME  — display name shown in inbox, e.g. "STEAMI Newsletter"
  SITE_URL           — e.g. https://steami.com
  SITE_NAME          — e.g. STEAMI

ENDPOINTS:
  GET  /api/newsletter/recipients      — list all subscribed emails (admin)
  POST /api/newsletter/subscribe       — subscribe an email (public)
  POST /api/newsletter/unsubscribe     — unsubscribe (public)
  POST /api/newsletter/send-daily      — send digest to all subscribers (admin)
  GET  /api/newsletter/preview         — preview today's digest HTML (admin)
  POST /api/newsletter/test            — send a test email to one address (admin)
  POST /api/newsletter/ai-subscribe    — AI agent subscription endpoint (public)

KEY FIX (v3):
  - Uses db.collection("name").where().stream() / .document(id).set/get/update()
    which is the correct _DB wrapper API used everywhere else in this codebase.
  - Previous version incorrectly used db["name"] (raw pymongo subscript syntax).
"""

import os
import uuid
import logging
import requests as http
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel

from mongodb_client import db
from auth import require_admin, get_uid

log = logging.getLogger(__name__)
router = APIRouter()

# ── Brevo config ─────────────────────────────────────────────────────────────
BREVO_API_KEY      = os.getenv("BREVO_API_KEY", "")
BREVO_SENDER_EMAIL = os.getenv("BREVO_SENDER_EMAIL", "hello@steami.com")
BREVO_SENDER_NAME  = os.getenv("BREVO_SENDER_NAME", "STEAMI Newsletter")
BREVO_API_BASE     = "https://api.brevo.com/v3"
SITE_URL           = os.getenv("SITE_URL",  "https://steami.com")
SITE_NAME          = os.getenv("SITE_NAME", "STEAMI")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _brevo_headers() -> dict:
    if not BREVO_API_KEY:
        raise HTTPException(
            503,
            detail=(
                "Brevo API key not configured. "
                "Set BREVO_API_KEY in your .env file. "
                "Get a free key at https://app.brevo.com → Settings → SMTP & API → API Keys"
            ),
        )
    return {
        "accept":       "application/json",
        "content-type": "application/json",
        "api-key":      BREVO_API_KEY,
    }


def _send_one_via_brevo(
    to_email:  str,
    to_name:   str,
    subject:   str,
    html_body: str,
) -> bool:
    """
    Send a single transactional email via Brevo.
    One API call per recipient = transactional routing = inbox delivery.
    Returns True on success, False on failure.
    """
    payload = {
        "sender":      {"name": BREVO_SENDER_NAME, "email": BREVO_SENDER_EMAIL},
        "to":          [{"email": to_email, "name": to_name or to_email}],
        "subject":     subject,
        "htmlContent": html_body,
    }
    try:
        resp = http.post(
            f"{BREVO_API_BASE}/smtp/email",
            headers=_brevo_headers(),
            json=payload,
            timeout=30,
        )
        if resp.status_code in (200, 201):
            log.info("Brevo sent to %s — messageId=%s", to_email, resp.json().get("messageId"))
            return True
        log.error("Brevo failed for %s: HTTP %d — %s", to_email, resp.status_code, resp.text[:300])
        return False
    except Exception as e:
        log.error("Brevo exception for %s: %s", to_email, e)
        return False


def _build_digest_html(articles: list[dict], recipient_name: str = "") -> str:
    """Build the branded HTML body for the daily digest email."""
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
            {img_block}
            {domain_badge}
            <h2 style="margin:0 0 10px;font-size:19px;font-weight:700;
                        color:#111827;line-height:1.35;">{title}</h2>
            <p style="margin:0 0 18px;font-size:14px;color:#6b7280;
                      line-height:1.65;">{summary}</p>
            <a href="{url}" target="_blank" rel="noopener"
               style="display:inline-block;background:#6366f1;color:#ffffff;
                      text-decoration:none;padding:11px 22px;border-radius:8px;
                      font-size:14px;font-weight:600;">
              Read Full Article →
            </a>
          </td></tr>
        </table>
        """

    today_str   = datetime.now(timezone.utc).strftime("%B %d, %Y")
    unsubscribe = f"{SITE_URL}/unsubscribe"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{SITE_NAME} Daily Digest — {today_str}</title>
</head>
<body style="margin:0;padding:0;background:#f3f4f6;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0"
         style="background:#f3f4f6;padding:32px 16px;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0"
             style="max-width:600px;width:100%;">

        <!-- Header -->
        <tr><td style="background:linear-gradient(135deg,#6366f1 0%,#8b5cf6 100%);
                        border-radius:14px 14px 0 0;padding:36px 32px;text-align:center;">
          <h1 style="margin:0;color:#ffffff;font-size:30px;font-weight:800;
                     letter-spacing:-0.5px;">🔬 {SITE_NAME}</h1>
          <p style="margin:10px 0 0;color:rgba(255,255,255,0.85);font-size:14px;">
            Your daily STEM digest &nbsp;·&nbsp; {today_str}
          </p>
        </td></tr>

        <!-- Greeting -->
        <tr><td style="padding:24px 0 4px;">
          <p style="margin:0 0 20px;font-size:15px;color:#374151;text-align:center;">
            {greeting} Here's what's new in science and technology today. ✨
          </p>
          {article_blocks}
        </td></tr>

        <!-- CTA -->
        <tr><td style="background:#ffffff;border-radius:12px;padding:28px;
                        text-align:center;border:1px solid #e5e7eb;margin-bottom:20px;">
          <h3 style="margin:0 0 8px;font-size:17px;color:#111827;">
            Explore more on {SITE_NAME}
          </h3>
          <p style="margin:0 0 18px;font-size:14px;color:#6b7280;">
            AI-powered insights, explainers, and research summaries — all in one place.
          </p>
          <a href="{SITE_URL}" target="_blank" rel="noopener"
             style="display:inline-block;background:#6366f1;color:#ffffff;
                    text-decoration:none;padding:13px 30px;border-radius:9px;
                    font-size:15px;font-weight:700;">
            Visit {SITE_NAME} →
          </a>
        </td></tr>

        <!-- Footer -->
        <tr><td style="padding:22px;text-align:center;">
          <p style="margin:0;font-size:12px;color:#9ca3af;line-height:1.6;">
            You're receiving this because you subscribed to {SITE_NAME}.<br>
            <a href="{unsubscribe}" style="color:#6366f1;text-decoration:none;">
              Unsubscribe
            </a>
          </p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _get_subscriber_emails() -> list[dict]:
    """
    Pull all active newsletter subscribers using db.collection() — the same
    _DB wrapper API used everywhere else in this codebase.
    Merges newsletter_subscribers (subscribed=True) + users (subscribed_newsletter=True).
    Deduplicates by email. Returns list of { uid, email, name }.
    """
    seen: dict[str, dict] = {}

    # Source 1 — newsletter_subscribers collection
    try:
        docs = (
            db.collection("newsletter_subscribers")
              .where("subscribed", "==", True)
              .stream()
        )
        for d in docs:
            s = d.to_dict()
            e = (s.get("email") or "").lower().strip()
            if e:
                seen[e] = {"uid": s.get("uid", ""), "email": e, "name": s.get("name", "")}
    except Exception as ex:
        log.warning("newsletter_subscribers read failed: %s", ex)

    # Source 2 — users with subscribed_newsletter=True
    try:
        docs = (
            db.collection("users")
              .where("subscribed_newsletter", "==", True)
              .stream()
        )
        for d in docs:
            u = d.to_dict()
            e = (u.get("email") or "").lower().strip()
            if e and e not in seen:
                seen[e] = {
                    "uid":   u.get("uid", ""),
                    "email": e,
                    "name":  u.get("display_name", ""),
                }
    except Exception as ex:
        log.warning("users subscribed read failed: %s", ex)

    return list(seen.values())


def _brevo_sync_contact(email: str, name: str = "") -> None:
    """Add/update a contact in Brevo's list. Silently skips if no API key."""
    if not BREVO_API_KEY:
        return
    try:
        parts      = name.strip().split(" ", 1)
        first_name = parts[0] if parts else ""
        last_name  = parts[1] if len(parts) > 1 else ""
        http.post(
            f"{BREVO_API_BASE}/contacts",
            headers=_brevo_headers(),
            json={
                "email": email,
                "attributes": {"FIRSTNAME": first_name, "LASTNAME": last_name},
                "updateEnabled": True,
            },
            timeout=10,
        )
    except Exception as e:
        log.warning("Brevo contact sync failed for %s: %s", email, e)


# ══════════════════════════════════════════════════════════════════════════════
# REQUEST BODIES
# ══════════════════════════════════════════════════════════════════════════════

class SubscribeBody(BaseModel):
    email: str
    name:  Optional[str] = ""

class UnsubscribeBody(BaseModel):
    email: str

class TestEmailBody(BaseModel):
    to_email: str
    subject:  Optional[str] = None

class AiSubscribeBody(BaseModel):
    email:    str
    name:     Optional[str] = ""
    source:   Optional[str] = "ai_agent"
    metadata: Optional[dict] = {}


# ══════════════════════════════════════════════════════════════════════════════
# GET /api/newsletter/recipients
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/recipients", summary="List all newsletter subscribers — ADMIN ONLY", tags=["Newsletter"])
def get_recipients(payload: dict = Depends(require_admin)):
    subs = _get_subscriber_emails()
    log.info("get_recipients: admin=%s total=%d", get_uid(payload), len(subs))
    return {"total": len(subs), "subscribers": subs}


# ══════════════════════════════════════════════════════════════════════════════
# POST /api/newsletter/subscribe
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/subscribe", summary="Subscribe an email to the newsletter — PUBLIC", tags=["Newsletter"])
def subscribe(body: SubscribeBody):
    email = body.email.lower().strip()
    if not email or "@" not in email:
        raise HTTPException(400, detail="Valid email is required")

    existing_list = list(
        db.collection("newsletter_subscribers")
          .where("email", "==", email)
          .limit(1)
          .stream()
    )

    if existing_list:
        sub = existing_list[0].to_dict()
        if sub.get("subscribed"):
            return {"subscribed": True, "already_existed": True, "email": email}
        db.collection("newsletter_subscribers").document(existing_list[0].id).update({
            "subscribed": True, "updated_at": _now(),
        })
        _brevo_sync_contact(email, body.name or "")
        return {"subscribed": True, "reactivated": True, "email": email}

    sub_id = str(uuid.uuid4())
    db.collection("newsletter_subscribers").document(sub_id).set({
        "uid":        sub_id,
        "email":      email,
        "name":       (body.name or "").strip(),
        "subscribed": True,
        "source":     "web_signup",
        "created_at": _now(),
        "id":         sub_id,
    })
    _brevo_sync_contact(email, body.name or "")
    log.info("newsletter subscribe: %s", email)
    return {"subscribed": True, "already_existed": False, "email": email}


# ══════════════════════════════════════════════════════════════════════════════
# POST /api/newsletter/unsubscribe
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/unsubscribe", summary="Unsubscribe from the newsletter — PUBLIC", tags=["Newsletter"])
def unsubscribe(body: UnsubscribeBody):
    email = body.email.lower().strip()
    if not email:
        raise HTTPException(400, detail="email is required")

    for d in db.collection("newsletter_subscribers").where("email", "==", email).stream():
        db.collection("newsletter_subscribers").document(d.id).update({
            "subscribed": False, "updated_at": _now(),
        })

    for d in db.collection("users").where("email", "==", email).limit(1).stream():
        db.collection("users").document(d.id).update({
            "subscribed_newsletter": False, "updated_at": _now(),
        })

    if BREVO_API_KEY:
        try:
            http.put(
                f"{BREVO_API_BASE}/contacts/{email}",
                headers=_brevo_headers(),
                json={"emailBlacklisted": True},
                timeout=10,
            )
        except Exception as e:
            log.warning("Brevo unsubscribe sync failed for %s: %s", email, e)

    log.info("newsletter unsubscribe: %s", email)
    return {"unsubscribed": True, "email": email}


# ══════════════════════════════════════════════════════════════════════════════
# POST /api/newsletter/send-daily
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/send-daily", summary="Send daily digest to all subscribers via Brevo — ADMIN ONLY", tags=["Newsletter"])
def send_daily_digest(
    limit:   int  = Query(5, ge=1, le=20, description="Articles to include"),
    payload: dict = Depends(require_admin),
):
    # 1. Fetch articles with AI insights, newest first
    try:
        docs = (
            db.collection("articles")
              .where("has_insight", "==", True)
              .order_by("fetched_at", direction="DESCENDING")
              .limit(limit)
              .stream()
        )
        articles = [d.to_dict() for d in docs]
    except Exception as e:
        raise HTTPException(500, detail=f"Failed to fetch articles: {e}")

    if not articles:
        raise HTTPException(
            404,
            detail=(
                "No articles with AI insights found. "
                "Run POST /api/articles/refresh then POST /api/articles/insights/process first."
            ),
        )

    # 2. Get subscribers
    subscribers = _get_subscriber_emails()
    if not subscribers:
        return {
            "sent": 0, "failed": 0,
            "total_subscribers": 0,
            "articles_included": len(articles),
            "message": "No subscribers found.",
        }

    # 3. Build subject
    subject = (
        f"🔬 {SITE_NAME} Daily Digest — "
        f"{datetime.now(timezone.utc).strftime('%B %d, %Y')}"
    )

    # 4. Send one individual email per subscriber (not bulk BCC — better deliverability)
    sent          = 0
    failed        = 0
    failed_emails = []

    for sub in subscribers:
        email     = sub["email"]
        name      = sub.get("name", "")
        html_body = _build_digest_html(articles, recipient_name=name)

        if _send_one_via_brevo(email, name, subject, html_body):
            sent += 1
        else:
            failed += 1
            failed_emails.append(email)

    # 5. Log to MongoDB
    log_id = str(uuid.uuid4())
    try:
        db.collection("newsletter_logs").document(log_id).set({
            "log_id":            log_id,
            "sent_at":           _now(),
            "sent_by":           get_uid(payload),
            "total_subscribers": len(subscribers),
            "emails_sent":       sent,
            "emails_failed":     failed,
            "articles_included": len(articles),
            "article_ids":       [a.get("id", "") for a in articles],
            "failed_emails":     failed_emails,
        })
    except Exception as e:
        log.warning("Failed to log newsletter send: %s", e)

    log.info("send_daily: sent=%d failed=%d subscribers=%d articles=%d",
             sent, failed, len(subscribers), len(articles))

    return {
        "sent":              sent,
        "failed":            failed,
        "total_subscribers": len(subscribers),
        "articles_included": len(articles),
        "log_id":            log_id,
    }


# ══════════════════════════════════════════════════════════════════════════════
# GET /api/newsletter/preview
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/preview", summary="Preview today's digest HTML — ADMIN ONLY", tags=["Newsletter"])
def preview_digest(
    limit:   int  = Query(5, ge=1, le=20),
    payload: dict = Depends(require_admin),
):
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
        "subject": (
            f"🔬 {SITE_NAME} Daily Digest — "
            f"{datetime.now(timezone.utc).strftime('%B %d, %Y')}"
        ),
        "html": _build_digest_html(articles),
    }


# ══════════════════════════════════════════════════════════════════════════════
# POST /api/newsletter/test
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/test", summary="Send a test digest email to one address — ADMIN ONLY", tags=["Newsletter"])
def send_test_email(body: TestEmailBody, payload: dict = Depends(require_admin)):
    """Body: { "to_email": "admin@example.com" }"""
    docs = (
        db.collection("articles")
          .where("has_insight", "==", True)
          .order_by("fetched_at", direction="DESCENDING")
          .limit(3)
          .stream()
    )
    articles  = [d.to_dict() for d in docs]
    subject   = body.subject or f"[TEST] {SITE_NAME} Daily Digest"
    html_body = _build_digest_html(articles)
    success   = _send_one_via_brevo(body.to_email, "", subject, html_body)

    return {
        "sent":     1 if success else 0,
        "failed":   0 if success else 1,
        "to_email": body.to_email,
        "success":  success,
    }


# ══════════════════════════════════════════════════════════════════════════════
# POST /api/newsletter/ai-subscribe
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/ai-subscribe", summary="AI agent endpoint — subscribe a user — PUBLIC", tags=["Newsletter"])
def ai_agent_subscribe(body: AiSubscribeBody):
    email = body.email.lower().strip()
    if not email or "@" not in email:
        raise HTTPException(400, detail="Valid email is required")

    source = (body.source or "ai_agent").strip()[:100]
    name   = (body.name or "").strip()

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
        return {
            "subscribed":      True,
            "email":           email,
            "already_existed": True,
            "message":         f"This email is already subscribed to {SITE_NAME}.",
        }

    sub_id = str(uuid.uuid4())
    db.collection("newsletter_subscribers").document(sub_id).set({
        "uid":        sub_id,
        "email":      email,
        "name":       name,
        "subscribed": True,
        "source":     source,
        "metadata":   body.metadata or {},
        "created_at": _now(),
        "id":         sub_id,
    })
    _brevo_sync_contact(email, name)
    log.info("ai-subscribe: email=%s source=%s", email, source)

    return {
        "subscribed":      True,
        "email":           email,
        "already_existed": False,
        "message": (
            f"Successfully subscribed to {SITE_NAME} daily newsletter. "
            "You'll receive the best STEM articles every morning!"
        ),
    }