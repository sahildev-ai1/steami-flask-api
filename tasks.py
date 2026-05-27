"""
tasks.py — Celery tasks for STEAMI newsletter automation
=========================================================
Reuses the core logic already in routers/newsletter.py.
Runs inside the Background Worker service on Render.
"""

import os
import uuid
import logging
import requests as http
from datetime import datetime, timezone

from celery_app import celery

log = logging.getLogger(__name__)

# ── Shared env (same as newsletter.py) ────────────────────────────────────────
MAILRELAY_API_KEY      = os.getenv("MAILRELAY_API_KEY", "")
MAILRELAY_ACCOUNT      = os.getenv("MAILRELAY_ACCOUNT", "")
MAILRELAY_SENDER_EMAIL = os.getenv("MAILRELAY_SENDER_EMAIL", "hello@steami.com")
MAILRELAY_SENDER_NAME  = os.getenv("MAILRELAY_SENDER_NAME", "STEAMI Newsletter")
MAILRELAY_API_BASE     = (
    f"https://{MAILRELAY_ACCOUNT}.ipzmarketing.com/api/v1"
    if MAILRELAY_ACCOUNT else ""
)
SITE_NAME = os.getenv("SITE_NAME", "STEAMI")
SITE_URL  = os.getenv("SITE_URL",  "https://steami.com")


# ── Helpers (lightweight copies — no FastAPI/router deps) ─────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%B %d, %Y")

def _mailrelay_headers() -> dict:
    return {
        "accept":       "application/json",
        "content-type": "application/json",
        "x-auth-token": MAILRELAY_API_KEY,
    }

def _send_one(to_email: str, to_name: str, subject: str, html_body: str) -> bool:
    payload = {
        "from":      {"email": MAILRELAY_SENDER_EMAIL, "name": MAILRELAY_SENDER_NAME},
        "to":        [{"email": to_email, "name": to_name or to_email}],
        "subject":   subject,
        "html_part": html_body,
    }
    try:
        resp = http.post(
            f"{MAILRELAY_API_BASE}/send_emails",
            headers=_mailrelay_headers(),
            json=payload,
            timeout=30,
        )
        if resp.status_code in (200, 201):
            log.info("MailRelay sent → %s", to_email)
            return True
        log.error("MailRelay failed %s: HTTP %d — %s", to_email, resp.status_code, resp.text[:300])
        return False
    except Exception as e:
        log.error("MailRelay exception %s: %s", to_email, e)
        return False

def _get_subscribers() -> list[dict]:
    """Pull active subscribers directly from Firestore (same as newsletter.py)."""
    try:
        from mongodb_client import db   # your existing Firestore client
        docs = (
            db.collection("newsletter_subscribers")
              .where("is_active", "==", True)
              .stream()
        )
        return [d.to_dict() for d in docs]
    except Exception as e:
        log.error("Could not fetch subscribers: %s", e)
        return []

def _get_articles(limit: int) -> list[dict]:
    """Fetch top AI-insight articles from Firestore."""
    try:
        from mongodb_client import db
        docs = (
            db.collection("articles")
              .where("has_insight", "==", True)
              .order_by("fetched_at", direction="DESCENDING")
              .limit(limit)
              .stream()
        )
        return [d.to_dict() for d in docs]
    except Exception as e:
        log.error("Could not fetch articles: %s", e)
        return []

