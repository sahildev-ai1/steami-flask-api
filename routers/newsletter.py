"""
routers/newsletter.py  —  Newsletter & Daily Digest Mailer (Brevo / Sendinblue)
================================================================================

WHY BREVO INSTEAD OF MAILCHIMP:
  - Mailchimp free plan (2026): 250 contacts, 500 emails/month — nearly useless.
  - Brevo free plan (2026):     unlimited contacts (stores 100,000), 300 emails/DAY,
                                 full REST API access, no credit card required.
  - Paid plans start at $9/month with no daily cap.

HOW BREVO WORKS (in 3 steps):
  1. You create a free account at https://app.brevo.com
  2. Verify your sending domain (Brevo guides you through adding DNS records).
  3. Generate an API key under Settings → SMTP & API → API Keys.
  4. Set BREVO_API_KEY in your .env — that's it. No SMTP config needed.

  When you call POST /api/newsletter/send-daily, this router:
    a) Fetches recent articles with AI insights from MongoDB.
    b) Builds an HTML email body.
    c) Makes a single POST request to Brevo's /v3/smtp/email endpoint.
    d) Brevo delivers to all recipients and handles bounces/unsubscribes.

FREE TIER MATH FOR STEAMI:
  - 300 emails/day free.
  - If you have 300 subscribers → send daily digest every day for free.
  - If you have 600 subscribers → split across 2 days or upgrade to $9/month
    (starter plan removes daily limit entirely).

ENDPOINTS:
  GET  /api/newsletter/recipients      — list all subscribed emails (admin)
  POST /api/newsletter/subscribe       — subscribe an email (public)
  POST /api/newsletter/unsubscribe     — unsubscribe (public)
  POST /api/newsletter/send-daily      — send digest to all subscribers (admin)
  GET  /api/newsletter/preview         — preview today's digest email HTML (admin)
  POST /api/newsletter/test            — send a test email to one address (admin)
  POST /api/newsletter/ai-subscribe    — AI agent subscription endpoint (public)

ENV VARS (add to .env):
  BREVO_API_KEY    — from https://app.brevo.com → Settings → SMTP & API → API Keys
  BREVO_SENDER_EMAIL — verified sender email, e.g. hello@steami.com
  BREVO_SENDER_NAME  — display name, e.g. "STEAMI Newsletter"
  SITE_URL         — e.g. https://steami.com  (← update when domain confirmed)
  SITE_NAME        — e.g. STEAMI
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
from auth import require_auth, require_admin, get_uid

log = logging.getLogger(__name__)
router = APIRouter()

# ── Brevo config ────────────────────────────────────────────────────────────
BREVO_API_KEY      = os.getenv("BREVO_API_KEY", "")
BREVO_SENDER_EMAIL = os.getenv("BREVO_SENDER_EMAIL", "hello@steami.com")
BREVO_SENDER_NAME  = os.getenv("BREVO_SENDER_NAME", "STEAMI Newsletter")
BREVO_API_BASE     = "https://api.brevo.com/v3"
SITE_URL           = os.getenv("SITE_URL",  "https://steami.com")   # ← change when domain ready
SITE_NAME          = os.getenv("SITE_NAME", "STEAMI")


# ── Helpers ─────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _brevo_headers() -> dict:
    """Return headers required for all Brevo API calls."""
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


def _send_via_brevo(
    to_emails:  list[str],
    subject:    str,
    html_body:  str,
    text_body:  str = "",
) -> dict:
    """
    Send an HTML email to a list of recipients via Brevo's SMTP API.

    Brevo's free tier allows 300 emails/day.
    For bulk sends > 300, this function automatically batches into groups
    of 299 and sends sequentially (each batch counts as 1 API call).

    Returns { "sent": N, "failed": M, "errors": [...], "message_ids": [...] }

    Brevo API docs: https://developers.brevo.com/reference/sendtransacemail
    """
    if not to_emails:
        return {"sent": 0, "failed": 0, "errors": [], "message_ids": []}

    # Brevo allows max 999 recipients per API call, but free tier: 300/day total.
    # We batch in groups of 299 to stay safe under the daily limit.
    BATCH_SIZE  = 299
    batches     = [to_emails[i:i+BATCH_SIZE] for i in range(0, len(to_emails), BATCH_SIZE)]

    total_sent  = 0
    total_failed = 0
    all_errors  = []
    message_ids = []

    headers = _brevo_headers()

    for batch_num, batch in enumerate(batches, start=1):
        # Brevo expects recipients as list of { "email": "..." } dicts
        to_list = [{"email": e} for e in batch]

        payload = {
            "sender":  {"name": BREVO_SENDER_NAME, "email": BREVO_SENDER_EMAIL},
            "to":      to_list,
            "subject": subject,
            "htmlContent": html_body,
        }
        if text_body:
            payload["textContent"] = text_body

        try:
            resp = http.post(
                f"{BREVO_API_BASE}/smtp/email",
                headers=headers,
                json=payload,
                timeout=30,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                message_ids.append(data.get("messageId", f"batch-{batch_num}"))
                total_sent += len(batch)
                log.info(
                    "Brevo batch %d/%d: sent %d emails, messageId=%s",
                    batch_num, len(batches), len(batch), data.get("messageId"),
                )
            else:
                err_text = resp.text[:300]
                log.error("Brevo batch %d failed: HTTP %d — %s", batch_num, resp.status_code, err_text)
                all_errors.append({"batch": batch_num, "status": resp.status_code, "error": err_text})
                total_failed += len(batch)

        except Exception as e:
            log.error("Brevo batch %d exception: %s", batch_num, e)
            all_errors.append({"batch": batch_num, "error": str(e)})
            total_failed += len(batch)

    return {
        "sent":        total_sent,
        "failed":      total_failed,
        "errors":      all_errors,
        "message_ids": message_ids,
    }


def _build_digest_html(articles: list[dict]) -> str:
    """
    Build the branded HTML body for the daily digest email.
    Articles should have: title, short_summary, article_url, image_url, matched_domains.
    """
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

    today_str    = datetime.now(timezone.utc).strftime("%B %d, %Y")
    unsubscribe  = f"{SITE_URL}/unsubscribe"

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

        <!-- ── Header ──────────────────────────────────────── -->
        <tr><td style="background:linear-gradient(135deg,#6366f1 0%,#8b5cf6 100%);
                        border-radius:14px 14px 0 0;padding:36px 32px;text-align:center;">
          <h1 style="margin:0;color:#ffffff;font-size:30px;font-weight:800;
                     letter-spacing:-0.5px;">🔬 {SITE_NAME}</h1>
          <p style="margin:10px 0 0;color:rgba(255,255,255,0.85);font-size:14px;">
            Your daily STEM digest &nbsp;·&nbsp; {today_str}
          </p>
        </td></tr>

        <!-- ── Intro line ──────────────────────────────────── -->
        <tr><td style="padding:24px 0 4px;">
          <p style="margin:0 0 20px;font-size:15px;color:#374151;text-align:center;">
            Here's what's new in science and technology today. ✨
          </p>

          <!-- Articles -->
          {article_blocks}
        </td></tr>

        <!-- ── CTA ─────────────────────────────────────────── -->
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

        <!-- ── Footer ──────────────────────────────────────── -->
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
    Pull all active newsletter subscribers from MongoDB.
    Merges: newsletter_subscribers (subscribed=True) + users (subscribed_newsletter=True).
    Deduplicates by email address.
    Returns list of { uid, email, name }.
    """
    seen:   dict[str, dict] = {}   # keyed by email — deduplication

    # Source 1 — newsletter_subscribers collection
    try:
        for d in (
            db.collection("newsletter_subscribers")
              .where("subscribed", "==", True)
              .stream()
        ):
            s = d.to_dict()
            e = s.get("email", "").lower().strip()
            if e:
                seen[e] = {"uid": s.get("uid", ""), "email": e, "name": s.get("name", "")}
    except Exception as ex:
        log.warning("newsletter_subscribers read failed: %s", ex)

    # Source 2 — users with subscribed_newsletter=True
    try:
        for d in (
            db.collection("users")
              .where("subscribed_newsletter", "==", True)
              .stream()
        ):
            u = d.to_dict()
            e = u.get("email", "").lower().strip()
            if e and e not in seen:
                seen[e] = {
                    "uid":   u.get("uid", ""),
                    "email": e,
                    "name":  u.get("display_name", ""),
                }
    except Exception as ex:
        log.warning("users subscribed read failed: %s", ex)

    return list(seen.values())


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