def _build_html(articles: list[dict], recipient_name: str = "") -> str:
    """
    Minimal digest HTML — replicates the _build_old_digest_html pattern
    from newsletter.py without importing the whole router.
    """
    greeting = f"Hi {recipient_name}," if recipient_name else "Hi there,"
    items_html = ""
    for a in articles:
        title   = a.get("title", "Untitled")
        summary = a.get("ai_summary") or a.get("summary", "")
        url     = a.get("url", "#")
        source  = a.get("source_name", "")
        items_html += f"""
        <div style="margin-bottom:24px;padding-bottom:24px;border-bottom:1px solid #e0eaf5;">
          <h3 style="margin:0 0 6px;font-size:16px;">
            <a href="{url}" style="color:#1a5fa8;text-decoration:none;">{title}</a>
          </h3>
          {f'<p style="margin:0 0 6px;font-size:12px;color:#7a9dc8;">{source}</p>' if source else ""}
          <p style="margin:0;font-size:14px;color:#334;">{summary}</p>
        </div>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>{SITE_NAME} Daily Digest</title></head>
<body style="background:#f0f6ff;font-family:'DM Sans',Arial,sans-serif;margin:0;padding:32px 0;">
  <div style="max-width:620px;margin:0 auto;background:#fff;border-radius:12px;
              padding:32px;box-shadow:0 2px 12px rgba(30,80,160,.08);">
    <div style="text-align:center;margin-bottom:28px;">
      <span style="font-size:22px;font-weight:700;color:#1a5fa8;">{SITE_NAME}</span>
      <div style="font-size:13px;color:#7a9dc8;margin-top:4px;">{_today_str()}</div>
    </div>
    <p style="color:#334;font-size:15px;">{greeting}</p>
    <p style="color:#556;font-size:14px;margin-bottom:24px;">
      Here are today's top AI &amp; tech stories with insights:
    </p>
    {items_html}
    <p style="font-size:12px;color:#aaa;text-align:center;margin-top:24px;">
      You're receiving this because you subscribed at
      <a href="{SITE_URL}" style="color:#7a9dc8;">{SITE_URL}</a>.
    </p>
  </div>
</body></html>"""

def _log_send(sent: int, failed: int, total: int, articles: int, failed_emails: list):
    """Write a send log to Firestore (best-effort)."""
    try:
        from mongodb_client import db
        log_id = str(uuid.uuid4())
        db.collection("newsletter_logs").document(log_id).set({
            "log_id":              log_id,
            "sent_at":             _now(),
            "type":                "celery_auto_digest",
            "total_subscribers":   total,
            "emails_sent":         sent,
            "emails_failed":       failed,
            "articles_included":   articles,
            "failed_emails":       failed_emails,
        })
        return log_id
    except Exception as e:
        log.warning("Could not write newsletter log: %s", e)
        return None


# ═════════════════════════════════════════════════════════════════════════════
# CELERY TASK
# ═════════════════════════════════════════════════════════════════════════════

@celery.task(
    name="tasks.send_daily_newsletter",
    bind=True,
    max_retries=3,
    default_retry_delay=300,    # retry after 5 min on failure
)
def send_daily_newsletter(self, limit: int = 5):
    """
    Triggered daily at 14:00 IST (08:30 UTC) by Celery Beat.
    Fetches top-{limit} AI-insight articles and emails all active subscribers.
    """
    log.info("▶ send_daily_newsletter started (limit=%d)", limit)

    articles = _get_articles(limit)
    if not articles:
        log.warning("No articles found — skipping today's send.")
        return {"status": "skipped", "reason": "no_articles"}

    subscribers = _get_subscribers()
    if not subscribers:
        log.warning("No active subscribers — skipping today's send.")
        return {"status": "skipped", "reason": "no_subscribers"}

    subject = f"🔬 {SITE_NAME} Daily Digest — {_today_str()}"
    sent = failed = 0
    failed_emails = []

    for sub in subscribers:
        email = sub.get("email", "")
        name  = sub.get("name", "")
        if not email:
            continue
        html = _build_html(articles, recipient_name=name)
        if _send_one(email, name, subject, html):
            sent += 1
        else:
            failed += 1
            failed_emails.append(email)

    log_id = _log_send(sent, failed, len(subscribers), len(articles), failed_emails)
    result = {
        "status":            "done",
        "sent":              sent,
        "failed":            failed,
        "total_subscribers": len(subscribers),
        "articles_included": len(articles),
        "log_id":            log_id,
    }
    log.info("✔ send_daily_newsletter done: %s", result)
    return result