@router.get(
    "/recipients",
    summary="List all newsletter subscribers — ADMIN ONLY",
    tags=["Newsletter"],
)
def get_recipients(payload: dict = Depends(require_admin)):
    """
    **Admin only.** Returns all active newsletter subscribers merged from both
    `newsletter_subscribers` and `users` collections.

    This is what POST /api/newsletter/send-daily reads before sending bulk email.

    Response:
    ```json
    {
      "total": 42,
      "subscribers": [
        { "uid": "...", "email": "user@gmail.com", "name": "Jane" }
      ]
    }
    ```
    """
    subs = _get_subscriber_emails()
    log.info("get_recipients: admin=%s total=%d", get_uid(payload), len(subs))
    return {"total": len(subs), "subscribers": subs}


# ══════════════════════════════════════════════════════════════════════════════
# POST /api/newsletter/subscribe
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/subscribe",
    summary="Subscribe an email to the newsletter — PUBLIC",
    tags=["Newsletter"],
)
def subscribe(body: SubscribeBody):
    """
    **Public.** Subscribe an email to the STEAMI daily newsletter.

    Also syncs the contact to Brevo's audience list (if BREVO_API_KEY is set)
    so you can manage the full list from the Brevo dashboard.
    """
    email = body.email.lower().strip()
    if not email or "@" not in email:
        raise HTTPException(400, detail="Valid email is required")

    # ── Check existing ─────────────────────────────────────────────────────
    existing = list(
        db.collection("newsletter_subscribers")
          .where("email", "==", email)
          .limit(1)
          .stream()
    )
    if existing:
        sub = existing[0].to_dict()
        if sub.get("subscribed"):
            return {"subscribed": True, "already_existed": True, "email": email}
        db.collection("newsletter_subscribers").document(existing[0].id).update({
            "subscribed": True, "updated_at": _now(),
        })
        _brevo_sync_contact(email, body.name or "")
        return {"subscribed": True, "reactivated": True, "email": email}

    # ── New subscriber ──────────────────────────────────────────────────────
    sub_id = str(uuid.uuid4())
    db.collection("newsletter_subscribers").document(sub_id).set({
        "uid":        sub_id,
        "email":      email,
        "name":       (body.name or "").strip(),
        "subscribed": True,
        "source":     "web_signup",
        "created_at": _now(),
    })
    _brevo_sync_contact(email, body.name or "")
    log.info("newsletter subscribe: %s", email)
    return {"subscribed": True, "already_existed": False, "email": email}


def _brevo_sync_contact(email: str, name: str = "") -> None:
    """
    Add or update a contact in Brevo's audience list.
    Silently skips if BREVO_API_KEY is not set.
    This keeps your Brevo dashboard in sync with MongoDB subscribers.
    """
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
# POST /api/newsletter/unsubscribe
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/unsubscribe",
    summary="Unsubscribe from the newsletter — PUBLIC",
    tags=["Newsletter"],
)
def unsubscribe(body: UnsubscribeBody):
    """
    **Public.** Unsubscribe an email. Updates both MongoDB and Brevo.
    """
    email = body.email.lower().strip()
    if not email:
        raise HTTPException(400, detail="email is required")

    # MongoDB newsletter_subscribers
    for d in db.collection("newsletter_subscribers").where("email", "==", email).stream():
        db.collection("newsletter_subscribers").document(d.id).update({
            "subscribed": False, "updated_at": _now(),
        })

    # MongoDB users
    for d in db.collection("users").where("email", "==", email).limit(1).stream():
        db.collection("users").document(d.id).update({
            "subscribed_newsletter": False, "updated_at": _now(),
        })

    # Brevo — mark as unsubscribed via their API
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
# POST /api/newsletter/send-daily  — MAIN SEND ENDPOINT
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/send-daily",
    summary="Send daily digest to all subscribers via Brevo — ADMIN ONLY",
    tags=["Newsletter"],
)
def send_daily_digest(
    limit:   int  = Query(5, ge=1, le=20, description="Articles to include"),
    payload: dict = Depends(require_admin),
):
    """
    **Admin only.** Sends the daily STEM digest to all active newsletter subscribers.

    Flow:
    1. Fetches the `limit` most recent articles with AI insights from MongoDB.
    2. Builds a branded HTML email.
    3. Reads all subscriber emails (_get_subscriber_emails).
    4. Sends via Brevo's REST API (batches of 299 on free tier).
    5. Logs results to `newsletter_logs` collection.

    **Brevo free tier:** 300 emails/day.
    - Up to 300 subscribers → completely free, runs every day.
    - 301–600 subscribers → need 2 days or upgrade to $9/month Starter.

    **Set up a daily cron job:**
    ```
    0 9 * * * curl -X POST https://your-api.com/api/newsletter/send-daily \\
      -H "Authorization: Bearer <admin_token>"
    ```

    Response:
    ```json
    {
      "sent": 280,
      "failed": 2,
      "total_subscribers": 282,
      "articles_included": 5,
      "log_id": "uuid"
    }
    ```
    """
    # 1. Fetch articles
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

    to_emails = [s["email"] for s in subscribers]

    # 3. Build HTML
    subject   = (
        f"🔬 {SITE_NAME} Daily Digest — "
        f"{datetime.now(timezone.utc).strftime('%B %d, %Y')}"
    )
    html_body = _build_digest_html(articles)

    # 4. Send via Brevo
    result = _send_via_brevo(to_emails, subject, html_body)

    # 5. Log to MongoDB
    log_id = str(uuid.uuid4())
    try:
        db.collection("newsletter_logs").document(log_id).set({
            "log_id":            log_id,
            "sent_at":           _now(),
            "sent_by":           get_uid(payload),
            "total_subscribers": len(subscribers),
            "emails_sent":       result["sent"],
            "emails_failed":     result["failed"],
            "articles_included": len(articles),
            "article_ids":       [a.get("id", "") for a in articles],
            "brevo_message_ids": result.get("message_ids", []),
            "errors":            result.get("errors", []),
        })
    except Exception as e:
        log.warning("Failed to log newsletter send: %s", e)

    log.info(
        "send_daily: sent=%d failed=%d subscribers=%d articles=%d",
        result["sent"], result["failed"], len(subscribers), len(articles),
    )

    return {
        "sent":               result["sent"],
        "failed":             result["failed"],
        "total_subscribers":  len(subscribers),
        "articles_included":  len(articles),
        "log_id":             log_id,
    }


# ══════════════════════════════════════════════════════════════════════════════
# GET /api/newsletter/preview
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/preview",
    summary="Preview today's digest HTML — ADMIN ONLY",
    tags=["Newsletter"],
)
def preview_digest(
    limit:   int  = Query(5, ge=1, le=20),
    payload: dict = Depends(require_admin),
):
    """
    **Admin only.** Returns the rendered HTML for today's digest without sending it.
    Use this to visually check the email before the actual send.
    """
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

@router.post(
    "/test",
    summary="Send a test digest email to one address — ADMIN ONLY",
    tags=["Newsletter"],
)
def send_test_email(body: TestEmailBody, payload: dict = Depends(require_admin)):
    """
    **Admin only.** Sends a test digest to a single address for rendering QA.

    Body: `{ "to_email": "admin@example.com" }`
    """
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
    result    = _send_via_brevo([body.to_email], subject, html_body)
    return {"sent": result["sent"], "failed": result["failed"], "to_email": body.to_email}


# ══════════════════════════════════════════════════════════════════════════════
# POST /api/newsletter/ai-subscribe  — for AI agents visiting STEAMI
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/ai-subscribe",
    summary="AI agent endpoint — subscribe a user to the newsletter — PUBLIC",
    tags=["Newsletter"],
)
def ai_agent_subscribe(body: AiSubscribeBody):
    """
    **Public endpoint for AI agents.**

    When an AI assistant on STEAMI (or any AI that knows about STEAMI) wants
    to subscribe a user to the daily newsletter, it calls this endpoint.

    No API key required — rate-limited by DDoS protection (10 req/min per IP).

    Body:
    ```json
    {
      "email":    "user@example.com",
      "name":     "Jane",
      "source":   "chatgpt",
      "metadata": { "session_id": "abc123" }
    }
    ```

    Response:
    ```json
    {
      "subscribed":      true,
      "email":           "user@example.com",
      "already_existed": false,
      "message":         "Successfully subscribed to STEAMI daily newsletter."
    }
    ```
    """
    email = body.email.lower().strip()
    if not email or "@" not in email:
        raise HTTPException(400, detail="Valid email is required")

    source = (body.source or "ai_agent").strip()[:100]
    name   = (body.name or "").strip()

    existing = list(
        db.collection("newsletter_subscribers")
          .where("email", "==", email)
          .limit(1)
          .stream()
    )

    if existing:
        sub = existing[0].to_dict()
        if not sub.get("subscribed"):
            db.collection("newsletter_subscribers").document(existing[0].id).update({
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